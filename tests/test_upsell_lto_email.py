"""
Tests for the one-shot LTO email fired when a Free user crosses the
prompt threshold.

The email is a partner to the in-app modal — same offer, same TTL,
opportunistically delivered to the user's inbox so they don't lose
the window if they close the tab. Two correctness concerns:

  1. **Idempotency** — exactly one email per qualification. Setting
     `upsell_lto_email_sent_at` is the guard.
  2. **Never break the request** — if Resend / SMTP is down, the
     user's request must still succeed and the in-app modal still
     trigger. The email is "best-effort," strictly additive.

We also lock in the source-aware subject + headline so a A/B copy
change can't accidentally regress the existing wiring.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from app import (
    UPSELL_PROMPT_THRESHOLD,
    _send_upsell_lto_email,
    db,
    record_upsell_prompt,
)
from app import app as flask_app
from dtutils import utcnow
from services.email_helper import render_upsell_lto_email


# ---------------------------------------------------------------------------
# render_upsell_lto_email — pure render helper
# ---------------------------------------------------------------------------

class TestRenderHelper:

    def test_subject_includes_headline(self):
        subject, _, _ = render_upsell_lto_email(
            user_name="Alex",
            headline="Need more workspaces?",
            upgrade_url="https://app.test/pricing",
            hours_left=12,
        )
        assert "Need more workspaces?" in subject
        assert "limited-time" in subject.lower()

    def test_text_body_uses_headline_and_url(self):
        _, text, _ = render_upsell_lto_email(
            user_name="Alex",
            headline="Ready to white-label your reports?",
            upgrade_url="https://app.test/pricing?source=upsell_lto",
            hours_left=24,
        )
        assert "Alex" in text
        assert "Ready to white-label your reports?" in text
        assert "https://app.test/pricing?source=upsell_lto" in text
        assert "24 hour" in text or "24 hours" in text

    def test_html_escapes_user_name(self):
        """If a Free user happens to have an HTML-ish display name
        the email must escape it — same pattern as the password
        reset email."""
        _, _, html = render_upsell_lto_email(
            user_name="<script>alert(1)</script>",
            headline="Need more workspaces?",
            upgrade_url="https://app.test/pricing",
            hours_left=12,
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_empty_headline_falls_back_to_default(self):
        subject, text, html = render_upsell_lto_email(
            user_name="Alex",
            headline="",
            upgrade_url="https://app.test/pricing",
            hours_left=12,
        )
        # All three surfaces should pick up the fallback.
        assert "Ready to unlock everything" in subject
        assert "Ready to unlock everything" in text
        assert "Ready to unlock everything" in html

    def test_hour_count_is_pluralized_correctly(self):
        _, text_one, _ = render_upsell_lto_email(
            user_name="A", headline="X",
            upgrade_url="https://x.test", hours_left=1,
        )
        _, text_many, _ = render_upsell_lto_email(
            user_name="A", headline="X",
            upgrade_url="https://x.test", hours_left=5,
        )
        assert "1 hour" in text_one and "1 hours" not in text_one
        assert "5 hours" in text_many

    def test_subject_strips_newlines_from_headline(self):
        """Defensive: if headline somehow contained CRLF, the subject
        must not become a multi-header injection vector."""
        subject, _, _ = render_upsell_lto_email(
            user_name="A",
            headline="legit\r\nBcc: attacker@evil.com",
            upgrade_url="https://x.test",
            hours_left=12,
        )
        assert "\n" not in subject
        assert "\r" not in subject


# ---------------------------------------------------------------------------
# _send_upsell_lto_email — idempotency + safe-failure
# ---------------------------------------------------------------------------

class TestSendHelper:

    def _qualified_user(self, make_user, email="lto-email@x.com"):
        u = make_user(plan="free", email=email)
        now = utcnow()
        u.upsell_lto_status = "shown"
        u.upsell_lto_offered_at = now
        u.upsell_lto_expires_at = now + timedelta(hours=12)
        u.upsell_lto_source = "workspace_cap"
        u.upsell_prompt_count = 4
        db.session.commit()
        return u

    def test_send_success_stamps_timestamp(self, make_user):
        u = self._qualified_user(make_user)
        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", return_value=True) as mock_send:
            ok = _send_upsell_lto_email(u)
        assert ok is True
        mock_send.assert_called_once()
        db.session.refresh(u)
        assert u.upsell_lto_email_sent_at is not None

    def test_send_failure_returns_false_no_timestamp(self, make_user):
        """Resend down / SMTP not configured — send_email returns False
        and we leave the timestamp NULL so a re-trigger (e.g. retry
        sweep) could still attempt."""
        u = self._qualified_user(make_user, email="lto-fail@x.com")
        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", return_value=False):
            ok = _send_upsell_lto_email(u)
        assert ok is False
        db.session.refresh(u)
        assert u.upsell_lto_email_sent_at is None

    def test_idempotent_when_already_sent(self, make_user):
        """If the timestamp is already set, the helper short-circuits
        and does NOT re-send. Critical: prevents a re-entered code
        path from spamming the user."""
        u = self._qualified_user(make_user, email="lto-idem@x.com")
        u.upsell_lto_email_sent_at = utcnow() - timedelta(hours=1)
        db.session.commit()

        with patch("services.email_helper.send_email") as mock_send:
            ok = _send_upsell_lto_email(u)
        assert ok is False
        mock_send.assert_not_called()

    def test_missing_email_no_op(self, make_user):
        u = self._qualified_user(make_user, email="lto-noemail@x.com")
        u.email = ""  # blanked
        db.session.commit()
        with patch("services.email_helper.send_email") as mock_send:
            ok = _send_upsell_lto_email(u)
        assert ok is False
        mock_send.assert_not_called()

    def test_uses_source_aware_headline(self, make_user):
        """The headline picked from the user's stored source should
        flow through to the rendered subject."""
        u = self._qualified_user(make_user, email="lto-source@x.com")
        u.upsell_lto_source = "gsc_dashboard_gate"
        db.session.commit()

        captured = {}

        def _capture(*, to, subject, body_text, body_html=None, reply_to=None):
            captured["subject"] = subject
            captured["text"] = body_text
            return True

        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", side_effect=_capture):
            _send_upsell_lto_email(u)

        assert "Want Search Console data?" in captured["subject"]
        assert "Want Search Console data?" in captured["text"]

    def test_uses_pricing_url_with_source_tag(self, make_user):
        """The CTA URL must carry source=upsell_lto so the webhook
        attribution (#142) credits the conversion correctly."""
        u = self._qualified_user(make_user, email="lto-url@x.com")
        captured = {}

        def _capture(*, to, subject, body_text, body_html=None, reply_to=None):
            captured["text"] = body_text
            return True

        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", side_effect=_capture):
            _send_upsell_lto_email(u)

        assert "source=upsell_lto" in captured["text"]


# ---------------------------------------------------------------------------
# record_upsell_prompt qualifying call fires the email
# ---------------------------------------------------------------------------

class TestQualificationHookFiresEmail:
    """Integration: when a Free user actually qualifies via
    record_upsell_prompt, the email pipeline runs. We patch send_email
    so the test doesn't hit Resend."""

    def test_qualifying_call_triggers_email(self, make_user):
        u = make_user(plan="free", email="hook-fire@x.com")
        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", return_value=True) as mock_send:
            for _ in range(UPSELL_PROMPT_THRESHOLD):
                record_upsell_prompt(u, source="workspace_cap")

        db.session.refresh(u)
        assert u.upsell_lto_status == "shown"
        assert u.upsell_lto_email_sent_at is not None
        mock_send.assert_called_once()
        # Verify the email went to the right address.
        kwargs = mock_send.call_args.kwargs
        assert kwargs["to"] == "hook-fire@x.com"

    def test_sub_threshold_calls_dont_email(self, make_user):
        u = make_user(plan="free", email="hook-sub@x.com")
        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", return_value=True) as mock_send:
            # One below threshold — qualification doesn't trigger.
            for _ in range(UPSELL_PROMPT_THRESHOLD - 1):
                record_upsell_prompt(u, source="workspace_cap")
        db.session.refresh(u)
        assert u.upsell_lto_status == "none"
        mock_send.assert_not_called()

    def test_email_failure_does_not_break_qualification(self, make_user):
        """The critical contract: if Resend is down, the modal still
        triggers. send_email returning False mustn't prevent the
        upsell_lto_status flip."""
        u = make_user(plan="free", email="hook-resend-down@x.com")
        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", return_value=False):
            for _ in range(UPSELL_PROMPT_THRESHOLD):
                record_upsell_prompt(u, source="workspace_cap")

        db.session.refresh(u)
        # In-app modal state intact.
        assert u.upsell_lto_status == "shown"
        # No timestamp because the send failed.
        assert u.upsell_lto_email_sent_at is None

    def test_email_exception_does_not_break_qualification(self, make_user):
        """Even rougher contract: if the email pipeline raises
        (network outage, missing env, surprise import error), the
        qualification still lands."""
        u = make_user(plan="free", email="hook-explode@x.com")
        with flask_app.test_request_context("/"), \
             patch(
                 "app._send_upsell_lto_email",
                 side_effect=RuntimeError("boom"),
             ):
            for _ in range(UPSELL_PROMPT_THRESHOLD):
                record_upsell_prompt(u, source="workspace_cap")

        db.session.refresh(u)
        assert u.upsell_lto_status == "shown"
        assert u.upsell_lto_email_sent_at is None

    def test_paid_user_qualification_path_never_emails(self, make_user):
        """Belt-and-braces: paid users are already excluded earlier
        in record_upsell_prompt, but a future bug could regress that.
        The email helper itself shouldn't fire for them."""
        u = make_user(plan="pro", email="hook-pro@x.com")
        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email") as mock_send:
            for _ in range(UPSELL_PROMPT_THRESHOLD + 2):
                record_upsell_prompt(u, source="workspace_cap")
        db.session.refresh(u)
        # Paid → no counter, no email.
        assert u.upsell_prompt_count == 0
        mock_send.assert_not_called()
