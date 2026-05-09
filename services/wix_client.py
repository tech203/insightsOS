"""
Wix CMS (Wix Data) client.

Wix supports several install paths:
  - Wix App Marketplace OAuth (full app distribution)
  - Headless API key (per-site permanent token from the dashboard)

We use the API-key path for the foundation: merchants generate a
permanent token in their Wix dashboard (Settings → Headless Settings
→ API Keys), paste it plus their site ID, and we verify by calling
/wix-data/v2/collections.

Switch to Wix Marketplace OAuth later when DarInsights is approved as
a Wix app — same model + client, just additional callback routes.

Wix Data API reference:
https://dev.wix.com/api/rest/wix-data/data-items
Untested against a live Wix site — verify the API key + site ID flow
on first deploy.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests


logger = logging.getLogger(__name__)


class WixConfigError(Exception):
    """Connection fields missing or malformed."""


class WixAPIError(Exception):
    """Non-2xx response from the Wix API."""


_API_BASE = "https://www.wixapis.com"


def _headers(api_key: str, site_id: str) -> Dict[str, str]:
    return {
        "Authorization": api_key,
        "wix-site-id": site_id,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def verify_connection(*, api_key: str, site_id: str) -> Dict[str, Any]:
    """Hit /wix-data/v2/collections to confirm credentials. Returns the
    JSON response so callers can pull collection list for the connect
    UI without a second round-trip."""
    if not api_key or not site_id:
        raise WixConfigError("API key and site ID are required.")
    resp = requests.get(
        f"{_API_BASE}/wix-data/v2/collections",
        headers=_headers(api_key, site_id),
        timeout=20,
    )
    if resp.status_code in (401, 403):
        raise WixAPIError("Invalid Wix API key or insufficient permissions.")
    if resp.status_code == 404:
        raise WixAPIError("Wix site ID not found — double-check the value.")
    if resp.status_code >= 400:
        raise WixAPIError(f"Wix returned {resp.status_code}: {resp.text[:200]}")
    return resp.json() or {}


class WixClient:
    """Read-list-create client for Wix Data collections — enough surface
    to drive 'publish a Generated content item to Wix CMS' once we wire
    the publish path."""

    def __init__(self, *, api_key: str, site_id: str):
        if not api_key or not site_id:
            raise WixConfigError("API key and site ID are required.")
        self.api_key = api_key
        self.site_id = site_id

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{_API_BASE}{path}"
        resp = requests.request(
            method,
            url,
            headers=_headers(self.api_key, self.site_id),
            params=params or {},
            json=json_body,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise WixAPIError(
                f"{method} {path} → {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json() or {}

    def list_collections(self) -> List[Dict[str, Any]]:
        body = self._request("GET", "/wix-data/v2/collections")
        return list(body.get("collections") or [])

    def list_items(self, *, collection_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        body = self._request(
            "POST",
            "/wix-data/v2/items/query",
            json_body={
                "dataCollectionId": collection_id,
                "query": {"paging": {"limit": int(limit)}},
            },
        )
        return list(body.get("dataItems") or [])

    def create_item(
        self, *, collection_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create a CMS item. `fields` maps field key → value."""
        body = self._request(
            "POST",
            "/wix-data/v2/items",
            json_body={
                "dataCollectionId": collection_id,
                "dataItem": {"data": fields},
            },
        )
        return body.get("dataItem") or {}
