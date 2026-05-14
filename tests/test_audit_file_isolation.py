"""Audit file IDOR regression tests.

The /audit/<summary_filename> and /audit/<summary_filename>/pdf routes
read directly from disk by filename. Filenames look like:

    enfactum-com_full_20260506_145038_summary.json

The slug at the front is derived from the workspace's website domain
— a public, trivially guessable string. So @login_required alone
let any logged-in user read any other user's audit by guessing the
URL. Audit files include competitor intelligence, business profile,
content gaps, and recommended actions — exactly the strategic data
customers pay to keep private.

This suite locks the ownership check in:
- Owner can read their own audit (positive case).
- Different user gets 404, not 200 with body (the leak).
- Same for the PDF route.
- Admin can read any audit (support / debugging path).
- Team member can read team owner's audits (shared workspaces).
- Audit files without a user_id stamp are only readable by admins
  (legacy data — no way to verify ownership).
"""
import json
import os
import tempfile
import pytest
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash

import app as app_module
from app import app as flask_app, db, User, Wallet


@pytest.fixture
def isolated_outputs(tmp_path, monkeypatch):
    """Point OUTPUTS_FOLDER at a temp dir so tests don't see (or
    pollute) real audit files."""
    monkeypatch.setattr(app_module, "OUTPUTS_FOLDER", str(tmp_path))
    return tmp_path


def _plant_audit(outputs_dir, filename, *, user_id, secret_marker="ALICE-SECRET"):
    """Drop a synthetic audit JSON in the outputs dir."""
    path = os.path.join(str(outputs_dir), filename)
    with open(path, "w") as f:
        json.dump({
            "user_id": user_id,
            "website": f"https://{secret_marker.lower()}.example.com",
            "client_name": secret_marker,
            "scores": {"normalized_score": 85, "visibility_score": 17},
            "summary": {"verdict": "STRONG"},
            "saved_at": "2026-05-14T12:00:00Z",
        }, f)
    return filename


@pytest.fixture
def alice_and_bob(app_ctx, isolated_outputs):
    """Two unrelated users + Alice owns one audit file."""
    alice = User(
        email="alice@aud.test", password_hash=generate_password_hash("xx"),
        name="Alice", plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    bob = User(
        email="bob@aud.test", password_hash=generate_password_hash("xx"),
        name="Bob", plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add_all([alice, bob])
    db.session.flush()
    db.session.add(Wallet(user_id=alice.id, balance=100))
    db.session.add(Wallet(user_id=bob.id, balance=100))
    db.session.commit()

    audit_filename = _plant_audit(
        isolated_outputs,
        "alicecorp_full_20260514_120000_summary.json",
        user_id=alice.id,
    )
    return alice, bob, audit_filename


def _logged_in_client(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def test_owner_can_view_their_audit_summary(alice_and_bob):
    alice, _, fn = alice_and_bob
    resp = _logged_in_client(alice).get(f"/audit/{fn}")
    assert resp.status_code == 200
    assert b"ALICE-SECRET" in resp.data


def test_other_user_cannot_view_audit_summary(alice_and_bob):
    """The CRITICAL test — Bob requesting Alice's audit URL must 404."""
    _, bob, fn = alice_and_bob
    resp = _logged_in_client(bob).get(f"/audit/{fn}", follow_redirects=False)
    assert resp.status_code == 404, (
        f"IDOR: Bob got {resp.status_code} on Alice's audit (expected 404).\n"
        f"Body (first 400b): {resp.data[:400]!r}"
    )
    assert b"ALICE-SECRET" not in resp.data


def test_other_user_cannot_view_audit_pdf(alice_and_bob):
    _, bob, fn = alice_and_bob
    resp = _logged_in_client(bob).get(f"/audit/{fn}/pdf", follow_redirects=False)
    assert resp.status_code == 404
    assert b"ALICE-SECRET" not in resp.data


def test_unauthenticated_request_redirects_to_login(alice_and_bob):
    """Unauthenticated request shouldn't even touch the file — Flask-
    Login blocks it before our ownership check fires."""
    _, _, fn = alice_and_bob
    resp = flask_app.test_client().get(f"/audit/{fn}", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers.get("Location", "")


def test_admin_can_view_any_audit(alice_and_bob, app_ctx):
    """Admins (and dev_unlimited) bypass the ownership check by design
    — they need it for support / debugging via admin tooling."""
    _, _, fn = alice_and_bob
    admin = User(
        email="admin@aud.test", password_hash=generate_password_hash("xx"),
        name="Admin", plan="growth", role="admin",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(admin)
    db.session.flush()
    db.session.add(Wallet(user_id=admin.id, balance=100))
    db.session.commit()

    resp = _logged_in_client(admin).get(f"/audit/{fn}")
    assert resp.status_code == 200
    assert b"ALICE-SECRET" in resp.data


def test_team_member_can_view_owners_audit(alice_and_bob, app_ctx):
    """Team members share the workspace owner's audits — they're on
    the same plan and the team is the unit of billing/data."""
    alice, _, fn = alice_and_bob
    teammate = User(
        email="teammate@aud.test", password_hash=generate_password_hash("xx"),
        name="Teammate", plan="growth",
        email_verified_at=datetime.now(timezone.utc),
        team_owner_id=alice.id,
    )
    db.session.add(teammate)
    db.session.flush()
    db.session.add(Wallet(user_id=teammate.id, balance=100))
    db.session.commit()

    resp = _logged_in_client(teammate).get(f"/audit/{fn}")
    assert resp.status_code == 200, (
        f"Team member should see owner's audit; got {resp.status_code}"
    )


def test_audit_without_user_id_is_admin_only(alice_and_bob, isolated_outputs):
    """Pre-multi-tenant audits without a user_id stamp are treated as
    unowned — only admins can view them."""
    _, bob, _ = alice_and_bob
    legacy = _plant_audit(
        isolated_outputs, "legacy_audit_20250101_120000_summary.json",
        user_id=None, secret_marker="LEGACY-DATA",
    )
    resp = _logged_in_client(bob).get(f"/audit/{legacy}", follow_redirects=False)
    assert resp.status_code == 404
    assert b"LEGACY-DATA" not in resp.data
