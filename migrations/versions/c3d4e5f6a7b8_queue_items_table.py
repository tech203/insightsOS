"""queue_items table — migrate content queue from JSON file to SQL

Replaces the JSON-list-of-dicts persistence at data/content_queue.json
with a real SQL table. See app.py QueueItem model for the column
rationale.

Data migration: reads any existing data/content_queue.json file and
inserts its rows into queue_items so an upgrade doesn't lose anyone's
queued briefs / drafts. The JSON file is left in place (renamed to
.bak by ops if they want to clean up) — the upgrade is idempotent.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-13 12:00:00.000000

"""
import json
import os
import uuid

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6a7b8'
# Chained after d4e5f6a7b8c9 (audits → SQL, merged as PR #104) so
# the Alembic chain stays linear. Originally branched off
# b2c3d4e5f6a7 alongside the audits migration; rebased to chain.
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


# Mirrors app.py's QueueItem.__tablename__ + column shape. Kept in
# sync by hand here so Alembic can run without importing the model
# (which would force a Flask app context).
TABLE_NAME = "queue_items"

JSON_FILE = os.path.join("data", "content_queue.json")


def upgrade():
    op.create_table(
        TABLE_NAME,
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('client_id', sa.String(length=255), nullable=True),
        sa.Column('client_name', sa.String(length=255), nullable=True),
        sa.Column('target_query', sa.Text(), nullable=True),
        sa.Column('content_type', sa.String(length=80), nullable=True),
        sa.Column('item_type', sa.String(length=20), nullable=False, server_default='brief'),
        sa.Column('title', sa.String(length=500), nullable=False, server_default='Untitled Item'),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=40), nullable=False, server_default='pending'),
        sa.Column('priority', sa.String(length=20), nullable=False, server_default='medium'),
        sa.Column('source', sa.String(length=40), nullable=False, server_default='manual'),
        sa.Column('credits_required', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('execution_type', sa.String(length=40), nullable=False, server_default='ai_executable'),
        sa.Column('source_action_title', sa.String(length=500), nullable=True),
        sa.Column('scheduled_for', sa.String(length=10), nullable=True),
        sa.Column('webflow_item_id', sa.String(length=120), nullable=True),
        sa.Column('webflow_collection', sa.String(length=120), nullable=True),
        sa.Column('webflow_live_url', sa.String(length=500), nullable=True),
        sa.Column('og_image_url', sa.String(length=500), nullable=True),
        sa.Column('chat_history', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.String(length=40), nullable=False),
        sa.Column('updated_at', sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table(TABLE_NAME, schema=None) as batch_op:
        batch_op.create_index('ix_queue_items_user_id', ['user_id'])
        batch_op.create_index('ix_queue_items_client_id', ['client_id'])
        batch_op.create_index('ix_queue_items_status', ['status'])

    _import_from_json()


def _import_from_json():
    """Best-effort import of existing data/content_queue.json rows
    into the new table. Skips silently if the file is missing or
    malformed — keeps the schema migration unblocked even on fresh
    deploys with no prior data."""
    if not os.path.exists(JSON_FILE):
        return

    try:
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        return

    if not isinstance(rows, list):
        return

    conn = op.get_bind()
    table = sa.table(
        TABLE_NAME,
        sa.column("id", sa.String),
        sa.column("user_id", sa.Integer),
        sa.column("client_id", sa.String),
        sa.column("client_name", sa.String),
        sa.column("target_query", sa.Text),
        sa.column("content_type", sa.String),
        sa.column("item_type", sa.String),
        sa.column("title", sa.String),
        sa.column("content", sa.Text),
        sa.column("status", sa.String),
        sa.column("priority", sa.String),
        sa.column("source", sa.String),
        sa.column("credits_required", sa.Integer),
        sa.column("execution_type", sa.String),
        sa.column("source_action_title", sa.String),
        sa.column("scheduled_for", sa.String),
        sa.column("webflow_item_id", sa.String),
        sa.column("webflow_collection", sa.String),
        sa.column("webflow_live_url", sa.String),
        sa.column("og_image_url", sa.String),
        sa.column("chat_history", sa.JSON),
        sa.column("created_at", sa.String),
        sa.column("updated_at", sa.String),
    )

    inserts = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        # Coerce shapes to match the new column types. We're permissive
        # on missing fields — defaults at the SQL layer fill the gaps.
        try:
            inserts.append({
                "id": str(r.get("id") or uuid.uuid4()),
                "user_id": int(r["user_id"]) if r.get("user_id") not in (None, "") else None,
                "client_id": str(r["client_id"]) if r.get("client_id") not in (None, "") else None,
                "client_name": (r.get("client_name") or "")[:255] or None,
                "target_query": r.get("target_query") or None,
                "content_type": (r.get("content_type") or "")[:80] or None,
                "item_type": (r.get("item_type") or "brief")[:20],
                "title": (r.get("title") or "Untitled Item")[:500],
                "content": r.get("content") or None,
                "status": (r.get("status") or "pending")[:40],
                "priority": (r.get("priority") or "medium")[:20],
                "source": (r.get("source") or "manual")[:40],
                "credits_required": int(r.get("credits_required") or 0),
                "execution_type": (r.get("execution_type") or "ai_executable")[:40],
                "source_action_title": (r.get("source_action_title") or "")[:500] or None,
                "scheduled_for": (r.get("scheduled_for") or None),
                "webflow_item_id": (r.get("webflow_item_id") or "")[:120] or None,
                "webflow_collection": (r.get("webflow_collection") or "")[:120] or None,
                "webflow_live_url": (r.get("webflow_live_url") or "")[:500] or None,
                "og_image_url": (r.get("og_image_url") or "")[:500] or None,
                "chat_history": r.get("chat_history") if isinstance(r.get("chat_history"), list) else None,
                "created_at": (r.get("created_at") or "")[:40] or _fallback_now(),
                "updated_at": (r.get("updated_at") or r.get("created_at") or "")[:40] or _fallback_now(),
            })
        except Exception:
            # Skip rows with unparseable shapes; ops can re-import
            # manually after the migration if needed.
            continue

    if inserts:
        conn.execute(table.insert(), inserts)


def _fallback_now() -> str:
    """ISO-8601 UTC timestamp for rows that came in without one. Local
    helper instead of dtutils so this migration stays runnable without
    a Flask context."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def downgrade():
    with op.batch_alter_table(TABLE_NAME, schema=None) as batch_op:
        batch_op.drop_index('ix_queue_items_status')
        batch_op.drop_index('ix_queue_items_client_id')
        batch_op.drop_index('ix_queue_items_user_id')
    op.drop_table(TABLE_NAME)
