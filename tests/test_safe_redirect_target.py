"""Tests for _safe_redirect_target — open-redirect defence used in
the OAuth callback (and any other place that honours a user-supplied
`next` parameter going forward).

Validates that:
- Relative paths and same-host URLs pass through unchanged (so
  deep-link-after-login still works).
- Absolute URLs to other hosts are rejected (returns None → caller
  falls back to a safe default like /index).
- Schemeless-protocol-relative URLs like "//evil.com/foo" are
  rejected (these get coerced to https://evil.com by some browsers).
- Non-http(s) schemes (javascript:, data:, file:) are rejected.
- None / empty inputs return None.
"""
import pytest

from app import app as flask_app, _safe_redirect_target


SAFE_TARGETS = [
    "/",
    "/dashboard",
    "/settings/credits",
    "/client/acme/visibility?tab=missed",
    "/clients?filter=active#section-1",
    # Same-host absolute URL — when we run in a request context the
    # request.host_url matches, so this should pass through.
    "http://localhost/dashboard",
    "https://localhost/dashboard",
]


UNSAFE_TARGETS = [
    "https://evil.com/phish",
    "http://attacker.example.com/login",
    "//evil.com/foo",  # protocol-relative — coerces to current scheme
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    # Whitespace + URL — the strip() inside the helper kills the
    # outer whitespace, so the URL itself still has to be safe.
    "   https://evil.com/x",
]


@pytest.mark.parametrize("target", SAFE_TARGETS)
def test_safe_targets_pass_through(target):
    with flask_app.test_request_context("http://localhost/"):
        result = _safe_redirect_target(target)
        assert result is not None, f"Safe target {target!r} was rejected"
        assert result.strip() == target.strip()


@pytest.mark.parametrize("target", UNSAFE_TARGETS)
def test_unsafe_targets_are_rejected(target):
    with flask_app.test_request_context("http://localhost/"):
        result = _safe_redirect_target(target)
        assert result is None, (
            f"Unsafe target {target!r} returned {result!r} "
            f"(expected None — caller must fall back to safe default)"
        )


@pytest.mark.parametrize("target", [None, "", "   "])
def test_empty_inputs_return_none(target):
    with flask_app.test_request_context("http://localhost/"):
        assert _safe_redirect_target(target) is None
