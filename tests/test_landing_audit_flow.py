"""Landing-page audit form → signup → auto-workspace flow.

Conversion-optimisation pattern: the visitor types their website
into the hero form, then signs up. We pre-fill the signup form
with their email and after signup auto-create the workspace +
redirect to brand-context (the same place /clients/new sends a
brand-new user). They never see "set up your workspace" because
we already did it for them.

Endpoints under test:
  POST /landing/start-audit   — captures form, stashes session
  GET  /signup                — pre-fills email/name from session
  POST /signup                — auto-creates workspace, redirects

Tests:
  - POST /landing/start-audit with valid fields stashes session
    + redirects anonymous visitor to /signup
  - Missing website / email → redirects with `audit=missing-…`
    query param (the form re-renders the error)
  - Logged-in visitor skips signup, jumps to /clients/new with
    the form data on the query string (lower priority but covered)
  - GET /signup pre-fills email + name from the stashed session
  - POST /signup with stashed intent: creates user, creates
    workspace via add_client(), redirects to brand-context
  - Workspace name is derived sensibly when the visitor didn't
    type one (extract "Acme" from "https://www.acme.com")
  - Empty/missing intent on signup falls through to /clients/new
    (the legacy first-time path)
"""
import pytest
from flask import session as flask_session
from werkzeug.security import generate_password_hash
from datetime import datetime, timezone

from app import app as flask_app, db, User, Wallet, Client


@pytest.fixture
def anon(app_ctx):
    return flask_app.test_client()


# ---------------------------------------------------------------------------
# /landing/start-audit
# ---------------------------------------------------------------------------

def test_start_audit_with_valid_fields_redirects_to_signup(anon):
    resp = anon.post("/landing/start-audit", data={
        "name": "Acme Coffee",
        "email": "owner@acme.example.com",
        "website": "https://acme.example.com",
        "industry": "Coffee",
        "location": "Singapore",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert "/signup" in resp.headers.get("Location", "")


def test_start_audit_without_website_redirects_with_error(anon):
    resp = anon.post("/landing/start-audit", data={
        "email": "owner@acme.example.com",
    }, follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers.get("Location", "")
    assert "audit=missing-website" in loc
    assert "#start-audit" in loc


def test_start_audit_without_email_redirects_with_error(anon):
    resp = anon.post("/landing/start-audit", data={
        "website": "https://acme.example.com",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert "audit=missing-email" in resp.headers.get("Location", "")


def test_start_audit_stashes_intent_in_session(anon):
    anon.post("/landing/start-audit", data={
        "name": "Acme",
        "email": "owner@acme.example.com",
        "website": "https://acme.example.com",
        "industry": "Coffee",
    })
    with anon.session_transaction() as s:
        intent = s.get("landing_audit_intent")
        assert intent is not None
        assert intent["website"] == "https://acme.example.com"
        assert intent["email"] == "owner@acme.example.com"
        assert intent["name"] == "Acme"
        assert intent["industry"] == "Coffee"


# ---------------------------------------------------------------------------
# /signup pre-fill from stashed intent
# ---------------------------------------------------------------------------

def test_signup_get_prefills_email_from_intent(anon):
    """After landing-form submit, GET /signup should show the
    email already filled in (saves the user from re-typing)."""
    anon.post("/landing/start-audit", data={
        "name": "Acme",
        "email": "owner@acme.example.com",
        "website": "https://acme.example.com",
    })
    resp = anon.get("/signup")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "owner@acme.example.com" in body
    assert "Acme" in body


# ---------------------------------------------------------------------------
# /signup POST → auto-workspace + brand-context redirect
# ---------------------------------------------------------------------------

def test_signup_with_intent_creates_workspace_and_redirects_to_brand_context(anon):
    # Step 1: visitor submits the landing audit form.
    anon.post("/landing/start-audit", data={
        "name": "Acme Coffee",
        "email": "owner@acme.example.com",
        "website": "https://acme.example.com",
        "industry": "Coffee",
        "location": "Singapore",
    })
    # Step 2: completes signup with the same email + a real password.
    resp = anon.post("/signup", data={
        "name": "Owner",
        "email": "owner@acme.example.com",
        "password": "knownpw1234",
        "confirm_password": "knownpw1234",
    }, follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers.get("Location", "")
    # CRITICAL: must redirect to the brand-context page of the
    # newly-created workspace (not to /clients/new — we already
    # made the workspace).
    assert "/client/" in loc
    assert "/brand-context" in loc

    # The workspace must actually exist in the DB.
    user = User.query.filter_by(email="owner@acme.example.com").first()
    assert user is not None
    workspace = Client.query.filter_by(user_id=user.id).first()
    assert workspace is not None
    assert workspace.website == "https://acme.example.com"
    assert workspace.industry == "Coffee"
    assert workspace.location == "Singapore"


def test_signup_derives_workspace_name_from_website_when_missing(anon):
    """If the visitor didn't type a name on the landing form, we
    should derive a friendly default from the domain instead of
    using the raw URL or an empty string."""
    anon.post("/landing/start-audit", data={
        "name": "",
        "email": "owner@enfactum.com",
        "website": "https://www.enfactum.com",
    })
    anon.post("/signup", data={
        "name": "Owner",
        "email": "owner@enfactum.com",
        "password": "knownpw1234",
        "confirm_password": "knownpw1234",
    })
    user = User.query.filter_by(email="owner@enfactum.com").first()
    workspace = Client.query.filter_by(user_id=user.id).first()
    # "https://www.enfactum.com" → "Enfactum" (strip scheme + www +
    # TLD, title-case).
    assert workspace.name == "Enfactum", f"got {workspace.name!r}"


def test_signup_consumes_intent_so_replays_dont_double_create(anon):
    """The session intent must be popped on signup — otherwise a
    re-visit to /signup (e.g. via the back button) would auto-
    create another workspace."""
    anon.post("/landing/start-audit", data={
        "email": "owner@acme.example.com",
        "website": "https://acme.example.com",
    })
    anon.post("/signup", data={
        "name": "Owner", "email": "owner@acme.example.com",
        "password": "knownpw1234", "confirm_password": "knownpw1234",
    })
    with anon.session_transaction() as s:
        assert "landing_audit_intent" not in s


def test_signup_without_intent_falls_through_to_clients_new(anon):
    """If the user signs up directly (not via the landing form),
    we should keep the legacy redirect to /clients/new — not
    try to invent a workspace from nothing."""
    resp = anon.post("/signup", data={
        "name": "Direct Signup",
        "email": "direct@example.com",
        "password": "knownpw1234",
        "confirm_password": "knownpw1234",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert "/clients/new" in resp.headers.get("Location", "")
    user = User.query.filter_by(email="direct@example.com").first()
    assert user is not None
    # No workspace created (would be bad — we'd be making up data).
    assert Client.query.filter_by(user_id=user.id).count() == 0


# ---------------------------------------------------------------------------
# Logged-in visitor flow
# ---------------------------------------------------------------------------

def test_logged_in_visitor_skips_signup_and_goes_to_clients_new(app_ctx):
    """If a logged-in user clicks 'Run my free audit' on the landing
    page (e.g. an existing customer wanting to add a workspace),
    they should jump straight to /clients/new with the form data
    on the query string — not through /signup."""
    u = User(
        email="existing@test.com",
        password_hash=generate_password_hash("xx"),
        name="Existing", plan="growth",
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
    resp = c.post("/landing/start-audit", data={
        "name": "New Workspace",
        "email": "existing@test.com",
        "website": "https://newco.example.com",
        "industry": "Retail",
    }, follow_redirects=False)
    assert resp.status_code == 302
    loc = resp.headers.get("Location", "")
    assert "/clients/new" in loc
    # Query params carry the form data so /clients/new can pre-fill.
    assert "website=https" in loc
    assert "from=landing" in loc


# ---------------------------------------------------------------------------
# Visual elements — make sure new visuals ship + don't get refactored away
# ---------------------------------------------------------------------------

def test_landing_page_contains_engine_strip(anon):
    """The 'tested across' AI engine strip must render in the hero —
    visual social proof for the form's CTA."""
    body = anon.get("/aeo-agency").data.decode()
    assert "aeo-engine-strip" in body
    # Each pill explicitly named so a refactor that drops one is caught.
    for engine in ("Google AI", "ChatGPT", "Perplexity", "Gemini"):
        assert engine in body, f"Engine pill missing: {engine}"


def test_landing_page_step_cards_have_icons(anon):
    """The 'How It Works' steps render with branded SVG icons, not
    plain numbered circles. If a refactor strips the icon class, this
    test fails with a clear pointer."""
    body = anon.get("/aeo-agency").data.decode()
    # 3 step cards × 1 icon container each
    assert body.count('class="aeo-step-icon"') == 3, (
        "Expected 3 aeo-step-icon containers (one per step card). "
        "The numbered-only fallback is back."
    )


def test_landing_page_contains_live_audit_mockup(anon):
    """The 'live audit' mockup section must render — it's the visceral
    conversion driver showing competitors in the AI answer."""
    body = anon.get("/aeo-agency").data.decode()
    # Section identifier + the three components (query, answer, takeaway)
    assert "aeo-mockup-grid" in body
    assert "aeo-mockup-query" in body
    assert "aeo-mockup-answer" in body
    assert "aeo-mockup-takeaway" in body
    # The "your brand: not mentioned" pill is what hooks the visitor —
    # check it's present so a copy refactor doesn't quietly delete it.
    assert "Your brand: not mentioned" in body
