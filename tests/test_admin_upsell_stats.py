"""
Tests for /admin/upsell-stats and the compute_upsell_funnel_stats
helper that backs it.

Two layers under test:

1. compute_upsell_funnel_stats() — pure SQL aggregation. Verify
   the dict shape and that the counts match what we seeded.
2. /admin/upsell-stats route — access control + window param
   handling + template render smoke test.

End-to-end funnel transitions (none → shown → accepted, etc.)
are covered by test_upsell_lto.py and test_upsell_conversion.py;
this surface is just the read-side aggregator.
"""

from __future__ import annotations

from datetime import timedelta

from app import (
    UPSELL_LTO_TTL_HOURS,
    UPSELL_PROMPT_THRESHOLD,
    compute_upsell_funnel_stats,
    db,
)
from app import app as flask_app
from dtutils import utcnow


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _seed_user(make_user, *, email, status, offered_days_ago=None,
               expires_in_hours=None, prompt_count=4):
    """Helper: create a Free user with the given LTO state seeded."""
    u = make_user(plan="free", email=email)
    u.upsell_lto_status = status
    u.upsell_prompt_count = prompt_count
    if offered_days_ago is not None:
        u.upsell_lto_offered_at = utcnow() - timedelta(days=offered_days_ago)
    if expires_in_hours is not None:
        u.upsell_lto_expires_at = utcnow() + timedelta(hours=expires_in_hours)
    db.session.commit()
    return u


# ---------------------------------------------------------------------------
# compute_upsell_funnel_stats — aggregation correctness
# ---------------------------------------------------------------------------

class TestComputeStats:

    def test_empty_db_returns_zero_everything(self, app_ctx):
        out = compute_upsell_funnel_stats(days=30)
        assert out["qualified_count"] == 0
        assert out["decision_count"] == 0
        assert out["accepted_count"] == 0
        assert out["conversion_rate"] == 0.0
        assert out["recent_offers"] == []
        assert out["still_live"] == []
        # Every status key is present in the breakdown so the template
        # can render without KeyError.
        for k in ("none", "shown", "dismissed", "accepted", "expired"):
            assert k in out["status_counts"]
            assert out["status_counts"][k] == 0

    def test_status_counts_match_seeded(self, app_ctx, make_user):
        # 1 shown, 2 dismissed, 1 accepted, 1 expired. Plus 1 user
        # who never qualified (status=none — the make_user default).
        _seed_user(make_user, email="s1@x.com", status="shown",
                   offered_days_ago=1, expires_in_hours=12)
        _seed_user(make_user, email="d1@x.com", status="dismissed",
                   offered_days_ago=2)
        _seed_user(make_user, email="d2@x.com", status="dismissed",
                   offered_days_ago=3)
        _seed_user(make_user, email="a1@x.com", status="accepted",
                   offered_days_ago=4)
        _seed_user(make_user, email="e1@x.com", status="expired",
                   offered_days_ago=5)
        make_user(plan="free", email="none@x.com")  # status defaults

        out = compute_upsell_funnel_stats(days=30)
        assert out["status_counts"]["shown"] == 1
        assert out["status_counts"]["dismissed"] == 2
        assert out["status_counts"]["accepted"] == 1
        assert out["status_counts"]["expired"] == 1
        assert out["status_counts"]["none"] >= 1
        assert out["qualified_count"] == 5
        assert out["decision_count"] == 4
        assert out["accepted_count"] == 1
        # 1 accepted / 4 decisions = 25.0%
        assert out["conversion_rate"] == 25.0

    def test_conversion_rate_excludes_still_shown(self, app_ctx, make_user):
        """Users still in 'shown' state haven't decided yet — they
        must NOT be in the denominator of the conversion rate, or
        a popup that hasn't expired yet would tank the metric."""
        _seed_user(make_user, email="s1@x.com", status="shown",
                   offered_days_ago=0, expires_in_hours=12)
        _seed_user(make_user, email="a1@x.com", status="accepted",
                   offered_days_ago=1)
        out = compute_upsell_funnel_stats(days=30)
        # 1 accepted out of 1 decision = 100%.
        assert out["conversion_rate"] == 100.0

    def test_recent_offers_respects_window(self, app_ctx, make_user):
        _seed_user(make_user, email="recent@x.com", status="dismissed",
                   offered_days_ago=5)
        _seed_user(make_user, email="ancient@x.com", status="dismissed",
                   offered_days_ago=40)
        out = compute_upsell_funnel_stats(days=7)
        emails = [u.email for u in out["recent_offers"]]
        assert "recent@x.com" in emails
        assert "ancient@x.com" not in emails

    def test_recent_offers_ordered_newest_first(self, app_ctx, make_user):
        _seed_user(make_user, email="oldest@x.com", status="dismissed",
                   offered_days_ago=10)
        _seed_user(make_user, email="middle@x.com", status="dismissed",
                   offered_days_ago=5)
        _seed_user(make_user, email="newest@x.com", status="dismissed",
                   offered_days_ago=1)
        out = compute_upsell_funnel_stats(days=30)
        emails = [u.email for u in out["recent_offers"]]
        assert emails == ["newest@x.com", "middle@x.com", "oldest@x.com"]

    def test_still_live_only_shown_with_future_expiry(self, app_ctx, make_user):
        # In "shown" with future expiry — should appear.
        _seed_user(make_user, email="live@x.com", status="shown",
                   offered_days_ago=0, expires_in_hours=12)
        # "shown" but expires_at in the past — past TTL, shouldn't
        # appear (would be expired on next view).
        _seed_user(make_user, email="stale@x.com", status="shown",
                   offered_days_ago=2, expires_in_hours=-1)
        # Dismissed — terminal state, never "live".
        _seed_user(make_user, email="dis@x.com", status="dismissed",
                   offered_days_ago=0, expires_in_hours=12)

        out = compute_upsell_funnel_stats(days=30)
        emails = [r["email"] for r in out["still_live"]]
        assert emails == ["live@x.com"]
        # And the dict carries the hours_left + minutes_left for the
        # template to render the countdown directly.
        live = out["still_live"][0]
        assert live["hours_left"] >= 0
        assert "minutes_left" in live
        assert live["prompt_count"] == 4

    def test_still_live_closest_to_expiry_first(self, app_ctx, make_user):
        _seed_user(make_user, email="far@x.com", status="shown",
                   offered_days_ago=0, expires_in_hours=20)
        _seed_user(make_user, email="close@x.com", status="shown",
                   offered_days_ago=0, expires_in_hours=2)
        _seed_user(make_user, email="medium@x.com", status="shown",
                   offered_days_ago=0, expires_in_hours=10)
        out = compute_upsell_funnel_stats(days=30)
        emails = [r["email"] for r in out["still_live"]]
        # Sorted by expires_at ascending → closest to expiry first.
        assert emails == ["close@x.com", "medium@x.com", "far@x.com"]


# ---------------------------------------------------------------------------
# /admin/upsell-stats — access control + window param
# ---------------------------------------------------------------------------

class TestAdminUpsellStatsRoute:

    def test_anonymous_redirected_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.get("/admin/upsell-stats", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_non_admin_user_gets_403(self, app_ctx, make_user):
        regular = make_user(role="user", email="ups-non-admin@x.com")
        r = _logged_in(regular).get(
            "/admin/upsell-stats", follow_redirects=False,
        )
        assert r.status_code == 403

    def test_admin_gets_200_renders(self, app_ctx, make_user):
        admin = make_user(role="admin", email="ups-admin-1@x.com")
        # Seed at least one row in each meaningful state so the
        # template renders all branches.
        _seed_user(make_user, email="ups-shown@x.com", status="shown",
                   offered_days_ago=0, expires_in_hours=12)
        _seed_user(make_user, email="ups-accepted@x.com", status="accepted",
                   offered_days_ago=1)

        r = _logged_in(admin).get("/admin/upsell-stats")
        assert r.status_code == 200
        # Headline tiles render.
        assert b"Total qualified" in r.data
        assert b"Conversion rate" in r.data
        # The seeded "still live" user surfaces in the live section.
        assert b"ups-shown@x.com" in r.data

    def test_default_window_is_30_days(self, app_ctx, make_user):
        admin = make_user(role="admin", email="ups-admin-2@x.com")
        r = _logged_in(admin).get("/admin/upsell-stats")
        assert r.status_code == 200
        # Window heading reflects 30 days.
        assert b"last 30d" in r.data

    def test_days_param_clamped(self, app_ctx, make_user):
        """A user passing days=99999 shouldn't trigger a full-table
        scan over forever-ago data. Clamp to [1, 365]."""
        admin = make_user(role="admin", email="ups-admin-3@x.com")
        r = _logged_in(admin).get("/admin/upsell-stats?days=99999")
        assert r.status_code == 200
        assert b"last 365d" in r.data

    def test_garbage_days_param_falls_back_to_default(
        self, app_ctx, make_user,
    ):
        admin = make_user(role="admin", email="ups-admin-4@x.com")
        r = _logged_in(admin).get("/admin/upsell-stats?days=banana")
        assert r.status_code == 200
        assert b"last 30d" in r.data

    def test_threshold_and_ttl_in_header(self, app_ctx, make_user):
        """The page header surfaces the active threshold/TTL constants
        so an admin tuning the constants doesn't have to dig in code
        to confirm what's live."""
        admin = make_user(role="admin", email="ups-admin-5@x.com")
        r = _logged_in(admin).get("/admin/upsell-stats")
        assert r.status_code == 200
        assert str(UPSELL_PROMPT_THRESHOLD).encode() in r.data
        assert str(UPSELL_LTO_TTL_HOURS).encode() in r.data
