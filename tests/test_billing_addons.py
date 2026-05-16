"""
Tests for the billing-addon routes:

  POST /billing/buy-workspace      — buy 1 extra workspace
  POST /billing/buy-seat           — buy 1 extra seat
  POST /billing/release-workspace  — release 1 extra workspace
  POST /billing/release-seat       — release 1 extra seat

Routes have two modes:
  - Stripe-configured: redirect to a Checkout session URL (303)
  - Dev fallback: directly increment User.extra_workspaces /
    .extra_seats so the rest of the flow can be exercised

We test both — the dev fallback covers the happy path under tests
without needing Stripe stubs; a parametric "with Stripe configured"
test confirms the Checkout-redirect branch fires correctly.

Critical safeguards locked down:
  - **Plan gate**: Free can't buy add-ons (plan_allows_*_addon)
  - **Release-without-orphan**: refuse to release a slot that
    would put used > new_total. Without this guard, a release
    could leave an existing workspace unreachable (over-cap) or
    a team member without a seat.
"""

from __future__ import annotations

from unittest.mock import patch

from app import Client, db
from app import app as flask_app


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


# ---------------------------------------------------------------------------
# POST /billing/buy-workspace
# ---------------------------------------------------------------------------

class TestBuyWorkspace:

    def test_pro_dev_fallback_increments_count(
        self, make_user, monkeypatch,
    ):
        """Dev mode (no STRIPE_SECRET_KEY) increments
        user.extra_workspaces directly so the flow can be tested
        end-to-end without Stripe."""
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        u = make_user(plan="pro", email="bw-pro@x.com")
        before = int(u.extra_workspaces or 0)

        r = _logged_in(u).post(
            "/billing/buy-workspace", follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert int(u.extra_workspaces) == before + 1

    def test_growth_can_buy(self, make_user, monkeypatch):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        u = make_user(plan="growth", email="bw-growth@x.com")
        _logged_in(u).post("/billing/buy-workspace")
        db.session.refresh(u)
        assert int(u.extra_workspaces) >= 1

    def test_free_user_blocked(self, make_user, monkeypatch):
        """Free plan can't purchase add-ons — must hit
        plan_allows_workspace_addon gate and bounce to pricing."""
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        u = make_user(plan="free", email="bw-free@x.com")
        r = _logged_in(u).post(
            "/billing/buy-workspace", follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")
        db.session.refresh(u)
        assert int(u.extra_workspaces or 0) == 0

    def test_stripe_path_redirects_to_checkout(
        self, make_user, monkeypatch,
    ):
        """When STRIPE_SECRET_KEY + STRIPE_PRICE_EXTRA_WORKSPACE are set,
        the route delegates to a real Checkout session. Stripe SDK is
        mocked to return a fake URL — confirms 303 with the
        right Location."""
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
        monkeypatch.setenv("STRIPE_PRICE_EXTRA_WORKSPACE", "price_xyz")
        u = make_user(plan="pro", email="bw-stripe@x.com")
        before = int(u.extra_workspaces or 0)

        with patch("stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = type(
                "S", (), {"url": "https://checkout.stripe.test/abc"}
            )()
            r = _logged_in(u).post(
                "/billing/buy-workspace", follow_redirects=False,
            )

        assert r.status_code == 303
        assert "checkout.stripe.test/abc" in (r.headers.get("Location") or "")
        # In the Stripe path, we DON'T directly increment — the
        # webhook does that on checkout.session.completed. So the
        # column should be unchanged here.
        db.session.refresh(u)
        assert int(u.extra_workspaces or 0) == before

    def test_stripe_failure_falls_back_gracefully(
        self, make_user, monkeypatch,
    ):
        """If Stripe throws (network blip, bad API key), the route
        flashes an error and bounces to settings — must NOT 500."""
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
        monkeypatch.setenv("STRIPE_PRICE_EXTRA_WORKSPACE", "price_xyz")
        u = make_user(plan="pro", email="bw-fail@x.com")

        with patch(
            "stripe.checkout.Session.create",
            side_effect=RuntimeError("simulated Stripe outage"),
        ):
            r = _logged_in(u).post(
                "/billing/buy-workspace", follow_redirects=False,
            )
        assert r.status_code == 302
        # Bounces back to settings, not /pricing or a 500.
        loc = r.headers.get("Location") or ""
        assert "/settings" in loc or "/billing" in loc


# ---------------------------------------------------------------------------
# POST /billing/buy-seat
# ---------------------------------------------------------------------------

class TestBuySeat:

    def test_pro_dev_fallback_increments_seats(
        self, make_user, monkeypatch,
    ):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        u = make_user(plan="pro", email="bs-pro@x.com")
        _logged_in(u).post("/billing/buy-seat")
        db.session.refresh(u)
        assert int(u.extra_seats) >= 1

    def test_free_user_blocked(self, make_user, monkeypatch):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        u = make_user(plan="free", email="bs-free@x.com")
        r = _logged_in(u).post(
            "/billing/buy-seat", follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")
        db.session.refresh(u)
        assert int(u.extra_seats or 0) == 0

    def test_stripe_path_redirects_to_checkout(
        self, make_user, monkeypatch,
    ):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_abc")
        monkeypatch.setenv("STRIPE_PRICE_EXTRA_SEAT", "price_seat_xyz")
        u = make_user(plan="growth", email="bs-stripe@x.com")

        with patch("stripe.checkout.Session.create") as mock_create:
            mock_create.return_value = type(
                "S", (), {"url": "https://checkout.stripe.test/seat"}
            )()
            r = _logged_in(u).post(
                "/billing/buy-seat", follow_redirects=False,
            )
        assert r.status_code == 303
        assert "checkout.stripe.test/seat" in (r.headers.get("Location") or "")


# ---------------------------------------------------------------------------
# POST /billing/release-workspace
# ---------------------------------------------------------------------------

class TestReleaseWorkspace:

    def test_owner_can_release_unused_extra(self, make_user):
        u = make_user(plan="pro", email="rw-ok@x.com")
        u.extra_workspaces = 2
        db.session.commit()

        r = _logged_in(u).post(
            "/billing/release-workspace", follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(u)
        assert int(u.extra_workspaces) == 1

    def test_no_extras_to_release_is_noop(self, make_user):
        u = make_user(plan="pro", email="rw-none@x.com")
        # extra_workspaces defaults to 0
        _logged_in(u).post("/billing/release-workspace")
        db.session.refresh(u)
        assert int(u.extra_workspaces) == 0

    def test_release_blocked_when_would_orphan_workspace(self, make_user):
        """Pro base = 3 workspaces; user has 2 extras → 5 total cap.
        If they're actively using all 5 workspaces, release would
        drop the cap to 4 — orphaning one. Refuse."""
        u = make_user(plan="pro", email="rw-orphan@x.com")
        u.extra_workspaces = 2
        db.session.commit()
        # Pro base cap is 3; +2 extras = 5. Create 5 workspaces.
        for i in range(5):
            ws = Client(
                slug=f"rw-orphan-ws-{i}",
                user_id=u.id, name=f"WS {i}",
                website=f"https://w{i}.x.com",
                website_normalized=f"w{i}.x.com",
                industry="A", location="B",
            )
            db.session.add(ws)
        db.session.commit()

        r = _logged_in(u).post(
            "/billing/release-workspace", follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(u)
        # Extras unchanged — the orphan guard refused the release.
        assert int(u.extra_workspaces) == 2

    def test_release_ok_when_using_less_than_new_total(self, make_user):
        """Pro base 3 + 2 extras = 5; using only 3 workspaces.
        Releasing drops total to 4, still ≥ 3 used → allowed."""
        u = make_user(plan="pro", email="rw-headroom@x.com")
        u.extra_workspaces = 2
        db.session.commit()
        for i in range(3):
            ws = Client(
                slug=f"rw-head-{i}",
                user_id=u.id, name=f"WS {i}",
                website=f"https://w{i}.x.com",
                website_normalized=f"w{i}.x.com",
                industry="A", location="B",
            )
            db.session.add(ws)
        db.session.commit()

        _logged_in(u).post("/billing/release-workspace")
        db.session.refresh(u)
        assert int(u.extra_workspaces) == 1


# ---------------------------------------------------------------------------
# POST /billing/release-seat
# ---------------------------------------------------------------------------

class TestReleaseSeat:

    def test_owner_can_release_unused_extra(self, make_user):
        u = make_user(plan="pro", email="rs-ok@x.com")
        u.extra_seats = 3
        db.session.commit()

        _logged_in(u).post("/billing/release-seat")
        db.session.refresh(u)
        assert int(u.extra_seats) == 2

    def test_no_extras_to_release_is_noop(self, make_user):
        u = make_user(plan="pro", email="rs-none@x.com")
        _logged_in(u).post("/billing/release-seat")
        db.session.refresh(u)
        assert int(u.extra_seats) == 0

    def test_release_blocked_when_would_orphan_member(self, make_user):
        """Pro base = 3 seats; +2 extras = 5. If 5 members are
        attached (incl. owner), releasing would drop the cap to 4,
        orphaning one. Refuse."""
        u = make_user(plan="pro", email="rs-orphan@x.com")
        u.extra_seats = 2
        db.session.commit()
        # Pro seat_limit is 3 base. count_team_members includes
        # owner + members + pending invites. Owner is 1, so add
        # 4 members to reach 5 total (matching base+extras).
        for i in range(4):
            m = make_user(plan="free", email=f"rs-mem-{i}@x.com")
            m.team_owner_id = u.id
        db.session.commit()

        r = _logged_in(u).post(
            "/billing/release-seat", follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(u)
        # Refused.
        assert int(u.extra_seats) == 2


# ---------------------------------------------------------------------------
# Anonymous access
# ---------------------------------------------------------------------------

class TestAnonymousAccess:
    """All four routes should redirect anonymous users to /login —
    not 500, not silently no-op."""

    def test_buy_workspace_redirects_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.post("/billing/buy-workspace", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_buy_seat_redirects_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.post("/billing/buy-seat", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_release_workspace_redirects_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.post("/billing/release-workspace", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_release_seat_redirects_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.post("/billing/release-seat", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")
