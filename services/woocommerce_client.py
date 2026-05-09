"""
WooCommerce REST API helpers.

WooCommerce uses key-based auth (no OAuth) — the user generates a
read-only "Consumer key + Consumer secret" pair in their WP admin
under WooCommerce → Settings → Advanced → REST API. We pass the keys
as basic auth on every request.

Scope: list products only. We do not write back to WooCommerce —
unlike Shopify, the connector is read-only by design.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests


DEFAULT_TIMEOUT = 30


class WooConfigError(Exception):
    """Raised when store_url / keys are missing or malformed."""


class WooAPIError(Exception):
    """Raised on non-2xx responses from WooCommerce."""


def _normalize_store_url(raw: str) -> str:
    """Accept any of: 'shop.example.com', 'https://shop.example.com',
    'https://shop.example.com/wp-json/'. Return the bare scheme+host."""
    s = (raw or "").strip()
    if not s:
        return ""
    if not s.startswith(("http://", "https://")):
        s = "https://" + s
    parsed = urlparse(s)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


class WooClient:
    """Tiny WooCommerce REST wrapper. One client per (store, keys)."""

    def __init__(self, store_url: str, consumer_key: str, consumer_secret: str):
        self.store_url = _normalize_store_url(store_url)
        if not self.store_url:
            raise WooConfigError("Invalid WooCommerce store URL.")
        if not consumer_key or not consumer_secret:
            raise WooConfigError("WooCommerce consumer key / secret required.")
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self._base = f"{self.store_url}/wp-json/wc/v3"

    def _auth_header(self) -> Dict[str, str]:
        token = base64.b64encode(
            f"{self.consumer_key}:{self.consumer_secret}".encode("utf-8")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self._base}{path}"
        resp = requests.get(
            url,
            headers=self._auth_header(),
            params=params or {},
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise WooAPIError(f"GET {path} → {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def list_products(self, limit: int = 50) -> List[Dict[str, Any]]:
        """First page of products. WooCommerce caps per_page at 100;
        we ask for at most 50 since downstream audits sample, not
        exhaustively iterate."""
        return self._get(
            "/products",
            params={"per_page": min(int(limit), 100), "status": "any"},
        ) or []

    def shop_summary(self) -> Dict[str, Any]:
        """Top-level shop info — useful as a smoke-test that the keys
        actually work + for displaying in the connection card."""
        # WooCommerce doesn't have a single "shop" endpoint, but the
        # /system_status/tools endpoint requires read+write. /products
        # with per_page=1 is the cheapest read-only ping.
        first = self._get("/products", params={"per_page": 1, "status": "any"})
        return {"store_url": self.store_url, "ok": isinstance(first, list)}


def normalize_woo_product_to_shopify_shape(p: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a WooCommerce product into the dict shape that
    services.shopify_audit expects so we can reuse the catalog-audit
    logic without duplicating it. Mapping notes:

      WC name          → Shopify title
      WC description   → Shopify body_html
      WC images        → Shopify images (kept as-is — same {src, alt} fields)
      WC categories[0] → product_type
      WC tags          → tags (joined names)
      WC status        → status
      WC store domain  → vendor (best available substitute)
    """
    images = p.get("images") or []
    tags = p.get("tags") or []
    categories = p.get("categories") or []
    return {
        "id": p.get("id"),
        "title": p.get("name") or "",
        "body_html": p.get("description") or p.get("short_description") or "",
        "images": [
            {"id": img.get("id"), "src": img.get("src"), "alt": img.get("alt") or ""}
            for img in images
            if isinstance(img, dict)
        ],
        "product_type": (categories[0].get("name") if categories else "") or "",
        "vendor": "",  # WC has no vendor field
        "tags": ", ".join(t.get("name") or "" for t in tags if isinstance(t, dict)),
        "status": (p.get("status") or "publish") if (p.get("status") != "publish") else "active",
    }
