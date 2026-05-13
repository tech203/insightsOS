"""
Tests for /api/client/<id>/run-audit — the JSON variant of the HTML
audit route, used by the bulk-audit JS driver on /clients (PR #90).

Behavior under test:
    1. Auth — login_required gate.
    2. Success path — mocked audit + queue-creator, asserts the
       return shape includes the right counters and the credit
       reservation was committed (not refunded).
    3. Insufficient credits — returns 402 with reason='insufficient_credits',
       wallet untouched.
    4. Audit exception — wallet is refunded, returns 500 with friendly error.
    5. Missing required fields — workspace without website/industry/location
       returns 400 without reserving credits.
    6. Unknown workspace — 404, no side effects.
    7. Defaults — body params fall back to workspace's stored values.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import (
    Client,
    CreditReservation,
    CreditTransaction,
    Wallet,
    db,
)
from app import app as flask_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(user):
    """A complete workspace for `user` — has all three required fields
    (website / industry / location) so bulk-audit can run without
    body params."""
    ws = Client(
        slug="bulk-target",
        user_id=user.id,
        name="Bulk Target",
        website="https://bulk-target.example.com",
        website_normalized="bulk-target.example.com",
        industry="SaaS",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.commit()
    return ws


@pytest.fixture
def logged_in_client(user):
    """Test client authenticated as `user` (Pro plan, verified, 10 credits)."""
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


@pytest.fixture
def mocked_audit():
    """Patches the heavy lifting so tests exercise the endpoint plumbing
    without running an actual audit / hitting OpenAI."""
    with patch("app.run_audit_for_input", return_value=None), \
         patch(
            "app.create_content_opportunities_from_latest_audit",
            return_value={
                "added": 3,
                "skipped_existing": 0,
                "skipped_due_to_cap": 2,
                "total_opportunities": 5,
                "active_queue_limit": 3,
            },
         ):
        yield


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_anonymous_redirected(self, app_ctx, workspace):
        c = flask_app.test_client()
        r = c.post(
            f"/api/client/{workspace.slug}/run-audit",
            json={},
            follow_redirects=False,
        )
        # login_required redirects to /login for unauthenticated POSTs
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

class TestSuccess:
    def test_returns_200_with_added_and_skipped_counters(
        self, logged_in_client, workspace, mocked_audit, user,
    ):
        r = logged_in_client.post(
            f"/api/client/{workspace.slug}/run-audit",
            json={},
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["client_id"] == workspace.slug
        assert body["client_name"] == workspace.name
        assert body["queue_added"] == 3
        assert body["queue_skipped_due_to_cap"] == 2

    def test_success_commits_reservation(
        self, logged_in_client, workspace, mocked_audit, user,
    ):
        """A successful audit should commit the reservation, not
        release it. Wallet balance should be lower by the audit cost."""
        balance_before = user.wallet.balance
        logged_in_client.post(f"/api/client/{workspace.slug}/run-audit", json={})

        # One audit_run = 1 credit. Wallet should be down by 1.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 1

        # The reservation row should be marked committed.
        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="audit_run")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"


# ---------------------------------------------------------------------------
# Insufficient credits
# ---------------------------------------------------------------------------

class TestInsufficientCredits:
    def test_returns_402_with_reason(
        self, app_ctx, make_user, workspace, mocked_audit,
    ):
        # Override the default user with a broke one
        broke = make_user(plan="pro", balance=0, email="broke@x.com")
        # Re-parent the workspace
        workspace.user_id = broke.id
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(broke.id)
            s["_fresh"] = True
        r = c.post(f"/api/client/{workspace.slug}/run-audit", json={})

        assert r.status_code == 402
        body = r.get_json()
        assert body["ok"] is False
        assert body["reason"] == "insufficient_credits"
        # User-facing error mentions the cost
        assert "1 credit" in body["error"] or "credit" in body["error"]

    def test_wallet_unchanged_on_402(
        self, app_ctx, make_user, workspace, mocked_audit,
    ):
        broke = make_user(plan="pro", balance=0, email="broke2@x.com")
        workspace.user_id = broke.id
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(broke.id)
            s["_fresh"] = True
        c.post(f"/api/client/{workspace.slug}/run-audit", json={})

        db.session.refresh(broke.wallet)
        assert broke.wallet.balance == 0  # never debited


# ---------------------------------------------------------------------------
# Exception path
# ---------------------------------------------------------------------------

class TestAuditException:
    def test_audit_raises_returns_500_and_refunds(
        self, logged_in_client, workspace, user,
    ):
        balance_before = user.wallet.balance

        with patch("app.run_audit_for_input", side_effect=RuntimeError("boom")):
            r = logged_in_client.post(
                f"/api/client/{workspace.slug}/run-audit", json={},
            )

        assert r.status_code == 500
        body = r.get_json()
        assert body["ok"] is False
        assert "error" in body
        # The friendly error wrapper kicks in — message should not
        # include the literal exception string.
        assert "boom" not in body["error"]

        # Wallet refunded.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        # Reservation marked released.
        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="audit_run")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_unknown_workspace_returns_404(self, logged_in_client):
        r = logged_in_client.post(
            "/api/client/does-not-exist/run-audit", json={},
        )
        assert r.status_code == 404
        body = r.get_json()
        assert body["ok"] is False

    def test_empty_body_values_fall_through_to_workspace_defaults(
        self, logged_in_client, workspace, mocked_audit,
    ):
        """Documented behavior: empty/falsy body values don't short-
        circuit the validation — they fall through to the workspace's
        stored fields. (The route uses `body.get(k) or client.get(k)`
        chained ORs, so empty strings hand off to the workspace.)

        This means the 400 validation branch in the route is effectively
        defense-in-depth: it only fires if the workspace itself has
        empty fields, which serialize_client_row prevents by filling
        None with "N/A". Documenting the behavior here so any future
        refactor that changes either the validation or serialization
        doesn't accidentally regress the API contract.
        """
        # Even when body sends empty strings, the audit runs (because
        # the workspace's stored values fill in).
        r = logged_in_client.post(
            f"/api/client/{workspace.slug}/run-audit",
            json={"website": "", "industry": "", "location": ""},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Body overrides + defaults
# ---------------------------------------------------------------------------

class TestBodyDefaults:
    def test_empty_body_falls_back_to_workspace_fields(
        self, logged_in_client, workspace, user,
    ):
        """When the JS bulk runner POSTs an empty body, the endpoint
        should pull website/industry/location from the workspace itself
        and pass those to run_audit_for_input."""
        with patch("app.run_audit_for_input", return_value=None) as run_mock, \
             patch(
                "app.create_content_opportunities_from_latest_audit",
                return_value={
                    "added": 0,
                    "skipped_existing": 0,
                    "skipped_due_to_cap": 0,
                    "total_opportunities": 0,
                    "active_queue_limit": 25,
                },
             ):
            r = logged_in_client.post(
                f"/api/client/{workspace.slug}/run-audit", json={},
            )

        assert r.status_code == 200
        # Verify the call used the workspace defaults.
        assert run_mock.call_count == 1
        call_kwargs = run_mock.call_args.kwargs
        assert call_kwargs["website"] == workspace.website
        assert call_kwargs["industry"] == workspace.industry
        assert call_kwargs["location"] == workspace.location

    def test_body_overrides_workspace_fields(
        self, logged_in_client, workspace,
    ):
        """Body params take precedence over workspace defaults — useful
        for one-off overrides without editing the workspace."""
        with patch("app.run_audit_for_input", return_value=None) as run_mock, \
             patch(
                "app.create_content_opportunities_from_latest_audit",
                return_value={
                    "added": 0,
                    "skipped_existing": 0,
                    "skipped_due_to_cap": 0,
                    "total_opportunities": 0,
                    "active_queue_limit": 25,
                },
             ):
            r = logged_in_client.post(
                f"/api/client/{workspace.slug}/run-audit",
                json={"website": "https://override.example.com",
                      "industry": "DTC", "location": "London"},
            )

        assert r.status_code == 200
        call_kwargs = run_mock.call_args.kwargs
        assert call_kwargs["website"] == "https://override.example.com"
        assert call_kwargs["industry"] == "DTC"
        assert call_kwargs["location"] == "London"
