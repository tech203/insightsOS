"""
SHOPLINE Open API client.

SHOPLINE has two install paths:
  - Public OAuth apps (App Store) — full OAuth dance, requires App Store approval
  - Custom app (single merchant) — bearer token issued in the merchant admin

We use the custom-app pattern for the foundation: the merchant pastes
their store handle and access token in the connect form. Switching to
the public-app OAuth flow later is additive — same model, same client,
add a few callback routes.

API surface follows the SHOPLINE Open Platform docs
(https://shopline-developers.readme.io/). Untested against a live
store — verify endpoint paths and response shape on first deploy.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import requests


logger = logging.getLogger(__name__)


class ShoplineConfigError(Exception):
    """Connection fields missing or malformed."""


class ShoplineAPIError(Exception):
    """Non-2xx response from the SHOPLINE API."""


_HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def _normalize_handle(handle: str) -> str:
    """Accept full myshopline.com URLs or bare store handles. Returns the
    bare handle (e.g. 'mystore') ready to plug into the API host."""
    if not handle:
        raise ShoplineConfigError("Store handle is required.")
    raw = handle.strip().lower()
    raw = re.sub(r"^https?://", "", raw)
    raw = raw.split("/")[0]
    raw = raw.replace(".myshopline.com", "")
    if not _HANDLE_RE.match(raw):
        raise ShoplineConfigError(
            "Store handle should look like 'mystore' (no spaces or slashes)."
        )
    return raw


def _api_base(store_handle: str) -> str:
    return f"https://{store_handle}.myshopline.com/admin/openapi/v20240601"


def _headers(access_token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def verify_connection(*, store_handle: str, access_token: str) -> Dict[str, Any]:
    """Hit a low-cost authenticated endpoint to confirm credentials."""
    handle = _normalize_handle(store_handle)
    if not access_token:
        raise ShoplineConfigError("Access token is required.")
    resp = requests.get(
        f"{_api_base(handle)}/shop.json",
        headers=_headers(access_token),
        timeout=20,
    )
    if resp.status_code == 401:
        raise ShoplineAPIError("Invalid access token for this SHOPLINE store.")
    if resp.status_code == 404:
        raise ShoplineAPIError("Store handle not found — double-check the value.")
    if resp.status_code >= 400:
        raise ShoplineAPIError(
            f"SHOPLINE returned {resp.status_code}: {resp.text[:200]}"
        )
    payload = resp.json() or {}
    return payload.get("shop") or payload


class ShoplineClient:
    """Read-only catalog client. Mirrors the Shopify/BigCommerce shape so
    audits can be wired in via a thin adapter when we extend Module 3."""

    def __init__(self, *, store_handle: str, access_token: str):
        self.store_handle = _normalize_handle(store_handle)
        if not access_token:
            raise ShoplineConfigError("Access token is required.")
        self.access_token = access_token

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{_api_base(self.store_handle)}{path}"
        resp = requests.get(
            url,
            headers=_headers(self.access_token),
            params=params or {},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise ShoplineAPIError(
                f"GET {path} → {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json() or {}

    def list_products(self, *, limit: int = 50, page: int = 1) -> List[Dict[str, Any]]:
        """Return up to `limit` products. SHOPLINE uses `limit` + `page`."""
        body = self._get(
            "/products.json",
            params={"limit": min(int(limit), 250), "page": int(page)},
        )
        return list(body.get("products") or body.get("data") or [])

    def shop_info(self) -> Dict[str, Any]:
        return verify_connection(
            store_handle=self.store_handle, access_token=self.access_token
        )
