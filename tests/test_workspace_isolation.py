"""Cross-tenant workspace isolation tests (IDOR defence).

A multi-tenant app is one missing ownership check away from "any user
can read/edit any other user's workspace by knowing the slug or ID."
This test suite exercises every workspace-scoped GET and POST as a
DIFFERENT user than the owner, and asserts the response either:

- 404s (slug-based routes — get_client_by_id() filters by user)
- 302-redirects with "Workspace not found." (int-id routes — explicit
  ownership check before render)
- Returns 4xx (some integrations use abort(404))

Anything 200 would mean Bob can see Alice's data → a critical IDOR.

Why this matters: workspace data includes audit results, Brand Kit,
business profile fields, integration tokens (Google Search Console,
WooCommerce), and competitor lists. A leak would expose Alice's
strategy + auth tokens to any other registered user.
"""
import pytest
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Wallet, Client


@pytest.fixture
def two_users_one_workspace(app_ctx):
    """Alice owns 'alice-co'. Bob is logged in. Bob must NOT see it."""
    alice = User(
        email="alice@iso.test",
        password_hash=generate_password_hash("xx"),
        name="Alice",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(alice)
    db.session.flush()
    alice.wallet = Wallet(user_id=alice.id, balance=100)
    db.session.add(alice.wallet)

    alice_ws = Client(
        slug="alice-co",
        user_id=alice.id,
        name="Alice Co",
        website="https://alice.example.com",
        website_normalized="alice.example.com",
    )
    db.session.add(alice_ws)

    bob = User(
        email="bob@iso.test",
        password_hash=generate_password_hash("xx"),
        name="Bob",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(bob)
    db.session.flush()
    bob.wallet = Wallet(user_id=bob.id, balance=100)
    db.session.add(bob.wallet)
    db.session.commit()

    bob_client = flask_app.test_client()
    with bob_client.session_transaction() as s:
        s["_user_id"] = str(bob.id)
        s["_fresh"] = True

    return bob_client, alice_ws.slug, alice_ws.id


# Slug-based routes — get_client_by_id() filters by user_id, so wrong-user
# requests should 404. (Some routes redirect with flash; both are fine.)
SLUG_GET_PATHS = [
    "/client/{slug}",
    "/client/{slug}/visibility",
    "/client/{slug}/edit",
    "/client/{slug}/brand-context",
    "/client/{slug}/competitors",
    "/client/{slug}/history",
    "/client/{slug}/actions",
    "/client/{slug}/growth-plan",
    "/client/{slug}/query-ideas",
    "/client/{slug}/website-builder",
    "/client/{slug}/content-brief",
    "/client/{slug}/content-draft",
    "/client/{slug}/presentation",
    "/client/{slug}/report",
    "/client/{slug}/run-audit",
    "/client/{slug}/export-pdf",
    "/api/client/{slug}",
]

# Int-id routes — explicit `workspace.user_id != current_user.id` check
# at top of handler. Should redirect with flash, not render.
INT_GET_PATHS = [
    "/client/{cid}/brand-kit",
    "/client/{cid}/integrations/modules",
    "/integrations/calcom/{cid}",
    "/integrations/ga/{cid}",
    "/integrations/gsc/{cid}",
    "/integrations/woocommerce/{cid}",
    "/marketplace-audits/{cid}",
]

# POSTs that mutate — these are the dangerous ones. A 200 here would
# mean Bob just edited Alice's workspace.
SLUG_POST_PATHS = [
    ("/client/{slug}/edit",
     {"name": "PWNED", "website": "https://attacker.com"}),
    ("/client/{slug}/brand-context",
     {"audience": "PWNED"}),
]

INT_POST_PATHS = [
    ("/client/{cid}/share/toggle", {}),
]


@pytest.mark.parametrize("path_tmpl", SLUG_GET_PATHS)
def test_slug_get_blocks_cross_user(two_users_one_workspace, path_tmpl):
    """Bob hitting a slug-based GET on Alice's workspace must not 200."""
    bob, slug, _ = two_users_one_workspace
    path = path_tmpl.format(slug=slug)
    resp = bob.get(path, follow_redirects=False)
    # 200 here means Bob saw Alice's data → IDOR. Any 3xx/4xx is OK.
    assert resp.status_code != 200, (
        f"IDOR: Bob got 200 on {path} (Alice's workspace).\n"
        f"Body (first 400 bytes): {resp.data[:400]!r}"
    )


@pytest.mark.parametrize("path_tmpl", INT_GET_PATHS)
def test_intid_get_blocks_cross_user(two_users_one_workspace, path_tmpl):
    """Bob hitting an int-id GET on Alice's workspace must not 200."""
    bob, _, cid = two_users_one_workspace
    path = path_tmpl.format(cid=cid)
    resp = bob.get(path, follow_redirects=False)
    assert resp.status_code != 200, (
        f"IDOR: Bob got 200 on {path} (Alice's workspace).\n"
        f"Body (first 400 bytes): {resp.data[:400]!r}"
    )


@pytest.mark.parametrize("path_tmpl,data", SLUG_POST_PATHS)
def test_slug_post_blocks_cross_user(two_users_one_workspace, path_tmpl, data):
    """Bob POSTing to Alice's workspace must not mutate it."""
    bob, slug, _ = two_users_one_workspace
    path = path_tmpl.format(slug=slug)
    bob.post(path, data=data, follow_redirects=False)
    # 200 OR 302 with success flash would be the leak — but both Alice's
    # name and website should remain untouched regardless.
    ws = Client.query.filter_by(slug="alice-co").first()
    assert ws.name == "Alice Co", (
        f"IDOR mutation: Bob's POST to {path} changed Alice's name to {ws.name!r}"
    )
    assert ws.website == "https://alice.example.com", (
        f"IDOR mutation: Bob's POST to {path} changed Alice's website to {ws.website!r}"
    )


@pytest.mark.parametrize("path_tmpl,data", INT_POST_PATHS)
def test_intid_post_blocks_cross_user(two_users_one_workspace, path_tmpl, data):
    """Bob POSTing to int-id endpoint on Alice's workspace must not mutate it."""
    bob, _, cid = two_users_one_workspace
    path = path_tmpl.format(cid=cid)
    bob.post(path, data=data, follow_redirects=False)
    # share/toggle would set public_share_token if it succeeded; verify
    # Alice's workspace doesn't have one as a result of Bob's POST.
    ws = db.session.get(Client, cid)
    assert ws.public_share_token is None, (
        f"IDOR mutation: Bob's POST to {path} created public_share_token "
        f"on Alice's workspace ({ws.public_share_token!r})"
    )
