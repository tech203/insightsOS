"""upsell_tracking — count paywall encounters, gate LTO popup

Adds four columns to `users` for the limited-time-offer upsell
flow. `record_upsell_prompt()` in app.py increments the count each
time a Free user bumps into a paid-feature paywall; once they
cross UPSELL_PROMPT_THRESHOLD, the LTO modal qualifies and an
expiry timestamp is set so the offer is genuinely time-bounded.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-14 14:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    # batch_alter_table for SQLite compat — production runs Postgres
    # but tests + dev use SQLite, which can't ALTER columns in place.
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'upsell_prompt_count',
            sa.Integer(), nullable=False, server_default='0',
        ))
        batch_op.add_column(sa.Column(
            'upsell_lto_status',
            sa.String(length=20), nullable=False, server_default='none',
        ))
        batch_op.add_column(sa.Column(
            'upsell_lto_offered_at',
            sa.DateTime(), nullable=True,
        ))
        batch_op.add_column(sa.Column(
            'upsell_lto_expires_at',
            sa.DateTime(), nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('upsell_lto_expires_at')
        batch_op.drop_column('upsell_lto_offered_at')
        batch_op.drop_column('upsell_lto_status')
        batch_op.drop_column('upsell_prompt_count')
