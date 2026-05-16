"""
Tests for content-queue state-transition routes.

Covers the routes that manipulate queue-item state without calling
external CMS APIs (those have a separate PR-shape — heavier mocking).
What's in scope here:

  POST /content-queue/<id>/approve     transition_queue_item("approve")
  POST /content-queue/<id>/unapprove   transition_queue_item("unapprove")
  POST /content-queue/<id>/status      update_queue_item_status
  POST /content-queue/<id>/delete      delete_queue_item
  POST /content-queue/<id>/edit        update_queue_item_details
  POST /content-queue/<id>/schedule    update_queue_item_schedule

The behaviour worth locking down:
  - **State-machine rules**: approve only from draft_generated; publish
    only from ready; unapprove only from ready. These live in
    APPROVAL_TRANSITIONS in content_queue.py — if someone widens the
    set without updating tests, the regression surfaces here.
  - **User isolation**: every helper takes user_id and refuses to
    touch items owned by another user. Without this guard, a logged-in
    user could mutate any item by guessing UUIDs.
  - **Redirect correctness**: each route bounces back to the queue
    page (or a related view), not 500ing or losing the client_id.

Out of scope: publish-to-{webflow,framer,wix} routes — they need CMS
client mocking and belong in a separate PR.
"""

from __future__ import annotations

from app import Client, db
from app import app as flask_app
from content_queue import add_queue_item, get_queue_item_by_id


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _workspace(user, slug="ws-1"):
    ws = Client(
        slug=slug,
        user_id=user.id,
        name="Test Workspace",
        website="https://x.example.com",
        website_normalized="x.example.com",
        industry="SaaS",
        location="SG",
    )
    db.session.add(ws)
    db.session.commit()
    return ws


def _queue_item(user, ws, *, status="draft_generated", title="Test brief"):
    return add_queue_item(
        client_id=ws.slug,
        client_name=ws.name,
        target_query="best AEO tools",
        content_type="article",
        item_type="brief",
        title=title,
        status=status,
        user_id=user.id,
    )


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/approve
# ---------------------------------------------------------------------------

class TestApprove:

    def test_draft_generated_can_be_approved(self, make_user):
        u = make_user(plan="pro", email="appr-ok@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws, status="draft_generated")

        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/approve",
            data={"client_id": ws.slug},
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Transition fired → status is "ready".
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed["status"] == "ready"

    def test_approve_from_wrong_status_no_transition(self, make_user):
        """APPROVAL_TRANSITIONS["approve"] requires from=draft_generated.
        Any other source must reject without mutating."""
        u = make_user(plan="pro", email="appr-wrong@x.com")
        ws = _workspace(u)
        # Item is already in "published" — approve must not fire.
        item = _queue_item(u, ws, status="published")

        _logged_in(u).post(
            f"/content-queue/{item['id']}/approve",
            data={"client_id": ws.slug},
            follow_redirects=False,
        )
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed["status"] == "published"  # unchanged

    def test_other_users_item_cannot_be_approved(self, make_user):
        """User isolation: approving with a wrong user_id is a no-op."""
        owner = make_user(plan="pro", email="appr-owner@x.com")
        intruder = make_user(plan="pro", email="appr-intruder@x.com")
        ws = _workspace(owner, slug="appr-owner-ws")
        item = _queue_item(owner, ws, status="draft_generated")

        _logged_in(intruder).post(
            f"/content-queue/{item['id']}/approve",
            data={"client_id": ws.slug},
            follow_redirects=False,
        )
        # Owner's item is unchanged.
        refreshed = get_queue_item_by_id(item["id"], user_id=owner.id)
        assert refreshed["status"] == "draft_generated"


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/unapprove
# ---------------------------------------------------------------------------

class TestUnapprove:

    def test_ready_can_be_unapproved(self, make_user):
        u = make_user(plan="pro", email="unappr-ok@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws, status="ready")

        _logged_in(u).post(
            f"/content-queue/{item['id']}/unapprove",
            data={"client_id": ws.slug},
            follow_redirects=False,
        )
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed["status"] == "draft_generated"

    def test_published_cannot_be_unapproved(self, make_user):
        """unapprove only from 'ready' — you can't reverse a publish."""
        u = make_user(plan="pro", email="unappr-pub@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws, status="published")

        _logged_in(u).post(
            f"/content-queue/{item['id']}/unapprove",
            data={"client_id": ws.slug},
            follow_redirects=False,
        )
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed["status"] == "published"  # unchanged


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/status
# ---------------------------------------------------------------------------

class TestStatusUpdate:
    """Raw status setter — bypasses the APPROVAL_TRANSITIONS guard.
    Used by the queue UI's status-pill dropdown for ops/manual moves.
    The flexibility is intentional but means tests should pin down
    user isolation + 404 behavior."""

    def test_owner_can_set_status(self, make_user):
        u = make_user(plan="pro", email="status-ok@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws, status="draft_generated")

        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/status",
            data={"status": "in_progress", "client_id": ws.slug},
            follow_redirects=False,
        )
        assert r.status_code == 302
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed["status"] == "in_progress"

    def test_unknown_status_falls_back_to_pending(self, make_user):
        """_normalize_status enforces a whitelist (VALID_STATUSES);
        anything outside it falls back to 'pending'. Lock that in
        so a UI typo can't silently set arbitrary status text."""
        u = make_user(plan="pro", email="status-bogus@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws, status="draft_generated")

        _logged_in(u).post(
            f"/content-queue/{item['id']}/status",
            data={"status": "obviously_not_a_status", "client_id": ws.slug},
            follow_redirects=False,
        )
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed["status"] == "pending"

    def test_unknown_item_returns_404(self, make_user):
        u = make_user(plan="pro", email="status-404@x.com")
        r = _logged_in(u).post(
            "/content-queue/00000000-0000-0000-0000-000000000000/status",
            data={"status": "ready"},
            follow_redirects=False,
        )
        assert r.status_code == 404

    def test_other_users_item_returns_404(self, make_user):
        owner = make_user(plan="pro", email="status-owner@x.com")
        intruder = make_user(plan="pro", email="status-intruder@x.com")
        ws = _workspace(owner, slug="status-owner-ws")
        item = _queue_item(owner, ws, status="draft_generated")

        r = _logged_in(intruder).post(
            f"/content-queue/{item['id']}/status",
            data={"status": "ready"},
            follow_redirects=False,
        )
        assert r.status_code == 404
        # Owner's item untouched.
        refreshed = get_queue_item_by_id(item["id"], user_id=owner.id)
        assert refreshed["status"] == "draft_generated"


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/delete
# ---------------------------------------------------------------------------

class TestDelete:

    def test_owner_can_delete(self, make_user):
        u = make_user(plan="pro", email="del-ok@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws)

        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/delete",
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Row gone.
        assert get_queue_item_by_id(item["id"], user_id=u.id) is None

    def test_missing_item_redirects_without_crash(self, make_user):
        u = make_user(plan="pro", email="del-missing@x.com")
        r = _logged_in(u).post(
            "/content-queue/00000000-0000-0000-0000-000000000000/delete",
            follow_redirects=False,
        )
        # Friendly flash + redirect, not 500.
        assert r.status_code == 302

    def test_other_users_delete_attempt_no_op(self, make_user):
        owner = make_user(plan="pro", email="del-owner@x.com")
        intruder = make_user(plan="pro", email="del-intruder@x.com")
        ws = _workspace(owner, slug="del-owner-ws")
        item = _queue_item(owner, ws)

        _logged_in(intruder).post(
            f"/content-queue/{item['id']}/delete",
            follow_redirects=False,
        )
        # Owner's row still there.
        assert get_queue_item_by_id(item["id"], user_id=owner.id) is not None


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/edit
# ---------------------------------------------------------------------------

class TestEdit:

    def test_owner_can_edit_fields(self, make_user):
        u = make_user(plan="pro", email="edit-ok@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws)

        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/edit",
            data={
                "target_query": "new query",
                "title": "New title",
                "content_type": "blog_post",
                "priority": "high",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed["target_query"] == "new query"
        assert refreshed["title"] == "New title"
        assert refreshed["content_type"] == "blog_post"
        assert refreshed["priority"] == "high"

    def test_empty_target_query_rejected(self, make_user):
        """Required field — empty string flashes error + redirects
        without mutating."""
        u = make_user(plan="pro", email="edit-empty@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws)
        original_query = item["target_query"]

        _logged_in(u).post(
            f"/content-queue/{item['id']}/edit",
            data={"target_query": "  ", "title": "X"},
            follow_redirects=False,
        )
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed["target_query"] == original_query

    def test_other_users_item_edit_no_op(self, make_user):
        owner = make_user(plan="pro", email="edit-owner@x.com")
        intruder = make_user(plan="pro", email="edit-intruder@x.com")
        ws = _workspace(owner, slug="edit-owner-ws")
        item = _queue_item(owner, ws, title="Original")

        _logged_in(intruder).post(
            f"/content-queue/{item['id']}/edit",
            data={
                "target_query": "intrusion attempt",
                "title": "Hacked",
                "content_type": "article",
                "priority": "high",
            },
            follow_redirects=False,
        )
        refreshed = get_queue_item_by_id(item["id"], user_id=owner.id)
        assert refreshed["title"] == "Original"


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/schedule
# ---------------------------------------------------------------------------

class TestSchedule:

    def test_owner_can_set_schedule(self, make_user):
        u = make_user(plan="pro", email="sched-ok@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws)

        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/schedule",
            data={
                "scheduled_for": "2026-08-15",
                "client_id": ws.slug,
                "redirect_to": "queue",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed.get("scheduled_for") == "2026-08-15"

    def test_empty_scheduled_for_clears_schedule(self, make_user):
        """Empty string treated as "clear" rather than rejected —
        users need a way to remove a scheduled date without
        deleting the item."""
        u = make_user(plan="pro", email="sched-clear@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws)

        c = _logged_in(u)
        # First set a date.
        c.post(
            f"/content-queue/{item['id']}/schedule",
            data={"scheduled_for": "2026-08-15", "client_id": ws.slug},
        )
        # Then clear it.
        c.post(
            f"/content-queue/{item['id']}/schedule",
            data={"scheduled_for": "", "client_id": ws.slug},
        )
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed.get("scheduled_for") in (None, "")

    def test_redirect_to_queue_param(self, make_user):
        """redirect_to=queue bounces back to the queue page;
        anything else goes to the growth calendar."""
        u = make_user(plan="pro", email="sched-redir@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws)

        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/schedule",
            data={
                "scheduled_for": "2026-08-15",
                "client_id": ws.slug,
                "redirect_to": "queue",
            },
            follow_redirects=False,
        )
        assert "/content-queue" in (r.headers.get("Location") or "")

    def test_redirect_default_growth_calendar(self, make_user):
        """No redirect_to → growth calendar (default for the
        recommendation-scheduling flow that originally called this
        route)."""
        u = make_user(plan="pro", email="sched-grow@x.com")
        ws = _workspace(u)
        item = _queue_item(u, ws)

        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/schedule",
            data={"scheduled_for": "2026-08-15", "client_id": ws.slug},
            follow_redirects=False,
        )
        assert "/growth-calendar" in (r.headers.get("Location") or "")

    def test_unknown_item_returns_404(self, make_user):
        u = make_user(plan="pro", email="sched-404@x.com")
        r = _logged_in(u).post(
            "/content-queue/00000000-0000-0000-0000-000000000000/schedule",
            data={"scheduled_for": "2026-08-15", "client_id": "x"},
            follow_redirects=False,
        )
        assert r.status_code == 404
