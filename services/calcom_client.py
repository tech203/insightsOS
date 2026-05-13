"""
Cal.com integration helpers.

Key-based auth (no OAuth). User generates an API key in Cal.com under
Settings → Developer and pastes it into our connect form alongside
their Cal.com username. We list event types and recent bookings to
surface a workspace-scoped booking dashboard.

API: https://api.cal.com/v2/  (Cal.com v2 API; legacy v1 still works
on api.cal.com/v1 but v2 is the recommended path).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dtutils import utcnow
from typing import Any, Dict, List, Optional

import requests


API_BASE = "https://api.cal.com/v2"
API_VERSION = "2024-08-13"
DEFAULT_TIMEOUT = 30


class CalComConfigError(Exception):
    """Raised when api_key / username are missing or malformed."""


class CalComAPIError(Exception):
    """Raised on non-2xx responses from Cal.com."""


class CalComClient:
    """Tiny Cal.com REST wrapper."""

    def __init__(self, api_key: str, username: Optional[str] = None):
        if not api_key:
            raise CalComConfigError("Cal.com API key required.")
        self.api_key = api_key
        self.username = (username or "").strip().lstrip("@")

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "cal-api-version": API_VERSION,
        }

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        resp = requests.get(
            f"{API_BASE}{path}",
            headers=self._headers(),
            params=params or {},
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise CalComAPIError(
                f"GET {path} → {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    def me(self) -> Dict[str, Any]:
        """Smoke-test that the API key works + return the user payload."""
        return (self._get("/me") or {}).get("data") or {}

    def list_event_types(self) -> List[Dict[str, Any]]:
        """Public + private event types on this Cal.com account."""
        data = self._get("/event-types") or {}
        body = data.get("data") or {}
        # API returns {data: {eventTypeGroups: [{eventTypes: [...]}]}}
        out: List[Dict[str, Any]] = []
        for group in body.get("eventTypeGroups") or []:
            for et in group.get("eventTypes") or []:
                out.append(et)
        # Some versions return {data: [{...event types...}]} flat;
        # handle that fallback too.
        if not out and isinstance(body, list):
            out = body
        return out

    def list_recent_bookings(self, days: int = 30) -> List[Dict[str, Any]]:
        """Bookings created in the last `days`. Cal.com filters by
        `afterStart` (ISO8601). Capped to 100 by API; fine for our
        30-day window since the dashboard only counts."""
        after = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
        data = self._get(
            "/bookings",
            params={"afterStart": after, "take": 100},
        ) or {}
        body = data.get("data") or []
        return body if isinstance(body, list) else []


def public_booking_url(username: Optional[str], event_slug: Optional[str] = None) -> Optional[str]:
    """Build the public Cal.com URL the workspace can share."""
    u = (username or "").strip().lstrip("@")
    if not u:
        return None
    if event_slug:
        return f"https://cal.com/{u}/{event_slug}"
    return f"https://cal.com/{u}"


def summarize(client: CalComClient) -> Dict[str, Any]:
    """One canonical pull for the dashboard. Returns event types,
    last 30-day booking count, and the public profile URL."""
    me = client.me()
    event_types = client.list_event_types()
    bookings = client.list_recent_bookings(days=30)
    username = client.username or me.get("username") or ""

    return {
        "username": username,
        "name": me.get("name") or username,
        "profile_url": public_booking_url(username),
        "event_types": [
            {
                "id": et.get("id"),
                "title": et.get("title") or et.get("slug"),
                "slug": et.get("slug"),
                "length": et.get("lengthInMinutes") or et.get("length") or 0,
                "price": et.get("price") or 0,
                "currency": et.get("currency") or "USD",
                "url": public_booking_url(username, et.get("slug")),
            }
            for et in (event_types or [])[:20]
        ],
        "bookings_30d": len(bookings or []),
        "fetched_at": utcnow().isoformat(),
    }
