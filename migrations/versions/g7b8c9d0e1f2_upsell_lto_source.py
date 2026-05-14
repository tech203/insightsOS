"""upsell_lto_source — record the paywall that triggered qualification

Each record_upsell_prompt() call passes a `source` tag (workspace_cap,
insufficient_credits, gsc_dashboard_gate, …). Until now we only logged
it. Storing it on the qualifying call lets the admin funnel view break
down conversions by surface and lets the modal personalize its copy.

Revision ID: g7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-14 15:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'g7b8c9d0e1f2'
down_revision = 'f6a7b8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'upsell_lto_source',
            sa.String(length=60),
            nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('upsell_lto_source')
