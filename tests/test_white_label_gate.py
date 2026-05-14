"""
Tests for the white-label plan gate.

The white-label feature has two surfaces that need to stay in sync:

  1. The settings route (POST /settings/white-label/update) gates
     enabling the toggle on whether the user currently has access
     to the `white_label` feature. Free users can save the agency
     fields but can't flip the toggle on.

  2. The render path (agency_branding() helper) re-checks feature
     access on every read. Without this, a Pro user who enabled
     white-label, then downgraded to Free, would keep delivering
     branded reports indefinitely without paying — a billing leak.

Both gates route through user_has_feature("white_label") which
checks PLAN_IMPLICIT_MODULES (Pro/Growth qualify) and explicit
UserModule rows (forward-compatible with the modules system).
"""

from __future__ import annotations

from app import agency_branding, db
from app import app as flask_app


# ---------------------------------------------------------------------------
# agency_branding() — read-time gate
# ---------------------------------------------------------------------------

class TestAgencyBrandingReadGate:
    """The helper that resolves the branding dict for templates / PDFs.
    Must short-circuit to DarInsights branding whenever the user no
    longer qualifies, even if their stored toggle is True."""

    def test_anonymous_user_gets_default_branding(self, app_ctx):
        out = agency_branding(None)
        assert out["active"] is False
        assert out["name"] == "DarInsights"

    def test_pro_with_toggle_on_returns_white_label(self, make_user):
        u = make_user(plan="pro", email="wl-pro@x.com")
        u.is_white_label_enabled = True
        u.agency_name = "Acme Agency"
        u.agency_tagline = "We make brands shine"
        db.session.commit()

        out = agency_branding(u)
        assert out["active"] is True
        assert out["name"] == "Acme Agency"
        assert out["tagline"] == "We make brands shine"

    def test_pro_with_toggle_off_returns_default(self, make_user):
        u = make_user(plan="pro", email="wl-pro-off@x.com")
        u.is_white_label_enabled = False
        u.agency_name = "Acme Agency"
        db.session.commit()

        out = agency_branding(u)
        assert out["active"] is False
        assert out["name"] == "DarInsights"

    def test_growth_with_toggle_on_returns_white_label(self, make_user):
        """Growth plan should pass the implicit-modules check just
        like Pro."""
        u = make_user(plan="growth", email="wl-growth@x.com")
        u.is_white_label_enabled = True
        u.agency_name = "Growth Agency"
        db.session.commit()

        out = agency_branding(u)
        assert out["active"] is True
        assert out["name"] == "Growth Agency"

    def test_free_with_toggle_on_returns_default_billing_leak_guard(
        self, make_user,
    ):
        """**THE billing leak guard.** A Free user with the toggle
        True on the row (legacy from a previous Pro stint, or
        manually set in support) must NOT get white-label branding.

        Reproduces the scenario: user signs up Pro, enables
        white-label, then their subscription lapses or they
        downgrade to Free. The `is_white_label_enabled` boolean
        stays True on the row — only this gate prevents the
        unpaid-branding leak.
        """
        u = make_user(plan="free", email="wl-free-leak@x.com")
        u.is_white_label_enabled = True
        u.agency_name = "Should Not Show"
        db.session.commit()

        out = agency_branding(u)
        assert out["active"] is False, (
            "Free user with toggle still True must NOT render "
            "white-label — this is the billing leak guard."
        )
        assert out["name"] == "DarInsights"

    def test_logo_url_surfaced_even_when_inactive(self, make_user):
        """The Settings → White-label upload card uses agency_branding()
        to render the current logo preview, so the logo_url field
        must be populated even when active=False."""
        u = make_user(plan="free", email="wl-logo-preview@x.com")
        u.is_white_label_enabled = False
        u.agency_logo_filename = "agency-1-abc.png"
        db.session.commit()

        out = agency_branding(u)
        assert out["active"] is False
        # logo_url is best-effort — may be None if storage backend
        # returns None for the lookup, but must be present in the dict.
        assert "logo_url" in out


# ---------------------------------------------------------------------------
# /settings/white-label/update — write-time gate
# ---------------------------------------------------------------------------

class TestWhiteLabelSettingsGate:
    """The form post that toggles white-label on/off. Free users who
    POST enable=on must have the toggle force-disabled with a
    friendly upgrade nudge; subscriber plans persist the choice."""

    def _logged_client(self, user):
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(user.id)
            s["_fresh"] = True
        return c

    def test_pro_can_enable(self, make_user):
        u = make_user(plan="pro", email="set-wl-pro@x.com")
        c = self._logged_client(u)
        r = c.post(
            "/settings/white-label/update",
            data={
                "enable": "on",
                "agency_name": "My Agency",
                "agency_tagline": "tagline",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert u.is_white_label_enabled is True
        assert u.agency_name == "My Agency"

    def test_free_cannot_enable_but_fields_persist(self, make_user):
        """Free users posting enable=on get their fields saved (so
        upgrading later activates instantly) but the toggle is
        force-disabled."""
        u = make_user(plan="free", email="set-wl-free@x.com")
        c = self._logged_client(u)
        r = c.post(
            "/settings/white-label/update",
            data={
                "enable": "on",
                "agency_name": "Prepping Brand",
                "agency_tagline": "Coming soon",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(u)
        # Toggle force-disabled.
        assert u.is_white_label_enabled is False
        # But the fields were saved so the user keeps their work.
        assert u.agency_name == "Prepping Brand"
        assert u.agency_tagline == "Coming soon"

    def test_pro_can_disable(self, make_user):
        """Disabling the toggle just persists False; no plan check
        needed for the disable path."""
        u = make_user(plan="pro", email="set-wl-pro-off@x.com")
        u.is_white_label_enabled = True
        db.session.commit()
        c = self._logged_client(u)
        r = c.post(
            "/settings/white-label/update",
            data={"agency_name": "X"},  # enable absent
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert u.is_white_label_enabled is False

    def test_anonymous_redirected_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.post(
            "/settings/white-label/update",
            data={"enable": "on"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")


# ---------------------------------------------------------------------------
# Cross-cutting: settings + read gate together
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_pro_enables_then_downgrades_to_free_loses_branding(
        self, make_user,
    ):
        """Simulate the billing-leak scenario end-to-end:
          1. User on Pro enables white-label, sets brand fields
          2. Stripe webhook downgrades them to Free (we just mutate
             the column directly here — the webhook code path is
             covered separately in test_stripe_webhook.py)
          3. agency_branding() should now return active=False even
             though is_white_label_enabled is still True on the row
        """
        u = make_user(plan="pro", email="downgrade-leak@x.com")
        # Step 1: enable on Pro
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(u.id)
            s["_fresh"] = True
        c.post(
            "/settings/white-label/update",
            data={
                "enable": "on",
                "agency_name": "Pre-downgrade Brand",
            },
        )
        db.session.refresh(u)
        assert u.is_white_label_enabled is True
        assert agency_branding(u)["active"] is True

        # Step 2: downgrade (mimics what Stripe webhook does on
        # subscription deletion / lapse)
        u.plan = "free"
        db.session.commit()

        # Step 3: branding now degrades to default automatically
        out = agency_branding(u)
        assert out["active"] is False
        assert out["name"] == "DarInsights"
        # The stored toggle hasn't moved — only the read-time gate
        # changed behavior.
        db.session.refresh(u)
        assert u.is_white_label_enabled is True
