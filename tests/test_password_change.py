"""
Tests for POST /settings/account/change-password.

Security-sensitive: requires the current password (defends against
drive-by changes on a hijacked session), enforces an 8-char
minimum, and re-logins after the change so the legitimate user
isn't kicked out by the password-hash-in-session-id defence.

test_session_invalidation.py already covers the "other sessions
are invalidated" side. This file covers the route's own
validation + the same-session continuity guarantee.

Fixture note: conftest's make_user sets the password to the
8-char string "xxxxxxxx" (generate_password_hash). Tests use that
as the known current password.
"""

from __future__ import annotations

from werkzeug.security import check_password_hash

from app import User, db
from app import app as flask_app

CURRENT_PW = "xxxxxxxx"  # conftest make_user default


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _post(client, **form):
    return client.post(
        "/settings/account/change-password",
        data=form,
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:

    def test_wrong_current_password_rejected(self, make_user):
        u = make_user(plan="pro", email="pw-wrongcur@x.com")
        old_hash = u.password_hash
        r = _post(
            _logged_in(u),
            current_password="not-the-password",
            new_password="brandnewpw123",
            confirm_password="brandnewpw123",
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert u.password_hash == old_hash  # unchanged

    def test_short_new_password_rejected(self, make_user):
        u = make_user(plan="pro", email="pw-short@x.com")
        old_hash = u.password_hash
        r = _post(
            _logged_in(u),
            current_password=CURRENT_PW,
            new_password="short",       # < 8
            confirm_password="short",
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert u.password_hash == old_hash

    def test_confirmation_mismatch_rejected(self, make_user):
        u = make_user(plan="pro", email="pw-mismatch@x.com")
        old_hash = u.password_hash
        r = _post(
            _logged_in(u),
            current_password=CURRENT_PW,
            new_password="brandnewpw123",
            confirm_password="different456",
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert u.password_hash == old_hash

    def test_exactly_8_chars_accepted(self, make_user):
        """Boundary: 8 chars is the minimum (>= 8, not > 8)."""
        u = make_user(plan="pro", email="pw-boundary@x.com")
        r = _post(
            _logged_in(u),
            current_password=CURRENT_PW,
            new_password="12345678",     # exactly 8
            confirm_password="12345678",
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert check_password_hash(u.password_hash, "12345678")


# ---------------------------------------------------------------------------
# Successful change
# ---------------------------------------------------------------------------

class TestSuccessfulChange:

    def test_password_hash_updated(self, make_user):
        u = make_user(plan="pro", email="pw-ok@x.com")
        old_hash = u.password_hash
        _post(
            _logged_in(u),
            current_password=CURRENT_PW,
            new_password="a-much-better-password",
            confirm_password="a-much-better-password",
        )
        db.session.refresh(u)
        assert u.password_hash != old_hash
        assert check_password_hash(u.password_hash, "a-much-better-password")
        # Old password no longer works.
        assert not check_password_hash(u.password_hash, CURRENT_PW)

    def test_can_login_with_new_password_after_change(self, make_user):
        """End-to-end: change the password, then a fresh login with
        the NEW credentials succeeds and the OLD ones fail."""
        u = make_user(plan="pro", email="pw-relogin@x.com")
        _post(
            _logged_in(u),
            current_password=CURRENT_PW,
            new_password="freshpassword99",
            confirm_password="freshpassword99",
        )

        anon = flask_app.test_client()
        bad = anon.post(
            "/login",
            data={"email": "pw-relogin@x.com", "password": CURRENT_PW},
            follow_redirects=False,
        )
        # Old password rejected — stays on /login (200) or re-renders,
        # never a 302 into the app.
        assert "/dashboard" not in (bad.headers.get("Location") or "")

        good = anon.post(
            "/login",
            data={"email": "pw-relogin@x.com", "password": "freshpassword99"},
            follow_redirects=False,
        )
        assert good.status_code == 302
        assert "/login" not in (good.headers.get("Location") or "")

    def test_same_session_survives_change(self, make_user):
        """The re-login inside the route keeps the acting session
        valid. Without it, the password-hash-in-session-id defence
        would log the legitimate user out on their very next
        request. Verify a follow-up authed request still works."""
        u = make_user(plan="pro", email="pw-continuity@x.com")
        c = _logged_in(u)
        c.post(
            "/settings/account/change-password",
            data={
                "current_password": CURRENT_PW,
                "new_password": "continuitypw1",
                "confirm_password": "continuitypw1",
            },
            follow_redirects=False,
        )
        # Same client, next request — must still be authenticated
        # (not bounced to /login).
        r = c.get("/settings/account", follow_redirects=False)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestAccessControl:

    def test_anonymous_redirected_to_login(self, app_ctx, make_user):
        u = make_user(plan="pro", email="pw-anon@x.com")
        old_hash = u.password_hash
        r = flask_app.test_client().post(
            "/settings/account/change-password",
            data={
                "current_password": CURRENT_PW,
                "new_password": "whatever123",
                "confirm_password": "whatever123",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")
        # Never reached the handler — hash untouched.
        fresh = db.session.get(User, u.id)
        assert fresh.password_hash == old_hash
