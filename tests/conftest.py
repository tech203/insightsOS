"""
Shared pytest fixtures for the DarInsights test suite.

Design:
- app is imported once per session; tables are created / dropped per
  test so each test gets a clean DB without paying the cost of
  re-importing the Flask app + all its routes.
- CSRF is disabled so test clients can POST without threading tokens
  through every fixture.
- Stripe stays unconfigured during tests; helpers that call it raise
  StripeNotConfigured, which the production code already handles
  gracefully (it's the dev-mode path that prod uses on first deploy).
- A test SQLite file lives at instance/test.db rather than :memory:
  because Flask-SQLAlchemy creates multiple connections; an in-memory
  database has different visibility per connection unless you wire up
  a StaticPool, which is more setup than it's worth at this scale.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Env setup must happen BEFORE app is imported. app.py reads these at
# module-import time to wire SQLAlchemy / Flask-Login / etc.
# ---------------------------------------------------------------------------
os.environ.setdefault("DATABASE_URL", "sqlite:///test.db")
os.environ.setdefault("SECRET_KEY", "test-secret-not-for-prod")
# Stop the import-time launch-config warning loop from spamming test
# output for things that are intentionally unset in CI.
os.environ.setdefault("STRIPE_SECRET_KEY", "")
os.environ.setdefault("RESEND_FROM", "test@example.com")

import pytest  # noqa: E402
from datetime import datetime  # noqa: E402
from dtutils import utcnow

# Import after env is primed. The app module wires the DB engine at
# import time, so this can only be done once env is set.
import app as app_module  # noqa: E402
from app import app as flask_app, db  # noqa: E402
from app import User, Wallet, CreditTransaction  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


# ---------------------------------------------------------------------------
# Session-scoped app config — runs once, before any tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _configure_app():
    """Flip the Flask app into test mode for the duration of the run."""
    flask_app.config["TESTING"] = True
    # CSRF is enforced on real form posts via Flask-WTF, but disabling
    # it here lets test fixtures hit POST routes without minting a token
    # per request. The handlers we're testing don't themselves care
    # about CSRF; that's a middleware concern covered separately.
    flask_app.config["WTF_CSRF_ENABLED"] = False
    yield flask_app


# ---------------------------------------------------------------------------
# Per-test fixtures — fresh DB every time
# ---------------------------------------------------------------------------

@pytest.fixture
def app_ctx():
    """Push an app context and create all tables. Tears down by
    rolling back the session and dropping the tables so the next
    test sees a clean DB."""
    with flask_app.app_context():
        db.create_all()
        yield flask_app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def user(app_ctx):
    """A standard 'Pro plan, 10 credits, email verified' user.

    Most tests want a user that can do things — the Pro plan + verified
    email + funded wallet is the default. Tests that care about Free /
    unverified / broke variants build their own user.

    Backfills a recent monthly_allowance CreditTransaction so the
    @app.before_request grant hook doesn't fire mid-test and silently
    top up the wallet by 75 credits — which would break any test
    asserting on a precise balance.
    """
    u = User(
        email="default@test.com",
        password_hash=generate_password_hash("xxxxxxxx"),
        name="Default Test User",
        plan="pro",
        email_verified_at=utcnow(),
    )
    db.session.add(u)
    db.session.flush()
    u.wallet = Wallet(user_id=u.id, balance=10)
    db.session.add(u.wallet)
    # Mark the monthly allowance as already granted in this 28-day
    # window. Without this, the before_request hook tops paid users
    # up by 75 credits on every request, which makes wallet-balance
    # assertions flaky.
    db.session.add(CreditTransaction(
        user_id=u.id,
        type="monthly_allowance",
        amount=75,
        balance_after=10,
        notes="Test fixture: pre-granted to suppress before_request top-up",
    ))
    db.session.commit()
    return u


@pytest.fixture
def make_user(app_ctx):
    """Factory for tests that need multiple users or specific shapes.

    Usage:
        def test_x(make_user):
            free = make_user(plan="free", balance=3)
            pro = make_user(plan="pro", balance=100, email="pro@x.com")
    """
    counter = {"i": 0}

    def _make(
        *,
        email=None,
        name=None,
        plan="pro",
        balance=10,
        email_verified=True,
        role="user",
        suppress_monthly_grant=True,
    ):
        counter["i"] += 1
        u = User(
            email=email or f"user{counter['i']}@test.com",
            password_hash=generate_password_hash("xxxxxxxx"),
            name=name or f"User {counter['i']}",
            plan=plan,
            role=role,
            email_verified_at=utcnow() if email_verified else None,
        )
        db.session.add(u)
        db.session.flush()
        u.wallet = Wallet(user_id=u.id, balance=balance)
        db.session.add(u.wallet)
        # Suppress the before_request monthly-credit top-up for paid
        # plans by default (see the `user` fixture above for the
        # rationale). Tests that specifically exercise the
        # grant_monthly_credits_if_due path (e.g. period rollover)
        # pass suppress_monthly_grant=False so the grant fires when
        # they expect it to.
        if suppress_monthly_grant and plan in ("pro", "growth", "agency", "starter"):
            db.session.add(CreditTransaction(
                user_id=u.id,
                type="monthly_allowance",
                amount=0,
                balance_after=balance,
                notes="Test fixture: pre-granted to suppress before_request top-up",
            ))
        db.session.commit()
        return u

    return _make


@pytest.fixture
def logged_in_client(user):
    """A Flask test client with the default user logged in.

    Tests that hit HTTP routes (vs calling helpers directly) use this.
    """
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c
