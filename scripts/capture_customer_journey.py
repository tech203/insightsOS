"""Capture the customer journey end-to-end for team review.

Walks the seeded `pro-test@example.com` user through the lifecycle:
landing → signup → first workspace → brand context → audit → results →
content → publish → growth calendar → settings → upgrade → invite.

Saves screenshots to docs/customer-journey/images/<step>.webp plus a
manifest.json the markdown doc reads to label each shot.

Run with:
    PORT=5002 python scripts/capture_customer_journey.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout


BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5002")
EMAIL = "pro-test@example.com"
PASSWORD = "test12345"

OUT = Path(__file__).resolve().parent.parent / "docs" / "customer-journey" / "images"
OUT.mkdir(parents=True, exist_ok=True)


# Each step: (slug, label, url, notes-for-team, requires_auth)
STEPS: list[dict] = [
    {"slug": "01-landing",       "label": "Landing page",            "url": "/aeo-agency",                                    "notes": "First impression. Are the value prop and CTA clear?",                              "auth": False},
    {"slug": "02-pricing",       "label": "Pricing",                 "url": "/pricing",                                       "notes": "Plan comparison + credit bundles. Do tiers feel distinct?",                        "auth": False},
    {"slug": "03-signup",        "label": "Sign up",                 "url": "/signup",                                        "notes": "Email/password vs. Google. Is the choice obvious?",                                "auth": False},
    {"slug": "04-login",         "label": "Log in",                  "url": "/login",                                         "notes": "Returning users. Forgot-password discoverable?",                                   "auth": False},
    {"slug": "05-workspaces",    "label": "Workspaces list",         "url": "/clients",                                       "notes": "Where the existing user lands after sign-in (if they have workspaces).",            "auth": True},
    {"slug": "06-workspace-new", "label": "Add a workspace",         "url": "/clients/new",                                   "notes": "First-run capture. Only name + website are required; the rest live behind a disclosure.", "auth": True},
    {"slug": "07-brand-context", "label": "Brand context",           "url": "/client/acme-bakery/brand-context",              "notes": "Soft step between workspace and first audit. Boost downstream content quality.",   "auth": True},
    {"slug": "08-brand-kit",     "label": "Brand kit studio",        "url": "/client/1/brand-kit",                            "notes": "Colors + typography. Used by generated pages and visuals.",                        "auth": True},
    {"slug": "09-run-audit",     "label": "Launch an audit",         "url": "/client/acme-bakery/run-audit",                  "notes": "Single click + credit confirmation. Wait time = first activation moment.",         "auth": True},
    {"slug": "10-workspace",     "label": "Workspace overview",      "url": "/client/acme-bakery",                            "notes": "The hub. Score, priorities, recommended actions, integrations.",                   "auth": True},
    {"slug": "11-visibility",    "label": "Visibility",              "url": "/client/acme-bakery/visibility",                 "notes": "Per-query state — where the brand appears and where it misses.",                   "auth": True},
    {"slug": "12-competitors",   "label": "Competitor gaps",         "url": "/client/acme-bakery/competitors",                "notes": "Who is showing up instead. Empty state nudges 'Run analysis'.",                    "auth": True},
    {"slug": "13-history",       "label": "Audit history",           "url": "/client/acme-bakery/history",                    "notes": "Returning users see deltas vs. the previous audit.",                               "auth": True},
    {"slug": "14-brief-form",    "label": "Content brief",           "url": "/client/acme-bakery/content-brief",              "notes": "Pick a query, pick a tone. The fastest path from finding to fixing.",              "auth": True},
    {"slug": "15-content-queue", "label": "Content queue",           "url": "/content-queue",                                 "notes": "Briefs and drafts in flight. Approve / AI-edit / publish from here.",              "auth": True},
    {"slug": "16-growth-cal",    "label": "Growth calendar",         "url": "/growth-calendar",                               "notes": "4-week schedule built from the latest audit.",                                     "auth": True},
    {"slug": "17-position",      "label": "Position tracking",       "url": "/position-tracking",                             "notes": "Score and per-query movement across audits over time.",                            "auth": True},
    {"slug": "18-answer-mon",    "label": "Answer monitor",          "url": "/answer-monitor",                                "notes": "Re-run individual prompts between audits.",                                        "auth": True},
    {"slug": "19-pricing-up",    "label": "Pricing (logged in)",     "url": "/pricing",                                       "notes": "Same page as step 2, but for an authenticated user — does the CTA shift?",         "auth": True},
    {"slug": "20-billing",       "label": "Settings → Billing",      "url": "/settings/billing",                              "notes": "Plan + Stripe portal + workspace seats.",                                          "auth": True},
    {"slug": "21-credits",       "label": "Settings → Credits",      "url": "/settings/credits",                              "notes": "Top up the wallet without changing plan.",                                         "auth": True},
    {"slug": "22-team",          "label": "Settings → Team",         "url": "/settings/team",                                 "notes": "Invite teammates, manage seats.",                                                  "auth": True},
    {"slug": "23-whitelabel",    "label": "Settings → White-label",  "url": "/settings/white-label",                          "notes": "Logo + brand colour applied to public reports + PDFs.",                            "auth": True},
    {"slug": "24-help",          "label": "Help center",             "url": "/help",                                          "notes": "Self-serve guidance for everything above.",                                        "auth": True},
]


def login(page: Page) -> None:
    page.goto(f"{BASE}/login")
    page.fill('input[name="email"]', EMAIL)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=10000)


def is_crash_page(path: Path) -> bool:
    """Werkzeug debugger has a distinctive dark blue 'Traceback' banner."""
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            px = im.load()
            # Sample the middle column for the dark-blue Traceback band.
            hits = 0
            for y in range(250, 350, 2):
                r, g, b = px[im.width // 2, y]
                if abs(r - 17) < 30 and abs(g - 85) < 30 and abs(b - 124) < 40:
                    hits += 1
            return hits > 3
    except Exception:
        return False


def capture(page: Page, step: dict) -> dict:
    url = f"{BASE}{step['url']}"
    png_path = OUT / f"{step['slug']}.png"
    webp_path = OUT / f"{step['slug']}.webp"
    status = "ok"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_load_state("networkidle", timeout=10000)
        time.sleep(0.4)
        page.screenshot(path=str(png_path))
    except PWTimeout:
        status = "timeout"
    except Exception as exc:
        status = f"error: {exc}"

    if status == "ok" and png_path.exists():
        if is_crash_page(png_path):
            status = "crash"

    if png_path.exists():
        # Convert to WebP, drop the PNG to keep the directory tidy.
        with Image.open(png_path) as im:
            im.convert("RGB").save(webp_path, format="WEBP", quality=85, method=6)
        png_path.unlink()

    return {**step, "status": status, "file": f"{step['slug']}.webp"}


def main() -> int:
    print(f"BASE = {BASE}")
    print(f"OUT  = {OUT}")
    results = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1440, "height": 900},
                            device_scale_factor=2)
        page = ctx.new_page()
        try:
            # Walk unauthenticated steps first.
            for step in STEPS:
                if not step["auth"]:
                    print(f"  {step['slug']}: {step['url']}")
                    results.append(capture(page, step))
            login(page)
            for step in STEPS:
                if step["auth"]:
                    print(f"  {step['slug']}: {step['url']}")
                    results.append(capture(page, step))
        finally:
            b.close()

    manifest = OUT.parent / "manifest.json"
    manifest.write_text(json.dumps(results, indent=2))
    print(f"manifest: {manifest}")

    # Quick summary
    crashed = [r["slug"] for r in results if r["status"] != "ok"]
    print(f"captured {len(results)} steps; {len(crashed)} not ok: {crashed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
