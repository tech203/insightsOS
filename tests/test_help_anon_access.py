"""/help is a public route — anon visitors must be able to read it.

Originally /help was @login_required. Help docs are valuable SEO
content (same questions that drive Google + AI-engine queries) and
letting prospects skim them lowers conversion friction. Making the
page public meant fixing two anon dead-ends:
  - Topbar CTAs were "Back to Dashboard" + "Open Workspaces"
    (both @login_required) → bounced anon to /login.
  - No sidebar / back-nav for anon → page was orphaned.
"""
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Wallet


@pytest.fixture
def anon(app_ctx):
    return flask_app.test_client()


@pytest.fixture
def logged_in(app_ctx):
    u = User(
        email="help@test.com", password_hash=generate_password_hash("xx"),
        name="Helper", plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=100))
    db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = u.get_id()
        s["_fresh"] = True
    return c


def test_help_is_public_for_anon(anon):
    """Anon GET /help must return 200, not 302 → /login.
    Regression guard against re-adding @login_required."""
    resp = anon.get("/help", follow_redirects=False)
    assert resp.status_code == 200, (
        f"GET /help (anon) returned {resp.status_code}. "
        f"@login_required may have been re-added — /help is in the "
        f"sitemap as a public route and the footer links to it."
    )


def test_help_anon_view_has_signup_cta(anon):
    """Anon visitors on /help need a working path to convert —
    the topbar CTAs should be 'Run free audit' + 'Sign up', not
    'Back to Dashboard' (dead-end at /login).

    Check the BUTTON TEXT rather than the href — base.html's
    mobile-brand-mark links to / regardless of auth state, so a
    raw href="/dashboard" check would false-positive."""
    body = anon.get("/help").data.decode()
    assert 'href="/signup"' in body
    # The audit-form scroll anchor — the conversion path from the
    # landing-form PR.
    assert "#start-audit" in body
    # The "Back to Dashboard" button text must NOT appear for anon
    # (it does for authed users via the else branch).
    assert "Back to Dashboard" not in body, (
        "Anon /help still shows the 'Back to Dashboard' CTA — it's "
        "@login_required and would dead-end anon visitors at /login."
    )


def test_help_anon_view_has_back_nav_strip(anon):
    """Anon visitors get a 'Back to home' + 'Sign in' strip at
    the top — base.html doesn't render a sidebar for them."""
    body = anon.get("/help").data.decode()
    assert 'class="help-anon-nav"' in body
    assert 'href="/aeo-agency"' in body
    assert 'href="/login"' in body


def test_help_authed_view_keeps_dashboard_ctas(logged_in):
    """Logged-in users keep the original 'Back to Dashboard' +
    'Open Workspaces' topbar CTAs — and don't see the anon
    back-nav strip."""
    body = logged_in.get("/help").data.decode()
    assert 'href="/dashboard"' in body or 'href="/"' in body
    assert 'href="/clients"' in body
    # Anon-only nav strip element should be absent
    assert 'class="help-anon-nav"' not in body


def test_landing_footer_links_to_help(anon):
    """The landing footer Company column should expose /help so
    prospects can find it without guessing the URL."""
    body = anon.get("/aeo-agency").data.decode()
    assert 'href="/help"' in body
    assert "Help center" in body
