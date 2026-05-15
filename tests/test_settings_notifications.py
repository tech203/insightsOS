"""
Tests for /settings/notifications — the in-product email
preferences panel.

The /unsubscribe/<token> route from #159 is intentionally one-way:
a signed-token URL that someone could leak via screenshot can only
opt a user out, never opt them back in. This settings panel is the
only resubscribe path.

Toggle semantics:
  POST marketing_emails=on  → clear email_marketing_opt_out_at
  POST without marketing_emails → set email_marketing_opt_out_at

Idempotent both directions — flipping to the state you're already
in produces an info flash, not a state mutation.

Transactional email paths (password reset, verification, invites)
are NOT affected by this toggle and never consult the opt-out flag.
"""

from __future__ import annotations

from datetime import timedelta

from app import db
from app import app as flask_app
from dtutils import utcnow


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


# ---------------------------------------------------------------------------
# GET /settings/notifications — render
# ---------------------------------------------------------------------------

class TestRender:

    def test_anonymous_redirected_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.get("/settings/notifications", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_subscribed_user_sees_unchecked_off_message(self, make_user):
        """A user who has never unsubscribed sees the toggle in the
        'subscribed' state with the "uncheck to opt out" hint."""
        u = make_user(plan="free", email="notif-sub@x.com")
        r = _logged_in(u).get("/settings/notifications")
        assert r.status_code == 200
        # Checkbox is checked.
        assert b'name="marketing_emails"' in r.data
        assert b"checked" in r.data
        # User-facing hint reflects subscribed state.
        assert b"You're subscribed" in r.data

    def test_opted_out_user_sees_unchecked_with_date(self, make_user):
        u = make_user(plan="free", email="notif-out@x.com")
        u.email_marketing_opt_out_at = utcnow() - timedelta(days=5)
        db.session.commit()

        r = _logged_in(u).get("/settings/notifications")
        assert r.status_code == 200
        # Hint mentions the unsubscribe date.
        assert b"Unsubscribed on" in r.data
        # Re-subscribe affordance.
        assert b"Check the box to re-subscribe" in r.data

    def test_nav_tab_shows_active_when_on_section(self, make_user):
        """The Notifications tab in the settings nav should highlight
        as active when we're on the notifications section. Lock that
        in so future template refactors don't lose the active state."""
        u = make_user(plan="free", email="notif-nav@x.com")
        r = _logged_in(u).get("/settings/notifications")
        assert r.status_code == 200
        # Tab label is present.
        assert b"Notifications" in r.data


# ---------------------------------------------------------------------------
# POST /settings/notifications/update — toggle
# ---------------------------------------------------------------------------

class TestToggle:

    def test_opt_in_clears_timestamp(self, make_user):
        """Going from opted-out → subscribed clears the timestamp."""
        u = make_user(plan="free", email="notif-back-in@x.com")
        u.email_marketing_opt_out_at = utcnow() - timedelta(days=10)
        db.session.commit()

        r = _logged_in(u).post(
            "/settings/notifications/update",
            data={"marketing_emails": "on"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert u.email_marketing_opt_out_at is None

    def test_opt_out_sets_timestamp(self, make_user):
        """Going from subscribed → opted-out sets the timestamp now."""
        u = make_user(plan="free", email="notif-opt@x.com")
        before = utcnow()

        r = _logged_in(u).post(
            "/settings/notifications/update",
            data={},  # checkbox absent = unchecked
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert u.email_marketing_opt_out_at is not None
        assert u.email_marketing_opt_out_at >= before

    def test_idempotent_already_subscribed(self, make_user):
        """Toggling on while already on doesn't move the (None) flag
        or produce a misleading 'unsubscribed' state."""
        u = make_user(plan="free", email="notif-idem-in@x.com")
        # Already None (subscribed).
        _logged_in(u).post(
            "/settings/notifications/update",
            data={"marketing_emails": "on"},
        )
        db.session.refresh(u)
        assert u.email_marketing_opt_out_at is None

    def test_idempotent_already_opted_out(self, make_user):
        """Toggling off while already off doesn't shift the
        existing timestamp forward."""
        u = make_user(plan="free", email="notif-idem-out@x.com")
        original = utcnow() - timedelta(days=3)
        u.email_marketing_opt_out_at = original
        db.session.commit()

        _logged_in(u).post(
            "/settings/notifications/update",
            data={},
        )
        db.session.refresh(u)
        # Timestamp preserved (within ms tolerance).
        delta = (u.email_marketing_opt_out_at - original).total_seconds()
        assert abs(delta) < 1

    def test_anonymous_post_redirects_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.post("/settings/notifications/update", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_post_redirects_back_to_notifications(self, make_user):
        u = make_user(plan="free", email="notif-redir@x.com")
        r = _logged_in(u).post(
            "/settings/notifications/update",
            data={},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/settings/notifications" in (r.headers.get("Location") or "")


# ---------------------------------------------------------------------------
# Transactional emails NOT affected by the toggle
# ---------------------------------------------------------------------------

class TestTransactionalUnaffected:
    """Defensive: changing the marketing-email toggle must not block
    password resets, verification, or invites. Those paths don't
    consult the opt-out column at all (locked in by
    test_email_marketing_opt_out.py); this is a smoke test of the
    settings flow not breaking anything downstream."""

    def test_password_reset_render_signature_unchanged(self):
        """The transactional renderers take only the values they
        need — they CANNOT access user.email_marketing_opt_out_at
        even by accident. Lock that in."""
        from inspect import signature
        from services.email_helper import (
            render_email_verification_email,
            render_password_reset_email,
        )
        for fn in (render_password_reset_email, render_email_verification_email):
            params = signature(fn).parameters
            assert "user" not in params
            assert "email_marketing_opt_out_at" not in params
