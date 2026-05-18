"""Auth pages (login / signup / forgot / reset) SEO + social meta.

These 4 templates are standalone (don't extend marketing_base.html)
so they shipped with zero OG tags, no canonical, no favicon. A
login/signup URL pasted into Slack unfurled as bare text; browser
tabs were blank. The shared partials/auth_meta.html fixes all 4.

Indexing policy:
  - /signup        → index, follow   (acquisition page, in sitemap)
  - /login         → noindex, follow (no SEO value, just a form)
  - /forgot-password → noindex, follow
  - /reset-password/<token> → noindex, follow (token in URL —
    must never be indexed)
"""
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

from app import (
    app as flask_app, db, User, Wallet, PasswordResetToken,
)


@pytest.fixture
def anon(app_ctx):
    return flask_app.test_client()


def _assert_common_meta(body: str, page: str):
    """Every auth page must carry the shared meta block."""
    assert 'property="og:title"' in body, f"{page}: missing og:title"
    assert 'property="og:description"' in body, f"{page}: missing og:description"
    assert 'property="og:url"' in body, f"{page}: missing og:url"
    assert 'name="twitter:card"' in body, f"{page}: missing twitter:card"
    assert 'rel="canonical"' in body, f"{page}: missing canonical link"
    assert 'rel="icon"' in body and "image/svg+xml" in body, (
        f"{page}: missing inline-SVG favicon"
    )
    assert 'name="description"' in body, f"{page}: missing meta description"


def test_login_page_has_meta_and_is_noindex(anon):
    body = anon.get("/login").data.decode()
    _assert_common_meta(body, "login")
    assert 'content="noindex, follow"' in body, (
        "login should be noindex — it's a form with no SEO value"
    )


def test_signup_page_has_meta_and_is_indexable(anon):
    """signup is the acquisition page + in sitemap.xml → must be
    indexable (index, follow), NOT noindex."""
    body = anon.get("/signup").data.decode()
    _assert_common_meta(body, "signup")
    assert 'content="index, follow"' in body, (
        "signup must be index,follow — it's in sitemap.xml as an "
        "acquisition page. A noindex here silently delists it."
    )
    assert 'content="noindex' not in body, (
        "signup has a noindex directive — contradicts sitemap.xml"
    )


def test_forgot_password_has_meta_and_is_noindex(anon):
    body = anon.get("/forgot-password").data.decode()
    _assert_common_meta(body, "forgot-password")
    assert 'content="noindex, follow"' in body


def test_reset_password_has_meta_and_is_noindex(app_ctx):
    """reset-password has a token in the URL — must NEVER be
    indexed. Needs a valid token to render the form."""
    u = User(
        email="reset@meta.test",
        password_hash=generate_password_hash("xx"),
        name="Reset", plan="free",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=3))
    token = secrets.token_urlsafe(32)
    db.session.add(PasswordResetToken(
        user_id=u.id, token=token,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    ))
    db.session.commit()

    resp = flask_app.test_client().get(f"/reset-password/{token}")
    assert resp.status_code == 200
    body = resp.data.decode()
    _assert_common_meta(body, "reset-password")
    assert 'content="noindex, follow"' in body, (
        "reset-password MUST be noindex — the URL contains a "
        "single-use token. Indexing it would leak tokens into "
        "search results."
    )


def test_auth_meta_canonical_uses_request_host(anon):
    """Canonical should build off the live request host so dev /
    staging / prod each get their own correct canonical (no
    hardcoded production domain that drifts)."""
    resp = anon.get("/login", base_url="https://app.example.test")
    body = resp.data.decode()
    assert 'href="https://app.example.test/login"' in body, (
        "Canonical didn't track the request host — found a hardcoded "
        "or wrong canonical URL."
    )
