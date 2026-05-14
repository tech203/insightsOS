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
    MarketplacePresence,
    PromptTracking,
    ShopifyConnection,
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


# ---------------------------------------------------------------------------
# /answer-monitor/run/<id> (answer_monitor_run_single cost = 1)
# ---------------------------------------------------------------------------

class TestAnswerMonitorRunSingle:
    """Re-running one tracked prompt. Same shape as the bulk run-all
    above but per-prompt (and only 1 credit). Covers the case where
    a user clicks "re-check this one" instead of running the whole
    monitor sweep."""

    @pytest.fixture
    def tracked_prompt(self, user, workspace):
        row = PromptTracking(
            user_id=user.id,
            domain="action-target.example.com",
            prompt="best AI visibility tool 2026",
        )
        db.session.add(row)
        db.session.commit()
        return row

    def test_success_commits_after_successful_check(
        self, logged_in_client, tracked_prompt, user,
    ):
        balance_before = user.wallet.balance

        fake_snap = [
            {"engine_label": "chatgpt", "brand_mentioned": True},
        ]
        with patch("app._run_answer_check_for_id", return_value=fake_snap):
            r = logged_in_client.post(
                f"/answer-monitor/run/{tracked_prompt.id}",
                follow_redirects=False,
            )

        # Redirects back to the answer monitor page.
        assert r.status_code == 302
        assert "/answer-monitor" in (r.headers.get("Location") or "")

        # 1-credit cost debited.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 1

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="answer_monitor_run_single")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"

    def test_no_result_releases_reservation(
        self, logged_in_client, tracked_prompt, user,
    ):
        """If the per-prompt check returns nothing (OpenAI not
        configured, transient API error), refund the user."""
        balance_before = user.wallet.balance

        with patch("app._run_answer_check_for_id", return_value=None):
            logged_in_client.post(
                f"/answer-monitor/run/{tracked_prompt.id}",
            )

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="answer_monitor_run_single")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_unknown_prompt_redirects_without_reserving(
        self, logged_in_client, user,
    ):
        """Unknown / other-user's prompt id: bail before reserving."""
        balance_before = user.wallet.balance
        r = logged_in_client.post(
            "/answer-monitor/run/999999",
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Wallet untouched; no reservation row created.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before
        assert CreditReservation.query.filter_by(
            user_id=user.id, action_key="answer_monitor_run_single",
        ).first() is None

    def test_insufficient_credits_redirects(self, app_ctx, make_user):
        thin = make_user(plan="pro", balance=0, email="thin-monitor-1@x.com")
        row = PromptTracking(
            user_id=thin.id, domain="any.example.com", prompt="x",
        )
        db.session.add(row)
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(thin.id)
            s["_fresh"] = True
        r = c.post(
            f"/answer-monitor/run/{row.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Wallet untouched.
        db.session.refresh(thin.wallet)
        assert thin.wallet.balance == 0
        # No reservation created — the insufficient-credit branch
        # is the early return path.
        assert CreditReservation.query.filter_by(
            user_id=thin.id, action_key="answer_monitor_run_single",
        ).first() is None


# ---------------------------------------------------------------------------
# /marketplace-audits/<client_id>/run/<presence_id> (marketplace_audit cost = 2)
# ---------------------------------------------------------------------------

class TestMarketplaceRunAudit:
    """Run the AI-visibility audit for one marketplace presence
    (Etsy / Amazon / Shopee / eBay shop). 2 credits — higher cost
    than answer monitor because it generates marketplace-flavoured
    prompts and runs them across configured engines."""

    @pytest.fixture
    def presence(self, user, workspace):
        p = MarketplacePresence(
            user_id=user.id,
            client_id=workspace.id,
            marketplace="etsy",
            shop_name="Action Target Shop",
            shop_url="https://www.etsy.com/shop/ActionTarget",
            category="vintage",
            region="US",
        )
        db.session.add(p)
        db.session.commit()
        return p

    def test_success_commits(self, logged_in_client, workspace, presence, user):
        balance_before = user.wallet.balance

        payload = {
            "visibility_score": 42,
            "queries": [
                {"query": "vintage etsy ?", "cited": True},
                {"query": "best US etsy shop", "cited": False},
            ],
        }
        with patch(
            "services.marketplace_audit.run_marketplace_audit",
            return_value=payload,
        ), patch(
            "ai_answer_agent.enabled_engines",
            return_value=["chatgpt"],
        ):
            r = logged_in_client.post(
                f"/marketplace-audits/{workspace.id}/run/{presence.id}",
                follow_redirects=False,
            )

        # Always redirects back to the marketplace audits page.
        assert r.status_code == 302
        assert "/marketplace-audits" in (r.headers.get("Location") or "")

        # 2-credit cost debited.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 2

        # Reservation committed.
        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="marketplace_audit")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"

        # Presence row updated with the audit payload.
        db.session.refresh(presence)
        assert presence.last_visibility_score == 42
        assert presence.last_audit_payload is not None

    def test_audit_failure_releases_reservation(
        self, logged_in_client, workspace, presence, user,
    ):
        """Exception inside the audit function: release the reservation
        (refund) and don't leave a half-written presence row."""
        balance_before = user.wallet.balance

        with patch(
            "services.marketplace_audit.run_marketplace_audit",
            side_effect=RuntimeError("simulated OpenAI 500"),
        ), patch(
            "ai_answer_agent.enabled_engines",
            return_value=["chatgpt"],
        ):
            logged_in_client.post(
                f"/marketplace-audits/{workspace.id}/run/{presence.id}",
            )

        # Wallet untouched, reservation released.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="marketplace_audit")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

        # The presence row should NOT have been updated with a payload.
        db.session.refresh(presence)
        assert presence.last_visibility_score is None
        assert presence.last_audit_payload is None

    def test_insufficient_credits_redirects(
        self, app_ctx, make_user,
    ):
        thin = make_user(plan="pro", balance=1, email="thin-mp@x.com")
        # marketplace_audit costs 2 — 1 credit isn't enough.
        ws = Client(
            slug="thin-mp", user_id=thin.id, name="Thin MP",
            website="https://thin-mp.example.com",
            website_normalized="thin-mp.example.com",
            industry="A", location="B",
        )
        db.session.add(ws)
        db.session.flush()
        p = MarketplacePresence(
            user_id=thin.id, client_id=ws.id,
            marketplace="etsy", shop_name="Thin",
            shop_url="https://etsy.com/shop/thin",
        )
        db.session.add(p)
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(thin.id)
            s["_fresh"] = True
        r = c.post(
            f"/marketplace-audits/{ws.id}/run/{p.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Wallet untouched.
        db.session.refresh(thin.wallet)
        assert thin.wallet.balance == 1

    def test_unknown_presence_redirects_without_reserving(
        self, logged_in_client, workspace, user,
    ):
        """Bogus presence id: route bails before reserving credits."""
        balance_before = user.wallet.balance
        r = logged_in_client.post(
            f"/marketplace-audits/{workspace.id}/run/999999",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before
        # No reservation made.
        assert CreditReservation.query.filter_by(
            user_id=user.id, action_key="marketplace_audit",
        ).first() is None


# ---------------------------------------------------------------------------
# /content-queue/<id>/ai-edit (ai_edit_turn cost = 1)
# ---------------------------------------------------------------------------
# Unlike the redirect-based routes above, this is a JSON endpoint —
# returns 200 on success, 402 on insufficient credits, 4xx on input
# errors. Same reserve/commit/release shape underneath.

class TestAiEditQueueItem:
    """Multi-turn AI revision on a queue item. Each turn appends to
    the chat history; the response surfaces revised content. 1 credit
    per turn — the only reserve/commit JSON endpoint in this suite,
    so its tests assert on response shape rather than redirects."""

    @pytest.fixture
    def queue_item(self, user, workspace, monkeypatch):
        # add_queue_item lives in content_queue; the route fetches via
        # get_queue_item_by_id. We construct via the same helper to
        # match the production write path.
        from content_queue import add_queue_item, update_queue_item_content
        item = add_queue_item(
            client_id=workspace.slug,
            client_name=workspace.name,
            target_query="how to improve AEO",
            content_type="article",
            item_type="draft",
            title="Draft test",
            user_id=user.id,
        )
        # Drafts arrive empty; populate so the route's "has content?"
        # check passes.
        update_queue_item_content(
            item_id=item["id"],
            user_id=user.id,
            content="# Heading\n\nFirst paragraph of the draft.",
        )
        # OPENAI_API_KEY must be present for the route to attempt the
        # call (otherwise it short-circuits with a 503). The test_key
        # value is fine — every test patches the OpenAI client itself.
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
        return item

    def test_success_returns_200_and_commits(
        self, logged_in_client, queue_item, user,
    ):
        balance_before = user.wallet.balance

        # Mock the OpenAI client's response shape. The route parses
        # message.content as JSON to extract revised_content +
        # summary.
        fake_response = type("R", (), {
            "choices": [type("C", (), {
                "message": type("M", (), {
                    "content": (
                        '{"revised_content": "# New heading\\n\\nRevised.",'
                        ' "summary": "Tightened the heading."}'
                    ),
                })(),
            })()],
        })()

        with patch("openai.OpenAI") as mock_openai:
            mock_openai.return_value.chat.completions.create.return_value = (
                fake_response
            )
            r = logged_in_client.post(
                f"/content-queue/{queue_item['id']}/ai-edit",
                data={"instruction": "tighten the heading"},
            )

        assert r.status_code == 200
        body = r.get_json()
        assert body.get("ok") is True

        # 1-credit cost debited.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 1

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="ai_edit_turn")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"

    def test_openai_failure_releases_reservation(
        self, logged_in_client, queue_item, user,
    ):
        """If the OpenAI call blows up, the reservation is released
        and the user gets back a 5xx-ish error (not charged)."""
        balance_before = user.wallet.balance

        with patch("openai.OpenAI") as mock_openai:
            mock_openai.return_value.chat.completions.create.side_effect = (
                RuntimeError("simulated OpenAI 500")
            )
            r = logged_in_client.post(
                f"/content-queue/{queue_item['id']}/ai-edit",
                data={"instruction": "tighten it"},
            )

        # Route handles the exception and returns a JSON error
        # response with the reservation released.
        assert r.status_code >= 400
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="ai_edit_turn")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_unknown_item_returns_404_without_reserving(
        self, logged_in_client, user,
    ):
        balance_before = user.wallet.balance
        r = logged_in_client.post(
            "/content-queue/does-not-exist/ai-edit",
            data={"instruction": "tighten it"},
        )
        assert r.status_code == 404
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before
        # No reservation row at all.
        assert CreditReservation.query.filter_by(
            user_id=user.id, action_key="ai_edit_turn",
        ).first() is None

    def test_missing_instruction_returns_400_without_reserving(
        self, logged_in_client, queue_item, user,
    ):
        balance_before = user.wallet.balance
        r = logged_in_client.post(
            f"/content-queue/{queue_item['id']}/ai-edit",
            data={"instruction": "   "},  # whitespace-only
        )
        assert r.status_code == 400
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before
        assert CreditReservation.query.filter_by(
            user_id=user.id, action_key="ai_edit_turn",
        ).first() is None

    def test_insufficient_credits_returns_402(
        self, app_ctx, make_user, monkeypatch,
    ):
        from content_queue import add_queue_item, update_queue_item_content
        thin = make_user(plan="pro", balance=0, email="thin-edit@x.com")
        ws = Client(
            slug="thin-edit-ws", user_id=thin.id, name="Thin Edit",
            website="https://x.com", website_normalized="x.com",
            industry="A", location="B",
        )
        db.session.add(ws)
        db.session.commit()
        item = add_queue_item(
            client_id=ws.slug, client_name=ws.name,
            target_query="q", content_type="article", item_type="draft",
            title="t", user_id=thin.id,
        )
        update_queue_item_content(
            item_id=item["id"], user_id=thin.id, content="something",
        )
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(thin.id)
            s["_fresh"] = True
        r = c.post(
            f"/content-queue/{item['id']}/ai-edit",
            data={"instruction": "tighten it"},
        )
        assert r.status_code == 402
        body = r.get_json()
        assert body.get("ok") is False
        assert "top up" in body.get("error", "").lower()
        # Wallet untouched.
        db.session.refresh(thin.wallet)
        assert thin.wallet.balance == 0


# ---------------------------------------------------------------------------
# /content-queue/<id>/generate-visual (visual_generation cost = 1)
# ---------------------------------------------------------------------------
# Placid integration. Unlike the OpenAI routes above, this depends
# on PLACID_API_TOKEN + PLACID_TEMPLATE_UUID_OG env vars — when those
# are missing the route bails before reserving credits.

class TestGenerateQueueItemVisual:
    """Placid-backed visual generation for a queue item. Commits on a
    finished render, releases on Placid errors or empty payload."""

    @pytest.fixture
    def queue_item(self, user, workspace, monkeypatch):
        from content_queue import add_queue_item
        item = add_queue_item(
            client_id=workspace.slug,
            client_name=workspace.name,
            target_query="best AEO tool",
            content_type="article",
            item_type="brief",
            title="The case for AEO",
            user_id=user.id,
        )
        # Both env vars set so the route gets past the config gate.
        monkeypatch.setenv("PLACID_API_TOKEN", "test-token-not-used")
        monkeypatch.setenv("PLACID_TEMPLATE_UUID_OG", "tpl-fake-uuid")
        return item

    def test_success_commits_after_finished_render(
        self, logged_in_client, queue_item, user,
    ):
        balance_before = user.wallet.balance

        # Mock the PlacidClient instance the route imports lazily.
        with patch("services.placid_client.PlacidClient") as MockPlacid:
            instance = MockPlacid.return_value
            instance.generate_image.return_value = {
                "status": "finished",
                "image_url": "https://placid.cdn/og-image-123.png",
            }
            r = logged_in_client.post(
                f"/content-queue/{queue_item['id']}/generate-visual",
                follow_redirects=False,
            )

        # Redirect back to the content queue.
        assert r.status_code == 302
        assert "/content-queue" in (r.headers.get("Location") or "")

        # 1-credit cost debited.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 1

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="visual_generation")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"

    def test_unexpected_status_releases_reservation(
        self, logged_in_client, queue_item, user,
    ):
        """Placid returns something other than 'finished' with no
        image_url (e.g. 'failed' or an empty payload): release the
        reservation so the user isn't charged for a no-op."""
        balance_before = user.wallet.balance

        with patch("services.placid_client.PlacidClient") as MockPlacid:
            instance = MockPlacid.return_value
            instance.generate_image.return_value = {
                "status": "failed",
                "image_url": None,
            }
            logged_in_client.post(
                f"/content-queue/{queue_item['id']}/generate-visual",
            )

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="visual_generation")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_placid_api_error_releases_reservation(
        self, logged_in_client, queue_item, user,
    ):
        """A PlacidAPIError (network blip, 5xx from Placid) should
        release the reservation and surface a friendly flash."""
        from services.placid_client import PlacidAPIError
        balance_before = user.wallet.balance

        with patch("services.placid_client.PlacidClient") as MockPlacid:
            MockPlacid.return_value.generate_image.side_effect = (
                PlacidAPIError("simulated 503")
            )
            logged_in_client.post(
                f"/content-queue/{queue_item['id']}/generate-visual",
            )

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="visual_generation")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_queued_status_keeps_reservation_pending(
        self, logged_in_client, queue_item, user,
    ):
        """Placid 'queued' means the render is still running on
        their side — the route leaves the reservation pending
        rather than charging now or releasing. The sweeper auto-
        releases at 15 min if the user never comes back to refresh."""
        with patch("services.placid_client.PlacidClient") as MockPlacid:
            instance = MockPlacid.return_value
            instance.generate_image.return_value = {
                "status": "queued",
                "image_url": None,
            }
            logged_in_client.post(
                f"/content-queue/{queue_item['id']}/generate-visual",
            )

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="visual_generation")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "pending"

    def test_placid_not_configured_bails_without_reserving(
        self, logged_in_client, queue_item, user, monkeypatch,
    ):
        """If PLACID_API_TOKEN is missing the route flashes an error
        and redirects — no reservation made."""
        monkeypatch.delenv("PLACID_API_TOKEN", raising=False)
        balance_before = user.wallet.balance

        r = logged_in_client.post(
            f"/content-queue/{queue_item['id']}/generate-visual",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before
        assert CreditReservation.query.filter_by(
            user_id=user.id, action_key="visual_generation",
        ).first() is None

    def test_insufficient_credits_redirects(
        self, app_ctx, make_user, monkeypatch,
    ):
        from content_queue import add_queue_item
        thin = make_user(plan="pro", balance=0, email="thin-vis@x.com")
        ws = Client(
            slug="thin-vis-ws", user_id=thin.id, name="Thin Vis",
            website="https://x.com", website_normalized="x.com",
            industry="A", location="B",
        )
        db.session.add(ws)
        db.session.commit()
        item = add_queue_item(
            client_id=ws.slug, client_name=ws.name,
            target_query="q", content_type="article", item_type="brief",
            title="t", user_id=thin.id,
        )
        monkeypatch.setenv("PLACID_API_TOKEN", "test-token-not-used")
        monkeypatch.setenv("PLACID_TEMPLATE_UUID_OG", "tpl-fake-uuid")

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(thin.id)
            s["_fresh"] = True
        r = c.post(
            f"/content-queue/{item['id']}/generate-visual",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(thin.wallet)
        assert thin.wallet.balance == 0
        # No reservation row.
        assert CreditReservation.query.filter_by(
            user_id=thin.id, action_key="visual_generation",
        ).first() is None


# ---------------------------------------------------------------------------
# /integrations/shopify/fix/alt-text/<client_id> (alt_text_fix_batch)
# ---------------------------------------------------------------------------
# Shopify integration. Bulk-fills missing alt text on every product
# image. Requires write_products scope on the connection. Charge
# semantics: commit only if at least one image was actually patched.

class TestShopifyAltTextFix:
    """The alt-text auto-fill route. Reservation is committed only
    when patched > 0 — otherwise released so a user with no missing
    alts (or a total Shopify outage) doesn't get billed for a no-op."""

    @pytest.fixture
    def connected_workspace(self, user, workspace):
        conn = ShopifyConnection(
            user_id=user.id,
            client_id=workspace.id,
            shop_domain="test-store.myshopify.com",
            access_token="shpat_test_token_not_real",
            scope="read_products,write_products",
            shop_meta={},
        )
        db.session.add(conn)
        db.session.commit()
        return workspace, conn

    def test_success_commits_when_at_least_one_image_patched(
        self, logged_in_client, connected_workspace, user,
    ):
        workspace, _ = connected_workspace
        balance_before = user.wallet.balance

        # One product with one image missing alt text. The route
        # should generate alt text and PATCH it back.
        products = [{
            "id": 101,
            "title": "Vintage Mug",
            "product_type": "Drinkware",
            "vendor": "Action Co",
            "tags": "vintage,ceramic",
            "images": [{
                "id": 9001,
                "alt": "",  # empty → eligible for fill
                "src": "https://cdn.shopify.com/products/mug.png",
            }],
        }]

        with patch("services.shopify_client.ShopifyAdminClient") as MockShopify, \
             patch(
                 "app._generate_alt_text_ai",
                 return_value="Vintage ceramic mug product image.",
             ):
            instance = MockShopify.return_value
            instance.list_products.return_value = products
            instance.update_product_image_alt.return_value = None

            r = logged_in_client.post(
                f"/integrations/shopify/fix/alt-text/{workspace.id}",
                follow_redirects=False,
            )

        # Redirects back to the Shopify products view.
        assert r.status_code == 302
        assert "/shopify" in (r.headers.get("Location") or "") or \
               "/integrations" in (r.headers.get("Location") or "")

        # Reservation committed; alt_text_fix_batch costs 2 credits.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 2
        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="alt_text_fix_batch")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"

    def test_no_images_to_patch_releases_reservation(
        self, logged_in_client, connected_workspace, user,
    ):
        """Every product already has alt text → nothing to patch →
        release the reservation (no charge)."""
        workspace, _ = connected_workspace
        balance_before = user.wallet.balance

        products = [{
            "id": 102,
            "title": "Pre-tagged",
            "images": [{
                "id": 9002,
                "alt": "Already has alt text",
                "src": "https://cdn.shopify.com/p.png",
            }],
        }]

        with patch("services.shopify_client.ShopifyAdminClient") as MockShopify:
            MockShopify.return_value.list_products.return_value = products
            logged_in_client.post(
                f"/integrations/shopify/fix/alt-text/{workspace.id}",
            )

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="alt_text_fix_batch")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_shopify_list_failure_releases_reservation(
        self, logged_in_client, connected_workspace, user,
    ):
        """If the initial list_products call blows up (rate limit,
        revoked token), release the reservation before anything is
        attempted — no point charging."""
        from services.shopify_client import ShopifyAPIError
        workspace, _ = connected_workspace
        balance_before = user.wallet.balance

        with patch("services.shopify_client.ShopifyAdminClient") as MockShopify:
            MockShopify.return_value.list_products.side_effect = (
                ShopifyAPIError("rate limit")
            )
            logged_in_client.post(
                f"/integrations/shopify/fix/alt-text/{workspace.id}",
            )

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before
        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="alt_text_fix_batch")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_no_shopify_connection_bails_without_reserving(
        self, logged_in_client, workspace, user,
    ):
        """No ShopifyConnection row for this workspace → flash + redirect
        without reserving credits."""
        balance_before = user.wallet.balance
        r = logged_in_client.post(
            f"/integrations/shopify/fix/alt-text/{workspace.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before
        assert CreditReservation.query.filter_by(
            user_id=user.id, action_key="alt_text_fix_batch",
        ).first() is None

    def test_missing_write_scope_bails_without_reserving(
        self, logged_in_client, workspace, user,
    ):
        """Connection exists but scope is read-only → flash + redirect.
        Defends against tokens issued before write_products was added
        to the app's scope list."""
        conn = ShopifyConnection(
            user_id=user.id,
            client_id=workspace.id,
            shop_domain="readonly.myshopify.com",
            access_token="shpat_readonly",
            scope="read_products",  # no write_products
            shop_meta={},
        )
        db.session.add(conn)
        db.session.commit()

        balance_before = user.wallet.balance
        r = logged_in_client.post(
            f"/integrations/shopify/fix/alt-text/{workspace.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before
        assert CreditReservation.query.filter_by(
            user_id=user.id, action_key="alt_text_fix_batch",
        ).first() is None


# ---------------------------------------------------------------------------
# /integrations/shopify/descriptions/apply/<client_id> (description_rewrite_batch)
# ---------------------------------------------------------------------------
# Bulk-apply user-approved description rewrites. Charge semantics:
# commit only when patched > 0. Selected products come from form
# data (apply_product_id, repeated); proposals are pre-cached on the
# ShopifyConnection's shop_meta.

class TestShopifyDescriptionRewrite:
    """The description rewrite apply route. Behavior mirrors the
    alt-text route — only charge for successful patches, but the
    user must explicitly select which proposals to apply."""

    @pytest.fixture
    def connected_with_proposals(self, user, workspace):
        conn = ShopifyConnection(
            user_id=user.id,
            client_id=workspace.id,
            shop_domain="descstore.myshopify.com",
            access_token="shpat_desc_test",
            scope="read_products,write_products",
            shop_meta={
                "cached_description_proposals": [
                    {
                        "product_id": "201",
                        "title": "Vintage Mug",
                        "proposed_html": "<p>A rewritten description.</p>",
                    },
                    {
                        "product_id": "202",
                        "title": "Modern Mug",
                        "proposed_html": "<p>Another rewrite.</p>",
                    },
                ],
            },
        )
        db.session.add(conn)
        db.session.commit()
        return workspace, conn

    def test_success_commits_after_at_least_one_patch(
        self, logged_in_client, connected_with_proposals, user,
    ):
        workspace, _ = connected_with_proposals
        balance_before = user.wallet.balance

        with patch("services.shopify_client.ShopifyAdminClient") as MockShopify:
            instance = MockShopify.return_value
            instance.update_product_description.return_value = None
            instance.list_products.return_value = []  # refresh call

            r = logged_in_client.post(
                f"/integrations/shopify/descriptions/apply/{workspace.id}",
                data={"apply_product_id": ["201", "202"]},
                follow_redirects=False,
            )

        assert r.status_code == 302
        # description_rewrite_batch costs 3 credits.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 3

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="description_rewrite_batch")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "committed"

    def test_no_products_selected_releases_reservation(
        self, logged_in_client, connected_with_proposals, user,
    ):
        """User submitted the form without ticking any product →
        release the reservation immediately (the route catches this
        before calling Shopify at all)."""
        workspace, _ = connected_with_proposals
        balance_before = user.wallet.balance

        logged_in_client.post(
            f"/integrations/shopify/descriptions/apply/{workspace.id}",
            data={},  # no apply_product_id at all
        )

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="description_rewrite_batch")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_all_patches_fail_releases_reservation(
        self, logged_in_client, connected_with_proposals, user,
    ):
        """Every Shopify PATCH fails → release the reservation so the
        user isn't billed for zero successful updates."""
        from services.shopify_client import ShopifyAPIError
        workspace, _ = connected_with_proposals
        balance_before = user.wallet.balance

        with patch("services.shopify_client.ShopifyAdminClient") as MockShopify:
            MockShopify.return_value.update_product_description.side_effect = (
                ShopifyAPIError("422 unprocessable")
            )
            logged_in_client.post(
                f"/integrations/shopify/descriptions/apply/{workspace.id}",
                data={"apply_product_id": ["201"]},
            )

        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before

        latest = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="description_rewrite_batch")
            .order_by(CreditReservation.id.desc())
            .first()
        )
        assert latest is not None
        assert latest.status == "released"

    def test_no_connection_bails_without_reserving(
        self, logged_in_client, workspace, user,
    ):
        balance_before = user.wallet.balance
        r = logged_in_client.post(
            f"/integrations/shopify/descriptions/apply/{workspace.id}",
            data={"apply_product_id": ["999"]},
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before
        assert CreditReservation.query.filter_by(
            user_id=user.id, action_key="description_rewrite_batch",
        ).first() is None

    def test_insufficient_credits_redirects(
        self, app_ctx, make_user,
    ):
        thin = make_user(plan="pro", balance=0, email="thin-desc@x.com")
        ws = Client(
            slug="thin-desc", user_id=thin.id, name="Thin Desc",
            website="https://x.com", website_normalized="x.com",
            industry="A", location="B",
        )
        db.session.add(ws)
        db.session.flush()
        conn = ShopifyConnection(
            user_id=thin.id, client_id=ws.id,
            shop_domain="thin.myshopify.com",
            access_token="shpat_thin",
            scope="read_products,write_products",
            shop_meta={
                "cached_description_proposals": [
                    {"product_id": "p1", "proposed_html": "<p>x</p>"},
                ],
            },
        )
        db.session.add(conn)
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(thin.id)
            s["_fresh"] = True
        r = c.post(
            f"/integrations/shopify/descriptions/apply/{ws.id}",
            data={"apply_product_id": ["p1"]},
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(thin.wallet)
        assert thin.wallet.balance == 0
        # No reservation at all.
        assert CreditReservation.query.filter_by(
            user_id=thin.id, action_key="description_rewrite_batch",
        ).first() is None
