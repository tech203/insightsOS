"""
Tests for POST /admin/users/<id>/grant-credits.

Admin-only manual credit adjustment. Financial + audit-relevant —
every grant writes a CreditTransaction(type="admin_grant") row and
mutates the wallet balance. Previously only smoke-tested.

Behaviour locked down:
  - Access control: anonymous → /login (302), non-admin → 403
  - Positive grant: wallet += amount, audit row written with the
    post-grant balance
  - Negative grant (clawback): wallet can go down; balance_after
    on the audit row reflects it
  - Zero / non-integer amount: no-op, no audit row, no 500
  - Wallet auto-created when the target user has none
  - Unknown user → 404
  - balance_after on the CreditTransaction is the balance AFTER
    applying the delta (not before) — this is what the admin
    activity log shows, so it must be correct
"""

from __future__ import annotations

from app import CreditTransaction, Wallet, db
from app import app as flask_app


def _client(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _grant(admin, target, **form):
    return _client(admin).post(
        f"/admin/users/{target.id}/grant-credits",
        data=form,
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestAccessControl:

    def test_anonymous_redirected_to_login(self, app_ctx, make_user):
        target = make_user(plan="free", email="gc-anon-target@x.com")
        r = flask_app.test_client().post(
            f"/admin/users/{target.id}/grant-credits",
            data={"amount": "10"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_non_admin_forbidden(self, make_user):
        regular = make_user(plan="pro", email="gc-regular@x.com")
        target = make_user(plan="free", email="gc-na-target@x.com")
        start = target.wallet.balance
        r = _grant(regular, target, amount="50")
        assert r.status_code == 403
        db.session.refresh(target.wallet)
        assert target.wallet.balance == start  # untouched

    def test_unknown_user_404(self, make_user):
        admin = make_user(role="admin", email="gc-admin-404@x.com")
        r = _client(admin).post(
            "/admin/users/999999/grant-credits",
            data={"amount": "10"},
            follow_redirects=False,
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Grant behaviour
# ---------------------------------------------------------------------------

class TestGrantBehaviour:

    def test_positive_grant_adds_and_audits(self, make_user):
        admin = make_user(role="admin", email="gc-pos-admin@x.com")
        target = make_user(plan="free", email="gc-pos-target@x.com")
        start = target.wallet.balance

        r = _grant(admin, target, amount="25", note="welcome bonus")
        assert r.status_code == 302
        db.session.refresh(target.wallet)
        assert target.wallet.balance == start + 25

        tx = (
            CreditTransaction.query
            .filter_by(user_id=target.id, type="admin_grant")
            .order_by(CreditTransaction.id.desc())
            .first()
        )
        assert tx is not None
        assert tx.amount == 25
        assert tx.notes == "welcome bonus"
        # balance_after is the POST-grant balance — this is what the
        # admin activity log renders.
        assert tx.balance_after == start + 25

    def test_negative_grant_is_a_clawback(self, make_user):
        """Admins can subtract credits (refund reversal, abuse
        clawback). Amount is signed; wallet goes down; the audit
        row records the negative delta + the reduced balance."""
        admin = make_user(role="admin", email="gc-neg-admin@x.com")
        target = make_user(plan="pro", balance=100, email="gc-neg-target@x.com")

        r = _grant(admin, target, amount="-30", note="chargeback reversal")
        assert r.status_code == 302
        db.session.refresh(target.wallet)
        assert target.wallet.balance == 70

        tx = (
            CreditTransaction.query
            .filter_by(user_id=target.id, type="admin_grant")
            .order_by(CreditTransaction.id.desc())
            .first()
        )
        assert tx.amount == -30
        assert tx.balance_after == 70

    def test_default_note_when_blank(self, make_user):
        admin = make_user(role="admin", email="gc-note-admin@x.com")
        target = make_user(plan="free", email="gc-note-target@x.com")
        _grant(admin, target, amount="5")  # no note
        tx = (
            CreditTransaction.query
            .filter_by(user_id=target.id, type="admin_grant")
            .order_by(CreditTransaction.id.desc())
            .first()
        )
        assert tx.notes == "Admin grant"

    def test_zero_amount_is_noop(self, make_user):
        admin = make_user(role="admin", email="gc-zero-admin@x.com")
        target = make_user(plan="free", email="gc-zero-target@x.com")
        start = target.wallet.balance

        r = _grant(admin, target, amount="0")
        assert r.status_code == 302
        db.session.refresh(target.wallet)
        assert target.wallet.balance == start
        # No admin_grant row written for a zero adjustment.
        assert CreditTransaction.query.filter_by(
            user_id=target.id, type="admin_grant",
        ).first() is None

    def test_non_integer_amount_is_rejected(self, make_user):
        admin = make_user(role="admin", email="gc-bad-admin@x.com")
        target = make_user(plan="free", email="gc-bad-target@x.com")
        start = target.wallet.balance

        r = _grant(admin, target, amount="ten")
        assert r.status_code == 302  # flash + redirect, not 500
        db.session.refresh(target.wallet)
        assert target.wallet.balance == start
        assert CreditTransaction.query.filter_by(
            user_id=target.id, type="admin_grant",
        ).first() is None

    def test_missing_amount_treated_as_zero(self, make_user):
        admin = make_user(role="admin", email="gc-noamt-admin@x.com")
        target = make_user(plan="free", email="gc-noamt-target@x.com")
        start = target.wallet.balance
        r = _grant(admin, target)  # no amount at all
        assert r.status_code == 302
        db.session.refresh(target.wallet)
        assert target.wallet.balance == start

    def test_wallet_autocreated_when_missing(self, make_user):
        """make_user always attaches a wallet, so simulate the
        no-wallet case by deleting it first. The route should
        create a zero-balance wallet then apply the grant."""
        admin = make_user(role="admin", email="gc-nowallet-admin@x.com")
        target = make_user(plan="free", email="gc-nowallet-target@x.com")
        Wallet.query.filter_by(user_id=target.id).delete()
        db.session.commit()
        # Confirm precondition.
        assert Wallet.query.filter_by(user_id=target.id).first() is None

        r = _grant(admin, target, amount="40")
        assert r.status_code == 302
        w = Wallet.query.filter_by(user_id=target.id).first()
        assert w is not None
        assert w.balance == 40

    def test_repeated_grants_accumulate(self, make_user):
        """Two sequential grants stack, and each audit row's
        balance_after reflects the running total — important for
        the activity-log reconciliation view."""
        admin = make_user(role="admin", email="gc-acc-admin@x.com")
        target = make_user(plan="free", email="gc-acc-target@x.com")
        start = target.wallet.balance

        _grant(admin, target, amount="10")
        _grant(admin, target, amount="15")
        db.session.refresh(target.wallet)
        assert target.wallet.balance == start + 25

        rows = (
            CreditTransaction.query
            .filter_by(user_id=target.id, type="admin_grant")
            .order_by(CreditTransaction.id.asc())
            .all()
        )
        assert len(rows) == 2
        assert rows[0].balance_after == start + 10
        assert rows[1].balance_after == start + 25
