"""
Tests for the HTML action routes that share the reserve/commit credit
pattern introduced in PR #85.

These routes are higher-stakes than the bulk-audit JSON endpoint
(which already has dedicated coverage in test_bulk_audit_api.py)
because:
    - They're the primary user actions (audit, brief, draft, answer
      monitor sweep) so a regression hits everyone, not just Pro/
      Growth agency users.
    - They redirect rather than return JSON, so a wrong destination
      lands users on the wrong page or pricing for no reason.
    - Each handles its own form-validation step, which is independent
      of the credit flow.

Coverage per route:
    1. Success → expected redirect, wallet debited, reservation committed
    2. Insufficient credits → redirect to /pricing, wallet untouched
    3. Exception during the AI call → wallet refunded, reservation released
    4. Validation: missing required form fields → form re-rendered

Routes covered:
    /client/<id>/run-audit          (audit_run, 1 credit)
    /client/<id>/content-brief      (content_brief, 1 credit)
    /client/<id>/content-draft      (content_draft, 2 credits)
    /answer-monitor/run-all         (answer_monitor_run_all, 2 credits)

The 5 remaining reserve/commit routes share the same shape and are
covered indirectly by the credit-reservation unit tests + the bulk
audit endpoint coverage. They could get this same treatment in a
follow-up.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import (
    Client,
    CreditReservation,
    PromptTracking,
    Wallet,
    db,
)
from app import app as flask_app


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(user):
    """A complete workspace for `user`. All three required fields
    (website / industry / location) populated so the audit form
    validation passes without body params."""
    ws = Client(
        slug="action-target",
        user_id=user.id,
        name="Action Target",
        website="https://action-target.example.com",
        website_normalized="action-target.example.com",
        industry="SaaS",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.commit()
    return ws


@pytest.fixture
def logged_in_client(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


# ---------------------------------------------------------------------------
# /client/<id>/run-audit (HTML form, audit_run cost = 1)
# ---------------------------------------------------------------------------

class TestRunClientAudit:
    """The HTML audit form. Shares the underlying reserve/commit pattern
    with the JSON bulk endpoint at /api/client/<id>/run-audit but has
    its own form-parsing layer and a different redirect destination
    (workspace detail vs JSON response)."""

    @pytest.fixture
    def mocked_audit(self):
        with patch("app.run_audit_for_input", return_value=None), \
             patch(
                "app.create_content_opportunities_from_latest_audit",
                return_value={
                    "added": 2,
                    "skipped_existing": 0,
                    "skipped_due_to_cap": 0,
                    "total_opportunities": 2,
                    "active_queue_limit": 25,
                },
             ):
            yield

    def _form(self, workspace):
        return {
            "website": workspace.website,
            "industry": workspace.industry,
            "location": workspace.location,
            "topic": "AI visibility",
            "audit_type": "quick",
        }

    def test_success_redirects_to_client_detail(
        self, logged_in_client, workspace, mocked_audit,
    ):
        r = logged_in_client.post(
            f"/client/{workspace.slug}/run-audit",
            data=self._form(workspace),
            follow_redirects=False,
        )
        assert r.status_code == 302
        # client_detail uses the slug-based URL
        assert f"/client/{workspace.slug}" in (r.headers.get("Location") or "")

    def test_success_debits_wallet_and_commits(
        self, logged_in_client, workspace, mocked_audit, user,
    ):
        balance_before = user.wallet.balance
        logged_in_client.post(
            f"/client/{workspace.slug}/run-audit",
            data=self._form(workspace),
        )

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 1

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="audit_run")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"

    def test_insufficient_credits_redirects_to_pricing(
        self, app_ctx, make_user, mocked_audit,
    ):
        broke = make_user(plan="pro", balance=0, email="broke-audit@x.com")
        ws = Client(
            slug="broke-ws",
            user_id=broke.id,
            name="Broke",
            website="https://broke.example.com",
            website_normalized="broke.example.com",
            industry="SaaS",
            location="SG",
        )
        db.session.add(ws)
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(broke.id)
            s["_fresh"] = True
        r = c.post(
            f"/client/{ws.slug}/run-audit",
            data={
                "website": ws.website,
                "industry": ws.industry,
                "location": ws.location,
                "topic": "x",
                "audit_type": "quick",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")

        db.session.refresh(broke.wallet)
        assert broke.wallet.balance == 0  # never debited

    def test_exception_refunds_wallet_and_releases_reservation(
        self, logged_in_client, workspace, user,
    ):
        balance_before = user.wallet.balance

        with patch("app.run_audit_for_input", side_effect=RuntimeError("boom")):
            logged_in_client.post(
                f"/client/{workspace.slug}/run-audit",
                data=self._form(workspace),
            )

        # Wallet refunded.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="audit_run")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_missing_required_fields_re_renders_form(
        self, logged_in_client, workspace, user,
    ):
        balance_before = user.wallet.balance
        r = logged_in_client.post(
            f"/client/{workspace.slug}/run-audit",
            data={"website": "", "industry": "", "location": ""},
        )
        # No redirect — form re-rendered with error message inline.
        assert r.status_code == 200
        assert b"Website, industry, and location are required" in r.data

        # No reservation made — wallet untouched.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before


# ---------------------------------------------------------------------------
# /client/<id>/content-brief (content_brief cost = 1)
# ---------------------------------------------------------------------------

class TestGenerateContentBrief:
    @pytest.fixture
    def mocked_brief(self):
        with patch(
            "app.generate_content_brief",
            return_value={"brief": "Mocked brief content. " * 30},
        ):
            yield

    def test_success_renders_result_page(
        self, logged_in_client, workspace, mocked_brief,
    ):
        r = logged_in_client.post(
            f"/client/{workspace.slug}/content-brief",
            data={
                "target_query": "how to improve AI visibility",
                "content_type": "service_page",
                "brand_context": "",
            },
        )
        # Content brief renders the result template directly (200),
        # doesn't redirect.
        assert r.status_code == 200
        assert b"Mocked brief content" in r.data

    def test_success_debits_wallet_and_commits(
        self, logged_in_client, workspace, mocked_brief, user,
    ):
        balance_before = user.wallet.balance
        logged_in_client.post(
            f"/client/{workspace.slug}/content-brief",
            data={"target_query": "x", "content_type": "service_page"},
        )

        db.session.refresh(user.wallet)
        # content_brief = 1 credit
        assert user.wallet.balance == balance_before - 1

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="content_brief")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"

    def test_insufficient_credits_redirects_to_pricing(
        self, app_ctx, make_user, mocked_brief,
    ):
        broke = make_user(plan="pro", balance=0, email="broke-brief@x.com")
        ws = Client(
            slug="brief-broke", user_id=broke.id, name="Brief Broke",
            website="https://x.com", website_normalized="x.com",
            industry="A", location="B",
        )
        db.session.add(ws)
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(broke.id)
            s["_fresh"] = True
        r = c.post(
            f"/client/{ws.slug}/content-brief",
            data={"target_query": "x"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")

        db.session.refresh(broke.wallet)
        assert broke.wallet.balance == 0

    def test_exception_refunds_wallet(
        self, logged_in_client, workspace, user,
    ):
        balance_before = user.wallet.balance

        with patch(
            "app.generate_content_brief",
            side_effect=RuntimeError("generator died"),
        ):
            logged_in_client.post(
                f"/client/{workspace.slug}/content-brief",
                data={"target_query": "x"},
            )

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="content_brief")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_missing_target_query_re_renders_form(
        self, logged_in_client, workspace, user,
    ):
        balance_before = user.wallet.balance
        r = logged_in_client.post(
            f"/client/{workspace.slug}/content-brief",
            data={"target_query": ""},
        )
        assert r.status_code == 200
        assert b"Target query is required" in r.data

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before


# ---------------------------------------------------------------------------
# /client/<id>/content-draft (content_draft cost = 2)
# ---------------------------------------------------------------------------

class TestGenerateContentDraft:
    @pytest.fixture
    def mocked_draft(self):
        with patch(
            "app.generate_content_draft",
            return_value={"draft": "Mocked draft content. " * 50},
        ):
            yield

    def test_success_renders_result_page(
        self, logged_in_client, workspace, mocked_draft,
    ):
        r = logged_in_client.post(
            f"/client/{workspace.slug}/content-draft",
            data={
                "target_query": "x",
                "content_type": "service_page",
                "brief_context": "",
                "brand_context": "",
            },
        )
        assert r.status_code == 200
        assert b"Mocked draft content" in r.data

    def test_success_debits_two_credits_and_commits(
        self, logged_in_client, workspace, mocked_draft, user,
    ):
        balance_before = user.wallet.balance
        logged_in_client.post(
            f"/client/{workspace.slug}/content-draft",
            data={"target_query": "x"},
        )

        db.session.refresh(user.wallet)
        # content_draft = 2 credits
        assert user.wallet.balance == balance_before - 2

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="content_draft")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"

    def test_insufficient_credits_when_balance_one(
        self, app_ctx, make_user, mocked_draft,
    ):
        """1 credit isn't enough for the 2-credit draft action — even
        though it would be enough for a 1-credit brief."""
        thin = make_user(plan="pro", balance=1, email="thin-draft@x.com")
        ws = Client(
            slug="thin-draft", user_id=thin.id, name="Thin",
            website="https://x.com", website_normalized="x.com",
            industry="A", location="B",
        )
        db.session.add(ws)
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(thin.id)
            s["_fresh"] = True
        r = c.post(
            f"/client/{ws.slug}/content-draft",
            data={"target_query": "x"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")

        db.session.refresh(thin.wallet)
        assert thin.wallet.balance == 1  # untouched

    def test_exception_refunds_two_credits(
        self, logged_in_client, workspace, user,
    ):
        balance_before = user.wallet.balance

        with patch(
            "app.generate_content_draft",
            side_effect=RuntimeError("draft generator died"),
        ):
            logged_in_client.post(
                f"/client/{workspace.slug}/content-draft",
                data={"target_query": "x"},
            )

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before  # both credits back

    def test_missing_target_query_re_renders_form(
        self, logged_in_client, workspace, user,
    ):
        balance_before = user.wallet.balance
        r = logged_in_client.post(
            f"/client/{workspace.slug}/content-draft",
            data={"target_query": ""},
        )
        assert r.status_code == 200
        assert b"Target query is required" in r.data

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before


# ---------------------------------------------------------------------------
# /answer-monitor/run-all (answer_monitor_run_all cost = 2)
# ---------------------------------------------------------------------------

class TestAnswerMonitorRunAll:
    """The bulk answer-monitor sweep is the Pro/Growth equivalent of
    running an audit — re-checks every tracked prompt across all
    configured AI engines. Highest-frequency paid action after audits
    themselves."""

    @pytest.fixture
    def workspace_with_prompts(self, user):
        ws = Client(
            slug="monitor-target",
            user_id=user.id,
            name="Monitor Target",
            website="https://monitor.example.com",
            website_normalized="monitor.example.com",
            industry="SaaS",
            location="SG",
        )
        db.session.add(ws)
        db.session.flush()
        # Add 2 tracked prompts so the route finds something to run.
        for q in ["how to fix X", "best Y for Z"]:
            db.session.add(PromptTracking(
                user_id=user.id,
                domain="monitor.example.com",
                prompt=q,
            ))
        db.session.commit()
        return ws

    def test_success_commits_after_successful_check(
        self, logged_in_client, workspace_with_prompts, user,
    ):
        balance_before = user.wallet.balance

        # Mock the per-prompt answer check to return a snapshot.
        fake_snap = [{"engine_label": "chatgpt", "brand_mentioned": True}]
        with patch("app._run_answer_check_for_id", return_value=fake_snap):
            logged_in_client.post(
                "/answer-monitor/run-all",
                data={"client_id": workspace_with_prompts.slug},
                follow_redirects=False,
            )

        # 2-credit cost, debited.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 2

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="answer_monitor_run_all")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"

    def test_all_checks_fail_releases_reservation(
        self, logged_in_client, workspace_with_prompts, user,
    ):
        """If every per-prompt check comes back empty (e.g. OPENAI_API_KEY
        missing), the route releases the reservation instead of
        committing — user shouldn't pay for a no-op."""
        balance_before = user.wallet.balance

        with patch("app._run_answer_check_for_id", return_value=None):
            logged_in_client.post(
                "/answer-monitor/run-all",
                data={"client_id": workspace_with_prompts.slug},
            )

        # Wallet refunded.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="answer_monitor_run_all")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_insufficient_credits_redirects(
        self, app_ctx, make_user,
    ):
        thin = make_user(plan="pro", balance=1, email="thin-monitor@x.com")
        # answer_monitor_run_all costs 2 — 1 credit isn't enough.
        ws = Client(
            slug="thin-monitor", user_id=thin.id, name="Thin Mon",
            website="https://x.com", website_normalized="x.com",
            industry="A", location="B",
        )
        db.session.add(ws)
        db.session.flush()
        db.session.add(PromptTracking(
            user_id=thin.id, domain="x.com", prompt="p",
        ))
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(thin.id)
            s["_fresh"] = True
        r = c.post(
            "/answer-monitor/run-all",
            data={"client_id": ws.slug},
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Bounces back to the answer monitor page, not /pricing —
        # the insufficient-credits flash is shown in-line.
        assert "/answer-monitor" in (r.headers.get("Location") or "")

        db.session.refresh(thin.wallet)
        assert thin.wallet.balance == 1  # untouched

    def test_no_tracked_prompts_short_circuits_without_reserving(
        self, logged_in_client, workspace, user,
    ):
        """If a workspace has no tracked prompts, the route bails
        early without reserving credits."""
        balance_before = user.wallet.balance
        r = logged_in_client.post(
            "/answer-monitor/run-all",
            data={"client_id": workspace.slug},
            follow_redirects=False,
        )
        assert r.status_code == 302  # bounced back

        # No reservation, no wallet movement.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before
        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="answer_monitor_run_all")
            .first()
        )
        assert latest is None
