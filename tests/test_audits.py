"""
Tests for the Audit SQL model and the helpers that read/write it.

Pins the contract after migrating from outputs/*.json:
    1. save_audit_results inserts a row, returns the legacy
       {"full_file", "summary_file"} shape
    2. get_saved_audits returns the same dict shape as the JSON era
       (so templates / sort / filter helpers still work)
    3. user_id scoping is a SQL WHERE, not a Python skip
    4. read_full_audit_data returns the full payload by filename;
       None for unknown filenames
    5. save_audit_payload (the schema-v2 writer in audit_schema.py)
       round-trips through the SAME table — both writers can
       coexist on the same surface
"""

from __future__ import annotations

import pytest

from app import Audit, db, get_saved_audits, read_full_audit_data
from save_results import save_audit_results


# ---------------------------------------------------------------------------
# save_audit_results — the legacy writer used by main.py + audit_runner
# ---------------------------------------------------------------------------

class TestSaveAuditResults:
    def _save(self, user_id, *, website="https://example.com", audit_type="quick"):
        return save_audit_results(
            website=website,
            audit_type=audit_type,
            business_profile={"title": "Test Co", "description": "x"},
            visibility_data={
                "queries_tested": 3,
                "appearances": 1,
                "average_query_score": 7.5,
                "visibility_score": 7.5,
            },
            ai_answer_results=[{"query": "x", "score": 12}],
            competitor_data={"direct_competitors": [("competitor.com", 3)]},
            content_gaps=["gap 1"],
            question_coverage={"results": [], "pages_used": []},
            audit_data={"content_score": 10, "schema_score": 5},
            final_report={
                "raw_score": 22,
                "normalized_score": 40.0,
                "summary": "Moderate",
                "verdict": "MODERATE",
                "report_text": "1. Do X\n2. Do Y",
            },
            client_id="test-client",
            client_name="Test Co",
            user_id=user_id,
        )

    def test_returns_filename_pair(self, user):
        result = self._save(user.id)
        # Legacy shape — two string filenames, matching the prior
        # disk-path return values.
        assert isinstance(result["full_file"], str)
        assert isinstance(result["summary_file"], str)
        assert result["full_file"].endswith("_full.json")
        assert result["summary_file"].endswith("_summary.json")

    def test_persists_audit_row(self, user):
        result = self._save(user.id)
        row = db.session.get(Audit, result["summary_file"])
        assert row is not None
        assert row.user_id == user.id
        assert row.website == "https://example.com"
        assert row.audit_type == "quick"

    def test_denormalized_scores_populated(self, user):
        """Score columns should be filled in from the final_report so
        the audit-history list page can sort without unpacking JSON."""
        result = self._save(user.id)
        row = db.session.get(Audit, result["summary_file"])
        # normalized_score came from final_report["normalized_score"]
        # via build_client_summary → scores.normalized_score
        assert row.normalized_score == 40.0

    def test_full_payload_round_trips(self, user):
        result = self._save(user.id)
        full = read_full_audit_data(result["summary_file"])
        assert full is not None
        assert full["website"] == "https://example.com"
        # The full payload includes everything passed in
        assert full["audit_data"]["content_score"] == 10
        assert full["ai_answer_results"][0]["query"] == "x"


# ---------------------------------------------------------------------------
# get_saved_audits — read path
# ---------------------------------------------------------------------------

class TestGetSavedAudits:
    def _seed(self, user_id, n=3):
        for i in range(n):
            save_audit_results(
                website=f"https://site-{i}.example.com",
                audit_type="quick",
                business_profile={},
                visibility_data={},
                ai_answer_results=[],
                competitor_data={},
                content_gaps=[],
                question_coverage={},
                audit_data={},
                final_report={
                    "raw_score": 0,
                    "normalized_score": float(50 + i),
                    "summary": "",
                    "verdict": "",
                    "report_text": "",
                },
                client_id=f"c-{i}",
                client_name=f"Client {i}",
                user_id=user_id,
            )

    def test_no_filter_returns_all(self, app_ctx, make_user):
        a = make_user(email="aud-a@x.com")
        b = make_user(email="aud-b@x.com")
        self._seed(a.id, n=2)
        self._seed(b.id, n=3)

        # No user_id filter → both users' audits
        assert len(get_saved_audits()) == 5

    def test_user_id_scoping(self, app_ctx, make_user):
        a = make_user(email="aud-a2@x.com")
        b = make_user(email="aud-b2@x.com")
        self._seed(a.id, n=2)
        self._seed(b.id, n=3)

        assert len(get_saved_audits(user_id=a.id)) == 2
        assert len(get_saved_audits(user_id=b.id)) == 3

    def test_returns_dict_shape(self, user):
        self._seed(user.id, n=1)
        rows = get_saved_audits(user_id=user.id)
        # Every key the templates / filter / sort helpers read
        for k in (
            "filename", "website", "website_normalized", "client_id",
            "client_name", "audit_type", "saved_at", "verdict",
            "opportunity_level", "normalized_score", "visibility_score",
            "content_score", "schema_score", "scores", "summary",
            "visibility_snapshot", "top_competitors", "top_content_gaps",
            "top_recommendations",
        ):
            assert k in rows[0]

    def test_newest_first(self, user):
        import time
        # Microsecond-resolution timestamps mean back-to-back saves
        # have different saved_at values, but tiny sleeps still help
        # the sort comparison be unambiguous.
        save_audit_results(
            website="https://first.example.com", audit_type="quick",
            business_profile={}, visibility_data={}, ai_answer_results=[],
            competitor_data={}, content_gaps=[], question_coverage={},
            audit_data={}, final_report={"normalized_score": 50.0,
                "raw_score": 0, "summary": "", "verdict": "", "report_text": ""},
            user_id=user.id,
        )
        time.sleep(0.01)
        save_audit_results(
            website="https://second.example.com", audit_type="quick",
            business_profile={}, visibility_data={}, ai_answer_results=[],
            competitor_data={}, content_gaps=[], question_coverage={},
            audit_data={}, final_report={"normalized_score": 60.0,
                "raw_score": 0, "summary": "", "verdict": "", "report_text": ""},
            user_id=user.id,
        )
        rows = get_saved_audits(user_id=user.id)
        # newest first
        assert rows[0]["website"] == "https://second.example.com"
        assert rows[1]["website"] == "https://first.example.com"


# ---------------------------------------------------------------------------
# read_full_audit_data
# ---------------------------------------------------------------------------

class TestReadFullAuditData:
    def test_unknown_filename_returns_none(self, app_ctx):
        assert read_full_audit_data("does-not-exist.json") is None

    def test_empty_filename_returns_none(self, app_ctx):
        assert read_full_audit_data("") is None
        assert read_full_audit_data(None) is None

    def test_round_trips_full_payload(self, user):
        result = save_audit_results(
            website="https://example.com", audit_type="quick",
            business_profile={"title": "X"},
            visibility_data={"score": 7},
            ai_answer_results=[],
            competitor_data={},
            content_gaps=["a", "b"],
            question_coverage={},
            audit_data={"foo": "bar"},
            final_report={
                "raw_score": 1, "normalized_score": 50.0,
                "summary": "", "verdict": "", "report_text": "",
            },
            user_id=user.id,
        )
        full = read_full_audit_data(result["summary_file"])
        assert full["website"] == "https://example.com"
        assert full["content_gaps"] == ["a", "b"]
        assert full["audit_data"]["foo"] == "bar"


# ---------------------------------------------------------------------------
# audit_schema.save_audit_payload — the schema-v2 writer
# ---------------------------------------------------------------------------

class TestSaveAuditPayloadSchemaV2:
    def test_round_trips_through_same_table(self, user):
        """Both writers (save_audit_results and save_audit_payload)
        write into the same audits table. Verify a row written by
        save_audit_payload is readable by get_saved_audits."""
        from audit_schema import save_audit_payload

        payload = {
            "website": "https://schema-v2.example.com",
            "audit_type": "full",
            "saved_at": "2026-05-14T10:00:00",
            "client_id": "v2-client",
            "client_name": "V2 Client",
            "user_id": user.id,
            "scores": {"normalized_score": 77.5, "visibility_score": 50},
            "summary": {"verdict": "STRONG", "opportunity_level": "LOW"},
            "recommended_actions": [],
            "content_opportunities": [],
            "meta": {"schema_version": "2.0"},
        }
        result = save_audit_payload(payload)
        assert result["summary_filename"].endswith("_summary.json")

        # Readable via the user-scoped query
        rows = get_saved_audits(user_id=user.id)
        match = [r for r in rows if r["filename"] == result["summary_filename"]]
        assert len(match) == 1
        assert match[0]["website"] == "https://schema-v2.example.com"
        # Denormalized score from the new column
        assert match[0]["normalized_score"] == 77.5

    def test_returns_legacy_path_shape(self, user):
        """The summary_path / full_path fields in the return dict
        are kept for back-compat with code that treats them as opaque
        identifiers — they should still be strings, even though no
        files exist on disk anymore."""
        from audit_schema import save_audit_payload

        payload = {
            "website": "https://x.example.com", "audit_type": "quick",
            "saved_at": "2026-05-14T10:00:00", "user_id": user.id,
            "scores": {}, "summary": {}, "meta": {},
        }
        result = save_audit_payload(payload)
        assert isinstance(result["summary_path"], str)
        assert isinstance(result["full_path"], str)
        assert isinstance(result["summary_filename"], str)
        assert isinstance(result["full_filename"], str)


# ---------------------------------------------------------------------------
# Score safety — malformed input shouldn't crash the save
# ---------------------------------------------------------------------------

class TestScoreCoercion:
    def test_save_results_with_missing_scores(self, user):
        """Older callers may pass a final_report without all four
        score fields. Each missing field should default to 0.0 in
        the denormalized columns."""
        result = save_audit_results(
            website="https://no-scores.example.com", audit_type="quick",
            business_profile={}, visibility_data={}, ai_answer_results=[],
            competitor_data={}, content_gaps=[], question_coverage={},
            audit_data={},
            # final_report missing visibility_score / content_score /
            # schema_score fields entirely
            final_report={"raw_score": 0, "summary": "", "verdict": "",
                          "report_text": ""},
            user_id=user.id,
        )
        row = db.session.get(Audit, result["summary_file"])
        assert row.normalized_score == 0.0
        assert row.visibility_score == 0.0
        assert row.content_score == 0.0
        assert row.schema_score == 0.0
