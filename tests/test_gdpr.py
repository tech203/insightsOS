"""GDPR Article 17 (erasure) + Article 20 (portability) tests.

Two new endpoints:
  GET  /settings/account/export-data — JSON bundle of user's data
  POST /settings/account/delete       — cascade-delete user + owned data

These tests exercise:
  - Export returns a JSON file with the user's profile, wallet, and
    workspaces (and never includes auth tokens / hashes — those are
    server state, not user data).
  - Delete requires the current password (anti-stranger-with-cookie).
  - Delete requires the literal phrase "delete my account" typed
    verbatim (anti-misclick).
  - Delete cascades — wallet, workspaces, transactions all gone.
  - Delete logs the user out and redirects.
  - Wrong password / wrong phrase → no-op + flash, user still exists.
  - Other users' data is untouched (multi-tenant isolation).
"""
import json
from datetime import datetime, timezone

import pytest
from werkzeug.security import generate_password_hash

from app import (
    app as flask_app, db,
    User, Wallet, Client, CreditTransaction,
)


@pytest.fixture
def user(app_ctx):
    u = User(
        email="gdpr@test.com",
        password_hash=generate_password_hash("knownpw"),
        name="GDPR Test",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=42))
    db.session.add(CreditTransaction(
        user_id=u.id, type="signup_bonus", amount=42, balance_after=42,
        notes="signup",
    ))
    # Stamp this 28-day window as already-granted so the
    # before_request monthly-allowance hook doesn't silently add 175
    # credits mid-test (Growth plan default).
    db.session.add(CreditTransaction(
        user_id=u.id, type="monthly_allowance", amount=0,
        balance_after=42, notes="suppress monthly grant in test",
    ))
    db.session.add(Client(
        slug="gdprco", user_id=u.id, name="GDPR Co",
        website="https://gdpr.example.com",
        website_normalized="gdpr.example.com",
        industry="SaaS", brand_audience="EU SMBs",
    ))
    db.session.commit()
    return u


@pytest.fixture
def client(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = user.get_id()
        s["_fresh"] = True
    return c


# ---------------------------------------------------------------------------
# Export (Article 20)
# ---------------------------------------------------------------------------

def test_export_returns_attachment_json(client):
    resp = client.get("/settings/account/export-data")
    assert resp.status_code == 200
    assert resp.headers.get("Content-Type") == "application/json"
    cd = resp.headers.get("Content-Disposition", "")
    assert cd.startswith('attachment; filename="')
    assert ".json" in cd


def test_export_includes_profile_wallet_workspaces(client, user):
    resp = client.get("/settings/account/export-data")
    payload = json.loads(resp.data)

    assert payload["profile"]["email"] == "gdpr@test.com"
    assert payload["profile"]["name"] == "GDPR Test"
    assert payload["profile"]["plan"] == "growth"

    assert payload["wallet"]["balance"] == 42
    assert len(payload["wallet"]["transactions"]) >= 1
    assert payload["wallet"]["transactions"][0]["type"] == "signup_bonus"

    assert len(payload["workspaces"]) == 1
    ws = payload["workspaces"][0]
    assert ws["slug"] == "gdprco"
    assert ws["name"] == "GDPR Co"
    assert ws["brand_audience"] == "EU SMBs"


def test_export_excludes_auth_secrets(client):
    """Password hash / Stripe customer ID / OAuth tokens must NOT be
    in the export — they're server state, not user data, and would
    let an attacker impersonate the user if the file leaks."""
    resp = client.get("/settings/account/export-data")
    body = resp.data.decode()
    payload = json.loads(body)

    # Spot-check the structured payload doesn't include these keys.
    assert "password_hash" not in body
    assert "stripe_customer_id" not in payload["profile"]


def test_export_requires_login(app_ctx):
    """Anonymous requests must redirect to /login."""
    anon = flask_app.test_client()
    resp = anon.get("/settings/account/export-data", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


# ---------------------------------------------------------------------------
# Delete (Article 17)
# ---------------------------------------------------------------------------

def test_delete_requires_correct_password(client, user):
    """Wrong current password → flash, no deletion."""
    resp = client.post("/settings/account/delete", data={
        "current_password": "WRONG",
        "confirm_phrase": "delete my account",
    }, follow_redirects=False)
    assert resp.status_code == 302
    # User row still exists.
    assert db.session.get(User,user.id) is not None


def test_delete_requires_exact_confirm_phrase(client, user):
    """Wrong / missing confirm phrase → no deletion (anti-misclick)."""
    for phrase in ["", "yes", "delete account", "delete my acc"]:
        resp = client.post("/settings/account/delete", data={
            "current_password": "knownpw",
            "confirm_phrase": phrase,
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert db.session.get(User,user.id) is not None, (
            f"Phrase {phrase!r} unexpectedly deleted the user"
        )


def test_delete_succeeds_with_password_and_phrase(client, user):
    """Happy path: correct password + exact phrase → user gone."""
    user_id = user.id
    resp = client.post("/settings/account/delete", data={
        "current_password": "knownpw",
        "confirm_phrase": "delete my account",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert db.session.get(User,user_id) is None
    # Cascade — workspaces, wallet, transactions all gone.
    assert Client.query.filter_by(user_id=user_id).count() == 0
    assert Wallet.query.filter_by(user_id=user_id).count() == 0
    assert CreditTransaction.query.filter_by(user_id=user_id).count() == 0


def test_delete_logs_user_out(client, user):
    """After deletion the session must no longer be authenticated."""
    client.post("/settings/account/delete", data={
        "current_password": "knownpw",
        "confirm_phrase": "delete my account",
    })
    # Next authenticated route must redirect to login.
    resp = client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_delete_requires_login(app_ctx):
    """Anonymous POST must redirect to login (no destruction)."""
    anon = flask_app.test_client()
    resp = anon.post("/settings/account/delete", data={
        "current_password": "anything",
        "confirm_phrase": "delete my account",
    }, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_delete_does_not_touch_other_users(app_ctx, user):
    """Multi-tenant isolation — Bob deleting his account must not
    touch Alice's data."""
    alice = User(
        email="alice@gdpr.test", password_hash=generate_password_hash("xx"),
        name="Alice", plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(alice)
    db.session.flush()
    db.session.add(Wallet(user_id=alice.id, balance=99))
    db.session.add(Client(
        slug="aliceco", user_id=alice.id, name="Alice Co",
        website="https://alice.example.com",
        website_normalized="alice.example.com",
    ))
    db.session.commit()

    # User (gdpr@test.com) deletes their account.
    bob_client = flask_app.test_client()
    with bob_client.session_transaction() as s:
        s["_user_id"] = user.get_id()
        s["_fresh"] = True
    bob_client.post("/settings/account/delete", data={
        "current_password": "knownpw",
        "confirm_phrase": "delete my account",
    })

    # Alice must be untouched.
    assert db.session.get(User,alice.id) is not None
    assert Client.query.filter_by(user_id=alice.id).count() == 1
    assert Wallet.query.filter_by(user_id=alice.id).first().balance == 99
