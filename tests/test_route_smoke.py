"""Smoke test: every parameterless GET route returns < 500.

Probes the entire URL surface as a logged-in admin to catch any route
that's silently broken (missing column, undefined template var, bad
import, etc.). Not a correctness test — just a "does this 500 in your
face" alarm.

A route is considered healthy if its response code is anything except
the 5xx server-error range. Auth redirects (302), not-found (404), and
forbidden (403) are all acceptable — we only fail on 500.
"""
import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Wallet, CreditTransaction
from tests.conftest import utcnow


# Every no-parameter GET route in the app, gathered from
# `app.url_map.iter_rules()`. Routes with `<...>` placeholders are
# excluded — they need real IDs to probe meaningfully.
ROUTES = [
    "/",
    "/admin",
    "/admin/campaigns",
    "/admin/campaigns/new",
    "/admin/interest",
    "/admin/interest/export.csv",
    "/admin/payments",
    "/admin/users",
    "/aeo-agency",
    "/answer-monitor",
    "/api/audits",
    "/api/clients",
    "/api/wallet",
    "/audit/new",
    "/clients",
    "/clients/new",
    "/content",
    "/content-queue",
    "/content/brief/new",
    "/dashboard",
    "/forgot-password",
    "/growth-calendar",
    "/help",
    "/integrations/webflow/settings",
    "/login",
    "/logout",
    "/position-tracking",
    "/pricing",
    "/prompt-detail",
    "/settings",
    "/settings/account",
    "/settings/billing",
    "/settings/credits",
    "/settings/preferences",
    "/settings/referrals",
    "/settings/team",
    "/settings/white-label",
    "/signup",
    "/start-audit",
    "/stripe/portal",
    "/stripe/success",
]


@pytest.fixture
def admin_client(app_ctx):
    """A test client logged in as an admin user with funded wallet."""
    u = User(
        email="smoke-admin@test.com",
        password_hash=generate_password_hash("xxxxxxxx"),
        name="Smoke Admin",
        plan="growth",
        role="admin",
        email_verified_at=utcnow(),
    )
    db.session.add(u)
    db.session.flush()
    u.wallet = Wallet(user_id=u.id, balance=100)
    db.session.add(u.wallet)
    db.session.add(CreditTransaction(
        user_id=u.id,
        type="monthly_allowance",
        amount=0,
        balance_after=100,
        notes="Smoke fixture",
    ))
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
    return c


@pytest.mark.parametrize("path", ROUTES)
def test_route_no_500(admin_client, path):
    """Every route should respond with < 500. 4xx is fine; 5xx is a bug."""
    resp = admin_client.get(path, follow_redirects=False)
    assert resp.status_code < 500, (
        f"{path} returned {resp.status_code}\n"
        f"Response (first 500 bytes): {resp.data[:500]!r}"
    )
