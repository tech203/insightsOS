import os
import json
import uuid
from datetime import datetime

QUEUE_FILE = os.path.join("data", "content_queue.json")

VALID_STATUSES = {
    "pending",
    "brief_generated",
    "draft_generated",
    "ready",
    "published",
}

VALID_ITEM_TYPES = {"brief", "draft"}

VALID_PRIORITIES = {"low", "medium", "high"}

VALID_SOURCES = {"manual", "audit"}


def _ensure_data_dir():
    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)


def _load_json_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def _safe_load_json(filepath, default):
    if not os.path.exists(filepath):
        return default
    try:
        return _load_json_file(filepath)
    except Exception:
        return default


def _save_json_file(filepath, payload):
    folder = os.path.dirname(filepath)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def _now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat()


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


def _normalize_item(raw):
    created_at = raw.get("created_at", _now_iso())

    return {
        "id": raw.get("id", str(uuid.uuid4())),
        "client_id": raw.get("client_id"),
        "client_name": _normalize_text(raw.get("client_name")),
        "target_query": _normalize_text(raw.get("target_query")),
        "content_type": _normalize_text(raw.get("content_type")),
        "item_type": _normalize_item_type(raw.get("item_type", "brief")),
        "title": _normalize_text(raw.get("title"), "Untitled Item"),
        "content": _normalize_text(raw.get("content")),
        "status": _normalize_status(raw.get("status", "pending")),
        "priority": _normalize_priority(raw.get("priority", "medium")),
        "source": _normalize_source(raw.get("source", "manual")),
        "user_id": raw.get("user_id"),
        "created_at": created_at,
        "updated_at": raw.get("updated_at", created_at),
    }


def load_queue_items():
    _ensure_data_dir()
    raw_items = _safe_load_json(QUEUE_FILE, [])
    if not isinstance(raw_items, list):
        raw_items = []

    items = [_normalize_item(item) for item in raw_items]
    return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)


def save_queue_items(items):
    normalized = [_normalize_item(item) for item in items]
    _save_json_file(QUEUE_FILE, normalized)


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
    user_id=None,
):
    items = load_queue_items()

    new_item = {
        "id": str(uuid.uuid4()),
        "client_id": client_id,
        "client_name": _normalize_text(client_name),
        "target_query": _normalize_text(target_query),
        "content_type": _normalize_text(content_type),
        "item_type": _normalize_item_type(item_type),
        "title": _normalize_text(title, "Untitled Item"),
        "content": _normalize_text(content),
        "status": _normalize_status(status),
        "priority": _normalize_priority(priority),
        "source": _normalize_source(source),
        "user_id": user_id,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }

    items.append(new_item)
    save_queue_items(items)
    return new_item


def get_queue_items(
    client_id=None,
    user_id=None,
    status=None,
    item_type=None,
    priority=None,
    source=None,
):
    items = load_queue_items()

    if user_id is not None:
        items = [item for item in items if item.get("user_id") == user_id]

    if client_id:
        items = [item for item in items if item.get("client_id") == client_id]

    if status:
        normalized_status = _normalize_status(status)
        items = [item for item in items if item.get("status") == normalized_status]

    if item_type:
        normalized_item_type = _normalize_item_type(item_type)
        items = [item for item in items if item.get("item_type") == normalized_item_type]

    if priority:
        normalized_priority = _normalize_priority(priority)
        items = [item for item in items if item.get("priority") == normalized_priority]

    if source:
        normalized_source = _normalize_source(source)
        items = [item for item in items if item.get("source") == normalized_source]

    return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)


def get_queue_item_by_id(item_id, user_id=None):
    items = load_queue_items()

    for item in items:
        if item.get("id") != item_id:
            continue
        if user_id is not None and item.get("user_id") != user_id:
            continue
        return item

    return None


def update_queue_item_status(item_id, new_status, user_id=None):
    items = load_queue_items()
    normalized_status = _normalize_status(new_status)

    updated_item = None
    for item in items:
        if item.get("id") != item_id:
            continue
        if user_id is not None and item.get("user_id") != user_id:
            continue

        item["status"] = normalized_status
        item["updated_at"] = _now_iso()
        updated_item = item
        break

    if not updated_item:
        return None

    save_queue_items(items)
    return updated_item


def update_queue_item_content(
    item_id,
    content=None,
    title=None,
    status=None,
    priority=None,
    source=None,
    user_id=None,
):
    items = load_queue_items()
    updated_item = None

    for item in items:
        if item.get("id") != item_id:
            continue
        if user_id is not None and item.get("user_id") != user_id:
            continue

        if content is not None:
            item["content"] = _normalize_text(content)

        if title is not None:
            item["title"] = _normalize_text(title, item.get("title", "Untitled Item"))

        if status is not None:
            item["status"] = _normalize_status(status)

        if priority is not None:
            item["priority"] = _normalize_priority(priority)

        if source is not None:
            item["source"] = _normalize_source(source)

        item["updated_at"] = _now_iso()
        updated_item = item
        break

    if not updated_item:
        return None

    save_queue_items(items)
    return updated_item


def delete_queue_item(item_id, user_id=None):
    items = load_queue_items()
    remaining = []
    deleted = False

    for item in items:
        if item.get("id") == item_id and (user_id is None or item.get("user_id") == user_id):
            deleted = True
            continue
        remaining.append(item)

    if not deleted:
        return False

    save_queue_items(remaining)
    return True


def delete_items_for_client(client_id, user_id=None):
    items = load_queue_items()
    remaining = []
    deleted_count = 0

    for item in items:
        same_client = item.get("client_id") == client_id
        same_user = user_id is None or item.get("user_id") == user_id

        if same_client and same_user:
            deleted_count += 1
            continue

        remaining.append(item)

    save_queue_items(remaining)
    return deleted_count


def get_next_action(item):
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
            "label": "Review / Publish",
            "action": "review_publish",
            "url": f"/content-queue",
        }

    if status == "ready":
        return {
            "label": "Publish",
            "action": "publish",
            "url": f"/content-queue",
        }

    return None


def get_client_progress(client_id, user_id=None):
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
    """
    Helper to turn an audit opportunity into a queue item.
    Expected opportunity example:
    {
        "title": "What is AEO for SMEs",
        "target_query": "what is aeo for small business",
        "content_type": "article",
        "priority": "high"
    }
    """
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
        user_id=user_id,
    )