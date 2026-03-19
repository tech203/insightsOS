def calculate_visibility_score(results):
    if not results:
        return {
            "queries_tested": 0,
            "appearances": 0,
            "average_query_score": 0,
            "visibility_score": 0
        }

    # Support both old boolean-style input and new dict-style input
    if isinstance(results[0], dict):
        appearances = sum(1 for row in results if row.get("brand_mentioned", False))
        total_score = sum(row.get("score", 0) for row in results)
        average_query_score = total_score / len(results)
        visibility_score = round(average_query_score, 2)
    else:
        appearances = sum(results)
        visibility_score = (appearances / len(results)) * 20
        average_query_score = visibility_score

    return {
        "queries_tested": len(results),
        "appearances": appearances,
        "average_query_score": round(average_query_score, 2),
        "visibility_score": round(visibility_score, 2)
    }


if __name__ == "__main__":
    sample_results_old = [True, False, False, True, False]
    print("OLD FORMAT TEST:")
    print(calculate_visibility_score(sample_results_old))

    sample_results_new = [
        {
            "query": "best will writing services in singapore",
            "brand_mentioned": True,
            "score": 14
        },
        {
            "query": "affordable estate planning singapore",
            "brand_mentioned": False,
            "score": 4
        },
        {
            "query": "will writing lawyer singapore",
            "brand_mentioned": True,
            "score": 11
        }
    ]
    print("\nNEW FORMAT TEST:")
    print(calculate_visibility_score(sample_results_new))