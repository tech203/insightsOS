# CLAUDE.md

## Test fixture gotchas

Any test that exercises a route handler which hits the DB must take the `app_ctx` fixture (or a downstream fixture like `make_user` that depends on it). Without it, the request runs against an empty SQLite and fails with `sqlite3.OperationalError: no such table: users`. Tests that short-circuit before the DB query (e.g. 503/403 paths) will pass and mask the bug — only the success-path tests surface it. Pattern: `def test_foo(self, app_ctx, monkeypatch): ...`.
