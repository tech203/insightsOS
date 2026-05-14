"""P0 billing + credit safety: webhook idempotency, credit reservations, downgrade reconciliation

Adds infrastructure for three P0 fixes:

1. webhook_events table — dedupes Stripe webhook replays by event ID and
   records the last action taken. Wraps every event handler in
   stripe_webhook() so retries are safe.

2. credit_reservations table — implements two-phase commit for credit
   spending. spend_credits_for() debited the wallet *before* the AI
   call, so anything that killed the worker between deduct and refund
   (OOM, timeout, 502) silently consumed a credit. Reservations let us
   reserve → run → commit or release, with a sweeper for the worker-
   kill case.

3. users.payment_status / users.stripe_extra_workspace_sub_ids /
   users.stripe_extra_seat_sub_ids — tracks add-on Stripe subscription
   IDs so we can cancel them on downgrade, and surfaces dunning state
   to the user via a banner.

4. clients.is_locked — soft-locks workspaces that exceed the new plan
   cap after a downgrade. User picks which to keep active rather than
   losing data.

Revision ID: a1b2c3d4e5f6
Revises: fb26cf3a7639
Create Date: 2026-05-13 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'fb26cf3a7639'
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
    # webhook_events — idempotency table for Stripe webhook handler.
    # Stripe replays events on retry; the dispatcher inserts a row keyed
    # on event_id at the top of the handler and bails fast if it sees a
    # duplicate. event_type + status + processed_at help debug failures.
    # Guard: table may already exist if db.create_all() ran first.
    # ------------------------------------------------------------------
    if not _table_exists('webhook_events'):
        op.create_table(
            'webhook_events',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('event_id', sa.String(length=120), nullable=False),
            sa.Column('event_type', sa.String(length=120), nullable=False),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='processed'),
            sa.Column('user_id', sa.Integer(), nullable=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('received_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column('processed_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('event_id', name='uq_webhook_events_event_id'),
        )
    if not _index_exists('webhook_events', 'ix_webhook_events_event_type'):
        with op.batch_alter_table('webhook_events', schema=None) as batch_op:
            batch_op.create_index('ix_webhook_events_event_type', ['event_type'])

    # ------------------------------------------------------------------
    # credit_reservations — two-phase commit for credit spending.
    # status lifecycle: pending → committed | released | expired
    # expires_at lets a background sweeper release dead reservations
    # (worker killed mid-action, exception not caught, etc.).
    # Guard: table may already exist if db.create_all() ran first.
    # ------------------------------------------------------------------
    if not _table_exists('credit_reservations'):
        op.create_table(
            'credit_reservations',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('amount', sa.Integer(), nullable=False),
            sa.Column('action_key', sa.String(length=80), nullable=False),
            sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('finalized_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['user_id'], ['users.id']),
            sa.PrimaryKeyConstraint('id'),
        )
    if not _index_exists('credit_reservations', 'ix_credit_reservations_user_id'):
        with op.batch_alter_table('credit_reservations', schema=None) as batch_op:
            batch_op.create_index('ix_credit_reservations_user_id', ['user_id'])
            batch_op.create_index('ix_credit_reservations_status', ['status'])
            batch_op.create_index('ix_credit_reservations_expires_at', ['expires_at'])

    # ------------------------------------------------------------------
    # users.payment_status / payment_status_updated_at — dunning state.
    # ok       — happy path
    # past_due — invoice.payment_failed fired; Stripe is retrying; show
    #            an "update your card" banner.
    # canceled — subscription.deleted fired; user is back on free tier.
    # Guard: columns may already exist if db.create_all() ran first.
    # ------------------------------------------------------------------
    with op.batch_alter_table('users', schema=None) as batch_op:
        if not _column_exists('users', 'payment_status'):
            batch_op.add_column(sa.Column(
                'payment_status', sa.String(length=30),
                nullable=False, server_default='ok',
            ))
        if not _column_exists('users', 'payment_status_updated_at'):
            batch_op.add_column(sa.Column(
                'payment_status_updated_at', sa.DateTime(), nullable=True,
            ))
        # JSON list of Stripe subscription IDs for extra-workspace addons.
        # One ID per addon so downgrade can cancel each individually.
        if not _column_exists('users', 'stripe_extra_workspace_sub_ids'):
            batch_op.add_column(sa.Column(
                'stripe_extra_workspace_sub_ids', sa.JSON(), nullable=True,
            ))
        # Same shape for extra-seat addons.
        if not _column_exists('users', 'stripe_extra_seat_sub_ids'):
            batch_op.add_column(sa.Column(
                'stripe_extra_seat_sub_ids', sa.JSON(), nullable=True,
            ))

    # ------------------------------------------------------------------
    # clients.is_locked — soft-lock flag for workspaces over the plan
    # cap after a downgrade. Locked workspaces still exist in the DB
    # but are excluded from default listings and read-only in the UI.
    # User reactivates one (after deleting another) or re-upgrades.
    # ------------------------------------------------------------------
    with op.batch_alter_table('clients', schema=None) as batch_op:
        if not _column_exists('clients', 'is_locked'):
            batch_op.add_column(sa.Column(
                'is_locked', sa.Boolean(),
                nullable=False, server_default=sa.false(),
            ))


def downgrade():
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.drop_column('is_locked')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('stripe_extra_seat_sub_ids')
        batch_op.drop_column('stripe_extra_workspace_sub_ids')
        batch_op.drop_column('payment_status_updated_at')
        batch_op.drop_column('payment_status')

    with op.batch_alter_table('credit_reservations', schema=None) as batch_op:
        batch_op.drop_index('ix_credit_reservations_expires_at')
        batch_op.drop_index('ix_credit_reservations_status')
        batch_op.drop_index('ix_credit_reservations_user_id')
    op.drop_table('credit_reservations')

    with op.batch_alter_table('webhook_events', schema=None) as batch_op:
        batch_op.drop_index('ix_webhook_events_event_type')
    op.drop_table('webhook_events')
