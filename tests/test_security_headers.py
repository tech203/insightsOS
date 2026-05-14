"""Security headers + cookie hardening regression tests.

The Flask defaults are not safe for production. This suite locks in
the headers we set via _apply_security_headers and the cookie flags
we apply at config time.

What's covered:
  - Every response has the baseline security headers attached.
  - The Server header is rewritten so we don't broadcast Werkzeug +
    Python versions to anyone scanning.
  - Session cookies have HttpOnly + SameSite=Lax (always).
  - SESSION_COOKIE_SECURE is enabled when FLASK_ENV signals production.
  - The launch warning catches a missing or default SECRET_KEY.
"""
import io
import os
import sys
from contextlib import contextmanager

import pytest

from app import app as flask_app


# ---------------------------------------------------------------------------
# Headers on every response
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    return flask_app.test_client()


def test_x_content_type_options_is_nosniff(client):
    resp = client.get("/login")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"


def test_x_frame_options_is_sameorigin(client):
    resp = client.get("/login")
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"


def test_referrer_policy_is_strict_origin(client):
    resp = client.get("/login")
    assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


def test_server_header_does_not_leak_versions(client):
    """Werkzeug's default Server header is "Werkzeug/X.Y.Z Python/A.B.C"
    — we rewrite to a single non-versioned string so scanners can't
    fingerprint our exact stack from the response."""
    resp = client.get("/login")
    server = resp.headers.get("Server", "")
    assert "Werkzeug" not in server, (
        f"Server header leaks Werkzeug version: {server!r}"
    )
    assert "Python" not in server, (
        f"Server header leaks Python version: {server!r}"
    )


def test_security_headers_applied_to_404_response(client):
    """A bug-favourite: error responses sometimes skip the after_request
    hook. Make sure 404s carry the same headers as 200s."""
    resp = client.get("/this-route-definitely-does-not-exist-9999")
    assert resp.status_code == 404
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"


# ---------------------------------------------------------------------------
# Session cookie flags
# ---------------------------------------------------------------------------

def test_session_cookie_is_httponly():
    """HttpOnly stops document.cookie from reading the session token —
    defence against XSS-driven session theft."""
    assert flask_app.config.get("SESSION_COOKIE_HTTPONLY") is True


def test_session_cookie_samesite_is_lax():
    """SameSite=Lax stops the session being sent on cross-site POSTs
    — defence in depth on top of CSRF tokens."""
    assert flask_app.config.get("SESSION_COOKIE_SAMESITE") == "Lax"


def test_remember_cookie_is_httponly_and_samesite():
    """Flask-Login's remember cookie needs the same protection."""
    assert flask_app.config.get("REMEMBER_COOKIE_HTTPONLY") is True
    assert flask_app.config.get("REMEMBER_COOKIE_SAMESITE") == "Lax"


# ---------------------------------------------------------------------------
# Production-conditional config
# ---------------------------------------------------------------------------

def test_session_cookie_secure_is_set_in_production():
    """SESSION_COOKIE_SECURE must be True when FLASK_ENV signals prod.
    Run in a subprocess so the re-import of app doesn't replace the
    already-imported `app` module other tests captured references into
    (that caused `test_stuck_job_recovery` to fail under suite ordering)."""
    import subprocess
    env = {**os.environ, "FLASK_ENV": "production"}
    script = (
        "import sys; "
        "sys.path.insert(0, '.'); "
        "from app import app; "
        "assert app.config.get('SESSION_COOKIE_SECURE') is True, app.config.get('SESSION_COOKIE_SECURE'); "
        "assert app.config.get('REMEMBER_COOKIE_SECURE') is True, app.config.get('REMEMBER_COOKIE_SECURE')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_session_cookie_secure_is_off_in_development():
    """Dev should NOT set Secure — local dev runs over http://localhost
    and Secure cookies wouldn't be sent at all."""
    # The default test env doesn't set FLASK_ENV, so SESSION_COOKIE_SECURE
    # should be unset (Flask treats unset as False).
    assert flask_app.config.get("SESSION_COOKIE_SECURE") in (None, False)


# ---------------------------------------------------------------------------
# SECRET_KEY launch-warning detection
# ---------------------------------------------------------------------------

@contextmanager
def _capture_print():
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


def test_launch_warning_fires_when_secret_key_is_default(monkeypatch):
    """The startup launch-config check must catch the placeholder
    SECRET_KEY value — that's the most dangerous single config gap
    (forgeable session cookies)."""
    monkeypatch.setenv("SECRET_KEY", "change-this-to-a-random-secret-key")
    from app import _check_launch_config
    with _capture_print() as buf:
        _check_launch_config()
    out = buf.getvalue()
    assert "SECRET_KEY" in out
    assert "placeholder" in out.lower()


def test_launch_warning_fires_when_secret_key_is_unset(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    from app import _check_launch_config
    with _capture_print() as buf:
        _check_launch_config()
    out = buf.getvalue()
    assert "SECRET_KEY" in out
    assert "not set" in out.lower()


def test_launch_warning_quiet_when_secret_key_is_real(monkeypatch):
    """A proper random secret should NOT trigger the warning."""
    monkeypatch.setenv("SECRET_KEY", "kZ7nXq2Qb8YpL5tWvA3hF6jR9mN1cD4eS0u")
    # Clear other warning-trigger envs so we only see SECRET_KEY signal.
    for k in ("STRIPE_SECRET_KEY", "RESEND_FROM", "SHOPIFY_REDIRECT_URI", "S3_ENDPOINT_URL"):
        monkeypatch.delenv(k, raising=False)
    from app import _check_launch_config
    with _capture_print() as buf:
        _check_launch_config()
    out = buf.getvalue()
    assert "SECRET_KEY" not in out
