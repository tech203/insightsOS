"""
Shopify integration helpers.

Two surfaces:

1. OAuth — install URL builder, callback verification, code → token exchange.
   Docs: https://shopify.dev/docs/apps/auth/oauth/getting-started

2. Admin REST — minimal helpers we actually use today: shop info + product list.
   Docs: https://shopify.dev/docs/api/admin-rest

The client deliberately stays small. Anything more (bulk product mutations,
collection sync, write-back, webhooks) gets added as the integration grows.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests


ADMIN_API_VERSION = "2024-10"
# Default scopes: read for catalog audits, write for one-click fixes
# (alt-text patches today; richer write-back later). Existing read-only
# tokens still work for read flows; write features check granted scope.
DEFAULT_SCOPES = "read_products,read_product_listings,write_products"
DEFAULT_TIMEOUT = 30


def scope_has(scope_string: Optional[str], target: str) -> bool:
    """True if `target` is one of the comma- or space-separated scopes."""
    if not scope_string:
        return False
    parts = [p.strip() for p in str(scope_string).replace(",", " ").split() if p.strip()]
    return target in parts

logger = logging.getLogger(__name__)


class ShopifyConfigError(Exception):
    """Raised when Shopify env vars are missing / placeholder."""


class ShopifyAPIError(Exception):
    """Raised when Shopify returns a non-2xx status."""


def is_shopify_configured() -> bool:
    key = os.getenv("SHOPIFY_API_KEY")
    secret = os.getenv("SHOPIFY_API_SECRET")
    return bool(key and secret) and not (
        key.startswith("your_") or secret.startswith("your_")
    )


def _normalize_shop_domain(shop: str) -> str:
    """Accept 'foo' or 'foo.myshopify.com' or 'https://foo.myshopify.com';
    return 'foo.myshopify.com'."""
    s = (shop or "").strip().lower()
    s = s.replace("https://", "").replace("http://", "").rstrip("/")
    if not s:
        return ""
    if "." not in s:
        s = f"{s}.myshopify.com"
    return s


def build_install_url(
    shop: str,
    redirect_uri: str,
    scopes: Optional[str] = None,
    state: Optional[str] = None,
) -> str:
    """Build the Shopify OAuth install URL the user gets redirected to."""
    api_key = os.getenv("SHOPIFY_API_KEY")
    if not api_key or api_key.startswith("your_"):
        raise ShopifyConfigError(
            "SHOPIFY_API_KEY is not set or still has the placeholder value."
        )
    domain = _normalize_shop_domain(shop)
    if not domain:
        raise ShopifyConfigError("Missing shop domain.")

    params = {
        "client_id": api_key,
        "scope": scopes or os.getenv("SHOPIFY_SCOPES") or DEFAULT_SCOPES,
        "redirect_uri": redirect_uri,
        "state": state or secrets.token_urlsafe(16),
    }
    return f"https://{domain}/admin/oauth/authorize?" + urllib.parse.urlencode(params)


def verify_hmac(query_params: Dict[str, str]) -> bool:
    """Verify the HMAC signature Shopify includes on every OAuth callback."""
    secret = os.getenv("SHOPIFY_API_SECRET")
    if not secret or secret.startswith("your_"):
        return False
    received = query_params.get("hmac")
    if not received:
        return False
    payload = "&".join(
        f"{k}={v}"
        for k, v in sorted(query_params.items())
        if k != "hmac" and k != "signature"
    )
    expected = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(received, expected)


def exchange_code_for_token(shop: str, code: str) -> Dict[str, Any]:
    """Trade the temporary code from the install callback for a permanent
    access token. Returns the full Shopify response (access_token + scope)."""
    api_key = os.getenv("SHOPIFY_API_KEY")
    api_secret = os.getenv("SHOPIFY_API_SECRET")
    if not api_key or not api_secret:
        raise ShopifyConfigError("SHOPIFY_API_KEY / SHOPIFY_API_SECRET not set.")
    domain = _normalize_shop_domain(shop)
    url = f"https://{domain}/admin/oauth/access_token"
    resp = requests.post(
        url,
        json={
            "client_id": api_key,
            "client_secret": api_secret,
            "code": code,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise ShopifyAPIError(
            f"Token exchange failed → {resp.status_code}: {resp.text[:200]}"
        )
    return resp.json()


class ShopifyAdminClient:
    """Tiny Admin-REST wrapper. One client per (shop, token)."""

    def __init__(self, shop_domain: str, access_token: str):
        self.shop_domain = _normalize_shop_domain(shop_domain)
        self.access_token = access_token
        self._base = f"https://{self.shop_domain}/admin/api/{ADMIN_API_VERSION}"

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self._base}{path}"
        resp = requests.get(
            url,
            headers={"X-Shopify-Access-Token": self.access_token},
            params=params or {},
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise ShopifyAPIError(f"GET {path} → {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _put(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self._base}{path}"
        resp = requests.put(
            url,
            headers={
                "X-Shopify-Access-Token": self.access_token,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise ShopifyAPIError(f"PUT {path} → {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def get_shop(self) -> Dict[str, Any]:
        return self._get("/shop.json").get("shop", {})

    def list_products(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch the first page of products. We don't follow Link-header
        pagination yet — most demo stores have well under 50 SKUs and the
        audit pass we'll add later only needs a sample."""
        data = self._get("/products.json", params={"limit": min(limit, 250)})
        return data.get("products") or []

    def update_product_image_alt(
        self, product_id: int, image_id: int, alt: str
    ) -> Dict[str, Any]:
        """Patch the `alt` field on a product image.

        Shopify keeps image alt as a top-level field on the image resource
        (not on the variant), so we PUT the resource directly. This is
        purely additive — existing alt text is overwritten only if a new
        non-empty value is sent."""
        path = f"/products/{int(product_id)}/images/{int(image_id)}.json"
        payload = {"image": {"id": int(image_id), "alt": alt}}
        return self._put(path, payload).get("image", {})
