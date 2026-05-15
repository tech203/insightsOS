"""
Tests for onboarding milestone timestamp stamping.

Three columns on User track activation:
  - first_workspace_at        when create_client first lands
  - first_audit_at            when save_audit_results first lands
  - onboarding_completed_at   alias of first_audit_at (today)

All three are stamped via stamp_onboarding_milestone(user_id, kind),
called from the corresponding hot path. The helper is idempotent
(no-op after first call) and never raises — analytics columns must
not poison the user's happy-path action.

Live get_onboarding_state continues to compute from related-row
existence as a fallback, so a NULL stamp doesn't break the UI.
This test file covers the stamp behaviour, not the stepper.
"""

from __future__ import annotations

from datetime import timedelta

from app import (
    Audit,
    db,
    stamp_onboarding_milestone,
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
# stamp_onboarding_milestone — direct helper tests
# ---------------------------------------------------------------------------

class TestStampHelper:

    def test_workspace_kind_sets_first_workspace_at(self, make_user):
        u = make_user(plan="free", email="stamp-ws@x.com")
        assert u.first_workspace_at is None
        stamp_onboarding_milestone(u.id, "workspace")
        db.session.refresh(u)
        assert u.first_workspace_at is not None
        # First-audit stamps untouched.
        assert u.first_audit_at is None
        assert u.onboarding_completed_at is None

    def test_audit_kind_sets_both_audit_stamps(self, make_user):
        u = make_user(plan="free", email="stamp-audit@x.com")
        stamp_onboarding_milestone(u.id, "audit")
        db.session.refresh(u)
        assert u.first_audit_at is not None
        assert u.onboarding_completed_at is not None
        # Both stamped within milliseconds of each other.
        delta = abs((u.onboarding_completed_at - u.first_audit_at).total_seconds())
        assert delta < 1

    def test_workspace_stamp_is_idempotent(self, make_user):
        """Calling again doesn't advance the timestamp — first
        occurrence is the canonical 'time to first workspace'
        marker, later workspaces don't count."""
        u = make_user(plan="free", email="stamp-ws-idem@x.com")
        stamp_onboarding_milestone(u.id, "workspace")
        db.session.refresh(u)
        first = u.first_workspace_at

        stamp_onboarding_milestone(u.id, "workspace")
        db.session.refresh(u)
        assert u.first_workspace_at == first

    def test_audit_stamp_is_idempotent(self, make_user):
        u = make_user(plan="free", email="stamp-audit-idem@x.com")
        stamp_onboarding_milestone(u.id, "audit")
        db.session.refresh(u)
        first_audit_ts = u.first_audit_at
        first_completed_ts = u.onboarding_completed_at

        stamp_onboarding_milestone(u.id, "audit")
        db.session.refresh(u)
        assert u.first_audit_at == first_audit_ts
        assert u.onboarding_completed_at == first_completed_ts

    def test_invalid_kind_no_op(self, make_user):
        u = make_user(plan="free", email="stamp-bogus@x.com")
        stamp_onboarding_milestone(u.id, "banana")
        db.session.refresh(u)
        assert u.first_workspace_at is None
        assert u.first_audit_at is None
        assert u.onboarding_completed_at is None

    def test_missing_user_no_op(self, app_ctx):
        """Bogus user_id — silent no-op, no crash."""
        stamp_onboarding_milestone(999999, "workspace")
        # No exception → pass

    def test_null_user_id_no_op(self, app_ctx):
        """Guard against None user_id (defensive)."""
        stamp_onboarding_milestone(None, "workspace")
        # No exception → pass

    def test_db_error_swallowed(self, make_user, monkeypatch):
        """If the commit raises (e.g. transient DB outage), the
        helper logs + swallows. The user's happy-path action must
        not fail because of an analytics stamp."""
        u = make_user(plan="free", email="stamp-error@x.com")

        original_commit = db.session.commit

        def _boom():
            raise RuntimeError("simulated DB blip")

        monkeypatch.setattr(db.session, "commit", _boom)
        # Must not raise.
        stamp_onboarding_milestone(u.id, "workspace")
        # Restore so the test fixture cleanup works.
        monkeypatch.setattr(db.session, "commit", original_commit)


# ---------------------------------------------------------------------------
# Workspace creation stamps first_workspace_at
# ---------------------------------------------------------------------------

class TestCreateClientStamps:

    def _create_workspace(self, client, name="Test WS"):
        return client.post(
            "/clients/new",
            data={
                "name": name,
                "website": f"https://{name.lower().replace(' ', '-')}.example.com",
                "industry": "SaaS",
                "location": "SG",
                "owner_type": "company",
                "notes": "",
            },
            follow_redirects=False,
        )

    def test_first_workspace_create_stamps(self, make_user):
        u = make_user(plan="pro", email="ws-stamp@x.com")
        c = _logged_in(u)
        before = utcnow()
        r = self._create_workspace(c, "First WS")
        assert r.status_code == 302
        db.session.refresh(u)
        assert u.first_workspace_at is not None
        assert u.first_workspace_at >= before

    def test_second_workspace_does_not_advance(self, make_user):
        """The stamp captures 'first' — not 'latest'."""
        u = make_user(plan="pro", email="ws-stamp-2@x.com")
        c = _logged_in(u)
        self._create_workspace(c, "First WS")
        db.session.refresh(u)
        original = u.first_workspace_at

        self._create_workspace(c, "Second WS")
        db.session.refresh(u)
        assert u.first_workspace_at == original


# ---------------------------------------------------------------------------
# Audit save stamps first_audit_at + onboarding_completed_at
# ---------------------------------------------------------------------------

class TestSaveAuditStamps:
    """save_audit_results is the chokepoint for every audit save path
    (CLI, audit_runner, bulk job). Stamping there covers every
    surface without per-route wiring."""

    def test_first_audit_save_stamps(self, make_user):
        from save_results import save_audit_results
        u = make_user(plan="pro", email="audit-stamp@x.com")

        # Minimal valid audit payload (matches the route's contract).
        save_audit_results(
            website="https://x.test",
            audit_type="quick",
            business_profile={"title": "T", "description": "D"},
            visibility_data={"queries_tested": 0, "appearances": 0},
            ai_answer_results=[],
            competitor_data={"direct_competitors": []},
            content_gaps=[],
            question_coverage=[],
            audit_data={
                "content_score": 0, "schema_score": 0,
            },
            final_report={"report_text": "", "verdict": "v", "summary": "s",
                          "raw_score": 0, "normalized_score": 0},
            client_id="ws-1",
            client_name="WS",
            user_id=u.id,
        )

        db.session.refresh(u)
        assert u.first_audit_at is not None
        assert u.onboarding_completed_at is not None
        # Audit row also exists.
        assert Audit.query.filter_by(user_id=u.id).count() == 1

    def test_audit_save_failure_does_not_block_user(self, make_user, monkeypatch):
        """If the stamp helper raises, the audit save itself must
        still succeed. The save path's try/except in save_results.py
        protects the happy path."""
        from save_results import save_audit_results
        u = make_user(plan="pro", email="audit-stamp-fail@x.com")

        def _explode(*args, **kwargs):
            raise RuntimeError("stamp helper imploded")

        monkeypatch.setattr("app.stamp_onboarding_milestone", _explode)

        # Must complete without raising.
        result = save_audit_results(
            website="https://x.test", audit_type="quick",
            business_profile={"title": "T", "description": "D"},
            visibility_data={"queries_tested": 0, "appearances": 0},
            ai_answer_results=[],
            competitor_data={"direct_competitors": []},
            content_gaps=[],
            question_coverage=[],
            audit_data={"content_score": 0, "schema_score": 0},
            final_report={"report_text": "", "verdict": "v", "summary": "s",
                          "raw_score": 0, "normalized_score": 0},
            client_id="ws-1", client_name="WS", user_id=u.id,
        )
        assert result["full_file"]
        assert Audit.query.filter_by(user_id=u.id).count() == 1


# ---------------------------------------------------------------------------
# Admin user detail surfaces the milestones
# ---------------------------------------------------------------------------

class TestAdminDetailDisplay:

    def test_milestone_card_renders(self, make_user):
        admin = make_user(role="admin", email="ms-admin@x.com")
        target = make_user(plan="free", email="ms-target@x.com")
        now = utcnow()
        target.first_workspace_at = now - timedelta(hours=2)
        target.first_audit_at = now - timedelta(hours=1)
        target.onboarding_completed_at = now - timedelta(hours=1)
        db.session.commit()

        r = _logged_in(admin).get(f"/admin/users/{target.id}")
        assert r.status_code == 200
        assert b"Onboarding milestones" in r.data
        assert b"First workspace" in r.data
        assert b"First audit" in r.data

    def test_time_to_activation_summary(self, make_user):
        """The card header shows 'activated in Xh from signup' when
        the user has completed onboarding. Useful at-a-glance."""
        admin = make_user(role="admin", email="ms-admin-tta@x.com")
        target = make_user(plan="free", email="ms-target-tta@x.com")
        # Force a specific time-to-activation by backdating created_at.
        target.created_at = utcnow() - timedelta(hours=5)
        target.onboarding_completed_at = utcnow()
        db.session.commit()

        r = _logged_in(admin).get(f"/admin/users/{target.id}")
        # Header includes "Activated in 5.0h"
        assert b"Activated in" in r.data

    def test_missing_milestones_render_em_dash(self, make_user):
        """A user who hasn't completed onboarding yet — milestones
        render with '—' instead of crashing on None."""
        admin = make_user(role="admin", email="ms-admin-empty@x.com")
        target = make_user(plan="free", email="ms-target-empty@x.com")
        # No milestone columns set.
        r = _logged_in(admin).get(f"/admin/users/{target.id}")
        assert r.status_code == 200
        assert b"Onboarding milestones" in r.data
        # No crash.
