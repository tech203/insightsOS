"""Capture screenshots for the help center.

Walks the app as the seeded `pro-test@example.com` user and saves a screenshot
for each `data-shot` placeholder referenced in templates/help.html.

Run with:
    PORT=5002 python scripts/capture_help_screenshots.py

Outputs PNGs. Run scripts/compress_help_screenshots.py afterward to convert
them to the WebPs the help template references (~66% size reduction).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout


BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5002")
EMAIL = "pro-test@example.com"
PASSWORD = "test12345"

OUT = Path(__file__).resolve().parent.parent / "static" / "images" / "help"
OUT.mkdir(parents=True, exist_ok=True)


def shot(page: Page, name: str, *, full: bool = False, wait: float = 0.4) -> None:
    """Save a screenshot under static/images/help/<name>.png."""
    time.sleep(wait)
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path), full_page=full)
    print(f"  saved {path.relative_to(OUT.parent.parent.parent)}")


def safe_goto(page: Page, url: str) -> bool:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        return True
    except PWTimeout:
        print(f"  timeout: {url}")
        return False


def login(page: Page) -> None:
    page.goto(f"{BASE}/login")
    page.fill('input[name="email"]', EMAIL)
    page.fill('input[name="password"]', PASSWORD)
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=10000)


def ensure_workspace(page: Page) -> str | None:
    """Return URL slug of a workspace, creating one if needed."""
    page.goto(f"{BASE}/clients")
    page.wait_for_load_state("networkidle")
    handle = page.query_selector('a[href^="/client/"]')
    if handle:
        href = handle.get_attribute("href") or ""
        cid = href.split("/client/")[-1].split("/")[0]
        print(f"  using existing workspace: {cid}")
        return cid

    print("  creating workspace…")
    page.goto(f"{BASE}/clients/new")
    page.wait_for_load_state("networkidle")
    # First-workspace mode collapses industry/location inside a <details>.
    # Open it before filling.
    disclosure = page.query_selector("details.workspace-form-disclosure")
    if disclosure:
        page.evaluate(
            "el => el.setAttribute('open', '')",
            disclosure,
        )
    for sel, val in [
        ('input[name="name"]', "Acme Bakery"),
        ('input[name="website"]', "https://acmebakery.example.com"),
        ('input[name="industry"]', "Food & beverage"),
        ('input[name="location"]', "Brooklyn, NY"),
    ]:
        field = page.query_selector(sel)
        if field:
            try:
                field.fill(val)
            except Exception as exc:
                print(f"  skip {sel}: {exc!r}")
    page.click('button[type="submit"]')
    page.wait_for_load_state("networkidle", timeout=15000)
    # After save we should land on /client/<id>
    href = page.url
    if "/client/" in href:
        cid = href.split("/client/")[-1].split("/")[0].split("?")[0]
        print(f"  created workspace: {cid}")
        return cid
    return None


def capture_auth(page: Page) -> None:
    print("auth pages…")
    # Logout first to capture auth pages cleanly.
    safe_goto(page, f"{BASE}/logout")
    safe_goto(page, f"{BASE}/signup")
    shot(page, "signup-form")
    safe_goto(page, f"{BASE}/forgot-password")
    shot(page, "forgot-password")
    safe_goto(page, f"{BASE}/login")
    shot(page, "login-form")


def capture_public(page: Page) -> None:
    print("public pages…")
    safe_goto(page, f"{BASE}/pricing")
    shot(page, "pricing-page", full=True)


def capture_logged_in(page: Page, cid: str | None) -> None:
    print("dashboard…")
    safe_goto(page, f"{BASE}/dashboard")
    shot(page, "quick-start-dashboard")

    print("workspaces list…")
    safe_goto(page, f"{BASE}/clients")
    shot(page, "workspaces-list")

    print("new workspace form…")
    safe_goto(page, f"{BASE}/clients/new")
    shot(page, "workspace-new-form")

    if not cid:
        print("no workspace id — skipping workspace-scoped pages.")
        return

    print(f"workspace overview ({cid})…")
    safe_goto(page, f"{BASE}/client/{cid}")
    shot(page, "workspace-overview", full=True)

    print("brand context…")
    safe_goto(page, f"{BASE}/client/{cid}/brand-context")
    shot(page, "brand-context")

    # These routes take an integer client_id. Fetch it from the workspace overview page.
    int_id = page.evaluate(
        "() => { const m = document.body.innerHTML.match(/\\/client\\/(\\d+)\\b/); return m ? m[1] : null }"
    )
    print(f"  integer id = {int_id}")

    if int_id:
        print("brand kit…")
        safe_goto(page, f"{BASE}/client/{int_id}/brand-kit")
        shot(page, "brand-kit")

    print("run audit page…")
    safe_goto(page, f"{BASE}/client/{cid}/run-audit")
    shot(page, "audit-run")

    print("visibility page…")
    safe_goto(page, f"{BASE}/client/{cid}/visibility")
    shot(page, "visibility-page")

    print("competitors…")
    safe_goto(page, f"{BASE}/client/{cid}/competitors")
    shot(page, "competitors-page")

    print("history…")
    safe_goto(page, f"{BASE}/client/{cid}/history")
    shot(page, "workspace-history")

    print("content brief form…")
    safe_goto(page, f"{BASE}/client/{cid}/content-brief")
    shot(page, "brief-form")

    print("content draft form…")
    safe_goto(page, f"{BASE}/client/{cid}/content-draft")
    shot(page, "draft-form")

    print("query ideas…")
    safe_goto(page, f"{BASE}/client/{cid}/query-ideas")
    shot(page, "query-ideas")

    print("growth plan…")
    safe_goto(page, f"{BASE}/client/{cid}/growth-plan")
    shot(page, "growth-plan")

    if int_id:
        print("marketplace audits…")
        safe_goto(page, f"{BASE}/marketplace-audits/{int_id}")
        shot(page, "marketplace-audits")

        print("integrations modules…")
        safe_goto(page, f"{BASE}/client/{int_id}/integrations/modules")
        shot(page, "integrations-grid")

    print("website builder…")
    safe_goto(page, f"{BASE}/client/{cid}/website-builder")
    shot(page, "website-builder")


def capture_top_nav(page: Page) -> None:
    print("growth calendar…")
    safe_goto(page, f"{BASE}/growth-calendar")
    shot(page, "growth-calendar", full=True)

    print("position tracking…")
    safe_goto(page, f"{BASE}/position-tracking")
    shot(page, "position-tracking")

    print("answer monitor…")
    safe_goto(page, f"{BASE}/answer-monitor")
    shot(page, "answer-monitor")

    print("content queue…")
    safe_goto(page, f"{BASE}/content-queue")
    shot(page, "content-queue")


def capture_settings(page: Page) -> None:
    print("settings pages…")
    for slug in (
        "account",
        "billing",
        "credits",
        "referrals",
        "preferences",
        "team",
        "white-label",
    ):
        ok = safe_goto(page, f"{BASE}/settings/{slug}")
        if ok:
            shot(page, f"settings-{slug}")


def main() -> int:
    print(f"BASE = {BASE}")
    print(f"OUT  = {OUT}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=2)
        page = ctx.new_page()
        try:
            capture_auth(page)
            login(page)
            capture_public(page)
            cid = ensure_workspace(page)
            capture_logged_in(page, cid)
            capture_top_nav(page)
            capture_settings(page)
        finally:
            browser.close()
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
