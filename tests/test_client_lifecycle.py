"""
Tests for workspace lifecycle routes:

  POST /client/<id>/share/toggle   generate / revoke public report token
  GET/POST /client/<id>/edit       edit workspace fields
  POST /client/<id>/delete         delete workspace (+ related queue)

These were only smoke-tested. The behaviour worth locking down:

  - share/toggle mints a urlsafe token, revoke clears it; the
    public /report/<token> route resolves only while the token is
    live (revoke must actually kill access)
  - edit validates required fields (name + website) and round-trips
    the rest
  - delete removes the workspace row; related queue items are
    NOT cascaded (current behavior — pinned + flagged, the
    helper's "and_related_queue" name is a misnomer)
  - **user isolation on all three** — a logged-in user must not be
    able to toggle/edit/delete another tenant's workspace by
    guessing an id/slug
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


def _workspace(user, slug="life-ws", **overrides):
    ws = Client(
        slug=slug,
        user_id=user.id,
        name=overrides.get("name", "Acme Co"),
        website=overrides.get("website", "https://acme.example.com"),
        website_normalized="acme.example.com",
        industry=overrides.get("industry", "SaaS"),
        location=overrides.get("location", "SG"),
    )
    db.session.add(ws)
    db.session.commit()
    return ws


# ---------------------------------------------------------------------------
# POST /client/<id>/share/toggle
# ---------------------------------------------------------------------------

class TestShareToggle:

    def test_generate_then_public_report_resolves(self, make_user):
        u = make_user(plan="pro", email="share-gen@x.com")
        ws = _workspace(u)
        assert ws.public_share_token is None

        c = _logged_in(u)
        r = c.post(
            f"/client/{ws.id}/share/toggle",
            data={"action": "generate"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(ws)
        token = ws.public_share_token
        assert token is not None
        assert ws.public_share_created_at is not None

        # Public report route resolves the live token (no auth).
        anon = flask_app.test_client()
        rep = anon.get(f"/report/{token}")
        assert rep.status_code == 200

    def test_revoke_kills_public_access(self, make_user):
        u = make_user(plan="pro", email="share-revoke@x.com")
        ws = _workspace(u)
        c = _logged_in(u)
        c.post(f"/client/{ws.id}/share/toggle", data={"action": "generate"})
        db.session.refresh(ws)
        old_token = ws.public_share_token

        c.post(f"/client/{ws.id}/share/toggle", data={"action": "revoke"})
        db.session.refresh(ws)
        assert ws.public_share_token is None
        assert ws.public_share_created_at is None

        # The old token must no longer resolve — revoke means
        # revoke, not "still works until rotated".
        anon = flask_app.test_client()
        rep = anon.get(f"/report/{old_token}")
        assert rep.status_code == 404

    def test_generate_rotates_token(self, make_user):
        """A second 'generate' issues a fresh token; the previous one
        stops working (single live token per workspace)."""
        u = make_user(plan="pro", email="share-rotate@x.com")
        ws = _workspace(u)
        c = _logged_in(u)
        c.post(f"/client/{ws.id}/share/toggle", data={"action": "generate"})
        db.session.refresh(ws)
        first = ws.public_share_token

        c.post(f"/client/{ws.id}/share/toggle", data={"action": "generate"})
        db.session.refresh(ws)
        second = ws.public_share_token
        assert second != first
        anon = flask_app.test_client()
        assert anon.get(f"/report/{first}").status_code == 404
        assert anon.get(f"/report/{second}").status_code == 200

    def test_unknown_workspace_404(self, make_user):
        u = make_user(plan="pro", email="share-404@x.com")
        r = _logged_in(u).post(
            "/client/999999/share/toggle",
            data={"action": "generate"},
            follow_redirects=False,
        )
        assert r.status_code == 404

    def test_other_users_workspace_404(self, make_user):
        owner = make_user(plan="pro", email="share-owner@x.com")
        intruder = make_user(plan="pro", email="share-intruder@x.com")
        ws = _workspace(owner, slug="share-owner-ws")

        r = _logged_in(intruder).post(
            f"/client/{ws.id}/share/toggle",
            data={"action": "generate"},
            follow_redirects=False,
        )
        assert r.status_code == 404
        db.session.refresh(ws)
        # Token never minted.
        assert ws.public_share_token is None


# ---------------------------------------------------------------------------
# GET/POST /client/<id>/edit
# ---------------------------------------------------------------------------

class TestEditClient:

    def test_get_renders_form(self, make_user):
        u = make_user(plan="pro", email="edit-get@x.com")
        ws = _workspace(u)
        r = _logged_in(u).get(f"/client/{ws.slug}/edit")
        assert r.status_code == 200
        assert b"Acme Co" in r.data

    def test_post_updates_fields(self, make_user):
        u = make_user(plan="pro", email="edit-post@x.com")
        ws = _workspace(u)
        r = _logged_in(u).post(
            f"/client/{ws.slug}/edit",
            data={
                "name": "Renamed Co",
                "website": "https://renamed.example.com",
                "industry": "Fintech",
                "location": "US",
                "owner_type": "company",
                "notes": "updated notes",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(ws)
        assert ws.name == "Renamed Co"
        assert ws.website == "https://renamed.example.com"
        assert ws.industry == "Fintech"

    def test_missing_name_re_renders_form(self, make_user):
        u = make_user(plan="pro", email="edit-noname@x.com")
        ws = _workspace(u)
        r = _logged_in(u).post(
            f"/client/{ws.slug}/edit",
            data={"name": "", "website": "https://x.com"},
        )
        assert r.status_code == 200
        assert b"required" in r.data.lower()
        db.session.refresh(ws)
        assert ws.name == "Acme Co"  # unchanged

    def test_missing_website_re_renders_form(self, make_user):
        u = make_user(plan="pro", email="edit-nourl@x.com")
        ws = _workspace(u)
        r = _logged_in(u).post(
            f"/client/{ws.slug}/edit",
            data={"name": "Has Name", "website": ""},
        )
        assert r.status_code == 200
        db.session.refresh(ws)
        assert ws.website == "https://acme.example.com"  # unchanged

    def test_unknown_workspace_404(self, make_user):
        u = make_user(plan="pro", email="edit-404@x.com")
        r = _logged_in(u).get("/client/nope/edit")
        assert r.status_code == 404

    def test_other_users_workspace_404(self, make_user):
        owner = make_user(plan="pro", email="edit-owner@x.com")
        intruder = make_user(plan="pro", email="edit-intruder@x.com")
        ws = _workspace(owner, slug="edit-owner-ws")

        r = _logged_in(intruder).post(
            f"/client/{ws.slug}/edit",
            data={"name": "Hacked", "website": "https://evil.com"},
            follow_redirects=False,
        )
        assert r.status_code == 404
        db.session.refresh(ws)
        assert ws.name == "Acme Co"  # untouched


# ---------------------------------------------------------------------------
# POST /client/<id>/delete
# ---------------------------------------------------------------------------

class TestDeleteClient:

    def test_owner_deletes_workspace(self, make_user):
        u = make_user(plan="pro", email="del-ws@x.com")
        ws = _workspace(u)
        ws_id = ws.id

        r = _logged_in(u).post(
            f"/client/{ws.slug}/delete", follow_redirects=False,
        )
        assert r.status_code == 302
        assert Client.query.filter_by(id=ws_id).first() is None

    def test_delete_cascades_related_queue_items(self, make_user):
        """delete_client_and_related_queue() must delete the
        workspace's QueueItem rows alongside the workspace itself.

        QueueItem.client_id is a plain String(255) holding the
        workspace *slug* — no ForeignKey, no relationship, no DB-level
        cascade — so the helper deletes the rows explicitly, scoped to
        the owning user, in the same transaction as the Client delete.
        Leaving them behind would orphan rows under a dangling slug
        that slug reuse could later resurrect under a new workspace.
        """
        u = make_user(plan="pro", email="del-cascade@x.com")
        ws = _workspace(u, slug="del-cascade-ws")
        item = add_queue_item(
            client_id=ws.slug,
            client_name=ws.name,
            target_query="q",
            content_type="article",
            item_type="brief",
            title="t",
            user_id=u.id,
        )
        item_id = item["id"]
        assert get_queue_item_by_id(item_id, user_id=u.id) is not None

        _logged_in(u).post(f"/client/{ws.slug}/delete")

        # Workspace gone...
        assert Client.query.filter_by(id=ws.id).first() is None
        # ...and so are its queue items — the helper cascades.
        assert get_queue_item_by_id(item_id, user_id=u.id) is None

    def test_unknown_workspace_404(self, make_user):
        u = make_user(plan="pro", email="del-404@x.com")
        r = _logged_in(u).post(
            "/client/does-not-exist/delete", follow_redirects=False,
        )
        assert r.status_code == 404

    def test_other_users_workspace_cannot_be_deleted(self, make_user):
        owner = make_user(plan="pro", email="del-owner@x.com")
        intruder = make_user(plan="pro", email="del-intruder@x.com")
        ws = _workspace(owner, slug="del-owner-ws")

        r = _logged_in(intruder).post(
            f"/client/{ws.slug}/delete", follow_redirects=False,
        )
        assert r.status_code == 404
        # Owner's workspace preserved.
        assert Client.query.filter_by(id=ws.id).first() is not None


# ---------------------------------------------------------------------------
# Anonymous access
# ---------------------------------------------------------------------------

class TestAnonymousAccess:

    def test_share_toggle_redirects_login(self, app_ctx, make_user):
        u = make_user(plan="pro", email="anon-share@x.com")
        ws = _workspace(u)
        r = flask_app.test_client().post(
            f"/client/{ws.id}/share/toggle",
            data={"action": "generate"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")
        db.session.refresh(ws)
        assert ws.public_share_token is None

    def test_edit_redirects_login(self, app_ctx, make_user):
        u = make_user(plan="pro", email="anon-edit@x.com")
        ws = _workspace(u)
        r = flask_app.test_client().get(
            f"/client/{ws.slug}/edit", follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_delete_redirects_login(self, app_ctx, make_user):
        u = make_user(plan="pro", email="anon-del@x.com")
        ws = _workspace(u)
        r = flask_app.test_client().post(
            f"/client/{ws.slug}/delete", follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")
        # Workspace preserved.
        assert Client.query.filter_by(id=ws.id).first() is not None
