"""
Tests for the limited-time-offer (LTO) upsell flow.

Lifecycle:
    none → shown → { dismissed | accepted | expired }

A Free user accumulates upsell_prompt_count each time they bump
into a paid-feature paywall via record_upsell_prompt(). At the
configured threshold the LTO modal qualifies: status flips to
"shown", offered_at is set, expires_at is set 24h out. From there
the user can dismiss (POST /upsell/dismiss), accept (handled by
the Stripe checkout flow elsewhere), or let the offer lapse (the
context processor auto-flips to "expired" on read).

The test surface here is the helpers and the dismiss endpoint —
end-to-end conversion is covered by test_stripe_webhook.py.
"""

from __future__ import annotations

from datetime import timedelta

from app import (
    UPSELL_LTO_TTL_HOURS,
    UPSELL_PROMPT_THRESHOLD,
    db,
    record_upsell_prompt,
    resolve_upsell_lto,
)
from app import app as flask_app
from dtutils import utcnow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


# ---------------------------------------------------------------------------
# record_upsell_prompt — threshold + qualification
# ---------------------------------------------------------------------------

class TestRecordUpsellPrompt:
    """The counter helper. Increments only for Free users; triggers
    the LTO at threshold; idempotent after qualification."""

    def test_free_user_under_threshold_just_increments(self, make_user):
        u = make_user(plan="free", email="up-under@x.com")
        # One call below threshold → count = 1, no qualification yet.
        record_upsell_prompt(u, source="test")
        db.session.refresh(u)
        assert u.upsell_prompt_count == 1
        assert u.upsell_lto_status == "none"
        assert u.upsell_lto_offered_at is None
        assert u.upsell_lto_expires_at is None

    def test_free_user_qualifies_at_threshold(self, make_user):
        u = make_user(plan="free", email="up-thresh@x.com")
        for _ in range(UPSELL_PROMPT_THRESHOLD):
            record_upsell_prompt(u, source="test")
        db.session.refresh(u)
        assert u.upsell_prompt_count == UPSELL_PROMPT_THRESHOLD
        assert u.upsell_lto_status == "shown"
        assert u.upsell_lto_offered_at is not None
        assert u.upsell_lto_expires_at is not None
        # Expiry is roughly UPSELL_LTO_TTL_HOURS out from offered_at,
        # within a few seconds of clock skew.
        gap = u.upsell_lto_expires_at - u.upsell_lto_offered_at
        assert abs(gap.total_seconds() - UPSELL_LTO_TTL_HOURS * 3600) < 5

    def test_already_shown_user_is_not_re_triggered(self, make_user):
        """Once a user is in 'shown' state, subsequent paywall
        encounters don't bump the count or reset the timer.
        Otherwise a user who never dismisses would have their
        offered_at perpetually slide forward."""
        u = make_user(plan="free", email="up-stable@x.com")
        for _ in range(UPSELL_PROMPT_THRESHOLD):
            record_upsell_prompt(u, source="test")
        db.session.refresh(u)
        offered_at_before = u.upsell_lto_offered_at
        count_before = u.upsell_prompt_count

        # More paywall encounters after qualification.
        for _ in range(5):
            record_upsell_prompt(u, source="more")
        db.session.refresh(u)
        # Nothing changed.
        assert u.upsell_lto_offered_at == offered_at_before
        assert u.upsell_prompt_count == count_before

    def test_dismissed_user_is_not_re_tracked(self, make_user):
        """Dismissal is final — re-tracking a dismissed user
        shouldn't kick them back to 'shown' or re-arm a fresh
        counter. We respect their explicit no."""
        u = make_user(plan="free", email="up-dismiss@x.com")
        for _ in range(UPSELL_PROMPT_THRESHOLD):
            record_upsell_prompt(u, source="test")
        u.upsell_lto_status = "dismissed"
        db.session.commit()
        before = u.upsell_prompt_count

        record_upsell_prompt(u, source="post-dismiss")
        db.session.refresh(u)
        assert u.upsell_lto_status == "dismissed"
        assert u.upsell_prompt_count == before

    def test_paid_user_is_never_tracked(self, make_user):
        """Pro/Growth users have already converted; the helper
        no-ops for them so we don't waste cycles or accidentally
        annoy a paying customer with the popup."""
        u = make_user(plan="pro", email="up-pro@x.com")
        for _ in range(UPSELL_PROMPT_THRESHOLD + 2):
            record_upsell_prompt(u, source="test")
        db.session.refresh(u)
        assert u.upsell_prompt_count == 0
        assert u.upsell_lto_status == "none"

    def test_admin_user_is_never_tracked(self, make_user):
        """Admins on Free (internal accounts) shouldn't see the
        popup either — same convention as every other gate."""
        u = make_user(plan="free", role="admin", email="up-admin@x.com")
        for _ in range(UPSELL_PROMPT_THRESHOLD + 2):
            record_upsell_prompt(u, source="test")
        db.session.refresh(u)
        assert u.upsell_prompt_count == 0
        assert u.upsell_lto_status == "none"

    def test_anonymous_no_op(self, app_ctx):
        """No user → no-op, no crash."""
        record_upsell_prompt(None, source="test")
        # No assertion needed beyond "didn't raise"


# ---------------------------------------------------------------------------
# resolve_upsell_lto — view-time resolution + auto-expire
# ---------------------------------------------------------------------------

class TestResolveUpsellLTO:

    def test_none_status_returns_none(self, make_user):
        u = make_user(plan="free", email="rs-none@x.com")
        assert resolve_upsell_lto(u) is None

    def test_dismissed_returns_none(self, make_user):
        u = make_user(plan="free", email="rs-dismissed@x.com")
        u.upsell_lto_status = "dismissed"
        db.session.commit()
        assert resolve_upsell_lto(u) is None

    def test_shown_within_window_returns_dict(self, make_user):
        u = make_user(plan="free", email="rs-live@x.com")
        now = utcnow()
        u.upsell_lto_status = "shown"
        u.upsell_lto_offered_at = now
        u.upsell_lto_expires_at = now + timedelta(hours=12)
        u.upsell_prompt_count = 4
        db.session.commit()

        out = resolve_upsell_lto(u)
        assert out is not None
        assert out["active"] is True
        # 12h window → hours_left is 11 or 12 depending on float rounding.
        assert out["hours_left"] in (11, 12)
        assert out["prompt_count"] == 4

    def test_shown_past_expiry_auto_flips_to_expired(self, make_user):
        """If the user loads a page after expires_at, the function
        is responsible for flipping their status to 'expired' and
        returning None so the modal doesn't render stale."""
        u = make_user(plan="free", email="rs-expired@x.com")
        now = utcnow()
        u.upsell_lto_status = "shown"
        u.upsell_lto_offered_at = now - timedelta(hours=48)
        u.upsell_lto_expires_at = now - timedelta(hours=24)  # 1d past
        db.session.commit()

        out = resolve_upsell_lto(u)
        assert out is None
        db.session.refresh(u)
        assert u.upsell_lto_status == "expired"

    def test_anonymous_returns_none(self, app_ctx):
        assert resolve_upsell_lto(None) is None


# ---------------------------------------------------------------------------
# POST /upsell/dismiss
# ---------------------------------------------------------------------------

class TestDismissEndpoint:

    def test_shown_user_can_dismiss(self, make_user):
        u = make_user(plan="free", email="dm-shown@x.com")
        now = utcnow()
        u.upsell_lto_status = "shown"
        u.upsell_lto_offered_at = now
        u.upsell_lto_expires_at = now + timedelta(hours=12)
        db.session.commit()

        r = _logged_in(u).post("/upsell/dismiss")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["transitioned"] is True

        db.session.refresh(u)
        assert u.upsell_lto_status == "dismissed"

    def test_none_status_is_idempotent_no_op(self, make_user):
        """A user who never qualified posting to /dismiss anyway
        gets a friendly ok response (no error), no state change."""
        u = make_user(plan="free", email="dm-none@x.com")
        r = _logged_in(u).post("/upsell/dismiss")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["transitioned"] is False
        db.session.refresh(u)
        assert u.upsell_lto_status == "none"

    def test_anonymous_redirected_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.post("/upsell/dismiss", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")


# ---------------------------------------------------------------------------
# Integration with paywall sites
# ---------------------------------------------------------------------------

class TestPaywallSiteIntegration:
    """Smoke tests that the wired-in call sites actually call
    record_upsell_prompt. We don't simulate the whole route flow —
    just enough to confirm the counter ticks when a Free user bumps
    into the paywall."""

    def test_workspace_cap_increments_counter(self, make_user):
        """Free user with 1 workspace hitting /clients/new sees the
        cap message and the counter increments by 1."""
        from app import Client
        u = make_user(plan="free", email="pw-ws-cap@x.com")
        # Free cap = 1; pre-fill the cap.
        existing = Client(
            slug="pw-existing", user_id=u.id, name="X",
            website="https://x.example.com",
            website_normalized="x.example.com",
            industry="A", location="B",
        )
        db.session.add(existing)
        db.session.commit()

        before = u.upsell_prompt_count
        r = _logged_in(u).get("/clients/new", follow_redirects=False)
        # Bounces to pricing.
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")
        db.session.refresh(u)
        assert u.upsell_prompt_count == before + 1

    def test_pricing_redirect_helper_increments(self, make_user):
        """The pricing_redirect_with_return_to helper covers all the
        credit-insufficient paywall sites in one place. We exercise
        it by posting a /client/<slug>/run-audit with no credits."""
        from app import Client
        u = make_user(plan="free", balance=0, email="pw-credits@x.com")
        ws = Client(
            slug="pw-audit", user_id=u.id, name="Audit Target",
            website="https://audit.example.com",
            website_normalized="audit.example.com",
            industry="A", location="B",
        )
        db.session.add(ws)
        db.session.commit()

        before = u.upsell_prompt_count
        c = _logged_in(u)
        r = c.post(
            f"/client/{ws.slug}/run-audit",
            data={
                "website": ws.website, "industry": "A", "location": "B",
                "topic": "x", "audit_type": "quick",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")
        db.session.refresh(u)
        assert u.upsell_prompt_count == before + 1
