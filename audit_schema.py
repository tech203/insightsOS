from __future__ import annotations

import os
from collections import Counter
from dtutils import utcnow
from typing import Any, Dict, List, Optional

from action_engine import build_content_opportunities, build_recommended_actions


OUTPUTS_FOLDER = "outputs"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(round(float(value)))
    except Exception:
        return default


def _now_iso() -> str:
    return utcnow().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    if not text:
        return "audit"

    text = text.strip().lower()
    cleaned = []

    for ch in text:
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in [" ", "-", "_", "."]:
            cleaned.append("-")

    slug = "".join(cleaned)
    while "--" in slug:
        slug = slug.replace("--", "-")

    return slug.strip("-") or "audit"


def normalize_website(url: str) -> str:
    if not url:
        return ""

    return (
        url.strip()
        .lower()
        .replace("https://", "")
        .replace("http://", "")
        .replace("www.", "")
        .rstrip("/")
    )


def compute_scores(
    *,
    ai_answer_results: List[Dict[str, Any]],
    site_findings: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the score sheet for an audit run.

    Scoring philosophy: visibility (does the brand actually show up in
    AI answers?) is the dominant signal of an *AI Visibility* audit.
    Every other factor — schema, content, technical hygiene, entity
    clarity — only matters insofar as it raises the ceiling on
    visibility. A brand that is never cited cannot have a "moderate"
    or higher overall score; site hygiene without visibility is just
    pre-work.

    Two changes from the prior formulation:

    1. All sub-scores are clamped to 0-10 before they enter the
       weighted sum. Previously content_depth_score etc. could be
       any number; for White Rabbit, content_depth flowed in at 17
       and double-counted toward the overall.

    2. The weighted-sum is gated by brand_mention_rate:
         - 0%  cited  → cap overall at 25
         - <25%        → cap at 55
         - <50%        → cap at 75
       This matches buyer expectation: zero AI citations should never
       read as "moderate" performance.
    """
    total_queries = len(ai_answer_results)
    brand_mentions = sum(
        1 for row in ai_answer_results if row.get("brand_mentioned", False)
    )
    total_score = sum(
        _safe_float(row.get("score", 0.0)) for row in ai_answer_results
    )
    avg_query_score = (total_score / total_queries) if total_queries else 0.0

    # Clamp all site sub-scores to the 0-10 range we document.
    def _clamp10(v):
        return max(0.0, min(10.0, _safe_float(v)))

    content_score_10 = round(_clamp10(site_findings.get("content_depth_score", 0)), 1)
    schema_score_10 = round(_clamp10(site_findings.get("schema_score", 0)), 1)
    entity_score_10 = round(_clamp10(site_findings.get("entity_score", 0)), 1)
    technical_score_10 = round(_clamp10(site_findings.get("technical_score", 0)), 1)

    # Visibility on a 0-100 scale (was previously a 0-20 sub-scale).
    brand_mention_rate = (
        (brand_mentions / total_queries) if total_queries else 0.0
    )
    visibility_score_100 = round(brand_mention_rate * 100, 1)

    # Site-hygiene composite on a 0-100 scale: average of 4 clamped
    # 0-10 sub-scores, scaled up to 100.
    site_hygiene_100 = round(
        ((content_score_10 + schema_score_10 + entity_score_10 + technical_score_10) / 4) * 10,
        1,
    )

    # Weighted blend: visibility 65%, site hygiene 25%, query-relevance 10%.
    # Visibility leads because that's the headline finding of an
    # "AI Visibility Audit" — site hygiene is supporting context.
    raw_score = (
        visibility_score_100 * 0.65
        + site_hygiene_100 * 0.25
        + min(100.0, avg_query_score * 10) * 0.10
    )

    # Cap the headline score by visibility tier so a 0-visibility brand
    # cannot read as "moderate" performance.
    if brand_mentions == 0:
        cap = 25.0
    elif brand_mention_rate < 0.25:
        cap = 55.0
    elif brand_mention_rate < 0.50:
        cap = 75.0
    else:
        cap = 100.0

    normalized_score = round(min(cap, raw_score), 1)

    return {
        "normalized_score": normalized_score,
        # Keep visibility_score on the 0-100 scale so the PDF metric
        # card matches the headline number's units. Old field name
        # preserved for downstream compatibility.
        "visibility_score": visibility_score_100,
        "content_score": content_score_10,
        "schema_score": schema_score_10,
        "entity_score": entity_score_10,
        "technical_score": technical_score_10,
        "avg_query_score": round(avg_query_score, 2),
        "brand_mention_rate": round(brand_mention_rate * 100, 1),
        "total_queries": total_queries,
        "brand_mentions": brand_mentions,
        "score_cap_applied": cap if cap < 100 else None,
    }


def build_summary(
    *,
    website: str,
    client_name: str,
    scores: Dict[str, Any],
    query_analysis: List[Dict[str, Any]],
    recommended_actions: List[Dict[str, Any]],
) -> Dict[str, Any]:
    normalized_score = _safe_float(scores.get("normalized_score"))
    brand_mention_rate = _safe_float(scores.get("brand_mention_rate"))

    if normalized_score >= 75:
        verdict = "Strong AEO foundation with visible growth opportunities."
        opportunity_level = "Low"
    elif normalized_score >= 55:
        verdict = "Moderate AEO foundation with clear opportunities to improve visibility."
        opportunity_level = "Moderate"
    else:
        verdict = "Weak AEO visibility with substantial improvement potential."
        opportunity_level = "High"

    missed_queries = [row for row in query_analysis if not row.get("brand_mentioned", False)]
    top_missed_query = missed_queries[0]["query"] if missed_queries else None

    biggest_problem = (
        f"{client_name or website} is still missing from too many tracked answer queries."
        if brand_mention_rate < 50
        else f"{client_name or website} has partial presence but inconsistent answer visibility."
    )

    biggest_opportunity = (
        f"Build content for missed query '{top_missed_query}'."
        if top_missed_query
        else "Expand strong content patterns into adjacent query clusters."
    )

    top_3_actions = [action.get("title", "") for action in recommended_actions[:3]]

    return {
        "verdict": verdict,
        "opportunity_level": opportunity_level,
        "biggest_problem": biggest_problem,
        "biggest_opportunity": biggest_opportunity,
        "top_3_actions": top_3_actions,
    }


def build_competitor_analysis(query_analysis: List[Dict[str, Any]]) -> Dict[str, Any]:
    counter: Counter = Counter()

    for row in query_analysis:
        for competitor in row.get("competitors_mentioned", []) or []:
            if competitor:
                counter[competitor] += 1

    top_competitors = [
        {"name": name, "mention_count": count}
        for name, count in counter.most_common(10)
    ]

    return {
        "top_competitors": top_competitors,
        "total_distinct_competitors": len(counter),
    }


def _extract_query_from_row(row: Dict[str, Any]) -> str:
    return str(row.get("query") or "").strip()


def normalize_ai_answer_results(
    ai_answer_results: List[Dict[str, Any]],
    previous_ai_answer_results: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    previous_map = {
        _extract_query_from_row(row): row
        for row in (previous_ai_answer_results or [])
        if _extract_query_from_row(row)
    }

    normalized: List[Dict[str, Any]] = []

    for row in ai_answer_results:
        query = _extract_query_from_row(row)
        if not query:
            continue

        previous = previous_map.get(query, {})

        latest_score = _safe_float(row.get("score", 0.0))
        previous_score = _safe_float(previous.get("score", 0.0))
        score_delta = round(latest_score - previous_score, 2)

        brand_mentioned = bool(
            row.get("brand_mentioned", row.get("client_mentioned", False))
        )
        previous_brand_mentioned = bool(
            previous.get("brand_mentioned", previous.get("client_mentioned", False))
        )

        latest_competitors = row.get("latest_competitors", row.get("competitors_mentioned", [])) or []
        previous_competitors = previous.get("previous_competitors", previous.get("competitors_mentioned", [])) or []

        if score_delta > 0:
            change_type = "improved"
        elif score_delta < 0:
            change_type = "declined"
        elif brand_mentioned != previous_brand_mentioned:
            change_type = "changed"
        else:
            change_type = "unchanged"

        priority = "high" if not brand_mentioned else ("medium" if score_delta < 0 else "low")

        normalized.append(
            {
                "query": query,
                "brand_mentioned": brand_mentioned,
                "previous_brand_mentioned": previous_brand_mentioned,
                "brand_position": row.get("brand_position"),
                "previous_brand_position": previous.get("brand_position"),
                "score": latest_score,
                "previous_score": previous_score,
                "score_delta": score_delta,
                "change_type": change_type,
                "priority": priority,
                "competitors_mentioned": latest_competitors,
                "previous_competitors_mentioned": previous_competitors,
                "answer_excerpt": row.get("answer_excerpt", ""),
            }
        )

    normalized.sort(
        key=lambda x: (
            0 if x["priority"] == "high" else 1 if x["priority"] == "medium" else 2,
            x["score"],
        )
    )
    return normalized


def build_site_findings(
    *,
    audit_data: Optional[Dict[str, Any]] = None,
    content_score: float = 0.0,
    schema_score: float = 0.0,
    entity_score: float = 0.0,
    technical_score: float = 0.0,
) -> Dict[str, Any]:
    audit_data = audit_data or {}

    findings = {
        "content_depth_score": round(content_score, 1),
        "schema_score": round(schema_score, 1),
        "entity_score": round(entity_score, 1),
        "technical_score": round(technical_score, 1),
        "technical_issues": audit_data.get("technical_issues", []),
        "content_gaps": audit_data.get("content_gaps", []),
        "entity_gaps": audit_data.get("entity_gaps", []),
        "schema_gaps": audit_data.get("schema_gaps", []),
        "notes": audit_data.get("notes", []),
    }
    return findings


def build_audit_payload(
    *,
    website: str,
    industry: str,
    location: str,
    audit_type: str,
    topic: Optional[str],
    client_id: Optional[str],
    client_name: Optional[str],
    user_id: Optional[int],
    ai_answer_results: List[Dict[str, Any]],
    previous_ai_answer_results: Optional[List[Dict[str, Any]]] = None,
    raw_audit_data: Optional[Dict[str, Any]] = None,
    research_pack: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    raw_audit_data = raw_audit_data or {}

    site_findings = build_site_findings(
        audit_data=raw_audit_data.get("site_findings", raw_audit_data),
        content_score=_safe_float(raw_audit_data.get("content_score", 0)),
        schema_score=_safe_float(raw_audit_data.get("schema_score", 0)),
        entity_score=_safe_float(raw_audit_data.get("entity_score", 0)),
        technical_score=_safe_float(raw_audit_data.get("technical_score", 0)),
    )

    query_analysis = normalize_ai_answer_results(
        ai_answer_results=ai_answer_results,
        previous_ai_answer_results=previous_ai_answer_results,
    )

    competitor_analysis = build_competitor_analysis(query_analysis)
    scores = compute_scores(ai_answer_results=query_analysis, site_findings=site_findings)

    recommended_actions = build_recommended_actions(
        client_name=client_name or "",
        website=website,
        scores=scores,
        query_analysis=query_analysis,
        competitor_analysis=competitor_analysis,
        site_findings=site_findings,
        research_pack=research_pack,
        industry=industry,
    )

    content_opportunities = build_content_opportunities(recommended_actions)
    summary = build_summary(
        website=website,
        client_name=client_name or website,
        scores=scores,
        query_analysis=query_analysis,
        recommended_actions=recommended_actions,
    )

    saved_at = _now_iso()
    website_normalized = normalize_website(website)

    payload = {
        "meta": {
            "website": website,
            "website_normalized": website_normalized,
            "industry": industry,
            "location": location,
            "audit_type": audit_type,
            "topic": topic,
            "client_id": client_id,
            "client_name": client_name,
            "user_id": user_id,
            "saved_at": saved_at,
            "schema_version": "2.0",
        },
        "scores": scores,
        "summary": summary,
        "query_analysis": query_analysis,
        "competitor_analysis": competitor_analysis,
        "site_findings": site_findings,
        "recommended_actions": recommended_actions,
        "content_opportunities": content_opportunities,
        "ai_answer_results": query_analysis,  # backward-compatible alias
        "research_pack": research_pack,  # Tavily research bundle (None when key unset)
        "website": website,
        "client_id": client_id,
        "client_name": client_name,
        "user_id": user_id,
        "audit_type": audit_type,
        "saved_at": saved_at,
    }

    return payload


def save_audit_payload(payload: Dict[str, Any], outputs_folder: str = OUTPUTS_FOLDER) -> Dict[str, str]:
    """Persist a build_audit_payload() result as an Audit row.

    Migrated from the two-file outputs/*.json scheme to SQL. The
    return shape is preserved (callers expect summary_filename /
    full_filename) so audit_runner.py and main.py don't need to
    change. `summary_path` and `full_path` are kept in the return
    dict as URL-style identifiers (no actual file exists at those
    paths anymore); callers used them as opaque strings.

    The `outputs_folder` kwarg is retained for back-compat but
    ignored — the filename-identifier prefix it would have produced
    isn't relevant once the data lives in SQL.
    """
    from app import Audit, db

    website = payload.get("meta", {}).get("website") or payload.get("website", "site")
    audit_type = payload.get("meta", {}).get("audit_type") or payload.get("audit_type", "audit")
    # Microsecond resolution so back-to-back saves don't collide on
    # the `filename` primary key (same fix as build_base_filename
    # in save_results.py).
    timestamp = utcnow().strftime("%Y%m%d_%H%M%S_%f")
    website_slug = slugify(normalize_website(website))
    base_name = f"{website_slug}_{audit_type}_{timestamp}"

    summary_filename = f"{base_name}_summary.json"
    full_filename = f"{base_name}_full.json"

    summary_payload = {
        "website": payload.get("website"),
        "client_id": payload.get("client_id"),
        "client_name": payload.get("client_name"),
        "user_id": payload.get("user_id"),
        "audit_type": payload.get("audit_type"),
        "saved_at": payload.get("saved_at"),
        "scores": payload.get("scores", {}),
        "summary": payload.get("summary", {}),
        "recommended_actions": payload.get("recommended_actions", [])[:5],
        "content_opportunities": payload.get("content_opportunities", [])[:5],
        "meta": payload.get("meta", {}),
        "schema_version": payload.get("meta", {}).get("schema_version", "2.0"),
    }

    scores = payload.get("scores", {}) or {}

    audit = Audit(
        filename=summary_filename,
        user_id=payload.get("user_id"),
        client_id=(
            str(payload.get("client_id"))
            if payload.get("client_id") is not None else None
        ),
        client_name=payload.get("client_name"),
        website=payload.get("website"),
        audit_type=payload.get("audit_type"),
        saved_at=payload.get("saved_at") or utcnow().isoformat(),
        normalized_score=_safe_score(scores.get("normalized_score")),
        visibility_score=_safe_score(scores.get("visibility_score")),
        content_score=_safe_score(scores.get("content_score")),
        schema_score=_safe_score(scores.get("schema_score")),
        summary_payload=summary_payload,
        full_payload=payload,
    )
    db.session.add(audit)
    db.session.commit()

    # Preserve the legacy return shape. The "path" fields are
    # opaque-string identifiers in the new world, not on-disk paths.
    summary_path = os.path.join(outputs_folder, summary_filename)
    full_path = os.path.join(outputs_folder, full_filename)

    return {
        "summary_filename": summary_filename,
        "full_filename": full_filename,
        "summary_path": summary_path,
        "full_path": full_path,
    }


def _safe_score(v) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except Exception:
        return 0.0