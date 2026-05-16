"""
Tests for the workspace brand-context routes:

  GET/POST /client/<id>/brand-context          — form view + save
  POST     /client/<id>/brand-context/suggest  — AI-drafted fields (JSON)
  POST     /client/<id>/refresh-profile        — Tavily/profile sync

These sit on the onboarding journey (workspace → brand context →
audit) and the post-onboarding "edit your brand context" path. Save
behaviour writes free-text `Client.notes` in a structured shape that
downstream content-brief and content-draft generators parse via
`has_brand_context`. The AI suggest endpoint returns JSON for the
form's "auto-fill" button; profile refresh kicks Tavily research.

Lockdowns:
  - Save flow writes the structured "Target audience:\\n... " blob
    and redirects to the correct `next` target (audit / workspace /
    default), preserving the onboarding stepper
  - User isolation: workspaces owned by another user 404
  - AI suggest mocks OpenAI, asserts JSON whitelist (allowed keys
    only), and verifies 502 on failure (Resend-style "never raise"
    error contract for the caller)
  - Profile refresh handles Tavily exceptions gracefully (flash +
    redirect, no 500)
"""

from __future__ import annotations

from unittest.mock import patch

from app import Client, db
from app import app as flask_app


def _logged_in(user):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(user.id)
        s["_fresh"] = True
    return c


def _workspace(user, slug="bc-ws"):
    ws = Client(
        slug=slug,
        user_id=user.id,
        name="Acme Co",
        website="https://acme.example.com",
        website_normalized="acme.example.com",
        industry="SaaS",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.commit()
    return ws


# ---------------------------------------------------------------------------
# GET /client/<id>/brand-context
# ---------------------------------------------------------------------------

class TestBrandContextGet:

    def test_owner_can_view_form(self, make_user):
        u = make_user(plan="pro", email="bc-get-ok@x.com")
        ws = _workspace(u)
        r = _logged_in(u).get(f"/client/{ws.slug}/brand-context")
        assert r.status_code == 200
        # Form rendered with the workspace name visible.
        assert b"Acme Co" in r.data

    def test_unknown_workspace_returns_404(self, make_user):
        u = make_user(plan="pro", email="bc-get-404@x.com")
        r = _logged_in(u).get("/client/does-not-exist/brand-context")
        assert r.status_code == 404

    def test_other_users_workspace_returns_404(self, make_user):
        """User isolation: a workspace owned by user A is invisible
        to user B even via direct slug guess."""
        owner = make_user(plan="pro", email="bc-get-owner@x.com")
        intruder = make_user(plan="pro", email="bc-get-intruder@x.com")
        ws = _workspace(owner, slug="bc-owner-ws")

        r = _logged_in(intruder).get(f"/client/{ws.slug}/brand-context")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /client/<id>/brand-context
# ---------------------------------------------------------------------------

class TestBrandContextSave:

    def _form_data(self, **overrides):
        base = {
            "audience": "small SaaS teams",
            "services": "AEO audits, AI content drafts",
            "differentiators": "Multi-engine answer monitor",
            "proof": "Used by 200+ early adopters",
            "tone": "Direct, helpful, friendly",
            "locations": "Singapore, US",
            "avoid": "Hype, jargon",
            "extra_notes": "B2B focus",
        }
        base.update(overrides)
        return base

    def test_save_writes_structured_notes(self, make_user):
        u = make_user(plan="pro", email="bc-save@x.com")
        ws = _workspace(u)
        r = _logged_in(u).post(
            f"/client/{ws.slug}/brand-context",
            data=self._form_data(),
            follow_redirects=False,
        )
        assert r.status_code == 302
        db.session.refresh(ws)
        assert ws.notes is not None
        # Structured blob — header lines downstream code parses
        # via has_brand_context.
        assert "Target audience:" in ws.notes
        assert "small SaaS teams" in ws.notes
        assert "Main services / products:" in ws.notes
        assert "AEO audits" in ws.notes

    def test_next_audit_redirects_to_audit_form(self, make_user):
        """Onboarding flow: next=audit sends user to the audit form
        so the stepper keeps moving."""
        u = make_user(plan="pro", email="bc-next-audit@x.com")
        ws = _workspace(u)
        r = _logged_in(u).post(
            f"/client/{ws.slug}/brand-context",
            data={**self._form_data(), "next": "audit"},
            follow_redirects=False,
        )
        loc = r.headers.get("Location") or ""
        # new_audit route — workspace audit form.
        assert "/audit" in loc or "/run-audit" in loc

    def test_next_workspace_redirects_to_client_detail(self, make_user):
        """Returning user editing context: next=workspace sends back
        to the workspace overview."""
        u = make_user(plan="pro", email="bc-next-ws@x.com")
        ws = _workspace(u)
        r = _logged_in(u).post(
            f"/client/{ws.slug}/brand-context",
            data={**self._form_data(), "next": "workspace"},
            follow_redirects=False,
        )
        loc = r.headers.get("Location") or ""
        # client_detail uses /client/<id>.
        assert f"/client/{ws.id}" in loc

    def test_default_redirect_to_query_ideas(self, make_user):
        """No `next` param → falls through to query-ideas (legacy
        default that the form has historically pointed at)."""
        u = make_user(plan="pro", email="bc-next-default@x.com")
        ws = _workspace(u)
        r = _logged_in(u).post(
            f"/client/{ws.slug}/brand-context",
            data=self._form_data(),
            follow_redirects=False,
        )
        loc = r.headers.get("Location") or ""
        assert "query-ideas" in loc or "/client/" in loc

    def test_save_overwrites_existing_notes(self, make_user):
        u = make_user(plan="pro", email="bc-overwrite@x.com")
        ws = _workspace(u)
        ws.notes = "Old free-text notes from before brand-context"
        db.session.commit()

        _logged_in(u).post(
            f"/client/{ws.slug}/brand-context",
            data=self._form_data(audience="totally new audience"),
        )
        db.session.refresh(ws)
        assert "totally new audience" in ws.notes
        # Old notes are replaced, not appended.
        assert "Old free-text" not in ws.notes

    def test_blank_fields_persist_as_not_specified(self, make_user):
        """The form's default-text behaviour — empty fields render as
        'Not specified' in the saved blob, not as empty strings.
        Downstream parsers rely on this consistency."""
        u = make_user(plan="pro", email="bc-blank@x.com")
        ws = _workspace(u)
        _logged_in(u).post(
            f"/client/{ws.slug}/brand-context",
            data={"audience": "", "services": "", "differentiators": ""},
        )
        db.session.refresh(ws)
        assert "Not specified" in ws.notes

    def test_save_other_users_workspace_404s(self, make_user):
        owner = make_user(plan="pro", email="bc-save-owner@x.com")
        intruder = make_user(plan="pro", email="bc-save-intruder@x.com")
        ws = _workspace(owner, slug="bc-save-owner-ws")
        original = ws.notes

        r = _logged_in(intruder).post(
            f"/client/{ws.slug}/brand-context",
            data=self._form_data(audience="injected"),
            follow_redirects=False,
        )
        assert r.status_code == 404
        # Owner's notes unchanged.
        db.session.refresh(ws)
        assert ws.notes == original


# ---------------------------------------------------------------------------
# POST /client/<id>/brand-context/suggest
# ---------------------------------------------------------------------------

class TestBrandContextSuggest:
    """AI-drafted field suggestions. Returns JSON the form's JS
    consumes to fill empty textareas. We mock OpenAI rather than
    hit the real API."""

    def _fake_openai_response(self, payload_dict):
        """Build a stand-in for the OpenAI ChatCompletion response shape."""
        import json as _json

        class _Msg:
            content = _json.dumps(payload_dict)

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()

    def test_returns_whitelisted_keys_only(self, make_user):
        """Model hallucinations or rogue keys must not pollute the
        form — the route filters to the 8 allowed keys."""
        u = make_user(plan="pro", email="bc-sug-ok@x.com")
        ws = _workspace(u)

        with patch("openai.OpenAI") as MockOpenAI:
            instance = MockOpenAI.return_value
            instance.chat.completions.create.return_value = (
                self._fake_openai_response({
                    "audience": "Small B2B teams",
                    "services": "AI audits and content briefs",
                    "differentiators": "Multi-engine answer monitor",
                    "proof": "200+ users",
                    "tone": "Direct, friendly",
                    "locations": "Singapore, US",
                    "avoid": "Hype",
                    "extra_notes": "B2B",
                    # Rogue extra key — must be filtered out.
                    "secret_field": "must not leak",
                    "credentials": "definitely not",
                })
            )
            r = _logged_in(u).post(
                f"/client/{ws.slug}/brand-context/suggest",
            )

        assert r.status_code == 200
        body = r.get_json()
        # All 8 expected keys present.
        for k in ("audience", "services", "differentiators", "proof",
                  "tone", "locations", "avoid", "extra_notes"):
            assert k in body
        # Rogue keys filtered out.
        assert "secret_field" not in body
        assert "credentials" not in body

    def test_openai_exception_returns_502(self, make_user):
        u = make_user(plan="pro", email="bc-sug-fail@x.com")
        ws = _workspace(u)
        with patch("openai.OpenAI") as MockOpenAI:
            MockOpenAI.return_value.chat.completions.create.side_effect = (
                RuntimeError("simulated OpenAI outage")
            )
            r = _logged_in(u).post(
                f"/client/{ws.slug}/brand-context/suggest",
            )
        assert r.status_code == 502
        body = r.get_json()
        assert "error" in body

    def test_unknown_workspace_returns_404(self, make_user):
        u = make_user(plan="pro", email="bc-sug-404@x.com")
        r = _logged_in(u).post("/client/does-not-exist/brand-context/suggest")
        assert r.status_code == 404

    def test_other_users_workspace_404s(self, make_user):
        owner = make_user(plan="pro", email="bc-sug-owner@x.com")
        intruder = make_user(plan="pro", email="bc-sug-intruder@x.com")
        ws = _workspace(owner, slug="bc-sug-owner-ws")
        r = _logged_in(intruder).post(
            f"/client/{ws.slug}/brand-context/suggest",
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /client/<id>/refresh-profile
# ---------------------------------------------------------------------------

class TestRefreshProfile:

    def test_owner_refresh_updates_columns(self, make_user):
        u = make_user(plan="pro", email="rp-ok@x.com")
        ws = _workspace(u)

        with patch(
            "services.business_profile_research.research_business_profile"
        ) as mock_research:
            mock_research.return_value = {
                "founded_year": 2020,
                "google_rating": 4.7,
                "google_review_count": 128,
                "executive_summary": "Acme builds AEO tools.",
                "core_services": ["audits", "content briefs"],
            }
            r = _logged_in(u).post(
                f"/client/{ws.id}/refresh-profile",
                follow_redirects=False,
            )
        assert r.status_code == 302
        db.session.refresh(ws)
        assert ws.founded_year == 2020
        assert ws.google_review_count == 128
        assert ws.business_summary == "Acme builds AEO tools."
        # business_profile_updated_at stamped.
        assert ws.business_profile_updated_at is not None

    def test_tavily_failure_does_not_crash(self, make_user):
        """research_business_profile raising must NOT 500 — flash an
        error and redirect back to the workspace."""
        u = make_user(plan="pro", email="rp-fail@x.com")
        ws = _workspace(u)
        original_summary = ws.business_summary
        with patch(
            "services.business_profile_research.research_business_profile",
            side_effect=RuntimeError("simulated Tavily outage"),
        ):
            r = _logged_in(u).post(
                f"/client/{ws.id}/refresh-profile",
                follow_redirects=False,
            )
        assert r.status_code == 302
        db.session.refresh(ws)
        # Profile unchanged.
        assert ws.business_summary == original_summary

    def test_unknown_workspace_returns_404(self, make_user):
        u = make_user(plan="pro", email="rp-404@x.com")
        r = _logged_in(u).post(
            "/client/999999/refresh-profile",
            follow_redirects=False,
        )
        assert r.status_code == 404

    def test_other_users_workspace_404s(self, make_user):
        owner = make_user(plan="pro", email="rp-owner@x.com")
        intruder = make_user(plan="pro", email="rp-intruder@x.com")
        ws = _workspace(owner, slug="rp-owner-ws")
        r = _logged_in(intruder).post(
            f"/client/{ws.id}/refresh-profile",
            follow_redirects=False,
        )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Anonymous access
# ---------------------------------------------------------------------------

class TestAnonymousAccess:

    def test_brand_context_redirects_to_login(self, app_ctx, make_user):
        u = make_user(plan="pro", email="anon-bc@x.com")
        ws = _workspace(u)
        c = flask_app.test_client()
        r = c.get(f"/client/{ws.slug}/brand-context", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_suggest_redirects_to_login(self, app_ctx, make_user):
        u = make_user(plan="pro", email="anon-sug@x.com")
        ws = _workspace(u)
        c = flask_app.test_client()
        r = c.post(
            f"/client/{ws.slug}/brand-context/suggest", follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")

    def test_refresh_profile_redirects_to_login(self, app_ctx, make_user):
        u = make_user(plan="pro", email="anon-rp@x.com")
        ws = _workspace(u)
        c = flask_app.test_client()
        r = c.post(
            f"/client/{ws.id}/refresh-profile", follow_redirects=False,
        )
        assert r.status_code == 302
        assert "/login" in (r.headers.get("Location") or "")
