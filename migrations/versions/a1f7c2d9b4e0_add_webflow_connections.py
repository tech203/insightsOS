"""add webflow_connections table

Revision ID: a1f7c2d9b4e0
Revises: 75e0af213b5d
Create Date: 2026-05-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1f7c2d9b4e0'
down_revision = '75e0af213b5d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'webflow_connections',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=True),
        sa.Column('api_token', sa.Text(), nullable=False),
        sa.Column('site_id', sa.String(length=100), nullable=False),
        sa.Column('site_name', sa.String(length=255), nullable=True),
        sa.Column('page_collection_id', sa.String(length=100), nullable=True),
        sa.Column('blog_collection_id', sa.String(length=100), nullable=True),
        sa.Column('faq_collection_id', sa.String(length=100), nullable=True),
        sa.Column('service_collection_id', sa.String(length=100), nullable=True),
        sa.Column('location_collection_id', sa.String(length=100), nullable=True),
        sa.Column('publish_on_export', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('field_map', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'client_id', name='uq_webflow_conn_user_client'),
    )
    with op.batch_alter_table('webflow_connections', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_webflow_connections_user_id'), ['user_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_webflow_connections_client_id'), ['client_id'], unique=False
        )


def downgrade():
    with op.batch_alter_table('webflow_connections', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webflow_connections_client_id'))
        batch_op.drop_index(batch_op.f('ix_webflow_connections_user_id'))
    op.drop_table('webflow_connections')
