"""
Tests for marketplace-audit CRUD routes (list / add / delete).

The /run route is already covered by:
  - test_marketplace_audit_cooldown.py (Free tier cooldown)
  - test_action_routes.py::TestMarketplaceRunAudit (credit flow)

This file fills the remaining gap: the surrounding CRUD that
creates the MarketplacePresence rows /run operates on.

Routes covered:
  GET  /marketplace-audits/<client_id>              list presences
  POST /marketplace-audits/<client_id>/add          link a presence
  POST /marketplace-audits/<client_id>/delete/<id>  remove a presence

Key things to lock down:
  - User isolation: workspace + presence rows owned by another
    user are invisible (404-style flash + redirect to index, not
    a hard 404 — the route's existing convention)
  - Input whitelist on /add: only the 5 enumerated marketplaces
    (etsy/amazon/shopee/ebay/other) accepted. tiktok_shop and
    lazada are in the UI dropdown but currently NOT in the
    handler's allow-list — that gap is locked in by a test so a
    future widening goes through code review.
  - shop_url required: form rejects empty URL with a flash
  - Delete is silent on not-found (no abort, just redirect) —
    matches the route's existing UX
"""

from __future__ import annotations

from app import Client, MarketplacePresence, db
from app import app as flask_app


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _workspace(user, slug="mp-ws"):
    ws = Client(
        slug=slug,
        user_id=user.id,
        name="Acme Shop",
        website="https://acme.example.com",
        website_normalized="acme.example.com",
        industry="Retail",
        location="SG",
    )
    db.session.add(ws)
    db.session.commit()
    return ws


def _presence(user, workspace, **overrides):
    p = MarketplacePresence(
        user_id=user.id,
        client_id=workspace.id,
        marketplace=overrides.get("marketplace", "etsy"),
        shop_name=overrides.get("shop_name", "Acme on Etsy"),
        shop_url=overrides.get("shop_url", "https://www.etsy.com/shop/acme"),
        category=overrides.get("category", "vintage"),
        region=overrides.get("region", "US"),
    )
    db.session.add(p)
    db.session.commit()
    return p


# ---------------------------------------------------------------------------
# GET /marketplace-audits/<client_id>
# ---------------------------------------------------------------------------

class TestMarketplaceListPage:

    def test_owner_can_view_list(self, make_user):
        u = make_user(plan="pro", email="mp-list-ok@x.com")
        ws = _workspace(u)
        _presence(u, ws, shop_name="Acme Etsy")
        _presence(u, ws, marketplace="amazon",
                  shop_name="Acme Amazon",
                  shop_url="https://amazon.com/shops/acme")

        r = _logged_in(u).get(f"/marketplace-audits/{ws.id}")
        assert r.status_code == 200
        assert b"Acme Etsy" in r.data
        assert b"Acme Amazon" in r.data

    def test_other_users_workspace_redirects_home(self, make_user):
        """Workspace-not-found / not-yours path flashes and redirects
        to /, not 404. Locks in the existing UX so a future change
        to a hard 404 has to update the test."""
        owner = make_user(plan="pro", email="mp-list-owner@x.com")
        intruder = make_user(plan="pro", email="mp-list-intruder@x.com")
        ws = _workspace(owner, slug="mp-owner-ws")
        # Owner has a presence the intruder shouldn't see.
        _presence(owner, ws, shop_name="Owner's secret shop")

        r = _logged_in(intruder).get(
            f"/marketplace-audits/{ws.id}", follow_redirects=False,
        )
        assert r.status_code == 302
        # Owner's presence not leaked in the response (the redirect
        # body is short; just confirms location).
        loc = r.headers.get("Location") or ""
        assert "/" in loc

    def test_isolation_filters_to_current_user(self, make_user):
        """Even when the user has a legitimate workspace, the list
        only shows THEIR presences — never another user's, even on
        the same workspace_id (defense in depth)."""
        u = make_user(plan="pro", email="mp-iso@x.com")
        ws = _workspace(u)
        # Plant a stale presence row from a "ghost" user with the
        # same client_id (simulates a bug or migration leftover).
        ghost = make_user(plan="pro", email="ghost@x.com")
        db.session.add(MarketplacePresence(
            user_id=ghost.id,
            client_id=ws.id,
            marketplace="etsy",
            shop_name="SHOULD NOT APPEAR",
            shop_url="https://etsy.com/ghost",
        ))
        db.session.commit()
        # The user's own presence.
        _presence(u, ws, shop_name="visible-shop")

        r = _logged_in(u).get(f"/marketplace-audits/{ws.id}")
        assert b"visible-shop" in r.data
        assert b"SHOULD NOT APPEAR" not in r.data


# ---------------------------------------------------------------------------
# POST /marketplace-audits/<client_id>/add
# ---------------------------------------------------------------------------

class TestMarketplaceAdd:

    def test_owner_adds_valid_presence(self, make_user):
        u = make_user(plan="pro", email="mp-add-ok@x.com")
        ws = _workspace(u)

        r = _logged_in(u).post(
            f"/marketplace-audits/{ws.id}/add",
            data={
                "marketplace": "etsy",
                "shop_url": "https://www.etsy.com/shop/new",
                "shop_name": "New Shop",
                "category": "vintage",
                "region": "US",
            },
            follow_redirects=False,
        )
        assert r.status_code == 302
        rows = MarketplacePresence.query.filter_by(
            user_id=u.id, client_id=ws.id,
        ).all()
        assert len(rows) == 1
        assert rows[0].marketplace == "etsy"
        assert rows[0].shop_name == "New Shop"
        assert rows[0].region == "US"

    def test_unknown_marketplace_rejected(self, make_user):
        """Allow-list is etsy/amazon/shopee/ebay/other. Anything
        else is rejected — no row created."""
        u = make_user(plan="pro", email="mp-add-bogus@x.com")
        ws = _workspace(u)
        _logged_in(u).post(
            f"/marketplace-audits/{ws.id}/add",
            data={
                "marketplace": "not_a_marketplace",
                "shop_url": "https://x.com/shop",
            },
        )
        assert MarketplacePresence.query.count() == 0

    def test_tiktok_lazada_currently_rejected_by_handler(self, make_user):
        """UI dropdown includes tiktok_shop and lazada, but the
        handler's whitelist doesn't — submitting them gets bounced.
        Locks in the current mismatch so a future widening goes
        through code review."""
        u = make_user(plan="pro", email="mp-add-tt@x.com")
        ws = _workspace(u)
        _logged_in(u).post(
            f"/marketplace-audits/{ws.id}/add",
            data={
                "marketplace": "tiktok_shop",
                "shop_url": "https://tiktok.com/@acme",
            },
        )
        assert MarketplacePresence.query.count() == 0

    def test_missing_shop_url_rejected(self, make_user):
        u = make_user(plan="pro", email="mp-add-nourl@x.com")
        ws = _workspace(u)
        _logged_in(u).post(
            f"/marketplace-audits/{ws.id}/add",
            data={"marketplace": "etsy", "shop_url": ""},
        )
        assert MarketplacePresence.query.count() == 0

    def test_optional_fields_stored_as_none_when_blank(self, make_user):
        """shop_name, category, region all optional — blank string
        from the form should store as NULL, not empty string,
        so downstream code can rely on a single null check."""
        u = make_user(plan="pro", email="mp-add-blank@x.com")
        ws = _workspace(u)
        _logged_in(u).post(
            f"/marketplace-audits/{ws.id}/add",
            data={
                "marketplace": "etsy",
                "shop_url": "https://www.etsy.com/shop/min",
                "shop_name": "",
                "category": "",
                "region": "",
            },
        )
        row = MarketplacePresence.query.filter_by(user_id=u.id).first()
        assert row is not None
        assert row.shop_name is None
        assert row.category is None
        assert row.region is None

    def test_other_users_workspace_blocked(self, make_user):
        owner = make_user(plan="pro", email="mp-add-owner@x.com")
        intruder = make_user(plan="pro", email="mp-add-intruder@x.com")
        ws = _workspace(owner, slug="mp-add-owner-ws")

        _logged_in(intruder).post(
            f"/marketplace-audits/{ws.id}/add",
            data={
                "marketplace": "etsy",
                "shop_url": "https://www.etsy.com/shop/x",
            },
            follow_redirects=False,
        )
        # No presence created for the intruder OR the owner.
        assert MarketplacePresence.query.count() == 0


# ---------------------------------------------------------------------------
# POST /marketplace-audits/<client_id>/delete/<presence_id>
# ---------------------------------------------------------------------------

class TestMarketplaceDelete:

    def test_owner_deletes_presence(self, make_user):
        u = make_user(plan="pro", email="mp-del-ok@x.com")
        ws = _workspace(u)
        p = _presence(u, ws)

        r = _logged_in(u).post(
            f"/marketplace-audits/{ws.id}/delete/{p.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert MarketplacePresence.query.filter_by(id=p.id).first() is None

    def test_unknown_presence_redirects_silently(self, make_user):
        """Route's existing UX is to redirect quietly on miss — no
        flash, no abort. Lock that in."""
        u = make_user(plan="pro", email="mp-del-404@x.com")
        ws = _workspace(u)
        r = _logged_in(u).post(
            f"/marketplace-audits/{ws.id}/delete/999999",
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_other_users_presence_cannot_be_deleted(self, make_user):
        """Intruder posting against another user's presence ID:
        silent no-op, row preserved."""
        owner = make_user(plan="pro", email="mp-del-owner@x.com")
        intruder = make_user(plan="pro", email="mp-del-intruder@x.com")
        ws = _workspace(owner, slug="mp-del-owner-ws")
        p = _presence(owner, ws)

        _logged_in(intruder).post(
            f"/marketplace-audits/{ws.id}/delete/{p.id}",
            follow_redirects=False,
        )
        # Owner's row still exists.
        assert MarketplacePresence.query.filter_by(id=p.id).first() is not None

    def test_presence_from_different_workspace_cannot_be_deleted(
        self, make_user,
    ):
        """Presence row belongs to one workspace; route validates the
        (client_id, presence_id) pair. Submitting the right presence
        ID under the wrong workspace ID must not delete."""
        u = make_user(plan="pro", email="mp-del-xws@x.com")
        ws_a = _workspace(u, slug="mp-del-a")
        ws_b = _workspace(u, slug="mp-del-b")
        p_on_a = _presence(u, ws_a)

        _logged_in(u).post(
            f"/marketplace-audits/{ws_b.id}/delete/{p_on_a.id}",
            follow_redirects=False,
        )
        # Row still on ws_a.
        assert MarketplacePresence.query.filter_by(id=p_on_a.id).first() is not None


# ---------------------------------------------------------------------------
# Anonymous access
# ---------------------------------------------------------------------------

class TestAnonymousAccess:

    def test_list_redirects_to_login(self, app_ctx, make_user):
        u = make_user(plan="pro", email="mp-anon-list@x.com")
        ws = _workspace(u)
        c = flask_app.test_client()
        r = c.get(f"/marketplace-audits/{ws.id}", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_add_redirects_to_login(self, app_ctx, make_user):
        u = make_user(plan="pro", email="mp-anon-add@x.com")
        ws = _workspace(u)
        c = flask_app.test_client()
        r = c.post(
            f"/marketplace-audits/{ws.id}/add",
            data={"marketplace": "etsy", "shop_url": "https://x.com"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_delete_redirects_to_login(self, app_ctx, make_user):
        u = make_user(plan="pro", email="mp-anon-del@x.com")
        ws = _workspace(u)
        p = _presence(u, ws)
        c = flask_app.test_client()
        r = c.post(
            f"/marketplace-audits/{ws.id}/delete/{p.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")
