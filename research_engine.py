import os
from dtutils import utcnow
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None


# ============================================================
# Tavily setup
# ============================================================

def get_tavily_client():
    """
    Creates Tavily client safely.
    App will not crash if API key/package is missing.
    """
    api_key = os.getenv("TAVILY_API_KEY")

    if not api_key:
        raise ValueError("Missing TAVILY_API_KEY in environment variables.")

    if TavilyClient is None:
        raise ImportError("tavily-python is not installed. Run: pip install tavily-python")

    return TavilyClient(api_key=api_key)


# ============================================================
# Helpers
# ============================================================

def clean_domain(url):
    """
    Extracts a clean domain from a URL.
    Example:
    https://www.whiterabbit.sg/shop -> whiterabbit.sg
    """
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def clean_website_for_site_search(website):
    """
    Converts website URL into a domain suitable for site: search.

    Example:
    https://whiterabbit.sg/ -> whiterabbit.sg
    """
    if not website:
        return None

    parsed = urlparse(website)

    if parsed.netloc:
        domain = parsed.netloc
    else:
        domain = website

    domain = domain.replace("https://", "")
    domain = domain.replace("http://", "")
    domain = domain.replace("www.", "")
    domain = domain.strip("/")
    domain = domain.split("/")[0]

    return domain


def normalize_tavily_results(response, query, category="general"):
    """
    Converts Tavily response into a clean standard format.
    """
    results = []

    for item in response.get("results", []):
        url = item.get("url", "")

        results.append({
            "query": query,
            "category": category,
            "title": item.get("title", ""),
            "url": url,
            "domain": clean_domain(url),
            "snippet": item.get("content", ""),
            "score": item.get("score", 0),
            "published_date": item.get("published_date"),
            "retrieved_at": utcnow().isoformat()
        })

    return results


# ============================================================
# Base Tavily search
# ============================================================

def tavily_search(query, category="general", max_results=8, search_depth="basic"):
    """
    Base Tavily search wrapper.

    search_depth:
    - basic = faster/cheaper
    - advanced = deeper/more expensive

    Returns [] on any error so a single bad query (rate limit, malformed
    site: clause, transient 500) doesn't crash the entire research pack.
    """
    if not query or not query.strip():
        return []
    try:
        client = get_tavily_client()
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            include_answer=False,
            include_raw_content=False,
        )
    except Exception as exc:
        # Caller logs the rest of the pack; we just yield no rows for
        # the failed category and keep going.
        print(f"Tavily search failed [{category}]: {type(exc).__name__}: {exc}")
        return []

    return normalize_tavily_results(response, query, category)


# ============================================================
# Research categories
# ============================================================

def search_brand_identity(business_name, website=None, location=None, max_results=8):
    """
    Checks whether search engines can identify the real business.
    Useful for brand confidence scoring.
    """
    website_domain = clean_website_for_site_search(website)

    if website_domain and location:
        query = f"{business_name} {location} official website {website_domain}"
    elif website_domain:
        query = f"{business_name} official website {website_domain}"
    elif location:
        query = f"{business_name} {location} official website"
    else:
        query = f"{business_name} official website"

    return tavily_search(query, category="brand_identity", max_results=max_results)


def search_website_signals(business_name, website, services=None, max_results=8):
    """
    Searches only within the official website.
    This checks whether the website has crawlable service/product content.
    """
    website_domain = clean_website_for_site_search(website)

    if not website_domain:
        return []

    service_text = services or ""

    query = f"site:{website_domain} {business_name} {service_text}"

    return tavily_search(query, category="website_signals", max_results=max_results)


def search_service_pages(business_name, website=None, services=None, location=None, max_results=8):
    """
    Searches for service/product pages.
    Uses site: search when both website AND service text are provided
    (Tavily rejects queries that are only site: operators with no
    search terms).
    """
    website_domain = clean_website_for_site_search(website)
    service_text = (services or "").strip()

    if website_domain and service_text:
        query = f"site:{website_domain} {service_text}"
    elif website_domain:
        # site: + a generic search term so Tavily accepts the query.
        query = f"site:{website_domain} services products"
    elif location:
        query = f"{business_name} {service_text} {location}".strip()
    else:
        query = f"{business_name} {service_text}".strip()

    if not query:
        return []

    return tavily_search(query, category="service_pages", max_results=max_results)


def search_competitors(business_name, industry, location=None, services=None, max_results=5):
    """
    Finds likely competitors across multiple discovery queries.
    Returns up to 5 results for each query.
    """
    if location:
        queries = [
            f"ice cream delivery {location}",
            f"ice cream party package {location}",
            f"ice cream catering {location}",
            f"unique food gifts {location}",
            f"nostalgic candy gifts {location}",
        ]
    else:
        queries = [
            "ice cream delivery",
            "ice cream party package",
            "ice cream catering",
            "unique food gifts",
            "nostalgic candy gifts",
        ]

    all_results = []

    for query in queries:
        all_results.extend(
            tavily_search(
                query=query,
                category="competitors",
                max_results=max_results
            )
        )

    return all_results

def search_customer_questions(industry, location=None, services=None, max_results=8):
    """
    Finds common questions customers ask before buying.
    """
    service_text = services or industry

    if location:
        query = f"common questions customers ask about {service_text} in {location}"
    else:
        query = f"common questions customers ask about {service_text}"

    return tavily_search(query, category="customer_questions", max_results=max_results)


def search_comparison_queries(industry, location=None, services=None, max_results=5):
    """
    Finds comparison/listicle pages.
    Returns up to 5 results for each query.
    """
    if location:
        queries = [
            f"best ice cream delivery {location}",
            f"best dessert delivery {location}",
            f"best ice cream catering {location}",
            f"best party snacks {location}",
            f"best food gifts {location}",
        ]
    else:
        queries = [
            "best ice cream delivery",
            "best dessert delivery",
            "best ice cream catering",
            "best party snacks",
            "best food gifts",
        ]

    all_results = []

    for query in queries:
        all_results.extend(
            tavily_search(
                query=query,
                category="comparison_queries",
                max_results=max_results
            )
        )

    return all_results

def search_review_signals(business_name, location=None, max_results=8):
    """
    Finds reviews, testimonials, social proof, and reputation signals.
    """
    if location:
        query = f"{business_name} reviews {location}"
    else:
        query = f"{business_name} reviews"

    return tavily_search(query, category="review_signals", max_results=max_results)


def search_social_signals(business_name, location=None, max_results=8):
    """
    Finds social media signals.
    """
    if location:
        query = f"{business_name} {location} Instagram Facebook TikTok"
    else:
        query = f"{business_name} Instagram Facebook TikTok"

    return tavily_search(query, category="social_signals", max_results=max_results)


def search_aeo_opportunities(industry, location=None, services=None, max_results=8):
    """
    Finds AI-search/AEO opportunities around the business category.
    """
    service_text = services or industry

    if location:
        query = f"AI search optimization opportunities for {service_text} businesses in {location}"
    else:
        query = f"AI search optimization opportunities for {service_text} businesses"

    return tavily_search(query, category="aeo_opportunities", max_results=max_results)


def search_ai_discovery_prompts(industry, location=None, services=None, max_results=5):
    """
    Simulates AI-style buyer discovery prompts.
    Returns up to 5 results for each query.
    """
    if location:
        queries = [
            f"where to buy White Rabbit ice cream in {location}",
            f"recommended ice cream delivery in {location}",
            f"best ice cream for birthday party in {location}",
            f"unique food gifts in {location}",
            f"halal ice cream in {location}",
        ]
    else:
        queries = [
            "where to buy White Rabbit ice cream",
            "recommended ice cream delivery",
            "best ice cream for birthday party",
            "unique food gifts",
            "halal ice cream",
        ]

    all_results = []

    for query in queries:
        all_results.extend(
            tavily_search(
                query=query,
                category="ai_discovery_prompts",
                max_results=max_results
            )
        )

    return all_results

# ============================================================
# Main research function
# ============================================================

def run_research_pack(
    business_name,
    industry,
    location=None,
    services=None,
    website=None,
    max_results_each=6
):
    """
    Main function we call after an audit.
    Returns a grouped research pack.

    Example:

    data = run_research_pack(
        business_name="White Rabbit Singapore",
        industry="ice cream and confectionery e-commerce",
        location="Singapore",
        services="White Rabbit ice cream, milk candy, party packages, merchandise, gift bundles",
        website="https://whiterabbit.sg"
    )
    """

    research_pack = {
        "business_name": business_name,
        "website": website,
        "industry": industry,
        "location": location,
        "services": services,
        "generated_at": utcnow().isoformat(),
        "results": {
            "brand_identity": [],
            "website_signals": [],
            "service_pages": [],
            "competitors": [],
            "customer_questions": [],
            "comparison_queries": [],
            "review_signals": [],
            "social_signals": [],
            "aeo_opportunities": [],
            "ai_discovery_prompts": [],
        }
    }

    # 1. Can search engines identify this business?
    research_pack["results"]["brand_identity"] = search_brand_identity(
        business_name=business_name,
        website=website,
        location=location,
        max_results=max_results_each
    )

    # 2. What does the official website expose to search engines?
    if website:
        research_pack["results"]["website_signals"] = search_website_signals(
            business_name=business_name,
            website=website,
            services=services,
            max_results=max_results_each
        )

    # 3. Can we find service/product pages?
    research_pack["results"]["service_pages"] = search_service_pages(
        business_name=business_name,
        website=website,
        services=services,
        location=location,
        max_results=max_results_each
    )

    # 4. Who are the likely competitors?
    research_pack["results"]["competitors"] = search_competitors(
        business_name=business_name,
        industry=industry,
        location=location,
        services=services,
        max_results=max_results_each
    )

    # 5. What questions do customers ask?
    research_pack["results"]["customer_questions"] = search_customer_questions(
        industry=industry,
        location=location,
        services=services,
        max_results=max_results_each
    )

    # 6. What comparison/listing pages exist?
    research_pack["results"]["comparison_queries"] = search_comparison_queries(
        industry=industry,
        location=location,
        services=services,
        max_results=max_results_each
    )

    # 7. What review signals exist?
    research_pack["results"]["review_signals"] = search_review_signals(
        business_name=business_name,
        location=location,
        max_results=max_results_each
    )

    # 8. What social signals exist?
    research_pack["results"]["social_signals"] = search_social_signals(
        business_name=business_name,
        location=location,
        max_results=max_results_each
    )

    # 9. What AEO opportunities exist?
    research_pack["results"]["aeo_opportunities"] = search_aeo_opportunities(
        industry=industry,
        location=location,
        services=services,
        max_results=max_results_each
    )

    # 10. What AI-style discovery prompts surface this category?
    research_pack["results"]["ai_discovery_prompts"] = search_ai_discovery_prompts(
        industry=industry,
        location=location,
        services=services,
        max_results=max_results_each
    )

    return research_pack

def summarize_research_pack(research_pack):
    """
    Creates lightweight dashboard summaries
    from raw Tavily research.
    """

    results = research_pack.get("results", {})

    competitors = results.get("competitors", [])
    comparison_queries = results.get("comparison_queries", [])
    customer_questions = results.get("customer_questions", [])
    review_signals = results.get("review_signals", [])
    aeo_opportunities = results.get("aeo_opportunities", [])

    top_competitors = []

    seen_domains = set()

    for row in competitors:
        domain = row.get("domain")

        if not domain or domain in seen_domains:
            continue

        seen_domains.add(domain)

        top_competitors.append({
            "title": row.get("title"),
            "domain": domain,
            "url": row.get("url"),
        })

        if len(top_competitors) >= 5:
            break

    return {
        "competitor_count": len(competitors),
        "comparison_query_count": len(comparison_queries),
        "customer_question_count": len(customer_questions),
        "review_signal_count": len(review_signals),
        "aeo_opportunity_count": len(aeo_opportunities),

        "top_competitors": top_competitors,

        "top_customer_questions": [
            x.get("title")
            for x in customer_questions[:5]
        ],

        "top_comparison_queries": [
            x.get("title")
            for x in comparison_queries[:5]
        ],
    }