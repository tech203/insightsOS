"""Regression tests for the website builder.

The brand-kit preview template renders inline CSS variables from the
blueprint dict (e.g. `--brand-primary: {{ blueprint.primary_color }}`).
If `build_demo_website_blueprint()` returns a dict missing those keys,
the CSS resolves to empty strings and the preview goes visually blank
without any 500 to surface the regression.

These tests pin the contract: the blueprint must carry brand-kit
fields (colors, personality, visual style) for every industry path.
"""
from unittest.mock import patch

from app import build_demo_website_blueprint, build_generated_site_page
from website_page_builder import _page_spec_for_content_type


# Each (industry, services) pair targets one branch in
# build_demo_website_blueprint so we catch a regression that breaks
# any single industry path.
INDUSTRY_FIXTURES = [
    ("Dental clinic", "Invisalign, whitening"),
    ("Tuition centre", "PSLE math, English enrichment"),
    ("Ice cream shop", "ice cream, dessert, confectionery"),
    ("Online store", "merchandise, products"),
    ("Marketing agency", "AEO, SEO"),  # default branch
]


REQUIRED_BRAND_KIT_FIELDS = [
    "primary_color",
    "secondary_color",
    "accent_color",
    "text_color",
    "personality",
    "visual_style",
    "tone_of_voice",
]


def _hex_color(value):
    return (
        isinstance(value, str)
        and len(value) == 7
        and value.startswith("#")
        and all(c in "0123456789abcdefABCDEF" for c in value[1:])
    )


def test_blueprint_includes_brand_kit_fields_for_every_industry():
    """Every industry branch must return populated brand-kit fields,
    otherwise the brand-kit preview renders with empty CSS variables."""
    for industry, services in INDUSTRY_FIXTURES:
        client = {
            "name": "Test Co",
            "industry": industry,
            "services": services,
            "location": "Singapore",
        }
        blueprint = build_demo_website_blueprint(client)

        for field in REQUIRED_BRAND_KIT_FIELDS:
            assert field in blueprint, (
                f"{industry!r}: blueprint missing {field!r}"
            )
            assert blueprint[field], (
                f"{industry!r}: blueprint[{field!r}] is empty/None"
            )

        for color_field in (
            "primary_color",
            "secondary_color",
            "accent_color",
            "text_color",
        ):
            assert _hex_color(blueprint[color_field]), (
                f"{industry!r}: {color_field}={blueprint[color_field]!r}"
                f" is not a valid hex color"
            )

        assert isinstance(blueprint["personality"], list)
        assert blueprint["personality"], (
            f"{industry!r}: personality list is empty"
        )


def test_blueprint_aeo_focus_prefers_queue_opportunities():
    """When real opportunities exist, AEO focus uses their
    target_query — not the generic "best X in Y" template."""
    client = {
        "name": "Test Co",
        "industry": "Marketing agency",
        "services": "AEO",
        "location": "Singapore",
    }
    opportunities = [
        {"target_query": "how to rank in ChatGPT answers"},
        {"target_query": "AEO vs SEO for B2B"},
        {"target_query": ""},  # empty — must be skipped
        {"target_query": "how to rank in ChatGPT answers"},  # dup — must be skipped
        {"target_query": "best AEO agency Singapore"},
        {"target_query": "Perplexity citation tracking"},
        {"target_query": "fifth item should be dropped"},
    ]

    blueprint = build_demo_website_blueprint(client, opportunities=opportunities)

    assert blueprint["aeo_focus"] == [
        "how to rank in ChatGPT answers",
        "AEO vs SEO for B2B",
        "best AEO agency Singapore",
        "Perplexity citation tracking",
    ]


def test_blueprint_aeo_focus_falls_back_when_no_opportunities():
    """No opportunities → generic templates still populate the list
    so a brand-new client still gets a starting brand kit."""
    client = {
        "name": "Test Co",
        "industry": "Marketing agency",
        "services": "AEO",
        "location": "Singapore",
    }
    blueprint = build_demo_website_blueprint(client, opportunities=[])
    assert len(blueprint["aeo_focus"]) == 4
    assert all(item for item in blueprint["aeo_focus"])


def test_blueprint_pages_and_aeo_focus_are_industry_appropriate():
    """Product-led themes use Shop/Products copy; service themes don't."""
    food_client = {
        "name": "Sweet Co",
        "industry": "Ice cream shop",
        "services": "ice cream, dessert",
        "location": "Singapore",
    }
    blueprint = build_demo_website_blueprint(food_client)

    assert blueprint["primary_cta"] == "Shop Now"
    page_titles = [p["title"] for p in blueprint["pages"]]
    assert "Products" in page_titles
    assert "Services" not in page_titles

    services_client = {
        "name": "Advisory Co",
        "industry": "Marketing agency",
        "services": "consulting",
        "location": "Singapore",
    }
    blueprint = build_demo_website_blueprint(services_client)

    assert blueprint["primary_cta"] == "Enquire Now"
    page_titles = [p["title"] for p in blueprint["pages"]]
    assert "Services" in page_titles
    assert "Products" not in page_titles


# ---------------------------------------------------------------------------
# AI-first / rule-based fallback path
# ---------------------------------------------------------------------------

_BLUEPRINT_FIXTURE = build_demo_website_blueprint({
    "name": "Test Co",
    "industry": "Marketing agency",
    "services": "AEO consulting",
    "location": "Singapore",
})

_PAGE_CONFIG = {
    "title": "Home",
    "slug": "home",
    "page_type": "home",
    "goal": "Explain the business clearly.",
}

_CLIENT = {
    "name": "Test Co",
    "industry": "Marketing agency",
    "location": "Singapore",
}


def test_ai_page_generation_used_when_successful():
    """Valid AI output passes through enrich_generated_page_json and
    becomes the page_json — proving the rule-based path is bypassed."""
    fake_ai_output = {
        "page_type": "home",
        "title": "AI-generated title",
        "slug": "home",
        "seo": {
            "meta_description": "AI desc",
            "keywords": ["Test Co"],
        },
        "sections": [
            {"type": "hero", "headline": "AI-generated hero"}
        ],
    }

    with patch(
        "app.generate_structured_website_page",
        return_value=fake_ai_output,
    ):
        page_json = build_generated_site_page(
            _CLIENT, _BLUEPRINT_FIXTURE, _PAGE_CONFIG
        )

    assert page_json["sections"][0]["headline"] == "AI-generated hero"
    # Enrichment must still run on the AI output.
    assert "semantic_profile" in page_json
    assert "schema_json" in page_json


def test_rule_based_fallback_when_ai_raises():
    """Any exception from the AI path → fall through to the
    deterministic rule-based generator. The page must still render
    fully (hero + sections + enrichment)."""
    with patch(
        "app.generate_structured_website_page",
        side_effect=RuntimeError("openai down"),
    ):
        page_json = build_generated_site_page(
            _CLIENT, _BLUEPRINT_FIXTURE, _PAGE_CONFIG
        )

    assert page_json["sections"], "rule-based generator must emit sections"
    assert page_json["sections"][0]["type"] == "hero"
    assert "semantic_profile" in page_json


def test_rule_based_fallback_when_ai_returns_invalid():
    """Empty or malformed AI output is treated as a failure — we trust
    the rule-based generator's complete output instead."""
    with patch(
        "app.generate_structured_website_page",
        return_value={"sections": []},
    ):
        page_json = build_generated_site_page(
            _CLIENT, _BLUEPRINT_FIXTURE, _PAGE_CONFIG
        )

    assert page_json["sections"], "must fall back when AI returns empty"
    assert page_json["sections"][0]["type"] == "hero"


# ---------------------------------------------------------------------------
# Per-page-type prompt shapes
# ---------------------------------------------------------------------------

def test_page_spec_returns_correct_slug_per_content_type():
    """Regression: the old prompt hardcoded slug="home" for every
    page, so all 5 generated pages had the same slug. Each content
    type must now yield its own slug."""
    for content_type, expected_slug in [
        ("home", "home"),
        ("landing_page", "home"),
        ("contact", "contact"),
        ("about", "about"),
        ("faq", "faq"),
        ("services", "services"),
    ]:
        spec = _page_spec_for_content_type(
            content_type, "Test Co", "SaaS", "Get Started", "Learn More"
        )
        assert spec["slug"] == expected_slug, content_type


def test_page_spec_section_shape_matches_page_purpose():
    """Contact pages must not generate a "services" or "value_prop"
    block; FAQ pages must lead with FAQ items; about pages must lead
    with story content."""
    contact = _page_spec_for_content_type(
        "contact", "Test Co", "SaaS", "Call Us", "Email"
    )
    assert "services" not in contact["sections_schema"]
    assert "value_prop" not in contact["sections_schema"]
    assert "contact_details" in contact["sections_schema"]

    about = _page_spec_for_content_type(
        "about", "Test Co", "SaaS", "Get Started", "Learn More"
    )
    assert '"type": "story"' in about["sections_schema"]

    faq = _page_spec_for_content_type(
        "faq", "Test Co", "SaaS", "Get Started", "Learn More"
    )
    # FAQ page should have at least 5 q/a placeholders to push the
    # model toward answer-engine-friendly density.
    assert faq["sections_schema"].count('"question"') >= 5


def test_page_spec_unknown_content_type_falls_back_to_home():
    """Unknown page types shouldn't break — fall back to homepage
    shape so the generator always returns a renderable page."""
    spec = _page_spec_for_content_type(
        "mystery_page_type", "Test Co", "SaaS", "Buy", "Browse"
    )
    assert spec["slug"] == "home"
    assert spec["page_label"] == "homepage"


# ---------------------------------------------------------------------------
# Section renderer — guard against silent "section type unknown → not
# rendered" regressions. The AI prompt emits cta_block / contact_details
# / story; the previous template only knew hero / services / value_prop /
# proof / faq / cta, so the final CTA on every AI-generated page was
# silently dropped. This test ensures each new section type renders.
# ---------------------------------------------------------------------------

def _render_engine_template(sections):
    """Render the website_engine_render.html partial with a
    full-featured page_json so we can grep the resulting HTML for
    section markers."""
    from flask import render_template
    from app import app as flask_app

    page_json = {
        "sections": sections,
        "semantic_profile": {
            "entity_name": "Test Co",
            "entity_type": "Marketing agency",
        },
    }
    # The partial includes via the parent template; render it directly
    # using a tiny wrapper template string.
    with flask_app.app_context():
        with flask_app.test_request_context():
            return render_template(
                "website_engine_render.html",
                page_json=page_json,
                page=type("P", (), {"title": "Test"})(),
            )


def test_renderer_handles_cta_block_section():
    """The AI prompt emits "cta_block" — the renderer must treat it
    identically to "cta" so the final CTA actually appears."""
    html = _render_engine_template([
        {
            "type": "cta_block",
            "headline": "Ready to dive in?",
            "subtext": "Take the next step.",
            "primary_cta": "Get Started",
        }
    ])
    assert "Ready to dive in?" in html
    assert "Get Started" in html


def test_renderer_handles_contact_details_section():
    """Contact pages emit a contact_details section — must render
    a card grid of items[]."""
    html = _render_engine_template([
        {
            "type": "contact_details",
            "headline": "How to reach us",
            "items": [
                {"title": "Phone", "description": "+65 1234 5678"},
                {"title": "Email", "description": "hi@test.co"},
            ],
        }
    ])
    assert "How to reach us" in html
    assert "Phone" in html
    assert "+65 1234 5678" in html
    assert "hi@test.co" in html


def test_renderer_handles_story_section():
    """About pages emit a story section — narrative prose, no grid."""
    html = _render_engine_template([
        {
            "type": "story",
            "headline": "The origin of Test Co",
            "body": "Founded in 2020 to solve X.",
        }
    ])
    assert "The origin of Test Co" in html
    assert "Founded in 2020 to solve X." in html


def test_renderer_hero_badge_is_theme_specific():
    """The hero badge used to hardcode Ice/Shop/AI strings, ignoring
    clinics and education. Confirm the new theme-keyed badge map
    picks an industry-appropriate icon."""
    page_json = {
        "sections": [{"type": "hero", "headline": "h"}],
        "semantic_profile": {
            "entity_name": "Test",
            "entity_type": "Dental clinic",
        },
    }
    from flask import render_template
    from app import app as flask_app
    with flask_app.app_context():
        with flask_app.test_request_context():
            html = render_template(
                "website_engine_render.html",
                page_json=page_json,
                page=type("P", (), {"title": "Test"})(),
            )
    # Clinic theme should yield the stethoscope, not the legacy "AI"
    # fallback string.
    assert "🩺" in html
    assert ">AI<" not in html


# ---------------------------------------------------------------------------
# Brand-kit preview — colour palette must render with non-empty inline
# styles. Regression for the Jinja {% set %} scope bug: declaring colour
# variables inside {% block head %} did NOT leak into {% block content %},
# so every direct {{ primary_color }} reference in the palette evaluated
# to empty string. CSS variables masked the bug for elements styled via
# class (avatar, mock buttons), but the palette swatches and <input
# type="color"> values went blank.
# ---------------------------------------------------------------------------

def test_brand_kit_preview_color_palette_renders_with_real_values(app_ctx, monkeypatch):
    """The colour samples in the brand-kit preview must have non-empty
    inline `style="background: #...;"` values, and the colour picker
    inputs must carry real hex values (not the default #000000)."""
    from werkzeug.security import generate_password_hash
    from app import db, User, Wallet, Client, app as flask_app
    from datetime import datetime, timezone

    u = User(
        email="kit@test.com",
        password_hash=generate_password_hash("xx"),
        name="Kit Test",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    u.wallet = Wallet(user_id=u.id, balance=100)
    db.session.add(u.wallet)
    ws = Client(
        slug="kitco",
        user_id=u.id,
        name="Kit Co",
        website="https://kit.example.com",
        website_normalized="kit.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True

    # Kick off the website-builder generate flow to populate the
    # pending blueprint in session, then load the brand-kit preview.
    resp = c.post(f"/client/{ws.slug}/website-builder/generate")
    assert resp.status_code in (302, 303), resp.status_code

    resp = c.get(f"/client/{ws.slug}/website-builder/brand-kit")
    assert resp.status_code == 200
    body = resp.data.decode()

    # The palette inline style must carry a real hex colour. The bug
    # left this as `style="background: ;"` for every swatch.
    assert 'style="background: ;"' not in body, (
        "Color palette swatch is rendering with empty inline style — "
        "{% set %} block-scope leak from head into content again"
    )
    assert 'background: #4f46e5;' in body or 'background: #f97316;' in body

    # The colour picker inputs must carry real hex values, not blank.
    assert 'value=""' not in body or "type=\"color\" value=\"\"" not in body


def test_website_engine_preview_sets_css_variables():
    """The in-dashboard page preview must define --site-primary /
    --site-text / etc. on a wrapper. Without these, the rendered card
    titles fall back to the dashboard's dark-theme inherited colour
    and become invisible against the white card backgrounds — the
    issue caught via screenshot review."""
    from flask import render_template
    from app import app as flask_app

    page_json = {
        "sections": [
            {
                "type": "contact_details",
                "headline": "How to reach us",
                "items": [
                    {"title": "Phone", "description": "+65 9123 4567"},
                ],
            }
        ],
        "semantic_profile": {"entity_name": "Test", "entity_type": "Agency"},
    }
    page = type(
        "P",
        (),
        {
            "id": 1,
            "title": "Contact",
            "page_json": page_json,
        },
    )()
    blueprint = {
        "primary_color": "#4f46e5",
        "secondary_color": "#eef2ff",
        "accent_color": "#c7d2fe",
        "text_color": "#0f172a",
    }
    with flask_app.app_context():
        with flask_app.test_request_context():
            html = render_template(
                "website_engine_preview.html",
                page=page,
                page_json=page_json,
                blueprint=blueprint,
            )

    assert "--site-primary: #4f46e5" in html
    assert "--site-text: #0f172a" in html
    # The wrapper class is what scopes the CSS variables to the
    # preview block.
    assert "generated-site-preview" in html
    # Regression: --site-primary-dark used to read the same blueprint
    # field as --site-primary, leaving them identical and flattening
    # the hero gradient + hover states. It now derives via color-mix.
    assert "color-mix(in srgb, #4f46e5, black" in html


def test_website_engine_preview_still_renders_without_blueprint():
    """Defensive: an old project with a missing/null blueprint_json
    must still render without 500 — the inline-style fallbacks must
    fire."""
    from flask import render_template
    from app import app as flask_app

    page = type(
        "P",
        (),
        {
            "id": 1,
            "title": "Contact",
            "page_json": {"sections": [{"type": "hero", "headline": "h"}]},
        },
    )()

    with flask_app.app_context():
        with flask_app.test_request_context():
            html = render_template(
                "website_engine_preview.html",
                page=page,
                page_json=page.page_json,
                blueprint=None,
            )

    # Fallbacks should still produce real hex values, not empty.
    assert "--site-primary: #f97316" in html
    assert "--site-text: #0f172a" in html


# ---------------------------------------------------------------------------
# Visual style class — the four brand-kit radio options must surface
# as a real CSS class on the rendered output so generated-site.css can
# apply per-style typography / radius / density rules. Before this,
# the radio buttons were pure decoration.
# ---------------------------------------------------------------------------

def test_preview_wrapper_carries_visual_style_class():
    """Each of the four visual_style values must appear as a
    style-<value> class on .generated-site-preview, so the CSS
    rules in generated-site.css scoped under .style-* actually
    match."""
    from flask import render_template
    from app import app as flask_app

    page = type(
        "P",
        (),
        {
            "id": 1,
            "title": "Home",
            "page_json": {"sections": [{"type": "hero", "headline": "h"}]},
        },
    )()

    for style_value in [
        "modern_ecommerce",
        "premium_minimal",
        "editorial_lifestyle",
        "playful_brand",
    ]:
        blueprint = {"visual_style": style_value}
        with flask_app.app_context():
            with flask_app.test_request_context():
                html = render_template(
                    "website_engine_preview.html",
                    page=page,
                    page_json=page.page_json,
                    blueprint=blueprint,
                )
        assert f"style-{style_value}" in html, (
            f"visual_style={style_value!r} did not produce style-* class"
        )


def test_public_site_nav_label_handles_user_added_pages():
    """User-added pages get page_type="landing_page" (the default in
    apply_brand_kit_form_edits), so the nav previously labelled them
    all as "Landing Page" regardless of what the user named them. The
    fix derives the nav label from the slug instead — handles default
    pages (home/about/faq/contact) and user-added ones (pricing/how-
    it-works) consistently."""
    from flask import render_template
    from app import app as flask_app

    # GeneratedWebsiteProject + GeneratedWebsitePage are model rows;
    # build lightweight stand-ins for template rendering only.
    project = type(
        "Proj",
        (),
        {
            "id": 1,
            "theme": "professional_services",
            "status": "draft",
            "blueprint_json": {"client_name": "Test"},
        },
    )()

    def _page(slug, page_type):
        return type(
            "Pg",
            (),
            {
                "id": 1,
                "title": "x",
                "slug": slug,
                "page_type": page_type,
                "status": "draft",
                "page_json": {"sections": [], "seo": {}},
            },
        )()

    pages = [
        _page("home", "home"),
        _page("about", "about"),
        _page("faq", "faq"),
        _page("pricing", "landing_page"),  # user-added page
        _page("how-it-works", "landing_page"),  # user-added, hyphenated
    ]
    page_for_render = pages[0]

    with flask_app.app_context():
        with flask_app.test_request_context():
            html = render_template(
                "generated_full_site.html",
                project=project,
                pages=pages,
                page=page_for_render,
                page_json=page_for_render.page_json,
                blueprint=project.blueprint_json,
            )

    # Isolate the nav so an assertion on "Home" doesn't false-match
    # the brand text elsewhere on the page.
    import re
    nav_match = re.search(r"<nav.*?</nav>", html, re.DOTALL)
    assert nav_match, "expected a nav element"
    nav = nav_match.group(0)

    # User-added pages now show their slug-derived label, not the
    # generic "Landing Page".
    assert "Pricing" in nav
    assert "How It Works" in nav
    assert "Landing Page" not in nav
    # FAQ gets the uppercase special-case.
    assert "FAQ" in nav
    # Defaults still work.
    assert "Home" in nav
    assert "About" in nav


def test_preview_wrapper_carries_theme_class():
    """The wrapper div must carry a theme-<value> class derived from
    blueprint.theme so the industry-theme CSS accents (clinic /
    education / ecommerce / food / professional services) actually
    apply alongside the visual_style class."""
    from flask import render_template
    from app import app as flask_app

    page = type(
        "P",
        (),
        {
            "id": 1,
            "title": "Home",
            "page_json": {"sections": [{"type": "hero", "headline": "h"}]},
        },
    )()

    for theme_value in [
        "clinic_wellness",
        "education_centre",
        "restaurant_cafe",
        "ecommerce_store",
        "professional_services",
    ]:
        blueprint = {
            "visual_style": "modern_ecommerce",
            "theme": theme_value,
        }
        with flask_app.app_context():
            with flask_app.test_request_context():
                html = render_template(
                    "website_engine_preview.html",
                    page=page,
                    page_json=page.page_json,
                    blueprint=blueprint,
                )
        assert f"theme-{theme_value}" in html, theme_value


def test_preview_wrapper_falls_back_to_modern_ecommerce_when_visual_style_missing():
    """An old project without visual_style on its blueprint must still
    render — the default class is style-modern_ecommerce (the same
    look as before the per-style work)."""
    from flask import render_template
    from app import app as flask_app

    page = type(
        "P",
        (),
        {
            "id": 1,
            "title": "Home",
            "page_json": {"sections": [{"type": "hero", "headline": "h"}]},
        },
    )()
    with flask_app.app_context():
        with flask_app.test_request_context():
            html = render_template(
                "website_engine_preview.html",
                page=page,
                page_json=page.page_json,
                blueprint={},  # no visual_style key
            )
    assert "style-modern_ecommerce" in html


# ---------------------------------------------------------------------------
# Skip-page flow — the brand-kit form's per-page "Skip this page"
# checkbox must filter that page out of the blueprint at generation
# time. Without this, users were locked to all 5 default pages even
# if their business didn't need an About or FAQ.
# ---------------------------------------------------------------------------

def _sample_blueprint():
    return build_demo_website_blueprint({
        "name": "Test Co",
        "industry": "Marketing agency",
        "services": "AEO",
        "location": "Singapore",
    })


def test_apply_brand_kit_form_edits_drops_skipped_pages():
    """When page_remove_<i> is truthy, that page must not appear in
    the resulting blueprint pages list."""
    from app import apply_brand_kit_form_edits
    from werkzeug.datastructures import ImmutableMultiDict

    blueprint = _sample_blueprint()
    assert len(blueprint["pages"]) == 5  # sanity

    # Skip About (index 2) and FAQ (index 3).
    form = ImmutableMultiDict(
        [
            ("page_remove_2", "1"),
            ("page_remove_3", "1"),
        ]
    )
    edited = apply_brand_kit_form_edits(blueprint, form)

    surviving_slugs = [p["slug"] for p in edited["pages"]]
    assert "about" not in surviving_slugs
    assert "faq" not in surviving_slugs
    assert "home" in surviving_slugs
    assert "services" in surviving_slugs
    assert "contact" in surviving_slugs
    assert len(edited["pages"]) == 3


def test_apply_brand_kit_form_edits_keeps_renames_when_skip_not_set():
    """Without skip, renames/slug edits should still apply normally —
    the skip flow must not break the existing edit logic."""
    from app import apply_brand_kit_form_edits
    from werkzeug.datastructures import ImmutableMultiDict

    blueprint = _sample_blueprint()
    form = ImmutableMultiDict(
        [
            ("page_title_0", "Welcome"),
            ("page_slug_0", "welcome"),
        ]
    )
    edited = apply_brand_kit_form_edits(blueprint, form)
    assert edited["pages"][0]["title"] == "Welcome"
    assert edited["pages"][0]["slug"] == "welcome"
    assert len(edited["pages"]) == 5


def test_apply_brand_kit_form_edits_can_drop_all_pages():
    """Edge case: all 5 skipped → pages becomes empty. The approve
    route then guards against this and warns the user — but the
    helper itself must produce a valid (empty) list, not raise."""
    from app import apply_brand_kit_form_edits
    from werkzeug.datastructures import ImmutableMultiDict

    blueprint = _sample_blueprint()
    form = ImmutableMultiDict(
        [(f"page_remove_{i}", "1") for i in range(5)]
    )
    edited = apply_brand_kit_form_edits(blueprint, form)
    assert edited["pages"] == []


def test_apply_brand_kit_form_edits_appends_new_pages():
    """new_page_title_<i> slots, when filled, append additional pages
    to the blueprint. Empty slots are skipped. Slug auto-generates
    from title if not provided."""
    from app import apply_brand_kit_form_edits
    from werkzeug.datastructures import ImmutableMultiDict

    blueprint = _sample_blueprint()
    form = ImmutableMultiDict(
        [
            ("new_page_title_0", "Pricing"),
            ("new_page_goal_0", "Explain pricing tiers"),
            # slot 1 left blank — must NOT be appended
            ("new_page_title_2", "Process"),
            ("new_page_slug_2", "how-it-works"),  # custom slug
        ]
    )
    edited = apply_brand_kit_form_edits(blueprint, form)
    titles = [p["title"] for p in edited["pages"]]
    assert "Pricing" in titles
    assert "Process" in titles
    assert len(edited["pages"]) == 5 + 2  # original 5 + 2 added

    pricing = next(p for p in edited["pages"] if p["title"] == "Pricing")
    assert pricing["slug"] == "pricing"  # auto-slugified from title
    assert pricing["goal"] == "Explain pricing tiers"
    assert pricing["page_type"] == "landing_page"

    process = next(p for p in edited["pages"] if p["title"] == "Process")
    assert process["slug"] == "how-it-works"  # custom slug honoured


def test_palette_variant_cycles_and_clamps():
    """get_palette_variant returns a (primary, secondary, accent)
    triple for any non-negative index, wrapping around when the
    index exceeds the number of variants for that theme."""
    from brand_kit_engine import get_palette_variant, PALETTE_VARIANTS

    for theme in ["food_and_beverage", "clinic", "education", "general"]:
        variants = PALETTE_VARIANTS[theme]
        # Sequential indices return sequentially different palettes.
        seen = {get_palette_variant(theme, i) for i in range(len(variants))}
        assert len(seen) == len(variants), (
            f"{theme} should have {len(variants)} distinct palettes"
        )
        # Wrap: index N == index 0 for N == len(variants).
        assert get_palette_variant(theme, len(variants)) == get_palette_variant(theme, 0)
        # Each triple is hex.
        for variant in variants:
            for hex_color in variant:
                assert hex_color.startswith("#")
                assert len(hex_color) == 7


def test_blueprint_includes_workspace_logo_url():
    """A workspace with a logo on its dashboard should not require a
    re-upload in the brand kit — its logo_url propagates into the
    blueprint at build time."""
    client = {
        "name": "Test Co",
        "industry": "Marketing agency",
        "services": "AEO",
        "location": "Singapore",
        "logo_url": "/static/uploads/workspace_logos/workspace-1-abc.png",
    }
    blueprint = build_demo_website_blueprint(client)
    assert blueprint["logo_url"] == "/static/uploads/workspace_logos/workspace-1-abc.png"


def test_classifier_drives_all_three_call_sites_consistently():
    """The same industry text must classify to the same canonical
    bucket no matter which entry point is used. Previously the three
    classifiers had subtly different keyword lists — e.g. "wellness"
    was a clinic word in build_demo_website_blueprint but not in
    generate_brand_kit, so a "wellness centre" got different theme
    decisions depending on which function was called. The shared
    classifier eliminates that drift."""
    from brand_kit_engine import classify_industry_theme, generate_brand_kit
    from website_page_builder import build_business_context
    from app import build_demo_website_blueprint

    cases = [
        # (industry, expected_canonical_bucket)
        ("Dental clinic", "clinic"),
        ("Wellness centre", "clinic"),  # was clinic-only in app.py before
        ("Tuition centre", "education"),
        ("PSLE math enrichment", "education"),  # "psle" + "math" + "enrichment"
        ("Ice cream shop", "food_and_beverage"),  # "ice cream" wins over "shop"
        ("Online merchandise store", "ecommerce"),
        ("Marketing agency", "general"),
    ]

    for industry, expected in cases:
        # Direct classifier.
        assert classify_industry_theme(industry) == expected, industry

        # Brand kit's industry_theme matches (clinic / food_and_beverage
        # / education / ecommerce / general — same canonical names).
        kit = generate_brand_kit(business_name="X", industry=industry)
        assert kit["industry_theme"] == expected, (industry, kit)

        # Business context maps clinic → "clinic" too, but uses
        # "general_business" for the general bucket. Match expected
        # → context business_type via a small map.
        context = build_business_context(business_name="X", industry=industry)
        expected_context_type = "general_business" if expected == "general" else expected
        assert context["business_type"] == expected_context_type, industry

        # Demo blueprint uses its own theme names (clinic_wellness /
        # restaurant_cafe etc.) — same bucket, mapped consistently.
        blueprint = build_demo_website_blueprint({
            "name": "X", "industry": industry, "location": "Singapore",
        })
        bucket_to_theme = {
            "clinic": "clinic_wellness",
            "food_and_beverage": "restaurant_cafe",
            "education": "education_centre",
            "ecommerce": "ecommerce_store",
            "general": "professional_services",
        }
        assert blueprint["theme"] == bucket_to_theme[expected], industry


def test_regenerate_page_replaces_page_json_and_preserves_identity(app_ctx):
    """POSTing to /website-engine/page/<id>/regenerate must:
    (a) replace the page's page_json with freshly-built content,
    (b) preserve the existing slug + page_type so URLs don't shift,
    (c) preserve any webflow export state attached to the old json,
    (d) redirect back to the page preview."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone
    from unittest.mock import patch

    u = User(
        email="regen@test.com",
        password_hash=generate_password_hash("xx"),
        name="Regen Test",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    u.wallet = Wallet(user_id=u.id, balance=10)
    db.session.add(u.wallet)
    ws = Client(
        slug="regenco",
        user_id=u.id,
        name="Regen Co",
        website="https://regen.example.com",
        website_normalized="regen.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()

    project = GeneratedWebsiteProject(
        user_id=u.id,
        client_id=ws.id,
        title="Regen Co Website",
        theme="professional_services",
        status="draft",
        blueprint_json={
            "client_name": "Regen Co",
            "business_type": "Marketing agency",
            "location": "Singapore",
            "industry_theme": "general",
            "primary_color": "#4f46e5",
        },
    )
    db.session.add(project)
    db.session.flush()

    page = GeneratedWebsitePage(
        project_id=project.id,
        user_id=u.id,
        client_id=ws.id,
        title="Home",
        slug="home",
        page_type="home",
        status="published",
        page_json={
            "page_type": "home",
            "title": "Old Title",
            "slug": "home",
            "sections": [{"type": "hero", "headline": "OLD CONTENT"}],
            "webflow": {"item_id": "wf123"},
        },
    )
    db.session.add(page)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True

    # Force AI to fail so the rule-based fallback fires — keeps the
    # test deterministic regardless of OPENAI_API_KEY state.
    with patch(
        "app.generate_structured_website_page",
        side_effect=RuntimeError("openai disabled in test"),
    ):
        resp = c.post(f"/website-engine/page/{page.id}/regenerate")

    assert resp.status_code in (302, 303)
    assert f"/website-engine/page/{page.id}/preview" in resp.headers["Location"]

    db.session.refresh(page)
    # Identity preserved.
    assert page.slug == "home"
    assert page.page_type == "home"
    # page_json was replaced with fresh content (no longer "OLD CONTENT").
    assert page.page_json["sections"]
    headlines = [s.get("headline") for s in page.page_json["sections"]]
    assert "OLD CONTENT" not in headlines
    # Webflow export state preserved on the new json.
    assert page.page_json.get("webflow", {}).get("item_id") == "wf123"


def test_delete_project_cascades_pages_and_redirects(app_ctx):
    """POSTing to /website-builder/project/<id>/delete must (a) delete
    every page row for the project, (b) delete the project row, and
    (c) redirect back to the workspace's website-builder landing."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    u = User(
        email="delproj@test.com",
        password_hash=generate_password_hash("xx"),
        name="Del Proj",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=10))
    ws = Client(
        slug="delproj-co",
        user_id=u.id,
        name="DelProj Co",
        website="https://dp.example.com",
        website_normalized="dp.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=u.id,
        client_id=ws.id,
        title="DelProj Co Website",
        theme="professional_services",
        status="draft",
        blueprint_json={"client_name": "DelProj Co"},
    )
    db.session.add(project)
    db.session.flush()
    for slug in ("home", "about", "contact"):
        db.session.add(
            GeneratedWebsitePage(
                project_id=project.id,
                user_id=u.id,
                client_id=ws.id,
                title=slug.title(),
                slug=slug,
                page_type=slug,
                status="draft",
                page_json={"sections": []},
            )
        )
    db.session.commit()
    project_id = project.id
    assert GeneratedWebsitePage.query.filter_by(project_id=project_id).count() == 3

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True

    resp = c.post(f"/website-builder/project/{project_id}/delete")
    assert resp.status_code in (302, 303)
    assert f"/client/{ws.slug}/website-builder" in resp.headers["Location"]

    assert GeneratedWebsiteProject.query.get(project_id) is None
    assert GeneratedWebsitePage.query.filter_by(project_id=project_id).count() == 0


def test_edit_page_updates_section_fields_in_place(app_ctx):
    """POSTing to /page/<id>/edit must update hero headline / subtext
    / CTAs, page title, SEO description, and FAQ item q/a pairs in
    place — keeping section structure (types, order, items count)
    unchanged. Editable fields are whitelisted per section type."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    u = User(
        email="edit@test.com",
        password_hash=generate_password_hash("xx"),
        name="Edit",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=10))
    ws = Client(
        slug="edit-co",
        user_id=u.id,
        name="Edit Co",
        website="https://e.example.com",
        website_normalized="e.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=u.id,
        client_id=ws.id,
        title="Edit Co Site",
        theme="professional_services",
        status="draft",
        blueprint_json={"client_name": "Edit Co"},
    )
    db.session.add(project)
    db.session.flush()
    page = GeneratedWebsitePage(
        project_id=project.id,
        user_id=u.id,
        client_id=ws.id,
        title="Old Title",
        slug="home",
        page_type="home",
        status="draft",
        page_json={
            "title": "Old Title",
            "slug": "home",
            "page_type": "home",
            "seo": {"meta_description": "old desc"},
            "sections": [
                {
                    "type": "hero",
                    "eyebrow": "Old eyebrow",
                    "headline": "Old headline",
                    "subtext": "Old subtext",
                    "primary_cta": "Old CTA",
                },
                {
                    "type": "faq",
                    "headline": "FAQ",
                    "items": [
                        {"question": "Old q1", "answer": "Old a1"},
                        {"question": "Old q2", "answer": "Old a2"},
                    ],
                },
            ],
        },
    )
    db.session.add(page)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True

    # GET must render without 500.
    resp = c.get(f"/website-engine/page/{page.id}/edit")
    assert resp.status_code == 200
    assert b"Old headline" in resp.data

    # POST with edits.
    resp = c.post(
        f"/website-engine/page/{page.id}/edit",
        data={
            "page_title": "New Title",
            "seo_description": "New desc",
            "section_0_headline": "New headline",
            "section_0_subtext": "New subtext",
            "section_0_primary_cta": "Get started",
            "section_1_item_0_question": "New q1",
            "section_1_item_1_answer": "New a2",
        },
    )
    assert resp.status_code in (302, 303)
    assert f"/website-engine/page/{page.id}/preview" in resp.headers["Location"]

    db.session.refresh(page)
    assert page.title == "New Title"
    assert page.page_json["title"] == "New Title"
    assert page.page_json["seo"]["meta_description"] == "New desc"
    hero = page.page_json["sections"][0]
    assert hero["headline"] == "New headline"
    assert hero["subtext"] == "New subtext"
    assert hero["primary_cta"] == "Get started"
    # Untouched fields preserved.
    assert hero["eyebrow"] == "Old eyebrow"
    assert hero["type"] == "hero"
    faq = page.page_json["sections"][1]
    assert faq["items"][0]["question"] == "New q1"
    assert faq["items"][0]["answer"] == "Old a1"  # untouched
    assert faq["items"][1]["answer"] == "New a2"
    assert faq["items"][1]["question"] == "Old q2"  # untouched


def test_mark_reviewed_route_stamps_reviewed_at(app_ctx):
    """POSTing to /page/<id>/mark-reviewed sets reviewed_at on the
    page_json so the project preview can show a "Reviewed" badge."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    u = User(
        email="review@test.com",
        password_hash=generate_password_hash("xx"),
        name="Reviewer",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=10))
    ws = Client(
        slug="review-co",
        user_id=u.id,
        name="Review Co",
        website="https://r.example.com",
        website_normalized="r.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=u.id,
        client_id=ws.id,
        title="Review Co Site",
        theme="professional_services",
        status="draft",
        blueprint_json={},
    )
    db.session.add(project)
    db.session.flush()
    page = GeneratedWebsitePage(
        project_id=project.id,
        user_id=u.id,
        client_id=ws.id,
        title="Home",
        slug="home",
        page_type="home",
        status="draft",
        page_json={"sections": []},
    )
    db.session.add(page)
    db.session.commit()

    assert "reviewed_at" not in (page.page_json or {})

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True

    resp = c.post(f"/website-engine/page/{page.id}/mark-reviewed")
    assert resp.status_code in (302, 303)
    assert f"/website-engine/page/{page.id}/preview" in resp.headers["Location"]

    db.session.refresh(page)
    reviewed_at = page.page_json.get("reviewed_at")
    assert reviewed_at
    # ISO-ish format with the Z suffix.
    assert reviewed_at.endswith("Z")
    assert "T" in reviewed_at


def test_mark_all_reviewed_stamps_only_unreviewed_pages(app_ctx):
    """POSTing to /project/<id>/mark-all-reviewed stamps reviewed_at
    on every page that doesn't already have it, leaving previously-
    reviewed pages' timestamps intact (so re-clicking doesn't bump
    everything to 'now')."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    u = User(
        email="bulkrev@test.com",
        password_hash=generate_password_hash("xx"),
        name="Bulk",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=10))
    ws = Client(
        slug="bulkrev-co",
        user_id=u.id,
        name="Bulk Co",
        website="https://b.example.com",
        website_normalized="b.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=u.id,
        client_id=ws.id,
        title="Bulk Co Site",
        theme="professional_services",
        status="draft",
        blueprint_json={},
    )
    db.session.add(project)
    db.session.flush()

    # 3 pages: one already reviewed with a known timestamp, two not.
    OLD_TS = "2020-01-01T00:00:00Z"
    already_reviewed = GeneratedWebsitePage(
        project_id=project.id,
        user_id=u.id,
        client_id=ws.id,
        title="Home",
        slug="home",
        page_type="home",
        status="draft",
        page_json={"sections": [], "reviewed_at": OLD_TS},
    )
    new1 = GeneratedWebsitePage(
        project_id=project.id,
        user_id=u.id,
        client_id=ws.id,
        title="About",
        slug="about",
        page_type="about",
        status="draft",
        page_json={"sections": []},
    )
    new2 = GeneratedWebsitePage(
        project_id=project.id,
        user_id=u.id,
        client_id=ws.id,
        title="Contact",
        slug="contact",
        page_type="contact",
        status="draft",
        page_json={"sections": []},
    )
    db.session.add_all([already_reviewed, new1, new2])
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True

    resp = c.post(
        f"/website-builder/project/{project.id}/mark-all-reviewed"
    )
    assert resp.status_code in (302, 303)
    assert f"/website-builder/project/{project.id}/preview" in resp.headers["Location"]

    db.session.refresh(already_reviewed)
    db.session.refresh(new1)
    db.session.refresh(new2)

    # Already-reviewed page kept its original timestamp (idempotent).
    assert already_reviewed.page_json["reviewed_at"] == OLD_TS
    # Previously-unreviewed pages now have fresh timestamps.
    assert new1.page_json.get("reviewed_at")
    assert new1.page_json["reviewed_at"].endswith("Z")
    assert new2.page_json.get("reviewed_at")


def test_edit_route_persists_eyebrow_on_non_hero_sections(app_ctx):
    """eyebrow is now editable on services/value_prop/proof/faq/
    contact_details/story (used to be hero-only). Posting an eyebrow
    for a services section must persist it on page_json."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    u = User(
        email="eb@test.com",
        password_hash=generate_password_hash("xx"),
        name="EB",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=10))
    ws = Client(
        slug="eb-co",
        user_id=u.id,
        name="EB Co",
        website="https://eb.example.com",
        website_normalized="eb.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=u.id,
        client_id=ws.id,
        title="EB Site",
        theme="professional_services",
        status="draft",
        blueprint_json={},
    )
    db.session.add(project)
    db.session.flush()
    page = GeneratedWebsitePage(
        project_id=project.id,
        user_id=u.id,
        client_id=ws.id,
        title="Home",
        slug="home",
        page_type="home",
        status="draft",
        page_json={
            "sections": [
                {"type": "services", "headline": "What we do", "items": []},
                {"type": "faq", "headline": "FAQ", "items": []},
            ],
        },
    )
    db.session.add(page)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True

    resp = c.post(
        f"/website-engine/page/{page.id}/edit",
        data={
            "section_0_eyebrow": "Our offerings",
            "section_1_eyebrow": "Customer questions",
        },
    )
    assert resp.status_code in (302, 303)
    db.session.refresh(page)
    assert page.page_json["sections"][0]["eyebrow"] == "Our offerings"
    assert page.page_json["sections"][1]["eyebrow"] == "Customer questions"


def test_regenerate_section_replaces_only_targeted_section(app_ctx):
    """POSTing to /page/<id>/section/<i>/regenerate must (a) replace
    the section at index i, (b) leave the other sections alone (so
    the user's hand edits elsewhere survive), (c) only match by type
    so we don't accidentally swap a hero for a contact_details."""
    from werkzeug.security import generate_password_hash
    from unittest.mock import patch
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    u = User(
        email="secregen@test.com",
        password_hash=generate_password_hash("xx"),
        name="SR",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=10))
    ws = Client(
        slug="secregen-co",
        user_id=u.id,
        name="SR Co",
        website="https://sr.example.com",
        website_normalized="sr.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=u.id,
        client_id=ws.id,
        title="SR Site",
        theme="professional_services",
        status="draft",
        blueprint_json={"client_name": "SR Co", "industry_theme": "general"},
    )
    db.session.add(project)
    db.session.flush()
    page = GeneratedWebsitePage(
        project_id=project.id,
        user_id=u.id,
        client_id=ws.id,
        title="Home",
        slug="home",
        page_type="home",
        status="draft",
        page_json={
            "sections": [
                {"type": "hero", "headline": "CUSTOM HERO — user edited", "subtext": "Custom subtext"},
                {"type": "services", "headline": "STALE services", "items": []},
                {"type": "faq", "headline": "FAQ", "items": []},
            ],
        },
    )
    db.session.add(page)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True

    # Force AI to fail → rule-based fallback fires deterministically.
    with patch(
        "app.generate_structured_website_page",
        side_effect=RuntimeError("openai disabled in test"),
    ):
        resp = c.post(
            f"/website-engine/page/{page.id}/section/1/regenerate"
        )
    assert resp.status_code in (302, 303)
    # Redirect goes back to the edit view so the user sees the
    # refreshed section in context.
    assert f"/website-engine/page/{page.id}/edit" in resp.headers["Location"]

    db.session.refresh(page)
    sections = page.page_json["sections"]
    # Hero and FAQ untouched — user's custom hero copy survives.
    assert sections[0]["headline"] == "CUSTOM HERO — user edited"
    assert sections[0]["subtext"] == "Custom subtext"
    assert sections[2]["type"] == "faq"
    # Services section was replaced — new headline came from the
    # rule-based generator, not "STALE services".
    assert sections[1]["type"] == "services"
    assert sections[1]["headline"] != "STALE services"


def test_regenerate_section_rejects_other_users(app_ctx):
    """Cross-user defence on the per-section regen route."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    owner = User(
        email="o-sr@test.com",
        password_hash=generate_password_hash("xx"),
        name="o",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    intruder = User(
        email="i-sr@test.com",
        password_hash=generate_password_hash("xx"),
        name="i",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add_all([owner, intruder])
    db.session.flush()
    db.session.add(Wallet(user_id=owner.id, balance=10))
    db.session.add(Wallet(user_id=intruder.id, balance=10))
    ws = Client(
        slug="o-sr-co",
        user_id=owner.id,
        name="o",
        website="https://o.example.com",
        website_normalized="o.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=owner.id,
        client_id=ws.id,
        title="o",
        theme="professional_services",
        status="draft",
        blueprint_json={},
    )
    db.session.add(project)
    db.session.flush()
    page = GeneratedWebsitePage(
        project_id=project.id,
        user_id=owner.id,
        client_id=ws.id,
        title="Home",
        slug="home",
        page_type="home",
        status="draft",
        page_json={"sections": [{"type": "hero", "headline": "h"}]},
    )
    db.session.add(page)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(intruder.id)
        s["_fresh"] = True
    assert c.post(
        f"/website-engine/page/{page.id}/section/0/regenerate"
    ).status_code == 403


def test_renderer_shows_both_ctas_on_cta_block():
    """cta_block sections from the AI generator carry primary_cta +
    secondary_cta. The edit form lets users set both, but the
    renderer used to only emit the primary — secondary was silently
    discarded. Confirm both render now."""
    from flask import render_template
    from app import app as flask_app

    page_json = {
        "sections": [
            {
                "type": "cta_block",
                "headline": "Ready?",
                "primary_cta": "Get Started",
                "secondary_cta": "Learn More",
            }
        ],
        "semantic_profile": {"entity_name": "x", "entity_type": "agency"},
    }
    page = type("P", (), {"id": 1, "title": "x", "page_json": page_json})()

    with flask_app.app_context():
        with flask_app.test_request_context():
            html = render_template(
                "website_engine_render.html",
                page=page,
                page_json=page_json,
            )

    assert "Get Started" in html
    assert "Learn More" in html


def test_renderer_keeps_single_cta_render_when_only_primary_set():
    """Backwards compat: old-style `cta` sections from the rule-based
    generator have only `button`. The new render path must still
    render that single button without breaking."""
    from flask import render_template
    from app import app as flask_app

    page_json = {
        "sections": [
            {"type": "cta", "headline": "Ready?", "button": "Enquire Now"}
        ],
        "semantic_profile": {"entity_name": "x", "entity_type": "agency"},
    }
    page = type("P", (), {"id": 1, "title": "x", "page_json": page_json})()
    with flask_app.app_context():
        with flask_app.test_request_context():
            html = render_template(
                "website_engine_render.html",
                page=page,
                page_json=page_json,
            )
    assert "Enquire Now" in html


def test_renderer_uses_section_eyebrow_when_set_falls_back_otherwise():
    """The renderer prefers section.eyebrow over the hardcoded kicker
    label so user-edited eyebrows actually appear on the rendered
    site. Empty/missing eyebrow keeps the fallback label."""
    from flask import render_template
    from app import app as flask_app

    page_json = {
        "sections": [
            {"type": "services", "headline": "h", "eyebrow": "Custom offerings", "items": []},
            {"type": "faq", "headline": "h", "items": []},  # no eyebrow
        ],
        "semantic_profile": {"entity_name": "x", "entity_type": "agency"},
    }
    page = type("P", (), {"id": 1, "title": "x", "page_json": page_json})()

    with flask_app.app_context():
        with flask_app.test_request_context():
            html = render_template(
                "website_engine_render.html",
                page=page,
                page_json=page_json,
            )

    # Services section uses the user-edited eyebrow.
    assert "Custom offerings" in html
    assert "Offerings" not in html or html.count("Custom offerings") >= html.count("Offerings")
    # FAQ section without eyebrow falls back to the literal "FAQ".
    assert "FAQ" in html


def test_mark_all_reviewed_rejects_other_users(app_ctx):
    """Cross-user defence on the bulk route."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        app as flask_app,
    )
    from datetime import datetime, timezone

    owner = User(
        email="o-bulk@test.com",
        password_hash=generate_password_hash("xx"),
        name="o",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    intruder = User(
        email="i-bulk@test.com",
        password_hash=generate_password_hash("xx"),
        name="i",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add_all([owner, intruder])
    db.session.flush()
    db.session.add(Wallet(user_id=owner.id, balance=10))
    db.session.add(Wallet(user_id=intruder.id, balance=10))
    ws = Client(
        slug="o-bulk-co",
        user_id=owner.id,
        name="o",
        website="https://o.example.com",
        website_normalized="o.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=owner.id,
        client_id=ws.id,
        title="o",
        theme="professional_services",
        status="draft",
        blueprint_json={},
    )
    db.session.add(project)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(intruder.id)
        s["_fresh"] = True
    assert c.post(
        f"/website-builder/project/{project.id}/mark-all-reviewed"
    ).status_code == 403


def test_mark_reviewed_rejects_other_users(app_ctx):
    """Cross-user defence on the mark-reviewed route."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    owner = User(
        email="o-mr@test.com",
        password_hash=generate_password_hash("xx"),
        name="o",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    intruder = User(
        email="i-mr@test.com",
        password_hash=generate_password_hash("xx"),
        name="i",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add_all([owner, intruder])
    db.session.flush()
    db.session.add(Wallet(user_id=owner.id, balance=10))
    db.session.add(Wallet(user_id=intruder.id, balance=10))
    ws = Client(
        slug="o-mr-co",
        user_id=owner.id,
        name="o",
        website="https://o.example.com",
        website_normalized="o.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=owner.id,
        client_id=ws.id,
        title="o",
        theme="professional_services",
        status="draft",
        blueprint_json={},
    )
    db.session.add(project)
    db.session.flush()
    page = GeneratedWebsitePage(
        project_id=project.id,
        user_id=owner.id,
        client_id=ws.id,
        title="Home",
        slug="home",
        page_type="home",
        status="draft",
        page_json={"sections": []},
    )
    db.session.add(page)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(intruder.id)
        s["_fresh"] = True
    assert c.post(f"/website-engine/page/{page.id}/mark-reviewed").status_code == 403


def test_edit_page_save_stamps_reviewed_at(app_ctx):
    """Editing a page implies reviewing it — the /edit POST must
    stamp reviewed_at in addition to applying field changes."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    u = User(
        email="editrev@test.com",
        password_hash=generate_password_hash("xx"),
        name="ER",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=10))
    ws = Client(
        slug="editrev-co",
        user_id=u.id,
        name="ER Co",
        website="https://er.example.com",
        website_normalized="er.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=u.id,
        client_id=ws.id,
        title="ER Site",
        theme="professional_services",
        status="draft",
        blueprint_json={},
    )
    db.session.add(project)
    db.session.flush()
    page = GeneratedWebsitePage(
        project_id=project.id,
        user_id=u.id,
        client_id=ws.id,
        title="Home",
        slug="home",
        page_type="home",
        status="draft",
        page_json={
            "title": "Home",
            "sections": [
                {"type": "hero", "headline": "Old", "subtext": "Old"},
            ],
        },
    )
    db.session.add(page)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True

    resp = c.post(
        f"/website-engine/page/{page.id}/edit",
        data={"section_0_headline": "New headline"},
    )
    assert resp.status_code in (302, 303)
    db.session.refresh(page)
    assert page.page_json["sections"][0]["headline"] == "New headline"
    assert page.page_json.get("reviewed_at")


def test_edit_page_rejects_other_users(app_ctx):
    """Cross-user defence — only the page owner can edit."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    owner = User(
        email="o-edit@test.com",
        password_hash=generate_password_hash("xx"),
        name="o",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    intruder = User(
        email="i-edit@test.com",
        password_hash=generate_password_hash("xx"),
        name="i",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add_all([owner, intruder])
    db.session.flush()
    db.session.add(Wallet(user_id=owner.id, balance=10))
    db.session.add(Wallet(user_id=intruder.id, balance=10))
    ws = Client(
        slug="o-edit-co",
        user_id=owner.id,
        name="o",
        website="https://o.example.com",
        website_normalized="o.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=owner.id,
        client_id=ws.id,
        title="Owner Site",
        theme="professional_services",
        status="draft",
        blueprint_json={},
    )
    db.session.add(project)
    db.session.flush()
    page = GeneratedWebsitePage(
        project_id=project.id,
        user_id=owner.id,
        client_id=ws.id,
        title="Home",
        slug="home",
        page_type="home",
        status="draft",
        page_json={"sections": []},
    )
    db.session.add(page)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(intruder.id)
        s["_fresh"] = True

    resp = c.get(f"/website-engine/page/{page.id}/edit")
    assert resp.status_code == 403
    resp = c.post(
        f"/website-engine/page/{page.id}/edit", data={"page_title": "Hacked"}
    )
    assert resp.status_code == 403


def test_delete_project_rejects_other_users(app_ctx):
    """Cross-user defence — a project's owner is the only one who can
    delete it. Anyone else gets 403, not silent success."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        app as flask_app,
    )
    from datetime import datetime, timezone

    owner = User(
        email="owner-del@test.com",
        password_hash=generate_password_hash("xx"),
        name="Owner Del",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    intruder = User(
        email="intruder-del@test.com",
        password_hash=generate_password_hash("xx"),
        name="Intruder Del",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add_all([owner, intruder])
    db.session.flush()
    db.session.add(Wallet(user_id=owner.id, balance=10))
    db.session.add(Wallet(user_id=intruder.id, balance=10))
    ws = Client(
        slug="owner-del-co",
        user_id=owner.id,
        name="Owner Del Co",
        website="https://od.example.com",
        website_normalized="od.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=owner.id,
        client_id=ws.id,
        title="Don't touch",
        theme="professional_services",
        status="draft",
        blueprint_json={"client_name": "Owner Del Co"},
    )
    db.session.add(project)
    db.session.commit()
    project_id = project.id

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(intruder.id)
        s["_fresh"] = True

    resp = c.post(f"/website-builder/project/{project_id}/delete")
    assert resp.status_code == 403
    # Project must still exist.
    assert GeneratedWebsiteProject.query.get(project_id) is not None


def test_regenerate_page_rejects_other_users_pages(app_ctx):
    """Cross-user defence: a user can't regenerate someone else's
    page. Must return 403, not silently regenerate."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    owner = User(
        email="owner@test.com",
        password_hash=generate_password_hash("xx"),
        name="Owner",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    intruder = User(
        email="intruder@test.com",
        password_hash=generate_password_hash("xx"),
        name="Intruder",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add_all([owner, intruder])
    db.session.flush()
    db.session.add(Wallet(user_id=owner.id, balance=10))
    db.session.add(Wallet(user_id=intruder.id, balance=10))

    ws = Client(
        slug="owner-co",
        user_id=owner.id,
        name="Owner Co",
        website="https://o.example.com",
        website_normalized="o.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.flush()
    project = GeneratedWebsiteProject(
        user_id=owner.id,
        client_id=ws.id,
        title="Owner site",
        theme="professional_services",
        status="draft",
        blueprint_json={"client_name": "Owner Co"},
    )
    db.session.add(project)
    db.session.flush()
    page = GeneratedWebsitePage(
        project_id=project.id,
        user_id=owner.id,
        client_id=ws.id,
        title="Home",
        slug="home",
        page_type="home",
        status="draft",
        page_json={"sections": []},
    )
    db.session.add(page)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(intruder.id)
        s["_fresh"] = True

    resp = c.post(f"/website-engine/page/{page.id}/regenerate")
    assert resp.status_code == 403


def test_blueprint_reads_persisted_workspace_brand_colors():
    """If a workspace already has brand_primary/secondary/accent on
    its Client row (from a prior approve or the standalone Brand
    Kit Studio), build_demo_website_blueprint must prefer them over
    the industry classifier's defaults — otherwise every regenerate
    silently blows away the user's customisations."""
    client = {
        "name": "Test Co",
        "industry": "Marketing agency",  # would default to indigo #4f46e5
        "services": "AEO",
        "location": "Singapore",
        "brand_kit": {
            "primary_color": "#dc2626",   # custom red
            "secondary_color": "#fef2f2",
            "accent_color": "#fecaca",
            "personality": "bold, distinctive, modern",
        },
    }
    blueprint = build_demo_website_blueprint(client)
    assert blueprint["primary_color"] == "#dc2626"
    assert blueprint["secondary_color"] == "#fef2f2"
    assert blueprint["accent_color"] == "#fecaca"
    # Personality string gets split on commas into a list.
    assert blueprint["personality"] == ["bold", "distinctive", "modern"]


def test_approve_persists_brand_kit_back_to_workspace(app_ctx):
    """When the user approves a brand kit, the colour + personality
    customisations must be written back to the Client row's brand_*
    columns. Next regenerate will read them — completes the loop
    so users don't lose their work between generations."""
    from werkzeug.security import generate_password_hash
    from unittest.mock import patch
    from app import db, User, Wallet, Client, app as flask_app
    from datetime import datetime, timezone

    u = User(
        email="brandsync@test.com",
        password_hash=generate_password_hash("xx"),
        name="Brand Sync",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=10))
    ws = Client(
        slug="brandsyncco",
        user_id=u.id,
        name="BrandSync Co",
        website="https://bs.example.com",
        website_normalized="bs.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.commit()

    # Fresh workspace — no brand colours persisted yet.
    assert ws.brand_primary_color is None
    assert ws.brand_personality is None

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
        # Plant a pending blueprint with custom colours, as if the
        # user has just gone through the form and is hitting approve.
        s["pending_website_blueprint"] = {
            "client_name": "BrandSync Co",
            "business_type": "Marketing agency",
            "location": "Singapore",
            "theme": "professional_services",
            "industry_theme": "general",
            "primary_color": "#dc2626",
            "secondary_color": "#fef2f2",
            "accent_color": "#fecaca",
            "personality": ["bold", "distinctive"],
            "pages": [
                {"title": "Home", "slug": "home", "page_type": "home", "goal": "intro"}
            ],
        }
        s["pending_website_client_id"] = ws.id
        s["pending_website_client_slug"] = ws.slug

    # Force AI to fail so the rule-based fallback fires — keeps test
    # deterministic.
    with patch(
        "app.generate_structured_website_page",
        side_effect=RuntimeError("openai disabled in test"),
    ):
        resp = c.post(f"/client/{ws.slug}/website-builder/approve-brand-kit")
    assert resp.status_code in (302, 303)

    db.session.refresh(ws)
    assert ws.brand_primary_color == "#dc2626"
    assert ws.brand_secondary_color == "#fef2f2"
    assert ws.brand_accent_color == "#fecaca"
    assert ws.brand_personality == "bold, distinctive"
    assert ws.brand_kit_approved_at is not None


def test_save_brand_kit_route_persists_without_generating_pages(app_ctx):
    """POSTing to /save-brand-kit must (a) write brand_* to the Client
    row, (b) stamp brand_kit_updated_at (NOT brand_kit_approved_at —
    that's reserved for full approve), (c) NOT create any project or
    page rows, (d) keep the pending blueprint in session so the user
    can keep editing."""
    from werkzeug.security import generate_password_hash
    from app import (
        db,
        User,
        Wallet,
        Client,
        GeneratedWebsiteProject,
        GeneratedWebsitePage,
        app as flask_app,
    )
    from datetime import datetime, timezone

    u = User(
        email="savekit@test.com",
        password_hash=generate_password_hash("xx"),
        name="Save Kit",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    db.session.add(Wallet(user_id=u.id, balance=10))
    ws = Client(
        slug="savekit-co",
        user_id=u.id,
        name="SaveKit Co",
        website="https://sk.example.com",
        website_normalized="sk.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.commit()

    assert ws.brand_primary_color is None
    assert ws.brand_kit_updated_at is None
    assert ws.brand_kit_approved_at is None

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
        s["pending_website_blueprint"] = {
            "client_name": "SaveKit Co",
            "business_type": "Marketing agency",
            "industry_theme": "general",
            "primary_color": "#10b981",
            "secondary_color": "#ecfdf5",
            "accent_color": "#a7f3d0",
            "personality": ["modern", "trustworthy"],
            "pages": [
                {"title": "Home", "slug": "home", "page_type": "home", "goal": ""}
            ],
            "theme": "professional_services",
        }

    resp = c.post(f"/client/{ws.slug}/website-builder/save-brand-kit")
    assert resp.status_code in (302, 303)
    # Redirected back to the brand-kit preview, not the project preview.
    assert "/website-builder/brand-kit" in resp.headers["Location"]

    db.session.refresh(ws)
    assert ws.brand_primary_color == "#10b981"
    assert ws.brand_secondary_color == "#ecfdf5"
    assert ws.brand_accent_color == "#a7f3d0"
    assert ws.brand_personality == "modern, trustworthy"
    assert ws.brand_kit_updated_at is not None
    # save-only intentionally does NOT stamp approved_at — that's the
    # full approve flow's signal.
    assert ws.brand_kit_approved_at is None

    # No project or page rows were created.
    assert GeneratedWebsiteProject.query.filter_by(user_id=u.id).count() == 0
    assert GeneratedWebsitePage.query.filter_by(user_id=u.id).count() == 0

    # Session blueprint still there for further editing.
    with c.session_transaction() as s:
        assert s.get("pending_website_blueprint") is not None


def test_blueprint_falls_back_to_industry_defaults_when_workspace_brand_unset():
    """A fresh workspace (no persisted brand) should still get
    sensible colours from the industry classifier."""
    client = {
        "name": "Test Co",
        "industry": "Marketing agency",
        "services": "AEO",
        "location": "Singapore",
        # no brand_kit key
    }
    blueprint = build_demo_website_blueprint(client)
    # General bucket default — indigo.
    assert blueprint["primary_color"] == "#4f46e5"


def test_palette_variant_unknown_theme_falls_back_to_general():
    """Unknown industry_theme values shouldn't crash — fall back to
    the general bucket so the Regenerate button always works."""
    from brand_kit_engine import get_palette_variant, PALETTE_VARIANTS

    assert get_palette_variant("nonexistent", 0) == PALETTE_VARIANTS["general"][0]


def test_regenerate_aeo_ideas_route_advances_focus_and_preserves_form(app_ctx):
    """End-to-end-ish: POSTing to the regenerate-aeo-ideas route
    must (a) replace blueprint.aeo_focus with a new slice of
    generate_query_ideas output, (b) bump aeo_variant, (c) preserve
    any in-flight form edits the user typed before clicking, and
    (d) redirect back to the brand-kit preview."""
    from werkzeug.security import generate_password_hash
    from app import db, User, Wallet, Client, app as flask_app
    from datetime import datetime, timezone

    u = User(
        email="aeo@test.com",
        password_hash=generate_password_hash("xx"),
        name="AEO Test",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    u.wallet = Wallet(user_id=u.id, balance=10)
    db.session.add(u.wallet)
    ws = Client(
        slug="aeoco",
        user_id=u.id,
        name="AEO Co",
        website="https://aeo.example.com",
        website_normalized="aeo.example.com",
        industry="Marketing agency",
        location="Singapore",
    )
    db.session.add(ws)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
        s["pending_website_blueprint"] = {
            "client_name": "AEO Co",
            "business_type": "Marketing agency",
            "location": "Singapore",
            "services": ["AEO consulting"],
            "industry_theme": "general",
            "aeo_focus": ["original 1", "original 2"],
            "aeo_variant": 0,
            "personality": ["clear"],
        }

    # User typed in the personality textarea before clicking
    # Regenerate Ideas — that edit must survive the redirect.
    resp = c.post(
        f"/client/{ws.slug}/website-builder/regenerate-aeo-ideas",
        data={"personality": "bold, modern, distinctive"},
    )
    assert resp.status_code in (302, 303)
    assert "brand-kit" in resp.headers["Location"]

    with c.session_transaction() as s:
        bp = s["pending_website_blueprint"]
    new_focus = bp.get("aeo_focus")
    assert new_focus != ["original 1", "original 2"]
    assert len(new_focus) >= 1
    assert bp.get("aeo_variant") == 1
    # Personality edit was preserved through the redirect.
    assert "bold" in (bp.get("personality") or [""])[0]


def test_apply_brand_kit_form_edits_skip_and_add_combined():
    """Skip + Add in the same submission: existing pages are filtered,
    new pages are appended, both happen in one form post."""
    from app import apply_brand_kit_form_edits
    from werkzeug.datastructures import ImmutableMultiDict

    blueprint = _sample_blueprint()
    form = ImmutableMultiDict(
        [
            ("page_remove_3", "1"),  # drop FAQ
            ("new_page_title_0", "Testimonials"),
        ]
    )
    edited = apply_brand_kit_form_edits(blueprint, form)
    slugs = [p["slug"] for p in edited["pages"]]
    assert "faq" not in slugs
    assert "testimonials" in slugs
    assert len(edited["pages"]) == 5 - 1 + 1  # 5 default - 1 skipped + 1 added
