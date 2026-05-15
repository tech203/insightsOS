"""
Tests for marketing-email opt-out / unsubscribe.

Three concerns:

  1. Signed-token round-trip — make_unsubscribe_token / decode pair
     must be authenticated (tampered tokens return None) and stable
     across calls so an old email's link keeps working.

  2. Unsubscribe route — GET shows confirmation form (or "already
     unsubscribed" if applicable); POST commits the opt-out. Invalid
     tokens get the friendly error page, not 500.

  3. LTO email respects opt-out — _send_upsell_lto_email skips
     sending for opted-out users but still stamps the idempotency
     timestamp so it doesn't retry. Transactional emails (password
     reset, email verification, team invites) are NOT affected —
     opt-out only gates marketing.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from app import (
    can_send_marketing_email,
    db,
    decode_unsubscribe_token,
    make_unsubscribe_token,
)
from app import app as flask_app
from dtutils import utcnow


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


# ---------------------------------------------------------------------------
# Signed-token serializer
# ---------------------------------------------------------------------------

class TestUnsubscribeToken:

    def test_round_trip(self, make_user):
        u = make_user(plan="free", email="tok-roundtrip@x.com")
        token = make_unsubscribe_token(u.id)
        assert decode_unsubscribe_token(token) == u.id

    def test_tampered_token_returns_none(self, make_user):
        u = make_user(plan="free", email="tok-tamper@x.com")
        token = make_unsubscribe_token(u.id)
        # Flip a character — signature mismatch.
        bad = token[:-3] + ("X" if token[-3] != "X" else "Y") + token[-2:]
        assert decode_unsubscribe_token(bad) is None

    def test_malformed_token_returns_none(self):
        assert decode_unsubscribe_token("") is None
        assert decode_unsubscribe_token("not-a-token") is None
        assert decode_unsubscribe_token("a.b.c.d.e") is None

    def test_token_stable_for_same_user(self, make_user):
        """Same SECRET_KEY + same user_id should produce the same
        token, so a user's old emails keep working when they find
        them a year later. (URLSafeSerializer is deterministic; this
        test pins that contract.)"""
        u = make_user(plan="free", email="tok-stable@x.com")
        t1 = make_unsubscribe_token(u.id)
        t2 = make_unsubscribe_token(u.id)
        assert t1 == t2

    def test_different_users_get_different_tokens(self, make_user):
        u1 = make_user(plan="free", email="tok-a@x.com")
        u2 = make_user(plan="free", email="tok-b@x.com")
        assert make_unsubscribe_token(u1.id) != make_unsubscribe_token(u2.id)


# ---------------------------------------------------------------------------
# can_send_marketing_email
# ---------------------------------------------------------------------------

class TestCanSendMarketingEmail:

    def test_not_opted_out_returns_true(self, make_user):
        u = make_user(plan="free", email="ms-ok@x.com")
        assert can_send_marketing_email(u) is True

    def test_opted_out_returns_false(self, make_user):
        u = make_user(plan="free", email="ms-opt@x.com")
        u.email_marketing_opt_out_at = utcnow()
        db.session.commit()
        assert can_send_marketing_email(u) is False

    def test_missing_email_returns_false(self, make_user):
        u = make_user(plan="free", email="ms-noemail@x.com")
        u.email = ""
        db.session.commit()
        assert can_send_marketing_email(u) is False

    def test_anonymous_returns_false(self, app_ctx):
        assert can_send_marketing_email(None) is False


# ---------------------------------------------------------------------------
# /unsubscribe/<token> route
# ---------------------------------------------------------------------------

class TestUnsubscribeRoute:

    def test_get_shows_confirmation_form(self, make_user):
        u = make_user(plan="free", email="unsub-form@x.com")
        token = make_unsubscribe_token(u.id)
        c = flask_app.test_client()
        r = c.get(f"/unsubscribe/{token}")
        assert r.status_code == 200
        assert b"unsub-form@x.com" in r.data
        assert b"Yes, unsubscribe" in r.data
        # No state change on GET.
        db.session.refresh(u)
        assert u.email_marketing_opt_out_at is None

    def test_post_commits_opt_out(self, make_user):
        u = make_user(plan="free", email="unsub-post@x.com")
        token = make_unsubscribe_token(u.id)
        c = flask_app.test_client()
        r = c.post(f"/unsubscribe/{token}")
        assert r.status_code == 200
        db.session.refresh(u)
        assert u.email_marketing_opt_out_at is not None
        # Page now shows the "already unsubscribed" branch.
        assert b"You're unsubscribed" in r.data

    def test_post_is_idempotent(self, make_user):
        """Posting twice should not advance the timestamp — opt-out
        is a one-way flag."""
        u = make_user(plan="free", email="unsub-twice@x.com")
        token = make_unsubscribe_token(u.id)
        c = flask_app.test_client()
        c.post(f"/unsubscribe/{token}")
        db.session.refresh(u)
        first_stamp = u.email_marketing_opt_out_at
        c.post(f"/unsubscribe/{token}")
        db.session.refresh(u)
        assert u.email_marketing_opt_out_at == first_stamp

    def test_get_already_opted_out_shows_confirmation(self, make_user):
        u = make_user(plan="free", email="unsub-already@x.com")
        u.email_marketing_opt_out_at = utcnow() - timedelta(days=2)
        db.session.commit()
        token = make_unsubscribe_token(u.id)
        c = flask_app.test_client()
        r = c.get(f"/unsubscribe/{token}")
        assert r.status_code == 200
        assert b"You're unsubscribed" in r.data

    def test_invalid_token_renders_friendly_error(self):
        c = flask_app.test_client()
        r = c.get("/unsubscribe/totally-invalid-token")
        assert r.status_code == 400
        assert b"Unsubscribe link invalid" in r.data

    def test_unknown_user_renders_friendly_error(self, make_user):
        """Decoded token references a user that doesn't exist
        anymore (deleted account). Same friendly page as a tampered
        token."""
        u = make_user(plan="free", email="unsub-gone@x.com")
        token = make_unsubscribe_token(u.id)
        db.session.delete(u)
        db.session.commit()
        c = flask_app.test_client()
        r = c.get(f"/unsubscribe/{token}")
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# LTO email respects opt-out
# ---------------------------------------------------------------------------

class TestLTOEmailRespectsOptOut:

    def _qualified(self, make_user, *, email, opted_out=False):
        u = make_user(plan="free", email=email)
        now = utcnow()
        u.upsell_lto_status = "shown"
        u.upsell_lto_offered_at = now
        u.upsell_lto_expires_at = now + timedelta(hours=12)
        u.upsell_lto_source = "workspace_cap"
        u.upsell_prompt_count = 4
        if opted_out:
            u.email_marketing_opt_out_at = now - timedelta(days=1)
        db.session.commit()
        return u

    def test_opted_out_user_does_not_get_email(self, make_user):
        from app import _send_upsell_lto_email
        u = self._qualified(make_user, email="lto-opt@x.com", opted_out=True)
        with patch("services.email_helper.send_email") as mock_send:
            ok = _send_upsell_lto_email(u)
        assert ok is False
        mock_send.assert_not_called()

    def test_opt_out_still_stamps_idempotency_timestamp(self, make_user):
        """Critical: even though we skipped the send, set the
        timestamp so a future re-qualification doesn't retry.
        Otherwise an opted-out user could be retried forever."""
        from app import _send_upsell_lto_email
        u = self._qualified(make_user, email="lto-stamp@x.com", opted_out=True)
        with patch("services.email_helper.send_email"):
            _send_upsell_lto_email(u)
        db.session.refresh(u)
        assert u.upsell_lto_email_sent_at is not None

    def test_subscribed_user_gets_email_with_unsub_link(self, make_user):
        """Non-opted-out user gets the email, and the email carries
        the signed unsubscribe URL so they can opt out from there."""
        from app import _send_upsell_lto_email
        u = self._qualified(make_user, email="lto-unsub-link@x.com")
        captured = {}

        def _capture(*, to, subject, body_text, body_html=None, reply_to=None, list_unsubscribe_url=None):
            captured["text"] = body_text
            captured["html"] = body_html or ""
            return True

        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", side_effect=_capture):
            _send_upsell_lto_email(u)

        # Both text and HTML bodies include an unsubscribe URL.
        assert "/unsubscribe/" in captured["text"]
        assert "/unsubscribe/" in captured["html"]
        assert "Unsubscribe" in captured["html"]


# ---------------------------------------------------------------------------
# Transactional emails NOT affected by opt-out
# ---------------------------------------------------------------------------

class TestTransactionalEmailsNotAffected:
    """Defensive — the opt-out gate is intentionally only on the LTO
    email send path. Password reset / verification / invite paths
    bypass can_send_marketing_email entirely because they're service
    emails. We lock this in by verifying those code paths don't
    reference the opt-out column."""

    def test_password_reset_render_does_not_check_opt_out(self):
        """render_password_reset_email is a pure function — takes no
        user object, can't access opt-out state. Lock that in."""
        from inspect import signature
        from services.email_helper import render_password_reset_email
        params = signature(render_password_reset_email).parameters
        assert "user_name" in params and "reset_url" in params
        # No `user` parameter that could carry an opt-out flag.
        assert "user" not in params

    def test_email_verification_render_does_not_check_opt_out(self):
        from inspect import signature
        from services.email_helper import render_email_verification_email
        params = signature(render_email_verification_email).parameters
        assert "user" not in params
