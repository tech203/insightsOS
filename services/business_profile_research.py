"""
Business profile enrichment.

Pulls the data the audit PDF's Business Profile table and Executive
Summary need — founded year, Google rating + review count, a short
business summary — using Tavily search results plus an OpenAI
extraction pass.

Two-step flow because Tavily returns raw search snippets and we want
structured fields:

  1. tavily.search()         → raw web snippets about the business
  2. gpt-4o-mini extraction  → JSON with founded_year, rating, review
                                count, services, summary

Cached on the Client row via business_profile_updated_at so the PDF
renders fast on repeat opens.
"""

from __future__ import annotations

import json
import os
import re
from dtutils import utcnow
from typing import Any, Dict, List, Optional


def _tavily_search(query: str, max_results: int = 6) -> List[Dict[str, Any]]:
    """Best-effort Tavily search. Returns empty list on any error so
    the PDF still renders without business profile data."""
    if not os.getenv("TAVILY_API_KEY"):
        return []
    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
        result = client.search(
            query=query,
            search_depth="basic",
            max_results=max_results,
            include_answer=True,
        )
        out: List[Dict[str, Any]] = []
        if result.get("answer"):
            out.append({"title": "summary", "content": result["answer"]})
        for r in result.get("results") or []:
            out.append({
                "title": r.get("title") or "",
                "content": r.get("content") or "",
                "url": r.get("url") or "",
            })
        return out
    except Exception:
        return []


def _extract_with_openai(
    *,
    name: str,
    website: str,
    snippets: List[Dict[str, Any]],
    site_context: Optional[str] = None,
) -> Dict[str, Any]:
    """Single OpenAI call to extract structured fields from Tavily
    snippets. Returns {} on any failure — caller treats missing fields
    as None and the PDF degrades gracefully.

    `site_context` is the homepage scan (title + meta description +
    hero copy + nav categories). When provided, the LLM is pinned to
    that as ground truth so Tavily snippets about a same-named but
    DIFFERENT business (e.g. "The White Rabbit" restaurant vs the
    nostalgia-candy brand at whiterabbit.sg) don't pollute the
    summary.
    """
    if not os.getenv("OPENAI_API_KEY"):
        return {}

    snippet_text = "\n\n".join(
        f"[{s.get('title') or 'snippet'}] {s.get('content') or ''}"
        for s in snippets[:10]
    )[:6000]
    if not snippet_text.strip() and not site_context:
        return {}

    grounding_block = ""
    if site_context and site_context.strip():
        grounding_block = (
            "\n\nGROUND TRUTH (from the live website at this exact URL — "
            "trust this over the snippets when they conflict):\n"
            f"{site_context.strip()[:1500]}\n"
        )

    user_prompt = (
        f"Extract structured business facts about {name} ({website}) from the "
        "search snippets below."
        f"{grounding_block}\n\n"
        "Return STRICT JSON with these keys (use null when information isn't "
        "supported by the snippets/ground-truth — never guess):\n"
        "  - founded_year: 4-digit string or null\n"
        "  - google_rating: number 0-5 as string (e.g. '4.7') or null\n"
        "  - google_review_count: integer or null\n"
        "  - core_services: comma-separated string or null\n"
        "  - executive_summary: 2-3 sentences in factual neutral tone, no "
        "marketing fluff. If snippets reference a same-named but different "
        "business than the website's homepage suggests, IGNORE those "
        "snippets and ground the summary in the homepage. ~60-90 words.\n\n"
        f"SNIPPETS:\n{snippet_text or '(no snippets — use ground truth only)'}"
    )
    try:
        from openai import OpenAI
        client = OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You extract structured business facts. Never invent. Use null when unsupported. The website's own homepage copy outranks third-party snippets when they conflict."},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=400,
        )
        raw = response.choices[0].message.content or "{}"
        return json.loads(raw)
    except Exception:
        return {}


def research_business_profile(
    *,
    name: str,
    website: str,
    location: Optional[str] = None,
    site_context: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the two-step research and return the dict the model expects.

    `site_context` is an optional homepage-scan blurb that grounds the
    LLM extraction. Pass it whenever you have it — without it, Tavily
    snippets can drift onto a different same-named business.

    Caller persists onto the Client row + business_profile_updated_at."""
    if not name and not website:
        return {}

    # If we don't already have site context, do a quick scan ourselves.
    if not site_context and website:
        try:
            from services.site_scan import scan_website, brand_context_blurb
            scan = scan_website(website)
            if scan and scan.get("fetched"):
                site_context = brand_context_blurb(scan)
        except Exception:
            site_context = None

    primary_query = f"{name} {website or ''} {location or ''} reviews founded services".strip()
    snippets = _tavily_search(primary_query, max_results=6)

    if not snippets:
        # Fall back to a slightly broader query so we have something
        # for the OpenAI extractor.
        snippets = _tavily_search(
            f"{name or website} {location or ''}".strip(), max_results=4
        )

    extracted = _extract_with_openai(
        name=name or website,
        website=website or "",
        snippets=snippets,
        site_context=site_context,
    )

    rating = extracted.get("google_rating")
    if isinstance(rating, (int, float)):
        rating = f"{float(rating):.1f}"
    elif isinstance(rating, str):
        rating = rating.strip() or None

    review_count = extracted.get("google_review_count")
    if isinstance(review_count, str):
        try:
            review_count = int(re.sub(r"[^0-9]", "", review_count))
        except (TypeError, ValueError):
            review_count = None
    elif not isinstance(review_count, int):
        review_count = None

    return {
        "founded_year": (extracted.get("founded_year") or None) or None,
        "google_rating": rating,
        "google_review_count": review_count,
        "core_services": extracted.get("core_services") or None,
        "executive_summary": extracted.get("executive_summary") or None,
    }


def is_profile_stale(client_row, *, max_age_days: int = 30) -> bool:
    """Has the business_profile_updated_at gone past max_age_days?"""
    last = getattr(client_row, "business_profile_updated_at", None)
    if not last:
        return True
    return (utcnow() - last).days >= max_age_days
