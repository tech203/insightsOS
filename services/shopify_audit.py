"""
Shopify product audit.

Inspects a list of Admin-REST product payloads and turns the gaps that
hurt AI visibility into action-shaped recommendations that slot into
the same `recommended_actions` list the Growth Calendar already reads.

Each finding is wrapped via action_engine._make_action so the priority,
credits, and dedupe behaviour stays consistent. They're tagged with
category_tag="ecommerce_product" so the UI can pill them apart from
generic ecommerce or score-based recs.

The checks are intentionally narrow — five high-signal gaps that an AI
answer engine demonstrably penalises (thin descriptions, missing alt
text, missing structured fields, no images, draft-only catalog). Add
more only when there's clear UX evidence they help.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

from action_engine import _make_action


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_THIN_DESCRIPTION_WORD_THRESHOLD = 50


def _strip_html(html: Optional[str]) -> str:
    if not html:
        return ""
    text = _HTML_TAG_RE.sub(" ", str(html))
    return _WS_RE.sub(" ", text).strip()


def _word_count(text: str) -> int:
    if not text:
        return 0
    return len([w for w in text.split() if w.strip()])


def _product_status(product: Dict[str, Any]) -> str:
    return str(product.get("status") or "").lower()


def _product_images(product: Dict[str, Any]) -> List[Dict[str, Any]]:
    images = product.get("images")
    if isinstance(images, list):
        return [img for img in images if isinstance(img, dict)]
    return []


def _missing_alt(product: Dict[str, Any]) -> bool:
    images = _product_images(product)
    if not images:
        return False
    return any(not str(img.get("alt") or "").strip() for img in images)


def _is_thin_description(product: Dict[str, Any]) -> bool:
    text = _strip_html(product.get("body_html"))
    return _word_count(text) < _THIN_DESCRIPTION_WORD_THRESHOLD


def _has_no_images(product: Dict[str, Any]) -> bool:
    return len(_product_images(product)) == 0


def _missing_structured_fields(product: Dict[str, Any]) -> List[str]:
    """Return the names of fields that meaningfully hurt AI surfacing."""
    missing: List[str] = []
    if not str(product.get("product_type") or "").strip():
        missing.append("product_type")
    if not str(product.get("vendor") or "").strip():
        missing.append("vendor")
    tags = product.get("tags")
    if isinstance(tags, list):
        if not tags:
            missing.append("tags")
    elif not str(tags or "").strip():
        missing.append("tags")
    return missing


def summarize_catalog(products: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure summary of a product list — no actions, just counts.

    Useful for the products view header and for deciding which findings
    should fire (e.g. only warn about draft-only catalogs when there are
    products at all)."""
    total = 0
    active = 0
    drafts = 0
    archived = 0
    thin_desc = 0
    no_images = 0
    missing_alt = 0
    missing_type = 0
    missing_vendor = 0
    missing_tags = 0

    for product in products or []:
        if not isinstance(product, dict):
            continue
        total += 1
        status = _product_status(product)
        if status == "active":
            active += 1
        elif status == "draft":
            drafts += 1
        elif status == "archived":
            archived += 1

        if _is_thin_description(product):
            thin_desc += 1
        if _has_no_images(product):
            no_images += 1
        if _missing_alt(product):
            missing_alt += 1

        missing = _missing_structured_fields(product)
        if "product_type" in missing:
            missing_type += 1
        if "vendor" in missing:
            missing_vendor += 1
        if "tags" in missing:
            missing_tags += 1

    return {
        "total": total,
        "active": active,
        "drafts": drafts,
        "archived": archived,
        "thin_description": thin_desc,
        "no_images": no_images,
        "missing_alt_text": missing_alt,
        "missing_product_type": missing_type,
        "missing_vendor": missing_vendor,
        "missing_tags": missing_tags,
    }


def _tag(action: Dict[str, Any]) -> Dict[str, Any]:
    action["category_tag"] = "ecommerce_product"
    return action


def find_shopify_action_items(
    products: Iterable[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Run the catalog audit and return action-shaped findings.

    Each finding is already a `_make_action`-formatted dict, ready to
    extend a workspace's `recommended_actions` list."""
    summary = summarize_catalog(products)
    total = summary["total"]
    if total == 0:
        return []

    findings: List[Dict[str, Any]] = []

    if summary["thin_description"]:
        share = summary["thin_description"] / total
        findings.append(
            _tag(
                _make_action(
                    category="content_gap",
                    priority="high" if share >= 0.4 else "medium",
                    title=f"Expand thin product descriptions ({summary['thin_description']} of {total})",
                    issue=(
                        f"{summary['thin_description']} of your products have a description "
                        f"shorter than {_THIN_DESCRIPTION_WORD_THRESHOLD} words."
                    ),
                    why_it_matters=(
                        "AI answer engines summarise product pages from their text body; "
                        "thin descriptions get skipped in favour of competitors with richer copy."
                    ),
                    recommended_fix=(
                        "Rewrite the affected product descriptions with use cases, materials, "
                        "fit/sizing, ingredients or specs, and a clear value-prop paragraph."
                    ),
                    suggested_content_type="service_page",
                    impact_score=15,
                    difficulty="medium",
                )
            )
        )

    if summary["missing_alt_text"]:
        findings.append(
            _tag(
                _make_action(
                    category="schema_fix",
                    priority="medium",
                    title=f"Add alt text to product images ({summary['missing_alt_text']} of {total})",
                    issue=(
                        f"{summary['missing_alt_text']} of your products have at least one image "
                        "without alt text."
                    ),
                    why_it_matters=(
                        "Image alt text is one of the few signals AI engines have to interpret "
                        "what's in a product photo. Missing alt = the image doesn't exist for AI."
                    ),
                    recommended_fix=(
                        "Add descriptive alt text on every product image (subject + key attribute, "
                        "e.g. 'matte black ceramic mug, 12oz')."
                    ),
                    impact_score=11,
                    difficulty="easy",
                )
            )
        )

    if summary["no_images"]:
        findings.append(
            _tag(
                _make_action(
                    category="content_gap",
                    priority="high",
                    title=f"Add product photography ({summary['no_images']} of {total} have no images)",
                    issue=f"{summary['no_images']} products have zero images.",
                    why_it_matters=(
                        "Image-less product pages are functionally invisible in image-rich AI "
                        "answers and rich product results."
                    ),
                    recommended_fix=(
                        "Upload at least one primary image and one secondary lifestyle / detail "
                        "image per affected product."
                    ),
                    impact_score=14,
                    difficulty="medium",
                )
            )
        )

    if summary["missing_product_type"] or summary["missing_tags"]:
        gaps: List[str] = []
        if summary["missing_product_type"]:
            gaps.append(f"product_type ({summary['missing_product_type']})")
        if summary["missing_tags"]:
            gaps.append(f"tags ({summary['missing_tags']})")
        gaps_str = ", ".join(gaps)

        findings.append(
            _tag(
                _make_action(
                    category="entity_fix",
                    priority="medium",
                    title="Fill in product taxonomy fields",
                    issue=(
                        f"Some products are missing structured taxonomy fields: {gaps_str}."
                    ),
                    why_it_matters=(
                        "AI engines lean on product_type, vendor, and tags to group items into "
                        "categories and surface them for category-style queries ('best <type> for X')."
                    ),
                    recommended_fix=(
                        "Set a consistent product_type per SKU and add 3–7 descriptive tags "
                        "(use case, material, audience). Backfill on existing items."
                    ),
                    impact_score=10,
                    difficulty="easy",
                )
            )
        )

    if summary["drafts"] and summary["active"] == 0:
        findings.append(
            _tag(
                _make_action(
                    category="visibility_gap",
                    priority="high",
                    title="Publish your draft products",
                    issue=(
                        f"All {summary['drafts']} of your products are still in draft state."
                    ),
                    why_it_matters=(
                        "Draft products aren't visible to crawlers or AI answer engines — "
                        "your store can't be cited until they're published."
                    ),
                    recommended_fix=(
                        "Review draft products and publish the ones that are ready. "
                        "Archive the rest so they don't dilute the catalog."
                    ),
                    impact_score=18,
                    difficulty="easy",
                )
            )
        )

    return findings
