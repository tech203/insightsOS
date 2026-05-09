"""
Stripe checkout helpers.

Two flows:

1. Credit topup bundles — one-time payments, Checkout `mode=payment`,
   credits land in the wallet via the webhook on success.
2. Subscription plans — recurring, Checkout `mode=subscription`,
   updates `User.plan` and grants the first month's credits via the
   webhook.

Price IDs come from env vars so test/live keys can swap cleanly. When
STRIPE_SECRET_KEY isn't set, every helper raises StripeNotConfigured —
routes catch that and surface a "Stripe isn't configured on this
server" flash to the user.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


class StripeNotConfigured(Exception):
    """Raised when STRIPE_SECRET_KEY (or a price ID) is missing."""


# Bundle credits → env var name for the corresponding Stripe Price ID.
# Each bundle has both a free-tier ($2/credit) and subscriber-tier
# ($1/credit) Price ID; the helper picks the right one per user plan.
BUNDLE_PRICE_ENV_FREE: Dict[int, str] = {
    5: "STRIPE_PRICE_BUNDLE_5_FREE",
    20: "STRIPE_PRICE_BUNDLE_20_FREE",
    50: "STRIPE_PRICE_BUNDLE_50_FREE",
}
BUNDLE_PRICE_ENV_SUB: Dict[int, str] = {
    5: "STRIPE_PRICE_BUNDLE_5_SUB",
    20: "STRIPE_PRICE_BUNDLE_20_SUB",
    50: "STRIPE_PRICE_BUNDLE_50_SUB",
    100: "STRIPE_PRICE_BUNDLE_100_SUB",
}

# Plan slug → env var name for the recurring Stripe Price ID.
PLAN_PRICE_ENV: Dict[str, str] = {
    "pro": "STRIPE_PRICE_PLAN_PRO",
    "growth": "STRIPE_PRICE_PLAN_GROWTH",
}


def is_stripe_configured() -> bool:
    """True when the secret key is set (placeholders are rejected)."""
    key = os.getenv("STRIPE_SECRET_KEY") or ""
    return bool(key) and not key.startswith("your_") and key.startswith(("sk_", "rk_"))


def _stripe_module():
    """Lazy-import + configure the stripe SDK."""
    if not is_stripe_configured():
        raise StripeNotConfigured("STRIPE_SECRET_KEY is not configured.")
    import stripe as _stripe
    _stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    return _stripe


def _bundle_price_id(credits: int, is_subscriber: bool) -> str:
    table = BUNDLE_PRICE_ENV_SUB if is_subscriber else BUNDLE_PRICE_ENV_FREE
    env_var = table.get(int(credits))
    if not env_var:
        raise StripeNotConfigured(
            f"No bundle price registered for {credits} credits."
        )
    price_id = os.getenv(env_var)
    if not price_id:
        raise StripeNotConfigured(f"{env_var} is not set.")
    return price_id


def _plan_price_id(plan_slug: str) -> str:
    env_var = PLAN_PRICE_ENV.get((plan_slug or "").lower())
    if not env_var:
        raise StripeNotConfigured(f"Plan {plan_slug} has no Stripe Price configured.")
    price_id = os.getenv(env_var)
    if not price_id:
        raise StripeNotConfigured(f"{env_var} is not set.")
    return price_id


def create_bundle_checkout_session(
    *,
    user_id: int,
    user_email: Optional[str],
    credits: int,
    is_subscriber: bool,
    success_url: str,
    cancel_url: str,
) -> Dict[str, Any]:
    """One-time payment Checkout Session for a credit topup bundle."""
    stripe = _stripe_module()
    price_id = _bundle_price_id(credits, is_subscriber)
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=str(user_id),
        customer_email=user_email,
        metadata={
            "kind": "bundle",
            "user_id": str(user_id),
            "credits": str(credits),
            "tier": "subscriber" if is_subscriber else "free",
        },
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return {"url": session.url, "id": session.id}


def create_subscription_checkout_session(
    *,
    user_id: int,
    user_email: Optional[str],
    plan_slug: str,
    success_url: str,
    cancel_url: str,
) -> Dict[str, Any]:
    """Recurring subscription Checkout Session for a plan tier."""
    stripe = _stripe_module()
    price_id = _plan_price_id(plan_slug)
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=str(user_id),
        customer_email=user_email,
        metadata={
            "kind": "subscription",
            "user_id": str(user_id),
            "plan_slug": plan_slug,
        },
        success_url=success_url,
        cancel_url=cancel_url,
    )
    return {"url": session.url, "id": session.id}


def construct_webhook_event(payload_bytes: bytes, signature_header: str):
    """Parse + verify a Stripe webhook payload. Raises on signature
    mismatch or missing webhook secret."""
    stripe = _stripe_module()
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise StripeNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")
    return stripe.Webhook.construct_event(payload_bytes, signature_header, secret)


def create_billing_portal_session(
    *,
    customer_id: str,
    return_url: str,
) -> Dict[str, Any]:
    """Open the Stripe-hosted billing portal so the user can manage
    payment method / cancel subscription."""
    stripe = _stripe_module()
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=return_url,
    )
    return {"url": session.url}
