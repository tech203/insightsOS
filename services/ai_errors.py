"""Classify OpenAI / AI-provider exceptions into friendly user-facing
messages, so routes can show actionable copy instead of leaking raw
exception strings.

Today every audit / brief / draft route catches `except Exception`
and either silently fails or flashes the raw `str(e)`. Users see
things like:

    Audit failed: HTTPSConnectionPool(host='api.openai.com', port=443):
    Read timed out. (read timeout=120)

Or:

    AI brief generation failed: Error code: 429 — {'error': {'message':
    'Rate limit reached for gpt-4o-mini in organization org-...',
    'type': 'requests', 'param': null, 'code': 'rate_limit_exceeded'}}

Both are server-leak unfriendly. This module maps the common OpenAI
SDK exception types to short, friendly strings the user can act on.

Usage in a route:

    from services.ai_errors import classify_ai_error
    try:
        response = client.chat.completions.create(...)
    except Exception as exc:
        category, msg = classify_ai_error(exc)
        flash(msg, "error" if category == "auth" else "warning")
        # refund credits + redirect — caller decides
        return redirect(url_for("..."))

`classify_ai_error()` never raises. If the exception isn't a known
OpenAI type, returns ("unknown", "Something went wrong with the AI
provider. Please try again in a moment.") and logs the full
traceback at WARN level for ops.
"""

from __future__ import annotations

import logging
from typing import Any, Tuple

logger = logging.getLogger(__name__)


_FRIENDLY = {
    "rate_limit": (
        "Our AI provider is rate-limited right now. "
        "Wait about 30 seconds and try again — your credits weren't deducted."
    ),
    "timeout": (
        "The AI request took too long and timed out. "
        "Try again — if this keeps happening on the same prompt, shorten the input."
    ),
    "connection": (
        "Couldn't reach our AI provider. "
        "Check your internet, then try again in a moment."
    ),
    "auth": (
        "Our AI provider rejected the request. "
        "This is a server config issue — please reach out via the help page."
    ),
    "bad_request": (
        "The AI provider couldn't understand the request. "
        "Try simplifying the prompt or removing unusual characters."
    ),
    "quota": (
        "Our AI provider's monthly quota is exhausted. "
        "Reach out via the help page so we can extend it."
    ),
    "service_unavailable": (
        "Our AI provider is having a temporary outage. "
        "Try again in a few minutes — status.openai.com has the latest."
    ),
    "unknown": (
        "Something went wrong with the AI provider. "
        "Please try again in a moment."
    ),
}


def classify_ai_error(exc: BaseException) -> Tuple[str, str]:
    """Return (category, friendly_message) for an exception raised by
    an OpenAI SDK call. Falls back to ("unknown", generic message)
    for anything unrecognised.

    Never raises. Always logs the original exception at WARN so ops
    can see the underlying details server-side."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    msg_lower = (str(exc) or "").lower()

    try:
        # Use the SDK's exception classes if importable. Doing this
        # at call time avoids hard-coupling to openai at import.
        import openai
        if isinstance(exc, openai.RateLimitError):
            # 429 covers both per-minute rate limit AND monthly quota
            # exhausted. Differentiate by error code where possible.
            if "quota" in msg_lower or "insufficient_quota" in msg_lower:
                cat = "quota"
            else:
                cat = "rate_limit"
            return cat, _FRIENDLY[cat]
        if isinstance(exc, openai.APITimeoutError):
            return "timeout", _FRIENDLY["timeout"]
        if isinstance(exc, openai.APIConnectionError):
            return "connection", _FRIENDLY["connection"]
        if isinstance(exc, openai.AuthenticationError):
            return "auth", _FRIENDLY["auth"]
        if isinstance(exc, openai.BadRequestError):
            return "bad_request", _FRIENDLY["bad_request"]
        # InternalServerError, APIError catch-all for 5xx
        if hasattr(openai, "InternalServerError") and isinstance(exc, openai.InternalServerError):
            return "service_unavailable", _FRIENDLY["service_unavailable"]
        if isinstance(exc, openai.APIError):
            if status and 500 <= int(status) < 600:
                return "service_unavailable", _FRIENDLY["service_unavailable"]
    except Exception:
        # openai SDK not importable or comparison failed — fall through
        pass

    # Fallback: look at HTTP status / message patterns
    if status == 429 or "rate limit" in msg_lower or "rate_limit_exceeded" in msg_lower:
        if "quota" in msg_lower:
            return "quota", _FRIENDLY["quota"]
        return "rate_limit", _FRIENDLY["rate_limit"]
    if "timeout" in msg_lower or "timed out" in msg_lower:
        return "timeout", _FRIENDLY["timeout"]
    if "connection" in msg_lower or "network" in msg_lower:
        return "connection", _FRIENDLY["connection"]
    if status == 401 or "authentication" in msg_lower or "api key" in msg_lower:
        return "auth", _FRIENDLY["auth"]

    logger.warning("Unclassified AI provider error (%s): %s", name, exc)
    return "unknown", _FRIENDLY["unknown"]


def friendly_ai_error_message(exc: BaseException) -> str:
    """Convenience wrapper — returns just the friendly message string,
    discarding the category. Use when you don't need to branch on the
    error type (e.g. just flashing a single message)."""
    _, msg = classify_ai_error(exc)
    return msg
