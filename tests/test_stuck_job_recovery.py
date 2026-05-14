"""
Tests for stuck-job recovery on app boot.

If a worker process dies while a JobRun is in `pending` or `running`
state, its daemon thread is gone with the process — the row would
otherwise stay "running" forever, the poll endpoint would never
report a terminal state, and the user's UI would spin indefinitely.

`recover_interrupted_jobs()` runs once per worker process on first
request and marks any such rows as `failed` with a recognizable
error message. From there the user can intentionally re-run.

These tests exercise the function directly (cheaper than a
process-restart simulation) plus the once-per-process before_request
guard.
"""

from __future__ import annotations

import pytest

import app as app_module
from app import JobRun, db, recover_interrupted_jobs
from dtutils import utcnow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_recovery_flag():
    """Each test starts with the recovery flag cleared so the
    before_request hook will fire. Without this, the second test in
    the run would short-circuit on the flag set by the first."""
    app_module._jobs_recovered = False
    yield
    app_module._jobs_recovered = False


def _make_job(user, *, status, jid=None, **extra):
    job = JobRun(
        id=jid or f"job-{status}-{user.id}",
        user_id=user.id,
        kind="bulk_audit",
        status=status,
        progress_current=extra.get("progress_current", 0),
        progress_total=extra.get("progress_total", 3),
        result=extra.get("result", []),
        created_at=utcnow().isoformat(timespec="seconds"),
        started_at=extra.get("started_at"),
        finished_at=extra.get("finished_at"),
        error=extra.get("error"),
    )
    db.session.add(job)
    db.session.commit()
    return job


# ---------------------------------------------------------------------------
# recover_interrupted_jobs() — direct call
# ---------------------------------------------------------------------------

class TestRecoverFunction:

    def test_no_jobs_is_noop(self, user):
        """Empty job_runs table — recovery sweeps zero rows."""
        assert recover_interrupted_jobs() == 0

    def test_running_job_marked_failed(self, user):
        job = _make_job(user, status="running",
                        started_at=utcnow().isoformat(timespec="seconds"))
        assert recover_interrupted_jobs() == 1
        db.session.refresh(job)
        assert job.status == "failed"
        assert "Interrupted by server restart" in (job.error or "")
        assert job.finished_at is not None

    def test_pending_job_marked_failed(self, user):
        job = _make_job(user, status="pending")
        assert recover_interrupted_jobs() == 1
        db.session.refresh(job)
        assert job.status == "failed"
        assert "Interrupted by server restart" in (job.error or "")

    def test_done_job_untouched(self, user):
        job = _make_job(user, status="done",
                        finished_at=utcnow().isoformat(timespec="seconds"))
        assert recover_interrupted_jobs() == 0
        db.session.refresh(job)
        assert job.status == "done"
        assert job.error is None

    def test_failed_job_untouched(self, user):
        job = _make_job(user, status="failed",
                        error="Real failure that already happened",
                        finished_at=utcnow().isoformat(timespec="seconds"))
        assert recover_interrupted_jobs() == 0
        db.session.refresh(job)
        assert job.status == "failed"
        assert job.error == "Real failure that already happened"

    def test_canceled_job_untouched(self, user):
        job = _make_job(user, status="canceled",
                        finished_at=utcnow().isoformat(timespec="seconds"))
        assert recover_interrupted_jobs() == 0
        db.session.refresh(job)
        assert job.status == "canceled"

    def test_mixed_states_only_pending_and_running_swept(self, user):
        running = _make_job(user, status="running", jid="j-running")
        pending = _make_job(user, status="pending", jid="j-pending")
        done = _make_job(user, status="done", jid="j-done",
                         finished_at=utcnow().isoformat(timespec="seconds"))
        failed = _make_job(user, status="failed", jid="j-failed",
                           finished_at=utcnow().isoformat(timespec="seconds"))

        assert recover_interrupted_jobs() == 2

        for j in (running, pending, done, failed):
            db.session.refresh(j)

        assert running.status == "failed"
        assert pending.status == "failed"
        assert done.status == "done"
        assert failed.status == "failed"
        # Don't clobber the original error on a pre-existing terminal row.
        assert "Interrupted by server restart" not in (failed.error or "")

    def test_preserves_existing_error_text(self, user):
        """If a running job already has partial error info, append
        the interrupted-restart message rather than overwriting it.
        Helpful for post-mortems: 'the worker logged this, then died'."""
        job = _make_job(
            user, status="running",
            error="Partial progress: 2 of 5 workspaces failed",
        )
        recover_interrupted_jobs()
        db.session.refresh(job)
        assert "Partial progress: 2 of 5 workspaces failed" in (job.error or "")
        assert "Interrupted by server restart" in (job.error or "")

    def test_finished_at_set_to_now(self, user):
        job = _make_job(user, status="running")
        before = utcnow().isoformat(timespec="seconds")
        recover_interrupted_jobs()
        db.session.refresh(job)
        # ISO strings are lexicographically sortable. `finished_at`
        # should be >= the timestamp captured just before the call.
        assert job.finished_at is not None
        assert job.finished_at >= before

    def test_idempotent_second_call_is_noop(self, user):
        _make_job(user, status="running", jid="j-1")
        _make_job(user, status="pending", jid="j-2")
        assert recover_interrupted_jobs() == 2
        # After the first call everything is in failed state.
        assert recover_interrupted_jobs() == 0

    def test_recovery_isolates_across_users(self, make_user):
        """Recovery is global to the process — every stuck job, every
        user, gets swept. Validates the query has no user filter."""
        u1 = make_user(email="a@test.com")
        u2 = make_user(email="b@test.com")
        _make_job(u1, status="running", jid="a-run")
        _make_job(u2, status="pending", jid="b-pend")
        assert recover_interrupted_jobs() == 2


# ---------------------------------------------------------------------------
# before_request hook — fires once per worker process
# ---------------------------------------------------------------------------

class TestBeforeRequestHook:

    def test_first_request_triggers_recovery(self, user, logged_in_client):
        """Hitting any route on a fresh worker (flag=False) should
        cause stuck jobs to be swept before the request handler runs."""
        job = _make_job(user, status="running")
        # Cheap auth'd GET — picks a route that's guaranteed to exist
        # and doesn't depend on workspace state.
        resp = logged_in_client.get("/api/wallet")
        assert resp.status_code == 200
        db.session.refresh(job)
        assert job.status == "failed"
        assert app_module._jobs_recovered is True

    def test_subsequent_requests_skip_recovery(self, user, logged_in_client, monkeypatch):
        """Once the flag is flipped, the hook short-circuits — no
        repeated SELECT on every request. Patch the function to a
        sentinel that flips a counter; assert it fires exactly once."""
        calls = {"n": 0}
        real = recover_interrupted_jobs

        def _spy():
            calls["n"] += 1
            return real()

        monkeypatch.setattr("app.recover_interrupted_jobs", _spy)
        _make_job(user, status="pending")
        logged_in_client.get("/api/wallet")
        logged_in_client.get("/api/wallet")
        logged_in_client.get("/api/wallet")
        assert calls["n"] == 1

    def test_hook_swallows_recovery_exceptions(self, user, logged_in_client, monkeypatch):
        """If recovery itself blows up (DB error, etc.), the request
        should still proceed. We never want the recovery sweep to take
        down the whole app. Flag still flips so we don't retry on
        every request after."""

        def _explode():
            raise RuntimeError("simulated DB outage")

        monkeypatch.setattr("app.recover_interrupted_jobs", _explode)
        resp = logged_in_client.get("/api/wallet")
        assert resp.status_code == 200
        assert app_module._jobs_recovered is True

    def test_static_routes_also_trigger_recovery(self, user, logged_in_client):
        """The hook is global — there's no static-route exemption,
        unlike sweep_expired_reservations(). Stuck-job recovery is
        cheap enough (one indexed SELECT, then a no-op) that we don't
        bother gating it."""
        job = _make_job(user, status="running")
        logged_in_client.get("/login")  # anonymous-friendly route
        db.session.refresh(job)
        assert job.status == "failed"
