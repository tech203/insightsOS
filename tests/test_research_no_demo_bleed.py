"""Regression: research/competitor queries + opportunity titles must
NOT be hardcoded to the original ice-cream demo client.

Found during an agency end-to-end eval: a fresh "Stripe" (payments
SaaS) workspace's Growth Calendar showed "Best Ice Cream Delivery
in Singapore". Root cause was two layers:

  1. research_engine.search_competitors / search_comparison_queries
     ignored the workspace's industry/services and ran hardcoded
     ice-cream/Singapore Tavily queries for EVERY client.
  2. action_engine.clean_opportunity_title then REWROTE any title
     containing "ice cream" / "party package" / "food gifts" into
     a fixed "… in Singapore" string.

These tests pin the fix so the demo scaffolding can't creep back.
"""
import pytest

import research_engine
from action_engine import clean_opportunity_title


# ---------------------------------------------------------------------------
# research_engine — queries derive from the workspace's own category
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_queries(monkeypatch):
    """Capture every tavily_search call instead of hitting the API."""
    calls = []
    monkeypatch.setattr(
        research_engine, "tavily_search",
        lambda query, category=None, max_results=5: calls.append((category, query)) or [],
    )
    return calls


_DEMO_TOKENS = ("ice cream", "dessert", "candy", "white rabbit", "party package")


def _assert_no_demo_bleed(calls):
    for cat, q in calls:
        low = q.lower()
        for tok in _DEMO_TOKENS:
            assert tok not in low, (
                f"Demo-data bleed: {cat} query {q!r} contains {tok!r}. "
                f"research_engine is hardcoding the ice-cream demo "
                f"client instead of using the workspace's industry."
            )


def test_search_competitors_uses_client_industry(captured_queries):
    research_engine.search_competitors(
        business_name="Stripe",
        industry="Payments / Fintech SaaS",
        location="Global",
        services="payment processing API",
    )
    assert captured_queries, "search_competitors made no queries"
    _assert_no_demo_bleed(captured_queries)
    # At least one query should reference the actual service.
    assert any("payment processing api" in q.lower()
               for _, q in captured_queries)


def test_search_comparison_queries_uses_client_industry(captured_queries):
    research_engine.search_comparison_queries(
        industry="Project management software",
        location=None,
        services="project management software",
    )
    assert captured_queries
    _assert_no_demo_bleed(captured_queries)
    assert any("project management software" in q.lower()
               for _, q in captured_queries)


def test_search_ai_discovery_prompts_uses_client_industry(captured_queries):
    """PR #204 fixed search_competitors / search_comparison_queries but
    missed search_ai_discovery_prompts — it still ran hardcoded
    'White Rabbit ice cream' discovery prompts for every client."""
    research_engine.search_ai_discovery_prompts(
        industry="Payments / Fintech SaaS",
        location="Global",
        services="payment processing API",
    )
    assert captured_queries, "search_ai_discovery_prompts made no queries"
    _assert_no_demo_bleed(captured_queries)
    assert any("payment processing api" in q.lower()
               for _, q in captured_queries)


def test_search_competitors_falls_back_to_brand_when_no_industry(captured_queries):
    """No industry/services → brand-relative discovery, NOT a
    hardcoded unrelated vertical."""
    research_engine.search_competitors(
        business_name="Acme Corp", industry="", location=None, services=None,
    )
    _assert_no_demo_bleed(captured_queries)
    assert all("acme corp" in q.lower() for _, q in captured_queries), (
        f"Expected brand-relative queries, got {captured_queries}"
    )


def test_research_empty_inputs_return_empty_no_api_calls(captured_queries):
    """Nothing to go on → return [] and make ZERO Tavily calls
    rather than fabricating off-topic competitor/listicle results."""
    r1 = research_engine.search_competitors(
        business_name="", industry="", location=None, services=None,
    )
    r2 = research_engine.search_comparison_queries(
        industry="", location=None, services=None,
    )
    r3 = research_engine.search_ai_discovery_prompts(
        industry="", location=None, services=None,
    )
    assert r1 == [] and r2 == [] and r3 == []
    assert captured_queries == [], (
        f"Expected no API calls on empty input, got {captured_queries}"
    )


# ---------------------------------------------------------------------------
# clean_opportunity_title — generic cleaning, no ice-cream rewrites
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Best CRMs for SMBs | TechRadar", "Best CRMs for SMBs"),
    ("Top payment APIs - G2", "Top payment APIs"),
    ("Stripe vs Adyen comparison — NerdWallet", "Stripe vs Adyen comparison"),
    ("10 Best Project Tools Updated May 2026", "10 Best Project Tools"),
    ("Best Marketing Automation (2026)", "Best Marketing Automation"),
])
def test_clean_opportunity_title_generic(raw, expected):
    assert clean_opportunity_title(raw) == expected


@pytest.mark.parametrize("raw", [
    "Best Food Gifts for Enterprise Clients",
    "How to plan a launch party package for B2B",
    "ice cream delivery logistics software",   # a real SaaS topic!
])
def test_clean_opportunity_title_does_not_rewrite_to_singapore(raw):
    """The old code rewrote anything with 'food gifts' / 'party
    package' / 'ice cream' into a fixed Singapore string. A payments
    or logistics client must keep its own title."""
    out = clean_opportunity_title(raw)
    assert "Singapore" not in out, (
        f"Title {raw!r} was rewritten to {out!r} — the hardcoded "
        f"ice-cream-demo remap is back."
    )
    assert out == raw  # no separators/dates here → unchanged


def test_clean_opportunity_title_empty_falls_back():
    assert clean_opportunity_title("", fallback_query="Pricing page audit") == "Pricing page audit"
    assert clean_opportunity_title("") == "Untitled content opportunity"


# ---------------------------------------------------------------------------
# search_ai_discovery_prompts — brand-intent fallback + module-wide guard
# ---------------------------------------------------------------------------
# #204 fixed search_competitors + search_comparison_queries; #214 then
# fixed search_ai_discovery_prompts' core query list. This block adds the
# brand-intent fallback coverage and a belt-and-braces static scan of the
# whole module so a demo string can't creep back via a docstring/example.

_DEMO_TOKENS_AI = _DEMO_TOKENS + ("halal", "nostalgic", "milk candy")


def test_ai_discovery_prompts_use_client_industry(captured_queries):
    research_engine.search_ai_discovery_prompts(
        business_name="Stripe",
        industry="Payments / Fintech SaaS",
        location="Global",
        services="payment processing API",
    )
    assert captured_queries, "search_ai_discovery_prompts made no queries"
    for cat, q in captured_queries:
        low = q.lower()
        for tok in _DEMO_TOKENS_AI:
            assert tok not in low, (
                f"Demo-data bleed: {cat} query {q!r} contains {tok!r}. "
                f"search_ai_discovery_prompts still hardcodes the "
                f"ice-cream demo client (PR #204 missed this function)."
            )
    assert any("payment processing api" in q.lower()
               for _, q in captured_queries)


def test_ai_discovery_prompts_brand_fallback_no_industry(captured_queries):
    """No industry/services → brand-intent prompts, not a hardcoded
    unrelated vertical."""
    research_engine.search_ai_discovery_prompts(
        business_name="Acme Corp", industry="", location=None, services=None,
    )
    for _, q in captured_queries:
        for tok in _DEMO_TOKENS_AI:
            assert tok not in q.lower(), f"demo bleed in {q!r}"
    assert all("acme corp" in q.lower() for _, q in captured_queries), (
        f"Expected brand-relative prompts, got {captured_queries}"
    )


def test_ai_discovery_prompts_empty_inputs_no_api_calls(captured_queries):
    """Nothing to go on → [] with ZERO Tavily calls (don't fabricate
    off-topic discovery prompts)."""
    r = research_engine.search_ai_discovery_prompts(
        business_name="", industry="", location=None, services=None,
    )
    assert r == []
    assert captured_queries == [], (
        f"Expected no API calls on empty input, got {captured_queries}"
    )


def test_research_engine_has_no_hardcoded_demo_strings():
    """Belt-and-braces: the whole module must not reintroduce the
    demo client's vocabulary in query-building code OR docstring/URL
    examples. Guards against a future copy-paste from the old demo
    branch."""
    import inspect
    src = inspect.getsource(research_engine)
    for tok in ("ice cream", "White Rabbit", "whiterabbit",
                "halal ice cream", "milk candy", "nostalgic candy"):
        assert tok not in src, (
            f"research_engine.py source still contains {tok!r} — "
            f"a demo-client string crept back in."
        )
