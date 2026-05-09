"""
Squarespace site connector.

Squarespace's Content API is read-only for pages/posts and
write-capable only for Commerce (products/orders/inventory). For the
AEO use case we want CMS-style publishing, which Squarespace does not
expose — so this connector is intentionally narrower than the others:

  - Connects via a Squarespace API key (Settings → Advanced → API Keys)
  - Pulls site metadata + commerce catalog (when available)
  - Does NOT publish CMS content (the API doesn't allow it)

When Squarespace ships a real Content write API, swap this for a full
publish path. Until then, the connector exists for catalog audits on
Squarespace Commerce stores.

Untested against a live Squarespace site — verify on first deploy.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests


logger = logging.getLogger(__name__)


class SquarespaceConfigError(Exception):
    """Connection fields missing or malformed."""


class SquarespaceAPIError(Exception):
    """Non-2xx response from the Squarespace API."""


_API_BASE = "https://api.squarespace.com/1.0"
_USER_AGENT = "DarInsights/1.0"


def _headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def verify_connection(*, api_key: str) -> Dict[str, Any]:
    """Hit /commerce/inventory (low-cost authenticated endpoint that
    returns 200 even on sites without commerce, just empty results)."""
    if not api_key:
        raise SquarespaceConfigError("API key is required.")
    resp = requests.get(
        f"{_API_BASE}/commerce/inventory",
        headers=_headers(api_key),
        params={"cursor": ""},
        timeout=20,
    )
    if resp.status_code in (401, 403):
        raise SquarespaceAPIError("Invalid Squarespace API key.")
    if resp.status_code >= 400:
        raise SquarespaceAPIError(
            f"Squarespace returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json() or {}


class SquarespaceClient:
    def __init__(self, *, api_key: str):
        if not api_key:
            raise SquarespaceConfigError("API key is required.")
        self.api_key = api_key

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{_API_BASE}{path}"
        resp = requests.get(
            url,
            headers=_headers(self.api_key),
            params=params or {},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise SquarespaceAPIError(
                f"GET {path} → {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json() or {}

    def list_products(self, *, cursor: str = "") -> Dict[str, Any]:
        """Commerce-only. Returns {'products': [...], 'pagination': {...}}.
        Empty list for sites without a commerce plan."""
        return self._get("/commerce/products", params={"cursor": cursor or ""})

    def list_inventory(self, *, cursor: str = "") -> Dict[str, Any]:
        return self._get("/commerce/inventory", params={"cursor": cursor or ""})
