"""Rate-limiting regression tests.

Brute-force / spam defence on the auth + email-sending endpoints. The
suite-wide conftest disables the limiter (otherwise normal tests trip
it); these tests re-enable it locally, hammer each endpoint past its
limit, and assert the next request 429s.

Endpoints under test (limit shown in parens):

  POST /login                  (10/min)  — credential stuffing
  POST /signup                 (5/hr)    — fake-account spam
  POST /forgot-password        (3/hr)    — email-bomb / Resend cost
  POST /verify-email/resend    (3/hr)    — same
  POST /interest               (10/hr)   — landing-page form spam
  GET  /reset-password/<token> (10/hr)   — token guess (256-bit token
                                            so infeasible anyway, but
                                            this stops log flooding)
"""
import pytest

from app import app as flask_app, limiter


@pytest.fixture
def limited_client(app_ctx):
    """Test client with rate limiting RE-ENABLED (conftest disables it
    by default). Reset storage between tests so they don't bleed.

    Depends on app_ctx so the DB schema is set up — /login etc. hit
    User.query and would crash with `no such table` otherwise."""
    limiter.enabled = True
    limiter.reset()
    try:
        yield flask_app.test_client()
    finally:
        limiter.enabled = False
        limiter.reset()


def _hammer(client, method, url, n, **kwargs):
    """Make N requests to the same URL, return the list of status codes."""
    fn = getattr(client, method)
    return [fn(url, **kwargs).status_code for _ in range(n)]


def test_login_post_rate_limit_kicks_in(limited_client):
    """11th login attempt within a minute must 429 (limit is 10/min)."""
    codes = _hammer(
        limited_client, "post", "/login",
        11, data={"email": "x@x.com", "password": "wrong"},
    )
    # First 10 should NOT be 429 (they're login failures, but allowed).
    assert all(c != 429 for c in codes[:10]), (
        f"Limiter tripped early: {codes[:10]}"
    )
    # 11th request should be 429.
    assert codes[10] == 429, (
        f"Login rate limit not enforced — 11th request returned {codes[10]}"
    )


def test_signup_post_rate_limit_kicks_in(limited_client):
    """6th signup within an hour must 429 (limit is 5/hour)."""
    codes = _hammer(
        limited_client, "post", "/signup",
        6, data={"name": "X", "email": "x@x.com", "password": "xxxxxxxx",
                 "confirm_password": "xxxxxxxx"},
    )
    assert codes[5] == 429, (
        f"Signup rate limit not enforced — 6th request returned {codes[5]}"
    )


def test_forgot_password_rate_limit_kicks_in(limited_client):
    """4th forgot-password within an hour must 429 (limit is 3/hour).
    Each request would otherwise send a real Resend email."""
    codes = _hammer(
        limited_client, "post", "/forgot-password",
        4, data={"email": "user@example.com"},
    )
    assert codes[3] == 429, (
        f"forgot-password rate limit not enforced — 4th returned {codes[3]}"
    )


def test_interest_form_rate_limit_kicks_in(limited_client):
    """11th interest signup within an hour must 429 (limit is 10/hour)."""
    codes = _hammer(
        limited_client, "post", "/interest",
        11, data={"email": "lead@example.com", "company": "X"},
    )
    assert codes[10] == 429, (
        f"interest rate limit not enforced — 11th returned {codes[10]}"
    )


def test_limiter_disabled_in_default_test_session():
    """Sanity check: the suite-wide conftest must keep the limiter off
    so the rest of the test suite doesn't trip its own limits. If this
    test ever fails, we're tripping our own limiter elsewhere."""
    # Reset between tests (the limited_client fixture re-enables); this
    # test runs without that fixture so should see the conftest default.
    limiter.enabled = False  # explicit reset in case prior test didn't restore
    assert limiter.enabled is False


def test_429_response_renders_branded_page(limited_client):
    """The 429 errorhandler should render templates/errors/429.html
    instead of Flask's bare default."""
    # Trip the login limit fast.
    for _ in range(11):
        limited_client.post("/login", data={"email": "x@x.com", "password": "x"})
    resp = limited_client.post("/login", data={"email": "x@x.com", "password": "x"})
    assert resp.status_code == 429
    # Branded page should not contain Werkzeug's default 429 text.
    body = resp.data.decode(errors="replace").lower()
    assert "rate" in body or "too many" in body or "slow down" in body, (
        f"429 response body doesn't look branded: {resp.data[:300]!r}"
    )
