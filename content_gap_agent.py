import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


QUESTION_WORDS = [
    "how",
    "what",
    "why",
    "when",
    "where",
    "who",
    "can",
    "does"
]


def extract_site_content(url):

    try:
        response = requests.get(url, verify=False, timeout=10)
        html = response.text
    except:
        return ""

    soup = BeautifulSoup(html, "html.parser")

    text = soup.get_text(" ", strip=True)

    return text.lower()


def detect_question_queries(queries):

    question_queries = []

    for q in queries:

        first_word = q.split()[0]

        if first_word.lower() in QUESTION_WORDS:
            question_queries.append(q)

    return question_queries


def detect_content_gaps(queries, site_text):

    gaps = []

    for q in queries:

        keyword = q.lower()

        if keyword not in site_text:
            gaps.append(q)

    return gaps


if __name__ == "__main__":

    sample_queries = [
        "how much does interior design cost singapore",
        "what is the best interior designer singapore",
        "interior designer singapore services"
    ]

    site_text = extract_site_content("https://example.com")

    questions = detect_question_queries(sample_queries)

    gaps = detect_content_gaps(questions, site_text)

    print("\nMissing Question Topics:")

    for g in gaps:
        print("-", g)