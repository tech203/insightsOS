"""upsell_lto_email_sent_at — track whether the LTO email was sent

When a Free user crosses the prompt threshold we now both show the
in-app modal AND fire a one-shot transactional email so users who
close the tab still hear about the offer inside the 24h window.
This column is the idempotency guard — once set, we never resend.

Revision ID: h8c9d0e1f2g3
Revises: g7b8c9d0e1f2
Create Date: 2026-05-14 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'h8c9d0e1f2g3'
down_revision = 'g7b8c9d0e1f2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'upsell_lto_email_sent_at',
            sa.DateTime(),
            nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('upsell_lto_email_sent_at')
