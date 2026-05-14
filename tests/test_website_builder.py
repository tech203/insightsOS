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
