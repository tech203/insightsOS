"""
Datetime helpers — single source of truth for "what time is it now."

Why this module exists
----------------------
Python 3.12+ deprecated `datetime.datetime.utcnow()` (and its `today`
sibling). The replacement is `datetime.datetime.now(timezone.utc)`,
which returns a **timezone-aware** value.

The codebase has ~180 `datetime.utcnow()` calls that all assumed a
timezone-naive value. Switching every one of them to a tz-aware value
all at once is risky:

  - SQLAlchemy DateTime columns are tz-naive by default; mixing tz-
    aware values into them either silently strips the tz or raises a
    StatementError depending on backend.
  - SQLite (our default) doesn't natively store timezones, so values
    read back are always tz-naive. Comparing those with tz-aware
    `datetime.now(tz=utc)` raises TypeError.
  - Every `x.expires_at > datetime.utcnow()` comparison in the
    codebase would need to be audited.

So the migration is staged. Today (this PR):

  - Add `utcnow()` here that returns a **tz-naive UTC datetime**.
    Drop-in replacement for `datetime.utcnow()`, uses the
    non-deprecated API under the hood.
  - Replace every call site repo-wide so the deprecation warning
    goes away and we have one place to switch later.

Later (separate PR):

  - Optionally migrate to truly tz-aware values throughout. Will
    need DB column type changes (DateTime(timezone=True)) plus a
    repo-wide comparison audit.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current UTC time as a **timezone-naive** datetime.

    Replacement for `datetime.utcnow()` that uses the non-deprecated
    `datetime.now(timezone.utc)` under the hood. The returned value is
    intentionally tz-naive so existing code that compares to DB
    columns (tz-naive on SQLite) keeps working without an audit.

    See module docstring for the migration strategy.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
