from collections import Counter


def classify_result(result, official_website=None):
    """
    Dynamically classifies each Tavily result into useful audit categories.
    No hardcoded competitor domains.
    """
    domain = result.get("domain", "").lower()
    title = result.get("title", "").lower()
    snippet = result.get("snippet", "").lower()
    category = result.get("category", "").lower()

    official_domain = ""
    if official_website:
        official_domain = (
            official_website
            .replace("https://", "")
            .replace("http://", "")
            .replace("www.", "")
            .strip("/")
            .lower()
        )

    # 1. Own official website
    if official_domain and official_domain in domain:
        return "official_site"

    # 2. Social platforms
    social_domains = [
        "instagram.com",
        "facebook.com",
        "tiktok.com",
        "linkedin.com",
        "youtube.com",
    ]

    if any(social in domain for social in social_domains):
        return "owned_social"

    # 3. Marketplaces / retailers / delivery platforms
    marketplace_domains = [
        "shopee.",
        "lazada.",
        "fairprice.",
        "grab.",
        "redmart.",
        "amazon.",
        "qoo10.",
    ]

    if any(marketplace in domain for marketplace in marketplace_domains):
        return "marketplace_or_retailer"

    # 4. Conflicting entity detection
    # Example: "The White Rabbit" restaurant is not the White Rabbit ice cream/e-commerce brand.
    conflicting_keywords = [
        "restaurant",
        "meal",
        "dining",
        "wine",
        "bar",
        "brunch",
        "sethlui",
        "yelp",
    ]

    if "the white rabbit" in title and any(word in snippet for word in conflicting_keywords):
        return "conflicting_entity"

    if "restaurant" in snippet and "white rabbit" in title:
        return "conflicting_entity"

    # 5. Comparison / listicle / directory sources
    comparison_keywords = [
        "best",
        "top",
        "guide",
        "list",
        "review",
        "comparison",
        "where to buy",
        "places",
        "spots",
        "directory",
        "near me",
    ]

    comparison_domains = [
        "timeout.",
        "eatbook.",
        "ladyironchef.",
        "thesmartlocal.",
        "foodline.",
        "havehalalwilltravel.",
        "honeykidsasia.",
        "sassymamasg.",
        "yelp.",
        "reddit.",
        "etsy.",
        "tripadvisor.",
        "blog.",
    ]

    if (
        any(word in title for word in comparison_keywords)
        or any(source in domain for source in comparison_domains)
    ):
        return "comparison_source"

    # 6. Review source
    if "review" in title or "reviews" in snippet:
        return "review_source"

    # 7. Dynamic competitor logic
    # If a result appears for competitor / comparison / AI discovery queries
    # and it is not the official site, not social, not marketplace, and not a listicle,
    # it may be a competing business or category player.
    competitor_categories = [
        "competitors",
        "comparison_queries",
        "ai_discovery_prompts",
    ]

    business_page_signals = [
        "shop",
        "delivery",
        "catering",
        "package",
        "order",
        "menu",
        "products",
        "services",
        "contact",
        "islandwide",
        "corporate",
        "party",
        "booking",
        "quote",
        "store",
        "cart",
    ]

    if category in competitor_categories:
        if any(signal in title or signal in snippet for signal in business_page_signals):
            return "competitor"

        if domain and not any(source in domain for source in comparison_domains):
            return "possible_competitor"

    # 8. Default
    return "third_party_source"


def score_audit(research_pack):
    """
    Creates simple audit scores from the research pack.
    """
    website = research_pack.get("website")
    results = research_pack.get("results", {})

    all_items = []

    for category, items in results.items():
        for item in items:
            item["_classification"] = classify_result(
                item,
                official_website=website
            )
            all_items.append(item)

    classifications = Counter(
        [item["_classification"] for item in all_items]
    )

    official_count = classifications.get("official_site", 0)
    social_count = classifications.get("owned_social", 0)
    conflict_count = classifications.get("conflicting_entity", 0)

    website_signals_count = len(results.get("website_signals", []))
    service_pages_count = len(results.get("service_pages", []))

    faq_found = any(
        "faq" in item.get("title", "").lower()
        or "faq" in item.get("url", "").lower()
        for item in results.get("website_signals", [])
        + results.get("customer_questions", [])
    )

    # Brand identity score
    brand_identity_score = 40

    if official_count > 0:
        brand_identity_score += 30

    if social_count > 0:
        brand_identity_score += 20

    if conflict_count > 0:
        brand_identity_score -= 15

    # Website clarity score
    website_score = 35

    if website_signals_count > 0:
        website_score += 20

    if service_pages_count > 0:
        website_score += 20

    if faq_found:
        website_score += 10

    # Do not allow website clarity to look perfect unless we later check
    # schema, local business details, reviews/testimonials, and structured product data.
    website_score = min(90, website_score)

    # Review signal clarity score
    review_score = 60

    if conflict_count > 0:
        review_score -= 35

    if len(results.get("review_signals", [])) == 0:
        review_score -= 20

    # Competitor visibility score
    competitor_score = 35
    competitor_items = results.get("competitors", [])

    non_official_competitors = [
        item for item in competitor_items
        if classify_result(item, official_website=website) != "official_site"
    ]

    if len(non_official_competitors) >= 5:
        competitor_score += 30
    elif len(non_official_competitors) >= 3:
        competitor_score += 20
    elif len(non_official_competitors) >= 1:
        competitor_score += 10

    competitor_score = min(80, competitor_score)

    # Answer readiness score
    answer_readiness_score = 45

    if faq_found:
        answer_readiness_score += 20

    if service_pages_count > 0:
        answer_readiness_score += 15

    # Cap this until we check if answers are structured with FAQ schema
    # and written in direct question-answer format.
    answer_readiness_score = min(85, answer_readiness_score)

    scores = {
        "brand_identity": max(0, min(100, brand_identity_score)),
        "website_clarity": max(0, min(90, website_score)),
        "review_signal_clarity": max(0, min(100, review_score)),
        "competitor_visibility": max(0, min(80, competitor_score)),
        "answer_readiness": max(0, min(85, answer_readiness_score)),
    }

    scores["overall"] = round(sum(scores.values()) / len(scores))

    return scores, classifications, all_items


def build_query_visibility(research_pack, limit=5):
    """
    Shows which competitors/sources appear for important discovery queries.

    This is called query_visibility instead of competitor_visibility because
    results may include competitors, marketplaces, comparison articles,
    social pages, directories, and other third-party sources.

    Returns max 5 results per query/category.
    """
    website = research_pack.get("website")
    results = research_pack.get("results", {})

    query_sections = []

    categories_to_include = [
        "competitors",
        "comparison_queries",
        "ai_discovery_prompts",
    ]

    for category in categories_to_include:
        items = results.get(category, [])

        if not items:
            continue

        grouped_by_query = {}

        for item in items:
            query = item.get("query", "")
            classification = classify_result(
                item,
                official_website=website
            )

            # Do not show own official website as competitor/source visibility.
            if classification == "official_site":
                continue

            grouped_by_query.setdefault(query, [])

            grouped_by_query[query].append({
                "title": item.get("title", ""),
                "domain": item.get("domain", ""),
                "url": item.get("url", ""),
                "snippet": item.get("snippet", ""),
                "score": item.get("score", 0),
                "classification": classification,
            })

        for query, query_items in grouped_by_query.items():
            sorted_items = sorted(
                query_items,
                key=lambda x: x.get("score", 0),
                reverse=True
            )

            query_sections.append({
                "category": category,
                "query": query,
                "top_results": sorted_items[:limit]
            })

    return query_sections


def generate_audit_report(research_pack):
    """
    Converts raw Tavily research into a readable AEO audit report.
    """
    scores, classifications, all_items = score_audit(research_pack)

    business_name = research_pack.get("business_name")
    website = research_pack.get("website")
    industry = research_pack.get("industry")
    location = research_pack.get("location")
    services = research_pack.get("services")

    results = research_pack.get("results", {})
    query_visibility = build_query_visibility(research_pack, limit=5)

    report = {
        "business_name": business_name,
        "website": website,
        "industry": industry,
        "location": location,
        "services": services,
        "aeo_score": scores["overall"],
        "scores": scores,
        "summary": "",
        "key_findings": [],
        "risks": [],
        "recommendations": [],
        "content_opportunities": [],
        "classified_result_counts": dict(classifications),

        # New, better name
        "query_visibility": query_visibility,

        # Keep this old key for now so your existing UI does not break.
        # Later, you can rename the template variable from competitor_visibility to query_visibility.
        "competitor_visibility": query_visibility,
    }

    # Summary status
    if scores["overall"] >= 80:
        status = "Strong"
    elif scores["overall"] >= 60:
        status = "Moderate"
    elif scores["overall"] >= 40:
        status = "Weak"
    else:
        status = "Poor"

    report["summary"] = (
        f"{business_name} has a {status.lower()} AEO visibility profile. "
        f"The business has identifiable website and social signals, but should improve "
        f"category-level discovery, comparison visibility, and review signal clarity."
    )

    # Key findings
    if len(results.get("website_signals", [])) > 0:
        report["key_findings"].append(
            "The official website is discoverable and contains useful product/service information."
        )

    if len(results.get("service_pages", [])) > 0:
        report["key_findings"].append(
            "Product and package pages are visible, which supports buyer-intent search queries."
        )

    if classifications.get("owned_social", 0) > 0:
        report["key_findings"].append(
            "Social profiles appear strongly in branded search results."
        )

    if classifications.get("competitor", 0) > 0:
        report["key_findings"].append(
            "Several competing brands or category players appear for relevant discovery queries."
        )

    if classifications.get("comparison_source", 0) > 0:
        report["key_findings"].append(
            "Third-party comparison and listicle sources are influencing visibility for category searches."
        )

    # Risks
    if classifications.get("conflicting_entity", 0) > 0:
        report["risks"].append(
            "Search results show possible entity confusion with another business using a similar name."
        )

    if scores["competitor_visibility"] < 60:
        report["risks"].append(
            "Competitor and comparison visibility is still weak. The brand may not appear strongly for broader category searches."
        )

    if scores["review_signal_clarity"] < 50:
        report["risks"].append(
            "Review signals are unclear or mixed with unrelated results. This may reduce AI confidence."
        )

    if classifications.get("marketplace_or_retailer", 0) > 0:
        report["risks"].append(
            "Marketplace and retailer pages may be more visible than the brand’s own website for some product searches."
        )

    # Recommendations
    report["recommendations"] = [
        "Create stronger category landing pages for non-branded discovery searches.",
        "Improve homepage metadata so the official website appears more clearly for branded queries.",
        "Add structured FAQ content for common customer questions.",
        "Add Organization, Product, FAQ, and LocalBusiness schema where relevant.",
        "Build third-party citations on review, directory, and comparison websites.",
        "Create content that answers high-intent customer questions directly.",
    ]

    # Content opportunities
    # For now this is semi-static. Later we can generate this dynamically from Tavily customer questions.
    report["content_opportunities"] = [
        {
            "title": "Where to Buy White Rabbit Ice Cream in Singapore",
            "type": "FAQ / Blog",
            "priority": "High"
        },
        {
            "title": "Is White Rabbit Ice Cream Halal?",
            "type": "FAQ",
            "priority": "High"
        },
        {
            "title": "White Rabbit Ice Cream Party Packages in Singapore",
            "type": "Service Page",
            "priority": "High"
        },
        {
            "title": "Unique Food Gifts and Nostalgic Candy Gifts in Singapore",
            "type": "Blog / Collection Page",
            "priority": "Medium"
        },
        {
            "title": "White Rabbit Ice Cream Flavours, Calories and Ingredients",
            "type": "FAQ / Product Guide",
            "priority": "Medium"
        },
    ]

    return report