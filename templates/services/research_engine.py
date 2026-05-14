import os
from dtutils import utcnow
from urllib.parse import urlparse

try:
    from tavily import TavilyClient
except ImportError:
    TavilyClient = None


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


def clean_domain(url):
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


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


def tavily_search(query, category="general", max_results=8, search_depth="basic"):
    """
    Base Tavily search wrapper.

    search_depth:
    - basic = faster/cheaper
    - advanced = deeper/more expensive
    """
    client = get_tavily_client()

    response = client.search(
        query=query,
        max_results=max_results,
        search_depth=search_depth,
        include_answer=False,
        include_raw_content=False,
    )

    return normalize_tavily_results(response, query, category)


def search_competitors(business_name, industry, location, max_results=8):
    query = f"{industry} companies in {location} similar to {business_name}"
    return tavily_search(query, category="competitors", max_results=max_results)


def search_customer_questions(industry, location=None, max_results=8):
    if location:
        query = f"common questions customers ask about {industry} in {location}"
    else:
        query = f"common questions customers ask about {industry}"

    return tavily_search(query, category="customer_questions", max_results=max_results)


def search_comparison_queries(industry, location=None, max_results=8):
    if location:
        query = f"best {industry} providers in {location} comparison"
    else:
        query = f"best {industry} providers comparison"

    return tavily_search(query, category="comparison_queries", max_results=max_results)


def search_review_signals(business_name, location=None, max_results=8):
    if location:
        query = f"{business_name} reviews {location}"
    else:
        query = f"{business_name} reviews"

    return tavily_search(query, category="review_signals", max_results=max_results)


def search_aeo_opportunities(industry, location=None, services=None, max_results=8):
    service_text = services or industry

    if location:
        query = f"AI search optimization opportunities for {service_text} businesses in {location}"
    else:
        query = f"AI search optimization opportunities for {service_text} businesses"

    return tavily_search(query, category="aeo_opportunities", max_results=max_results)


def run_research_pack(
    business_name,
    industry,
    location=None,
    services=None,
    max_results_each=6
):
    """
    Main function we call after an audit.
    Returns a grouped research pack.
    """
    research_pack = {
        "business_name": business_name,
        "industry": industry,
        "location": location,
        "services": services,
        "generated_at": utcnow().isoformat(),
        "results": {
            "competitors": [],
            "customer_questions": [],
            "comparison_queries": [],
            "review_signals": [],
            "aeo_opportunities": [],
        }
    }

    research_pack["results"]["competitors"] = search_competitors(
        business_name, industry, location, max_results=max_results_each
    )

    research_pack["results"]["customer_questions"] = search_customer_questions(
        industry, location, max_results=max_results_each
    )

    research_pack["results"]["comparison_queries"] = search_comparison_queries(
        industry, location, max_results=max_results_each
    )

    research_pack["results"]["review_signals"] = search_review_signals(
        business_name, location, max_results=max_results_each
    )

    research_pack["results"]["aeo_opportunities"] = search_aeo_opportunities(
        industry, location, services, max_results=max_results_each
    )

    return research_pack