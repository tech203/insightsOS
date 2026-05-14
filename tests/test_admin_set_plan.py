"""
Tests for /admin/users/<id>/plan — the admin plan-change form.

Behavior under test:
    1. Downgrades (growth → pro, pro → free, etc.) route through
       downgrade_plan() so workspaces over the new cap are soft-
       locked, addons are canceled in Stripe, and an audit row is
       written. Without this routing, admins downgrading users
       manually leaked Growth-tier perks indefinitely.
    2. Upgrades just flip the plan column. No state cleanup is
       needed because the user already had whatever they had.
    3. Same-plan POSTs are no-ops with an informational flash.
    4. Unknown plan slugs are rejected (existing behavior preserved).
    5. Access control: non-admin → 403, anonymous → redirect.
"""

from __future__ import annotations

from app import Client, CreditTransaction, db
from app import app as flask_app


def _admin_client(admin):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(admin.id)
        s["_fresh"] = True
    return c


def _make_workspaces(user, n):
    """Helper: create `n` unlocked workspaces in chronological order
    so soft-lock-by-age tests can predict which ones get locked."""
    out = []
    for i in range(n):
        ws = Client(
            slug=f"ws-{user.id}-{i}",
            user_id=user.id,
            name=f"WS {i}",
            website=f"https://ws-{i}.example.com",
            website_normalized=f"ws-{i}.example.com",
            industry="SaaS",
            location="SG",
        )
        db.session.add(ws)
        out.append(ws)
    db.session.commit()
    # Refresh so created_at ordering is locked in.
    for ws in out:
        db.session.refresh(ws)
    return out


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestAccessControl:
    def test_anonymous_redirected(self, app_ctx, make_user):
        target = make_user(email="acl-target-1@x.com")
        c = flask_app.test_client()
        r = c.post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "pro"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_non_admin_gets_403(self, app_ctx, make_user):
        regular = make_user(role="user", email="acl-user@x.com")
        target = make_user(email="acl-target-2@x.com")
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(regular.id)
            s["_fresh"] = True
        r = c.post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "pro"},
            follow_redirects=False,
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Downgrade routing
# ---------------------------------------------------------------------------

class TestDowngradeRouting:
    """A downgrade through admin must reconcile state the same way
    a Stripe-driven cancellation does. The smoking-gun behavior is
    workspaces-over-the-new-cap getting soft-locked: that's the
    visible signal that downgrade_plan() ran."""

    def test_growth_to_pro_soft_locks_over_cap_workspaces(
        self, app_ctx, make_user,
    ):
        admin = make_user(role="admin", email="dg-admin-1@x.com")
        target = make_user(plan="growth", email="dg-growth-target@x.com")
        # Growth lets 10 workspaces; Pro caps at 3. Create 5
        # workspaces so the downgrade locks 2.
        workspaces = _make_workspaces(target, 5)

        r = _admin_client(admin).post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "pro"},
            follow_redirects=False,
        )
        assert r.status_code == 302

        db.session.refresh(target)
        assert target.plan == "pro"

        # Workspaces are soft-locked oldest-stays. Of the 5 we made,
        # 3 should stay unlocked (the first 3 by created_at) and 2
        # should be locked.
        unlocked = [ws for ws in workspaces if not ws.is_locked]
        locked = [ws for ws in workspaces if ws.is_locked]
        for ws in workspaces:
            db.session.refresh(ws)
        assert sum(1 for ws in workspaces if not ws.is_locked) == 3
        assert sum(1 for ws in workspaces if ws.is_locked) == 2
        # Existence assertions (refreshing the fresh references):
        del unlocked, locked

    def test_pro_to_free_locks_all_but_one(self, app_ctx, make_user):
        admin = make_user(role="admin", email="dg-admin-2@x.com")
        target = make_user(plan="pro", email="dg-pro-target@x.com")
        # Pro caps at 3; Free caps at 1. Make 3, expect 2 locked.
        workspaces = _make_workspaces(target, 3)

        _admin_client(admin).post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "free"},
        )

        for ws in workspaces:
            db.session.refresh(ws)
        assert sum(1 for ws in workspaces if ws.is_locked) == 2
        assert sum(1 for ws in workspaces if not ws.is_locked) == 1

    def test_downgrade_writes_audit_row(self, app_ctx, make_user):
        """downgrade_plan() emits a `plan_downgrade` CreditTransaction
        so support can answer 'when did this user's plan change' from
        the activity log."""
        admin = make_user(role="admin", email="dg-admin-3@x.com")
        target = make_user(plan="growth", email="dg-audit-target@x.com")

        _admin_client(admin).post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "free"},
        )

        audit = (
            CreditTransaction.query
            .filter_by(user_id=target.id, type="plan_downgrade")
            .order_by(CreditTransaction.id.desc())
            .first()
        )
        assert audit is not None
        assert "growth" in (audit.notes or "")
        assert "free" in (audit.notes or "")

    def test_downgrade_clears_addon_counters(self, app_ctx, make_user):
        """extra_workspaces / extra_seats on the user row should reset
        to 0 on downgrade — these are 'addons we expect to be billing
        for' and the downgrade releases that expectation."""
        admin = make_user(role="admin", email="dg-admin-4@x.com")
        target = make_user(plan="growth", email="dg-addons@x.com")
        target.extra_workspaces = 4
        target.extra_seats = 2
        db.session.commit()

        _admin_client(admin).post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "free"},
        )

        db.session.refresh(target)
        assert target.extra_workspaces == 0
        assert target.extra_seats == 0


# ---------------------------------------------------------------------------
# Upgrade path — no state cleanup
# ---------------------------------------------------------------------------

class TestUpgradePath:
    def test_free_to_pro_just_flips_plan(self, app_ctx, make_user):
        """Upgrades should NOT call downgrade_plan() — there's nothing
        to clean up. The plan column flips and that's it. No
        plan_downgrade audit row should appear."""
        admin = make_user(role="admin", email="up-admin-1@x.com")
        target = make_user(plan="free", email="up-target-1@x.com")

        r = _admin_client(admin).post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "pro"},
            follow_redirects=False,
        )
        assert r.status_code == 302

        db.session.refresh(target)
        assert target.plan == "pro"

        # No downgrade audit row (the upgrade-path doesn't write one).
        audit = (
            CreditTransaction.query
            .filter_by(user_id=target.id, type="plan_downgrade")
            .first()
        )
        assert audit is None

    def test_pro_to_growth_just_flips_plan(self, app_ctx, make_user):
        admin = make_user(role="admin", email="up-admin-2@x.com")
        target = make_user(plan="pro", email="up-target-2@x.com")

        # Pre-existing workspaces should stay unlocked on upgrade.
        workspaces = _make_workspaces(target, 2)

        _admin_client(admin).post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "growth"},
        )

        db.session.refresh(target)
        assert target.plan == "growth"
        for ws in workspaces:
            db.session.refresh(ws)
            assert ws.is_locked is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_unknown_plan_rejected(self, app_ctx, make_user):
        admin = make_user(role="admin", email="ec-admin-1@x.com")
        target = make_user(plan="pro", email="ec-target-1@x.com")

        r = _admin_client(admin).post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "banana"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(target)
        # Plan unchanged.
        assert target.plan == "pro"

    def test_internal_tier_accepted(self, app_ctx, make_user):
        """Internal/comp tiers (starter, agency, dev_unlimited) aren't
        in the public PLAN_CATALOG but admins still need to assign
        them. The route should accept them via the _PLAN_RANK table."""
        admin = make_user(role="admin", email="ec-admin-2@x.com")
        target = make_user(plan="pro", email="ec-target-2@x.com")

        _admin_client(admin).post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "agency"},
        )

        db.session.refresh(target)
        assert target.plan == "agency"

    def test_same_plan_is_noop(self, app_ctx, make_user):
        """Posting the user's current plan back is a harmless no-op.
        downgrade_plan() must not fire (it would still write a
        useless audit row)."""
        admin = make_user(role="admin", email="ec-admin-3@x.com")
        target = make_user(plan="pro", email="ec-target-3@x.com")
        target.extra_workspaces = 2
        db.session.commit()

        _admin_client(admin).post(
            f"/admin/users/{target.id}/plan",
            data={"plan": "pro"},
        )

        db.session.refresh(target)
        assert target.plan == "pro"
        # extra_workspaces must NOT have been reset — that would be
        # the smoking-gun signal that downgrade_plan() fired.
        assert target.extra_workspaces == 2
        audit = (
            CreditTransaction.query
            .filter_by(user_id=target.id, type="plan_downgrade")
            .first()
        )
        assert audit is None
