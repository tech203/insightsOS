from __future__ import annotations

from typing import Any, Dict, List, Optional


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(round(float(value)))
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _unique_actions(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    output = []

    for action in actions:
        key = (
            action.get("category", ""),
            action.get("title", ""),
            action.get("linked_query", ""),
            action.get("recommended_fix", ""),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(action)

    output.sort(key=lambda x: PRIORITY_ORDER.get(x.get("priority", "low"), 99))
    return output


def _make_action(
    *,
    category: str,
    priority: str,
    title: str,
    issue: str,
    why_it_matters: str,
    recommended_fix: str,
    linked_query: Optional[str] = None,
    suggested_content_type: Optional[str] = None,
    support_signal: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "category": category,
        "priority": priority,
        "title": title,
        "issue": issue,
        "why_it_matters": why_it_matters,
        "recommended_fix": recommended_fix,
        "linked_query": linked_query,
        "suggested_content_type": suggested_content_type,
        "support_signal": support_signal or "",
    }


def _infer_content_type_from_query(query: str, competitors: Optional[List[str]] = None) -> str:
    q = (query or "").lower()

    if " vs " in q or "versus" in q or "alternative" in q or competitors:
        return "comparison_page"
    if q.startswith("what ") or q.startswith("how ") or q.startswith("why "):
        return "guide"
    if "near me" in q or "singapore" in q or "location" in q:
        return "location_page"
    if "faq" in q or q.startswith("can ") or q.startswith("does "):
        return "faq_page"
    return "service_page"


def build_recommended_actions(
    *,
    client_name: str,
    website: str,
    scores: Dict[str, Any],
    query_analysis: List[Dict[str, Any]],
    competitor_analysis: Dict[str, Any],
    site_findings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []

    normalized_score = _safe_int(scores.get("normalized_score"))
    visibility_score = _safe_float(scores.get("visibility_score"))
    content_score = _safe_float(scores.get("content_score"))
    schema_score = _safe_float(scores.get("schema_score"))
    entity_score = _safe_float(scores.get("entity_score"))

    missing_brand_queries = [
        row for row in query_analysis
        if not row.get("brand_mentioned", False)
    ]
    declining_queries = [
        row for row in query_analysis
        if _safe_float(row.get("score_delta")) < 0
    ]
    competitor_heavy_queries = [
        row for row in query_analysis
        if row.get("competitors_mentioned")
    ]

    if visibility_score <= 10:
        actions.append(
            _make_action(
                category="visibility_gap",
                priority="high",
                title="Improve brand visibility in AI answers",
                issue="Overall visibility score is weak.",
                why_it_matters="If the brand is not being surfaced in answer engines, competitors are more likely to be recommended first.",
                recommended_fix="Prioritize direct-answer pages, comparison pages, and FAQ-led content for missed commercial and recommendation-style queries.",
                support_signal="Low visibility score across the latest audit.",
            )
        )

    if content_score <= 3:
        actions.append(
            _make_action(
                category="content_gap",
                priority="high",
                title="Expand content coverage around target intent",
                issue="Content score is weak.",
                why_it_matters="Thin or incomplete topic coverage makes it harder for answer engines to understand when your brand should be included.",
                recommended_fix="Build or expand service pages, FAQ pages, and educational content mapped to the top missed query clusters.",
                suggested_content_type="service_page",
                support_signal="Low content score in latest audit.",
            )
        )

    if schema_score <= 3:
        actions.append(
            _make_action(
                category="schema_fix",
                priority="medium",
                title="Strengthen structured data support",
                issue="Schema score is weak.",
                why_it_matters="Structured data helps clarify business identity, page purpose, and reusable question-answer content.",
                recommended_fix="Add or improve Organization, FAQPage, Service, and LocalBusiness schema where relevant.",
                support_signal="Low schema score in latest audit.",
            )
        )

    if entity_score <= 3:
        actions.append(
            _make_action(
                category="entity_fix",
                priority="medium",
                title="Clarify brand and entity signals",
                issue="Entity clarity is weak.",
                why_it_matters="If your site does not clearly explain who you are, what you offer, and how you differ, answer engines may prefer more explicit competitors.",
                recommended_fix="Strengthen homepage, about page, and core service pages with clearer positioning, repeated brand signals, proof, and differentiators.",
                support_signal="Low entity score in latest audit.",
            )
        )

    technical_issues = site_findings.get("technical_issues", [])
    if technical_issues:
        actions.append(
            _make_action(
                category="technical_fix",
                priority="medium",
                title="Resolve site-level technical blockers",
                issue="Technical issues were detected in the site findings.",
                why_it_matters="Weak internal structure, missing crawl signals, or broken page patterns can reduce how easily content is discovered and interpreted.",
                recommended_fix="Resolve the highest-impact issues first, especially homepage clarity, missing FAQ blocks, weak internal linking, and missing structured content sections.",
                support_signal=", ".join(technical_issues[:3]),
            )
        )

    for row in sorted(missing_brand_queries, key=lambda x: _safe_float(x.get("score")), reverse=False)[:6]:
        query = row.get("query", "Unknown query")
        competitors = row.get("competitors_mentioned", []) or []
        content_type = _infer_content_type_from_query(query, competitors)

        actions.append(
            _make_action(
                category="query_opportunity",
                priority="high",
                title=f"Target missed query: {query}",
                issue="Brand is not mentioned for this tracked query.",
                why_it_matters="This is a direct visibility gap where answer engines are not associating the brand with the query intent.",
                recommended_fix=f"Create or improve a {content_type.replace('_', ' ')} focused on '{query}' with direct answers, proof, and stronger differentiators.",
                linked_query=query,
                suggested_content_type=content_type,
                support_signal=f"Competitors seen: {', '.join(competitors[:3]) if competitors else 'none recorded'}",
            )
        )

    for row in sorted(declining_queries, key=lambda x: _safe_float(x.get("score_delta")))[:3]:
        query = row.get("query", "Unknown query")
        actions.append(
            _make_action(
                category="refresh_existing",
                priority="medium",
                title=f"Refresh declining query: {query}",
                issue="This query declined compared with the previous audit.",
                why_it_matters="A falling query score may signal stale content, weak structure, or stronger competitor presence.",
                recommended_fix="Refresh the related page with clearer answer blocks, stronger headings, updated proof, and better comparison positioning.",
                linked_query=query,
                suggested_content_type=_infer_content_type_from_query(query, row.get("competitors_mentioned")),
                support_signal=f"Score delta: {row.get('score_delta', 0)}",
            )
        )

    top_competitors = competitor_analysis.get("top_competitors", [])
    for comp in top_competitors[:3]:
        if comp.get("mention_count", 0) <= 1:
            continue

        competitor_name = comp.get("name", "Competitor")
        actions.append(
            _make_action(
                category="competitor_counter",
                priority="medium",
                title=f"Build counter-positioning against {competitor_name}",
                issue=f"{competitor_name} appears repeatedly in tracked answer comparisons.",
                why_it_matters="Repeated competitor presence suggests stronger topic ownership or clearer entity positioning.",
                recommended_fix=f"Create competitor-aware comparison pages, differentiation content, and FAQ content showing when {client_name} is a better fit.",
                suggested_content_type="comparison_page",
                support_signal=f"{competitor_name} appeared in {comp.get('mention_count', 0)} tracked queries.",
            )
        )

    if normalized_score >= 70 and not missing_brand_queries:
        actions.append(
            _make_action(
                category="scale_wins",
                priority="low",
                title="Scale winning content patterns",
                issue="Overall performance is healthy.",
                why_it_matters="Once core visibility gaps are reduced, growth usually comes from expanding winning structures into adjacent topics.",
                recommended_fix="Identify the best-performing pages and replicate their structure across adjacent query clusters.",
                support_signal=f"Normalized score: {normalized_score}",
            )
        )

    return _unique_actions(actions)


def build_content_opportunities(recommended_actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    opportunities: List[Dict[str, Any]] = []

    for action in recommended_actions:
        linked_query = action.get("linked_query")
        suggested_content_type = action.get("suggested_content_type")

        if not linked_query or not suggested_content_type:
            continue

        opportunities.append(
            {
                "title": linked_query,
                "target_query": linked_query,
                "content_type": suggested_content_type,
                "priority": action.get("priority", "medium"),
                "source_action_title": action.get("title", ""),
                "reason": action.get("issue", ""),
                "status": "idea",
            }
        )

    seen = set()
    deduped = []
    for item in opportunities:
        key = (item["target_query"], item["content_type"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    deduped.sort(key=lambda x: PRIORITY_ORDER.get(x.get("priority", "low"), 99))
    return deduped