"""
AI-visibility analysis for existing Webflow CMS content.

This powers the "existing Webflow user" path: pull a live CMS item's
current copy, score how well it answers AI/search queries, and produce
concrete field rewrites that can be pushed straight back to the same
Webflow item (matched by item id, so no duplicate pages are created).
"""

import json
import os

from openai import OpenAI

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# Webflow field slugs we treat as editable AI-visibility surfaces.
TEXT_FIELD_CANDIDATES = [
    "name",
    "meta-title",
    "meta-description",
    "summary",
    "post-summary",
    "content",
    "post-body",
    "body",
    "rich-text",
]


def extract_readable_fields(field_data):
    """Return the subset of a Webflow item's fieldData that is human copy."""
    readable = {}
    for slug, value in (field_data or {}).items():
        if not isinstance(value, str):
            continue
        if not value.strip():
            continue
        if slug in TEXT_FIELD_CANDIDATES or slug.endswith(
            ("-title", "-description", "-summary", "-body", "-content")
        ):
            readable[slug] = value
    return readable


def _truncate(text, limit=6000):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …[truncated]"


def analyze_item_for_ai_visibility(name, slug, field_data, model="gpt-4.1-mini"):
    """Score an existing Webflow item and propose field-level rewrites.

    Returns a dict:
      {
        "score": float (0-10),
        "summary": str,
        "issues": [str, ...],
        "suggested_fields": { "<webflow-field-slug>": "<rewritten value>" }
      }
    Only fields that already exist on the item are suggested for rewrite,
    so applying changes never introduces unknown fields.
    """
    readable = extract_readable_fields(field_data)
    editable_slugs = sorted(readable.keys())

    current_copy = "\n".join(
        f"[{slug}]\n{_truncate(value)}" for slug, value in readable.items()
    ) or "(no readable text fields found)"

    prompt = f"""You are an expert in Answer Engine Optimization (AEO) and
AI search visibility (ChatGPT, Perplexity, Google AI Overviews).

Analyze this existing web page's CMS content and improve it so AI answer
engines are more likely to cite it. Focus on: a clear answer-first
opening, specific entities, concise factual statements, a strong meta
title and meta description, and a short FAQ where natural.

Page name: {name}
Slug: {slug}

Current editable fields and their content:
{current_copy}

Rewrite ONLY these existing field slugs (do not invent new fields):
{json.dumps(editable_slugs)}

Return STRICT JSON, no markdown, with exactly this shape:
{{
  "score": <number 0-10 for current AI visibility>,
  "summary": "<one-sentence assessment>",
  "issues": ["<specific issue>", "..."],
  "suggested_fields": {{ "<field-slug>": "<improved value>" }}
}}
Keep rewrites truthful — never invent statistics, awards, or claims.
Preserve HTML structure for rich-text/body fields."""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You return only valid JSON. You optimize web copy for AI answer engines without fabricating facts.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "score": None,
            "summary": "Analysis returned an unparseable response.",
            "issues": [],
            "suggested_fields": {},
        }

    suggested = parsed.get("suggested_fields") or {}
    # Hard guard: never apply a field the item does not already have.
    safe_suggested = {
        slug: value
        for slug, value in suggested.items()
        if slug in readable and isinstance(value, str) and value.strip()
    }

    try:
        score = float(parsed.get("score"))
    except (TypeError, ValueError):
        score = None

    return {
        "score": score,
        "summary": str(parsed.get("summary") or "").strip(),
        "issues": [str(i) for i in (parsed.get("issues") or []) if str(i).strip()],
        "suggested_fields": safe_suggested,
    }
