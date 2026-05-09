"""
BigCommerce Storefront API client.

BigCommerce supports two install modes:
  - Public OAuth apps (App Marketplace) — full OAuth dance
  - Self-installed (single-merchant) — store hash + API account token

We use the self-installed pattern: the merchant pastes their store hash
and a Stencil API account token in the connect form, we verify by
calling /v3/store, persist the credentials, and use them for catalog
reads.

API call shape is per BigCommerce v3 REST docs
(https://developer.bigcommerce.com/docs/rest-management/products).
Untested against a live store — verify the token + store-hash flow on
first end-to-end deploy.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests


logger = logging.getLogger(__name__)


class BigCommerceConfigError(Exception):
    """Connection fields missing or malformed."""


class BigCommerceAPIError(Exception):
    """Non-2xx response from the BigCommerce API."""


def _api_base(store_hash: str) -> str:
    return f"https://api.bigcommerce.com/stores/{store_hash.strip()}/v3"


def _headers(access_token: str) -> Dict[str, str]:
    return {
        "X-Auth-Token": access_token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def verify_connection(*, store_hash: str, access_token: str) -> Dict[str, Any]:
    """Hit /v3/store to confirm the credentials work and pull store
    metadata. Raises BigCommerceAPIError on auth failure."""
    if not store_hash or not access_token:
        raise BigCommerceConfigError("Store hash and access token are required.")
    resp = requests.get(
        f"{_api_base(store_hash)}/store",
        headers=_headers(access_token),
        timeout=20,
    )
    if resp.status_code == 401:
        raise BigCommerceAPIError("Invalid API token for this store.")
    if resp.status_code == 404:
        raise BigCommerceAPIError("Store hash not found — double-check the value.")
    if resp.status_code >= 400:
        raise BigCommerceAPIError(
            f"BigCommerce returned {resp.status_code}: {resp.text[:200]}"
        )
    payload = resp.json() or {}
    return payload.get("data") or payload


class BigCommerceClient:
    """Thin wrapper for the read-only catalog endpoints we need today.
    Add write methods when we wire description + alt-text write-back."""

    def __init__(self, *, store_hash: str, access_token: str):
        if not store_hash or not access_token:
            raise BigCommerceConfigError("Store hash and access token are required.")
        self.store_hash = store_hash.strip()
        self.access_token = access_token

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{_api_base(self.store_hash)}{path}"
        resp = requests.get(
            url,
            headers=_headers(self.access_token),
            params=params or {},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise BigCommerceAPIError(
                f"GET {path} → {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json() or {}

    def list_products(self, *, limit: int = 50, page: int = 1) -> List[Dict[str, Any]]:
        """Return up to `limit` products. BigCommerce paginates with
        `page` + `limit` query params; max 250 per page."""
        body = self._get(
            "/catalog/products",
            params={"limit": min(int(limit), 250), "page": int(page), "include": "images"},
        )
        return list(body.get("data") or [])

    def store_info(self) -> Dict[str, Any]:
        """Convenience — same call as verify_connection but on the
        instance, used by the post-connect refresh path."""
        return verify_connection(
            store_hash=self.store_hash, access_token=self.access_token
        )
