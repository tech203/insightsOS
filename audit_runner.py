from __future__ import annotations
import os

from typing import Any, Dict, List, Optional

from audit_schema import build_audit_payload, save_audit_payload
from save_results import save_audit_results


def _safe_import(module_name: str, func_name: str):
    try:
        module = __import__(module_name, fromlist=[func_name])
        return getattr(module, func_name, None)
    except Exception:
        return None


generate_queries = _safe_import("query_agent", "generate_queries")
discover_competitors = _safe_import("competitor_agent", "discover_competitors")
audit_website = _safe_import("audit_agent", "audit_website")
calculate_visibility_score = _safe_import("visibility_agent", "calculate_visibility_score")
build_report = _safe_import("report_agent", "build_report")
run_ai_answer_test = _safe_import("ai_answer_agent", "run_ai_answer_test")

def _safe_call(func, *args, **kwargs):
    if not callable(func):
        print("SAFE CALL FAILED: function not callable")
        return None
    try:
        return func(*args, **kwargs)
    except Exception as e:
        print(f"SAFE CALL ERROR in {getattr(func, '__name__', 'unknown')}: {e}")
        return None

def _normalize_query_list(raw_queries: Any, industry: str, website: str, topic: Optional[str]) -> List[str]:
    if isinstance(raw_queries, list):
        cleaned = [str(q).strip() for q in raw_queries if str(q).strip()]
        if cleaned:
            return cleaned[:12]

    seed_topic = topic or industry or website
    seed_topic = str(seed_topic).strip()

    fallback_queries = [
        f"what is {seed_topic}",
        f"best {seed_topic}",
        f"{seed_topic} pricing",
        f"{seed_topic} alternatives",
        f"how to choose {seed_topic}",
        f"{seed_topic} for small business",
    ]
    return fallback_queries[:8]


def _normalize_competitors(raw_competitors: Any) -> List[str]:
    if isinstance(raw_competitors, dict):
        direct = raw_competitors.get("direct_competitors", []) or []
        cleaned = []

        for item in direct:
            if isinstance(item, (list, tuple)) and len(item) >= 1:
                cleaned.append(str(item[0]).strip())
            elif isinstance(item, dict):
                name = item.get("name") or item.get("domain") or item.get("website")
                if name:
                    cleaned.append(str(name).strip())
            elif item:
                cleaned.append(str(item).strip())

        return cleaned[:10]

    if isinstance(raw_competitors, list):
        cleaned = []
        for item in raw_competitors:
            if isinstance(item, dict):
                name = item.get("name") or item.get("domain") or item.get("website")
                if name:
                    cleaned.append(str(name).strip())
            elif item:
                cleaned.append(str(item).strip())
        return cleaned[:10]

    return []

def _simulate_ai_answer_results(
    *,
    queries: List[str],
    client_name: str,
    website: str,
    competitors: List[str],
    audit_data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    content_score = min(float(audit_data.get("content_score", 0) or 0), 10)
    schema_score = min(float(audit_data.get("schema_score", 0) or 0), 10)
    entity_score = min(float(audit_data.get("entity_score", 0) or 0), 10)
    technical_score = min(float(audit_data.get("technical_score", 0) or 0), 10)

    # Much stricter baseline
    base_visibility = 0.02
    base_visibility += content_score * 0.015
    base_visibility += schema_score * 0.010
    base_visibility += entity_score * 0.014
    base_visibility += technical_score * 0.010

    for idx, query in enumerate(queries):
        q = query.lower()
        query_bonus = 0.0

        if "vs" in q or "alternative" in q or "alternatives" in q:
            query_bonus -= 0.10

        if q.startswith("what ") or q.startswith("how "):
            query_bonus += 0.03

        if "pricing" in q:
            query_bonus -= 0.04

        visibility_probability = max(0.01, min(0.85, base_visibility + query_bonus))
        score = round(2 + (visibility_probability * 6), 2)

        # Harder to get brand mentioned
        brand_mentioned = score >= 4.8 and entity_score >= 4.0 and content_score >= 4.0

        if not brand_mentioned:
            brand_position = None
        elif score >= 6.8:
            brand_position = 1
        elif score >= 5.8:
            brand_position = 2
        else:
            brand_position = 3

        competitors_mentioned = []
        if competitors:
            if not brand_mentioned:
                competitors_mentioned = competitors[:2]
            elif idx % 2 == 0:
                competitors_mentioned = competitors[:1]

        answer_excerpt = (
            f"{client_name or website} appears associated with this topic."
            if brand_mentioned
            else f"The answer surface is dominated by alternative providers instead of {client_name or website}."
        )

        results.append(
            {
                "query": query,
                "brand_mentioned": brand_mentioned,
                "brand_position": brand_position,
                "score": score,
                "competitors_mentioned": competitors_mentioned,
                "answer_excerpt": answer_excerpt,
            }
        )

    return results

def _extract_site_scores(raw_audit: Any) -> Dict[str, Any]:
    if not isinstance(raw_audit, dict):
        return {
            "content_score": 4.0,
            "schema_score": 3.0,
            "entity_score": 3.0,
            "technical_score": 4.0,
            "site_findings": {},
        }

    content_score = raw_audit.get("content_score")
    schema_score = raw_audit.get("schema_score")
    entity_score = raw_audit.get("entity_score")
    technical_score = raw_audit.get("technical_score")

    if content_score is None:
        content_score = raw_audit.get("scores", {}).get("content_score", 4.0)
    if schema_score is None:
        schema_score = raw_audit.get("scores", {}).get("schema_score", 3.0)
    if entity_score is None:
        entity_score = raw_audit.get("scores", {}).get("entity_score", 3.0)
    if technical_score is None:
        technical_score = raw_audit.get("scores", {}).get("technical_score", 4.0)

    return {
        "content_score": float(content_score if content_score is not None else 0),
        "schema_score": float(schema_score if schema_score is not None else 0),
        "entity_score": float(entity_score if entity_score is not None else 0),
        "technical_score": float(technical_score if technical_score is not None else 0),
        "site_findings": raw_audit.get("site_findings", raw_audit),
        "used_fallback_scores": any(v is None for v in [content_score, schema_score, entity_score, technical_score]),
    }

def run_audit_for_input(
    *,
    website: str,
    industry: str,
    location: str,
    audit_type: str = "quick",
    topic: Optional[str] = None,
    client_id: Optional[str] = None,
    client_name: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Full replacement audit runner with:
    - normalized schema
    - action engine
    - saved summary/full outputs
    - backward-compatible keys for the current app
    """
    print("========== RUN_AUDIT_FOR_INPUT START ==========")
    print("website:", website)
    print("industry:", industry)
    print("location:", location)
    print("audit_type:", audit_type)
    print("topic:", topic)
    print("client_id:", client_id)
    print("client_name:", client_name)
    print("user_id:", user_id)

    raw_queries = _safe_call(
        generate_queries,
        topic=topic or industry,
        location=location,
    )
    print("raw_queries:", raw_queries)

    queries = _normalize_query_list(
        raw_queries,
        industry=industry,
        website=website,
        topic=topic,
    )
    print("queries:", queries)
    
    raw_competitors = _safe_call(
        discover_competitors,
        queries=queries,
    )
    print("raw_competitors:", raw_competitors)

    competitors = _normalize_competitors(raw_competitors)
    print("competitors:", competitors)

    raw_audit = _safe_call(audit_website, website) or {}
    print("raw_audit:", raw_audit)

    extracted_scores = _extract_site_scores(raw_audit)
    print("extracted_scores:", extracted_scores)

    raw_audit.update(extracted_scores)

    brand_name = client_name or website.replace("https://", "").replace("http://", "").replace("www.", "").split(".")[0].replace("-", " ").title()

    business_profile = {
        "title": brand_name
    }

    try:
        ai_answer_results = run_ai_answer_test(
            queries_to_test=queries,
            business_profile=business_profile,
        ) or []
        print("REAL AI ANSWER TEST USED")
        print("ai_answer_results sample:", ai_answer_results[:2])
    except Exception as e:
        print("REAL AI ANSWER TEST FAILED:", str(e))
        ai_answer_results = []

    if not ai_answer_results:
        print("FALLING BACK TO SIMULATED AI ANSWERS")
        ai_answer_results = _simulate_ai_answer_results(
            queries=queries,
            client_name=client_name or website,
            website=website,
            competitors=competitors,
            audit_data=raw_audit,
        )

    print("ai_answer_results count:", len(ai_answer_results))

    payload = build_audit_payload(
        website=website,
        industry=industry,
        location=location,
        audit_type=audit_type,
        topic=topic,
        client_id=client_id,
        client_name=client_name,
        user_id=user_id,
        ai_answer_results=ai_answer_results,
        previous_ai_answer_results=None,
        raw_audit_data=raw_audit,
    )

    print("PAYLOAD BUILT")
    print("payload keys:", list(payload.keys()))
    print("payload client_id:", payload.get("client_id"))
    print("payload client_name:", payload.get("client_name"))
    print("payload user_id:", payload.get("user_id"))
    print("payload website:", payload.get("website"))
    print("payload audit_type:", payload.get("audit_type"))

    saved_files = save_audit_payload(payload)

    print("SAVE COMPLETE")
    print("saved_files:", saved_files)

    summary_path = os.path.join("outputs", saved_files["summary_filename"])
    full_path = os.path.join("outputs", saved_files["full_filename"])
    print("summary exists:", os.path.exists(summary_path), summary_path)
    print("full exists:", os.path.exists(full_path), full_path)

    report = _safe_call(build_report, payload) or {}
    print("report built:", bool(report))

    response = {
        "website": website,
        "audit_type": audit_type,
        "summary_filename": saved_files["summary_filename"],
        "full_filename": saved_files["full_filename"],
        "scores": payload["scores"],
        "summary": payload["summary"],
        "recommended_actions": payload["recommended_actions"],
        "content_opportunities": payload["content_opportunities"],
        "report": report,
    }

    print("========== RUN_AUDIT_FOR_INPUT END ==========")
    print("response:", response)
    return response