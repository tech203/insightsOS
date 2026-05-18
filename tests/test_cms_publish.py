"""
Tests for the content-queue publish routes.

Four routes, three shapes:

  POST /content-queue/<id>/publish            pure state transition
  POST /content-queue/<id>/publish-to-webflow Webflow CMS push
  POST /content-queue/<id>/publish-to-wix     module-CMS push (Wix)
  POST /content-queue/<id>/publish-to-framer  module-CMS push (Framer)

Strategy:
  - /publish is a pure transition_queue_item("publish") — no
    mocking, just the ready→published state machine + isolation.
  - The CMS pushes have a lot of pre-flight gating (status,
    content-type routing, env config, connection existence) that
    runs BEFORE any external client. We test that gating
    deterministically. One success path per platform is exercised
    with the CMS client mocked.

The recurring guarantee across all four: a user cannot publish or
mutate another tenant's queue item (get_queue_item_by_id is
user-scoped → 404).
"""

from __future__ import annotations

from unittest.mock import patch

from app import Client, FramerConnection, WixConnection, db
from app import app as flask_app
from content_queue import add_queue_item, get_queue_item_by_id


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _workspace(user, slug="cms-ws"):
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


def _item(user, ws, *, status="ready", content_type="article"):
    item = add_queue_item(
        client_id=ws.slug,
        client_name=ws.name,
        target_query="best AEO tool",
        content_type=content_type,
        item_type="draft",
        title="The case for AEO",
        status=status,
        user_id=user.id,
    )
    return item


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/publish  (pure state transition)
# ---------------------------------------------------------------------------

class TestPublishTransition:

    def test_ready_item_publishes(self, make_user):
        u = make_user(plan="pro", email="pub-ready@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready")
        r = _logged_in(u).post(
            f"/content-queue/{item['id']}/publish",
            data={"client_id": ws.slug},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert get_queue_item_by_id(item["id"], user_id=u.id)["status"] == "published"

    def test_draft_generated_cannot_publish_directly(self, make_user):
        """APPROVAL_TRANSITIONS['publish'].from == {'ready'} — a
        draft must be approved first. Status stays unchanged."""
        u = make_user(plan="pro", email="pub-draft@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="draft_generated")
        _logged_in(u).post(
            f"/content-queue/{item['id']}/publish",
            data={"client_id": ws.slug},
        )
        assert get_queue_item_by_id(
            item["id"], user_id=u.id,
        )["status"] == "draft_generated"

    def test_other_users_item_not_published(self, make_user):
        owner = make_user(plan="pro", email="pub-owner@x.com")
        intruder = make_user(plan="pro", email="pub-intruder@x.com")
        ws = _workspace(owner, slug="pub-owner-ws")
        item = _item(owner, ws, status="ready")
        _logged_in(intruder).post(
            f"/content-queue/{item['id']}/publish",
            data={"client_id": ws.slug},
        )
        # Owner's item still ready (transition_queue_item is
        # user-scoped → returns "not found" for the intruder).
        assert get_queue_item_by_id(
            item["id"], user_id=owner.id,
        )["status"] == "ready"


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/publish-to-webflow
# ---------------------------------------------------------------------------

class TestPublishToWebflow:

    def test_unknown_item_404(self, make_user):
        u = make_user(plan="pro", email="wf-404@x.com")
        r = _logged_in(u).post(
            "/content-queue/does-not-exist/publish-to-webflow",
            follow_redirects=False,
        )
        assert r.status_code == 404

    def test_other_users_item_404(self, make_user):
        owner = make_user(plan="pro", email="wf-owner@x.com")
        intruder = make_user(plan="pro", email="wf-intruder@x.com")
        ws = _workspace(owner, slug="wf-owner-ws")
        item = _item(owner, ws, status="ready")
        r = _logged_in(intruder).post(
            f"/content-queue/{item['id']}/publish-to-webflow",
            follow_redirects=False,
        )
        assert r.status_code == 404

    def test_wrong_status_blocked(self, make_user):
        """Only ready / draft_generated can publish. A published or
        pending item bounces with a flash, no CMS call."""
        u = make_user(plan="pro", email="wf-status@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="published")
        with patch("services.webflow_client.WebflowCMSClient") as MockWF:
            r = _logged_in(u).post(
                f"/content-queue/{item['id']}/publish-to-webflow",
                data={"client_id": ws.slug},
                follow_redirects=False,
            )
        assert r.status_code == 302
        MockWF.assert_not_called()

    def test_unmapped_content_type_blocked(self, make_user):
        """A content_type with no Webflow collection mapping
        bounces before any CMS call."""
        u = make_user(plan="pro", email="wf-ct@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready", content_type="press_release")
        with patch("services.webflow_client.WebflowCMSClient") as MockWF:
            r = _logged_in(u).post(
                f"/content-queue/{item['id']}/publish-to-webflow",
                data={"client_id": ws.slug},
                follow_redirects=False,
            )
        assert r.status_code == 302
        MockWF.assert_not_called()

    def test_collection_env_not_configured_blocked(self, make_user, monkeypatch):
        """content_type maps to a collection, but the WEBFLOW_*_ID
        env isn't set → bounce with 'not set up' flash, no CMS
        call. This is the default state under tests (no env)."""
        monkeypatch.delenv("WEBFLOW_BLOG_COLLECTION_ID", raising=False)
        u = make_user(plan="pro", email="wf-noenv@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready", content_type="blog_post")
        with patch("services.webflow_client.WebflowCMSClient") as MockWF:
            r = _logged_in(u).post(
                f"/content-queue/{item['id']}/publish-to-webflow",
                data={"client_id": ws.slug},
                follow_redirects=False,
            )
        assert r.status_code == 302
        MockWF.assert_not_called()

    def test_success_path_marks_export(self, make_user, monkeypatch):
        """Full happy path: env configured + WebflowCMSClient mocked.
        The queue item gets its webflow_* export fields stamped."""
        monkeypatch.setenv("WEBFLOW_BLOG_COLLECTION_ID", "col_blog_123")
        monkeypatch.setenv("WEBFLOW_SITE_ID", "site_abc")
        u = make_user(plan="pro", email="wf-ok@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready", content_type="blog_post")

        with patch("services.webflow_client.WebflowCMSClient") as MockWF:
            inst = MockWF.return_value
            inst.create_item.return_value = "wf_item_999"
            # _build_live_url calls into the client; give it
            # something harmless to chain on.
            inst.get_site_domain.return_value = "acme.webflow.io"
            r = _logged_in(u).post(
                f"/content-queue/{item['id']}/publish-to-webflow",
                data={"client_id": ws.slug},
                follow_redirects=False,
            )
        assert r.status_code == 302
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        # Export bookkeeping recorded on the queue item.
        assert refreshed.get("webflow_item_id") == "wf_item_999"
        assert refreshed.get("webflow_collection") == "blog"

    def test_webflow_api_error_is_handled(self, make_user, monkeypatch):
        """If the Webflow client raises WebflowAPIError, the route
        flashes a friendly message and redirects — never 500."""
        monkeypatch.setenv("WEBFLOW_BLOG_COLLECTION_ID", "col_blog_123")
        u = make_user(plan="pro", email="wf-apierr@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready", content_type="blog_post")

        from services.webflow_client import WebflowAPIError
        with patch("services.webflow_client.WebflowCMSClient") as MockWF:
            MockWF.return_value.create_item.side_effect = WebflowAPIError(
                "simulated 500 from Webflow"
            )
            r = _logged_in(u).post(
                f"/content-queue/{item['id']}/publish-to-webflow",
                data={"client_id": ws.slug},
                follow_redirects=False,
            )
        assert r.status_code == 302
        # Item NOT marked exported.
        refreshed = get_queue_item_by_id(item["id"], user_id=u.id)
        assert not refreshed.get("webflow_item_id")


# ---------------------------------------------------------------------------
# POST /content-queue/<id>/publish-to-{wix,framer}
# ---------------------------------------------------------------------------

class TestPublishToModuleCMS:

    def test_wix_unknown_item_404(self, make_user):
        u = make_user(plan="pro", email="wix-404@x.com")
        r = _logged_in(u).post(
            "/content-queue/nope/publish-to-wix", follow_redirects=False,
        )
        assert r.status_code == 404

    def test_framer_unknown_item_404(self, make_user):
        u = make_user(plan="pro", email="fr-404@x.com")
        r = _logged_in(u).post(
            "/content-queue/nope/publish-to-framer", follow_redirects=False,
        )
        assert r.status_code == 404

    def test_wix_wrong_status_blocked(self, make_user):
        u = make_user(plan="pro", email="wix-status@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="published")
        with patch("services.cms_publisher.publish_to_wix") as mock_pub:
            r = _logged_in(u).post(
                f"/content-queue/{item['id']}/publish-to-wix",
                data={"client_id": str(ws.id)},
                follow_redirects=False,
            )
        assert r.status_code == 302
        mock_pub.assert_not_called()

    def test_wix_no_connection_blocked(self, make_user):
        """ready item but no WixConnection for the workspace →
        bounce with a 'connect one' flash, no publish call."""
        u = make_user(plan="pro", email="wix-noconn@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready")
        with patch("services.cms_publisher.publish_to_wix") as mock_pub:
            r = _logged_in(u).post(
                f"/content-queue/{item['id']}/publish-to-wix",
                data={"client_id": str(ws.id)},
                follow_redirects=False,
            )
        assert r.status_code == 302
        mock_pub.assert_not_called()

    def test_wix_success_path(self, make_user):
        u = make_user(plan="pro", email="wix-ok@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready")
        db.session.add(WixConnection(
            user_id=u.id, client_id=ws.id,
            site_id="site_x", api_key="wix_key",
        ))
        db.session.commit()

        with patch("services.cms_publisher.publish_to_wix") as mock_pub:
            mock_pub.return_value = {"id": "wix_item_42"}
            r = _logged_in(u).post(
                f"/content-queue/{item['id']}/publish-to-wix",
                data={"client_id": str(ws.id)},
                follow_redirects=False,
            )
        assert r.status_code == 302
        mock_pub.assert_called_once()

    def test_framer_success_path(self, make_user):
        u = make_user(plan="pro", email="fr-ok@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready")
        db.session.add(FramerConnection(
            user_id=u.id, client_id=ws.id,
            project_id="proj_x", access_token="fr_token",
        ))
        db.session.commit()

        with patch("services.cms_publisher.publish_to_framer") as mock_pub:
            mock_pub.return_value = {"id": "fr_item_7"}
            r = _logged_in(u).post(
                f"/content-queue/{item['id']}/publish-to-framer",
                data={"client_id": str(ws.id)},
                follow_redirects=False,
            )
        assert r.status_code == 302
        mock_pub.assert_called_once()

    def test_wix_publish_exception_handled(self, make_user):
        """cms_publisher raising must not 500 — flash + redirect."""
        u = make_user(plan="pro", email="wix-exc@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready")
        db.session.add(WixConnection(
            user_id=u.id, client_id=ws.id,
            site_id="site_x", api_key="wix_key",
        ))
        db.session.commit()
        with patch(
            "services.cms_publisher.publish_to_wix",
            side_effect=RuntimeError("simulated Wix outage"),
        ):
            r = _logged_in(u).post(
                f"/content-queue/{item['id']}/publish-to-wix",
                data={"client_id": str(ws.id)},
                follow_redirects=False,
            )
        assert r.status_code == 302  # not 500


# ---------------------------------------------------------------------------
# Anonymous access
# ---------------------------------------------------------------------------

class TestAnonymousAccess:

    def test_publish_requires_auth(self, app_ctx, make_user):
        u = make_user(plan="pro", email="anon-pub@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready")
        r = flask_app.test_client().post(
            f"/content-queue/{item['id']}/publish",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")
        assert get_queue_item_by_id(item["id"], user_id=u.id)["status"] == "ready"

    def test_publish_to_webflow_requires_auth(self, app_ctx, make_user):
        u = make_user(plan="pro", email="anon-wf@x.com")
        ws = _workspace(u)
        item = _item(u, ws, status="ready")
        r = flask_app.test_client().post(
            f"/content-queue/{item['id']}/publish-to-webflow",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")
