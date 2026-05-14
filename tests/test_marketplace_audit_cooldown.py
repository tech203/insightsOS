"""
Tests for the Free-tier marketplace audit cooldown.

Marketplace audits aren't plan-gated (a Free user can pay
$2/credit × 2 credits to run one — consistent with the pay-per-use
Free model). But they're rate-limited to once per 30 days per shop
on Free, to protect:

  - server-side cost (each run hits ~5 AI engine queries)
  - pricing integrity (the Action Plan module is $19/mo; pay-per-
    credit shouldn't let Free users grind out a paid feature daily)

Paid plans (pro/growth/agency/starter) and admin/dev_unlimited
bypass the cooldown — they pay credits per run; the cost is
bounded by their wallet.

The cooldown is per-presence: a user with three linked shops gets
three separate 30-day windows, not a global limit.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from app import (
    Client,
    CreditReservation,
    MARKETPLACE_AUDIT_FREE_COOLDOWN_DAYS,
    MarketplacePresence,
    db,
)
from app import app as flask_app
from dtutils import utcnow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace_for(make_user):
    """Factory: return (user, workspace) for a given plan."""
    def _make(plan, *, email, role="user"):
        u = make_user(plan=plan, email=email, role=role, balance=10)
        ws = Client(
            slug=f"mp-ws-{u.id}",
            user_id=u.id,
            name="Marketplace Test",
            website="https://mp.example.com",
            website_normalized="mp.example.com",
            industry="Retail",
            location="SG",
        )
        db.session.add(ws)
        db.session.commit()
        return u, ws
    return _make


@pytest.fixture
def presence_for():
    """Factory: return a MarketplacePresence row for (user, workspace),
    optionally pre-aged with `last_audited_at = now - days_ago`."""
    def _make(user, workspace, *, last_audited_days_ago=None):
        last_at = (
            utcnow() - timedelta(days=last_audited_days_ago)
            if last_audited_days_ago is not None else None
        )
        p = MarketplacePresence(
            user_id=user.id,
            client_id=workspace.id,
            marketplace="etsy",
            shop_name="Test Shop",
            shop_url="https://www.etsy.com/shop/TestShop",
            category="ceramics",
            region="US",
            last_audited_at=last_at,
            last_visibility_score=(40 if last_at else None),
            last_audit_payload=({"visibility_score": 40} if last_at else None),
        )
        db.session.add(p)
        db.session.commit()
        return p
    return _make


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _mock_audit_payload(visibility_score=50):
    return {
        "visibility_score": visibility_score,
        "queries": [
            {"query": "best etsy ceramic shops", "cited": True},
            {"query": "trusted etsy ceramic sellers", "cited": False},
        ],
    }


# ---------------------------------------------------------------------------
# Free tier — cooldown enforced
# ---------------------------------------------------------------------------

class TestFreeCooldown:
    """Free users hit the rate limit; the gate fires before credits
    are reserved and before any AI engine is queried."""

    def test_first_audit_succeeds(self, workspace_for, presence_for):
        """A Free user with no prior audit on this shop can run once."""
        u, ws = workspace_for("free", email="mp-free-first@x.com")
        p = presence_for(u, ws, last_audited_days_ago=None)
        balance_before = u.wallet.balance

        with patch(
            "services.marketplace_audit.run_marketplace_audit",
            return_value=_mock_audit_payload(),
        ), patch("ai_answer_agent.enabled_engines", return_value=["chatgpt"]):
            r = _logged_in(u).post(
                f"/marketplace-audits/{ws.id}/run/{p.id}",
                follow_redirects=False,
            )

        assert r.status_code == 302
        # Credits debited normally (2 for marketplace_audit).
        db.session.refresh(u.wallet)
        assert u.wallet.balance == balance_before - 2
        db.session.refresh(p)
        assert p.last_audited_at is not None

    def test_second_audit_within_cooldown_blocked(
        self, workspace_for, presence_for,
    ):
        """A Free user trying to re-run the same shop inside the
        30-day window: bounce, no credit reserved, no AI call."""
        u, ws = workspace_for("free", email="mp-free-cooldown@x.com")
        # Last audit was 10 days ago — well inside the 30-day window.
        p = presence_for(u, ws, last_audited_days_ago=10)
        balance_before = u.wallet.balance

        ran_audit = {"n": 0}

        def _spy(*a, **kw):
            ran_audit["n"] += 1
            return _mock_audit_payload()

        with patch(
            "services.marketplace_audit.run_marketplace_audit",
            side_effect=_spy,
        ):
            r = _logged_in(u).post(
                f"/marketplace-audits/{ws.id}/run/{p.id}",
                follow_redirects=False,
            )

        assert r.status_code == 302
        # Wallet untouched.
        db.session.refresh(u.wallet)
        assert u.wallet.balance == balance_before
        # No reservation row created (the gate fires before reservation).
        assert CreditReservation.query.filter_by(
            user_id=u.id, action_key="marketplace_audit",
        ).first() is None
        # AI engine never called.
        assert ran_audit["n"] == 0

    def test_audit_outside_cooldown_succeeds(
        self, workspace_for, presence_for,
    ):
        """Free user re-running 31 days after the previous audit: ok."""
        u, ws = workspace_for("free", email="mp-free-expired@x.com")
        # Pre-age the audit just past the cooldown window.
        p = presence_for(
            u, ws,
            last_audited_days_ago=MARKETPLACE_AUDIT_FREE_COOLDOWN_DAYS + 1,
        )
        balance_before = u.wallet.balance

        with patch(
            "services.marketplace_audit.run_marketplace_audit",
            return_value=_mock_audit_payload(),
        ), patch("ai_answer_agent.enabled_engines", return_value=["chatgpt"]):
            r = _logged_in(u).post(
                f"/marketplace-audits/{ws.id}/run/{p.id}",
                follow_redirects=False,
            )

        assert r.status_code == 302
        db.session.refresh(u.wallet)
        assert u.wallet.balance == balance_before - 2

    def test_cooldown_is_per_presence(self, workspace_for, presence_for):
        """A Free user with TWO shops should be able to audit shop B
        even if shop A is still in its cooldown window. The cap is
        per-(user, presence), not per-user."""
        u, ws = workspace_for("free", email="mp-free-two-shops@x.com")
        # Shop A audited recently — in cooldown.
        p_a = presence_for(u, ws, last_audited_days_ago=5)
        # Shop B never audited — eligible.
        p_b = MarketplacePresence(
            user_id=u.id,
            client_id=ws.id,
            marketplace="amazon",
            shop_name="Other Shop",
            shop_url="https://www.amazon.com/shop/OtherShop",
            category="kitchen",
        )
        db.session.add(p_b)
        db.session.commit()

        balance_before = u.wallet.balance

        with patch(
            "services.marketplace_audit.run_marketplace_audit",
            return_value=_mock_audit_payload(),
        ), patch("ai_answer_agent.enabled_engines", return_value=["chatgpt"]):
            r = _logged_in(u).post(
                f"/marketplace-audits/{ws.id}/run/{p_b.id}",
                follow_redirects=False,
            )

        assert r.status_code == 302
        # Shop B audit went through.
        db.session.refresh(u.wallet)
        assert u.wallet.balance == balance_before - 2
        db.session.refresh(p_b)
        assert p_b.last_audited_at is not None
        # Shop A's cooldown is independent — no change to its audit row.
        db.session.refresh(p_a)
        # last_audited_at on shop A should NOT have been moved by the
        # shop B run (different presence row).
        assert (utcnow() - p_a.last_audited_at).days < 30


# ---------------------------------------------------------------------------
# Paid tiers — cooldown bypassed
# ---------------------------------------------------------------------------

class TestPaidTierBypass:
    """Paying plans pay credits per run — the cooldown doesn't apply
    to them. The cost is bounded by their wallet, not a calendar."""

    def test_pro_user_can_re_run_immediately(
        self, workspace_for, presence_for,
    ):
        u, ws = workspace_for("pro", email="mp-pro-rerun@x.com")
        # Audited 30 seconds ago — well inside any cooldown window.
        p = presence_for(u, ws, last_audited_days_ago=0)
        balance_before = u.wallet.balance

        with patch(
            "services.marketplace_audit.run_marketplace_audit",
            return_value=_mock_audit_payload(visibility_score=60),
        ), patch("ai_answer_agent.enabled_engines", return_value=["chatgpt"]):
            r = _logged_in(u).post(
                f"/marketplace-audits/{ws.id}/run/{p.id}",
                follow_redirects=False,
            )

        assert r.status_code == 302
        db.session.refresh(u.wallet)
        # 2 credits debited — the cap doesn't apply.
        assert u.wallet.balance == balance_before - 2

    def test_growth_user_can_re_run_immediately(
        self, workspace_for, presence_for,
    ):
        u, ws = workspace_for("growth", email="mp-growth-rerun@x.com")
        p = presence_for(u, ws, last_audited_days_ago=1)

        with patch(
            "services.marketplace_audit.run_marketplace_audit",
            return_value=_mock_audit_payload(),
        ), patch("ai_answer_agent.enabled_engines", return_value=["chatgpt"]):
            r = _logged_in(u).post(
                f"/marketplace-audits/{ws.id}/run/{p.id}",
                follow_redirects=False,
            )
        assert r.status_code == 302
        # Reservation was created and committed (cooldown bypassed).
        latest = (
            CreditReservation.query
            .filter_by(user_id=u.id, action_key="marketplace_audit")
            .order_by(CreditReservation.id.desc()).first()
        )
        assert latest is not None
        assert latest.status == "committed"

    def test_admin_user_bypasses_cooldown(
        self, workspace_for, presence_for,
    ):
        """Admins on a Free plan still bypass — same convention as
        every other gate in the app (admin = unlimited)."""
        u, ws = workspace_for("free", email="mp-admin@x.com", role="admin")
        p = presence_for(u, ws, last_audited_days_ago=2)

        with patch(
            "services.marketplace_audit.run_marketplace_audit",
            return_value=_mock_audit_payload(),
        ), patch("ai_answer_agent.enabled_engines", return_value=["chatgpt"]):
            r = _logged_in(u).post(
                f"/marketplace-audits/{ws.id}/run/{p.id}",
                follow_redirects=False,
            )
        assert r.status_code == 302
        # Audit ran — last_audited_at moved forward.
        db.session.refresh(p)
        assert (utcnow() - p.last_audited_at).total_seconds() < 60
