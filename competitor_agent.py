from ddgs import DDGS
from urllib.parse import urlparse
from collections import Counter

IGNORE_DOMAINS = {
    "google.com", "www.google.com",
    "facebook.com", "www.facebook.com",
    "instagram.com", "www.instagram.com",
    "youtube.com", "www.youtube.com",
    "linkedin.com", "www.linkedin.com"
}

DIRECTORY_KEYWORDS = [
    "best", "top", "directory", "list", "finestservices",
    "singaporebrand", "bestinsingapore", "shopinsg"
]

MEDIA_KEYWORDS = [
    "tatler", "timeout", "honeycombers", "asiaone",
    "yelp", "tripadvisor", "sethlui", "ladyironchef",
    "news", "magazine", "media", "guide", "blog"
]

SPAM_KEYWORDS = [
    "topbloghub", "newsnblogs", "blogspot", "wordpress",
    "tumblr", "weebly", "medium"
]


def clean_domain(url):
    domain = urlparse(url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def classify_domain(domain):
    if domain in IGNORE_DOMAINS:
        return "ignore"

    for word in SPAM_KEYWORDS:
        if word in domain:
            return "spam"

    for word in DIRECTORY_KEYWORDS:
        if word in domain:
            return "directory"

    for word in MEDIA_KEYWORDS:
        if word in domain:
            return "media"

    return "direct"


def discover_competitors(queries):
    direct_domains = []
    directory_domains = []
    media_domains = []
    spam_domains = []
    all_domains = []

    with DDGS() as ddgs:
        for q in queries:
            results = ddgs.text(q, max_results=5)

            for r in results:
                url = r.get("href")
                if not url:
                    continue

                domain = clean_domain(url)
                if not domain:
                    continue

                category = classify_domain(domain)

                if category == "ignore":
                    continue

                all_domains.append(domain)

                if category == "direct":
                    direct_domains.append(domain)
                elif category == "directory":
                    directory_domains.append(domain)
                elif category == "media":
                    media_domains.append(domain)
                elif category == "spam":
                    spam_domains.append(domain)

    return {
        "all_results": Counter(all_domains).most_common(10),
        "direct_competitors": Counter(direct_domains).most_common(10),
        "directory_sites": Counter(directory_domains).most_common(10),
        "media_sites": Counter(media_domains).most_common(10),
        "spam_sites": Counter(spam_domains).most_common(10),
    }


if __name__ == "__main__":
    queries = [
        "best will writing singapore",
        "will writing services singapore",
        "estate planning singapore"
    ]

    result = discover_competitors(queries)

    print("\nDIRECT COMPETITORS")
    for domain, count in result["direct_competitors"]:
        print(f"{domain} - {count} appearances")

    print("\nDIRECTORY / LIST SITES")
    for domain, count in result["directory_sites"]:
        print(f"{domain} - {count} appearances")

    print("\nMEDIA / INFORMATION SITES")
    for domain, count in result["media_sites"]:
        print(f"{domain} - {count} appearances")

    print("\nSPAM / LOW-QUALITY SITES")
    for domain, count in result["spam_sites"]:
        print(f"{domain} - {count} appearances")