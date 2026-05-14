"""
Tests for downgrade_plan() and reactivate_workspace() (PR #85).

Behavior under test:
- Cancels addon Stripe subscriptions (via cancel_at_period_end). The
  Stripe API call is patched out — we assert we'd have made the
  cancel call, not that Stripe actually succeeds.
- Soft-locks over-cap workspaces (oldest stay active, newer ones
  get is_locked=True). Data preserved, listings exclude them by
  default.
- Resets extra_workspaces / extra_seats counters and clears the
  stripe_extra_*_sub_ids tracking lists.
- Revokes pending team invites when the new plan can't hold all
  current team members.
- Writes a 'plan_downgrade' CreditTransaction row for the audit log.
- reactivate_workspace refuses if reactivating would push the user
  back over cap; succeeds when there's room.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from app import (
    Client,
    CreditTransaction,
    TeamInvite,
    db,
    downgrade_plan,
    reactivate_workspace,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_workspace(user, *, slug, name=None, website=None):
    ws = Client(
        slug=slug,
        user_id=user.id,
        name=name or f"Workspace {slug}",
        website=website or f"https://{slug}.example.com",
        website_normalized=f"{slug}.example.com",
    )
    db.session.add(ws)
    db.session.flush()
    return ws


def _patch_stripe_disabled():
    """Patch Stripe to act as unconfigured so downgrade_plan exercises
    the local-cleanup path. Stripe-configured cases are tested by
    asserting the would-be cancel calls separately."""
    from services.stripe_helper import StripeNotConfigured
    return patch(
        "services.stripe_helper._stripe_module",
        side_effect=StripeNotConfigured("not configured in test"),
    )


# ---------------------------------------------------------------------------
# downgrade_plan
# ---------------------------------------------------------------------------

class TestDowngradePlan:
    def test_sets_user_plan(self, make_user):
        u = make_user(plan="growth")
        with _patch_stripe_disabled():
            summary = downgrade_plan(u, "pro")

        db.session.refresh(u)
        assert u.plan == "pro"
        assert summary["old_plan"] == "growth"
        assert summary["new_plan"] == "pro"

    def test_growth_to_pro_locks_excess_workspaces(self, make_user):
        """Growth allows 10 workspaces, Pro allows 3. Locking the
        oldest-first means the original 3 stay active, the 5 newest
        get locked."""
        u = make_user(plan="growth")
        for i in range(8):
            _make_workspace(u, slug=f"ws-{i}")
        db.session.commit()

        with _patch_stripe_disabled():
            summary = downgrade_plan(u, "pro")

        active = Client.query.filter_by(user_id=u.id, is_locked=False).count()
        locked = Client.query.filter_by(user_id=u.id, is_locked=True).count()
        assert active == 3
        assert locked == 5
        assert summary["workspaces_locked"] == 5

    def test_under_cap_locks_nothing(self, make_user):
        """If the user only has 2 workspaces on Growth and downgrades
        to Pro (cap 3), nothing should be locked."""
        u = make_user(plan="growth")
        for i in range(2):
            _make_workspace(u, slug=f"ws-{i}")
        db.session.commit()

        with _patch_stripe_disabled():
            summary = downgrade_plan(u, "pro")

        locked = Client.query.filter_by(user_id=u.id, is_locked=True).count()
        assert locked == 0
        assert summary["workspaces_locked"] == 0

    def test_resets_extra_workspaces_counter(self, make_user):
        u = make_user(plan="pro")
        u.extra_workspaces = 3
        u.stripe_extra_workspace_sub_ids = ["sub_w1", "sub_w2", "sub_w3"]
        db.session.commit()

        with _patch_stripe_disabled():
            downgrade_plan(u, "free")

        db.session.refresh(u)
        assert u.extra_workspaces == 0
        assert u.stripe_extra_workspace_sub_ids == []

    def test_resets_extra_seats_counter(self, make_user):
        u = make_user(plan="pro")
        u.extra_seats = 2
        u.stripe_extra_seat_sub_ids = ["sub_s1", "sub_s2"]
        db.session.commit()

        with _patch_stripe_disabled():
            downgrade_plan(u, "free")

        db.session.refresh(u)
        assert u.extra_seats == 0
        assert u.stripe_extra_seat_sub_ids == []

    def test_to_free_clears_subscription_id(self, make_user):
        u = make_user(plan="pro")
        u.stripe_subscription_id = "sub_legacy_plan"
        db.session.commit()

        with _patch_stripe_disabled():
            downgrade_plan(u, "free")

        db.session.refresh(u)
        assert u.stripe_subscription_id is None

    def test_revokes_pending_invites_when_over_seat_cap(self, make_user):
        """Free plan allows 1 seat. If the owner had 2 pending invites,
        both should be revoked on downgrade."""
        u = make_user(plan="pro")
        for i in range(2):
            db.session.add(TeamInvite(
                owner_user_id=u.id,
                email=f"inv{i}@x.com",
                token=f"tok_{i}",
                status="pending",
            ))
        db.session.commit()

        with _patch_stripe_disabled():
            summary = downgrade_plan(u, "free")

        # All pending invites should now be revoked
        pending = TeamInvite.query.filter_by(
            owner_user_id=u.id, status="pending"
        ).count()
        assert pending == 0
        revoked = TeamInvite.query.filter_by(
            owner_user_id=u.id, status="revoked"
        ).count()
        assert revoked == 2
        # Free seat cap is 1 (owner only), 3 total members > 1 = over cap
        assert summary["over_seat_cap"] is True

    def test_writes_audit_log_transaction(self, make_user):
        u = make_user(plan="pro")
        before = CreditTransaction.query.filter_by(
            user_id=u.id, type="plan_downgrade"
        ).count()

        with _patch_stripe_disabled():
            downgrade_plan(u, "free", reason="test reason")

        after = CreditTransaction.query.filter_by(
            user_id=u.id, type="plan_downgrade"
        ).count()
        assert after == before + 1

        tx = (
            CreditTransaction.query.filter_by(
                user_id=u.id, type="plan_downgrade"
            )
            .order_by(CreditTransaction.id.desc())
            .first()
        )
        assert "test reason" in (tx.notes or "")
        assert tx.amount == 0  # no credit change, audit only

    def test_calls_stripe_cancel_when_configured(self, make_user):
        """When Stripe is wired up, each tracked addon sub_id should
        get a stripe.Subscription.modify(cancel_at_period_end=True)
        call before we clear the local trackers."""
        u = make_user(plan="pro")
        u.stripe_extra_workspace_sub_ids = ["sub_w1", "sub_w2"]
        u.extra_workspaces = 2
        db.session.commit()

        # Mock the Stripe module so the cancel call doesn't actually
        # hit the API.
        fake_stripe = MagicMock()
        with patch("services.stripe_helper._stripe_module", return_value=fake_stripe):
            summary = downgrade_plan(u, "free")

        # We should have asked Stripe to cancel each addon sub.
        modify_calls = fake_stripe.Subscription.modify.call_args_list
        sub_ids_called = [args[0][0] for args in modify_calls]
        assert set(sub_ids_called) == {"sub_w1", "sub_w2"}
        # Each call should have used cancel_at_period_end
        for call in modify_calls:
            assert call.kwargs.get("cancel_at_period_end") is True
        assert summary["addons_canceled"] == 2

    def test_stripe_cancel_failure_is_logged_not_fatal(self, make_user):
        """If one addon cancel fails (Stripe 404, network blip, etc.),
        downgrade_plan should record the error and continue — not
        roll back the whole downgrade."""
        u = make_user(plan="pro")
        u.stripe_extra_workspace_sub_ids = ["sub_bad", "sub_good"]
        u.extra_workspaces = 2
        db.session.commit()

        fake_stripe = MagicMock()
        # First call raises, second succeeds
        fake_stripe.Subscription.modify.side_effect = [
            Exception("Stripe 404"),
            None,
        ]
        with patch("services.stripe_helper._stripe_module", return_value=fake_stripe):
            summary = downgrade_plan(u, "free")

        # One succeeded, one error recorded; downgrade itself still
        # completed.
        assert summary["addons_canceled"] == 1
        assert len(summary["errors"]) == 1
        assert "sub_bad" in summary["errors"][0]
        db.session.refresh(u)
        assert u.plan == "free"

    def test_none_user_returns_error(self, app_ctx):
        out = downgrade_plan(None, "free")
        assert out.get("error") == "no_user"


# ---------------------------------------------------------------------------
# reactivate_workspace
# ---------------------------------------------------------------------------

class TestReactivateWorkspace:
    def _setup_locked(self, u):
        """Create a Pro-plan user with 3 active + 2 locked workspaces."""
        actives = []
        locked = []
        for i in range(3):
            ws = _make_workspace(u, slug=f"active-{i}")
            actives.append(ws)
        for i in range(2):
            ws = _make_workspace(u, slug=f"locked-{i}")
            ws.is_locked = True
            locked.append(ws)
        db.session.commit()
        return actives, locked

    def test_refuses_when_at_cap(self, make_user):
        u = make_user(plan="pro")  # cap 3
        _, locked = self._setup_locked(u)

        # 3 active already = at cap. Reactivating should fail.
        assert reactivate_workspace(u, locked[0].id) is False
        db.session.refresh(locked[0])
        assert locked[0].is_locked is True  # untouched

    def test_succeeds_when_slot_frees_up(self, make_user):
        u = make_user(plan="pro")
        actives, locked = self._setup_locked(u)

        # Delete one active to free a slot
        db.session.delete(actives[0])
        db.session.commit()

        assert reactivate_workspace(u, locked[0].id) is True
        db.session.refresh(locked[0])
        assert locked[0].is_locked is False

    def test_refuses_unknown_workspace(self, make_user):
        u = make_user(plan="pro")
        assert reactivate_workspace(u, 999_999) is False

    def test_refuses_already_unlocked(self, make_user):
        u = make_user(plan="pro")
        ws = _make_workspace(u, slug="already-active")
        db.session.commit()
        # Not locked — there's nothing to reactivate.
        assert reactivate_workspace(u, ws.id) is False

    def test_refuses_workspace_owned_by_other_user(self, make_user):
        u1 = make_user(plan="pro", email="u1@x.com")
        u2 = make_user(plan="pro", email="u2@x.com")
        ws = _make_workspace(u2, slug="u2-ws")
        ws.is_locked = True
        db.session.commit()
        # u1 can't reactivate u2's workspace.
        assert reactivate_workspace(u1, ws.id) is False

    def test_none_user_returns_false(self, app_ctx):
        assert reactivate_workspace(None, 1) is False
