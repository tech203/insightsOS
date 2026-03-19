from audit_agent import audit_website
from visibility_agent import calculate_visibility_score
from report_agent import build_report
from query_agent import generate_queries
from competitor_agent import discover_competitors
from business_profile_agent import extract_business_profile
from content_gap_agent import extract_site_content, detect_question_queries, detect_content_gaps
from question_coverage_agent import run_question_coverage_audit
from ai_answer_agent import run_ai_answer_test
from save_results import save_audit_results
import config


def run_full_audit():
    website = config.WEBSITE
    industry = config.INDUSTRY
    location = config.LOCATION
    topic = config.TOPIC if hasattr(config, "TOPIC") and config.TOPIC else industry

    # Business profile
    business_profile = extract_business_profile(website)

    print("\nBUSINESS PROFILE")
    print(business_profile)

    # Generate queries
    queries = generate_queries(topic, location)

    if config.AUDIT_TYPE == "free":
        query_count = config.FREE_QUERY_COUNT
    else:
        query_count = config.DETAILED_QUERY_COUNT

    queries_to_test = queries[:query_count]

    print("\nQUERIES TESTED:")
    for q in queries_to_test:
        print("-", q)

    # AI answer visibility test
    ai_answer_results = run_ai_answer_test(
        queries_to_test=queries_to_test,
        business_profile=business_profile
    )

    print("\nAI ANSWER TEST:")
    for row in ai_answer_results:
        print("-", row.get("query", ""))
        print("  Brand mentioned:", row.get("brand_mentioned", False))
        print("  Brand position:", row.get("brand_position"))
        print("  Competitors:", row.get("competitors_mentioned", []))
        print("  Directories:", row.get("directories_mentioned", []))
        print("  Media:", row.get("media_mentioned", []))
        print("  Answer type:", row.get("answer_type", "unknown"))
        print("  Score:", row.get("score", 0))
        print("  Answer:", row.get("answer", ""))
        print()

    # Calculate visibility score directly from full AI answer results
    visibility_data = calculate_visibility_score(ai_answer_results)

    print("\nVISIBILITY DATA:")
    print(visibility_data)

    # Content gap analysis
    site_text = extract_site_content(website)
    question_queries = detect_question_queries(queries_to_test)
    content_gaps = detect_content_gaps(question_queries, site_text)

    print("\nCONTENT GAPS:")
    for gap in content_gaps:
        print("-", gap)

    # Discover competitors
    competitor_data = discover_competitors(queries_to_test)

    print("\nDIRECT COMPETITORS:")
    for domain, count in competitor_data.get("direct_competitors", []):
        print("-", domain, "|", count, "appearances in analyzed results")

    print("\nDIRECTORY / LIST SITES:")
    for domain, count in competitor_data.get("directory_sites", []):
        print("-", domain, "|", count, "appearances in analyzed results")

    print("\nMEDIA / INFORMATION SITES:")
    for domain, count in competitor_data.get("media_sites", []):
        print("-", domain, "|", count, "appearances in analyzed results")

    print("\nSPAM / LOW-QUALITY SITES:")
    for domain, count in competitor_data.get("spam_sites", []):
        print("-", domain, "|", count, "appearances in analyzed results")

    # Run website audit
    audit_data = audit_website(website)

    # Question coverage audit
    question_coverage = run_question_coverage_audit(
        website,
        topic,
        max_questions=10 if config.AUDIT_TYPE == "free" else 25
    )

    print("\nQUESTION COVERAGE AUDIT:")

    if isinstance(question_coverage, dict):
        for row in question_coverage.get("results", []):
            print("-", row.get("question", ""), "|", row.get("status", ""), "|", row.get("score", ""))

        print("\nQUESTION COVERAGE PAGES USED:")
        for page in question_coverage.get("pages_used", []):
            print("-", page)

    else:
        for row in question_coverage:
            print("-", row.get("question", ""), "|", row.get("status", ""), "|", row.get("score", ""))

    # Build final report
    final_report = build_report(
        audit_data=audit_data,
        visibility_data=visibility_data,
        website=website,
        competitor_data=competitor_data,
        business_profile=business_profile,
        content_gaps=content_gaps,
        audit_type=config.AUDIT_TYPE,
        question_coverage=question_coverage,
        ai_answer_results=ai_answer_results
    )

    # Save results to JSON
    saved_files = save_audit_results(
        website=website,
        audit_type=config.AUDIT_TYPE,
        business_profile=business_profile,
        visibility_data=visibility_data,
        ai_answer_results=ai_answer_results,
        competitor_data=competitor_data,
        content_gaps=content_gaps,
        question_coverage=question_coverage,
        audit_data=audit_data,
        final_report=final_report
    )

    print("\nFINAL REPORT:")
    print(final_report["report_text"])

    print("\nRESULTS SAVED TO:")
    print("Full data:", saved_files["full_file"])
    print("Client summary:", saved_files["summary_file"])

    return final_report["report_text"]


if __name__ == "__main__":
    run_full_audit()