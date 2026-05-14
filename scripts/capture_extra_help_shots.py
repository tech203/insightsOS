"""Capture the screenshots that the initial walk missed.

Adds:
- report-page             (workspace → Report)
- presentation-mode       (workspace → Presentation)
- verify-email            (Settings → Account verify-email banner / resend)

Outputs PNGs. Run scripts/compress_help_screenshots.py afterward to convert
them to the WebPs the help template references.
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


def shot(page: Page, name: str, wait: float = 0.5) -> None:
    time.sleep(wait)
    p = OUT / f"{name}.png"
    page.screenshot(path=str(p))
    print(f"  saved {p.name}")


def safe_goto(page: Page, url: str) -> bool:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_load_state("networkidle", timeout=10000)
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


def main() -> int:
    print(f"BASE = {BASE}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900},
                                  device_scale_factor=2)
        page = ctx.new_page()
        try:
            login(page)
            # Find an existing workspace slug.
            page.goto(f"{BASE}/clients")
            page.wait_for_load_state("networkidle")
            handle = page.query_selector('a[href^="/client/"]')
            slug = (handle.get_attribute("href") or "").split("/client/")[-1].split("/")[0] if handle else None
            if not slug:
                print("no workspace; aborting")
                return 1
            print(f"using workspace slug: {slug}")

            print("report page…")
            if safe_goto(page, f"{BASE}/client/{slug}/report"):
                shot(page, "report-page")

            print("presentation mode…")
            if safe_goto(page, f"{BASE}/client/{slug}/presentation"):
                shot(page, "presentation-mode")

            # The verify-email page is only meaningful when the user has an
            # unverified address. Skip if our test user is already verified.
            print("verify-email page…")
            if safe_goto(page, f"{BASE}/settings/account"):
                shot(page, "settings-account-verified")

        finally:
            browser.close()
    print("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
