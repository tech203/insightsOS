from __future__ import annotations

from typing import Any, Dict, List, Optional


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

DEFAULT_CREDIT_COSTS = {
    "technical": 3,
    "schema_fix": 3,
    "entity_fix": 5,
    "content_gap": 8,
    "query_opportunity": 8,
    "faq_opportunity": 5,
    "comparison_opportunity": 12,
    "competitor_counter": 12,
    "review_opportunity": 6,
    "trust_opportunity": 6,
    "visibility_gap": 10,
    "refresh_existing": 5,
    "scale_wins": 6,
    "website_page": 15,
    "manual": 0,
}


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


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _priority_from_impact(impact_score: int) -> str:
    if impact_score >= 14:
        return "high"
    if impact_score >= 8:
        return "medium"
    return "low"


def _credit_cost_for(category: str, suggested_content_type: Optional[str] = None) -> int:
    if suggested_content_type == "comparison_page":
        return 12
    if suggested_content_type == "faq_page":
        return 5
    if suggested_content_type == "guide":
        return 8
    if suggested_content_type == "location_page":
        return 8
    if suggested_content_type == "service_page":
        return 8
    return DEFAULT_CREDIT_COSTS.get(category, 5)


def _execution_type_for(category: str) -> str:
    manual_categories = {"review_opportunity", "trust_opportunity"}
    if category in manual_categories:
        return "manual_or_ai_assisted"
    return "ai_executable"


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

    output.sort(
        key=lambda x: (
            PRIORITY_ORDER.get(x.get("priority", "low"), 99),
            -_safe_int(x.get("impact_score")),
            _safe_int(x.get("credits_required")),
        )
    )

    return output


def _make_action(
    *,
    category: str,
    priority: Optional[str] = None,
    title: str,
    issue: str,
    why_it_matters: str,
    recommended_fix: str,
    linked_query: Optional[str] = None,
    suggested_content_type: Optional[str] = None,
    support_signal: Optional[str] = None,
    impact_score: int = 8,
    difficulty: str = "medium",
    credits_required: Optional[int] = None,
    execution_type: Optional[str] = None,
    source_url: Optional[str] = None,
    source_domain: Optional[str] = None,
) -> Dict[str, Any]:
    final_priority = priority or _priority_from_impact(impact_score)
    final_credits = credits_required

    if final_credits is None:
        final_credits = _credit_cost_for(category, suggested_content_type)

    clean_recommended_fix = _clean_text(recommended_fix)

    return {
        "category": category,
        "priority": final_priority,
        "title": _clean_text(title),
        "issue": _clean_text(issue),
        "why_it_matters": _clean_text(why_it_matters),
        "recommended_fix": clean_recommended_fix,
        "recommended_action": clean_recommended_fix,
        "linked_query": _clean_text(linked_query),
        "suggested_content_type": suggested_content_type,
        "support_signal": _clean_text(support_signal),
        "impact_score": _safe_int(impact_score),
        "difficulty": difficulty,
        "credits_required": _safe_int(final_credits),
        "execution_type": execution_type or _execution_type_for(category),
        "source_url": _clean_text(source_url),
        "source_domain": _clean_text(source_domain),
        "status": "open",
    }


def _infer_content_type_from_query(
    query: str,
    competitors: Optional[List[str]] = None
) -> str:
    q = (query or "").lower()

    if " vs " in q or "versus" in q or "alternative" in q or competitors:
        return "comparison_page"

    if q.startswith("what ") or q.startswith("how ") or q.startswith("why "):
        return "guide"

    if "near me" in q or "singapore" in q or "location" in q:
        return "location_page"

    if "faq" in q or q.startswith("can ") or q.startswith("does ") or q.startswith("is "):
        return "faq_page"

    if "best " in q or "top " in q or "compare" in q:
        return "comparison_page"

    return "service_page"


def _extract_research_results(
    research_pack: Optional[Dict[str, Any]],
    section: str
) -> List[Dict[str, Any]]:
    if not research_pack:
        return []

    results = research_pack.get("results", {})
    if not isinstance(results, dict):
        return []

    section_results = results.get(section, [])
    if not isinstance(section_results, list):
        return []

    return section_results


def _result_query_or_title(row: Dict[str, Any]) -> str:
    return (
        _clean_text(row.get("query"))
        or _clean_text(row.get("title"))
        or "Untitled opportunity"
    )

def _result_domain(row: Dict[str, Any]) -> str:
    return _clean_text(row.get("domain"))


def _result_url(row: Dict[str, Any]) -> str:
    return _clean_text(row.get("url"))


def _shorten_title(title: str, max_len: int = 110) -> str:
    title = _clean_text(title)
    if len(title) <= max_len:
        return title
    return title[: max_len - 3].rstrip() + "..."


_ECOM_KEYWORDS = (
    "ecommerce", "e-commerce", "e commerce",
    "shopify", "shoplazza", "woocommerce", "magento", "bigcommerce",
    "shop", "store", "marketplace",
    "retail", "online retail",
)


def _is_ecommerce_industry(industry: Optional[str]) -> bool:
    if not industry:
        return False
    text = str(industry).lower()
    return any(kw in text for kw in _ECOM_KEYWORDS)


def _ecommerce_actions(client_name: str, industry: str) -> List[Dict[str, Any]]:
    """Ecommerce-specific recommendations injected when the workspace's
    industry mentions ecommerce, store, marketplace, etc.

    These complement (not replace) the score-based audit actions. Each
    action gets category_tag="ecommerce" so the calendar / queue UI can
    pill them distinctly without us inventing new core categories.
    """
    short_industry = (industry or "").lower().replace("e-commerce", "ecom")[:60] or "your store"

    def _tag(action: Dict[str, Any]) -> Dict[str, Any]:
        action["category_tag"] = "ecommerce"
        return action

    raw = [
        _make_action(
            category="comparison_opportunity",
            title="Build comparison content for your top product categories",
            issue="Shoppers researching alternatives don't have a side-by-side guide on your site.",
            why_it_matters="AI answer engines surface comparison pages for 'X vs Y' and 'best X for Y' queries; without your own, competitor-led pages win.",
            recommended_fix="Create a comparison page for your two highest-revenue product categories (or top SKU vs nearest alternative).",
            suggested_content_type="comparison_page",
            impact_score=15,
            difficulty="medium",
        ),
        _make_action(
            category="content_gap",
            title="Add a buying guide for your main product line",
            issue="There's no top-of-funnel buying guide that explains how to choose between options.",
            why_it_matters="Buying guides capture high-intent informational traffic and feed AI answers for 'how to choose…' queries.",
            recommended_fix=f"Write a buying guide for {short_industry} buyers — sizing / fit / use case / price tier — and link it from category pages.",
            suggested_content_type="guide",
            impact_score=12,
            difficulty="medium",
        ),
        _make_action(
            category="faq_opportunity",
            title="Generate FAQ blocks for product / category pages",
            issue="Product detail pages don't surface common pre-purchase questions.",
            why_it_matters="FAQ schema is one of the strongest AI-readable signals; well-structured FAQs feed Featured Snippets and AI Overviews directly.",
            recommended_fix="Add a FAQ section (with FAQPage schema) on top categories — shipping, returns, sizing, materials.",
            suggested_content_type="faq_page",
            impact_score=11,
            difficulty="easy",
        ),
        _make_action(
            category="schema_fix",
            title="Audit Product schema across listings",
            issue="Without complete Product / Offer schema, AI answer engines can't pull price, availability, or rating reliably.",
            why_it_matters="Product-rich results in AI answers (price, stock, ratings) require valid schema. Missing fields silently demote you in AI surfaces.",
            recommended_fix="Run a structured-data check on top product URLs and fix missing fields (Product, Offer, AggregateRating, BreadcrumbList).",
            impact_score=10,
            difficulty="easy",
        ),
        _make_action(
            category="content_gap",
            title="Create 'best X for Y use case' landing pages",
            issue="Use-case landing pages capture niche AI-answer demand that broad category pages miss.",
            why_it_matters="AI answer engines reward specificity. 'Best for travel', 'Best for gifting', 'Best for sensitive skin' often outrank generic category pages in AI surfaces.",
            recommended_fix="Identify two or three high-intent 'best for' searches in your category and build dedicated landing pages.",
            suggested_content_type="landing_page",
            impact_score=12,
            difficulty="medium",
        ),
    ]

    return [_tag(a) for a in raw]


def build_recommended_actions(
    *,
    client_name: str,
    website: str,
    scores: Dict[str, Any],
    query_analysis: List[Dict[str, Any]],
    competitor_analysis: Dict[str, Any],
    site_findings: Dict[str, Any],
    research_pack: Optional[Dict[str, Any]] = None,
    industry: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Builds an AI Visibility Action Plan.

    Uses:
    - audit scores
    - tracked query analysis
    - competitor analysis
    - site findings
    - real search research pack from research_engine.py

    Output:
    list of executable action items with impact score, difficulty,
    credit cost, category, and execution type.
    """

    actions: List[Dict[str, Any]] = []

    scores = scores or {}
    query_analysis = query_analysis or []
    competitor_analysis = competitor_analysis or {}
    site_findings = site_findings or {}

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

    # ------------------------------------------------------------
    # 1. Score-based audit actions
    # ------------------------------------------------------------

    if visibility_score <= 10:
        actions.append(
            _make_action(
                category="visibility_gap",
                priority="high",
                title="Improve brand visibility in AI answers",
                issue="Overall visibility score is weak.",
                why_it_matters=(
                    "If the brand is not being surfaced in AI answers, "
                    "competitors are more likely to be recommended first."
                ),
                recommended_fix=(
                    "Prioritize direct-answer pages, comparison pages, FAQ-led content, "
                    "and stronger trust signals for missed commercial queries."
                ),
                support_signal="Low visibility score across the latest audit.",
                impact_score=18,
                difficulty="medium",
                credits_required=10,
            )
        )

    if content_score <= 3:
        actions.append(
            _make_action(
                category="content_gap",
                priority="high",
                title="Expand content coverage around target intent",
                issue="Content score is weak.",
                why_it_matters=(
                    "Thin or incomplete topic coverage makes it harder for answer engines "
                    "to understand when your brand should be included."
                ),
                recommended_fix=(
                    "Build or expand service pages, FAQ pages, and educational content "
                    "mapped to the top missed query clusters."
                ),
                suggested_content_type="service_page",
                support_signal="Low content score in latest audit.",
                impact_score=16,
                difficulty="medium",
                credits_required=8,
            )
        )

    if schema_score <= 3:
        actions.append(
            _make_action(
                category="schema_fix",
                priority="medium",
                title="Strengthen structured data support",
                issue="Schema score is weak.",
                why_it_matters=(
                    "Structured data helps clarify business identity, page purpose, "
                    "services, and reusable question-answer content."
                ),
                recommended_fix=(
                    "Add or improve Organization, FAQPage, Service, Breadcrumb, "
                    "and LocalBusiness schema where relevant."
                ),
                support_signal="Low schema score in latest audit.",
                impact_score=10,
                difficulty="easy",
                credits_required=3,
            )
        )

    if entity_score <= 3:
        actions.append(
            _make_action(
                category="entity_fix",
                priority="medium",
                title="Clarify brand and entity signals",
                issue="Entity clarity is weak.",
                why_it_matters=(
                    "If your site does not clearly explain who you are, what you offer, "
                    "where you operate, and why you are trusted, answer engines may prefer "
                    "more explicit competitors."
                ),
                recommended_fix=(
                    "Strengthen homepage, about page, service pages, contact details, "
                    "brand mentions, proof points, reviews, and differentiators."
                ),
                support_signal="Low entity score in latest audit.",
                impact_score=11,
                difficulty="medium",
                credits_required=5,
            )
        )

    # ------------------------------------------------------------
    # 2. Technical/site finding actions
    # ------------------------------------------------------------

    technical_issues = site_findings.get("technical_issues", [])
    if technical_issues:
        actions.append(
            _make_action(
                category="technical_fix",
                priority="medium",
                title="Resolve site-level technical blockers",
                issue="Technical issues were detected in the site findings.",
                why_it_matters=(
                    "Weak crawl signals, missing structured sections, broken pages, "
                    "or poor internal linking can reduce how easily content is discovered "
                    "and interpreted."
                ),
                recommended_fix=(
                    "Resolve the highest-impact issues first, especially missing schema, "
                    "weak internal linking, missing FAQ blocks, and weak homepage clarity."
                ),
                support_signal=", ".join([str(x) for x in technical_issues[:3]]),
                impact_score=10,
                difficulty="medium",
                credits_required=5,
            )
        )

    # ------------------------------------------------------------
    # 3. Query-based actions from tracked prompts
    # ------------------------------------------------------------

    for row in sorted(
        missing_brand_queries,
        key=lambda x: _safe_float(x.get("score")),
        reverse=False
    )[:6]:
        query = _clean_text(row.get("query")) or "Unknown query"
        competitors = row.get("competitors_mentioned", []) or []
        content_type = _infer_content_type_from_query(query, competitors)

        actions.append(
            _make_action(
                category="query_opportunity",
                priority="high",
                title=f"Target missed query: {query}",
                issue="Brand is not mentioned for this tracked query.",
                why_it_matters=(
                    "This is a direct visibility gap where answer engines are not "
                    "associating the brand with the query intent."
                ),
                recommended_fix=(
                    f"Create or improve a {content_type.replace('_', ' ')} focused on "
                    f"'{query}' with direct answers, proof, differentiators, and internal links."
                ),
                linked_query=query,
                suggested_content_type=content_type,
                support_signal=(
                    f"Competitors seen: {', '.join(competitors[:3]) if competitors else 'none recorded'}"
                ),
                impact_score=16,
                difficulty="medium",
                credits_required=_credit_cost_for("query_opportunity", content_type),
            )
        )

    for row in sorted(
        declining_queries,
        key=lambda x: _safe_float(x.get("score_delta"))
    )[:3]:
        query = _clean_text(row.get("query")) or "Unknown query"
        content_type = _infer_content_type_from_query(
            query,
            row.get("competitors_mentioned")
        )

        actions.append(
            _make_action(
                category="refresh_existing",
                priority="medium",
                title=f"Refresh declining query: {query}",
                issue="This query declined compared with the previous audit.",
                why_it_matters=(
                    "A falling query score may signal stale content, weak structure, "
                    "or stronger competitor presence."
                ),
                recommended_fix=(
                    "Refresh the related page with clearer answer blocks, updated proof, "
                    "stronger headings, better schema, and improved comparison positioning."
                ),
                linked_query=query,
                suggested_content_type=content_type,
                support_signal=f"Score delta: {row.get('score_delta', 0)}",
                impact_score=9,
                difficulty="easy",
                credits_required=_credit_cost_for("refresh_existing", content_type),
            )
        )

    if competitor_heavy_queries:
        actions.append(
            _make_action(
                category="competitor_counter",
                priority="medium",
                title="Create counter-positioning content for competitor-heavy queries",
                issue="Several tracked queries mention competitors.",
                why_it_matters=(
                    "Repeated competitor presence suggests stronger topic ownership, "
                    "more trusted pages, or clearer positioning."
                ),
                recommended_fix=(
                    "Create comparison pages, alternative pages, FAQ content, and proof-led "
                    "service pages that explain when your brand is a better fit."
                ),
                suggested_content_type="comparison_page",
                support_signal=f"{len(competitor_heavy_queries)} competitor-heavy tracked queries detected.",
                impact_score=12,
                difficulty="medium",
                credits_required=12,
            )
        )

    top_competitors = competitor_analysis.get("top_competitors", [])
    for comp in top_competitors[:3]:
        if _safe_int(comp.get("mention_count")) <= 1:
            continue

        competitor_name = _clean_text(comp.get("name")) or "Competitor"

        actions.append(
            _make_action(
                category="competitor_counter",
                priority="medium",
                title=f"Build counter-positioning against {competitor_name}",
                issue=f"{competitor_name} appears repeatedly in tracked answer comparisons.",
                why_it_matters=(
                    "Repeated competitor presence suggests stronger topic ownership "
                    "or clearer entity positioning."
                ),
                recommended_fix=(
                    f"Create competitor-aware comparison pages, differentiation content, "
                    f"and FAQ content showing when {client_name} is a better fit."
                ),
                suggested_content_type="comparison_page",
                support_signal=(
                    f"{competitor_name} appeared in {comp.get('mention_count', 0)} tracked queries."
                ),
                impact_score=12,
                difficulty="medium",
                credits_required=12,
            )
        )

    # ------------------------------------------------------------
    # 4. Real research-based actions from Tavily research pack
    # ------------------------------------------------------------

    customer_questions = _extract_research_results(research_pack, "customer_questions")
    for row in customer_questions[:5]:
        question = _result_query_or_title(row)

        actions.append(
            _make_action(
                category="faq_opportunity",
                priority="high",
                title=f"Create FAQ content for: {_shorten_title(question)}",
                issue="Real search data shows customers are actively looking for answers around this topic.",
                why_it_matters=(
                    "Clear answer-style content improves AI readability and can increase "
                    "the likelihood of being cited in AI-generated responses."
                ),
                recommended_fix=(
                    f"Create a dedicated FAQ block or article answering '{question}' "
                    "with a direct answer, supporting details, internal links, and FAQ schema."
                ),
                linked_query=question,
                suggested_content_type="faq_page",
                support_signal=_result_domain(row),
                source_url=_result_url(row),
                source_domain=_result_domain(row),
                impact_score=15,
                difficulty="easy",
                credits_required=5,
            )
        )

    comparison_queries = _extract_research_results(research_pack, "comparison_queries")
    for row in comparison_queries[:10]:
        title = _result_query_or_title(row)

        actions.append(
            _make_action(
                category="comparison_opportunity",
                priority="high",
                title=f"Build comparison content: {_shorten_title(title)}",
                issue="Real search data shows comparison-style queries with commercial intent.",
                why_it_matters=(
                    "AI systems often use comparison and recommendation-style content "
                    "when answering decision-making queries."
                ),
                recommended_fix=(
                    "Create a structured comparison page with clear differences, pricing signals, "
                    "pros and cons, proof points, FAQs, and schema."
                ),
                linked_query=title,
                suggested_content_type="comparison_page",
                support_signal=_result_domain(row),
                source_url=_result_url(row),
                source_domain=_result_domain(row),
                impact_score=17,
                difficulty="medium",
                credits_required=12,
            )
        )

    review_signals = _extract_research_results(research_pack, "review_signals")
    if review_signals:
        actions.append(
            _make_action(
                category="review_opportunity",
                priority="medium",
                title="Strengthen review and trust signals",
                issue="Search results show review-related signals that may affect trust and AI visibility.",
                why_it_matters=(
                    "Reviews, freshness, sentiment, and external validation can influence "
                    "whether a brand is considered trustworthy enough to recommend."
                ),
                recommended_fix=(
                    "Create a review collection campaign, add review proof to key pages, "
                    "respond to recent reviews, and improve review consistency across platforms."
                ),
                suggested_content_type=None,
                support_signal=", ".join(
                    [_result_domain(row) for row in review_signals[:3] if _result_domain(row)]
                ),
                impact_score=11,
                difficulty="medium",
                credits_required=6,
                execution_type="manual_or_ai_assisted",
            )
        )

    competitors_from_research = _extract_research_results(research_pack, "competitors")
    for row in competitors_from_research[:10]:
        competitor_title = _result_query_or_title(row)
        domain = _result_domain(row)

        actions.append(
            _make_action(
                category="competitor_counter",
                priority="medium",
                title=f"Analyze competitor positioning: {_shorten_title(competitor_title)}",
                issue="Real search results surfaced a competitor or industry comparison source.",
                why_it_matters=(
                    "Competitor pages can reveal what answer engines and search systems may already trust."
                ),
                recommended_fix=(
                    "Review this competitor/source and create stronger positioning, FAQ, comparison, "
                    "or service content to close the visibility gap."
                ),
                suggested_content_type="comparison_page",
                support_signal=domain,
                source_url=_result_url(row),
                source_domain=domain,
                impact_score=10,
                difficulty="medium",
                credits_required=8,
            )
        )

    aeo_opportunities = _extract_research_results(research_pack, "aeo_opportunities")
    for row in aeo_opportunities[:3]:
        title = _result_query_or_title(row)

        actions.append(
            _make_action(
                category="trust_opportunity",
                priority="medium",
                title=f"Apply AEO opportunity: {_shorten_title(title)}",
                issue="External research surfaced AEO-related guidance or market signals.",
                why_it_matters=(
                    "AEO visibility depends on both website content and broader trust, authority, "
                    "entity clarity, and external validation."
                ),
                recommended_fix=(
                    "Review this opportunity and convert it into a practical improvement: "
                    "content updates, schema, trust proof, reviews, entity consistency, or authority campaigns."
                ),
                support_signal=_result_domain(row),
                source_url=_result_url(row),
                source_domain=_result_domain(row),
                impact_score=9,
                difficulty="medium",
                credits_required=6,
                execution_type="manual_or_ai_assisted",
            )
        )

    # ------------------------------------------------------------
    # 5. Healthy account scaling action
    # ------------------------------------------------------------

    if normalized_score >= 70 and not missing_brand_queries:
        actions.append(
            _make_action(
                category="scale_wins",
                priority="low",
                title="Scale winning content patterns",
                issue="Overall performance is healthy.",
                why_it_matters=(
                    "Once core visibility gaps are reduced, growth usually comes from "
                    "expanding winning structures into adjacent query clusters."
                ),
                recommended_fix=(
                    "Identify the best-performing pages and replicate their structure "
                    "across adjacent services, locations, FAQs, and comparison topics."
                ),
                support_signal=f"Normalized score: {normalized_score}",
                impact_score=6,
                difficulty="easy",
                credits_required=6,
            )
        )

    # Ecommerce-specific recommendations, gated on industry keywords.
    if _is_ecommerce_industry(industry):
        actions.extend(_ecommerce_actions(client_name, industry or ""))

    return _unique_actions(actions)

def clean_opportunity_title(raw_title, fallback_query=None):
    """
    Converts noisy search result titles into cleaner content opportunity titles.
    """
    title = _clean_text(raw_title) or _clean_text(fallback_query)

    if not title:
        return "Untitled content opportunity"

    # Remove common source/site suffixes
    remove_phrases = [
        "| Eatbook.sg",
        "| Time Out",
        "| Ladyironchef",
        "| Delcie's Desserts and Cakes",
        "| Singapore",
        "- Carrara",
        "- Creamier",
        "- White Rabbit",
        "Updated May 2026",
    ]

    for phrase in remove_phrases:
        title = title.replace(phrase, "")

    # Remove anything after pipe if still source-like
    if "|" in title:
        title = title.split("|")[0].strip()

    title = " ".join(title.split())
    lower_title = title.lower()

    # Convert noisy result titles into clean opportunity topics
    if "ice cream delivery" in lower_title:
        return "Best Ice Cream Delivery in Singapore"

    if "dessert box delivery" in lower_title:
        return "Best Dessert Box Delivery Services in Singapore"

    if "dessert delivery" in lower_title:
        return "Best Dessert Delivery in Singapore"

    if "ice cream catering" in lower_title:
        return "Best Ice Cream Catering in Singapore"

    if "ice cream party" in lower_title or "party package" in lower_title:
        return "Ice Cream Party Packages in Singapore"

    if "food gifts" in lower_title:
        return "Best Food Gifts in Singapore"

    if "nostalgic" in lower_title and ("snack" in lower_title or "candy" in lower_title):
        return "Nostalgic Snacks and Candy Gifts in Singapore"

    if "halal ice cream" in lower_title:
        return "Halal Ice Cream in Singapore"

    if "white rabbit ice cream" in lower_title:
        return "Where to Buy White Rabbit Ice Cream in Singapore"

    return title

def build_content_opportunities(
    recommended_actions: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Converts recommended actions into content queue opportunities.

    Only actions with linked_query + suggested_content_type become content ideas.
    Cleans noisy source titles into client-friendly content topics.
    """

    opportunities: List[Dict[str, Any]] = []

    for action in recommended_actions or []:
        linked_query = _clean_text(action.get("linked_query"))
        suggested_content_type = action.get("suggested_content_type")

        if not linked_query or not suggested_content_type:
            continue

        clean_title = clean_opportunity_title(
            raw_title=linked_query,
            fallback_query=action.get("title")
        )

        clean_content_type = suggested_content_type

        if "party package" in clean_title.lower() or "party packages" in clean_title.lower():
            clean_content_type = "service_page"

        if "where to buy" in clean_title.lower():
            clean_content_type = "faq_page"

        if clean_title.lower().startswith("best "):
            clean_content_type = "comparison_page"

        opportunities.append(
            {
                "title": clean_title,
                "target_query": clean_title,
                "content_type": clean_content_type,
                "priority": action.get("priority", "medium"),
                "source_action_title": action.get("title", ""),
                "reason": action.get("issue", ""),
                "status": "idea",
                "impact_score": _safe_int(action.get("impact_score")),
                "credits_required": _safe_int(action.get("credits_required")),
                "execution_type": action.get("execution_type", "ai_executable"),
                "source_url": action.get("source_url", ""),
                "source_domain": action.get("source_domain", ""),
            }
        )

    # Deduplicate by cleaned title + content type
    seen = set()
    deduped = []

    for item in opportunities:
        key = (
            item.get("title", "").lower().strip(),
            item.get("content_type", "").lower().strip(),
        )

        if key in seen:
            continue

        seen.add(key)
        deduped.append(item)

    deduped.sort(
        key=lambda x: (
            PRIORITY_ORDER.get(x.get("priority", "low"), 99),
            -_safe_int(x.get("impact_score")),
        )
    )

    return deduped

def group_actions_by_priority(
    recommended_actions: List[Dict[str, Any]]
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Helper for templates.
    """

    grouped = {
        "high": [],
        "medium": [],
        "low": [],
    }

    for action in recommended_actions or []:
        priority = action.get("priority", "low")
        if priority not in grouped:
            priority = "low"

        grouped[priority].append(action)

    return grouped


def summarize_action_plan(
    recommended_actions: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Summary numbers for dashboard cards.
    """

    actions = recommended_actions or []

    total_credits = sum(_safe_int(a.get("credits_required")) for a in actions)
    high_count = len([a for a in actions if a.get("priority") == "high"])
    medium_count = len([a for a in actions if a.get("priority") == "medium"])
    low_count = len([a for a in actions if a.get("priority") == "low"])
    ai_executable_count = len([
        a for a in actions
        if a.get("execution_type") == "ai_executable"
    ])

    return {
        "total_actions": len(actions),
        "high_priority": high_count,
        "medium_priority": medium_count,
        "low_priority": low_count,
        "ai_executable": ai_executable_count,
        "estimated_total_credits": total_credits,
    }


def estimate_action_plan_credits(
    recommended_actions: List[Dict[str, Any]]
) -> int:
    """
    Total estimated credits needed if user asks AI agents to execute all suggested actions.
    """

    return sum(
        _safe_int(action.get("credits_required"))
        for action in recommended_actions or []
    )
