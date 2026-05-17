"""add webflow_imported_items table

Revision ID: b3e8d1a6f720
Revises: a1f7c2d9b4e0
Create Date: 2026-05-17 00:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3e8d1a6f720'
down_revision = 'a1f7c2d9b4e0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'webflow_imported_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('connection_id', sa.Integer(), nullable=False),
        sa.Column('collection_id', sa.String(length=100), nullable=False),
        sa.Column('collection_kind', sa.String(length=40), nullable=False),
        sa.Column('webflow_item_id', sa.String(length=100), nullable=False),
        sa.Column('name', sa.String(length=300), nullable=True),
        sa.Column('slug', sa.String(length=300), nullable=True),
        sa.Column('is_draft', sa.Boolean(), nullable=True),
        sa.Column('fields_json', sa.JSON(), nullable=True),
        sa.Column('analysis_json', sa.JSON(), nullable=True),
        sa.Column('visibility_score', sa.Float(), nullable=True),
        sa.Column('analyzed_at', sa.DateTime(), nullable=True),
        sa.Column('last_applied_at', sa.DateTime(), nullable=True),
        sa.Column('last_action', sa.String(length=40), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['connection_id'], ['webflow_connections.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'user_id', 'webflow_item_id', name='uq_webflow_item_user_item'
        ),
    )
    with op.batch_alter_table('webflow_imported_items', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_webflow_imported_items_user_id'),
            ['user_id'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_webflow_imported_items_connection_id'),
            ['connection_id'], unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_webflow_imported_items_webflow_item_id'),
            ['webflow_item_id'], unique=False,
        )


def downgrade():
    with op.batch_alter_table('webflow_imported_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_webflow_imported_items_webflow_item_id'))
        batch_op.drop_index(batch_op.f('ix_webflow_imported_items_connection_id'))
        batch_op.drop_index(batch_op.f('ix_webflow_imported_items_user_id'))
    op.drop_table('webflow_imported_items')
