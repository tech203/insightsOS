"""
Tests for the two-phase credit reservation system introduced in PR #85.

The contract being tested:
    1. reserve_credits_for(user, action) debits the wallet AND inserts
       a CreditReservation row in one commit. Returns None when the
       wallet doesn't have the funds.
    2. commit_reservation(row) marks the row committed and logs a
       CreditTransaction. Idempotent — second commit is a no-op.
    3. release_reservation(row) refunds the wallet and marks the row
       released. Also idempotent.
    4. sweep_expired_reservations() releases pending rows past their
       expires_at — the worker-kill safety net.
    5. Unlimited users (admin / dev_unlimited) get a zero-amount
       sentinel row so call sites don't have to branch.
    6. Team members spend from the owner's wallet, not their own.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from dtutils import utcnow

import pytest

import app as app_module
from app import (
    CreditReservation,
    CreditTransaction,
    User,
    Wallet,
    commit_reservation,
    release_reservation,
    reserve_credits,
    reserve_credits_for,
    sweep_expired_reservations,
    db,
)


# ---------------------------------------------------------------------------
# Reserve
# ---------------------------------------------------------------------------

class TestReserveCredits:
    def test_reserve_debits_wallet_immediately(self, user):
        """reserve takes the credits out of the wallet up front so
        concurrent reserves see correct headroom."""
        before = user.wallet.balance
        row = reserve_credits_for(user, "audit_run")

        assert row is not None
        assert row.status == "pending"
        assert row.amount == 1  # ACTION_CREDIT_COSTS["audit_run"]
        db.session.refresh(user.wallet)
        assert user.wallet.balance == before - 1

    def test_reserve_writes_reservation_row(self, user):
        row = reserve_credits_for(user, "content_brief", notes="my note")
        assert row.id is not None
        assert row.user_id == user.id
        assert row.action_key == "content_brief"
        assert row.notes == "my note"
        # 15-minute TTL by default
        assert row.expires_at > utcnow() + timedelta(minutes=14)
        assert row.expires_at < utcnow() + timedelta(minutes=16)

    def test_reserve_returns_none_when_insufficient_funds(self, make_user):
        broke = make_user(balance=0)
        row = reserve_credits_for(broke, "audit_run")
        assert row is None
        # Wallet untouched
        db.session.refresh(broke.wallet)
        assert broke.wallet.balance == 0

    def test_reserve_returns_none_when_partial_funds(self, make_user):
        """A 1-credit balance can't fund a 2-credit action."""
        u = make_user(balance=1)
        row = reserve_credits_for(u, "content_draft")  # costs 2
        assert row is None
        db.session.refresh(u.wallet)
        assert u.wallet.balance == 1  # untouched

    def test_reserve_logs_transaction(self, user):
        before = CreditTransaction.query.filter_by(user_id=user.id).count()
        reserve_credits_for(user, "audit_run")
        after = CreditTransaction.query.filter_by(user_id=user.id).count()
        assert after == before + 1
        # Most recent tx should be the reserve entry
        tx = (
            CreditTransaction.query.filter_by(user_id=user.id)
            .order_by(CreditTransaction.id.desc())
            .first()
        )
        assert tx.type == "reserve_audit_run"
        assert tx.amount == -1

    def test_zero_cost_action_returns_sentinel(self, user):
        """No-cost actions skip the wallet entirely but still return
        a row the caller can pass to commit / release uniformly."""
        before = user.wallet.balance
        row = reserve_credits(user, 0, "free_action")
        assert row is not None
        assert row.amount == 0
        assert row.status == "pending"
        db.session.refresh(user.wallet)
        assert user.wallet.balance == before  # untouched

    def test_unlimited_user_gets_sentinel(self, make_user):
        """Admins / dev_unlimited never burn credits; reserve returns
        a sentinel row so call sites don't have to special-case."""
        admin = make_user(plan="dev_unlimited", balance=0)
        row = reserve_credits_for(admin, "audit_run")
        assert row is not None
        assert row.amount == 0
        # Wallet stays at 0 — no debit, no error
        db.session.refresh(admin.wallet)
        assert admin.wallet.balance == 0


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

class TestCommitReservation:
    def test_commit_marks_row_committed(self, user):
        row = reserve_credits_for(user, "audit_run")
        ok = commit_reservation(row, notes="audit ran fine")
        assert ok is True

        db.session.refresh(row)
        assert row.status == "committed"
        assert row.finalized_at is not None

    def test_commit_logs_usage_transaction(self, user):
        reserve_credits_for(user, "audit_run")
        # Reset count baseline post-reserve
        before = CreditTransaction.query.filter_by(
            user_id=user.id, type="usage_audit_run"
        ).count()
        # Reserve a second one and commit it
        row = reserve_credits_for(user, "audit_run")
        commit_reservation(row, notes="done")
        after = CreditTransaction.query.filter_by(
            user_id=user.id, type="usage_audit_run"
        ).count()
        assert after == before + 1

    def test_commit_is_idempotent(self, user):
        row = reserve_credits_for(user, "audit_run")
        assert commit_reservation(row) is True
        # Second commit returns False but doesn't crash or double-log
        before_count = CreditTransaction.query.filter_by(
            user_id=user.id, type="usage_audit_run"
        ).count()
        assert commit_reservation(row) is False
        after_count = CreditTransaction.query.filter_by(
            user_id=user.id, type="usage_audit_run"
        ).count()
        assert after_count == before_count

    def test_commit_after_release_is_noop(self, user):
        """If a route accidentally calls both commit and release, the
        second wins-then-loses gracefully — no double-debit or crash."""
        row = reserve_credits_for(user, "audit_run")
        release_reservation(row)
        assert commit_reservation(row) is False
        db.session.refresh(row)
        assert row.status == "released"  # still released

    def test_commit_none_returns_false(self, app_ctx):
        assert commit_reservation(None) is False


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------

class TestReleaseReservation:
    def test_release_refunds_wallet(self, user):
        before = user.wallet.balance
        row = reserve_credits_for(user, "audit_run")
        # Wallet is down by 1 after reserve
        db.session.refresh(user.wallet)
        assert user.wallet.balance == before - 1

        release_reservation(row, reason="action failed")
        db.session.refresh(user.wallet)
        assert user.wallet.balance == before  # fully refunded

    def test_release_marks_row_released(self, user):
        row = reserve_credits_for(user, "audit_run")
        release_reservation(row, reason="bad input")
        db.session.refresh(row)
        assert row.status == "released"
        assert row.finalized_at is not None
        assert "bad input" in (row.notes or "")

    def test_release_is_idempotent(self, user):
        row = reserve_credits_for(user, "audit_run")
        before = user.wallet.balance  # post-reserve balance
        db.session.refresh(user.wallet)
        before = user.wallet.balance

        assert release_reservation(row) is True
        db.session.refresh(user.wallet)
        balance_after_first = user.wallet.balance

        # Second release returns False; wallet not double-credited
        assert release_reservation(row) is False
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_after_first

    def test_release_none_returns_false(self, app_ctx):
        assert release_reservation(None) is False


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

class TestSweepExpiredReservations:
    def _make_stale(self, user, *, action="audit_run", minutes_ago=20):
        """Insert a pending reservation with a past expires_at,
        bypassing the helper which would set a fresh expiry."""
        # Drain the wallet by 1 to mimic what reserve_credits_for did.
        user.wallet.balance -= 1
        row = CreditReservation(
            user_id=user.id,
            amount=1,
            action_key=action,
            status="pending",
            expires_at=utcnow() - timedelta(minutes=minutes_ago),
        )
        db.session.add(row)
        db.session.commit()
        return row

    def test_sweep_releases_expired_pending(self, user, monkeypatch):
        # Reset the per-process throttle so the sweep actually runs.
        monkeypatch.setattr(app_module, "_last_reservation_sweep_at", None)

        before = user.wallet.balance
        stale = self._make_stale(user)
        # Balance is now 1 less (we simulated the debit in _make_stale)
        db.session.refresh(user.wallet)
        assert user.wallet.balance == before - 1

        swept = sweep_expired_reservations()
        assert swept >= 1

        db.session.refresh(stale)
        assert stale.status == "released"
        db.session.refresh(user.wallet)
        assert user.wallet.balance == before  # refunded

    def test_sweep_ignores_committed_rows(self, user, monkeypatch):
        monkeypatch.setattr(app_module, "_last_reservation_sweep_at", None)
        row = reserve_credits_for(user, "audit_run")
        commit_reservation(row)
        # Manually expire it
        row.expires_at = utcnow() - timedelta(minutes=20)
        db.session.commit()

        balance_before = user.wallet.balance
        sweep_expired_reservations()
        db.session.refresh(row)
        # Status unchanged, wallet unchanged
        assert row.status == "committed"
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

    def test_sweep_ignores_already_released(self, user, monkeypatch):
        monkeypatch.setattr(app_module, "_last_reservation_sweep_at", None)
        row = reserve_credits_for(user, "audit_run")
        release_reservation(row)
        row.expires_at = utcnow() - timedelta(minutes=20)
        db.session.commit()

        balance_before = user.wallet.balance
        sweep_expired_reservations()
        db.session.refresh(row)
        assert row.status == "released"
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before  # no double-refund

    def test_sweep_leaves_non_expired_alone(self, user, monkeypatch):
        """A reservation that's still within its TTL should not be
        swept even on a forced sweep."""
        monkeypatch.setattr(app_module, "_last_reservation_sweep_at", None)
        row = reserve_credits_for(user, "audit_run")
        # expires_at is ~15 min in the future by default
        sweep_expired_reservations()
        db.session.refresh(row)
        assert row.status == "pending"  # not touched

    def test_sweep_throttle(self, user, monkeypatch):
        """The sweeper is throttled so we don't hammer the DB on every
        before_request. Two successive calls should run at most once
        unless we force the timestamp back."""
        monkeypatch.setattr(app_module, "_last_reservation_sweep_at", None)
        self._make_stale(user, minutes_ago=20)

        first = sweep_expired_reservations()
        assert first >= 1

        # Second call within the throttle window returns 0 without
        # touching the DB. (We don't assert on side effects since
        # nothing's expired; we just assert the early return shape.)
        self._make_stale(user, minutes_ago=20)
        second = sweep_expired_reservations()
        assert second == 0


# ---------------------------------------------------------------------------
# Team member spending — bills come out of the owner's wallet
# ---------------------------------------------------------------------------

class TestTeamMemberWallet:
    def test_team_member_reserves_from_owner_wallet(self, make_user):
        owner = make_user(balance=10, email="owner@x.com")
        member = make_user(balance=0, email="member@x.com")
        # Wire the member as an owned team account
        member.team_owner_id = owner.id
        db.session.commit()

        row = reserve_credits_for(member, "audit_run")
        assert row is not None
        # The reservation row is attributed to the OWNER (that's whose
        # wallet was debited).
        assert row.user_id == owner.id
        db.session.refresh(owner.wallet)
        assert owner.wallet.balance == 9

        # Member's own wallet untouched.
        db.session.refresh(member.wallet)
        assert member.wallet.balance == 0

    def test_team_member_refused_when_owner_broke(self, make_user):
        owner = make_user(balance=0, email="owner2@x.com")
        member = make_user(balance=100, email="member2@x.com")  # member's
        member.team_owner_id = owner.id
        db.session.commit()

        # Even though the member has 100 credits, billing follows the
        # team owner — and the owner is broke.
        row = reserve_credits_for(member, "audit_run")
        assert row is None
