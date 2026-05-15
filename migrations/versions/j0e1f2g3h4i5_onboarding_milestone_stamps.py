"""onboarding milestone timestamps on users

Persist three onboarding milestones — when the user created their
first workspace, ran their first audit, and "completed" onboarding
(which today = first-audit moment). Live computation in
get_onboarding_state stays correct via fallback when these columns
are NULL on legacy rows; the columns let admin analytics measure
time-to-activation without joining and aggregating Client/Audit
tables.

Backfill: best-effort for existing users. Uses the earliest
Client.created_at as first_workspace_at and the earliest Audit.saved_at
as first_audit_at / onboarding_completed_at. Saved_at is a string
column (ISO 8601), so we cast to datetime in Python after the SELECT
rather than in SQL — keeps the migration portable across SQLite
(tests) and Postgres (prod).

Revision ID: j0e1f2g3h4i5
Revises: i9d0e1f2g3h4
Create Date: 2026-05-15 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from datetime import datetime


# revision identifiers, used by Alembic.
revision = 'j0e1f2g3h4i5'
down_revision = 'i9d0e1f2g3h4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'first_workspace_at', sa.DateTime(), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'first_audit_at', sa.DateTime(), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'onboarding_completed_at', sa.DateTime(), nullable=True,
        ))

    # Best-effort backfill. Skip silently if the schema doesn't have
    # the source tables yet (fresh installs hitting all migrations in
    # sequence have empty users + clients + audits tables; the loop
    # below just does nothing in that case).
    bind = op.get_bind()
    try:
        users = bind.execute(sa.text("SELECT id FROM users")).fetchall()
    except Exception:
        users = []

    for (user_id,) in users:
        # First workspace timestamp — min(created_at) on the Client
        # rows the user owns.
        ws_row = bind.execute(
            sa.text(
                "SELECT MIN(created_at) FROM clients WHERE user_id = :uid"
            ),
            {"uid": user_id},
        ).scalar()
        if ws_row:
            bind.execute(
                sa.text(
                    "UPDATE users SET first_workspace_at = :ts "
                    "WHERE id = :uid"
                ),
                {"uid": user_id, "ts": ws_row},
            )

        # First audit — min(saved_at) on the audits table. saved_at
        # is a string column (ISO 8601); pull it as text and parse
        # in Python so SQLite + Postgres behave the same way.
        audit_row = bind.execute(
            sa.text(
                "SELECT MIN(saved_at) FROM audits WHERE user_id = :uid"
            ),
            {"uid": user_id},
        ).scalar()
        if audit_row:
            try:
                # saved_at may be 'YYYY-MM-DDTHH:MM:SS' or with seconds
                # truncated. fromisoformat handles both.
                ts = datetime.fromisoformat(audit_row.replace("Z", ""))
                bind.execute(
                    sa.text(
                        "UPDATE users SET first_audit_at = :ts, "
                        "onboarding_completed_at = :ts WHERE id = :uid"
                    ),
                    {"uid": user_id, "ts": ts},
                )
            except (ValueError, AttributeError):
                # Malformed legacy timestamp — skip rather than crash
                # the whole migration. The column stays NULL and the
                # live get_onboarding_state fallback handles it.
                pass


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('onboarding_completed_at')
        batch_op.drop_column('first_audit_at')
        batch_op.drop_column('first_workspace_at')
