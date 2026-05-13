"""
Marketplace presence audit.

Generate marketplace-flavoured prompts for a user's storefront and
check how often AI answer engines cite that shop. Reuses the existing
ai_answer_agent so the analysis (brand_mentioned, position, score)
stays consistent with the AI Answer Monitor.

We don't ingest the marketplace's catalog — that would require fragile
scraping or per-marketplace API integrations. Instead, we lean on the
fact that AI assistants are increasingly asked questions like
"best handmade ceramic mugs on Etsy" and check whether the user's shop
is one of the answers.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from dtutils import utcnow
from typing import Any, Dict, List, Optional


MARKETPLACE_LABELS: Dict[str, str] = {
    "etsy": "Etsy",
    "amazon": "Amazon",
    "shopee": "Shopee",
    "ebay": "eBay",
    "tiktok_shop": "TikTok Shop",
    "lazada": "Lazada",
    "other": "the marketplace",
}


def _label(marketplace: str) -> str:
    return MARKETPLACE_LABELS.get((marketplace or "").lower(), MARKETPLACE_LABELS["other"])


def _fallback_queries(marketplace: str, shop_name: str, category: str, region: str) -> List[str]:
    """Deterministic prompts used when OpenAI isn't configured. Five
    high-signal queries that tend to surface marketplace shops."""
    label = _label(marketplace)
    cat = category or "products"
    where = f" in {region}" if region else ""
    return [
        f"What are the best {cat} shops on {label}{where}?",
        f"Top-rated {cat} sellers on {label}{where} right now",
        f"Recommend a trusted {cat} shop on {label}{where}",
        f"Where can I buy quality {cat} on {label}?",
        f"Is {shop_name} a good seller on {label} for {cat}?",
    ]


def generate_marketplace_queries(
    *,
    marketplace: str,
    shop_name: str,
    category: Optional[str] = None,
    region: Optional[str] = None,
    count: int = 5,
) -> List[str]:
    """Generate marketplace-aware prompts via OpenAI; fall back to a
    deterministic set if the model is unavailable."""
    if not os.getenv("OPENAI_API_KEY"):
        return _fallback_queries(marketplace, shop_name, category or "", region or "")

    label = _label(marketplace)
    user_prompt = (
        f"Generate {count} natural-language search queries someone might ask "
        f"an AI assistant when looking for shops on {label}. Focus on "
        f"category: {category or 'general products'}; "
        f"region: {region or 'worldwide'}. "
        "Mix general-discovery queries (e.g. 'best mug shops on Etsy') with "
        "shop-specific recall queries (e.g. 'is <shop> trustworthy on Etsy'). "
        "Return JSON: {\"queries\": [\"…\", …]}. Each query should be a "
        "single sentence under 90 characters."
    )
    try:
        from openai import OpenAI
        client = OpenAI()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You write realistic shopper search queries."},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=400,
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        queries = parsed.get("queries") or []
        cleaned = [str(q).strip() for q in queries if isinstance(q, str) and q.strip()]
        if cleaned:
            return cleaned[:count]
    except Exception:
        pass
    return _fallback_queries(marketplace, shop_name, category or "", region or "")


def run_marketplace_audit(
    *,
    presence,
    simulate_ai_answer,
    engines: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run the marketplace audit for one MarketplacePresence row.

    Returns a payload with per-query results, an aggregate visibility
    score, and a citation rate. The caller is responsible for
    persisting `last_audit_payload` + `last_visibility_score` on the
    presence row."""
    engines = engines or ["chatgpt"]
    queries = generate_marketplace_queries(
        marketplace=presence.marketplace,
        shop_name=presence.shop_name or presence.shop_url,
        category=presence.category,
        region=presence.region,
    )

    rows: List[Dict[str, Any]] = []
    cited_count = 0
    score_total = 0
    score_n = 0

    brand_name = (presence.shop_name or presence.shop_url or "").strip()

    for query in queries:
        per_engine: List[Dict[str, Any]] = []
        for engine in engines:
            try:
                result = simulate_ai_answer(
                    query=query,
                    company_name=brand_name,
                    engine=engine,
                )
            except Exception:
                continue
            per_engine.append(
                {
                    "engine": engine,
                    "engine_label": result.get("engine_label", engine),
                    "brand_mentioned": bool(result.get("brand_mentioned")),
                    "score": int(result.get("score") or 0),
                    "competitors": result.get("competitors_mentioned") or [],
                    "answer_type": result.get("answer_type"),
                }
            )

        any_cited = any(r["brand_mentioned"] for r in per_engine)
        if any_cited:
            cited_count += 1
        best = max((r["score"] for r in per_engine), default=0)
        score_total += best
        score_n += 1

        rows.append(
            {
                "query": query,
                "any_cited": any_cited,
                "best_score": best,
                "engines": per_engine,
            }
        )

    citation_rate = round(cited_count / len(queries), 2) if queries else 0.0
    avg_score = round(score_total / score_n, 1) if score_n else 0.0
    visibility_score = int(round(citation_rate * 100))

    return {
        "marketplace": presence.marketplace,
        "marketplace_label": _label(presence.marketplace),
        "shop_name": presence.shop_name or presence.shop_url,
        "shop_url": presence.shop_url,
        "queries": rows,
        "citation_rate": citation_rate,
        "avg_score": avg_score,
        "visibility_score": visibility_score,
        "audited_at": utcnow().isoformat(),
    }
