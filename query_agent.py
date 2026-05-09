"""
Query generator for the audit pipeline.

Produces the set of natural-language shopper queries we test against
AI engines (ChatGPT / Perplexity / Gemini) to gauge a brand's
visibility. Real shoppers don't search "best ice cream and
confectionery e-commerce Singapore" — they search "where to buy
durian ice cream in Singapore" or "ice cream delivery Singapore late
night". So we lean on OpenAI to produce realistic queries from the
brand's industry + location, with a deterministic template fallback
that keeps the audit running when no API key is configured.

The downstream audit uses these queries to:
  1. Hit each AI engine with the query
  2. Detect whether the brand is mentioned in the answer
  3. Detect which competitors the engines cite instead

So the *quality* of these queries directly drives how convincing the
final audit report reads — generic queries → generic findings.
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional


logger = logging.getLogger(__name__)


def _llm_query_ideas(
    *,
    topic: str,
    location: Optional[str],
    count: int = 18,
    brand_context: Optional[str] = None,
) -> List[str]:
    """Ask OpenAI for `count` realistic shopper queries.

    `brand_context` is the homepage-scan summary (title, description,
    nav categories, hero copy) — when provided, the LLM is told to
    generate queries that match what the brand *actually* sells, not
    just what the typed industry suggests. This is what stops a
    nostalgia-candy brand from getting audited as if it were Ben &
    Jerry's.

    Returns [] on any failure so the caller can fall back to the
    templates.
    """
    if not os.getenv("OPENAI_API_KEY"):
        return []
    try:
        from openai import OpenAI

        loc = (location or "").strip()
        location_clause = f"in {loc}" if loc else "(no specific location)"
        context_block = ""
        if brand_context and brand_context.strip():
            context_block = (
                "\n\nGround truth about what this brand actually sells "
                "(scraped from the live website — trust this over the "
                f"industry label):\n{brand_context.strip()[:1500]}\n"
                "\nGenerate queries that match what THIS brand sells, "
                "not the generic category. If the brand is a heritage / "
                "nostalgia / licensed brand, lean into that angle. If "
                "the brand sells merchandise alongside its core product, "
                "include merch-style queries. Use the brand's own name "
                "where relevant."
            )

        prompt = (
            f"Generate {count} natural-language search queries a real "
            f"shopper or buyer might ask an AI assistant when looking "
            f"for {topic} {location_clause}.\n\n"
            "Mix the query types:\n"
            "- discovery: 'best …', 'top-rated …', 'where to buy …'\n"
            "- comparison: '<brand> vs <brand>', 'alternatives to …'\n"
            "- decision: 'is X worth it?', 'how much does X cost?', "
            "'what's the difference between …'\n"
            "- problem-led: 'X for Y', 'X near me', 'late-night X'\n"
            "- specific feature: vary by what people actually care about "
            "for this category (e.g. flavours / pricing / delivery / "
            "subscriptions / dietary / occasion / nostalgia / gifting)."
            f"{context_block}\n\n"
            "Return JSON only: {\"queries\": [\"…\", …]}\n"
            "Each query: under 90 characters, lowercase, conversational, "
            "no quotes around it."
        )
        client = OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You write realistic shopper search queries. "
                        "Vary phrasing — never lean on a single template. "
                        "When a brand context is provided, prefer queries "
                        "that match what THAT specific brand sells over "
                        "generic category queries."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            max_tokens=900,
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        queries = parsed.get("queries") or []
        cleaned = [
            str(q).strip()
            for q in queries
            if isinstance(q, str) and q.strip()
        ]
        return cleaned[:count]
    except Exception as exc:
        logger.warning("LLM query generation failed: %s", exc)
        return []


def _template_query_ideas(
    *,
    topic: str,
    location: Optional[str],
) -> List[str]:
    """Deterministic fallback. Used when OPENAI_API_KEY is unset or
    the LLM call raised. Same shape as the original generator so the
    pipeline keeps working — just less convincing in the report."""
    location_part = f" {location}" if location else ""
    intent = [
        f"best {topic}{location_part}",
        f"top {topic}{location_part}",
        f"{topic} services{location_part}",
        f"recommended {topic}{location_part}",
        f"{topic} company{location_part}",
    ]
    problem = [
        f"how much does {topic} cost{location_part}",
        f"how to choose {topic}{location_part}",
        f"what is the best {topic}{location_part}",
        f"who are the top {topic} companies{location_part}",
        f"affordable {topic}{location_part}",
    ]
    comparison = [
        f"{topic} companies in{location_part}",
        f"compare {topic} services{location_part}",
        f"top rated {topic}{location_part}",
        f"{topic} experts{location_part}",
        f"trusted {topic}{location_part}",
    ]
    question = [
        f"what is {topic}",
        f"how does {topic} work",
        f"is {topic} worth it",
        f"who needs {topic}",
        f"why is {topic} important",
    ]
    return intent + problem + comparison + question


def generate_queries(topic, location=None, brand_context=None):
    """Return a list of queries to test against AI engines.

    LLM-driven when OPENAI_API_KEY is set; template fallback otherwise.
    Either way the contract is the same — a flat list of strings.

    `brand_context` is an optional summary of the homepage scan; pass
    it through to anchor the LLM on what the brand actually sells.
    """
    topic_str = (topic or "").strip()
    if not topic_str:
        return []

    llm_ideas = _llm_query_ideas(
        topic=topic_str,
        location=location,
        brand_context=brand_context,
    )
    if llm_ideas:
        return llm_ideas

    return _template_query_ideas(topic=topic_str, location=location)


if __name__ == "__main__":
    print("\nGenerated Queries:\n")
    for q in generate_queries("interior designer", "singapore"):
        print("-", q)
