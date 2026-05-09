"""
CMS publisher — pushes a content_queue item into a non-Webflow CMS.

The Webflow publish path lives in app.publish_queue_item_to_webflow
because it predates the modules system and has its own field-routing
logic. This module is the parallel for the Module 2 connectors that
were added later: Wix and Framer.

Each publish helper:
  - takes a connection model + queue item dict + target collection ID
  - maps title / content / target_query onto the CMS field shape
  - calls the platform client's create_item method
  - returns {"id": str, "platform": str} on success
  - raises a platform-specific *APIError on failure

The mapping is deliberately minimal — title → name, content → body,
target_query → summary. Real CMS schemas vary per site, so the user's
collection needs to either accept these field keys or be customised
in their own dashboard. Document this in the per-connector README
when we ship the dashboard cutover.

Untested against live Wix / Framer projects — verify on first deploy.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    """Basic slug — lowercase, dashes, trimmed. Same shape as the
    Webflow path so the user gets consistent URLs across CMSes."""
    s = (value or "").lower().strip()
    s = _SLUG_RE.sub("-", s)
    return s.strip("-")[:120] or "untitled"


def _build_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    """Common queue item → CMS-field mapping shared across publishers."""
    title = item.get("title") or item.get("target_query") or "Untitled"
    fields: Dict[str, Any] = {
        "name": title,
        "title": title,
        "slug": _slugify(title),
        "content": item.get("content") or "",
        "body": item.get("content") or "",
    }
    target_query = item.get("target_query")
    if target_query:
        fields["summary"] = f"Target query: {target_query}"
    return fields


def publish_to_wix(
    *, connection, item: Dict[str, Any], collection_id: Optional[str] = None
) -> Dict[str, Any]:
    """Create a Wix CMS item from a content_queue item.

    `connection` is a WixConnection row. `collection_id` defaults to the
    first cached collection if not provided.
    """
    from services.wix_client import WixClient

    target_collection = collection_id or _first_cached_collection_id(
        connection.collections_cache
    )
    if not target_collection:
        raise ValueError(
            "No Wix collection selected — pick one or refresh the cached list."
        )
    api = WixClient(api_key=connection.api_key, site_id=connection.site_id)
    result = api.create_item(
        collection_id=target_collection, fields=_build_fields(item)
    )
    return {
        "id": result.get("id") or result.get("_id") or "",
        "platform": "wix",
        "collection_id": target_collection,
    }


def publish_to_framer(
    *, connection, item: Dict[str, Any], collection_id: Optional[str] = None
) -> Dict[str, Any]:
    """Create a Framer CMS item from a content_queue item."""
    from services.framer_client import FramerClient

    target_collection = collection_id or _first_cached_collection_id(
        connection.collections_cache
    )
    if not target_collection:
        raise ValueError(
            "No Framer collection selected — pick one or refresh the cached list."
        )
    api = FramerClient(
        access_token=connection.access_token, project_id=connection.project_id
    )
    result = api.create_item(
        collection_id=target_collection, fields=_build_fields(item)
    )
    return {
        "id": result.get("id") or "",
        "platform": "framer",
        "collection_id": target_collection,
    }


def _first_cached_collection_id(cache: Any) -> str:
    """Pull the first collection ID out of the cached list, regardless
    of whether it was stored as [{id: ...}] or [{_id: ...}]."""
    if not isinstance(cache, list):
        return ""
    for entry in cache:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id") or entry.get("_id") or entry.get("collectionId")
        if cid:
            return str(cid)
    return ""
