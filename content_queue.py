import os
import json
import uuid
from datetime import datetime

QUEUE_FILE = os.path.join("data", "content_queue.json")

VALID_STATUSES = {"new", "in_progress", "done"}
VALID_ITEM_TYPES = {"brief", "draft"}


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


def _normalize_status(status):
    value = (status or "").strip().lower()
    return value if value in VALID_STATUSES else "new"


def _normalize_item_type(item_type):
    value = (item_type or "").strip().lower()
    return value if value in VALID_ITEM_TYPES else "brief"


def _normalize_text(value, default=""):
    return (value or default).strip()


def _normalize_item(raw):
    return {
        "id": raw.get("id", str(uuid.uuid4())),
        "client_id": raw.get("client_id"),
        "client_name": raw.get("client_name", ""),
        "target_query": raw.get("target_query", ""),
        "content_type": raw.get("content_type", ""),
        "item_type": _normalize_item_type(raw.get("item_type", "brief")),
        "title": raw.get("title", ""),
        "content": raw.get("content", ""),
        "status": _normalize_status(raw.get("status", "new")),
        "user_id": raw.get("user_id"),
        "created_at": raw.get("created_at", _now_iso()),
        "updated_at": raw.get("updated_at", raw.get("created_at", _now_iso())),
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
    content,
    status="new",
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
        "user_id": user_id,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }

    items.append(new_item)
    save_queue_items(items)
    return new_item


def get_queue_items(client_id=None, user_id=None, status=None, item_type=None):
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


def update_queue_item_content(item_id, content=None, title=None, status=None, user_id=None):
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
        same_user = (user_id is None or item.get("user_id") == user_id)

        if same_client and same_user:
            deleted_count += 1
            continue

        remaining.append(item)

    save_queue_items(remaining)
    return deleted_count