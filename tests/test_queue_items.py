"""
Tests for the content queue (QueueItem model + content_queue.py).

Migrated from data/content_queue.json to SQL — these tests pin the
new contract:
    1. CRUD goes through SQL (no on-disk pollution)
    2. user_id / client_id scoping works as a SQL WHERE clause
       (regression check: the JSON era used Python list comprehensions)
    3. upsert_generation_item dedupes correctly
    4. The dict shape returned by every function matches what the
       templates expect
    5. delete_items_for_client cascades only within the client/user
       scope, not across them
"""

from __future__ import annotations

import pytest

from app import QueueItem, db
from content_queue import (
    add_queue_item,
    delete_items_for_client,
    delete_queue_item,
    get_next_action,
    get_queue_item_by_id,
    get_queue_items,
    transition_queue_item,
    update_queue_item_content,
    update_queue_item_schedule,
    update_queue_item_status,
    upsert_generation_item,
    append_queue_item_chat_messages,
    clear_queue_item_chat_history,
)


# ---------------------------------------------------------------------------
# add_queue_item
# ---------------------------------------------------------------------------

class TestAddQueueItem:
    def test_insert_returns_dict_with_expected_shape(self, user):
        item = add_queue_item(
            client_id="some-slug",
            client_name="Some Client",
            target_query="how to improve AEO",
            content_type="service_page",
            item_type="brief",
            title="Brief: how to improve AEO",
            user_id=user.id,
        )
        # Every key the templates / route handlers read
        for k in (
            "id", "user_id", "client_id", "client_name", "target_query",
            "content_type", "item_type", "title", "content", "status",
            "priority", "source", "credits_required", "execution_type",
            "source_action_title", "scheduled_for", "webflow_item_id",
            "webflow_collection", "webflow_live_url", "og_image_url",
            "chat_history", "created_at", "updated_at",
        ):
            assert k in item

    def test_id_is_uuid_string(self, user):
        item = add_queue_item(
            client_id="x", client_name="x", target_query="q",
            content_type="article", item_type="brief", title="t",
            user_id=user.id,
        )
        # uuid4 string = 36 chars with dashes
        assert isinstance(item["id"], str)
        assert len(item["id"]) == 36
        assert item["id"].count("-") == 4

    def test_persists_to_sql(self, user):
        item = add_queue_item(
            client_id="x", client_name="x", target_query="q",
            content_type="article", item_type="brief", title="t",
            user_id=user.id,
        )
        row = QueueItem.query.filter_by(id=item["id"]).first()
        assert row is not None
        assert row.user_id == user.id

    def test_invalid_enums_fall_back_to_defaults(self, user):
        item = add_queue_item(
            client_id="x", client_name="x", target_query="q",
            content_type="article", item_type="invalid-type",
            title="t",
            status="completely-bogus",
            priority="enormous",
            source="space-aliens",
            user_id=user.id,
        )
        assert item["item_type"] == "brief"
        assert item["status"] == "pending"
        assert item["priority"] == "medium"
        assert item["source"] == "manual"


# ---------------------------------------------------------------------------
# get_queue_items — SQL filter pushdown
# ---------------------------------------------------------------------------

class TestGetQueueItems:
    def _seed(self, user_id, n=3, *, client_id="ws-a", status="pending"):
        items = []
        for i in range(n):
            items.append(add_queue_item(
                client_id=client_id, client_name="WS A",
                target_query=f"q{i}", content_type="article",
                item_type="brief", title=f"item-{i}",
                status=status, user_id=user_id,
            ))
        return items

    def test_user_scoping_is_enforced(self, make_user):
        a = make_user(email="a@x.com")
        b = make_user(email="b@x.com")
        self._seed(a.id, n=2)
        self._seed(b.id, n=3)

        assert len(get_queue_items(user_id=a.id)) == 2
        assert len(get_queue_items(user_id=b.id)) == 3

    def test_client_scoping_filters(self, user):
        self._seed(user.id, n=2, client_id="ws-a")
        self._seed(user.id, n=3, client_id="ws-b")

        a_items = get_queue_items(user_id=user.id, client_id="ws-a")
        b_items = get_queue_items(user_id=user.id, client_id="ws-b")
        assert len(a_items) == 2
        assert len(b_items) == 3
        assert {i["client_id"] for i in a_items} == {"ws-a"}

    def test_dismissed_hidden_by_default(self, user):
        self._seed(user.id, n=1, status="pending")
        self._seed(user.id, n=1, status="dismissed")

        default = get_queue_items(user_id=user.id)
        with_dismissed = get_queue_items(user_id=user.id, include_dismissed=True)
        assert len(default) == 1
        assert len(with_dismissed) == 2

    def test_status_filter_overrides_dismissed_hiding(self, user):
        """Explicitly filtering by status=dismissed should return
        dismissed rows even though they're hidden by default."""
        self._seed(user.id, n=1, status="pending")
        self._seed(user.id, n=1, status="dismissed")

        only_dismissed = get_queue_items(user_id=user.id, status="dismissed")
        assert len(only_dismissed) == 1
        assert only_dismissed[0]["status"] == "dismissed"

    def test_returns_newest_first(self, user):
        # add_queue_item uses _now_iso() which has second-level
        # resolution. Insert sequentially; created_at strings sort
        # lexicographically which == temporal for ISO-8601.
        import time
        a = add_queue_item(
            client_id="x", client_name="x", target_query="q1",
            content_type="article", item_type="brief", title="first",
            user_id=user.id,
        )
        time.sleep(1.1)  # ensure created_at increments at second resolution
        b = add_queue_item(
            client_id="x", client_name="x", target_query="q2",
            content_type="article", item_type="brief", title="second",
            user_id=user.id,
        )
        items = get_queue_items(user_id=user.id)
        # newest first means b comes before a
        assert items[0]["id"] == b["id"]
        assert items[1]["id"] == a["id"]


# ---------------------------------------------------------------------------
# Single-field mutators
# ---------------------------------------------------------------------------

class TestMutators:
    def _make(self, user_id, **kwargs):
        return add_queue_item(
            client_id=kwargs.get("client_id", "x"),
            client_name="x", target_query="q",
            content_type="article", item_type="brief", title="t",
            user_id=user_id,
        )

    def test_update_status_persists(self, user):
        item = self._make(user.id)
        updated = update_queue_item_status(
            item["id"], "brief_generated", user_id=user.id,
        )
        assert updated["status"] == "brief_generated"
        # Verify from a fresh read, not the returned dict
        fresh = get_queue_item_by_id(item["id"], user_id=user.id)
        assert fresh["status"] == "brief_generated"

    def test_update_status_user_scoping(self, make_user):
        owner = make_user(email="owner-q@x.com")
        intruder = make_user(email="intruder-q@x.com")
        item = self._make(owner.id)
        # The intruder can't flip another user's status
        result = update_queue_item_status(
            item["id"], "ready", user_id=intruder.id,
        )
        assert result is None
        fresh = get_queue_item_by_id(item["id"], user_id=owner.id)
        assert fresh["status"] == "pending"

    def test_update_content_partial(self, user):
        item = self._make(user.id)
        updated = update_queue_item_content(
            item["id"], content="new body", priority="high",
            user_id=user.id,
        )
        assert updated["content"] == "new body"
        assert updated["priority"] == "high"
        # Other fields stayed put
        assert updated["title"] == item["title"]

    def test_update_schedule_clears(self, user):
        item = self._make(user.id)
        update_queue_item_schedule(item["id"], "2026-06-01", user_id=user.id)
        assert get_queue_item_by_id(item["id"], user_id=user.id)["scheduled_for"] == "2026-06-01"
        # Pass None to clear it
        update_queue_item_schedule(item["id"], None, user_id=user.id)
        assert get_queue_item_by_id(item["id"], user_id=user.id)["scheduled_for"] is None


# ---------------------------------------------------------------------------
# upsert_generation_item
# ---------------------------------------------------------------------------

class TestUpsertGenerationItem:
    def test_first_call_creates(self, user):
        item = upsert_generation_item(
            client_id="ws-a", client_name="WS A",
            target_query="how to X", content_type="service_page",
            item_type="brief", status="brief_generated",
            content="content body", title="Brief: how to X",
            user_id=user.id,
        )
        assert item["status"] == "brief_generated"
        # Only one row exists
        assert QueueItem.query.filter_by(user_id=user.id).count() == 1

    def test_second_call_with_same_query_updates_existing(self, user):
        first = upsert_generation_item(
            client_id="ws-a", client_name="WS A",
            target_query="how to X", content_type="service_page",
            item_type="brief", status="brief_generated",
            content="brief body", title="Brief: how to X",
            user_id=user.id,
        )
        # Calling again with the SAME target_query+client_id flips
        # the same row from brief_generated → draft_generated.
        second = upsert_generation_item(
            client_id="ws-a", client_name="WS A",
            target_query="how to X", content_type="service_page",
            item_type="draft", status="draft_generated",
            content="draft body", title="Draft: how to X",
            user_id=user.id,
        )
        # Same row
        assert second["id"] == first["id"]
        # Status flipped
        assert second["status"] == "draft_generated"
        assert second["content"] == "draft body"
        # Still only one row
        assert QueueItem.query.filter_by(user_id=user.id).count() == 1

    def test_different_target_query_spawns_new_row(self, user):
        upsert_generation_item(
            client_id="ws-a", client_name="WS A",
            target_query="first query", content_type="service_page",
            item_type="brief", status="brief_generated",
            content="x", title="t", user_id=user.id,
        )
        upsert_generation_item(
            client_id="ws-a", client_name="WS A",
            target_query="second query", content_type="service_page",
            item_type="brief", status="brief_generated",
            content="x", title="t", user_id=user.id,
        )
        assert QueueItem.query.filter_by(user_id=user.id).count() == 2

    def test_different_user_spawns_new_row(self, make_user):
        a = make_user(email="upsert-a@x.com")
        b = make_user(email="upsert-b@x.com")
        upsert_generation_item(
            client_id="ws-a", client_name="WS A",
            target_query="same query", content_type="service_page",
            item_type="brief", status="brief_generated",
            content="x", title="t", user_id=a.id,
        )
        upsert_generation_item(
            client_id="ws-a", client_name="WS A",
            target_query="same query", content_type="service_page",
            item_type="brief", status="brief_generated",
            content="x", title="t", user_id=b.id,
        )
        # Two rows — one per user, even though the query+client match
        assert QueueItem.query.count() == 2


# ---------------------------------------------------------------------------
# delete_items_for_client — scoped cascade
# ---------------------------------------------------------------------------

class TestDeleteItemsForClient:
    def test_deletes_only_within_client_and_user(self, make_user):
        a = make_user(email="del-a@x.com")
        b = make_user(email="del-b@x.com")

        # a has items in ws-1 and ws-2; b has items in ws-1
        for client, owner in [
            ("ws-1", a), ("ws-1", a), ("ws-2", a), ("ws-1", b),
        ]:
            add_queue_item(
                client_id=client, client_name="X", target_query="q",
                content_type="article", item_type="brief", title="t",
                user_id=owner.id,
            )

        deleted = delete_items_for_client("ws-1", user_id=a.id)
        assert deleted == 2  # both of a's ws-1 items

        # a's ws-2 still there
        assert QueueItem.query.filter_by(user_id=a.id).count() == 1
        # b's ws-1 not touched
        assert QueueItem.query.filter_by(user_id=b.id, client_id="ws-1").count() == 1


# ---------------------------------------------------------------------------
# Chat history mutators
# ---------------------------------------------------------------------------

class TestChatHistory:
    def test_append_and_clear(self, user):
        item = add_queue_item(
            client_id="x", client_name="x", target_query="q",
            content_type="article", item_type="brief", title="t",
            user_id=user.id,
        )
        # Empty by default
        assert item["chat_history"] == []

        append_queue_item_chat_messages(
            item["id"],
            [{"role": "user", "content": "hi"},
             {"role": "assistant", "content": "hello"}],
            user_id=user.id,
        )
        fresh = get_queue_item_by_id(item["id"], user_id=user.id)
        assert len(fresh["chat_history"]) == 2
        assert fresh["chat_history"][0]["role"] == "user"
        assert fresh["chat_history"][1]["content"] == "hello"

        clear_queue_item_chat_history(item["id"], user_id=user.id)
        fresh = get_queue_item_by_id(item["id"], user_id=user.id)
        assert fresh["chat_history"] == []


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

class TestTransitions:
    def test_approve_from_draft(self, user):
        item = add_queue_item(
            client_id="x", client_name="x", target_query="q",
            content_type="article", item_type="draft", title="t",
            status="draft_generated", user_id=user.id,
        )
        updated, err = transition_queue_item(item["id"], "approve", user_id=user.id)
        assert err is None
        assert updated["status"] == "ready"

    def test_publish_from_pending_is_blocked(self, user):
        """publish is only valid from `ready` — never directly from
        pending or draft_generated. This is the gate that keeps a
        bot from skipping the approval step."""
        item = add_queue_item(
            client_id="x", client_name="x", target_query="q",
            content_type="article", item_type="brief", title="t",
            user_id=user.id,
        )
        updated, err = transition_queue_item(item["id"], "publish", user_id=user.id)
        assert updated is None
        assert "Cannot publish from status 'pending'" in (err or "")

    def test_unknown_transition_errors(self, user):
        item = add_queue_item(
            client_id="x", client_name="x", target_query="q",
            content_type="article", item_type="brief", title="t",
            user_id=user.id,
        )
        updated, err = transition_queue_item(item["id"], "warp-drive", user_id=user.id)
        assert updated is None
        assert "Unknown transition" in (err or "")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestGetNextAction:
    @pytest.mark.parametrize("status,expected_label", [
        ("pending", "Generate Brief"),
        ("brief_generated", "Generate Draft"),
        ("draft_generated", "Approve Draft"),
        ("ready", "Publish"),
        ("published", None),  # no further action
        ("dismissed", None),
    ])
    def test_next_action_per_status(self, status, expected_label):
        item = {"id": "x", "status": status}
        result = get_next_action(item)
        if expected_label is None:
            assert result is None
        else:
            assert result["label"] == expected_label

    def test_none_item_returns_none(self):
        assert get_next_action(None) is None


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDelete:
    def test_delete_removes_row(self, user):
        item = add_queue_item(
            client_id="x", client_name="x", target_query="q",
            content_type="article", item_type="brief", title="t",
            user_id=user.id,
        )
        assert delete_queue_item(item["id"], user_id=user.id) is True
        assert get_queue_item_by_id(item["id"], user_id=user.id) is None

    def test_delete_returns_false_for_unknown(self, user):
        assert delete_queue_item("does-not-exist", user_id=user.id) is False

    def test_delete_scoped_by_user(self, make_user):
        owner = make_user(email="del-owner@x.com")
        intruder = make_user(email="del-intruder@x.com")
        item = add_queue_item(
            client_id="x", client_name="x", target_query="q",
            content_type="article", item_type="brief", title="t",
            user_id=owner.id,
        )
        # Wrong-user delete attempt returns False; row survives
        assert delete_queue_item(item["id"], user_id=intruder.id) is False
        assert get_queue_item_by_id(item["id"], user_id=owner.id) is not None
