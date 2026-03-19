import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import urllib3
from ddgs import DDGS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

QUESTION_STARTERS = (
    "what", "how", "why", "when", "where", "who", "can", "does", "is", "are", "should"
)

FALLBACK_TEMPLATES = [
    "What is {topic}?",
    "How does {topic} work?",
    "What are the benefits of {topic}?",
    "What are the risks of {topic}?",
    "How much does {topic} cost?",
    "Is {topic} worth it?",
    "Who is {topic} suitable for?",
    "How do I choose {topic}?",
]

MAX_INTERNAL_PAGES = 8


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


def normalize_text(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s\?\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_into_candidate_questions(text):
    chunks = re.split(r"[.\n\r•\|\-]+", text)
    questions = []

    for chunk in chunks:
        cleaned = chunk.strip()
        if not cleaned:
            continue

        lowered = cleaned.lower().strip()

        if lowered.startswith(QUESTION_STARTERS):
            if not lowered.endswith("?"):
                cleaned = cleaned.rstrip() + "?"
            questions.append(cleaned)

    return questions


def dedupe_keep_order(items):
    seen = set()
    result = []

    for item in items:
        key = normalize_text(item)
        if key not in seen:
            seen.add(key)
            result.append(item.strip())

    return result


def discover_questions(topic, max_questions=20):
    discovered = []

    search_queries = [
        topic,
        f"{topic} questions",
        f"{topic} reddit",
        f"how {topic}",
        f"what is {topic}",
        f"{topic} singapore",
    ]

    try:
        with DDGS() as ddgs:
            for q in search_queries:
                results = ddgs.text(q, max_results=8)

                for r in results:
                    title = r.get("title", "")
                    body = r.get("body", "")

                    discovered.extend(split_into_candidate_questions(title))
                    discovered.extend(split_into_candidate_questions(body))

                    text_blob = f"{title}. {body}"
                    lowered = text_blob.lower()
                    topic_clean = topic.strip()

                    if f"cost of {topic_clean.lower()}" in lowered:
                        discovered.append(f"How much does {topic_clean} cost?")

                    if f"benefits of {topic_clean.lower()}" in lowered:
                        discovered.append(f"What are the benefits of {topic_clean}?")

                    if "risk" in lowered and topic_clean.lower() in lowered:
                        discovered.append(f"What are the risks of {topic_clean}?")

    except Exception:
        pass

    if len(discovered) < 8:
        discovered.extend([q.format(topic=topic) for q in FALLBACK_TEMPLATES])

    discovered = dedupe_keep_order(discovered)

    filtered = []
    for q in discovered:
        q_norm = normalize_text(q)
        if q_norm.startswith(QUESTION_STARTERS):
            filtered.append(q.rstrip("?").strip() + "?")

    filtered = dedupe_keep_order(filtered)

    return filtered[:max_questions]


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


def score_link(url):
    url_lower = url.lower()
    score = 0

    if "faq" in url_lower or "faqs" in url_lower:
        score += 10
    if "blog" in url_lower or "article" in url_lower or "news" in url_lower:
        score += 8
    if "service" in url_lower or "services" in url_lower or "solution" in url_lower:
        score += 8
    if "guide" in url_lower or "how-to" in url_lower:
        score += 7
    if "about" in url_lower:
        score += 5
    if "contact" in url_lower:
        score += 3

    low_value_patterns = [
        "cart", "checkout", "login", "account",
        "policy", "privacy", "terms", "search",
        "collections", "product", "products"
    ]

    for pattern in low_value_patterns:
        if pattern in url_lower:
            score -= 5

    return score


def get_page_text(html):
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ", strip=True).lower()


def fetch_site_text_multi_page(url, max_pages=MAX_INTERNAL_PAGES, debug=False):
    homepage_html = fetch_html(url)
    if not homepage_html:
        return "", []

    links = extract_internal_links(url, homepage_html)
    links = sorted(links, key=score_link, reverse=True)

    selected_pages = [url] + links[:max_pages]

    seen = set()
    unique_pages = []
    for page in selected_pages:
        if page not in seen:
            unique_pages.append(page)
            seen.add(page)

    combined_text_parts = []

    for page in unique_pages:
        html = fetch_html(page)
        page_text = get_page_text(html)

        if page_text:
            combined_text_parts.append(page_text)

        if debug:
            print("QUESTION AUDIT PAGE:", page, "| score:", score_link(page))

    combined_text = "\n".join(combined_text_parts)

    return combined_text, unique_pages


def question_keywords(question):
    stop_words = {
        "what", "how", "does", "do", "is", "are", "the", "a", "an",
        "of", "in", "to", "for", "it", "its", "and", "or", "worth",
        "who", "when", "where", "why", "can", "should"
    }
    words = normalize_text(question).replace("?", "").split()
    return [w for w in words if w not in stop_words]


def score_question_coverage(question, site_text):
    keywords = question_keywords(question)

    if not keywords:
        return 0.0

    hits = sum(1 for kw in keywords if kw in site_text)
    score = hits / len(keywords)

    return round(score, 2)


def classify_coverage(score):
    if score >= 0.75:
        return "Answered"
    elif score >= 0.4:
        return "Weak"
    return "Missing"


def run_question_coverage_audit(url, topic, max_questions=20, debug=False):
    site_text, pages_used = fetch_site_text_multi_page(url, debug=debug)
    questions = discover_questions(topic, max_questions=max_questions)

    results = []

    for q in questions:
        score = score_question_coverage(q, site_text)
        status = classify_coverage(score)

        results.append({
            "question": q,
            "status": status,
            "score": score
        })

    return {
        "topic": topic,
        "pages_used": pages_used,
        "results": results
    }


if __name__ == "__main__":
    audit = run_question_coverage_audit(
        "https://www.chungwillwritingservices.sg/",
        "will writing singapore",
        max_questions=15,
        debug=True
    )

    print("\nPAGES USED:")
    for page in audit["pages_used"]:
        print("-", page)

    print("\nQUESTION COVERAGE:")
    for row in audit["results"]:
        print(row)