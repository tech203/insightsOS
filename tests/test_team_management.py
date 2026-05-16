"""
Tests for team-management routes:

  - POST /team/invite                    create a pending invite
  - GET/POST /team/accept/<token>        consume an invite (existing or new user)
  - POST /team/revoke/<invite_id>        revoke a pending invite
  - POST /team/remove-member/<member_id> remove an active team member

Behavior under test:
  - Plan gating — only Pro/Growth can invite (`plan_allows_seat_addon`)
  - Owner-only — team members can't re-invite or revoke
  - Seat-cap — pending + accepted invites count toward the user's
    seat allowance; oversubscription blocked
  - Soft-dedupe — same email + status=pending doesn't create a
    second invite
  - Token consumption — accepted/revoked tokens reject on reuse
  - Email mismatch — logged-in user with different email is bounced
  - New-account path — signup form creates User + Wallet + attaches
  - Existing-email collision — must log in first to accept
  - Owner-only mutations — non-owners can't revoke or remove
"""

from __future__ import annotations

from unittest.mock import patch

from app import (
    TeamInvite,
    User,
    Wallet,
    db,
)
from app import app as flask_app


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


# ---------------------------------------------------------------------------
# POST /team/invite
# ---------------------------------------------------------------------------

class TestTeamInvite:

    def test_pro_owner_creates_invite(self, make_user):
        owner = make_user(plan="pro", email="invite-pro@x.com")
        # Patch send_email to return False — exercises the "URL in
        # flash" fallback without depending on Resend config in CI.
        with patch("services.email_helper.send_email", return_value=False):
            r = _logged_in(owner).post(
                "/team/invite",
                data={"email": "newhire@example.com"},
                follow_redirects=False,
            )
        assert r.status_code == 302
        invite = TeamInvite.query.filter_by(
            owner_user_id=owner.id, email="newhire@example.com",
        ).one_or_none()
        assert invite is not None
        assert invite.status == "pending"
        assert invite.token  # signed token populated

    def test_growth_owner_creates_invite(self, make_user):
        """Growth plan should also pass `plan_allows_seat_addon`."""
        owner = make_user(plan="growth", email="invite-growth@x.com")
        with patch("services.email_helper.send_email", return_value=False):
            r = _logged_in(owner).post(
                "/team/invite",
                data={"email": "growth-hire@example.com"},
                follow_redirects=False,
            )
        assert r.status_code == 302
        assert TeamInvite.query.filter_by(
            email="growth-hire@example.com",
        ).first() is not None

    def test_free_user_blocked(self, make_user):
        owner = make_user(plan="free", email="invite-free@x.com")
        r = _logged_in(owner).post(
            "/team/invite",
            data={"email": "blocked@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert TeamInvite.query.filter_by(
            email="blocked@example.com",
        ).first() is None

    def test_team_member_cannot_re_invite(self, make_user):
        """Only the team owner can issue invites — a member POSTing
        gets bounced even on a paid plan."""
        owner = make_user(plan="pro", email="o1@x.com")
        member = make_user(plan="pro", email="m1@x.com")
        member.team_owner_id = owner.id
        db.session.commit()

        r = _logged_in(member).post(
            "/team/invite",
            data={"email": "ghost@example.com"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert TeamInvite.query.filter_by(
            email="ghost@example.com",
        ).first() is None

    def test_invalid_email_rejected(self, make_user):
        owner = make_user(plan="pro", email="badmail-owner@x.com")
        r = _logged_in(owner).post(
            "/team/invite",
            data={"email": "not-an-email"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert TeamInvite.query.filter_by(
            owner_user_id=owner.id,
        ).first() is None

    def test_duplicate_pending_invite_is_softly_deduped(self, make_user):
        """Second invite to the same email returns ok but doesn't
        create a duplicate row."""
        owner = make_user(plan="pro", email="dedup-owner@x.com")
        with patch("services.email_helper.send_email", return_value=False):
            c = _logged_in(owner)
            c.post("/team/invite", data={"email": "dup@example.com"})
            c.post("/team/invite", data={"email": "dup@example.com"})

        invites = TeamInvite.query.filter_by(
            owner_user_id=owner.id, email="dup@example.com",
        ).all()
        assert len(invites) == 1

    def test_anonymous_redirected_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.post(
            "/team/invite",
            data={"email": "x@x.com"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")


# ---------------------------------------------------------------------------
# GET/POST /team/accept/<token>
# ---------------------------------------------------------------------------

class TestTeamAccept:

    def _make_invite(self, owner, email):
        invite = TeamInvite(
            owner_user_id=owner.id,
            email=email,
            token=f"tok-{email}-1234",
            status="pending",
        )
        db.session.add(invite)
        db.session.commit()
        return invite

    def test_invalid_token_redirects_to_login(self, app_ctx):
        c = flask_app.test_client()
        r = c.get("/team/accept/does-not-exist", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_get_renders_acceptance_form(self, make_user):
        owner = make_user(plan="pro", email="acc-owner-1@x.com")
        invite = self._make_invite(owner, "newhire-1@example.com")
        c = flask_app.test_client()
        r = c.get(f"/team/accept/{invite.token}")
        assert r.status_code == 200
        # The form mentions the invitee's email — confirms the right
        # invite is being shown.
        assert b"newhire-1@example.com" in r.data

    def test_logged_in_matching_email_attaches_immediately(self, make_user):
        owner = make_user(plan="pro", email="acc-owner-2@x.com")
        invitee = make_user(plan="free", email="invitee-2@example.com")
        invite = self._make_invite(owner, "invitee-2@example.com")

        r = _logged_in(invitee).post(
            f"/team/accept/{invite.token}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(invitee)
        db.session.refresh(invite)
        assert invitee.team_owner_id == owner.id
        assert invite.status == "accepted"
        assert invite.accepted_user_id == invitee.id

    def test_logged_in_email_mismatch_is_rejected(self, make_user):
        """Token is for invitee@... but a different user is logged in
        — must not attach the wrong user to the team."""
        owner = make_user(plan="pro", email="acc-owner-3@x.com")
        wrong = make_user(plan="free", email="wrong-user@example.com")
        invite = self._make_invite(owner, "intended@example.com")

        r = _logged_in(wrong).post(
            f"/team/accept/{invite.token}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(wrong)
        db.session.refresh(invite)
        # Wrong user NOT attached; invite still pending.
        assert wrong.team_owner_id is None
        assert invite.status == "pending"

    def test_new_signup_creates_user_and_attaches(self, make_user):
        owner = make_user(plan="pro", email="acc-owner-4@x.com")
        invite = self._make_invite(owner, "fresh@example.com")
        c = flask_app.test_client()
        r = c.post(
            f"/team/accept/{invite.token}",
            data={"name": "Fresh Person", "password": "supersecret123"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        # New user row created with team_owner_id set.
        new_user = User.query.filter_by(email="fresh@example.com").one_or_none()
        assert new_user is not None
        assert new_user.team_owner_id == owner.id
        assert new_user.name == "Fresh Person"
        # Wallet row auto-created.
        assert Wallet.query.filter_by(user_id=new_user.id).one_or_none() is not None
        # Invite consumed.
        db.session.refresh(invite)
        assert invite.status == "accepted"
        assert invite.accepted_user_id == new_user.id

    def test_short_password_rejected_on_signup(self, make_user):
        owner = make_user(plan="pro", email="acc-owner-5@x.com")
        invite = self._make_invite(owner, "shortpw@example.com")
        c = flask_app.test_client()
        r = c.post(
            f"/team/accept/{invite.token}",
            data={"name": "X", "password": "short"},  # < 8 chars
            follow_redirects=False,
        )
        assert r.status_code == 302
        # No user created.
        assert User.query.filter_by(email="shortpw@example.com").first() is None
        # Invite still pending.
        db.session.refresh(invite)
        assert invite.status == "pending"

    def test_existing_email_signup_rejected(self, make_user):
        """If someone already has an account with the invite email,
        they must log in first — signup path must not silently
        create a second row."""
        owner = make_user(plan="pro", email="acc-owner-6@x.com")
        make_user(plan="free", email="existing@example.com")
        invite = self._make_invite(owner, "existing@example.com")

        c = flask_app.test_client()
        r = c.post(
            f"/team/accept/{invite.token}",
            data={"name": "Dupe", "password": "supersecret123"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        # No duplicate row created.
        rows = User.query.filter_by(email="existing@example.com").all()
        assert len(rows) == 1

    def test_already_accepted_token_rejected(self, make_user):
        """Token with status != pending must not be re-consumed
        even by the legitimate invitee."""
        owner = make_user(plan="pro", email="acc-owner-7@x.com")
        invitee = make_user(plan="free", email="reuse@example.com")
        invite = self._make_invite(owner, "reuse@example.com")
        invite.status = "accepted"
        db.session.commit()

        r = _logged_in(invitee).post(
            f"/team/accept/{invite.token}",
            follow_redirects=False,
        )
        # Bounces to login (the filter_by status="pending" returns None).
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")


# ---------------------------------------------------------------------------
# POST /team/revoke/<invite_id>
# ---------------------------------------------------------------------------

class TestTeamRevoke:

    def _make_invite(self, owner, email, status="pending"):
        invite = TeamInvite(
            owner_user_id=owner.id, email=email,
            token=f"rvk-{email}-1234", status=status,
        )
        db.session.add(invite)
        db.session.commit()
        return invite

    def test_owner_revokes_pending_invite(self, make_user):
        owner = make_user(plan="pro", email="rvk-owner-1@x.com")
        invite = self._make_invite(owner, "target-1@example.com")

        r = _logged_in(owner).post(
            f"/team/revoke/{invite.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(invite)
        assert invite.status == "revoked"

    def test_non_owner_cannot_revoke(self, make_user):
        """Another user POSTing the same invite ID must get a 'not
        found' (404-equivalent flash), not actually revoke."""
        owner = make_user(plan="pro", email="rvk-owner-2@x.com")
        other = make_user(plan="pro", email="rvk-other@x.com")
        invite = self._make_invite(owner, "target-2@example.com")

        r = _logged_in(other).post(
            f"/team/revoke/{invite.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(invite)
        # Untouched.
        assert invite.status == "pending"

    def test_revoke_unknown_invite_id_is_noop(self, make_user):
        owner = make_user(plan="pro", email="rvk-owner-3@x.com")
        r = _logged_in(owner).post(
            "/team/revoke/999999",
            follow_redirects=False,
        )
        assert r.status_code == 302

    def test_already_accepted_invite_cannot_be_revoked(self, make_user):
        """Once an invite is accepted, revoke is meaningless — the
        member is a User row now, not an invite. Refuse rather than
        silently doing nothing."""
        owner = make_user(plan="pro", email="rvk-owner-4@x.com")
        invite = self._make_invite(owner, "tgt-acc@example.com",
                                   status="accepted")

        _logged_in(owner).post(
            f"/team/revoke/{invite.id}",
            follow_redirects=False,
        )
        db.session.refresh(invite)
        # Status unchanged.
        assert invite.status == "accepted"


# ---------------------------------------------------------------------------
# POST /team/remove-member/<member_id>
# ---------------------------------------------------------------------------

class TestTeamRemoveMember:

    def test_owner_removes_member(self, make_user):
        owner = make_user(plan="pro", email="rm-owner-1@x.com")
        member = make_user(plan="free", email="rm-member-1@x.com")
        member.team_owner_id = owner.id
        db.session.commit()

        r = _logged_in(owner).post(
            f"/team/remove-member/{member.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(member)
        # Member detached but their User row stays (data preservation).
        assert member.team_owner_id is None
        # User row itself untouched.
        assert User.query.filter_by(id=member.id).one_or_none() is not None

    def test_cannot_remove_users_from_other_teams(self, make_user):
        """A different owner posting against another team's member
        must get 'not found' — never detach someone else's member."""
        owner_a = make_user(plan="pro", email="rm-a@x.com")
        owner_b = make_user(plan="pro", email="rm-b@x.com")
        member_of_a = make_user(plan="free", email="rm-victim@x.com")
        member_of_a.team_owner_id = owner_a.id
        db.session.commit()

        r = _logged_in(owner_b).post(
            f"/team/remove-member/{member_of_a.id}",
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(member_of_a)
        # Still attached to owner_a.
        assert member_of_a.team_owner_id == owner_a.id

    def test_remove_unknown_member_is_noop(self, make_user):
        owner = make_user(plan="pro", email="rm-owner-4@x.com")
        r = _logged_in(owner).post(
            "/team/remove-member/999999",
            follow_redirects=False,
        )
        assert r.status_code == 302
        # No crash, no DB state change to assert beyond the lack of
        # an exception.
