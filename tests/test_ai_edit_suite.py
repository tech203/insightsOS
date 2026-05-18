"""
Tests for the AI-edit support routes around the multi-turn editor.

The AI turn itself (POST /content-queue/<id>/ai-edit) is already
covered by test_action_routes.py::TestAiEditQueueItem (credit
flow + OpenAI mocking). This file covers the 4 supporting routes
that had no dedicated tests:

  GET  /content-queue/<id>/ai-edit/sections   section picker JSON
  GET  /content-queue/<id>/ai-edit/history    chat + content JSON
  POST /content-queue/<id>/ai-edit/clear      wipe chat history
  POST /content-queue/<id>/apply-ai-edit      commit a revision

All four scope on user_id=current_user.id via the content_queue
helpers — the isolation guarantee (another user can't read a
queue item's chat history or overwrite its content) is the main
thing pinned here, alongside the JSON contract + 404 shape.
"""

from __future__ import annotations

from app import Client, db
from app import app as flask_app
from content_queue import (
    add_queue_item,
    append_queue_item_chat_messages,
    get_queue_item_by_id,
)


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _workspace(user, slug="ai-ws"):
    ws = Client(
        slug=slug,
        user_id=user.id,
        name="Acme",
        website="https://acme.example.com",
        website_normalized="acme.example.com",
        industry="SaaS",
        location="SG",
    )
    db.session.add(ws)
    db.session.commit()
    return ws


def _item(user, ws, *, content="", title="Draft"):
    item = add_queue_item(
        client_id=ws.slug,
        client_name=ws.name,
        target_query="q",
        content_type="article",
        item_type="draft",
        title=title,
        user_id=user.id,
    )
    if content:
        from content_queue import update_queue_item_content
        update_queue_item_content(
            item_id=item["id"], content=content, user_id=user.id,
        )
    return item


_SECTIONED = "# Intro\n\nHello.\n\n# Body\n\nDetails here.\n\n# Close\n\nBye."


# ---------------------------------------------------------------------------
# GET /content-queue/<id>/ai-edit/sections
# ---------------------------------------------------------------------------

class TestSections:

    def test_returns_parsed_sections(self, make_user):
        u = make_user(plan="pro", email="ai-sec-ok@x.com")
        ws = _workspace(u)
        item = _item(u, ws, content=_SECTIONED)

        r = _logged_in(u).get(
            f"/content-queue/{item['id']}/ai-edit/sections"
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        titles = [s["title"] for s in body["sections"]]
        assert "Intro" in titles and "Body" in titles and "Close" in titles
        # Each section carries an idx for the picker.
        assert all("idx" in s for s in body["sections"])

    def test_empty_content_yields_empty_or_single_section(self, make_user):
        u = make_user(plan="pro", email="ai-sec-empty@x.com")
        ws = _workspace(u)
        item = _item(u, ws, content="")
        r = _logged_in(u).get(
            f"/content-queue/{item['id']}/ai-edit/sections"
        )
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_unknown_item_404_json(self, make_user):
        u = make_user(plan="pro", email="ai-sec-404@x.com")
        r = _logged_in(u).get(
            "/content-queue/does-not-exist/ai-edit/sections"
        )
        assert r.status_code == 404
        assert r.get_json()["ok"] is False

    def test_other_users_item_404(self, make_user):
        owner = make_user(plan="pro", email="ai-sec-owner@x.com")
        intruder = make_user(plan="pro", email="ai-sec-intruder@x.com")
        ws = _workspace(owner, slug="ai-sec-owner-ws")
        item = _item(owner, ws, content=_SECTIONED)
        r = _logged_in(intruder).get(
            f"/content-queue/{item['id']}/ai-edit/sections"
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /content-queue/<id>/ai-edit/history
# ---------------------------------------------------------------------------

class TestHistory:

    def test_returns_content_and_chat(self, make_user):
        u = make_user(plan="pro", email="ai-hist-ok@x.com")
        ws = _workspace(u)
        item = _item(u, ws, content="current body")
        append_queue_item_chat_messages(
            item["id"],
            [
                {"role": "user", "content": "tighten it", "ts": "2026-01-01T00:00:00"},
                {"role": "assistant", "content": "done",
                 "revised_content": "tighter body", "summary": "tightened",
                 "ts": "2026-01-01T00:00:01"},
            ],
            user_id=u.id,
        )

        r = _logged_in(u).get(
            f"/content-queue/{item['id']}/ai-edit/history"
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["current_content"] == "current body"
        assert len(body["chat_history"]) == 2
        assert body["chat_history"][0]["role"] == "user"

    def test_no_history_returns_empty_list(self, make_user):
        u = make_user(plan="pro", email="ai-hist-empty@x.com")
        ws = _workspace(u)
        item = _item(u, ws, content="x")
        r = _logged_in(u).get(
            f"/content-queue/{item['id']}/ai-edit/history"
        )
        body = r.get_json()
        assert body["chat_history"] == []

    def test_other_users_item_404(self, make_user):
        """Isolation: chat history can carry sensitive draft content
        — another user must not be able to read it."""
        owner = make_user(plan="pro", email="ai-hist-owner@x.com")
        intruder = make_user(plan="pro", email="ai-hist-intruder@x.com")
        ws = _workspace(owner, slug="ai-hist-owner-ws")
        item = _item(owner, ws, content="secret draft")
        append_queue_item_chat_messages(
            item["id"],
            [{"role": "user", "content": "confidential instruction"}],
            user_id=owner.id,
        )
        r = _logged_in(intruder).get(
            f"/content-queue/{item['id']}/ai-edit/history"
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/ai-edit/clear
# ---------------------------------------------------------------------------

class TestClearHistory:

    def test_owner_clears_chat(self, make_user):
        u = make_user(plan="pro", email="ai-clr-ok@x.com")
        ws = _workspace(u)
        item = _item(u, ws, content="body")
        append_queue_item_chat_messages(
            item["id"], [{"role": "user", "content": "hi"}], user_id=u.id,
        )
        assert get_queue_item_by_id(item["id"], user_id=u.id)["chat_history"]

        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/ai-edit/clear"
        )
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        assert get_queue_item_by_id(item["id"], user_id=u.id)["chat_history"] == []

    def test_unknown_item_404(self, make_user):
        u = make_user(plan="pro", email="ai-clr-404@x.com")
        r = _logged_in(u).post("/content-queue/nope/ai-edit/clear")
        assert r.status_code == 404
        assert r.get_json()["ok"] is False

    def test_other_users_item_cannot_be_cleared(self, make_user):
        owner = make_user(plan="pro", email="ai-clr-owner@x.com")
        intruder = make_user(plan="pro", email="ai-clr-intruder@x.com")
        ws = _workspace(owner, slug="ai-clr-owner-ws")
        item = _item(owner, ws, content="body")
        append_queue_item_chat_messages(
            item["id"], [{"role": "user", "content": "keep me"}],
            user_id=owner.id,
        )
        r = _logged_in(intruder).post(
            f"/content-queue/{item['id']}/ai-edit/clear"
        )
        assert r.status_code == 404
        # Owner's history preserved.
        assert get_queue_item_by_id(item["id"], user_id=owner.id)["chat_history"]


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/apply-ai-edit
# ---------------------------------------------------------------------------

class TestApplyAiEdit:

    def test_owner_applies_revision(self, make_user):
        u = make_user(plan="pro", email="ai-apply-ok@x.com")
        ws = _workspace(u)
        item = _item(u, ws, content="old content")

        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/apply-ai-edit",
            data={"revised_content": "shiny new content",
                  "client_id": ws.slug},
            follow_redirects=False,
        )
        assert r.status_code == 302
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed["content"] == "shiny new content"

    def test_empty_revision_rejected(self, make_user):
        u = make_user(plan="pro", email="ai-apply-empty@x.com")
        ws = _workspace(u)
        item = _item(u, ws, content="unchanged")
        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/apply-ai-edit",
            data={"revised_content": "   ", "client_id": ws.slug},
            follow_redirects=False,
        )
        assert r.status_code == 302
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert refreshed["content"] == "unchanged"

    def test_unknown_item_404(self, make_user):
        u = make_user(plan="pro", email="ai-apply-404@x.com")
        r = _logged_in(u).post(
            "/content-queue/nope/apply-ai-edit",
            data={"revised_content": "x"},
            follow_redirects=False,
        )
        assert r.status_code == 404

    def test_other_users_item_not_overwritten(self, make_user):
        """Isolation: an intruder must not be able to overwrite
        another user's draft content."""
        owner = make_user(plan="pro", email="ai-apply-owner@x.com")
        intruder = make_user(plan="pro", email="ai-apply-intruder@x.com")
        ws = _workspace(owner, slug="ai-apply-owner-ws")
        item = _item(owner, ws, content="owner's words")

        r = _logged_in(intruder).post(
            f"/content-queue/{item['id']}/apply-ai-edit",
            data={"revised_content": "INJECTED", "client_id": ws.slug},
            follow_redirects=False,
        )
        assert r.status_code == 404
        refreshed = get_queue_item_by_id(item["id"], user_id=owner.id)
        assert refreshed["content"] == "owner's words"


# ---------------------------------------------------------------------------
# Anonymous access
# ---------------------------------------------------------------------------

class TestAnonymousAccess:

    def test_sections_requires_auth(self, app_ctx, make_user):
        u = make_user(plan="pro", email="ai-anon-sec@x.com")
        ws = _workspace(u)
        item = _item(u, ws, content="x")
        r = flask_app.test_client().get(
            f"/content-queue/{item['id']}/ai-edit/sections",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_clear_requires_auth(self, app_ctx, make_user):
        u = make_user(plan="pro", email="ai-anon-clr@x.com")
        ws = _workspace(u)
        item = _item(u, ws, content="x")
        append_queue_item_chat_messages(
            item["id"], [{"role": "user", "content": "hi"}], user_id=u.id,
        )
        r = flask_app.test_client().post(
            f"/content-queue/{item['id']}/ai-edit/clear",
            follow_redirects=False,
        )
        assert r.status_code == 302
        # History untouched — handler never reached.
        assert get_queue_item_by_id(item["id"], user_id=u.id)["chat_history"]

    def test_apply_requires_auth(self, app_ctx, make_user):
        u = make_user(plan="pro", email="ai-anon-apply@x.com")
        ws = _workspace(u)
        item = _item(u, ws, content="keep")
        r = flask_app.test_client().post(
            f"/content-queue/{item['id']}/apply-ai-edit",
            data={"revised_content": "nope"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert get_queue_item_by_id(item["id"], user_id=u.id)["content"] == "keep"
