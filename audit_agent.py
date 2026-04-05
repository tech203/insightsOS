import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

QUESTION_WORDS = ("what", "how", "why", "when", "where", "can", "is", "are")
MAX_PAGES_TO_CHECK = 10


def fetch_html(url):
    try:
        response = requests.get(
            url,
            verify=False,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        return response.text
    except Exception:
        return ""


def score_link(url):
    url_lower = url.lower()
    score = 0

    # High-value AEO pages
    if "faq" in url_lower or "faqs" in url_lower:
        score += 10
    if "blog" in url_lower or "article" in url_lower or "news" in url_lower:
        score += 8
    if "service" in url_lower or "services" in url_lower or "solution" in url_lower:
        score += 8
    if "about" in url_lower:
        score += 5
    if "contact" in url_lower:
        score += 4
    if "guide" in url_lower or "how-to" in url_lower:
        score += 7
    if "menu" in url_lower:
        score += 3

    # Lower-value / noisy pages
    low_value_patterns = [
        "cart", "checkout", "login", "account",
        "policy", "privacy", "terms", "search",
        "collections", "product", "products",
        "wishlist", "track-order"
    ]

    for pattern in low_value_patterns:
        if pattern in url_lower:
            score -= 5

    return score


def extract_internal_links(base_url, html):
    soup = BeautifulSoup(html, "html.parser")
    links = set()
    base_domain = urlparse(base_url).netloc

    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue

        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)

        if parsed.scheme not in ("http", "https"):
            continue

        if parsed.netloc != base_domain:
            continue

        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            clean_url += f"?{parsed.query}"

        links.add(clean_url)

    return list(links)


def get_title_and_headings(html):
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""

    headings = [
        tag.get_text(" ", strip=True).lower()
        for tag in soup.find_all(["h1", "h2", "h3"])
    ]

    return title, headings


def is_faq_page(url, html):
    url_lower = url.lower()
    title, headings = get_title_and_headings(html)

    # Strong signal: URL
    if "faq" in url_lower or "faqs" in url_lower:
        return True

    # Strong signal: title
    if "faq" in title or "frequently asked questions" in title:
        return True

    # Strong signal: headings
    for h in headings:
        if "faq" in h or "frequently asked questions" in h:
            return True

    # Strong signal: FAQ schema
    if "FAQPage" in html:
        return True

    # Fallback: page has several question-like headings
    question_count = 0
    for h in headings:
        if h.startswith(QUESTION_WORDS):
            question_count += 1

    if question_count >= 3:
        return True

    return False


def is_blog_page(url, html):
    url_lower = url.lower()
    title, headings = get_title_and_headings(html)
    combined = " ".join(headings)

    if "blog" in url_lower or "article" in url_lower or "news" in url_lower:
        return True

    blog_signals = ["blog", "article", "insights", "news", "journal"]
    for signal in blog_signals:
        if signal in title or signal in combined:
            return True

    if "Article" in html:
        return True

    return False


def is_service_page(url, html):
    url_lower = url.lower()
    title, headings = get_title_and_headings(html)
    combined = " ".join(headings)

    if "service" in url_lower or "services" in url_lower or "solution" in url_lower:
        return True

    service_signals = [
        "our services",
        "what we do",
        "solutions",
        "book now",
        "enquiry",
        "contact us"
    ]

    for signal in service_signals:
        if signal in title or signal in combined:
            return True

    if "Service" in html:
        return True

    return False


def classify_page(url, html=""):
    if is_faq_page(url, html):
        return "faq"

    if is_blog_page(url, html):
        return "blog"

    if is_service_page(url, html):
        return "service"

    return "other"


def detect_question_headings(html):
    soup = BeautifulSoup(html, "html.parser")
    count = 0

    for tag in soup.find_all(["h1", "h2", "h3"]):
        text = tag.get_text(" ", strip=True).lower()

        if text.startswith(QUESTION_WORDS):
            count += 1

    return count


def detect_schema(html):
    soup = BeautifulSoup(html, "html.parser")
    found = set()

    for script in soup.find_all("script", type="application/ld+json"):
        text = script.get_text()

        if "Organization" in text:
            found.add("Organization")
        if "FAQPage" in text:
            found.add("FAQPage")
        if "Article" in text:
            found.add("Article")
        if "LocalBusiness" in text:
            found.add("LocalBusiness")
        if "Service" in text:
            found.add("Service")
        if "Product" in text:
            found.add("Product")

    return sorted(found)


def deduplicate_preserve_order(items):
    seen = set()
    result = []

    for item in items:
        if item not in seen:
            result.append(item)
            seen.add(item)

    return result


def audit_website(website, debug=False):
    homepage_html = fetch_html(website)
    links = extract_internal_links(website, homepage_html)

    # Prioritize best links first
    links = sorted(links, key=score_link, reverse=True)

    if debug:
        print("\nTOP LINKS SELECTED FOR AUDIT:")
        for link in links[:MAX_PAGES_TO_CHECK]:
            print(link, "| score:", score_link(link))

    pages_to_check = [website] + links[:MAX_PAGES_TO_CHECK]
    pages_to_check = deduplicate_preserve_order(pages_to_check)

    service_pages = 0
    blog_pages = 0
    faq_pages = 0
    question_headings = 0
    all_schema = set()

    for page in pages_to_check:
        html = fetch_html(page)
        page_type = classify_page(page, html)

        if debug:
            print("PAGE:", page, "| TYPE:", page_type)

        if page_type == "service":
            service_pages += 1
        elif page_type == "blog":
            blog_pages += 1
        elif page_type == "faq":
            faq_pages += 1

        question_headings += detect_question_headings(html)
        all_schema.update(detect_schema(html))

    # Content scoring breakdown
    service_score = min(service_pages * 2, 10)
    blog_score = min(blog_pages * 2, 10)
    faq_score = min(faq_pages * 3, 6)
    question_score = min(question_headings, 4)

    content_score = min(service_score + blog_score + faq_score + question_score, 30)

    schema_score = 0
    if "Organization" in all_schema:
        schema_score += 3
    if "FAQPage" in all_schema:
        schema_score += 5
    if "Article" in all_schema:
        schema_score += 3
    if "LocalBusiness" in all_schema:
        schema_score += 4

    schema_score = min(schema_score, 15)

    return {
        "pages_checked": len(pages_to_check),
        "service_pages": service_pages,
        "blog_pages": blog_pages,
        "faq_pages": faq_pages,
        "question_headings": question_headings,
        "schema_types": sorted(all_schema),

        "content_score": float(content_score),
        "schema_score": float(schema_score),

        # new fields expected by audit_runner
        "entity_score": float(min(
            10,
            (2 if "Organization" in all_schema else 0) +
            (2 if "LocalBusiness" in all_schema else 0) +
            (2 if service_pages > 0 else 0) +
            (2 if faq_pages > 0 else 0) +
            (2 if question_headings >= 3 else 0)
        )),
        "technical_score": float(min(
            10,
            4 +
            (2 if pages_to_check else 0) +
            (2 if homepage_html else 0) +
            (2 if len(links) >= 3 else 0)
        )),

        "site_findings": {
            "pages_checked": len(pages_to_check),
            "service_pages": service_pages,
            "blog_pages": blog_pages,
            "faq_pages": faq_pages,
            "question_headings": question_headings,
            "schema_types": sorted(all_schema),
            "content_score_breakdown": {
                "service_score": service_score,
                "blog_score": blog_score,
                "faq_score": faq_score,
                "question_score": question_score,
            },
            "technical_issues": [],
            "content_gaps": [],
            "entity_gaps": [],
            "schema_gaps": [],
            "notes": [],
        },
    }

if __name__ == "__main__":
    print("\n=== TEST 1: Keong Saik Bakery FAQ page ===")
    website = "https://www.keongsaikbakery.com/pages/faqs"
    result = audit_website(website, debug=True)
    print(result)

    print("\n=== TEST 2: Keong Saik Bakery homepage ===")
    result = audit_website("https://www.keongsaikbakery.com/", debug=True)
    print(result)

    print("\n=== TEST 3: LOOP ===")
    result = audit_website("https://loop.com.sg/", debug=True)
    print(result)