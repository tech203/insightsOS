"""/pricing CTAs must route correctly for anonymous visitors.

The pricing page is public (no @login_required), but every paid-
action endpoint is login-only. Pre-fix, anonymous CTAs like
"Manage subscription", "Buy Credits", "Add Agency Layer" all
linked to /settings/* which bounced anon visitors to /login and
dropped them on a settings page with no context.

These tests lock in the anon-vs-auth conditional routing so a
future template refactor can't silently re-introduce the dead end.
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
        email="paid@test.com", password_hash=generate_password_hash("xx"),
        name="Paid", plan="growth",
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


# ---------------------------------------------------------------------------
# Anonymous visitor — CTAs must route to /signup, not /settings/*
# ---------------------------------------------------------------------------

def test_pricing_renders_for_anon(anon):
    resp = anon.get("/pricing")
    assert resp.status_code == 200


def test_anon_pricing_shows_back_nav_strip(anon):
    """Anonymous visitors need a path BACK to landing — without it,
    /pricing is a dead end (no sidebar to navigate from)."""
    body = anon.get("/pricing").data.decode()
    assert "pricing-anon-nav" in body
    assert "Back to home" in body
    assert 'href="/aeo-agency"' in body
    # Plus a sign-in escape hatch for returning users.
    assert "Sign in" in body
    assert 'href="/login"' in body


def test_anon_pricing_ctas_link_to_signup_not_settings(anon):
    """Every paid-action CTA on the anon view must go to /signup —
    NOT to /settings/billing or /settings/credits (which would
    bounce to /login and lose the visitor's context).

    Check `href="/settings/..."` rather than the bare path string —
    the CSS class names (.settings-...) and inline styles would
    otherwise false-positive the assertion."""
    body = anon.get("/pricing").data.decode()
    assert 'href="/settings/billing"' not in body, (
        "Anon pricing page has a CTA to /settings/billing. "
        "Anonymous visitors would be bounced to /login and lose "
        "context. Route to /signup instead via the cta_buy_url var."
    )
    assert 'href="/settings/credits"' not in body, (
        "Anon pricing page has a CTA to /settings/credits — same "
        "dead-end problem. Route to /signup instead."
    )
    # And /signup must be reachable from the page (at minimum the
    # hero + comparison CTAs + topup buttons).
    assert 'href="/signup"' in body


def test_anon_pricing_plan_buttons_route_to_signup(anon):
    """Each paid plan (Pro, Growth) should offer 'Sign up for Pro' /
    'Sign up for Growth' rather than the Stripe checkout URL
    (which is @login_required)."""
    body = anon.get("/pricing").data.decode()
    # The plan-card branches should say 'Sign up for ...' on anon
    assert "Sign up for Pro" in body
    assert "Sign up for Growth" in body
    # And /stripe/checkout/plan/... must NOT be in the anon HTML.
    assert "/stripe/checkout/plan/" not in body, (
        "Anon page rendered Stripe checkout URLs — these require "
        "login and would dead-end the visitor."
    )


# ---------------------------------------------------------------------------
# Authenticated user — CTAs keep their original /settings/* targets
# ---------------------------------------------------------------------------

def test_authed_pricing_keeps_settings_ctas(logged_in):
    """Logged-in users keep getting taken to /settings/billing and
    /settings/credits (the right destination for them) and to
    the Stripe checkout URL on the paid plans."""
    body = logged_in.get("/pricing").data.decode()
    # Authenticated user gets the in-app destinations
    assert 'href="/settings/billing"' in body
    assert 'href="/settings/credits"' in body
    # And the anon-only "Back to home" strip element is hidden.
    # (The CSS class name is in <style> on every render — check the
    # actual HTML class attribute that only appears on anon.)
    assert 'class="pricing-anon-nav"' not in body


def test_authed_pricing_shows_stripe_checkout_links(logged_in):
    """For authenticated users (with Stripe configured OR not), the
    paid-plan buttons either link to /stripe/checkout/plan/<slug>
    OR show the 'Checkout unavailable' disabled state. They should
    NOT say 'Sign up for ...' (that's the anon path)."""
    body = logged_in.get("/pricing").data.decode()
    assert "Sign up for Pro" not in body
    assert "Sign up for Growth" not in body
