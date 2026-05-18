"""
Tests for the integration disconnect routes.

Seven storefront/integration platforms each expose a disconnect
POST that deletes the stored connection row (and with it the
stored API key / access token). The security-critical invariant
across all of them: the delete is scoped to the owning account,
so a logged-in user can NOT sever another tenant's integration by
guessing a client_id.

Two URL shapes exist:
  - Most: /integrations/<platform>/<client_id>/disconnect
          filter_by(user_id=effective_owner_id(), client_id=...)
  - Shopify: /integrations/shopify/disconnect/<client_id>
          extra workspace-ownership check, filter on current_user.id

Both shapes are parametrized below. Each platform gets:
  - owner disconnects → row deleted, redirect (no 500)
  - intruder cannot disconnect → row preserved
  - no-connection → graceful redirect, no crash
  - anonymous → redirect to /login
"""

from __future__ import annotations

import pytest

from app import (
    BigCommerceConnection,
    CalComConnection,
    Client,
    ShoplineConnection,
    ShopifyConnection,
    SquarespaceConnection,
    WixConnection,
    WooCommerceConnection,
    db,
)
from app import app as flask_app


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _workspace(user, slug):
    ws = Client(
        slug=slug,
        user_id=user.id,
        name="WS",
        website="https://x.example.com",
        website_normalized="x.example.com",
        industry="SaaS",
        location="SG",
    )
    db.session.add(ws)
    db.session.commit()
    return ws


# Per-platform: (model class, kwargs-builder, disconnect-URL-template).
# The URL template takes a single {cid} placeholder.
def _calcom(user, ws):
    return CalComConnection(
        user_id=user.id, client_id=ws.id,
        api_key="cal_key", username="acme",
    )


def _woo(user, ws):
    return WooCommerceConnection(
        user_id=user.id, client_id=ws.id,
        store_url="https://shop.example.com",
        consumer_key="ck_x", consumer_secret="cs_x",
    )


def _shopify(user, ws):
    return ShopifyConnection(
        user_id=user.id, client_id=ws.id,
        shop_domain="acme.myshopify.com",
        access_token="shpat_x",
    )


def _bigcommerce(user, ws):
    return BigCommerceConnection(
        user_id=user.id, client_id=ws.id,
        store_hash="abc123", access_token="bc_x",
    )


def _shopline(user, ws):
    return ShoplineConnection(
        user_id=user.id, client_id=ws.id,
        store_handle="acme", access_token="sl_x",
    )


def _wix(user, ws):
    return WixConnection(
        user_id=user.id, client_id=ws.id,
        site_id="site_x", api_key="wix_x",
    )


def _squarespace(user, ws):
    return SquarespaceConnection(
        user_id=user.id, client_id=ws.id,
        api_key="sq_x",
    )


PLATFORMS = [
    ("calcom", CalComConnection, _calcom,
     "/integrations/calcom/{cid}/disconnect"),
    ("woocommerce", WooCommerceConnection, _woo,
     "/integrations/woocommerce/{cid}/disconnect"),
    ("shopify", ShopifyConnection, _shopify,
     "/integrations/shopify/disconnect/{cid}"),
    ("bigcommerce", BigCommerceConnection, _bigcommerce,
     "/integrations/bigcommerce/{cid}/disconnect"),
    ("shopline", ShoplineConnection, _shopline,
     "/integrations/shopline/{cid}/disconnect"),
    ("wix", WixConnection, _wix,
     "/integrations/wix/{cid}/disconnect"),
    ("squarespace", SquarespaceConnection, _squarespace,
     "/integrations/squarespace/{cid}/disconnect"),
]

_IDS = [p[0] for p in PLATFORMS]


@pytest.mark.parametrize(
    "name,model,builder,url_tpl", PLATFORMS, ids=_IDS,
)
class TestDisconnectOwner:

    def test_owner_disconnect_deletes_row(
        self, name, model, builder, url_tpl, make_user,
    ):
        u = make_user(plan="pro", email=f"disc-{name}-owner@x.com")
        ws = _workspace(u, slug=f"disc-{name}-ws")
        conn = builder(u, ws)
        db.session.add(conn)
        db.session.commit()
        conn_id = conn.id

        r = _logged_in(u).post(
            url_tpl.format(cid=ws.id), follow_redirects=False,
        )
        assert r.status_code == 302
        # Connection row gone.
        assert model.query.filter_by(id=conn_id).first() is None

    def test_no_connection_is_graceful(
        self, name, model, builder, url_tpl, make_user,
    ):
        """Disconnect with nothing connected: redirect, no 500."""
        u = make_user(plan="pro", email=f"disc-{name}-none@x.com")
        ws = _workspace(u, slug=f"disc-{name}-none-ws")
        r = _logged_in(u).post(
            url_tpl.format(cid=ws.id), follow_redirects=False,
        )
        assert r.status_code == 302


@pytest.mark.parametrize(
    "name,model,builder,url_tpl", PLATFORMS, ids=_IDS,
)
class TestDisconnectIsolation:
    """The security-critical case: an intruder must not be able to
    delete another user's connection by guessing the workspace ID."""

    def test_intruder_cannot_disconnect(
        self, name, model, builder, url_tpl, make_user,
    ):
        owner = make_user(plan="pro", email=f"disc-{name}-o@x.com")
        intruder = make_user(plan="pro", email=f"disc-{name}-i@x.com")
        ws = _workspace(owner, slug=f"disc-{name}-iso-ws")
        conn = builder(owner, ws)
        db.session.add(conn)
        db.session.commit()
        conn_id = conn.id

        r = _logged_in(intruder).post(
            url_tpl.format(cid=ws.id), follow_redirects=False,
        )
        assert r.status_code == 302
        # Owner's connection row preserved — the ownership filter
        # (user_id=effective_owner_id / current_user.id) protected it.
        assert model.query.filter_by(id=conn_id).first() is not None


@pytest.mark.parametrize(
    "name,model,builder,url_tpl", PLATFORMS, ids=_IDS,
)
class TestDisconnectAnonymous:

    def test_anonymous_redirected_to_login(
        self, name, model, builder, url_tpl, app_ctx, make_user,
    ):
        owner = make_user(plan="pro", email=f"disc-{name}-anon@x.com")
        ws = _workspace(owner, slug=f"disc-{name}-anon-ws")
        conn = builder(owner, ws)
        db.session.add(conn)
        db.session.commit()
        conn_id = conn.id

        c = flask_app.test_client()
        r = c.post(url_tpl.format(cid=ws.id), follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")
        # Row untouched — never reached the handler.
        assert model.query.filter_by(id=conn_id).first() is not None


# ---------------------------------------------------------------------------
# Team-member access (effective_owner_id paths only)
# ---------------------------------------------------------------------------

class TestTeamMemberCanDisconnect:
    """The non-Shopify routes scope on effective_owner_id(), so a
    team member acting on the owner's workspace SHOULD be able to
    disconnect (they share the owner's integrations). Shopify uses
    current_user.id so a member is blocked there — both behaviors
    are intentional and pinned here."""

    def test_team_member_disconnects_via_owner_scope(self, make_user):
        owner = make_user(plan="pro", email="disc-team-owner@x.com")
        member = make_user(plan="free", email="disc-team-member@x.com")
        member.team_owner_id = owner.id
        db.session.commit()
        ws = _workspace(owner, slug="disc-team-ws")
        conn = CalComConnection(
            user_id=owner.id, client_id=ws.id,
            api_key="k", username="acme",
        )
        db.session.add(conn)
        db.session.commit()
        conn_id = conn.id

        r = _logged_in(member).post(
            f"/integrations/calcom/{ws.id}/disconnect",
            follow_redirects=False,
        )
        assert r.status_code == 302
        # effective_owner_id() resolves the member to the owner, so
        # the owner-scoped delete succeeds.
        assert CalComConnection.query.filter_by(id=conn_id).first() is None

    def test_shopify_blocks_team_member(self, make_user):
        """Shopify disconnect filters on current_user.id AND does an
        explicit workspace.user_id == current_user.id check, so a
        team member CANNOT disconnect the owner's Shopify store.
        Pinned to lock in the asymmetry."""
        owner = make_user(plan="pro", email="disc-shop-owner@x.com")
        member = make_user(plan="free", email="disc-shop-member@x.com")
        member.team_owner_id = owner.id
        db.session.commit()
        ws = _workspace(owner, slug="disc-shop-team-ws")
        conn = ShopifyConnection(
            user_id=owner.id, client_id=ws.id,
            shop_domain="acme.myshopify.com", access_token="shpat_x",
        )
        db.session.add(conn)
        db.session.commit()
        conn_id = conn.id

        r = _logged_in(member).post(
            f"/integrations/shopify/disconnect/{ws.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Workspace-ownership check fails for the member → bounced to
        # index, connection preserved.
        assert ShopifyConnection.query.filter_by(id=conn_id).first() is not None
