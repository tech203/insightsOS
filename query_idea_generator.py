def generate_query_ideas(industry="", location="", services=None):
    services = services or []

    base_terms = []

    if industry:
        base_terms.append(industry.lower())

    for service in services:
        if service:
            base_terms.append(service.lower())

    # remove duplicates
    base_terms = list(dict.fromkeys(base_terms))

    ideas = []

    for term in base_terms:
        if location:
            ideas.extend([
                f"best {term} in {location}",
                f"{term} near me {location}",
                f"{term} {location}",
                f"how to choose a {term} in {location}",
                f"where to find {term} in {location}",
                f"{term} delivery {location}",
                f"affordable {term} {location}",
                f"recommended {term} {location}",
            ])
        else:
            ideas.extend([
                f"best {term}",
                f"{term} near me",
                f"how to choose a {term}",
                f"where to find {term}",
                f"affordable {term}",
                f"recommended {term}",
            ])

    cleaned = []

    for idea in ideas:
        idea = idea.replace("  ", " ").strip()
        if idea and idea not in cleaned:
            cleaned.append(idea)

    return cleaned[:30]