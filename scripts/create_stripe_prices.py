"""
One-shot helper to create the 12 Stripe Prices DarInsights expects.

Usage:
    source venv/bin/activate
    python scripts/create_stripe_prices.py

Reads STRIPE_SECRET_KEY from your .env. For each row in PRICES below,
creates a Stripe Product if one with that name doesn't exist yet,
then creates a Price with the right amount + recurring/one-time spec.
Writes the Price IDs back into .env automatically.

Idempotent: re-running won't create duplicates — it skips any env var
that's already filled with a real `price_…` value.

To dry-run first (no Stripe writes, just print what would happen):
    python scripts/create_stripe_prices.py --dry-run
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Tuple


# Resolve the .env file relative to the project root, not the script
# location, so it works whether invoked from project root or from
# inside the worktree.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


# Each entry: (env_var_name, product_name, amount_cents, mode)
# mode: "one_time" | "monthly"
PRICES: list[Tuple[str, str, int, str]] = [
    # Free-tier credit bundles ($2/credit)
    ("STRIPE_PRICE_BUNDLE_5_FREE",   "5 credits (Free plan)",   1000,  "one_time"),
    ("STRIPE_PRICE_BUNDLE_20_FREE",  "20 credits (Free plan)",  3800,  "one_time"),
    ("STRIPE_PRICE_BUNDLE_50_FREE",  "50 credits (Free plan)",  9000,  "one_time"),
    # Subscriber-tier credit bundles ($1/credit)
    ("STRIPE_PRICE_BUNDLE_5_SUB",    "5 credits (Subscriber)",   500,  "one_time"),
    ("STRIPE_PRICE_BUNDLE_20_SUB",   "20 credits (Subscriber)", 1900,  "one_time"),
    ("STRIPE_PRICE_BUNDLE_50_SUB",   "50 credits (Subscriber)", 4500,  "one_time"),
    ("STRIPE_PRICE_BUNDLE_100_SUB",  "100 credits (Subscriber)", 8500, "one_time"),
    # Subscription tiers (Pro $29 + Growth $79 — match existing
    # Stripe Prices when present, otherwise create at these amounts).
    ("STRIPE_PRICE_PLAN_PRO",        "DarInsights Pro",          2900, "monthly"),
    ("STRIPE_PRICE_PLAN_GROWTH",     "DarInsights Growth",       7900, "monthly"),
    # Add-ons
    ("STRIPE_PRICE_EXTRA_WORKSPACE", "Extra Workspace",           900, "monthly"),
    ("STRIPE_PRICE_EXTRA_SEAT",      "Extra Team Seat",           500, "monthly"),
    # Modular subscription line items (foundation — not yet surfaced
    # in the dashboard, but the Prices need to exist in Stripe for the
    # backend wiring to work end-to-end).
    ("STRIPE_PRICE_MODULE_ACTION_PLAN", "Action Plan Automation", 1900, "monthly"),
    ("STRIPE_PRICE_MODULE_WEBSITE",     "Website Module",         2900, "monthly"),
    ("STRIPE_PRICE_MODULE_ECOMMERCE",   "Ecommerce Module",       2900, "monthly"),
    ("STRIPE_PRICE_MODULE_MARKETPLACE", "Marketplace Module",     1900, "monthly"),
    ("STRIPE_PRICE_MODULE_REAL_DATA",   "Real-Data Stack",        2900, "monthly"),
    ("STRIPE_PRICE_MODULE_AGENCY",      "Agency Module",          4900, "monthly"),
]


def load_env() -> dict[str, str]:
    """Read .env into a dict. Preserves order via subsequent rewrite."""
    if not ENV_PATH.exists():
        return {}
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def write_env_value(key: str, value: str) -> None:
    """Update or append a single key in .env, preserving every other
    line untouched (incl. comments)."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    pattern = re.compile(rf"^{re.escape(key)}\s*=")
    found = False
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


def looks_like_real_price_id(value: str) -> bool:
    return bool(value) and value.startswith("price_") and len(value) > 8


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    env = load_env()
    secret_key = env.get("STRIPE_SECRET_KEY") or os.getenv("STRIPE_SECRET_KEY")
    if not secret_key or not (secret_key.startswith("sk_test_") or secret_key.startswith("sk_live_")):
        print("ERROR: STRIPE_SECRET_KEY missing or malformed in .env.")
        print("Set it (test mode is fine for dev) and re-run.")
        return 1

    if dry_run:
        print("[DRY RUN] No Stripe writes — showing what would be created.\n")
    else:
        print(f"Mode: {'TEST' if 'sk_test_' in secret_key else 'LIVE'}\n")

    try:
        import stripe  # type: ignore
    except ImportError:
        print("ERROR: stripe package isn't installed. `pip install stripe` and re-run.")
        return 1
    stripe.api_key = secret_key

    skipped = 0
    created = 0
    for env_key, product_name, amount_cents, mode in PRICES:
        existing = env.get(env_key, "")
        if looks_like_real_price_id(existing):
            print(f"  ✓ {env_key:32s} already set ({existing[:18]}…) — skipping")
            skipped += 1
            continue

        amount_str = f"${amount_cents / 100:.2f}"
        recurring_str = "/month" if mode == "monthly" else " one-time"
        action = f"Create {product_name!r} → {amount_str}{recurring_str}"

        if dry_run:
            print(f"  → {env_key:32s} {action}")
            continue

        try:
            # Find or create the Product. Using a deterministic name
            # match means re-runs are idempotent at the Product level
            # too (won't litter your dashboard).
            existing_products = stripe.Product.list(limit=100, active=True).data
            product = next(
                (p for p in existing_products if p.name == product_name), None
            )
            if not product:
                product = stripe.Product.create(name=product_name)

            # Create the Price.
            price_kwargs = {
                "product": product.id,
                "unit_amount": amount_cents,
                "currency": "usd",
            }
            if mode == "monthly":
                price_kwargs["recurring"] = {"interval": "month"}
            price = stripe.Price.create(**price_kwargs)

            write_env_value(env_key, price.id)
            print(f"  ✓ {env_key:32s} {action}  ←  {price.id}")
            created += 1
        except Exception as exc:
            print(f"  ✗ {env_key:32s} FAILED: {exc}")

    print()
    print(f"Done. Created {created} new Prices, skipped {skipped} already-set.")
    if not dry_run and created:
        print(f"\n.env updated at {ENV_PATH}")
        print("Restart your Flask app to pick up the new env values.")
    if dry_run:
        print("\nRun without --dry-run to actually create them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
