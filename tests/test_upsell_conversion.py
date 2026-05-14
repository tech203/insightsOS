"""
Tests for upsell conversion attribution.

The Limited-Time-Offer popup (PR #139) tracks Free-user paywall
encounters and shows a 24h-expiry modal at the threshold. This
test surface covers the conversion side of the funnel:

  - /pricing receives a `?source=upsell_lto` query param when the
    user clicks the modal CTA. Stashed in session.
  - /stripe/checkout/plan/<slug> pops it from session and passes
    through to Stripe metadata.
  - The webhook reads metadata.source on checkout.session.completed
    and flips upsell_lto_status from "shown" to "accepted" — only
    when both conditions are true:
        * source == "upsell_lto"
        * existing status == "shown"
    so stale source values can't false-attribute and an organic
    upgrade (no source) doesn't touch a still-active "shown" row.

End-to-end Stripe live tests would require real Stripe keys; the
webhook handler is exercised directly here by simulating the
parsed event payload Stripe would have sent.
"""

from __future__ import annotations

from datetime import timedelta

from app import _handle_checkout_completed, db
from app import app as flask_app
from dtutils import utcnow


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _qualified_lto(user, hours_left=12):
    """Put `user` into the "shown" state with a live expiry."""
    now = utcnow()
    user.upsell_lto_status = "shown"
    user.upsell_lto_offered_at = now
    user.upsell_lto_expires_at = now + timedelta(hours=hours_left)
    user.upsell_prompt_count = 4
    db.session.commit()


# ---------------------------------------------------------------------------
# /pricing?source= stashes into session
# ---------------------------------------------------------------------------

class TestPricingSourceCapture:

    def test_known_source_is_stashed(self, app_ctx):
        c = flask_app.test_client()
        c.get("/pricing?source=upsell_lto")
        with c.session_transaction() as s:
            assert s.get("pricing_source") == "upsell_lto"

    def test_unknown_source_not_stashed(self, app_ctx):
        """Defense against URL-injected attribution — only whitelisted
        sources land in the session."""
        c = flask_app.test_client()
        c.get("/pricing?source=arbitrary_payload")
        with c.session_transaction() as s:
            assert "pricing_source" not in s

    def test_no_source_param_no_session_write(self, app_ctx):
        c = flask_app.test_client()
        c.get("/pricing")
        with c.session_transaction() as s:
            assert "pricing_source" not in s

    def test_source_case_normalized(self, app_ctx):
        """Mixed-case input should still match the whitelist."""
        c = flask_app.test_client()
        c.get("/pricing?source=Upsell_LTO")
        with c.session_transaction() as s:
            assert s.get("pricing_source") == "upsell_lto"


# ---------------------------------------------------------------------------
# Webhook flips upsell_lto_status → accepted
# ---------------------------------------------------------------------------

class TestWebhookAcceptanceAttribution:
    """The webhook handler is the source of truth for plan + LTO
    state. Calling it directly with a fabricated Stripe payload
    exercises the real branch logic without needing live Stripe."""

    def _payload(self, user_id, *, source=None, plan_slug="pro"):
        meta = {
            "kind": "subscription",
            "user_id": str(user_id),
            "plan_slug": plan_slug,
        }
        if source is not None:
            meta["source"] = source
        return {
            "metadata": meta,
            "customer": "cus_test_123",
            "subscription": "sub_test_456",
        }

    def test_lto_source_flips_status_to_accepted(self, make_user):
        u = make_user(plan="free", email="conv-shown@x.com")
        _qualified_lto(u)
        out_user_id, notes = _handle_checkout_completed(
            self._payload(u.id, source="upsell_lto"),
        )
        db.session.refresh(u)
        assert out_user_id == u.id
        assert u.plan == "pro"
        assert u.upsell_lto_status == "accepted"
        assert "lto:accepted" in (notes or "")

    def test_organic_upgrade_leaves_status_alone(self, make_user):
        """A user with status='shown' who upgrades from /pricing without
        the source param (e.g. they dismissed the popup and came back
        organically) should NOT have status flipped to accepted —
        the LTO offer keeps ticking until they dismiss or it expires."""
        u = make_user(plan="free", email="conv-organic@x.com")
        _qualified_lto(u)
        _handle_checkout_completed(self._payload(u.id, source=None))
        db.session.refresh(u)
        assert u.plan == "pro"
        # Status untouched — this conversion was organic.
        assert u.upsell_lto_status == "shown"

    def test_lto_source_on_non_shown_user_no_op(self, make_user):
        """Stale source values mustn't false-attribute. If the user's
        LTO state isn't 'shown' (already dismissed / expired / never
        qualified), the webhook flips plan but leaves LTO alone."""
        u = make_user(plan="free", email="conv-dismissed@x.com")
        _qualified_lto(u)
        u.upsell_lto_status = "dismissed"
        db.session.commit()

        _handle_checkout_completed(self._payload(u.id, source="upsell_lto"))
        db.session.refresh(u)
        assert u.plan == "pro"
        assert u.upsell_lto_status == "dismissed"

    def test_unknown_source_value_no_attribution(self, make_user):
        """Future / typo source values shouldn't attribute either —
        only the literal 'upsell_lto' triggers the flip."""
        u = make_user(plan="free", email="conv-unknown@x.com")
        _qualified_lto(u)
        _handle_checkout_completed(
            self._payload(u.id, source="some_other_thing"),
        )
        db.session.refresh(u)
        assert u.plan == "pro"
        assert u.upsell_lto_status == "shown"

    def test_paid_user_upgrade_no_lto_touch(self, make_user):
        """A user who's already paid and is changing plans (pro→growth)
        shouldn't have LTO state churn — they're past the LTO funnel.
        Helper no-ops for status != 'shown'."""
        u = make_user(plan="pro", email="conv-paid@x.com")
        # Past-LTO state shouldn't even exist for paid users, but if
        # it somehow did (legacy data, manual flag), the webhook
        # shouldn't break.
        u.upsell_lto_status = "accepted"
        db.session.commit()
        _handle_checkout_completed(
            self._payload(u.id, source="upsell_lto", plan_slug="growth"),
        )
        db.session.refresh(u)
        assert u.plan == "growth"
        # Already 'accepted' — stays accepted (no re-transition).
        assert u.upsell_lto_status == "accepted"


# ---------------------------------------------------------------------------
# End-to-end: /pricing → /stripe/checkout/plan → metadata propagation
# ---------------------------------------------------------------------------

class TestSourcePropagation:
    """Verify the session-based handoff between /pricing and the
    Stripe checkout endpoint. We don't hit Stripe here — we patch
    the helper to capture what would have been sent."""

    def test_source_propagates_to_checkout_metadata(
        self, app_ctx, make_user, monkeypatch,
    ):
        from unittest.mock import patch as patch_

        u = make_user(plan="free", email="prop-source@x.com")
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return {"url": "https://stripe.test/checkout", "id": "cs_test"}

        # Stripe must look "configured" so we hit the real branch.
        with patch_(
            "services.stripe_helper.create_subscription_checkout_session",
            side_effect=_capture,
        ), patch_(
            "services.stripe_helper.is_stripe_configured", return_value=True,
        ):
            c = _logged_in(u)
            # Step 1: hit /pricing with the source param. This stashes
            # the source into session.
            c.get("/pricing?source=upsell_lto")
            # Step 2: hit the checkout route. The helper is patched to
            # capture kwargs instead of calling Stripe.
            r = c.post("/stripe/checkout/plan/pro", follow_redirects=False)

        # The checkout route either 303-redirects (Stripe live) or
        # bounces somewhere; what matters is the captured kwargs.
        assert captured.get("source") == "upsell_lto"
        del r  # silence unused-var

    def test_no_source_when_user_didnt_come_through_modal(
        self, app_ctx, make_user,
    ):
        """A user who hits /stripe/checkout/plan/<slug> without going
        through /pricing?source=… (deep link, bookmark, organic) gets
        no source attribution — that's correct, the conversion is
        organic."""
        from unittest.mock import patch as patch_

        u = make_user(plan="free", email="prop-nosource@x.com")
        captured = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return {"url": "https://stripe.test/checkout", "id": "cs_test"}

        with patch_(
            "services.stripe_helper.create_subscription_checkout_session",
            side_effect=_capture,
        ), patch_(
            "services.stripe_helper.is_stripe_configured", return_value=True,
        ):
            c = _logged_in(u)
            # Skip /pricing entirely.
            c.post("/stripe/checkout/plan/pro", follow_redirects=False)

        # No source key (or None) on the captured call.
        assert captured.get("source") is None
