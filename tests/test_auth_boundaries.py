"""Auth-boundary regression tests.

Verifies the four classes of auth gate the app uses are actually
enforced at the route level. None of these found bugs at the time
of writing — the value is catching the day someone removes a
@login_required or _require_admin() check by accident.

Gates exercised:
  1. @login_required          — redirect to /login on anon access
  2. _require_admin()         — 403 for non-admin authenticated users
  3. CRON_SECRET header check — 403 without the right secret
  4. Stripe-Signature header  — 400 without a valid HMAC

If any of these flips to 200 for the wrong actor, the corresponding
test fails loudly with the response body for triage.
"""
import pytest
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Wallet


@pytest.fixture
def anon_client():
    return flask_app.test_client()


@pytest.fixture
def regular_user_client(app_ctx):
    """Authenticated as a regular (non-admin) user."""
    u = User(
        email="user@auth.test", password_hash=generate_password_hash("xx"),
        name="Regular", plan="growth", role="user",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=100))
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
    return c


# ---------------------------------------------------------------------------
# 1. @login_required — JSON APIs and authenticated pages
# ---------------------------------------------------------------------------

LOGIN_REQUIRED_PATHS = [
    "/api/wallet",
    "/api/clients",
    "/api/audits",
    "/api/client/some-slug",
    "/dashboard",
    "/clients",
    "/settings",
    "/settings/credits",
    "/start-audit",
    "/content-queue",
]


@pytest.mark.parametrize("path", LOGIN_REQUIRED_PATHS)
def test_login_required_routes_redirect_anon(anon_client, path):
    """Anonymous request must 302 to /login (not 200, not 500)."""
    resp = anon_client.get(path, follow_redirects=False)
    assert resp.status_code == 302, (
        f"{path} returned {resp.status_code} for anon request "
        f"(expected 302 → /login). Body: {resp.data[:300]!r}"
    )
    assert "/login" in resp.headers.get("Location", ""), (
        f"{path} redirected to {resp.headers.get('Location')!r} — "
        f"expected /login?next={path}"
    )


# ---------------------------------------------------------------------------
# 2. _require_admin() — admin-only routes must 403 for regular users
# ---------------------------------------------------------------------------

ADMIN_ONLY_PATHS = [
    "/admin",
    "/admin/users",
    "/admin/users/1",
    "/admin/users/1/activity",
    "/admin/payments",
    "/admin/interest",
    "/admin/interest/export.csv",
    "/admin/campaigns",
    "/admin/campaigns/new",
]


@pytest.mark.parametrize("path", ADMIN_ONLY_PATHS)
def test_admin_routes_403_for_non_admin(regular_user_client, path):
    """Non-admin authenticated user must 403 (not 200, not 302 to
    a 200 page they shouldn't see)."""
    resp = regular_user_client.get(path, follow_redirects=False)
    assert resp.status_code == 403, (
        f"{path} returned {resp.status_code} for non-admin "
        f"(expected 403). Body: {resp.data[:300]!r}"
    )


def test_admin_post_route_403_for_non_admin(regular_user_client):
    """POST routes also need to gate, not just GETs — `create_campaign`
    is the canary."""
    resp = regular_user_client.post(
        "/admin/campaigns/new", data={"name": "pwn"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 3. CRON_SECRET — /cron/* needs the right header
# ---------------------------------------------------------------------------

def test_cron_endpoint_403_without_secret(anon_client):
    resp = anon_client.get("/cron/answer-monitor", follow_redirects=False)
    assert resp.status_code == 403


def test_cron_endpoint_403_with_wrong_secret(anon_client):
    resp = anon_client.get(
        "/cron/answer-monitor",
        headers={"X-Cron-Secret": "definitely-not-the-real-secret"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_cron_endpoint_403_for_logged_in_non_admin(regular_user_client):
    """Even a logged-in regular user can't hit cron — it's secret-
    based, not session-based."""
    resp = regular_user_client.get("/cron/answer-monitor", follow_redirects=False)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 4. Stripe webhook signature
# ---------------------------------------------------------------------------

def test_stripe_webhook_rejects_request_without_signature(anon_client):
    """Webhook must NOT process events without a Stripe-Signature header.
    Accepts:
      400 — signature header missing/invalid (Stripe configured)
      503 — STRIPE_SECRET_KEY not set in this env (CI fallback)
    Rejects:
      200/201 — would mean the bogus event was processed."""
    resp = anon_client.post(
        "/stripe/webhook", data='{"type":"payment_intent.succeeded"}',
        content_type="application/json", follow_redirects=False,
    )
    assert resp.status_code in (400, 503), (
        f"Webhook returned {resp.status_code} without signature — "
        f"expected 400 (sig missing) or 503 (Stripe not configured). "
        f"Body: {resp.data[:300]!r}"
    )
    # Critical: must NOT have processed the bogus event.
    assert resp.status_code not in (200, 201)


def test_stripe_webhook_rejects_request_with_bogus_signature(anon_client):
    """Webhook must reject obviously-fake signatures (HMAC mismatch)."""
    resp = anon_client.post(
        "/stripe/webhook", data='{"type":"payment_intent.succeeded"}',
        headers={"Stripe-Signature": "t=1,v1=deadbeef"},
        content_type="application/json", follow_redirects=False,
    )
    assert resp.status_code in (400, 503)
    assert resp.status_code not in (200, 201)
