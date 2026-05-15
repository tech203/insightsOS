"""email_marketing_opt_out_at — track unsubscribed users

When a user clicks the unsubscribe link inside a marketing email
(the LTO nudge today; presumably more in the future), we stamp
this timestamp on their row. `can_send_marketing_email(user)`
checks it before sending. Transactional emails (password reset,
email verification, team invites) are unaffected — those are
service emails, exempt from CAN-SPAM commercial-content rules and
required for the user to operate their account.

Revision ID: i9d0e1f2g3h4
Revises: h8c9d0e1f2g3
Create Date: 2026-05-14 16:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'i9d0e1f2g3h4'
down_revision = 'h8c9d0e1f2g3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'email_marketing_opt_out_at',
            sa.DateTime(),
            nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('email_marketing_opt_out_at')
