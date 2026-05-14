"""
Tests for the Google Search Console plan gate.

The connector is marketed and priced as a Pro/Growth feature
(`plan_allows_google_search_console`). Before this PR, only the
dashboard route (`GET /integrations/gsc/<id>`) enforced the gate;
the three mutating routes did not:

  - `/integrations/gsc/callback`        OAuth completion
  - `/integrations/gsc/<id>/select-site`  property picker
  - `/integrations/gsc/<id>/sync`         force-refresh

That meant a Pro user who connected GSC, then downgraded to Free,
could keep using the connection forever by hitting the POST routes
directly (URL bookmark, scripted call, browser-extension that
auto-syncs, etc.) even though the dashboard hid the controls.

Each test exercises:
  - The route succeeds (or at least doesn't 302 to /pricing) for a
    Pro user — feature still works for paying customers
  - The same route bounces a Free user to /pricing with no DB-level
    side effects — leak closed

`/integrations/gsc/<id>/disconnect` intentionally stays ungated:
a downgraded user should always be able to sever the link.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from app import (
    Client,
    GoogleSearchConsoleConnection,
    db,
)
from app import app as flask_app
from dtutils import utcnow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace_for(make_user):
    """Factory: return (user, workspace) for a given plan."""
    def _make(plan, *, email):
        u = make_user(plan=plan, email=email)
        ws = Client(
            slug=f"gsc-ws-{u.id}",
            user_id=u.id,
            name=f"WS for {u.email}",
            website="https://gsc.example.com",
            website_normalized="gsc.example.com",
            industry="SaaS",
            location="SG",
        )
        db.session.add(ws)
        db.session.commit()
        return u, ws
    return _make


@pytest.fixture
def connected_workspace(workspace_for):
    """Factory: return (user, workspace, connection) for a given plan."""
    def _make(plan, *, email, with_site=True):
        u, ws = workspace_for(plan, email=email)
        conn = GoogleSearchConsoleConnection(
            user_id=u.id,
            client_id=ws.id,
            site_url=("https://gsc.example.com/" if with_site else None),
            access_token="fake-access-token",
            refresh_token="fake-refresh-token",
            token_expires_at=utcnow() + timedelta(hours=1),
            scope="https://www.googleapis.com/auth/webmasters.readonly",
        )
        db.session.add(conn)
        db.session.commit()
        return u, ws, conn
    return _make


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


# ---------------------------------------------------------------------------
# /integrations/gsc/<id>/select-site
# ---------------------------------------------------------------------------

class TestSelectSiteGate:
    """The property picker — first POST after OAuth completes."""

    def test_pro_user_can_select_site(self, connected_workspace):
        u, ws, conn = connected_workspace("pro", email="gsc-select-pro@x.com")
        with patch("app._refresh_gsc_payload", return_value={"clicks": 0}):
            r = _logged_in(u).post(
                f"/integrations/gsc/{ws.id}/select-site",
                data={"site_url": "https://chosen.example.com/"},
                follow_redirects=False,
            )
        assert r.status_code == 302
        # Bounces back to the GSC dashboard, NOT /pricing.
        assert "/integrations/gsc" in (r.headers.get("Location") or "")
        db.session.refresh(conn)
        assert conn.site_url == "https://chosen.example.com/"

    def test_free_user_redirected_to_pricing(self, connected_workspace):
        """The Pro→Free downgrade leak: connection row exists from
        when the user was Pro; on Free, POST /select-site must
        bounce with no DB change."""
        u, ws, conn = connected_workspace(
            "free", email="gsc-select-free@x.com", with_site=True,
        )
        original_site = conn.site_url

        r = _logged_in(u).post(
            f"/integrations/gsc/{ws.id}/select-site",
            data={"site_url": "https://newly-picked.example.com/"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")
        # Site URL untouched.
        db.session.refresh(conn)
        assert conn.site_url == original_site

    def test_growth_user_can_select_site(self, connected_workspace):
        u, ws, conn = connected_workspace(
            "growth", email="gsc-select-growth@x.com",
        )
        with patch("app._refresh_gsc_payload", return_value={}):
            r = _logged_in(u).post(
                f"/integrations/gsc/{ws.id}/select-site",
                data={"site_url": "https://growth.example.com/"},
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert "/pricing" not in (r.headers.get("Location") or "")


# ---------------------------------------------------------------------------
# /integrations/gsc/<id>/sync
# ---------------------------------------------------------------------------

class TestSyncGate:
    """The force-refresh route. Same leak shape as select-site."""

    def test_pro_user_can_sync(self, connected_workspace):
        u, ws, conn = connected_workspace("pro", email="gsc-sync-pro@x.com")
        called = {"n": 0}

        def _spy(conn):
            called["n"] += 1
            return {"clicks": 42}

        with patch("app._refresh_gsc_payload", side_effect=_spy):
            r = _logged_in(u).post(
                f"/integrations/gsc/{ws.id}/sync",
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert "/pricing" not in (r.headers.get("Location") or "")
        assert called["n"] == 1

    def test_free_user_redirected_no_sync_runs(self, connected_workspace):
        u, ws, conn = connected_workspace("free", email="gsc-sync-free@x.com")
        called = {"n": 0}

        def _spy(conn):
            called["n"] += 1
            return {}

        with patch("app._refresh_gsc_payload", side_effect=_spy):
            r = _logged_in(u).post(
                f"/integrations/gsc/{ws.id}/sync",
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")
        # Critical: the gate fires BEFORE _refresh_gsc_payload is
        # called, so the Free user can't burn Google API quota.
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# /integrations/gsc/callback
# ---------------------------------------------------------------------------

class TestOAuthCallbackGate:
    """Edge case: user starts OAuth on Pro, downgrades while
    bouncing through Google, then returns. The callback must
    refuse to persist the new connection."""

    def test_free_user_oauth_callback_bounces(self, workspace_for):
        """Free user hits the callback (e.g. session survived a
        downgrade) — should bounce to /pricing without persisting
        a connection row. Also clears the OAuth session state so
        a retry doesn't loop on stale data."""
        u, ws = workspace_for("free", email="gsc-cb-free@x.com")

        c = _logged_in(u)
        # Plant the session state /connect would have set, then hit
        # the callback as Google would.
        with c.session_transaction() as s:
            s["gsc_oauth_state"] = "abc123"
            s["gsc_oauth_client_id"] = ws.id

        r = c.get(
            "/integrations/gsc/callback?state=abc123&code=fake",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")

        # No connection row was created.
        assert GoogleSearchConsoleConnection.query.filter_by(
            user_id=u.id, client_id=ws.id,
        ).first() is None

        # And the session state was cleared so a retry doesn't loop.
        with c.session_transaction() as s:
            assert "gsc_oauth_state" not in s
            assert "gsc_oauth_client_id" not in s


# ---------------------------------------------------------------------------
# Disconnect intentionally stays ungated
# ---------------------------------------------------------------------------

class TestDisconnectStaysOpen:
    """Disconnecting must work for ANY plan — users on Free should
    be able to clean up a stale Pro-era connection row."""

    def test_free_user_can_disconnect(self, connected_workspace):
        u, ws, conn = connected_workspace(
            "free", email="gsc-disconnect-free@x.com",
        )
        r = _logged_in(u).post(
            f"/integrations/gsc/{ws.id}/disconnect",
            follow_redirects=False,
        )
        assert r.status_code == 302
        # Bounces back to the workspace detail, NOT /pricing.
        assert "/pricing" not in (r.headers.get("Location") or "")
        # Connection row removed.
        assert GoogleSearchConsoleConnection.query.filter_by(
            user_id=u.id, client_id=ws.id,
        ).first() is None


# ---------------------------------------------------------------------------
# /integrations/ga/<id>/select-property and /sync
# ---------------------------------------------------------------------------
# GA4 piggybacks on the GSC OAuth grant so the dashboard, select,
# and sync routes share the same paid-plan policy. Same leak shape
# as the GSC routes above — fix mirrors fix.

class TestGASelectPropertyGate:

    def test_pro_user_can_select_property(self, connected_workspace):
        u, ws, conn = connected_workspace("pro", email="ga-select-pro@x.com")
        with patch(
            "services.ga_client.summarize_property",
            return_value={"users": 100},
        ), patch("services.ga_client.GA4Client"):
            r = _logged_in(u).post(
                f"/integrations/ga/{ws.id}/select-property",
                data={"property_id": "properties/12345"},
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert "/pricing" not in (r.headers.get("Location") or "")
        db.session.refresh(conn)
        assert conn.ga_property_id == "properties/12345"

    def test_free_user_redirected_no_property_saved(self, connected_workspace):
        u, ws, conn = connected_workspace(
            "free", email="ga-select-free@x.com",
        )
        original_property = conn.ga_property_id

        r = _logged_in(u).post(
            f"/integrations/ga/{ws.id}/select-property",
            data={"property_id": "properties/99999"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")
        db.session.refresh(conn)
        assert conn.ga_property_id == original_property


class TestGASyncGate:

    def test_pro_user_can_sync(self, connected_workspace):
        u, ws, conn = connected_workspace("pro", email="ga-sync-pro@x.com")
        conn.ga_property_id = "properties/12345"
        db.session.commit()

        called = {"n": 0}

        def _spy(*args, **kwargs):
            called["n"] += 1
            return {"users": 250}

        with patch("services.ga_client.summarize_property", side_effect=_spy), \
             patch("services.ga_client.GA4Client"):
            r = _logged_in(u).post(
                f"/integrations/ga/{ws.id}/sync",
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert "/pricing" not in (r.headers.get("Location") or "")
        assert called["n"] == 1

    def test_free_user_redirected_no_sync_runs(self, connected_workspace):
        u, ws, conn = connected_workspace("free", email="ga-sync-free@x.com")
        conn.ga_property_id = "properties/12345"
        db.session.commit()
        called = {"n": 0}

        def _spy(*args, **kwargs):
            called["n"] += 1
            return {}

        with patch("services.ga_client.summarize_property", side_effect=_spy):
            r = _logged_in(u).post(
                f"/integrations/ga/{ws.id}/sync",
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert "/pricing" in (r.headers.get("Location") or "")
        # Gate fires before _ensure_gsc_access_token + GA4Client calls,
        # so the Free user can't burn Google API quota.
        assert called["n"] == 0


class TestGADisconnectStaysOpen:

    def test_free_user_can_ga_disconnect(self, connected_workspace):
        u, ws, conn = connected_workspace(
            "free", email="ga-disconnect-free@x.com",
        )
        conn.ga_property_id = "properties/12345"
        db.session.commit()

        r = _logged_in(u).post(
            f"/integrations/ga/{ws.id}/disconnect",
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/pricing" not in (r.headers.get("Location") or "")
        # GA half cleared; GSC half intact.
        db.session.refresh(conn)
        assert conn.ga_property_id is None
        assert conn.site_url is not None
