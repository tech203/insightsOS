"""
Tests for source-aware upsell attribution.

Three things to verify:

  1. record_upsell_prompt persists the source that finally tipped
     a Free user past the threshold. Earlier paywalls (calls below
     threshold) don't overwrite; only the qualifying call sets it.

  2. resolve_upsell_lto exposes both the raw source and a derived
     `headline` string the modal can render directly. Unknown
     sources fall back to a generic headline.

  3. compute_upsell_funnel_stats produces a per-source breakdown
     with the same conversion-rate math as the headline metric
     (accepted ÷ decided, excluding 'shown').

Plus a smoke test that the admin template renders the new section
without crashing.
"""

from __future__ import annotations

from datetime import timedelta

from app import (
    UPSELL_PROMPT_THRESHOLD,
    _upsell_headline_for_source,
    compute_upsell_funnel_stats,
    db,
    record_upsell_prompt,
    resolve_upsell_lto,
)
from app import app as flask_app
from dtutils import utcnow


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


# ---------------------------------------------------------------------------
# record_upsell_prompt persists source on qualification
# ---------------------------------------------------------------------------

class TestRecordPersistsSource:

    def test_qualifying_call_sets_source(self, make_user):
        u = make_user(plan="free", email="src-qualify@x.com")
        for _ in range(UPSELL_PROMPT_THRESHOLD):
            record_upsell_prompt(u, source="workspace_cap")
        db.session.refresh(u)
        assert u.upsell_lto_status == "shown"
        assert u.upsell_lto_source == "workspace_cap"

    def test_only_qualifying_source_is_stored(self, make_user):
        """If the user accumulated prompts from different paywalls,
        the SOURCE that finally tipped them is what we store —
        most actionable signal. Earlier sources are ignored."""
        u = make_user(plan="free", email="src-multi@x.com")
        record_upsell_prompt(u, source="workspace_cap")
        record_upsell_prompt(u, source="insufficient_credits")
        record_upsell_prompt(u, source="gsc_dashboard_gate")
        db.session.refresh(u)
        # Threshold is 3; the 3rd call (gsc) is the one that
        # crossed it, so source should be gsc_dashboard_gate.
        assert u.upsell_lto_status == "shown"
        assert u.upsell_lto_source == "gsc_dashboard_gate"

    def test_source_truncated_to_column_width(self, make_user):
        """Defensive: a future paywall site passing a long tag
        shouldn't blow up the INSERT. The column is 60 chars."""
        u = make_user(plan="free", email="src-long@x.com")
        long_tag = "extremely_long_source_tag_that_exceeds_the_column_width_by_a_lot_more_than_should_ever_be_needed"
        for _ in range(UPSELL_PROMPT_THRESHOLD):
            record_upsell_prompt(u, source=long_tag)
        db.session.refresh(u)
        assert u.upsell_lto_source is not None
        assert len(u.upsell_lto_source) <= 60

    def test_empty_source_stores_none(self, make_user):
        """A paywall site calling without a source tag (legacy code,
        bug) should result in NULL — better than empty string,
        easier to filter in admin views."""
        u = make_user(plan="free", email="src-empty@x.com")
        for _ in range(UPSELL_PROMPT_THRESHOLD):
            record_upsell_prompt(u, source="")
        db.session.refresh(u)
        assert u.upsell_lto_status == "shown"
        assert u.upsell_lto_source is None


# ---------------------------------------------------------------------------
# resolve_upsell_lto exposes source + headline
# ---------------------------------------------------------------------------

class TestResolverExposesSource:

    def _set_shown(self, u, source):
        now = utcnow()
        u.upsell_lto_status = "shown"
        u.upsell_lto_offered_at = now
        u.upsell_lto_expires_at = now + timedelta(hours=12)
        u.upsell_lto_source = source
        u.upsell_prompt_count = 4
        db.session.commit()

    def test_resolver_returns_source(self, make_user):
        u = make_user(plan="free", email="resv-src@x.com")
        self._set_shown(u, "workspace_cap")
        out = resolve_upsell_lto(u)
        assert out is not None
        assert out["source"] == "workspace_cap"

    def test_resolver_picks_source_aware_headline(self, make_user):
        u = make_user(plan="free", email="resv-headline@x.com")
        self._set_shown(u, "gsc_dashboard_gate")
        out = resolve_upsell_lto(u)
        assert out["headline"] == "Want Search Console data?"

    def test_resolver_falls_back_for_unknown_source(self, make_user):
        """A future / unmapped source string shouldn't crash —
        fall through to the generic headline."""
        u = make_user(plan="free", email="resv-unknown@x.com")
        self._set_shown(u, "future_paywall_tag")
        out = resolve_upsell_lto(u)
        assert out["headline"] == "Ready to unlock everything?"

    def test_resolver_falls_back_for_null_source(self, make_user):
        """Legacy rows from before the source column existed have
        upsell_lto_source = None — resolver must still produce a
        valid dict with the default headline."""
        u = make_user(plan="free", email="resv-null@x.com")
        self._set_shown(u, None)
        out = resolve_upsell_lto(u)
        assert out["source"] is None
        assert out["headline"] == "Ready to unlock everything?"


class TestHeadlineHelper:
    """The _upsell_headline_for_source helper directly. Pure
    function; easy to lock in the wired source → copy mapping."""

    def test_known_sources_have_distinct_copy(self):
        h1 = _upsell_headline_for_source("workspace_cap")
        h2 = _upsell_headline_for_source("insufficient_credits")
        h3 = _upsell_headline_for_source("white_label_settings_gate")
        assert h1 != h2 != h3
        assert "workspace" in h1.lower()
        assert "credits" in h2.lower()
        assert "white-label" in h3.lower()

    def test_unknown_source_returns_default(self):
        assert _upsell_headline_for_source("not_a_real_source") == \
               "Ready to unlock everything?"

    def test_empty_or_none_returns_default(self):
        assert _upsell_headline_for_source("") == \
               "Ready to unlock everything?"
        assert _upsell_headline_for_source(None) == \
               "Ready to unlock everything?"


# ---------------------------------------------------------------------------
# compute_upsell_funnel_stats — per-source breakdown
# ---------------------------------------------------------------------------

class TestStatsSourceBreakdown:

    def _seed(self, make_user, *, email, source, status, days_ago=1):
        u = make_user(plan="free", email=email)
        u.upsell_prompt_count = 4
        u.upsell_lto_status = status
        u.upsell_lto_source = source
        u.upsell_lto_offered_at = utcnow() - timedelta(days=days_ago)
        if status == "shown":
            u.upsell_lto_expires_at = utcnow() + timedelta(hours=12)
        db.session.commit()
        return u

    def test_empty_db_has_empty_source_breakdown(self, app_ctx):
        out = compute_upsell_funnel_stats(days=30)
        assert out["source_breakdown"] == {}

    def test_breakdown_groups_by_source(self, app_ctx, make_user):
        # workspace_cap: 2 accepted, 1 dismissed → 66.7% conversion
        self._seed(make_user, email="ws1@x.com",
                   source="workspace_cap", status="accepted")
        self._seed(make_user, email="ws2@x.com",
                   source="workspace_cap", status="accepted")
        self._seed(make_user, email="ws3@x.com",
                   source="workspace_cap", status="dismissed")
        # insufficient_credits: 1 accepted, 1 expired → 50% conversion
        self._seed(make_user, email="ic1@x.com",
                   source="insufficient_credits", status="accepted")
        self._seed(make_user, email="ic2@x.com",
                   source="insufficient_credits", status="expired")
        # gsc_dashboard_gate: 1 shown (not yet decided) → 0% conversion
        self._seed(make_user, email="g1@x.com",
                   source="gsc_dashboard_gate", status="shown",
                   days_ago=0)

        out = compute_upsell_funnel_stats(days=30)
        breakdown = out["source_breakdown"]

        assert breakdown["workspace_cap"]["qualified"] == 3
        assert breakdown["workspace_cap"]["accepted"] == 2
        assert breakdown["workspace_cap"]["dismissed"] == 1
        assert breakdown["workspace_cap"]["decided"] == 3
        # 2/3 = 66.7%
        assert breakdown["workspace_cap"]["conversion_rate"] == 66.7

        assert breakdown["insufficient_credits"]["qualified"] == 2
        assert breakdown["insufficient_credits"]["conversion_rate"] == 50.0

        # shown rows aren't in the denominator
        assert breakdown["gsc_dashboard_gate"]["qualified"] == 1
        assert breakdown["gsc_dashboard_gate"]["decided"] == 0
        assert breakdown["gsc_dashboard_gate"]["conversion_rate"] == 0.0

    def test_breakdown_ordered_by_qualified_count(self, app_ctx, make_user):
        # 1 from source_a, 3 from source_b. source_b should come first.
        self._seed(make_user, email="a1@x.com",
                   source="source_a", status="dismissed")
        for i in range(3):
            self._seed(make_user, email=f"b{i}@x.com",
                       source="source_b", status="dismissed")
        out = compute_upsell_funnel_stats(days=30)
        keys = list(out["source_breakdown"].keys())
        assert keys == ["source_b", "source_a"]

    def test_null_source_rows_excluded_from_breakdown(
        self, app_ctx, make_user,
    ):
        """Legacy rows (qualified before the column existed) have
        source=NULL — they appear in lifetime status counts but
        NOT in the per-source breakdown, which is appropriate
        (the breakdown is forward-looking)."""
        self._seed(make_user, email="legacy@x.com",
                   source=None, status="accepted")
        self._seed(make_user, email="modern@x.com",
                   source="workspace_cap", status="accepted")
        out = compute_upsell_funnel_stats(days=30)
        # Lifetime counts include both.
        assert out["accepted_count"] == 2
        # Per-source breakdown only the source-tagged one.
        assert list(out["source_breakdown"].keys()) == ["workspace_cap"]


# ---------------------------------------------------------------------------
# Admin template renders the new section
# ---------------------------------------------------------------------------

class TestAdminStatsTemplate:

    def test_source_section_visible_with_data(self, app_ctx, make_user):
        # Seed one source-tagged accepted row + one admin.
        admin = make_user(role="admin", email="adm-src@x.com")
        target = make_user(plan="free", email="tgt-src@x.com")
        target.upsell_prompt_count = 4
        target.upsell_lto_status = "accepted"
        target.upsell_lto_source = "marketplace_cooldown"
        target.upsell_lto_offered_at = utcnow() - timedelta(days=1)
        db.session.commit()

        r = _logged_in(admin).get("/admin/upsell-stats")
        assert r.status_code == 200
        assert b"Conversion by source" in r.data
        # Source tag visible in the table.
        assert b"marketplace_cooldown" in r.data

    def test_source_section_empty_state(self, app_ctx, make_user):
        """No source-tagged rows → friendly empty message instead
        of an empty table."""
        admin = make_user(role="admin", email="adm-src-empty@x.com")
        r = _logged_in(admin).get("/admin/upsell-stats")
        assert r.status_code == 200
        assert b"No source-tagged qualifications yet" in r.data
