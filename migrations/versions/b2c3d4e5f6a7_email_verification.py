"""email verification: users.email_verified_at + email_verification_tokens

Adds the schema for email verification (P2-8).

Policy:
- New signups create users with email_verified_at = NULL and receive a
  verification email. They can use the free tier immediately, but
  Stripe checkout is gated on email_verified_at being set.
- Existing users are backfilled to email_verified_at = COALESCE(
  created_at, now()) so the rollout doesn't suddenly lock anyone out
  of paid features.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-13 10:30:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def _table_exists(name):
    return name in sa_inspect(op.get_bind()).get_table_names()


def _column_exists(table, column):
    cols = [c['name'] for c in sa_inspect(op.get_bind()).get_columns(table)]
    return column in cols


def _index_exists(table, index_name):
    idxs = [i['name'] for i in sa_inspect(op.get_bind()).get_indexes(table)]
    return index_name in idxs


def upgrade():
    # ------------------------------------------------------------------
    # users.email_verified_at — NULL = not verified, timestamp = when.
    # Added as nullable so the column add itself doesn't fail; we
    # backfill existing rows below to grandfather them in.
    # Guard: column may already exist if db.create_all() ran first.
    # ------------------------------------------------------------------
    if not _column_exists('users', 'email_verified_at'):
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'email_verified_at', sa.DateTime(), nullable=True,
            ))

    # Backfill existing users so the launch doesn't suddenly lock them
    # out of Stripe checkout. Anyone who's been using the product
    # already proved their email works (they got the welcome flash,
    # presumably checked it at some point) — re-verifying them adds
    # friction without benefit.
    conn = op.get_bind()
    conn.execute(sa.text(
        "UPDATE users "
        "SET email_verified_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
        "WHERE email_verified_at IS NULL"
    ))

    # ------------------------------------------------------------------
    # email_verification_tokens — same shape as password_reset_tokens.
    # One-shot tokens; consumed at /verify-email/<token> by setting
    # used_at to the current timestamp. Expired tokens (>24h old) are
    # rejected by the route, swept lazily — there's no harm in leaving
    # them in the table since the unique constraint is on token, not
    # user_id.
    # Guard: table may already exist if db.create_all() ran first.
    # ------------------------------------------------------------------
    if not _table_exists('email_verification_tokens'):
        op.create_table(
            'email_verification_tokens',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('token', sa.String(length=80), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('used_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('token', name='uq_email_verification_tokens_token'),
        )
    if not _index_exists('email_verification_tokens', 'ix_email_verification_tokens_user_id'):
        with op.batch_alter_table('email_verification_tokens', schema=None) as batch_op:
            batch_op.create_index('ix_email_verification_tokens_user_id', ['user_id'])
            batch_op.create_index('ix_email_verification_tokens_token', ['token'])


def downgrade():
    with op.batch_alter_table('email_verification_tokens', schema=None) as batch_op:
        batch_op.drop_index('ix_email_verification_tokens_token')
        batch_op.drop_index('ix_email_verification_tokens_user_id')
    op.drop_table('email_verification_tokens')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('email_verified_at')
