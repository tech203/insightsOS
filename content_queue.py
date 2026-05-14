"""
Content queue persistence — backed by the SQLAlchemy `QueueItem`
model (see app.py).

This module is the only place in the codebase that writes to or reads
from the queue. Callers (app.py routes, cleanup_queue_duplicates.py)
use the public functions defined here and expect dicts back, not ORM
rows — same shape as the legacy JSON-file scheme so templates and
existing call sites are unaffected by the storage swap.

History: before c3d4e5f6a7b8 the queue lived in
data/content_queue.json, loaded into memory on every read and
rewritten on every change. That worked at zero-scale but:
  - tests polluted the on-disk file every time they ran upsert
  - "filter by user_id" was a Python list comprehension, not a
    SQL WHERE clause
  - the admin activity log (PR #95) couldn't join the queue with
    anything else
  - concurrent writes raced on the JSON file with no locking
Migrating to SQL fixes all four.
"""

import uuid

from dtutils import utcnow


# Re-exported for back-compat — call sites that did
# `from content_queue import VALID_STATUSES` keep working.
VALID_STATUSES = {
    "pending",
    "in_progress",
    "brief_generated",
    "draft_generated",
    "ready",
    "published",
    "dismissed",
}

VALID_ITEM_TYPES = {"brief", "draft"}

VALID_PRIORITIES = {"low", "medium", "high"}

VALID_SOURCES = {
    "manual",
    "audit",
    "audit_opportunity",
    "query_ideas",
    "competitor_research",
    "prompt_tracking",
    # `generation` wasn't in the old VALID_SOURCES but
    # upsert_generation_item passes it; treat as valid.
    "generation",
}


# ---------------------------------------------------------------------------
# Normalization helpers — preserved verbatim from the JSON-era module
# since templates / consumers depend on these defaults.
# ---------------------------------------------------------------------------

def _now_iso():
    return utcnow().replace(microsecond=0).isoformat()


def _normalize_text(value, default=""):
    return (value or default).strip()


def _normalize_status(status):
    value = (status or "").strip().lower()
    return value if value in VALID_STATUSES else "pending"


def _normalize_item_type(item_type):
    value = (item_type or "").strip().lower()
    return value if value in VALID_ITEM_TYPES else "brief"


def _normalize_priority(priority):
    value = (priority or "").strip().lower()
    return value if value in VALID_PRIORITIES else "medium"


def _normalize_source(source):
    value = (source or "").strip().lower()
    return value if value in VALID_SOURCES else "manual"


def _normalize_int(value, default=0):
    try:
        if value is None:
            return default
        return int(round(float(value)))
    except Exception:
        return default


def _normalize_scheduled_for(value):
    """Accepts an ISO date string ('YYYY-MM-DD') or empty/None."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:10]


# ---------------------------------------------------------------------------
# Model lookup — lazy import so this module stays importable without a
# Flask app context (test stubs, migrations, etc.).
# ---------------------------------------------------------------------------

def _model():
    """Return (QueueItem, db) from the app module. Lazy import to
    avoid a circular dependency at module load time."""
    from app import QueueItem, db
    return QueueItem, db


def _serialize(row):
    """Convert a QueueItem ORM row to the dict shape that all callers
    were written against. Returning a dict instead of the ORM object
    means templates don't accidentally trigger lazy DB hits when they
    iterate attributes, and means the swap was a true drop-in
    replacement for the JSON-era API."""
    if row is None:
        return None
    return {
        "id": row.id,
        "user_id": row.user_id,
        "client_id": row.client_id,
        "client_name": row.client_name or "",
        "target_query": row.target_query or "",
        "content_type": row.content_type or "",
        "item_type": row.item_type,
        "title": row.title,
        "content": row.content or "",
        "status": row.status,
        "priority": row.priority,
        "source": row.source,
        "credits_required": row.credits_required or 0,
        "execution_type": row.execution_type or "ai_executable",
        "source_action_title": row.source_action_title or "",
        "scheduled_for": row.scheduled_for,
        "webflow_item_id": row.webflow_item_id,
        "webflow_collection": row.webflow_collection,
        "webflow_live_url": row.webflow_live_url,
        "og_image_url": row.og_image_url,
        "chat_history": list(row.chat_history) if isinstance(row.chat_history, list) else [],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _row_by_id(item_id, user_id=None):
    """Internal: return the ORM row for `item_id`, optionally scoped to
    a user. Returns None if no match or the user_id doesn't match."""
    QueueItem, _db = _model()
    row = QueueItem.query.filter_by(id=item_id).first()
    if not row:
        return None
    if user_id is not None and row.user_id != user_id:
        return None
    return row


# ---------------------------------------------------------------------------
# Bulk loads — used by handful of legacy callers; kept for back-compat.
# Most code paths should use get_queue_items() with filters instead.
# ---------------------------------------------------------------------------

def load_queue_items():
    """Return every queue item as a list of dicts, newest first.

    Kept for back-compat with the JSON-era API. New call sites should
    prefer get_queue_items() with filters — load_queue_items() pulls
    every row in the table, which doesn't scale beyond the no-load
    JSON era.
    """
    QueueItem, _db = _model()
    rows = QueueItem.query.order_by(QueueItem.created_at.desc()).all()
    return [_serialize(r) for r in rows]


def save_queue_items(items):
    """Replace the entire queue with `items`. Destructive — wipes
    every existing row and recreates from the supplied list.

    Kept for back-compat with cleanup_queue_duplicates.py which used
    to read the whole file, dedupe in memory, and write it back. SQL-
    native cleanup paths should use targeted UPDATE/DELETE; we keep
    this here as a transitional bridge.
    """
    QueueItem, db = _model()
    # Wipe and rebuild. Same destructive semantics as the JSON-file
    # version: caller is responsible for passing the full new state.
    db.session.query(QueueItem).delete()
    for raw in items or []:
        row = _new_row_from_dict(raw)
        db.session.add(row)
    db.session.commit()


def _new_row_from_dict(raw):
    """Build a fresh QueueItem row from a dict, applying the same
    field defaults that _normalize_item did in the JSON era."""
    QueueItem, _db = _model()
    now = _now_iso()
    return QueueItem(
        id=raw.get("id") or str(uuid.uuid4()),
        user_id=raw.get("user_id"),
        client_id=str(raw["client_id"]) if raw.get("client_id") not in (None, "") else None,
        client_name=_normalize_text(raw.get("client_name")),
        target_query=_normalize_text(raw.get("target_query")),
        content_type=_normalize_text(raw.get("content_type")),
        item_type=_normalize_item_type(raw.get("item_type", "brief")),
        title=_normalize_text(raw.get("title"), "Untitled Item"),
        content=_normalize_text(raw.get("content")),
        status=_normalize_status(raw.get("status", "pending")),
        priority=_normalize_priority(raw.get("priority", "medium")),
        source=_normalize_source(raw.get("source", "manual")),
        credits_required=_normalize_int(raw.get("credits_required")),
        execution_type=_normalize_text(raw.get("execution_type"), "ai_executable"),
        source_action_title=_normalize_text(raw.get("source_action_title")),
        scheduled_for=_normalize_scheduled_for(raw.get("scheduled_for")),
        webflow_item_id=_normalize_text(raw.get("webflow_item_id")) or None,
        webflow_collection=_normalize_text(raw.get("webflow_collection")) or None,
        webflow_live_url=_normalize_text(raw.get("webflow_live_url")) or None,
        og_image_url=_normalize_text(raw.get("og_image_url")) or None,
        chat_history=raw.get("chat_history") if isinstance(raw.get("chat_history"), list) else [],
        created_at=raw.get("created_at") or now,
        updated_at=raw.get("updated_at") or raw.get("created_at") or now,
    )


# ---------------------------------------------------------------------------
# Public CRUD API — same signatures as the JSON-era module so app.py
# and other callers don't need to change.
# ---------------------------------------------------------------------------

def add_queue_item(
    client_id,
    client_name,
    target_query,
    content_type,
    item_type,
    title,
    content="",
    status="pending",
    priority="medium",
    source="manual",
    credits_required=0,
    execution_type="ai_executable",
    source_action_title="",
    scheduled_for=None,
    user_id=None,
):
    """Insert a new queue item. Returns the created item as a dict."""
    QueueItem, db = _model()
    now = _now_iso()
    row = QueueItem(
        id=str(uuid.uuid4()),
        user_id=user_id,
        client_id=str(client_id) if client_id not in (None, "") else None,
        client_name=_normalize_text(client_name),
        target_query=_normalize_text(target_query),
        content_type=_normalize_text(content_type),
        item_type=_normalize_item_type(item_type),
        title=_normalize_text(title, "Untitled Item"),
        content=_normalize_text(content),
        status=_normalize_status(status),
        priority=_normalize_priority(priority),
        source=_normalize_source(source),
        credits_required=_normalize_int(credits_required),
        execution_type=_normalize_text(execution_type, "ai_executable"),
        source_action_title=_normalize_text(source_action_title),
        scheduled_for=_normalize_scheduled_for(scheduled_for),
        chat_history=[],
        created_at=now,
        updated_at=now,
    )
    db.session.add(row)
    db.session.commit()
    return _serialize(row)


def get_queue_items(
    client_id=None,
    user_id=None,
    status=None,
    item_type=None,
    priority=None,
    source=None,
    include_dismissed=False,
):
    """List queue items, optionally filtered. Newest first.

    All filters translate to indexed SQL WHERE clauses now (vs the
    previous full-list-comprehension scan), so this is order-of-
    magnitude faster on real-world data.
    """
    QueueItem, _db = _model()
    q = QueueItem.query

    if user_id is not None:
        q = q.filter_by(user_id=user_id)
    if client_id is not None and client_id != "":
        q = q.filter_by(client_id=str(client_id))
    if status:
        q = q.filter_by(status=_normalize_status(status))
    elif not include_dismissed:
        # Hide dismissed items from default listings.
        q = q.filter(QueueItem.status != "dismissed")
    if item_type:
        q = q.filter_by(item_type=_normalize_item_type(item_type))
    if priority:
        q = q.filter_by(priority=_normalize_priority(priority))
    if source:
        q = q.filter_by(source=_normalize_source(source))

    rows = q.order_by(QueueItem.created_at.desc()).all()
    return [_serialize(r) for r in rows]


def get_queue_item_by_id(item_id, user_id=None):
    """Fetch one queue item. Returns None if not found or the user_id
    doesn't match (defense-in-depth — routes also auth via
    login_required + the workspace's user_id)."""
    return _serialize(_row_by_id(item_id, user_id=user_id))


# ---------------------------------------------------------------------------
# Single-field updates — mutation helpers
# ---------------------------------------------------------------------------

def update_queue_item_status(item_id, new_status, user_id=None):
    QueueItem, db = _model()
    row = _row_by_id(item_id, user_id=user_id)
    if not row:
        return None
    row.status = _normalize_status(new_status)
    row.updated_at = _now_iso()
    db.session.commit()
    return _serialize(row)


def update_queue_item_schedule(item_id, scheduled_for, user_id=None):
    """Set or clear the scheduled_for date on a queue item."""
    QueueItem, db = _model()
    row = _row_by_id(item_id, user_id=user_id)
    if not row:
        return None
    row.scheduled_for = _normalize_scheduled_for(scheduled_for)
    row.updated_at = _now_iso()
    db.session.commit()
    return _serialize(row)


def update_queue_item_content(
    item_id,
    content=None,
    title=None,
    status=None,
    priority=None,
    source=None,
    credits_required=None,
    execution_type=None,
    source_action_title=None,
    user_id=None,
):
    QueueItem, db = _model()
    row = _row_by_id(item_id, user_id=user_id)
    if not row:
        return None

    if content is not None:
        row.content = _normalize_text(content)
    if title is not None:
        row.title = _normalize_text(title, row.title or "Untitled Item")
    if status is not None:
        row.status = _normalize_status(status)
    if priority is not None:
        row.priority = _normalize_priority(priority)
    if source is not None:
        row.source = _normalize_source(source)
    if credits_required is not None:
        row.credits_required = _normalize_int(credits_required)
    if execution_type is not None:
        row.execution_type = _normalize_text(execution_type, "ai_executable")
    if source_action_title is not None:
        row.source_action_title = _normalize_text(source_action_title)

    row.updated_at = _now_iso()
    db.session.commit()
    return _serialize(row)


def update_queue_item_details(
    item_id,
    title=None,
    target_query=None,
    content_type=None,
    item_type=None,
    priority=None,
    source=None,
    user_id=None,
):
    QueueItem, db = _model()
    row = _row_by_id(item_id, user_id=user_id)
    if not row:
        return None

    if title is not None:
        row.title = _normalize_text(title, row.title or "Untitled Item")
    if target_query is not None:
        row.target_query = _normalize_text(target_query)
    if content_type is not None:
        row.content_type = _normalize_text(content_type, row.content_type or "service_page")
    if item_type is not None:
        row.item_type = _normalize_item_type(item_type)
    if priority is not None:
        row.priority = _normalize_priority(priority)
    if source is not None:
        row.source = _normalize_source(source)

    row.updated_at = _now_iso()
    db.session.commit()
    return _serialize(row)


def update_queue_item_og_image(item_id, og_image_url, user_id=None):
    """Record the URL of a generated visual on a queue item."""
    QueueItem, db = _model()
    row = _row_by_id(item_id, user_id=user_id)
    if not row:
        return None
    row.og_image_url = _normalize_text(og_image_url) or None
    row.updated_at = _now_iso()
    db.session.commit()
    return _serialize(row)


def update_queue_item_webflow_export(
    item_id,
    webflow_item_id,
    webflow_collection,
    webflow_live_url=None,
    user_id=None,
):
    """Record that a queue item was successfully exported to Webflow."""
    QueueItem, db = _model()
    row = _row_by_id(item_id, user_id=user_id)
    if not row:
        return None
    row.webflow_item_id = _normalize_text(webflow_item_id) or None
    row.webflow_collection = _normalize_text(webflow_collection) or None
    if webflow_live_url is not None:
        row.webflow_live_url = _normalize_text(webflow_live_url) or None
    row.status = "published"
    row.updated_at = _now_iso()
    db.session.commit()
    return _serialize(row)


# ---------------------------------------------------------------------------
# Chat history mutators
# ---------------------------------------------------------------------------

def append_queue_item_chat_messages(item_id, messages, user_id=None):
    """Append one or more chat messages to a queue item's chat_history.

    Each message is a dict shaped like:
      {"role": "user"|"assistant", "content": str,
       "revised_content": str|None, "summary": str|None, "ts": iso}
    """
    QueueItem, db = _model()
    row = _row_by_id(item_id, user_id=user_id)
    if not row:
        return None

    history = list(row.chat_history) if isinstance(row.chat_history, list) else []
    for m in messages or []:
        if isinstance(m, dict):
            history.append(m)
    row.chat_history = history
    row.updated_at = _now_iso()
    db.session.commit()
    return _serialize(row)


def clear_queue_item_chat_history(item_id, user_id=None):
    QueueItem, db = _model()
    row = _row_by_id(item_id, user_id=user_id)
    if not row:
        return None
    row.chat_history = []
    row.updated_at = _now_iso()
    db.session.commit()
    return _serialize(row)


# ---------------------------------------------------------------------------
# Upsert from generation flow
# ---------------------------------------------------------------------------

def upsert_generation_item(
    *,
    client_id,
    client_name,
    target_query,
    content_type,
    item_type,
    status,
    content,
    title,
    user_id,
    credits_required=0,
):
    """Persist a generated brief or draft into the queue, reusing
    any existing item that matches on (user_id, client_id,
    target_query, content_type) instead of creating duplicates.

    The previous flow rendered the result page without persisting
    anything — users had to click a separate "Save to Queue" button
    or the brief / draft was silently lost when they clicked
    "Generate Draft" or navigated away. This helper closes that
    loop: every generation immediately materialises a queue item,
    so the result page can act as that item's detail view and
    "Generate Draft" can advance the same item forward instead of
    spawning a parallel one. (Audit finding #5.)

    Returns the created or updated item as a dict.
    """
    QueueItem, db = _model()

    normalized_query = _normalize_text(target_query)
    normalized_type = _normalize_text(content_type)
    normalized_status = _normalize_status(status)
    client_id_s = str(client_id) if client_id not in (None, "") else None

    # Find an existing item that matches on (user, client, query).
    # content_type match is preferred but not required — re-running
    # the generator without picking a type should advance the same
    # row, not spawn a duplicate.
    q = QueueItem.query.filter_by(
        user_id=user_id,
        client_id=client_id_s,
    )
    candidates = q.all()
    match = None
    for row in candidates:
        if _normalize_text(row.target_query) != normalized_query:
            continue
        existing_type = _normalize_text(row.content_type)
        if normalized_type and existing_type and existing_type != normalized_type:
            continue
        match = row
        break

    if match:
        match.target_query = normalized_query
        match.content_type = normalized_type or match.content_type or ""
        match.item_type = _normalize_item_type(item_type)
        match.title = _normalize_text(title, match.title or "Untitled Item")
        match.content = _normalize_text(content)
        match.status = normalized_status
        match.updated_at = _now_iso()
        db.session.commit()
        return _serialize(match)

    return add_queue_item(
        client_id=client_id,
        client_name=client_name,
        target_query=normalized_query,
        content_type=normalized_type,
        item_type=item_type,
        title=title,
        content=content,
        status=normalized_status,
        priority="medium",
        source="generation",
        credits_required=credits_required,
        execution_type="ai_executable",
        user_id=user_id,
    )


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def delete_queue_item(item_id, user_id=None):
    QueueItem, db = _model()
    row = _row_by_id(item_id, user_id=user_id)
    if not row:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


def delete_items_for_client(client_id, user_id=None):
    """Delete every queue item belonging to `client_id` (and
    optionally scoped to a user). Returns the number deleted."""
    QueueItem, db = _model()
    q = QueueItem.query.filter_by(client_id=str(client_id))
    if user_id is not None:
        q = q.filter_by(user_id=user_id)
    rows = q.all()
    count = len(rows)
    for r in rows:
        db.session.delete(r)
    db.session.commit()
    return count


# ---------------------------------------------------------------------------
# Pure helpers — no DB access
# ---------------------------------------------------------------------------

def get_next_action(item):
    """Return the next user-visible action for a queue item, or None
    if no further action is available from its current status. Pure
    function; doesn't touch the DB."""
    if not item:
        return None

    status = item.get("status")

    if status == "pending":
        return {
            "label": "Generate Brief",
            "action": "generate_brief",
            "url": f"/generate-brief/{item['id']}",
        }

    if status == "brief_generated":
        return {
            "label": "Generate Draft",
            "action": "generate_draft",
            "url": f"/generate-draft/{item['id']}",
        }

    if status == "draft_generated":
        return {
            "label": "Approve Draft",
            "action": "approve",
            "url": f"/content-queue/{item['id']}/approve",
        }

    if status == "ready":
        return {
            "label": "Publish",
            "action": "publish",
            "url": f"/content-queue/{item['id']}/publish",
        }

    return None


# Transitions allowed by the human-in-the-loop approval gate.
# `approve` moves a draft into "ready" (a held state awaiting publish).
# `publish` is only valid from "ready" — never directly from "draft_generated".
APPROVAL_TRANSITIONS = {
    "approve": {"from": {"draft_generated"}, "to": "ready"},
    "publish": {"from": {"ready"}, "to": "published"},
    "unapprove": {"from": {"ready"}, "to": "draft_generated"},
}


def transition_queue_item(item_id, transition, user_id=None):
    """Apply a named approval transition. Returns (item, error_message)."""
    rule = APPROVAL_TRANSITIONS.get(transition)
    if not rule:
        return None, f"Unknown transition: {transition}"

    item = get_queue_item_by_id(item_id, user_id=user_id)
    if not item:
        return None, "Queue item not found."

    current = (item.get("status") or "").lower()
    if current not in rule["from"]:
        return None, (
            f"Cannot {transition} from status '{current}'. "
            f"Expected one of: {sorted(rule['from'])}."
        )

    updated = update_queue_item_status(item_id, rule["to"], user_id=user_id)
    return updated, None


def get_client_progress(client_id, user_id=None):
    """Aggregate counts for a client's queue. Used by the workspace
    detail page's "progress" pill."""
    items = get_queue_items(client_id=client_id, user_id=user_id)

    total = len(items)
    completed = len([item for item in items if item.get("status") == "published"])
    progress_pct = int((completed / total) * 100) if total else 0

    return {
        "client_id": client_id,
        "total": total,
        "completed": completed,
        "remaining": total - completed,
        "progress_pct": progress_pct,
    }


def create_queue_item_from_audit_opportunity(client_id, client_name, opportunity, user_id=None):
    """Helper to turn an audit opportunity dict into a queue item."""
    return add_queue_item(
        client_id=client_id,
        client_name=client_name,
        target_query=opportunity.get("target_query", opportunity.get("title", "")),
        content_type=opportunity.get("content_type", "article"),
        item_type="brief",
        title=opportunity.get("title", "Untitled Opportunity"),
        content="",
        status="pending",
        priority=opportunity.get("priority", "medium"),
        source="audit",
        credits_required=opportunity.get("credits_required", 0),
        execution_type=opportunity.get("execution_type", "ai_executable"),
        source_action_title=opportunity.get("source_action_title", ""),
        user_id=user_id,
    )
