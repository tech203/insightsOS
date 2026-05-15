"""
Tests for the LTO email retry sweeper.

Closes the email-pipeline loop: when `_send_upsell_lto_email` fires
inline at qualification but Resend / SMTP is temporarily down, the
email is lost. A scheduled cron tick (`/cron/upsell-lto-email-retries`)
picks up the missing sends and retries — but only within a bounded
window so we don't email people on stale offers.

Candidate filter:
    upsell_lto_status = "shown"
    AND upsell_lto_email_sent_at IS NULL
    AND upsell_lto_offered_at > now - UPSELL_LTO_EMAIL_RETRY_WINDOW_HOURS
    AND upsell_lto_expires_at > now
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from app import (
    UPSELL_LTO_EMAIL_RETRY_BATCH,
    UPSELL_LTO_EMAIL_RETRY_WINDOW_HOURS,
    db,
    sweep_upsell_lto_email_retries,
)
from app import app as flask_app
from dtutils import utcnow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed(make_user, *, email, status="shown", offered_hours_ago=0.5,
          ttl_hours=12, email_sent=False, opted_out=False):
    """Create a user in a configurable qualification state."""
    u = make_user(plan="free", email=email)
    now = utcnow()
    u.upsell_lto_status = status
    u.upsell_lto_offered_at = now - timedelta(hours=offered_hours_ago)
    u.upsell_lto_expires_at = now + timedelta(hours=ttl_hours)
    u.upsell_lto_source = "workspace_cap"
    u.upsell_prompt_count = 4
    if email_sent:
        u.upsell_lto_email_sent_at = now - timedelta(hours=offered_hours_ago)
    if opted_out:
        u.email_marketing_opt_out_at = now - timedelta(days=1)
    db.session.commit()
    return u


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

class TestCandidateSelection:
    """The sweep's WHERE clause — verify each filter individually."""

    def test_picks_up_failed_inline_send(self, make_user):
        """The happy path: user qualified, inline send failed,
        email_sent_at is NULL, qualification is recent. Should
        retry."""
        u = _seed(make_user, email="retry-happy@x.com",
                  offered_hours_ago=0.25)  # 15 min ago
        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", return_value=True):
            out = sweep_upsell_lto_email_retries()
        assert out["attempted"] == 1
        assert out["succeeded"] == 1
        db.session.refresh(u)
        assert u.upsell_lto_email_sent_at is not None

    def test_skips_users_already_emailed(self, make_user):
        """email_sent_at NOT NULL → not a candidate. The inline send
        worked the first time; don't double-send."""
        _seed(make_user, email="retry-done@x.com",
              offered_hours_ago=0.5, email_sent=True)
        with patch("services.email_helper.send_email") as mock_send:
            out = sweep_upsell_lto_email_retries()
        assert out["attempted"] == 0
        mock_send.assert_not_called()

    def test_skips_users_past_retry_window(self, make_user):
        """Qualification > WINDOW hours ago → out of the candidate
        set. We don't keep retrying forever; user's modal still
        works in-app."""
        u = _seed(
            make_user, email="retry-stale@x.com",
            offered_hours_ago=UPSELL_LTO_EMAIL_RETRY_WINDOW_HOURS + 0.5,
            ttl_hours=20,  # offer still has time left
        )
        with patch("services.email_helper.send_email") as mock_send:
            out = sweep_upsell_lto_email_retries()
        assert out["attempted"] == 0
        mock_send.assert_not_called()
        db.session.refresh(u)
        assert u.upsell_lto_email_sent_at is None  # stayed null

    def test_skips_expired_offers(self, make_user):
        """expires_at < now → don't retry. Offer's gone; emailing
        about it now would be misleading."""
        _seed(make_user, email="retry-expired@x.com",
              offered_hours_ago=1, ttl_hours=-1)
        with patch("services.email_helper.send_email") as mock_send:
            out = sweep_upsell_lto_email_retries()
        assert out["attempted"] == 0
        mock_send.assert_not_called()

    def test_skips_dismissed_users(self, make_user):
        """status=dismissed → user actively closed the modal. Don't
        email them about an offer they explicitly waved off."""
        _seed(make_user, email="retry-dismissed@x.com",
              status="dismissed", offered_hours_ago=0.5)
        with patch("services.email_helper.send_email") as mock_send:
            out = sweep_upsell_lto_email_retries()
        assert out["attempted"] == 0
        mock_send.assert_not_called()

    def test_skips_accepted_users(self, make_user):
        """status=accepted → user already converted. Definitely don't
        send them the offer email after the fact."""
        _seed(make_user, email="retry-accepted@x.com",
              status="accepted", offered_hours_ago=0.5)
        with patch("services.email_helper.send_email") as mock_send:
            out = sweep_upsell_lto_email_retries()
        assert out["attempted"] == 0
        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# Batch + failure handling
# ---------------------------------------------------------------------------

class TestBatchHandling:

    def test_capped_at_batch_size(self, make_user):
        """A spike of qualifications during a Resend outage shouldn't
        blast every retry at once when Resend recovers. The sweep
        caps each tick at UPSELL_LTO_EMAIL_RETRY_BATCH."""
        # Seed batch+5 candidates.
        for i in range(UPSELL_LTO_EMAIL_RETRY_BATCH + 5):
            _seed(make_user, email=f"batch-{i}@x.com",
                  offered_hours_ago=0.5)

        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", return_value=True):
            out = sweep_upsell_lto_email_retries()
        assert out["attempted"] == UPSELL_LTO_EMAIL_RETRY_BATCH

    def test_per_user_exception_does_not_break_sweep(self, make_user):
        """If one user's send blows up (programming error, weird DB
        state), the sweep still processes the rest. Counted as
        skipped."""
        _seed(make_user, email="ok-1@x.com", offered_hours_ago=0.5)
        _seed(make_user, email="ok-2@x.com", offered_hours_ago=0.6)

        call_count = {"n": 0}

        def _flaky(*, to, subject, body_text, body_html=None,
                   reply_to=None, list_unsubscribe_url=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            return True

        with flask_app.test_request_context("/"), \
             patch("services.email_helper.send_email", side_effect=_flaky):
            out = sweep_upsell_lto_email_retries()
        assert out["attempted"] == 2
        assert out["succeeded"] == 1
        assert out["skipped"] == 1

    def test_send_failure_leaves_user_for_next_tick(self, make_user):
        """Resend still down: the send returns False, email_sent_at
        stays NULL, the user remains a candidate for the next sweep
        tick (until they age out of the window)."""
        u = _seed(make_user, email="retry-down@x.com",
                  offered_hours_ago=0.25)
        with patch("services.email_helper.send_email", return_value=False):
            out = sweep_upsell_lto_email_retries()
        assert out["attempted"] == 1
        assert out["succeeded"] == 0
        assert out["skipped"] == 1
        db.session.refresh(u)
        # Still NULL — eligible for the next tick.
        assert u.upsell_lto_email_sent_at is None

    def test_opted_out_user_stamps_and_moves_on(
        self, make_user, monkeypatch,
    ):
        """If a user unsubscribed between qualification and the
        retry, _send_upsell_lto_email's opt-out gate stamps the
        timestamp (so we don't retry again) and returns False. The
        sweep counts it as skipped, not succeeded."""
        u = _seed(make_user, email="retry-optout@x.com",
                  offered_hours_ago=0.5, opted_out=True)
        with patch("services.email_helper.send_email") as mock_send:
            out = sweep_upsell_lto_email_retries()
        assert out["attempted"] == 1
        assert out["succeeded"] == 0
        # Mock not called — opt-out gate fired before send_email.
        mock_send.assert_not_called()
        db.session.refresh(u)
        # Timestamp WAS stamped so we don't sweep this user again.
        assert u.upsell_lto_email_sent_at is not None


# ---------------------------------------------------------------------------
# /cron/upsell-lto-email-retries endpoint
# ---------------------------------------------------------------------------

class TestCronEndpoint:
    """The HTTP shell around sweep_upsell_lto_email_retries — auth
    + response shape."""

    def _enable_cron(self, monkeypatch, secret="test-cron-secret"):
        monkeypatch.setenv("CRON_SECRET", secret)
        return secret

    def test_503_when_cron_secret_not_set(self, monkeypatch):
        monkeypatch.delenv("CRON_SECRET", raising=False)
        c = flask_app.test_client()
        r = c.post("/cron/upsell-lto-email-retries")
        assert r.status_code == 503

    def test_403_on_wrong_secret(self, monkeypatch):
        self._enable_cron(monkeypatch)
        c = flask_app.test_client()
        r = c.post(
            "/cron/upsell-lto-email-retries",
            headers={"X-Cron-Secret": "wrong"},
        )
        assert r.status_code == 403

    def test_200_with_secret_via_header(self, app_ctx, monkeypatch):
        secret = self._enable_cron(monkeypatch)
        c = flask_app.test_client()
        r = c.post(
            "/cron/upsell-lto-email-retries",
            headers={"X-Cron-Secret": secret},
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        for k in ("attempted", "succeeded", "skipped"):
            assert k in body

    def test_200_with_secret_via_query_param(self, app_ctx, monkeypatch):
        """Some cron platforms can't set custom headers. Query-param
        fallback should also work."""
        secret = self._enable_cron(monkeypatch)
        c = flask_app.test_client()
        r = c.post(f"/cron/upsell-lto-email-retries?secret={secret}")
        assert r.status_code == 200

    def test_runs_actual_sweep(self, make_user, monkeypatch):
        """Wire-through smoke test: a real candidate gets retried
        when the endpoint hits."""
        self._enable_cron(monkeypatch, secret="cron-wire-secret")
        u = _seed(make_user, email="cron-wire@x.com",
                  offered_hours_ago=0.5)
        with patch("services.email_helper.send_email", return_value=True):
            c = flask_app.test_client()
            r = c.post(
                "/cron/upsell-lto-email-retries",
                headers={"X-Cron-Secret": "cron-wire-secret"},
            )
        body = r.get_json()
        assert body["attempted"] == 1
        assert body["succeeded"] == 1
        db.session.refresh(u)
        assert u.upsell_lto_email_sent_at is not None
