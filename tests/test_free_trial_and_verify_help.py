"""Two fixes from the agency-evaluation pass:

1. Free trial must cover ONE full value loop. Found: signup granted
   3 credits but audit(1)+brief(1)+draft(2)=4, so an evaluating
   agency could audit + brief but never see a finished draft — the
   trial never demonstrated the core deliverable.

2. Email-verification escape hatch. Verification gates billing;
   email delivery can fail for reasons the user can't fix (sandbox
   sender domain, bounce, provider outage). Without an in-app path
   the customer is permanently unable to upgrade. /verify-email/help
   is that path.
"""
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Wallet
from pricing import SIGNUP_STARTER_CREDITS, ACTION_CREDIT_COSTS


@pytest.fixture
def anon(app_ctx):
    return flask_app.test_client()


# ---------------------------------------------------------------------------
# 1. Free trial covers a full audit -> brief -> draft cycle
# ---------------------------------------------------------------------------

def test_starter_credits_cover_one_full_cycle():
    """The whole point of the trial: a new user can run the complete
    loop once without paying. audit + brief + draft must fit inside
    the starter grant (with headroom for a mistimed retry)."""
    full_cycle = (
        ACTION_CREDIT_COSTS["audit_run"]
        + ACTION_CREDIT_COSTS["content_brief"]
        + ACTION_CREDIT_COSTS["content_draft"]
    )
    assert SIGNUP_STARTER_CREDITS >= full_cycle, (
        f"Starter credits ({SIGNUP_STARTER_CREDITS}) < one full cycle "
        f"({full_cycle}). A trial user can't see the core deliverable "
        f"— the exact gap the agency eval flagged."
    )
    # Headroom: at least one spare credit so a single retry / mistimed
    # click doesn't strand them one credit short of the draft.
    assert SIGNUP_STARTER_CREDITS > full_cycle, (
        "No headroom over the exact cycle cost — one accidental "
        "double-click on audit and the user can't finish the loop."
    )


def test_email_signup_grants_starter_credits(anon):
    """The real /signup route must fund the wallet with the full
    starter grant (regression guard against the value drifting back
    to 3 in one path but not the other)."""
    page = anon.get("/signup")
    import re
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.data.decode())
    resp = anon.post("/signup", data={
        "name": "Cycle Tester",
        "email": "cycle@trial.test",
        "password": "trialpass1234",
        "confirm_password": "trialpass1234",
        "csrf_token": csrf.group(1) if csrf else "",
    }, follow_redirects=False)
    assert resp.status_code == 302
    u = User.query.filter_by(email="cycle@trial.test").first()
    assert u is not None and u.wallet is not None
    assert u.wallet.balance == SIGNUP_STARTER_CREDITS, (
        f"signup granted {u.wallet.balance}, expected "
        f"{SIGNUP_STARTER_CREDITS}"
    )


# ---------------------------------------------------------------------------
# 2. /verify-email/help — in-app escape hatch
# ---------------------------------------------------------------------------

@pytest.fixture
def unverified_client(app_ctx):
    u = User(
        email="unverified@trial.test",
        password_hash=generate_password_hash("xx"),
        name="Unverified", plan="free",
        email_verified_at=None,  # the blocked state
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=SIGNUP_STARTER_CREDITS))
    db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = u.get_id()
        s["_fresh"] = True
    return c, u


def test_verify_help_renders_clickable_link_for_unverified(unverified_client):
    c, u = unverified_client
    resp = c.get("/verify-email/help")
    assert resp.status_code == 200
    body = resp.data.decode()
    # A real verify link (button), not a raw URL in a flash.
    assert "/verify-email/" in body
    assert "Verify my email now" in body
    assert u.email in body


def test_verify_help_link_actually_verifies(unverified_client):
    """The button on the help page must consume a real token and
    flip the user to verified — closing the loop end to end."""
    c, u = unverified_client
    import re
    body = c.get("/verify-email/help").data.decode()
    # The page also contains the banner's /verify-email/help link and
    # the /verify-email/resend form action. The real token link uses
    # secrets.token_urlsafe(32) (~43 chars), far longer than the words
    # "help" / "resend" — require length >= 20 to isolate it.
    m = re.search(r'href="(/verify-email/[A-Za-z0-9_\-]{20,})"', body)
    assert m, "No verify token link found on the help page"
    c.get(m.group(1))  # click it
    db.session.refresh(u)
    assert u.email_verified_at is not None, (
        "Clicking the in-app verify link did NOT verify the user — "
        "the escape hatch is decorative."
    )


def test_verify_help_redirects_already_verified(app_ctx):
    u = User(
        email="already@trial.test",
        password_hash=generate_password_hash("xx"),
        name="Already", plan="free",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=5))
    db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = u.get_id()
        s["_fresh"] = True
    resp = c.get("/verify-email/help", follow_redirects=False)
    assert resp.status_code == 302
    assert "/settings" in resp.headers.get("Location", "")


def test_verify_help_requires_login(anon):
    resp = anon.get("/verify-email/help", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_verification_banner_links_to_help(unverified_client):
    """The banner must expose the escape hatch so a user whose
    email never arrived can discover it (it was buried in a
    transient flash before)."""
    c, _ = unverified_client
    body = c.get("/dashboard").data.decode()
    assert "/verify-email/help" in body, (
        "Verification banner doesn't link to the in-app escape "
        "hatch — a user with broken email delivery can't find it."
    )
