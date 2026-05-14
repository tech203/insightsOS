"""Performance regression — N+1 query guard on the workspace-list path.

Pre-fix: build_client_views() ran a per-workspace query for each
integration type (Shopify connection, GSC connection, Webflow
exports). Concretely:

  N=  1 workspaces → ~18 queries on /dashboard, /clients
  N=  5 workspaces → ~52 queries
  N= 20 workspaces → ~187 queries

An agency on the Growth plan with 30 workspaces was looking at 270+
queries per page load over the network to Postgres. Post-fix: those
3 query types are pre-fetched once for the whole user, then dict
lookups inside the loop — query count is constant ~16-18 regardless
of N.

This test guards against the regression coming back. If someone
adds another `Model.query.filter_by(client_id=...)` inside the
build_client_views loop, the linear growth returns and this test
fails with a clear "you put SQL in a hot loop" message.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import event
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Wallet, CreditTransaction, Client


@pytest.fixture
def query_counter(app_ctx):
    """Yield a callable that returns the SQL query count since reset."""
    counter = {"n": 0}

    @event.listens_for(db.engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    def reset():
        counter["n"] = 0

    def value():
        return counter["n"]

    return reset, value


@pytest.fixture
def user_with_n_workspaces(app_ctx):
    def _make(n: int):
        u = User(
            email=f"perf-n{n}@test.com",
            password_hash=generate_password_hash("xx"),
            name="Perf",
            plan="growth",
            email_verified_at=datetime.now(timezone.utc),
        )
        db.session.add(u)
        db.session.flush()
        db.session.add(Wallet(user_id=u.id, balance=100))
        db.session.add(CreditTransaction(
            user_id=u.id, type="monthly_allowance", amount=0,
            balance_after=100, notes="Perf fixture",
        ))
        for i in range(n):
            db.session.add(Client(
                slug=f"co-{i}", user_id=u.id, name=f"Co {i}",
                website=f"https://c{i}.example.com",
                website_normalized=f"c{i}.example.com",
            ))
        db.session.commit()
        client = flask_app.test_client()
        with client.session_transaction() as s:
            s["_user_id"] = u.get_id()
            s["_fresh"] = True
        return client
    return _make


def test_clients_query_count_is_constant_regardless_of_workspace_count(
    user_with_n_workspaces, query_counter,
):
    """The whole point of pre-fetching: query count must NOT scale
    with the number of workspaces. We allow some slack (≤2× the
    1-workspace baseline) — but linear growth (which would give
    ~10×) is the failure mode we're guarding against."""
    reset, value = query_counter

    c1 = user_with_n_workspaces(1)
    reset()
    c1.get("/clients")
    baseline = value()

    c20 = user_with_n_workspaces(20)
    reset()
    c20.get("/clients")
    at_20 = value()

    # If at_20 is more than 2× baseline, build_client_views() has
    # an N+1 again — likely a Model.query.filter_by(client_id=...)
    # call that snuck back into the per-workspace loop.
    assert at_20 <= baseline * 2, (
        f"N+1 regression on /clients: {baseline} queries with 1 workspace, "
        f"{at_20} with 20. Linear scaling means a per-workspace query "
        f"got added back to build_client_views() — bulk-fetch it before "
        f"the loop instead. See app.py build_client_views() for the "
        f"existing pre-fetch pattern."
    )


def test_dashboard_query_count_is_constant_regardless_of_workspace_count(
    user_with_n_workspaces, query_counter,
):
    """Same guard, /dashboard. Both pages render via build_client_views
    so they share the regression risk."""
    reset, value = query_counter

    c1 = user_with_n_workspaces(1)
    reset()
    c1.get("/dashboard")
    baseline = value()

    c20 = user_with_n_workspaces(20)
    reset()
    c20.get("/dashboard")
    at_20 = value()

    assert at_20 <= baseline * 2, (
        f"N+1 regression on /dashboard: {baseline} queries with 1 workspace, "
        f"{at_20} with 20."
    )
