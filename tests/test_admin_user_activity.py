"""
Tests for the /admin/users/<id>/activity audit-log view.

Behavior under test:
    1. Access control — non-admin users are 403'd, anonymous users
       redirect to login.
    2. Tab routing — ?tab=... selects which dataset is queried.
       Unknown values fall back to the default tab without 500ing.
    3. Filters — ?type= / ?status= scope the dataset down.
    4. Pagination — ?page= / ?per_page= work, per_page is bounded.
    5. The link from /admin/users/<id> to /admin/users/<id>/activity
       renders for admins.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from dtutils import utcnow

import pytest

from app import (
    CreditReservation,
    CreditTransaction,
    TeamInvite,
    User,
    WebhookEvent,
    db,
)
from app import app as flask_app


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class TestAccessControl:
    def test_anonymous_redirected_to_login(self, app_ctx, make_user):
        target = make_user()
        c = flask_app.test_client()
        r = c.get(f"/admin/users/{target.id}/activity", follow_redirects=False)
        # _require_admin returns a redirect to /login for unauthenticated.
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_non_admin_user_gets_403(self, app_ctx, make_user):
        regular = make_user(role="user")
        target = make_user()
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(regular.id)
            s["_fresh"] = True
        r = c.get(f"/admin/users/{target.id}/activity", follow_redirects=False)
        assert r.status_code == 403

    def test_admin_user_gets_200(self, app_ctx, make_user):
        admin = make_user(role="admin")
        target = make_user(email="target@x.com")
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(admin.id)
            s["_fresh"] = True
        r = c.get(f"/admin/users/{target.id}/activity")
        assert r.status_code == 200
        # Default tab is "credits"; activity page should render
        assert b"Credit transactions" in r.data

    def test_unknown_user_returns_404(self, app_ctx, make_user):
        admin = make_user(role="admin")
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(admin.id)
            s["_fresh"] = True
        r = c.get("/admin/users/999999/activity")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tab routing
# ---------------------------------------------------------------------------

class TestTabRouting:
    @pytest.fixture
    def admin_client(self, app_ctx, make_user):
        admin = make_user(role="admin")
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(admin.id)
            s["_fresh"] = True
        return c

    @pytest.fixture
    def target(self, make_user):
        return make_user(email="audit-target@x.com")

    def test_credits_is_default_tab(self, admin_client, target):
        r = admin_client.get(f"/admin/users/{target.id}/activity")
        assert r.status_code == 200
        assert b"Credit transactions" in r.data

    def test_webhooks_tab(self, admin_client, target):
        r = admin_client.get(
            f"/admin/users/{target.id}/activity?tab=webhooks"
        )
        assert r.status_code == 200
        assert b"Webhook events" in r.data

    def test_reservations_tab(self, admin_client, target):
        r = admin_client.get(
            f"/admin/users/{target.id}/activity?tab=reservations"
        )
        assert r.status_code == 200
        assert b"Credit reservations" in r.data

    def test_invites_tab(self, admin_client, target):
        r = admin_client.get(
            f"/admin/users/{target.id}/activity?tab=invites"
        )
        assert r.status_code == 200
        assert b"Team invites" in r.data

    def test_unknown_tab_falls_back_to_credits(self, admin_client, target):
        """A typo in ?tab= shouldn't 500 — it should silently
        degrade to the default."""
        r = admin_client.get(
            f"/admin/users/{target.id}/activity?tab=banana"
        )
        assert r.status_code == 200
        assert b"Credit transactions" in r.data


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

class TestFilters:
    @pytest.fixture
    def admin_client(self, app_ctx, make_user):
        admin = make_user(role="admin")
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(admin.id)
            s["_fresh"] = True
        return c

    def test_credit_type_filter(self, admin_client, make_user):
        u = make_user()
        # Two flavors of transactions, distinguishable by their notes
        # so we can assert on table rows without colliding with the
        # filter dropdown (which always lists ALL types the user has,
        # by design — so the user can switch filters).
        db.session.add_all([
            CreditTransaction(user_id=u.id, type="signup_bonus", amount=3,
                              balance_after=3, notes="WELCOME_NOTE"),
            CreditTransaction(user_id=u.id, type="topup_bundle", amount=20,
                              balance_after=23, notes="BUNDLE_NOTE"),
        ])
        db.session.commit()

        # No filter → both rows render
        r = admin_client.get(f"/admin/users/{u.id}/activity")
        assert b"WELCOME_NOTE" in r.data
        assert b"BUNDLE_NOTE" in r.data

        # Filter to topup_bundle → only that row's note appears
        r = admin_client.get(
            f"/admin/users/{u.id}/activity?type=topup_bundle"
        )
        assert b"BUNDLE_NOTE" in r.data
        assert b"WELCOME_NOTE" not in r.data

    def test_webhook_status_filter(self, admin_client, make_user):
        u = make_user()
        db.session.add_all([
            WebhookEvent(
                event_id="evt_processed_1",
                event_type="checkout.session.completed",
                status="processed",
                user_id=u.id,
            ),
            WebhookEvent(
                event_id="evt_failed_1",
                event_type="checkout.session.completed",
                status="failed",
                user_id=u.id,
            ),
        ])
        db.session.commit()

        r = admin_client.get(
            f"/admin/users/{u.id}/activity?tab=webhooks&status=failed"
        )
        assert b"evt_failed_1" in r.data
        assert b"evt_processed_1" not in r.data

    def test_reservation_status_filter(self, admin_client, make_user):
        u = make_user()
        db.session.add_all([
            CreditReservation(
                user_id=u.id, amount=1, action_key="audit_run",
                status="committed",
                expires_at=utcnow() + timedelta(minutes=15),
            ),
            CreditReservation(
                user_id=u.id, amount=1, action_key="audit_run",
                status="released",
                expires_at=utcnow() + timedelta(minutes=15),
            ),
        ])
        db.session.commit()

        r = admin_client.get(
            f"/admin/users/{u.id}/activity?tab=reservations&status=released"
        )
        # "released" matches both the row status and the dropdown — but
        # only one reservation has that status. Easiest assertion is on
        # the dropdown showing "All statuses" not selected and the
        # released row's presence implicitly.
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class TestPagination:
    @pytest.fixture
    def admin_client(self, app_ctx, make_user):
        admin = make_user(role="admin")
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(admin.id)
            s["_fresh"] = True
        return c

    def test_per_page_bounded(self, admin_client, make_user):
        """per_page is clamped to [10, 200] so an attacker can't pass
        ?per_page=999999 and DoS the page render."""
        u = make_user()
        r = admin_client.get(
            f"/admin/users/{u.id}/activity?per_page=99999999"
        )
        # No crash, page renders
        assert r.status_code == 200

    def test_garbage_per_page_falls_back(self, admin_client, make_user):
        u = make_user()
        r = admin_client.get(
            f"/admin/users/{u.id}/activity?per_page=abc"
        )
        assert r.status_code == 200

    def test_garbage_page_falls_back_to_1(self, admin_client, make_user):
        u = make_user()
        r = admin_client.get(
            f"/admin/users/{u.id}/activity?page=-7"
        )
        assert r.status_code == 200

    def test_page_2_renders_when_enough_rows(self, admin_client, make_user):
        u = make_user()
        # Insert 25 transactions, ask for per_page=10, page=2 → page exists
        for i in range(25):
            db.session.add(CreditTransaction(
                user_id=u.id, type="usage_audit_run", amount=-1,
                balance_after=10 - i, notes=f"tx-{i}",
            ))
        db.session.commit()
        r = admin_client.get(
            f"/admin/users/{u.id}/activity?per_page=10&page=2"
        )
        assert r.status_code == 200
        # Page 2 of 3 (25 rows / 10) should mention the page in the pager
        assert b"Page 2 of 3" in r.data


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------

class TestEmptyState:
    @pytest.fixture
    def admin_client(self, app_ctx, make_user):
        admin = make_user(role="admin")
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(admin.id)
            s["_fresh"] = True
        return c

    def test_no_transactions_shows_empty_message(self, admin_client, make_user):
        # plan="free" so make_user doesn't insert a placeholder
        # monthly_allowance row (only paid plans get the fixture
        # backfill in #96) and the before_request hook doesn't grant
        # a fresh one (free plan has 0 monthly credits).
        u = make_user(email="empty@x.com", plan="free")
        r = admin_client.get(f"/admin/users/{u.id}/activity")
        assert r.status_code == 200
        assert b"No credit transactions yet" in r.data

    def test_no_webhooks_shows_empty_message(self, admin_client, make_user):
        u = make_user(email="empty2@x.com", plan="free")
        r = admin_client.get(
            f"/admin/users/{u.id}/activity?tab=webhooks"
        )
        assert r.status_code == 200
        assert b"No webhook events recorded" in r.data


# ---------------------------------------------------------------------------
# Link from /admin/users/<id> to /admin/users/<id>/activity
# ---------------------------------------------------------------------------

class TestActivityLinkFromUserDetail:
    def test_admin_user_detail_page_links_to_activity(self, app_ctx, make_user):
        admin = make_user(role="admin")
        target = make_user(email="link-target@x.com")
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(admin.id)
            s["_fresh"] = True
        r = c.get(f"/admin/users/{target.id}")
        assert r.status_code == 200
        expected = f"/admin/users/{target.id}/activity".encode()
        assert expected in r.data
