"""
Tests for the second wave of paywall sites wired into
record_upsell_prompt (after the initial #139 wiring: workspace
cap, marketplace cooldown, all credit-insufficient routes via
pricing_redirect_with_return_to).

This wave adds the dashboard-level plan-gate redirects:
  - GET /integrations/gsc/<id>
  - GET /integrations/ga/<id>
  - POST /settings/white-label/update (when Free user tries to enable)

These are lower-volume than the workspace/credit sites but they're
direct expressions of intent: a Free user actively trying to use
a paid integration is high-signal. Tagged with distinct sources
(gsc_dashboard_gate, ga_dashboard_gate, white_label_settings_gate)
so the admin funnel view can break down conversions by surface.

We don't re-test the helper logic here — that lives in
test_upsell_lto.py. The integration test here just confirms each
route fires the counter when a Free user hits the paywall.
"""

from __future__ import annotations

from app import Client, db
from app import app as flask_app


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _free_user_with_ws(make_user, *, email):
    """Free user + one workspace they own — minimum needed to reach
    the integration dashboard routes (each scopes by workspace)."""
    u = make_user(plan="free", email=email)
    ws = Client(
        slug=f"upsell-ws-{u.id}",
        user_id=u.id,
        name=f"WS for {u.email}",
        website="https://x.example.com",
        website_normalized="x.example.com",
        industry="A",
        location="B",
    )
    db.session.add(ws)
    db.session.commit()
    return u, ws


# ---------------------------------------------------------------------------
# GSC dashboard plan gate → record_upsell_prompt
# ---------------------------------------------------------------------------

class TestGSCDashboardCounter:

    def test_free_hit_increments_counter(self, app_ctx, make_user):
        u, ws = _free_user_with_ws(make_user, email="gsc-dash-up@x.com")
        before = u.upsell_prompt_count

        r = _logged_in(u).get(
            f"/integrations/gsc/{ws.id}", follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")

        db.session.refresh(u)
        assert u.upsell_prompt_count == before + 1

    def test_pro_user_no_counter_change(self, app_ctx, make_user):
        """Paid plan goes through the gate and reaches the dashboard;
        no upsell counter movement."""
        u = make_user(plan="pro", email="gsc-dash-pro@x.com")
        ws = Client(
            slug=f"gsc-dash-pro-ws-{u.id}",
            user_id=u.id, name="x",
            website="https://x.example.com",
            website_normalized="x.example.com",
            industry="A", location="B",
        )
        db.session.add(ws)
        db.session.commit()

        r = _logged_in(u).get(f"/integrations/gsc/{ws.id}")
        assert r.status_code == 200
        db.session.refresh(u)
        # Paid users are never counted (record_upsell_prompt no-ops).
        assert u.upsell_prompt_count == 0


# ---------------------------------------------------------------------------
# GA dashboard plan gate → record_upsell_prompt
# ---------------------------------------------------------------------------

class TestGADashboardCounter:

    def test_free_hit_increments_counter(self, app_ctx, make_user):
        u, ws = _free_user_with_ws(make_user, email="ga-dash-up@x.com")
        before = u.upsell_prompt_count

        r = _logged_in(u).get(
            f"/integrations/ga/{ws.id}", follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")

        db.session.refresh(u)
        assert u.upsell_prompt_count == before + 1


# ---------------------------------------------------------------------------
# White-label settings (enable from Free) → record_upsell_prompt
# ---------------------------------------------------------------------------

class TestWhiteLabelSettingsCounter:

    def test_free_enable_attempt_increments_counter(
        self, app_ctx, make_user,
    ):
        """A Free user posting enable=on to /settings/white-label/update
        is the most explicit 'I want this paid feature' signal we
        get — should count toward LTO qualification."""
        u = make_user(plan="free", email="wl-up@x.com")
        before = u.upsell_prompt_count

        r = _logged_in(u).post(
            "/settings/white-label/update",
            data={"enable": "on", "agency_name": "Future Agency"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert u.upsell_prompt_count == before + 1
        # Fields still saved (the existing behavior — Free preps for
        # upgrade), the toggle just stays force-disabled.
        assert u.agency_name == "Future Agency"
        assert u.is_white_label_enabled is False

    def test_free_save_without_enable_no_counter_change(
        self, app_ctx, make_user,
    ):
        """Just saving fields without toggling enable=on isn't an
        upgrade signal — the counter shouldn't tick."""
        u = make_user(plan="free", email="wl-noenable@x.com")
        before = u.upsell_prompt_count

        _logged_in(u).post(
            "/settings/white-label/update",
            data={"agency_name": "Just Saving"},
        )
        db.session.refresh(u)
        assert u.upsell_prompt_count == before  # unchanged

    def test_pro_enable_no_counter_change(self, app_ctx, make_user):
        u = make_user(plan="pro", email="wl-pro@x.com")
        _logged_in(u).post(
            "/settings/white-label/update",
            data={"enable": "on", "agency_name": "Pro Agency"},
        )
        db.session.refresh(u)
        # Pro user qualifies → enable succeeds → no upsell counter movement.
        assert u.upsell_prompt_count == 0
        assert u.is_white_label_enabled is True


# ---------------------------------------------------------------------------
# Source tag granularity — distinct sources per surface
# ---------------------------------------------------------------------------

class TestSourceTagsAreDistinct:
    """Smoke test: each new surface uses a distinct source string in
    record_upsell_prompt calls, so the admin breakdown can attribute
    conversions per paywall. We patch the helper to record sources
    and just verify the tags fire correctly."""

    def test_each_site_calls_with_its_own_source(
        self, app_ctx, make_user, monkeypatch,
    ):
        u, ws = _free_user_with_ws(make_user, email="srctags@x.com")
        captured = []

        def _spy(user, source=""):
            captured.append(source)

        monkeypatch.setattr("app.record_upsell_prompt", _spy)

        c = _logged_in(u)
        c.get(f"/integrations/gsc/{ws.id}")
        c.get(f"/integrations/ga/{ws.id}")
        c.post(
            "/settings/white-label/update",
            data={"enable": "on", "agency_name": "X"},
        )

        # Each route called record_upsell_prompt with its tag.
        assert "gsc_dashboard_gate" in captured
        assert "ga_dashboard_gate" in captured
        assert "white_label_settings_gate" in captured
