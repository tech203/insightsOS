"""Smoke test: parametrized GET routes return < 500 for missing resources.

Most parametrized routes (e.g. /client/<id>, /audit/<filename>) take a
resource ID. The most common 500 in this kind of code is "I forgot to
handle the case where the row doesn't exist" — the route either calls
.get_or_404() (good — 404 not 500), redirects with a flash (good — 302
not 500), or dereferences a None (bad — AttributeError → 500).

This test passes a guaranteed-nonexistent value to every parametrized
GET route and asserts the response is < 500. We don't care whether
it's 404, 403, or 302 — just that it's not a server crash.

Routes intentionally skipped (require external state we can't easily
mock without making the test slow or flaky):
- /audit/<summary_filename>* — needs an audit file in S3/disk
- /stripe/checkout/* — round-trips to Stripe
- /auth/google/callback — needs an OAuth code
"""
import pytest
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Wallet, CreditTransaction
from tests.conftest import utcnow


# Each entry: (route_pattern, probe_path, expected_codes_range)
# probe_path uses a ID/token guaranteed not to exist.
# All assertions are "code < 500"; the comments below note what we
# actually expect (404/302/etc.) so a future regression that flips one
# from 404 to 500 is obvious.
PROBE_PATHS = [
    # Client-scoped (slug-based — slug "no-such-client" is unique)
    "/client/no-such-client",
    "/client/no-such-client/actions",
    "/client/no-such-client/brand-context",
    "/client/no-such-client/competitors",
    "/client/no-such-client/competitors/auto-detect",
    "/client/no-such-client/content-brief",
    "/client/no-such-client/content-draft",
    "/client/no-such-client/edit",
    "/client/no-such-client/export-pdf",
    "/client/no-such-client/growth-plan",
    "/client/no-such-client/history",
    "/client/no-such-client/presentation",
    "/client/no-such-client/query-ideas",
    "/client/no-such-client/report",
    "/client/no-such-client/run-audit",
    "/client/no-such-client/visibility",
    "/client/no-such-client/website-builder",
    "/client/no-such-client/website-builder/brand-kit",
    "/client/no-such-client/website-engine/demo",
    # Client-scoped (int IDs)
    "/client/999999/brand-kit",
    "/client/999999/integrations/modules",
    # API
    "/api/client/no-such-client",
    # Integrations (int client_id)
    "/integrations/calcom/999999",
    "/integrations/ga/999999",
    "/integrations/gsc/999999",
    "/integrations/gsc/connect/999999",
    "/integrations/shopify/connect/999999",
    "/integrations/shopify/products/999999",
    "/integrations/woocommerce/999999",
    "/marketplace-audits/999999",
    # Content queue items (string id, file-backed)
    "/content-queue/no-such-item/ai-edit/history",
    "/content-queue/no-such-item/ai-edit/sections",
    "/content-queue/no-such-item/brief",
    "/content-queue/no-such-item/generate-page",
    "/generate-brief/no-such-item",
    "/generate-draft/no-such-item",
    # Generated content (int prompt_id)
    "/generate-content/999999",
    # Tokens — guaranteed-bad strings
    "/report/bogus-share-token",
    "/report/bogus-share-token/pdf",
    "/reset-password/bogus-reset-token",
    "/team/accept/bogus-invite-token",
    "/verify-email/bogus-verify-token",
    # Plan slugs
    "/subscribe/no-such-plan",
    # Website builder / engine (int IDs)
    "/site/999999",
    "/site/999999/anything",
    "/p/999999/anything",
    "/website-builder/project/999999/preview",
    "/website-engine/page/999999/preview",
    # Admin (int IDs)
    "/admin/campaigns/999999",
    "/admin/users/999999",
    "/admin/users/999999/activity",
    # Dev shortcuts
    "/dev/set-plan/no-such-plan",
    "/dev/view-mode/no-such-mode",
]


@pytest.fixture
def admin_client(app_ctx):
    """Test client logged in as an admin so admin routes don't 403 first."""
    u = User(
        email="param-smoke@test.com",
        password_hash=generate_password_hash("xxxxxxxx"),
        name="Param Smoke",
        plan="growth",
        role="admin",
        email_verified_at=utcnow(),
    )
    db.session.add(u)
    db.session.flush()
    u.wallet = Wallet(user_id=u.id, balance=100)
    db.session.add(u.wallet)
    db.session.add(CreditTransaction(
        user_id=u.id,
        type="monthly_allowance",
        amount=0,
        balance_after=100,
        notes="Param smoke fixture",
    ))
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
    return c


@pytest.mark.parametrize("path", PROBE_PATHS)
def test_param_route_no_500(admin_client, path):
    """Every route should respond with < 500 for non-existent resources.
    302 (redirect with flash), 404 (not found), or 403 (forbidden) are
    all fine — only 5xx counts as a bug."""
    resp = admin_client.get(path, follow_redirects=False)
    assert resp.status_code < 500, (
        f"{path} returned {resp.status_code}\n"
        f"Body (first 500 bytes): {resp.data[:500]!r}"
    )


# ---------------------------------------------------------------------------
# Happy path — real Client, real workspace routes
# ---------------------------------------------------------------------------
#
# The negative-case test above proves routes don't crash on missing data.
# This second pass exercises the same routes with a *real* Client record,
# which catches a different class of bug: template variables that are
# undefined when the workspace exists but has no audits, no brand kit,
# no integrations yet — i.e. the "freshly-created workspace" state every
# new user hits.
#
# These are the most-trafficked workspace pages — the dashboard pages a
# brand-new user sees right after `/clients/new` redirects them in.

CLIENT_HAPPY_PATHS = [
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
    "/client/{slug}/website-builder/brand-kit",
    "/api/client/{slug}",
]


@pytest.fixture
def client_with_workspace(app_ctx):
    """Logged-in test client + a real Client workspace owned by them.

    Mimics the just-after-/clients/new state every new user lands in:
    workspace exists, but has no audits, no integrations, no brand
    kit yet. This is the exact state most page-load 500s lurk in.
    """
    from app import Client

    u = User(
        email="happy-smoke@test.com",
        password_hash=generate_password_hash("xxxxxxxx"),
        name="Happy Smoke",
        plan="growth",
        role="user",
        email_verified_at=utcnow(),
    )
    db.session.add(u)
    db.session.flush()
    u.wallet = Wallet(user_id=u.id, balance=100)
    db.session.add(u.wallet)
    db.session.add(CreditTransaction(
        user_id=u.id,
        type="monthly_allowance",
        amount=0,
        balance_after=100,
        notes="Happy smoke fixture",
    ))

    workspace = Client(
        slug="acme-co",
        user_id=u.id,
        name="Acme Co",
        website="https://acme.example.com",
        website_normalized="acme.example.com",
        industry="SaaS",
        location="Remote",
        owner_type="company",
    )
    db.session.add(workspace)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
    return c, workspace.slug


@pytest.mark.parametrize("path_tmpl", CLIENT_HAPPY_PATHS)
def test_client_route_happy_path(client_with_workspace, path_tmpl):
    """Each workspace route should render without 500 for a freshly-
    created workspace (no audits, no brand kit, no integrations yet)."""
    c, slug = client_with_workspace
    path = path_tmpl.format(slug=slug)
    resp = c.get(path, follow_redirects=False)
    assert resp.status_code < 500, (
        f"{path} returned {resp.status_code}\n"
        f"Body (first 500 bytes): {resp.data[:500]!r}"
    )
