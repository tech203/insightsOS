"""Rendering-quality regression tests.

Smoke tests catch 500s. These tests catch the 200-but-broken case:
templates rendering literal "None" / "undefined" / "null" in user-
visible text, unrendered Jinja expressions leaking through (e.g. a
typo in a variable name results in `{{ misspelled_var }}` showing
in the page), Python exception class names ending up in the body,
or stale TODO / FIXME markers shipping to production.

Strategy: walk every workspace page (and key settings pages) with
a real Client + a planted audit JSON, strip inline JS/CSS, and
grep the user-visible HTML for known broken-rendering signatures.

This test pairs with the post-audit walkthrough that found PR #110
(/export-pdf 500 with no audit) — the walkthrough catches blocking
500s; this catches the silent "page renders but says None" bugs.
"""
import json
import os
import re
import pytest
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash

import app as app_module
from app import app as flask_app, db, User, Wallet, CreditTransaction, Client


# Patterns that indicate a USER-VISIBLE rendering bug.
# Each entry is (regex, human description). Regexes are anchored to
# *between tags* (>...<) for the "literal value as display text" cases
# so we don't false-positive on inline JS / data attributes.
RED_FLAGS = [
    (r">\s*None\s*<", "literal None as display text"),
    (r">\s*undefined\s*<", "literal undefined as display text"),
    (r"\{\{[^}]+\}\}", "unrendered Jinja expression in body"),
    (r"\{%[^%]+%\}", "unrendered Jinja statement in body"),
    (r"TypeError:|AttributeError:|KeyError:|ValueError:",
     "Python exception class in body"),
    (r">\s*NaN\s*<", "NaN as display text"),
    (r">\s*null\s*<", "literal null as display text"),
    (r"\bTODO\b|\bFIXME\b|\bXXX\b", "TODO/FIXME marker"),
]


# Pages worth scanning. Limited to workspace + settings — those are the
# templates with the most variable substitution and therefore the most
# likely to develop "None" leaks. Static marketing pages don't need it.
PAGES_WITH_AUDIT = [
    "/dashboard",
    "/clients",
    "/client/{slug}",
    "/client/{slug}/visibility",
    "/client/{slug}/competitors",
    "/client/{slug}/history",
    "/client/{slug}/actions",
    "/client/{slug}/growth-plan",
    "/client/{slug}/query-ideas",
    "/client/{slug}/content-brief",
    "/client/{slug}/content-draft",
    "/client/{slug}/presentation",
    "/settings",
    "/settings/credits",
    "/settings/team",
    "/settings/white-label",
    "/settings/billing",
]


def _strip_inline_js_css(html: str) -> str:
    """Remove <script>, <style>, and JSON in data-* attributes so we
    only check user-visible text. Without this, any inline JS like
    `var x = null;` or `data-foo='{...,"key":null}'` false-positives
    on the literal-null check."""
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    html = re.sub(r'data-[\w-]+="[^"]*"', "", html)
    return html


@pytest.fixture
def workspace_with_audit(app_ctx, tmp_path, monkeypatch):
    """Logged-in user + a real Client workspace + a planted audit JSON.
    Mirrors the state right after a user's first audit completes —
    the moment we're rendering the most data and most likely to leak
    a None into a template."""
    # Pin the audit outputs dir to a temp dir so the test doesn't
    # see (or pollute) real audit files.
    monkeypatch.setattr(app_module, "OUTPUTS_FOLDER", str(tmp_path))

    u = User(
        email="render@test.com",
        password_hash=generate_password_hash("xx"),
        name="Render Test", plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    u.wallet = Wallet(user_id=u.id, balance=100)
    db.session.add(u.wallet)
    db.session.add(CreditTransaction(
        user_id=u.id, type="monthly_allowance", amount=0,
        balance_after=100, notes="Render fixture",
    ))

    ws = Client(
        slug="renderco", user_id=u.id, name="Render Co",
        website="https://render.example.com",
        website_normalized="render.example.com",
        industry="SaaS", location="Remote",
    )
    db.session.add(ws)
    db.session.commit()

    # Planted audit payload — the shape build_client_views() expects.
    audit = {
        "user_id": u.id, "client_id": "renderco",
        "client_name": "Render Co",
        "website": "https://render.example.com",
        "audit_type": "full", "saved_at": "2026-05-14T12:00:00Z",
        "scores": {
            "normalized_score": 58, "visibility_score": 9.4,
            "content_score": 12, "schema_score": 6,
        },
        "summary": {"verdict": "MODERATE",
                    "opportunity_level": "High Opportunity"},
        "recommended_actions": [
            {"title": "Fix service page coverage",
             "recommended_action": "Add comparison content",
             "priority": "high"},
        ],
        "top_competitors": [{"domain": "comp1.com", "appearances": 5}],
        "top_content_gaps": [{"query": "best chat for SMB", "count": 3}],
        "visibility_snapshot": {"mentions": 7, "missed": 13},
    }
    audit_filename = "renderco_full_20260514_120000_aBcDeFgH_summary.json"
    with open(os.path.join(str(tmp_path), audit_filename), "w") as f:
        json.dump(audit, f)

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
    return c, ws.slug, audit_filename


@pytest.mark.parametrize("path_tmpl", PAGES_WITH_AUDIT)
def test_page_has_no_user_visible_rendering_red_flags(
    workspace_with_audit, path_tmpl,
):
    """User-visible HTML must not contain any of the broken-rendering
    signatures (literal None/undefined/null/NaN in display text,
    unrendered Jinja expressions, Python exception class names, or
    TODO markers)."""
    c, slug, _ = workspace_with_audit
    path = path_tmpl.format(slug=slug)
    resp = c.get(path, follow_redirects=False)
    assert resp.status_code < 400, (
        f"{path} returned {resp.status_code} — fix this before checking"
        f" rendering quality"
    )
    body = _strip_inline_js_css(resp.data.decode(errors="replace"))
    failures = []
    for pattern, desc in RED_FLAGS:
        for m in re.finditer(pattern, body, re.IGNORECASE):
            ctx_start = max(0, m.start() - 60)
            ctx_end = min(len(body), m.end() + 60)
            ctx = re.sub(r"\s+", " ", body[ctx_start:ctx_end])
            failures.append(f"  [{desc}] match={m.group(0)!r}\n    ctx: ...{ctx}...")
    assert not failures, (
        f"\n{path} has {len(failures)} rendering red flags:\n"
        + "\n".join(failures[:5])  # cap to first 5 for readable output
    )
