"""
DarInsights pricing config — single source of truth for credit costs
and topup bundles.

Credits are denominated so 1 credit ≈ $1 of value for paying subscribers.
Free users (no monthly subscription) pay double per credit on topups,
which is why the bundles split into a `free` and a `subscriber` table.

Action costs were chosen so the cheapest action still has a healthy
margin over the underlying API spend (typically 15× — 1000×). When
adding a new action, fold its real cost into ACTION_CREDIT_COSTS rather
than scattering hard-coded amounts across the codebase.
"""

from __future__ import annotations

from typing import Any, Dict, List


# ---------------------------------------------------------------------------
# Per-action credit costs
# ---------------------------------------------------------------------------
# Each key here is a stable identifier for an internal operation. The
# corresponding integer is what gets debited from the user's wallet.
# Real per-call API costs (for reference; do not re-encode in code):
#
#   audit_run            ~ $0.20   (gpt-4.1-mini × 10–15 prompts + Tavily)
#   content_brief        ~ $0.001  (gpt-4o-mini, 2K tokens)
#   content_draft        ~ $0.003  (gpt-4o-mini, 5K tokens)
#   ai_edit_turn         ~ $0.002
#   visual_generation    ~ $0.06   (Placid)
#   alt_text_fix_batch   ~ $0.10   (gpt-4o-mini vision × N images)
#   answer_monitor_run_all     ~ $0.10  (10 prompts × 2 engines)
#   answer_monitor_run_single  ~ $0.02

ACTION_CREDIT_COSTS: Dict[str, int] = {
    # Audits are intentionally priced as a low-friction adoption hook
    # (1 credit ≈ break-even on a single audit). The margin lives in the
    # downstream actions a user takes after seeing the report.
    "audit_run": 1,
    "content_brief": 1,
    "content_draft": 2,
    "ai_edit_turn": 1,
    "visual_generation": 1,
    "alt_text_fix_batch": 2,
    "answer_monitor_run_all": 2,
    "answer_monitor_run_single": 1,
    # Marketplace audit ≈ 5 generated queries × N engines × OpenAI rate;
    # ~$0.10 worst-case → 20× margin at 2 credits.
    "marketplace_audit": 2,
    # Description rewrites: gpt-4o-mini once per thin product, ~$0.004
    # per rewrite. Worst case 50 thin products → ~$0.20. 3 credits ≈
    # 15× margin and lets a free user do a full catalog rewrite for $6.
    "description_rewrite_batch": 3,
    # Free-cost operations (Webflow CMS publishes, Shopify product reads,
    # Shopify product image PUTs) intentionally not listed — they cost
    # nothing on our side and stay free for the user.
}


def get_action_cost(action_key: str) -> int:
    """Look up credit cost for an action; unknown keys cost 1."""
    return int(ACTION_CREDIT_COSTS.get(action_key, 1))


# Credits granted on signup (free tier). Sized so a brand-new user —
# or an agency evaluating the product — can complete ONE full value
# loop without paying: audit_run (1) + content_brief (1) +
# content_draft (2) = 4, plus 1 credit of headroom so a mistimed
# click or a single retry doesn't strand them one credit short of
# the draft (the previous grant of 3 did exactly that — you could
# audit + brief but never see a finished draft, so the trial never
# demonstrated the core deliverable).
SIGNUP_STARTER_CREDITS = 5


# ---------------------------------------------------------------------------
# Plan tiers
# ---------------------------------------------------------------------------
SUBSCRIBER_PLANS = {"pro", "growth", "agency", "starter", "dev_unlimited"}


def is_subscriber(plan: str | None) -> bool:
    """True when the plan grants the lower (subscriber) credit price."""
    return (plan or "free").lower() in SUBSCRIBER_PLANS


# ---------------------------------------------------------------------------
# Subscription plan catalog
# ---------------------------------------------------------------------------
# Single source of truth for what each tier includes. Workspace and queue
# limits are read from here by app.py helpers; the pricing page renders
# directly from this dict so adding a tier is a one-place change.
#
# Effective per-credit cost on each paid plan (price ÷ monthly_credits):
#   Pro:     $29 / 75  = $0.39 / credit
#   Growth:  $79 / 175 = $0.45 / credit
#
# All paid tiers also unlock the $1/credit topup table (vs the free
# tier's $2/credit).

PLAN_CATALOG: Dict[str, Dict[str, Any]] = {
    "free": {
        "label": "Free",
        "monthly_price_usd": 0,
        "monthly_credits": 0,
        "workspace_limit": 1,
        "active_queue_limit": 3,
        "seat_limit": 1,
        "allows_workspace_addon": False,
        "allows_seat_addon": False,
        "tagline": "Run your first audits and see how the system works.",
        "features": [
            "1 workspace (no add-ons)",
            "1 seat",
            "3 active queue items",
            "Topup credits at $2 each",
        ],
        "popular": False,
    },
    "pro": {
        "label": "Pro",
        "monthly_price_usd": 29,
        "monthly_credits": 75,
        "workspace_limit": 3,
        "active_queue_limit": 25,
        "seat_limit": 3,
        "allows_workspace_addon": True,
        "allows_seat_addon": True,
        "allows_google_search_console": True,
        "tagline": "For solo operators and consultants getting serious about AI visibility.",
        "features": [
            "75 credits / month",
            "Up to 3 workspaces (+$9 / extra workspace / month)",
            "3 seats (+$5 / extra seat / month)",
            "25 active queue items",
            "Topup credits at $1 each",
            "Google Search Console + Analytics",
            "Multi-engine answer monitor",
            "Priority support",
        ],
        "popular": True,
    },
    "growth": {
        "label": "Growth",
        "monthly_price_usd": 79,
        "monthly_credits": 175,
        "workspace_limit": 10,
        "active_queue_limit": 50,
        "seat_limit": 10,
        "allows_workspace_addon": True,
        "allows_seat_addon": True,
        "allows_google_search_console": True,
        "tagline": "For agencies running AI visibility for their roster.",
        "features": [
            "175 credits / month",
            "Up to 10 workspaces (+$9 / extra workspace / month)",
            "10 seats (+$5 / extra seat / month)",
            "50 active queue items",
            "Topup credits at $1 each",
            "Google Search Console + Analytics",
            "Multi-engine answer monitor",
            "White-label add-on available",
        ],
        "popular": False,
    },
}

# Recurring monthly price for an extra workspace beyond the plan cap.
EXTRA_WORKSPACE_ADDON_PRICE_USD = 9
# Recurring monthly price for an extra team seat beyond the plan cap.
EXTRA_SEAT_ADDON_PRICE_USD = 5


# Public ordering for the pricing page.
PLAN_ORDER: List[str] = ["free", "pro", "growth"]


def get_plan(slug: str | None) -> Dict[str, Any]:
    """Look up a plan by slug; falls back to Free for unknown slugs."""
    return PLAN_CATALOG.get((slug or "free").lower(), PLAN_CATALOG["free"])


def list_public_plans() -> List[Dict[str, Any]]:
    """Plans exposed on the pricing page, in display order."""
    return [{"slug": s, **PLAN_CATALOG[s]} for s in PLAN_ORDER if s in PLAN_CATALOG]


def workspace_limit_for_plan(slug: str | None) -> int:
    return int(get_plan(slug).get("workspace_limit", 1))


def plan_allows_workspace_addon(slug: str | None) -> bool:
    """True if the plan allows buying additional workspaces beyond its
    base cap. Free is the only tier that doesn't."""
    return bool(get_plan(slug).get("allows_workspace_addon", False))


def seat_limit_for_plan(slug: str | None) -> int:
    return int(get_plan(slug).get("seat_limit", 1))


def plan_allows_seat_addon(slug: str | None) -> bool:
    """True if the plan supports inviting team members + buying extra
    seats. Pro and Growth only."""
    return bool(get_plan(slug).get("allows_seat_addon", False))


def plan_allows_google_search_console(slug: str | None) -> bool:
    """True if the plan unlocks the Google Search Console connector.
    Pro and Growth only — Free sees an upgrade nudge."""
    return bool(get_plan(slug).get("allows_google_search_console", False))


def active_queue_limit_for_plan(slug: str | None) -> int:
    return int(get_plan(slug).get("active_queue_limit", 3))


def monthly_credit_allowance(slug: str | None) -> int:
    return int(get_plan(slug).get("monthly_credits", 0))


# ---------------------------------------------------------------------------
# Topup bundles
# ---------------------------------------------------------------------------
# Two tables: subscribers buy at $1/credit; free users pay $2/credit.
# Both tiers have a small bulk discount that compounds with bundle size,
# rounded to readable prices ($38, $90, $85, etc.).

CREDIT_BUNDLES: Dict[str, List[Dict[str, Any]]] = {
    "free": [
        {"credits": 5, "price_usd": 10.00, "list_price_usd": 10.00, "discount_pct": 0},
        {"credits": 20, "price_usd": 38.00, "list_price_usd": 40.00, "discount_pct": 5},
        {"credits": 50, "price_usd": 90.00, "list_price_usd": 100.00, "discount_pct": 10},
    ],
    "subscriber": [
        {"credits": 5, "price_usd": 5.00, "list_price_usd": 5.00, "discount_pct": 0},
        {"credits": 20, "price_usd": 19.00, "list_price_usd": 20.00, "discount_pct": 5},
        {"credits": 50, "price_usd": 45.00, "list_price_usd": 50.00, "discount_pct": 10},
        {"credits": 100, "price_usd": 85.00, "list_price_usd": 100.00, "discount_pct": 15},
    ],
}


def get_bundles_for_plan(plan: str | None) -> List[Dict[str, Any]]:
    """Return the topup bundle list appropriate for the user's plan,
    annotated with per-credit price and a savings string for the UI."""
    bundles = CREDIT_BUNDLES["subscriber" if is_subscriber(plan) else "free"]
    annotated: List[Dict[str, Any]] = []
    for b in bundles:
        per_credit = b["price_usd"] / b["credits"] if b["credits"] else 0.0
        savings = b["list_price_usd"] - b["price_usd"]
        annotated.append(
            {
                **b,
                "per_credit": round(per_credit, 2),
                "savings_usd": round(savings, 2),
                "tier": "subscriber" if is_subscriber(plan) else "free",
            }
        )
    return annotated


def baseline_credit_price(plan: str | None) -> float:
    """The 1-credit price for the user's plan (subscriber: $1, free: $2)."""
    return 1.0 if is_subscriber(plan) else 2.0
