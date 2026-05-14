"""job_runs table — backing store for background jobs

Adds the SQL table behind the new async bulk-audit flow. See app.py
JobRun model for column rationale.

Revision ID: e5f6a7b8c9d0
Revises: b2c3d4e5f6a7
Create Date: 2026-05-14 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f6a7b8c9d0'
# Chained after c3d4e5f6a7b8 (queue items → SQL, merged as PR #102),
# which itself chains after d4e5f6a7b8c9 (audits → SQL, PR #104).
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'job_runs',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=40), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='pending'),
        sa.Column('progress_current', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('progress_total', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.Column('started_at', sa.String(length=40), nullable=True),
        sa.Column('finished_at', sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.create_index('ix_job_runs_user_id', ['user_id'])
        batch_op.create_index('ix_job_runs_kind', ['kind'])
        batch_op.create_index('ix_job_runs_status', ['status'])


def downgrade():
    with op.batch_alter_table('job_runs', schema=None) as batch_op:
        batch_op.drop_index('ix_job_runs_status')
        batch_op.drop_index('ix_job_runs_kind')
        batch_op.drop_index('ix_job_runs_user_id')
    op.drop_table('job_runs')
