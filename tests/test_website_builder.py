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


def test_palette_variant_unknown_theme_falls_back_to_general():
    """Unknown industry_theme values shouldn't crash — fall back to
    the general bucket so the Regenerate button always works."""
    from brand_kit_engine import get_palette_variant, PALETTE_VARIANTS

    assert get_palette_variant("nonexistent", 0) == PALETTE_VARIANTS["general"][0]


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
