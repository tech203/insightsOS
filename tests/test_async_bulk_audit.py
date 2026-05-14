"""
Tests for the async bulk-audit infrastructure.

Three layers under test:
    1. POST /api/jobs/bulk-audit/start — auth, plan gate, body
       validation, JobRun row created with the expected shape
    2. GET /api/jobs/<id> — auth, returns the JobRun state, 404 on
       unknown or other-user's jobs
    3. _bulk_audit_worker (called synchronously, NOT via the
       daemon-thread spawn) — verifies the per-iteration progress
       commits, credit reservation handling, and per-workspace
       result shape

We test the worker directly rather than going through the daemon
thread because:
  - Threads make assertions racy without an explicit "wait" hook
  - The thread wrapper just translates status; the meat is in
    _bulk_audit_worker
  - A bug in the thread wrapper is far less likely than a bug in
    the per-workspace processing loop
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app import (
    Client,
    CreditReservation,
    JobRun,
    _bulk_audit_worker,
    db,
)
from app import app as flask_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspaces(user):
    """Three complete workspaces for the default Pro user."""
    out = []
    for i in range(3):
        ws = Client(
            slug=f"ws-{i}",
            user_id=user.id,
            name=f"Workspace {i}",
            website=f"https://ws-{i}.example.com",
            website_normalized=f"ws-{i}.example.com",
            industry="SaaS",
            location="SG",
        )
        db.session.add(ws)
        out.append(ws)
    db.session.commit()
    return out


@pytest.fixture
def logged_in_client(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


@pytest.fixture
def mocked_audit_internals():
    """Mock out the heavy audit machinery used by _bulk_audit_worker
    so tests exercise the loop / reservation / progress logic
    without hitting OpenAI / Tavily."""
    with patch("app.run_audit_for_input", return_value=None), \
         patch(
            "app.create_content_opportunities_from_latest_audit",
            return_value={
                "added": 2,
                "skipped_existing": 0,
                "skipped_due_to_cap": 0,
                "total_opportunities": 2,
                "active_queue_limit": 25,
            },
         ):
        yield


# ---------------------------------------------------------------------------
# POST /api/jobs/bulk-audit/start
# ---------------------------------------------------------------------------

class TestStartEndpoint:
    def test_anonymous_redirected(self, app_ctx, workspaces):
        c = flask_app.test_client()
        r = c.post(
            "/api/jobs/bulk-audit/start",
            json={"client_ids": ["ws-0"]},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_pro_user_starts_job(self, logged_in_client, workspaces):
        r = logged_in_client.post(
            "/api/jobs/bulk-audit/start",
            json={"client_ids": ["ws-0", "ws-1"]},
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["job_id"]

        # JobRun row created in pending status
        job = db.session.get(JobRun, body["job_id"])
        assert job is not None
        assert job.kind == "bulk_audit"
        assert job.progress_total == 2
        # `progress_current` may be 0 (pending) or 1+ if the daemon
        # thread already started; both are valid. The point is the
        # job exists with the right shape.

    def test_free_user_rejected(self, app_ctx, make_user, workspaces):
        free = make_user(plan="free", email="free-bulk@x.com")
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(free.id)
            s["_fresh"] = True
        r = c.post(
            "/api/jobs/bulk-audit/start",
            json={"client_ids": ["ws-0"]},
        )
        assert r.status_code == 403
        body = r.get_json()
        assert "Pro" in body["error"] or "Growth" in body["error"]

    def test_admin_user_skips_plan_gate(
        self, app_ctx, make_user, workspaces,
    ):
        """Admins (and dev_unlimited) bypass the subscriber gate so
        internal accounts aren't locked out of their own product."""
        admin = make_user(plan="free", role="admin", email="admin-bulk@x.com")
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(admin.id)
            s["_fresh"] = True
        r = c.post(
            "/api/jobs/bulk-audit/start",
            json={"client_ids": ["ws-0"]},
        )
        assert r.status_code == 200

    def test_empty_client_ids_rejected(self, logged_in_client):
        r = logged_in_client.post(
            "/api/jobs/bulk-audit/start",
            json={"client_ids": []},
        )
        assert r.status_code == 400

    def test_missing_body_rejected(self, logged_in_client):
        r = logged_in_client.post(
            "/api/jobs/bulk-audit/start",
            json={},
        )
        assert r.status_code == 400

    def test_dedupes_client_ids(self, logged_in_client, workspaces):
        """Duplicates in the input are silently deduped — sending
        ["ws-0", "ws-0", "ws-1"] starts a job for 2 workspaces."""
        r = logged_in_client.post(
            "/api/jobs/bulk-audit/start",
            json={"client_ids": ["ws-0", "ws-0", "ws-1"]},
        )
        body = r.get_json()
        job = db.session.get(JobRun, body["job_id"])
        assert job.progress_total == 2

    def test_oversize_batch_rejected(self, logged_in_client):
        """Cap at 50 workspaces per batch — defense in depth against
        runaway request bodies."""
        r = logged_in_client.post(
            "/api/jobs/bulk-audit/start",
            json={"client_ids": [f"ws-{i}" for i in range(51)]},
        )
        assert r.status_code == 400
        body = r.get_json()
        assert "max" in body["error"].lower() or "too many" in body["error"].lower()


# ---------------------------------------------------------------------------
# GET /api/jobs/<id>
# ---------------------------------------------------------------------------

class TestPollEndpoint:
    def test_returns_job_state(self, logged_in_client, user):
        # Make a JobRun directly so we don't have to wait for the
        # daemon thread.
        from app import utcnow
        job = JobRun(
            id="test-job-1",
            user_id=user.id,
            kind="bulk_audit",
            status="running",
            progress_current=1,
            progress_total=3,
            result=[{"client_id": "ws-0", "ok": True}],
            created_at=utcnow().isoformat(timespec="seconds"),
        )
        db.session.add(job)
        db.session.commit()

        r = logged_in_client.get("/api/jobs/test-job-1")
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["job"]["status"] == "running"
        assert body["job"]["progress_current"] == 1
        assert body["job"]["progress_total"] == 3
        assert len(body["job"]["result"]) == 1

    def test_unknown_job_returns_404(self, logged_in_client):
        r = logged_in_client.get("/api/jobs/does-not-exist")
        assert r.status_code == 404

    def test_other_users_job_returns_404(self, app_ctx, make_user):
        """Job poll is per-user — looking up another user's job is
        indistinguishable from "doesn't exist". Avoids leaking the
        existence of jobs across users."""
        from app import utcnow
        owner = make_user(email="job-owner@x.com")
        intruder = make_user(email="job-intruder@x.com")

        job = JobRun(
            id="owner-job", user_id=owner.id, kind="bulk_audit",
            status="done", progress_current=1, progress_total=1,
            result=[], created_at=utcnow().isoformat(timespec="seconds"),
        )
        db.session.add(job)
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(intruder.id)
            s["_fresh"] = True
        r = c.get("/api/jobs/owner-job")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# _bulk_audit_worker — the meat
# ---------------------------------------------------------------------------

class TestBulkAuditWorker:
    """Tests the worker function directly, synchronously. The daemon-
    thread wrapper (_spawn_background_job) just translates terminal
    status — the per-workspace loop is what matters."""

    def _make_job(self, user, total):
        from app import utcnow
        import secrets
        job = JobRun(
            id=secrets.token_urlsafe(16),
            user_id=user.id,
            kind="bulk_audit",
            status="running",  # worker assumes the wrapper already set this
            progress_current=0,
            progress_total=total,
            result=[],
            created_at=utcnow().isoformat(timespec="seconds"),
            started_at=utcnow().isoformat(timespec="seconds"),
        )
        db.session.add(job)
        db.session.commit()
        return job

    def test_processes_all_workspaces(
        self, user, workspaces, mocked_audit_internals,
    ):
        balance_before = user.wallet.balance
        job = self._make_job(user, total=3)

        _bulk_audit_worker(job.id, ["ws-0", "ws-1", "ws-2"], user.id)

        db.session.refresh(job)
        assert job.status == "done"
        assert job.progress_current == 3
        assert len(job.result) == 3
        # All 3 succeeded
        assert all(r["ok"] for r in job.result)

        # 3 audits × 1 credit each = 3 credits debited.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 3

    def test_progress_is_persisted_per_iteration(
        self, user, workspaces, mocked_audit_internals,
    ):
        """The worker should commit progress after each workspace
        so the poll endpoint can render live progress, not just
        the final state."""
        job = self._make_job(user, total=2)

        # Run the worker and observe an intermediate poll. We
        # achieve this by patching create_content_opportunities to
        # check the JobRun row mid-loop.
        seen_progress = []

        def spy(*args, **kwargs):
            # Force a fresh read of the job row to observe partial state
            db.session.expire_all()
            j = db.session.get(JobRun, job.id)
            seen_progress.append(j.progress_current)
            return {
                "added": 0, "skipped_existing": 0,
                "skipped_due_to_cap": 0,
                "total_opportunities": 0, "active_queue_limit": 25,
            }

        with patch("app.run_audit_for_input", return_value=None), \
             patch(
                 "app.create_content_opportunities_from_latest_audit",
                 side_effect=spy,
             ):
            _bulk_audit_worker(job.id, ["ws-0", "ws-1"], user.id)

        # The worker calls create_content_opportunities AFTER the
        # audit but BEFORE committing the per-iteration progress.
        # So seen_progress at call N should reflect the result of
        # the previous iteration's commit. First call: 0 (no prior
        # iterations); second call: 1 (one iteration done).
        assert seen_progress == [0, 1]

    def test_out_of_credits_skips_remaining(
        self, app_ctx, make_user, workspaces, mocked_audit_internals,
    ):
        """If credits run dry mid-batch, remaining workspaces should
        be marked 'skipped' with reason=insufficient_credits, not
        attempted as audits."""
        # User has exactly 2 credits, enough for 2 of 3 audits.
        # The 3rd should be skipped.
        u = make_user(plan="pro", balance=2, email="thin-bulk@x.com")
        for i in range(3):
            db.session.add(Client(
                slug=f"thin-ws-{i}", user_id=u.id,
                name=f"Thin {i}", website=f"https://t{i}.example.com",
                website_normalized=f"t{i}.example.com",
                industry="A", location="B",
            ))
        db.session.commit()

        job = self._make_job(u, total=3)
        _bulk_audit_worker(
            job.id, ["thin-ws-0", "thin-ws-1", "thin-ws-2"], u.id,
        )

        db.session.refresh(job)
        results = job.result
        assert len(results) == 3
        assert results[0]["ok"] is True
        assert results[1]["ok"] is True
        # Third: out of credits — either fails the reserve OR is
        # explicitly marked skipped depending on order. Either way
        # the user isn't billed for it.
        third = results[2]
        assert third["ok"] is False
        # The error explicitly mentions credits / reason
        assert (
            third.get("reason") == "insufficient_credits"
            or third.get("skipped") is True
            or "credit" in (third.get("error") or "").lower()
        )

        db.session.refresh(u.wallet)
        # Wallet drained to 0 after the 2 successful audits;
        # third didn't reserve so wallet stays at 0.
        assert u.wallet.balance == 0

    def test_unknown_workspace_recorded_as_failure(
        self, user, mocked_audit_internals,
    ):
        """An unknown slug shouldn't crash the worker — record the
        failure and continue with the rest of the batch."""
        # Create one real workspace, then ask the worker to process
        # one real + one missing.
        ws = Client(
            slug="real-ws", user_id=user.id, name="Real",
            website="https://r.example.com",
            website_normalized="r.example.com",
            industry="A", location="B",
        )
        db.session.add(ws)
        db.session.commit()

        job = self._make_job(user, total=2)
        _bulk_audit_worker(job.id, ["real-ws", "missing-ws"], user.id)

        db.session.refresh(job)
        assert len(job.result) == 2
        # Lookup order matches the input order.
        real_rec = next(r for r in job.result if r["client_id"] == "real-ws")
        missing_rec = next(r for r in job.result if r["client_id"] == "missing-ws")
        assert real_rec["ok"] is True
        assert missing_rec["ok"] is False
        assert "not found" in (missing_rec["error"] or "").lower()

    def test_audit_exception_refunds_credit(
        self, user, workspaces,
    ):
        """A per-workspace audit raise should:
          - release the reservation (wallet refunded)
          - record the failure in result
          - NOT crash the whole job — remaining workspaces continue
        """
        balance_before = user.wallet.balance
        job = self._make_job(user, total=2)

        # Audit raises for the first ws, succeeds for the second.
        call_count = {"n": 0}

        def flaky_audit(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("boom")
            return None

        with patch("app.run_audit_for_input", side_effect=flaky_audit), \
             patch(
                 "app.create_content_opportunities_from_latest_audit",
                 return_value={"added": 0, "skipped_existing": 0,
                               "skipped_due_to_cap": 0,
                               "total_opportunities": 0, "active_queue_limit": 25},
             ):
            _bulk_audit_worker(job.id, ["ws-0", "ws-1"], user.id)

        db.session.refresh(job)
        # First failed (audit raised), second succeeded
        assert job.result[0]["ok"] is False
        assert "boom" not in (job.result[0]["error"] or "")  # friendly wrap
        assert job.result[1]["ok"] is True

        # Wallet: -1 credit (the 2nd audit's reservation committed).
        # The 1st audit's reservation should have been released.
        db.session.refresh(user.wallet)
        assert user.wallet.balance == balance_before - 1

        # Reservation rows reflect the release for the failed one
        # and the commit for the succeeded one.
        reservations = (
            CreditReservation.query
            .filter_by(user_id=user.id, action_key="audit_run")
            .order_by(CreditReservation.id.asc())
            .all()
        )
        assert len(reservations) == 2
        assert reservations[0].status == "released"
        assert reservations[1].status == "committed"
