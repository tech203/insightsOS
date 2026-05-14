"""
Tests for the email verification surface introduced in PR #91.

The policy under test:
    1. New users can use the free tier immediately. Verification is
       best-effort at signup.
    2. Stripe checkout (both bundle and plan) is gated on
       email_verified_at being set. Admin / dev_unlimited users skip
       the gate.
    3. Token consume (GET /verify-email/<token>) is idempotent for
       valid tokens and bounces invalid/expired tokens.
    4. Resend (POST /verify-email/resend) is rate-limited to 60s per
       user.
    5. Signup wires up the verification token mint + email send.

Helpers tested directly (no HTTP layer):
    - email_verification_required_for_user
    - issue_email_verification_token
    - issue_and_send_email_verification
"""

from __future__ import annotations

from datetime import timedelta
from dtutils import utcnow
from unittest.mock import patch


from app import (
    EmailVerificationToken,
    User,
    db,
    email_verification_required_for_user,
    issue_and_send_email_verification,
    issue_email_verification_token,
)
from app import app as flask_app


# ---------------------------------------------------------------------------
# email_verification_required_for_user
# ---------------------------------------------------------------------------

class TestEmailVerificationRequired:
    def test_verified_user_returns_false(self, user):
        # Default `user` fixture is verified (email_verified_at set)
        assert email_verification_required_for_user(user) is False

    def test_unverified_user_returns_true(self, make_user):
        u = make_user(email_verified=False)
        assert email_verification_required_for_user(u) is True

    def test_none_user_returns_true(self, app_ctx):
        # Defensive: anonymous / missing user is treated as
        # unverified — the caller might check this before knowing
        # whether the request is authenticated.
        assert email_verification_required_for_user(None) is True


# ---------------------------------------------------------------------------
# issue_email_verification_token
# ---------------------------------------------------------------------------

class TestIssueToken:
    def test_creates_row_with_24h_ttl(self, make_user):
        u = make_user(email_verified=False)
        row = issue_email_verification_token(u)

        assert row.id is not None
        assert row.user_id == u.id
        assert row.used_at is None
        assert row.expires_at > utcnow() + timedelta(hours=23)
        assert row.expires_at < utcnow() + timedelta(hours=25)

    def test_token_is_url_safe_string(self, make_user):
        u = make_user(email_verified=False)
        row = issue_email_verification_token(u)
        # secrets.token_urlsafe(32) → ~43 chars, alphanumeric + - + _
        assert len(row.token) >= 40
        assert all(c.isalnum() or c in "-_" for c in row.token)

    def test_multiple_outstanding_tokens_allowed(self, make_user):
        """Resend mints a new token without invalidating the old one —
        whichever lands first wins. The unique constraint is on token,
        not user_id."""
        u = make_user(email_verified=False)
        r1 = issue_email_verification_token(u)
        r2 = issue_email_verification_token(u)
        assert r1.token != r2.token
        assert (
            EmailVerificationToken.query.filter_by(user_id=u.id).count()
            == 2
        )


# ---------------------------------------------------------------------------
# issue_and_send_email_verification
# ---------------------------------------------------------------------------

class TestIssueAndSend:
    def test_returns_row_and_delivered_flag(self, make_user):
        u = make_user(email_verified=False)
        # Email isn't configured in test env; helper returns False
        # without raising.
        with flask_app.test_request_context("/"):
            row, delivered = issue_and_send_email_verification(u)
        assert row is not None
        assert delivered is False  # no SMTP / Resend wired up in tests

    def test_returns_true_when_email_send_succeeds(self, make_user):
        u = make_user(email_verified=False)
        # Patch the helper's send_email to simulate delivery
        with patch("app.send_email_verification", return_value=True):
            with flask_app.test_request_context("/"):
                row, delivered = issue_and_send_email_verification(u)
        assert delivered is True


# ---------------------------------------------------------------------------
# GET /verify-email/<token>
# ---------------------------------------------------------------------------

class TestVerifyEmailRoute:
    def test_valid_token_sets_email_verified_at(self, app_ctx, make_user):
        u = make_user(email_verified=False)
        row = issue_email_verification_token(u)

        c = flask_app.test_client()
        r = c.get(f"/verify-email/{row.token}", follow_redirects=False)
        # Anonymous click redirects to /login with success flash
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

        # User now has verified timestamp
        fresh = db.session.get(User, u.id)
        assert fresh.email_verified_at is not None

        # Token marked used
        fresh_row = db.session.get(EmailVerificationToken, row.id)
        assert fresh_row.used_at is not None

    def test_logged_in_user_redirected_to_settings(self, app_ctx, make_user):
        u = make_user(email_verified=False)
        row = issue_email_verification_token(u)

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(u.id)
            s["_fresh"] = True
        r = c.get(f"/verify-email/{row.token}", follow_redirects=False)
        assert r.status_code == 302
        assert "/settings" in (r.headers.get("Location") or "")

    def test_invalid_token_redirects_with_warning(self, app_ctx):
        c = flask_app.test_client()
        r = c.get("/verify-email/totally-fake-token", follow_redirects=False)
        # No matching row → bounced to /login
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_expired_token_rejected(self, app_ctx, make_user):
        u = make_user(email_verified=False)
        row = EmailVerificationToken(
            user_id=u.id,
            token="expired-token-123",
            expires_at=utcnow() - timedelta(hours=1),
        )
        db.session.add(row)
        db.session.commit()

        c = flask_app.test_client()
        r = c.get(f"/verify-email/{row.token}", follow_redirects=False)
        # Expired → treated as invalid → bounce. user remains unverified.
        assert r.status_code == 302
        fresh = db.session.get(User, u.id)
        assert fresh.email_verified_at is None

    def test_used_token_rejected(self, app_ctx, make_user):
        u = make_user(email_verified=False)
        row = issue_email_verification_token(u)
        # Mark used + commit
        row.used_at = utcnow()
        db.session.commit()

        c = flask_app.test_client()
        r = c.get(f"/verify-email/{row.token}", follow_redirects=False)
        # Single-use — second click bounces.
        assert r.status_code == 302
        # User stays unverified since the token's already been consumed
        # (and the act of marking it used didn't carry a verification).
        fresh = db.session.get(User, u.id)
        assert fresh.email_verified_at is None


# ---------------------------------------------------------------------------
# POST /verify-email/resend
# ---------------------------------------------------------------------------

class TestResendRoute:
    def test_anonymous_redirected(self, app_ctx):
        c = flask_app.test_client()
        r = c.post("/verify-email/resend", follow_redirects=False)
        # login_required → /login
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_already_verified_short_circuits(self, app_ctx, user):
        """Verified users hitting /resend get a friendly flash, no new
        token minted."""
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(user.id)
            s["_fresh"] = True

        before = EmailVerificationToken.query.filter_by(user_id=user.id).count()
        r = c.post("/verify-email/resend", follow_redirects=False)
        after = EmailVerificationToken.query.filter_by(user_id=user.id).count()

        assert r.status_code == 302
        assert after == before  # no new token

    def test_resend_mints_new_token(self, app_ctx, make_user):
        u = make_user(email_verified=False)
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(u.id)
            s["_fresh"] = True

        before = EmailVerificationToken.query.filter_by(user_id=u.id).count()
        r = c.post("/verify-email/resend", follow_redirects=False)
        after = EmailVerificationToken.query.filter_by(user_id=u.id).count()

        assert r.status_code == 302
        assert after == before + 1

    def test_resend_within_60s_blocked(self, app_ctx, make_user):
        """Second resend within EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS
        is refused — prevents inbox-flooding via a stuck button."""
        u = make_user(email_verified=False)
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(u.id)
            s["_fresh"] = True

        c.post("/verify-email/resend", follow_redirects=False)
        count_after_first = EmailVerificationToken.query.filter_by(
            user_id=u.id
        ).count()

        # Immediate second call should NOT mint another token.
        c.post("/verify-email/resend", follow_redirects=False)
        count_after_second = EmailVerificationToken.query.filter_by(
            user_id=u.id
        ).count()

        assert count_after_second == count_after_first

    def test_resend_after_60s_allowed(self, app_ctx, make_user):
        """Backdating the previous token's created_at past the cooldown
        window should let a fresh resend through."""
        u = make_user(email_verified=False)
        first_row = issue_email_verification_token(u)
        first_row.created_at = utcnow() - timedelta(seconds=120)
        db.session.commit()

        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(u.id)
            s["_fresh"] = True

        before = EmailVerificationToken.query.filter_by(user_id=u.id).count()
        c.post("/verify-email/resend", follow_redirects=False)
        after = EmailVerificationToken.query.filter_by(user_id=u.id).count()

        assert after == before + 1


# ---------------------------------------------------------------------------
# Stripe checkout gates
# ---------------------------------------------------------------------------

class TestCheckoutGate:
    def test_unverified_user_blocked_from_bundle_checkout(
        self, app_ctx, make_user,
    ):
        u = make_user(email_verified=False)
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(u.id)
            s["_fresh"] = True
        r = c.post("/stripe/checkout/bundle/5", follow_redirects=False)
        # Bounced to /settings with a flash, NOT to Stripe
        assert r.status_code == 302
        location = r.headers.get("Location") or ""
        assert "/settings" in location
        assert "stripe" not in location.lower()

    def test_unverified_user_blocked_from_plan_checkout(
        self, app_ctx, make_user,
    ):
        u = make_user(email_verified=False)
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(u.id)
            s["_fresh"] = True
        r = c.post("/stripe/checkout/plan/pro", follow_redirects=False)
        assert r.status_code == 302
        location = r.headers.get("Location") or ""
        assert "/settings" in location
        assert "stripe" not in location.lower()

    def test_admin_user_skips_verification_gate(
        self, app_ctx, make_user, monkeypatch,
    ):
        """Admin / dev_unlimited users bypass the gate so internal
        accounts aren't locked out of their own product."""
        admin = make_user(role="admin", email_verified=False)
        c = flask_app.test_client()
        with c.session_transaction() as s:
            s["_user_id"] = str(admin.id)
            s["_fresh"] = True
        # Stripe isn't configured in tests → admin should pass the
        # verification gate and hit the next branch ("Stripe isn't
        # configured" flash to /settings/credits).
        r = c.post("/stripe/checkout/bundle/5", follow_redirects=False)
        assert r.status_code == 302
        # Not bounced back to /settings (the verify-gate destination).
        # The Stripe-not-configured destination is /settings/credits.
        location = r.headers.get("Location") or ""
        assert "/settings/credits" in location


# ---------------------------------------------------------------------------
# Signup wires up verification
# ---------------------------------------------------------------------------

class TestSignupWiring:
    def test_signup_creates_unverified_user(self, app_ctx):
        c = flask_app.test_client()
        # POST signup form. The route generates referral code +
        # wallet + verification token; assert the user lands
        # unverified with a pending token row.
        r = c.post("/signup", data={
            "name": "Brand New",
            "email": "fresh@signup.test",
            "password": "supersecure12",
            "confirm_password": "supersecure12",
        }, follow_redirects=False)

        # Successful signup redirects somewhere — exact destination
        # varies (workspace create vs onboarding). What matters is
        # the user exists, unverified, with a token.
        assert r.status_code == 302

        u = User.query.filter_by(email="fresh@signup.test").first()
        assert u is not None
        assert u.email_verified_at is None

        # One verification token should have been minted as part of
        # the best-effort send. Even when email delivery fails (test
        # env has no SMTP), the token persists so /verify-email/resend
        # has something to compare its cooldown against.
        token_count = EmailVerificationToken.query.filter_by(
            user_id=u.id
        ).count()
        assert token_count >= 1
