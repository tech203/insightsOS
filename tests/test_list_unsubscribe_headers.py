"""
Tests for RFC 8058 one-click List-Unsubscribe headers.

Required by Gmail's bulk-sender rules (Feb 2024) and Yahoo's parallel
policy. Marketing mail without these headers is increasingly flagged
as spam regardless of content quality.

We send two headers when `list_unsubscribe_url` is provided:

    List-Unsubscribe: <https://app/unsubscribe/TOKEN>
    List-Unsubscribe-Post: List-Unsubscribe=One-Click

Both must be present — Gmail won't render its native Unsubscribe
button if either is missing. We test the wiring at two layers:

  1. Backend layer — _send_via_resend builds the right JSON payload,
     _send_via_smtp adds the right SMTP headers
  2. Public layer — send_email threads the URL through to both
     backends, and never passes it to transactional sends (defense
     against accidental cross-wiring)

Plus the integration check: _send_upsell_lto_email actually passes
its unsubscribe URL through to send_email.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

from app import db
from app import app as flask_app
from dtutils import utcnow
from services.email_helper import (
    _send_via_resend,
    _send_via_smtp,
    send_email,
)


# ---------------------------------------------------------------------------
# Resend backend
# ---------------------------------------------------------------------------

class TestResendBackend:
    """The Resend HTTP path. Verifies the right JSON keys land in
    the POST body when list_unsubscribe_url is provided."""

    def _stub_resend(self, monkeypatch, capture):
        monkeypatch.setenv("RESEND_API_KEY", "test-key")
        monkeypatch.setenv("RESEND_FROM", "test@x.com")

        class _Resp:
            status_code = 200
            text = ""

        def _post(url, **kwargs):
            capture["url"] = url
            capture["json"] = kwargs.get("json")
            return _Resp()

        fake_requests = MagicMock()
        fake_requests.post.side_effect = _post
        monkeypatch.setitem(
            __import__("sys").modules, "requests", fake_requests,
        )

    def test_unsub_url_adds_both_headers(self, monkeypatch):
        captured = {}
        self._stub_resend(monkeypatch, captured)
        ok = _send_via_resend(
            to="alex@x.com",
            subject="test",
            body_text="body",
            body_html=None,
            reply_to=None,
            list_unsubscribe_url="https://app.test/unsubscribe/abc",
        )
        assert ok is True
        hdrs = captured["json"].get("headers") or {}
        # The full <...> bracketed form is required by RFC 2369/8058.
        assert hdrs.get("List-Unsubscribe") == "<https://app.test/unsubscribe/abc>"
        # One-Click constant — Gmail requires this exact value.
        assert hdrs.get("List-Unsubscribe-Post") == "List-Unsubscribe=One-Click"

    def test_no_unsub_url_omits_headers(self, monkeypatch):
        """Transactional sends pass list_unsubscribe_url=None →
        no headers in the payload at all (not an empty dict, not
        a placeholder). Locks in the boundary."""
        captured = {}
        self._stub_resend(monkeypatch, captured)
        _send_via_resend(
            to="alex@x.com",
            subject="test",
            body_text="body",
            body_html=None,
            reply_to=None,
        )
        assert "headers" not in (captured["json"] or {})


# ---------------------------------------------------------------------------
# SMTP backend
# ---------------------------------------------------------------------------

class TestSMTPBackend:
    """The SMTP path. Verifies the EmailMessage carries the right
    headers without us touching the actual SMTP socket."""

    def _stub_smtp(self, monkeypatch, capture):
        monkeypatch.setenv("SMTP_HOST", "smtp.test")
        monkeypatch.setenv("SMTP_FROM", "noreply@test")
        monkeypatch.setenv("SMTP_TLS", "false")

        class _FakeServer:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def ehlo(self_inner):
                pass

            def starttls(self_inner, **kw):
                pass

            def login(self_inner, *a):
                pass

            def send_message(self_inner, msg):
                capture["msg"] = msg

        monkeypatch.setattr(
            "services.email_helper.smtplib.SMTP",
            lambda *a, **kw: _FakeServer(),
        )

    def test_unsub_url_adds_both_headers(self, monkeypatch):
        captured = {}
        self._stub_smtp(monkeypatch, captured)
        _send_via_smtp(
            to="alex@x.com",
            subject="test",
            body_text="body",
            body_html=None,
            reply_to=None,
            list_unsubscribe_url="https://app.test/unsubscribe/abc",
        )
        msg = captured["msg"]
        assert msg["List-Unsubscribe"] == "<https://app.test/unsubscribe/abc>"
        assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"

    def test_no_unsub_url_omits_headers(self, monkeypatch):
        captured = {}
        self._stub_smtp(monkeypatch, captured)
        _send_via_smtp(
            to="alex@x.com",
            subject="test",
            body_text="body",
            body_html=None,
            reply_to=None,
        )
        msg = captured["msg"]
        assert msg.get("List-Unsubscribe") is None
        assert msg.get("List-Unsubscribe-Post") is None


# ---------------------------------------------------------------------------
# Public send_email — threads the URL through
# ---------------------------------------------------------------------------

class TestSendEmailThreading:

    def test_url_passes_to_resend(self, monkeypatch):
        captured = {}

        def _spy(**kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr(
            "services.email_helper._resend_configured", lambda: True,
        )
        monkeypatch.setattr(
            "services.email_helper._send_via_resend", _spy,
        )
        send_email(
            to="alex@x.com", subject="s", body_text="t",
            list_unsubscribe_url="https://x.test/u/abc",
        )
        assert captured["list_unsubscribe_url"] == "https://x.test/u/abc"

    def test_url_passes_to_smtp_when_resend_not_configured(self, monkeypatch):
        captured = {}

        def _spy(**kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr(
            "services.email_helper._resend_configured", lambda: False,
        )
        monkeypatch.setattr(
            "services.email_helper._smtp_configured", lambda: True,
        )
        monkeypatch.setattr(
            "services.email_helper._send_via_smtp", _spy,
        )
        send_email(
            to="alex@x.com", subject="s", body_text="t",
            list_unsubscribe_url="https://x.test/u/abc",
        )
        assert captured["list_unsubscribe_url"] == "https://x.test/u/abc"

    def test_omitted_url_passes_none_through(self, monkeypatch):
        """Backwards-compatible — existing callers that don't pass
        the new kwarg get None, which the backends interpret as
        'no headers'."""
        captured = {}

        def _spy(**kwargs):
            captured.update(kwargs)
            return True

        monkeypatch.setattr(
            "services.email_helper._resend_configured", lambda: True,
        )
        monkeypatch.setattr(
            "services.email_helper._send_via_resend", _spy,
        )
        send_email(to="alex@x.com", subject="s", body_text="t")
        assert captured["list_unsubscribe_url"] is None


# ---------------------------------------------------------------------------
# LTO email integration
# ---------------------------------------------------------------------------

class TestLTOEmailWiresUnsubHeader:

    def _qualified(self, make_user):
        u = make_user(plan="free", email="hdr-lto@x.com")
        now = utcnow()
        u.upsell_lto_status = "shown"
        u.upsell_lto_offered_at = now
        u.upsell_lto_expires_at = now + timedelta(hours=12)
        u.upsell_lto_source = "workspace_cap"
        u.upsell_prompt_count = 4
        db.session.commit()
        return u

    def test_lto_send_carries_list_unsubscribe_url(self, make_user):
        """The end-to-end check: when the LTO email fires for a
        qualified user, the underlying send_email call gets the
        list_unsubscribe_url kwarg with the same signed token URL
        that lives in the email body."""
        from app import _send_upsell_lto_email
        u = self._qualified(make_user)
        captured = {}

        def _spy(*, to, subject, body_text, body_html=None,
                 reply_to=None, list_unsubscribe_url=None):
            captured["url"] = list_unsubscribe_url
            captured["body_text"] = body_text
            return True

        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", side_effect=_spy):
            _send_upsell_lto_email(u)

        # URL is present and points at /unsubscribe/<token>.
        assert captured["url"] is not None
        assert "/unsubscribe/" in captured["url"]
        # Same URL appears in the body — both surfaces agree.
        assert captured["url"] in captured["body_text"]
