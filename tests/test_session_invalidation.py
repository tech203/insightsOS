"""Session invalidation on password change — regression tests.

Closes the "stolen cookie outlives password reset" gap. The classic
attack:
  1. Attacker steals user's session cookie (XSS, malware, log leak).
  2. User notices something fishy and resets their password.
  3. With the default Flask-Login behaviour, the attacker's cookie
     stays valid until it expires (Flask-Login default: 31 days).

Fix (User.get_id + load_user, both in app.py): the session id is now
the user.id PLUS a slice of the current password hash. When the
password changes, the slice changes, and any session that doesn't
match is treated as anonymous on its next request — attacker logged
out immediately.

Tests:
  - get_id() returns "<id>|<hash[:32]>" not just "<id>"
  - load_user() returns the user when the slice matches
  - load_user() returns None when the slice doesn't match (stale
    cookie from before a password change)
  - load_user() accepts the legacy bare-int format (back-compat for
    cookies issued before this code shipped)
  - load_user() returns None for malformed input (defensive)
  - End-to-end: change password while authenticated → the session
    cookie is reissued so the legitimate user stays logged in
  - End-to-end: an OLD session cookie (captured before password
    change) is rejected on the next request — the simulated stolen
    cookie scenario
"""
import pytest
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Wallet, load_user


@pytest.fixture
def user(app_ctx):
    u = User(
        email="sess@test.com",
        password_hash=generate_password_hash("originalpw"),
        name="Sess",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=100))
    db.session.commit()
    return u


# ---------------------------------------------------------------------------
# get_id() composite format
# ---------------------------------------------------------------------------

def test_get_id_includes_password_hash_slice(user):
    sid = user.get_id()
    assert "|" in sid, f"get_id() must be composite, got {sid!r}"
    raw_id, _, hash_slice = sid.partition("|")
    assert raw_id == str(user.id)
    assert hash_slice and hash_slice == user.password_hash[:32]


def test_get_id_changes_when_password_hash_changes(user):
    """The whole point: password change → different session id →
    old sessions invalid on next request."""
    before = user.get_id()
    user.password_hash = generate_password_hash("differentpw")
    db.session.commit()
    after = user.get_id()
    assert before != after, (
        "get_id() didn't change on password reset — old sessions would "
        "stay valid. This is the defence that's supposed to log out "
        "hijacked sessions."
    )


# ---------------------------------------------------------------------------
# load_user() behaviour
# ---------------------------------------------------------------------------

def test_load_user_with_current_session_id_returns_user(user):
    sid = user.get_id()
    loaded = load_user(sid)
    assert loaded is not None
    assert loaded.id == user.id


def test_load_user_with_stale_session_id_returns_none(user):
    """The CRITICAL case: an attacker has a session cookie issued
    BEFORE the password reset. After the reset, that cookie's hash
    slice no longer matches the live user's password hash.
    load_user must return None — treating the cookie as anonymous."""
    stale_sid = user.get_id()  # captured BEFORE the password change

    # Simulate password reset
    user.password_hash = generate_password_hash("brandnewpw")
    db.session.commit()

    loaded = load_user(stale_sid)
    assert loaded is None, (
        "Stale session cookie still loads the user — hijacked sessions "
        "outlive password reset. This is the bug we're trying to fix."
    )


def test_load_user_accepts_legacy_bare_int_format(user):
    """Back-compat: cookies issued before this change have just the
    user.id with no '|<hash>' suffix. Accept them once — the next
    session.save() rewrites them with the composite format."""
    legacy_sid = str(user.id)
    loaded = load_user(legacy_sid)
    assert loaded is not None
    assert loaded.id == user.id


@pytest.mark.parametrize("bad_input", [
    None,
    "",
    "not-a-number",
    "abc|def",
    "999999999|whatever",  # nonexistent user
])
def test_load_user_rejects_malformed_or_missing(app_ctx, bad_input):
    assert load_user(bad_input) is None


# ---------------------------------------------------------------------------
# End-to-end: legitimate user changing password stays logged in
# ---------------------------------------------------------------------------

def test_change_password_keeps_legitimate_user_logged_in(user):
    """The user changing their OWN password shouldn't get logged out
    on the next request — the change-password handler reissues the
    session with the new composite id."""
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = user.get_id()
        s["_fresh"] = True

    resp = c.post("/settings/account/change-password", data={
        "current_password": "originalpw",
        "new_password": "newpassword123",
        "confirm_password": "newpassword123",
    }, follow_redirects=False)
    assert resp.status_code in (200, 302)

    # Next request should still be authenticated — the handler
    # called login_user() to reissue the cookie with the new id.
    follow = c.get("/dashboard", follow_redirects=False)
    assert follow.status_code != 302 or "/login" not in follow.headers.get("Location", ""), (
        "User got logged out after changing their own password. "
        "change-password handler is missing login_user(current_user) "
        "after the commit."
    )


# ---------------------------------------------------------------------------
# End-to-end: attacker's stolen cookie is invalidated by password reset
# ---------------------------------------------------------------------------

def test_stale_cookie_loses_access_after_password_reset(user):
    """Simulates: attacker hijacked Bob's session cookie. Bob notices,
    resets password. Attacker's cookie should stop working on next
    request.

    Note: in pytest the `app_ctx` fixture wraps the whole test in one
    flask_app.app_context(), and Flask binds `g` to the app context
    (not the request context). That means two test-client requests in
    the same test SHARE `g` — including Flask-Login's `g._login_user`
    cache from the first authenticated request. In production each
    HTTP request has its own context and this isn't a problem; in the
    test we have to clear the cache manually to simulate a fresh
    request."""
    from flask import g

    # Pre-reset: cookie is valid; attacker can access authenticated routes.
    # Plant the OLD composite id, capture password change, verify the
    # NEXT request is rejected. We skip the "before" request entirely
    # since the load_user() unit tests above already prove the
    # validity-check behaviour.
    stale_sid = user.get_id()  # captured pre-reset

    # Bob resets their password.
    user.password_hash = generate_password_hash("bobsnewpassword")
    db.session.commit()

    # Clear any cached user from prior fixture/test setup so this
    # request actually exercises load_user() with the stale sid.
    if hasattr(g, "_login_user"):
        g.pop("_login_user", None)

    attacker = flask_app.test_client()
    with attacker.session_transaction() as s:
        s["_user_id"] = stale_sid
        s["_fresh"] = True

    # Attacker's request must be redirected to login.
    post = attacker.get("/dashboard", follow_redirects=False)
    assert post.status_code == 302
    assert "/login" in post.headers.get("Location", ""), (
        f"Attacker still has access after password reset. "
        f"Got: {post.status_code} → {post.headers.get('Location')!r}"
    )
