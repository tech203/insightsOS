"""Regression: tests/conftest.py must refuse to run under a DATABASE_URL
that points at the live SQLite database.

History: the per-test app_ctx fixture in conftest.py calls db.drop_all()
on teardown. Twice now, a developer has run `source .env && pytest`,
leaking the live DATABASE_URL into the test process, and the next test
cycle silently wiped the production admin DB. The conftest now has a
hard guard that raises SystemExit if DATABASE_URL contains a known
prod path marker — unless the developer explicitly opts in via
ALLOW_PROD_DB_IN_TESTS=1.

This test runs pytest in a subprocess with the bad DATABASE_URL set
and asserts the guard fires. Using a subprocess is the only sane way
to test conftest.py logic that runs at import time — by the time any
in-process test executes, conftest has already been imported once and
the guard has either fired (killing the suite) or passed (so we never
see it fire here).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_pytest_subprocess(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    """Run `pytest -q --collect-only` in a clean subprocess with `env_overrides`
    layered on top of the parent env. --collect-only is enough to trigger
    conftest.py import without actually executing any tests."""
    env = os.environ.copy()
    # Make sure we don't inherit ALLOW_PROD_DB_IN_TESTS or DATABASE_URL
    # from the parent — caller sets them explicitly via env_overrides.
    env.pop("DATABASE_URL", None)
    env.pop("ALLOW_PROD_DB_IN_TESTS", None)
    env.update(env_overrides)
    # PYTHONDONTWRITEBYTECODE keeps the working tree clean of stray .pyc.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "tests/test_conftest_prod_db_guard.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_guard_blocks_app_prod_db_path():
    """DATABASE_URL pointing at app-prod.db must abort pytest startup."""
    result = _run_pytest_subprocess({"DATABASE_URL": "sqlite:////tmp/instance/app-prod.db"})
    assert result.returncode != 0, (
        "Expected non-zero exit; the conftest guard should have aborted. "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    assert "REFUSING TO RUN TESTS" in combined, (
        f"Expected guard's REFUSING marker. stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_guard_blocks_instance_app_db_path():
    """The other production-path marker — instance/app.db — must also abort."""
    result = _run_pytest_subprocess({"DATABASE_URL": "sqlite:////tmp/instance/app.db"})
    assert result.returncode != 0
    assert "REFUSING TO RUN TESTS" in (result.stdout + result.stderr)


def test_guard_allows_test_db_path():
    """Normal test DB path must pass the guard (sanity check — if the
    guard fired here, every test run in CI would break)."""
    # Use a temp file so we don't clobber the repo's instance/test.db.
    with tempfile.TemporaryDirectory() as tmp:
        result = _run_pytest_subprocess(
            {"DATABASE_URL": f"sqlite:///{tmp}/test.db"}
        )
    combined = result.stdout + result.stderr
    assert "REFUSING TO RUN TESTS" not in combined, (
        f"Guard fired on a non-prod path. combined={combined!r}"
    )


def test_guard_respects_explicit_opt_out():
    """ALLOW_PROD_DB_IN_TESTS=1 must bypass the guard even with a prod path.
    Lets a developer run tests against a backed-up copy if they really need to."""
    with tempfile.TemporaryDirectory() as tmp:
        # Copy a fake app-prod.db file path that won't actually exist;
        # pytest collection doesn't open the file, so the guard's path
        # check is what we're verifying, not actual DB I/O.
        fake_prod = Path(tmp) / "app-prod.db"
        fake_prod.touch()
        result = _run_pytest_subprocess(
            {
                "DATABASE_URL": f"sqlite:///{fake_prod}",
                "ALLOW_PROD_DB_IN_TESTS": "1",
            }
        )
    combined = result.stdout + result.stderr
    assert "REFUSING TO RUN TESTS" not in combined, (
        f"Guard fired despite ALLOW_PROD_DB_IN_TESTS=1. combined={combined!r}"
    )
