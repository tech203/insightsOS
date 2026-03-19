def generate_queries(topic, location=None):

    location_part = f" {location}" if location else ""

    # Core intent queries (commercial intent)
    intent_queries = [
        f"best {topic}{location_part}",
        f"top {topic}{location_part}",
        f"{topic} services{location_part}",
        f"recommended {topic}{location_part}",
        f"{topic} company{location_part}",
    ]

    # Problem / decision queries
    problem_queries = [
        f"how much does {topic} cost{location_part}",
        f"how to choose {topic}{location_part}",
        f"what is the best {topic}{location_part}",
        f"who are the top {topic} companies{location_part}",
        f"affordable {topic}{location_part}",
    ]

    # Comparison queries
    comparison_queries = [
        f"{topic} companies in{location_part}",
        f"compare {topic} services{location_part}",
        f"top rated {topic}{location_part}",
        f"{topic} experts{location_part}",
        f"trusted {topic}{location_part}",
    ]

    # Question-style queries (important for AI answers)
    question_queries = [
        f"what is {topic}",
        f"how does {topic} work",
        f"is {topic} worth it",
        f"who needs {topic}",
        f"why is {topic} important",
    ]

    return (
        intent_queries
        + problem_queries
        + comparison_queries
        + question_queries
    )


if __name__ == "__main__":

    topic = "interior designer"
    location = "singapore"

    queries = generate_queries(topic, location)

    print("\nGenerated Queries:\n")

    for q in queries:
        print("-", q)