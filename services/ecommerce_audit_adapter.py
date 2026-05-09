"""
Cross-platform ecommerce-product normalizer.

Maps BigCommerce / SHOPLINE / Squarespace product payloads onto the
Shopify-Admin-REST shape that services/shopify_audit.py already knows
how to audit. Every audit heuristic (thin descriptions, missing alt
text, missing structured fields, no images, draft-only catalog) flows
through one path — adding a fourth platform is a normalize function
plus a one-line route.

Output dict shape (per product):
    {
        "title": str,
        "status": "active" | "draft" | "archived",
        "body_html": str,
        "images": [{"alt": str, "src": str}, ...],
        "product_type": str,
        "vendor": str,
        "tags": [str, ...] | str,
    }

These adapters are written against the official platform docs and have
not been tested with real responses. First end-to-end deploy may need
small field-name tweaks once we see a live payload.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List


# ---------------------------------------------------------------------------
# BigCommerce — v3 catalog/products
# ---------------------------------------------------------------------------

def normalize_bigcommerce_product(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    images_raw = raw.get("images") or []
    images: List[Dict[str, Any]] = []
    for img in images_raw if isinstance(images_raw, list) else []:
        if isinstance(img, dict):
            images.append(
                {
                    "alt": str(img.get("description") or "").strip(),
                    "src": img.get("url_standard") or img.get("url_zoom") or "",
                }
            )
    is_visible = bool(raw.get("is_visible"))
    availability = str(raw.get("availability") or "").lower()
    if not is_visible:
        status = "draft"
    elif availability == "disabled":
        status = "archived"
    else:
        status = "active"
    return {
        "title": str(raw.get("name") or "").strip(),
        "status": status,
        "body_html": str(raw.get("description") or ""),
        "images": images,
        "product_type": str(raw.get("type") or "").strip(),
        # BigCommerce stores brand_id; the live brand name needs a
        # separate /catalog/brands lookup. Treat unknown as missing.
        "vendor": str(raw.get("brand_name") or "").strip(),
        "tags": [],
    }


def normalize_bigcommerce_catalog(
    raw_products: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [normalize_bigcommerce_product(p) for p in raw_products or [] if p]


# ---------------------------------------------------------------------------
# SHOPLINE — Open API products endpoint (Shopify-shaped)
# ---------------------------------------------------------------------------

def normalize_shopline_product(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    images_raw = raw.get("images") or []
    images: List[Dict[str, Any]] = []
    for img in images_raw if isinstance(images_raw, list) else []:
        if isinstance(img, dict):
            images.append(
                {
                    "alt": str(img.get("alt") or "").strip(),
                    "src": img.get("src") or img.get("url") or "",
                }
            )
    published = bool(raw.get("published"))
    if "status" in raw:
        status = str(raw.get("status") or "").lower() or ("active" if published else "draft")
    else:
        status = "active" if published else "draft"
    tags = raw.get("tags")
    if isinstance(tags, str):
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list):
        tags_list = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tags_list = []
    return {
        "title": str(raw.get("title") or raw.get("name") or "").strip(),
        "status": status,
        "body_html": str(raw.get("body_html") or raw.get("description") or ""),
        "images": images,
        "product_type": str(raw.get("product_type") or "").strip(),
        "vendor": str(raw.get("vendor") or "").strip(),
        "tags": tags_list,
    }


def normalize_shopline_catalog(
    raw_products: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [normalize_shopline_product(p) for p in raw_products or [] if p]


# ---------------------------------------------------------------------------
# Squarespace Commerce — /1.0/commerce/products
# ---------------------------------------------------------------------------

def normalize_squarespace_product(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    images_raw = raw.get("images") or []
    images: List[Dict[str, Any]] = []
    for img in images_raw if isinstance(images_raw, list) else []:
        if isinstance(img, dict):
            images.append(
                {
                    # Squarespace's image objects don't always carry alt;
                    # treat empty as missing for the audit's purposes.
                    "alt": str(img.get("altText") or img.get("alt") or "").strip(),
                    "src": img.get("url") or img.get("originalSize", {}).get("url") or "",
                }
            )
    is_visible = bool(raw.get("isVisible", True))
    status = "active" if is_visible else "draft"
    tags = raw.get("tags")
    if isinstance(tags, list):
        tags_list = [str(t).strip() for t in tags if str(t).strip()]
    elif isinstance(tags, str):
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        tags_list = []
    return {
        "title": str(raw.get("name") or "").strip(),
        "status": status,
        "body_html": str(raw.get("description") or ""),
        "images": images,
        "product_type": str(raw.get("productType") or "").strip(),
        # Squarespace doesn't have a vendor field at the product level.
        "vendor": "",
        "tags": tags_list,
    }


def normalize_squarespace_catalog(
    raw_products: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [normalize_squarespace_product(p) for p in raw_products or [] if p]
