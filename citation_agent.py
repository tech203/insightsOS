from duckduckgo_search import DDGS


def check_citations(company_name, queries):

    results = []

    with DDGS() as ddgs:

        for q in queries:

            search_results = ddgs.text(q, max_results=5)

            found = False
            competitor = None

            for r in search_results:

                title = r.get("title", "").lower()
                body = r.get("body", "").lower()

                if company_name.lower() in title or company_name.lower() in body:
                    found = True
                else:
                    if not competitor:
                        competitor = r.get("href")

            results.append({
                "query": q,
                "cited": found,
                "competitor": competitor
            })

    return results


if __name__ == "__main__":

    queries = [
        "best interior designer singapore",
        "hdb renovation singapore",
        "condo interior design singapore"
    ]

    data = check_citations("brightside interior design", queries)

    for r in data:
        print(r)