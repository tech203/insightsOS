"""
Framer CMS client.

Framer's CMS API is in beta and uses Personal Access Tokens (PATs).
The merchant generates a PAT in their Framer account settings and
pastes it into the connect form along with their project ID, which
maps to the Framer site whose CMS we'll publish into.

Surface kept minimal because Framer's CMS API is still narrow:
list collections, list items, create an item. Read-write update is
deferred until we have a live site to test against.

Untested against a live Framer project — verify on first deploy.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests


logger = logging.getLogger(__name__)


class FramerConfigError(Exception):
    """Connection fields missing or malformed."""


class FramerAPIError(Exception):
    """Non-2xx response from the Framer API."""


_API_BASE = "https://api.framer.com/v1"


def _headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def verify_connection(*, access_token: str, project_id: str) -> Dict[str, Any]:
    """Confirm the PAT works and the project ID is reachable."""
    if not access_token or not project_id:
        raise FramerConfigError("Access token and project ID are required.")
    resp = requests.get(
        f"{_API_BASE}/projects/{project_id}",
        headers=_headers(access_token),
        timeout=20,
    )
    if resp.status_code in (401, 403):
        raise FramerAPIError("Invalid Framer access token.")
    if resp.status_code == 404:
        raise FramerAPIError("Framer project not found — double-check the project ID.")
    if resp.status_code >= 400:
        raise FramerAPIError(
            f"Framer returned {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json() or {}


class FramerClient:
    def __init__(self, *, access_token: str, project_id: str):
        if not access_token or not project_id:
            raise FramerConfigError("Access token and project ID are required.")
        self.access_token = access_token
        self.project_id = project_id

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{_API_BASE}{path}"
        resp = requests.request(
            method,
            url,
            headers=_headers(self.access_token),
            json=json_body,
            timeout=30,
        )
        if resp.status_code >= 400:
            raise FramerAPIError(
                f"{method} {path} → {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json() or {}

    def list_collections(self) -> List[Dict[str, Any]]:
        body = self._request("GET", f"/projects/{self.project_id}/collections")
        return list(body.get("collections") or body.get("data") or [])

    def create_item(
        self, *, collection_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        body = self._request(
            "POST",
            f"/projects/{self.project_id}/collections/{collection_id}/items",
            json_body={"fields": fields},
        )
        return body.get("item") or body
