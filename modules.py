"""
Module catalog for the modular subscription model.

Each module is an independently-purchasable monthly subscription line
item — customers buy one Stripe Subscription with one line item per
active module. A module unlocks a specific feature surface (e.g. the
Website module unlocks Webflow CMS publishing).

Status: foundation only. Modules aren't surfaced in the dashboard yet
and no checkout routes are wired. The legacy plan tiers (free/pro/
growth) still drive feature gates today via PLAN_IMPLICIT_MODULES,
which says "every legacy paid-plan user implicitly owns every module"
— so when we flip the dashboard over, no existing customer regresses.

Open questions (intentionally deferred until the dashboard cutover):
- Credit allowance: should each module grant N monthly credits, or
  should credits become a separate line-item add-on?
- Plan vs modules coexistence: do we keep pro/growth as bundles
  ("buy all 6 modules at a discount"), or sunset them entirely?
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
# Single source of truth for what each module includes and what it costs.
# `unlocks` is a list of feature slugs the module turns on — feature gates
# in app.py call `module_for_feature()` to find which module a feature
# belongs to, then `user_has_module()` to check access.

MODULE_CATALOG: Dict[str, Dict[str, Any]] = {
    "action_plan": {
        "label": "Action Plan Automation",
        "monthly_price_usd": 19,
        "stripe_price_env": "STRIPE_PRICE_MODULE_ACTION_PLAN",
        "description": (
            "AI briefs, drafts, and edits; multi-engine answer monitor; "
            "Growth Calendar with stale-content feed; marketplace audits."
        ),
        "unlocks": [
            "ai_content_brief",
            "ai_content_draft",
            "ai_editing",
            "multi_engine_monitor",
            "growth_calendar",
            "marketplace_audit",
        ],
    },
    "website": {
        "label": "Website Module",
        "monthly_price_usd": 29,
        "stripe_price_env": "STRIPE_PRICE_MODULE_WEBSITE",
        "description": (
            "Webflow CMS publishing, conversational AI editing, Placid "
            "visual generation, Wix / Squarespace / Framer integrations."
        ),
        "unlocks": [
            "webflow_publishing",
            "conversational_editing",
            "placid_visuals",
            "wix_integration",
            "squarespace_integration",
            "framer_integration",
        ],
    },
    "ecommerce": {
        "label": "Ecommerce Module",
        "monthly_price_usd": 29,
        "stripe_price_env": "STRIPE_PRICE_MODULE_ECOMMERCE",
        "description": (
            "Shopify, WooCommerce, SHOPLINE, BigCommerce connectors; "
            "catalog audits; alt-text and description write-back."
        ),
        "unlocks": [
            "shopify",
            "woocommerce",
            "shopline",
            "bigcommerce",
            "catalog_audit",
            "alt_text_writeback",
            "description_writeback",
        ],
    },
    "marketplace": {
        "label": "Marketplace Module",
        "monthly_price_usd": 19,
        "stripe_price_env": "STRIPE_PRICE_MODULE_MARKETPLACE",
        "description": (
            "Etsy, Amazon, Shopee, TikTok Shop, Lazada visibility tracking."
        ),
        "unlocks": [
            "etsy_tracking",
            "amazon_tracking",
            "shopee_tracking",
            "tiktok_shop_tracking",
            "lazada_tracking",
        ],
    },
    "real_data": {
        "label": "Real-Data Stack",
        "monthly_price_usd": 29,
        "stripe_price_env": "STRIPE_PRICE_MODULE_REAL_DATA",
        "description": (
            "Google Search Console, Google Analytics, and (later) SerpAPI "
            "integrations; data-driven recommendations."
        ),
        "unlocks": [
            "google_search_console",
            "google_analytics",
            "serpapi",
            "data_driven_recommendations",
        ],
    },
    "agency": {
        "label": "Agency",
        "monthly_price_usd": 49,
        "stripe_price_env": "STRIPE_PRICE_MODULE_AGENCY",
        "description": (
            "White-label, team seats, multi-workspace, agency reporting, "
            "branded public report sharing."
        ),
        "unlocks": [
            "white_label",
            "team_seats",
            "multi_workspace",
            "agency_reporting",
            "branded_public_reports",
        ],
    },
}


# Render order — matches the launch screenshot order.
MODULE_ORDER: List[str] = [
    "action_plan",
    "website",
    "ecommerce",
    "marketplace",
    "real_data",
    "agency",
]


# Implicit module access for legacy plan tiers. When `user_has_module()`
# can't find an explicit UserModule row, it falls back to this map so
# existing free/pro/growth customers don't regress while the modules
# system stays dark. Pro/Growth implicitly own everything because
# today they already get every feature.
PLAN_IMPLICIT_MODULES: Dict[str, List[str]] = {
    "free": [],
    "pro": list(MODULE_ORDER),
    "growth": list(MODULE_ORDER),
    # Internal / comp tiers — keep them whole.
    "starter": list(MODULE_ORDER),
    "agency": list(MODULE_ORDER),
    "dev_unlimited": list(MODULE_ORDER),
}


# Feature slug → owning module slug. Inverted from MODULE_CATALOG so
# feature names live next to their module above (one source of truth).
FEATURE_TO_MODULE: Dict[str, str] = {
    feature: slug
    for slug, mod in MODULE_CATALOG.items()
    for feature in mod["unlocks"]
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def get_module(slug: str) -> Dict[str, Any]:
    """Return the catalog entry for `slug`, or {} for unknown slugs."""
    return MODULE_CATALOG.get((slug or "").lower(), {})


def module_label(slug: str) -> str:
    return get_module(slug).get("label") or slug


def module_price_usd(slug: str) -> int:
    return int(get_module(slug).get("monthly_price_usd") or 0)


def module_unlocks(slug: str) -> List[str]:
    return list(get_module(slug).get("unlocks") or [])


def module_for_feature(feature_slug: str) -> Optional[str]:
    """Which module owns this feature? None if the feature isn't gated."""
    return FEATURE_TO_MODULE.get(feature_slug)


def implicit_modules_for_plan(plan_slug: Optional[str]) -> List[str]:
    """Modules a legacy-plan user implicitly owns. Empty list for free."""
    return list(PLAN_IMPLICIT_MODULES.get((plan_slug or "free").lower(), []))


def all_module_slugs() -> List[str]:
    """Catalog slugs in render order."""
    return list(MODULE_ORDER)
