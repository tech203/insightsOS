import json
import os
from datetime import datetime


def ensure_output_folder(folder="outputs"):
    os.makedirs(folder, exist_ok=True)
    return folder


def clean_website_name(website):
    return (
        website.replace("https://", "")
        .replace("http://", "")
        .replace("www.", "")
        .replace("/", "_")
        .replace(":", "_")
        .replace(".", "_")
    )


def build_base_filename(website, audit_type):
    clean_website = clean_website_name(website)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{clean_website}_{audit_type}_{timestamp}"


def save_json(payload, filepath):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def build_client_summary(
    website,
    audit_type,
    business_profile,
    visibility_data,
    competitor_data,
    content_gaps,
    audit_data,
    final_report,
    client_id=None,
    client_name=None,
    user_id=None,
):
    direct_competitors = competitor_data.get("direct_competitors", [])
    top_competitors = [
        {"domain": domain, "appearances": count}
        for domain, count in direct_competitors[:5]
    ]

    recommendations = []
    report_text = final_report.get("report_text", "")

    for line in report_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("1.", "2.", "3.", "4.", "5.")):
            recommendations.append(stripped)

    if not recommendations:
        recommendations = [
            "Add FAQ content and strengthen topical coverage.",
            "Improve schema markup and structured data.",
            "Increase AI visibility with stronger brand/entity signals."
        ]

    summary_payload = {
        "website": website,
        "audit_type": audit_type,
        "saved_at": datetime.now().isoformat(),
        "client_id": client_id,
        "client_name": client_name,
        "user_id": user_id,
        "scores": {
            "content_score": audit_data.get("content_score", 0),
            "schema_score": audit_data.get("schema_score", 0),
            "visibility_score": visibility_data.get("visibility_score", 0),
            "average_query_score": visibility_data.get("average_query_score", 0),
            "raw_score": final_report.get("raw_score", 0),
            "normalized_score": final_report.get("normalized_score", 0),
        },
        "summary": {
            "verdict": final_report.get("verdict", "N/A"),
            "opportunity_level": final_report.get("summary", "N/A"),
        },
        "business_profile": {
            "title": business_profile.get("title", "N/A"),
            "description": business_profile.get("description", "N/A"),
            "email": business_profile.get("email", "N/A"),
            "phone": business_profile.get("phone", "N/A"),
            "services_detected": business_profile.get("services_detected", []),
        },
        "visibility_snapshot": {
            "queries_tested": visibility_data.get("queries_tested", 0),
            "brand_appearances": visibility_data.get("appearances", 0),
        },
        "top_competitors": top_competitors,
        "top_content_gaps": content_gaps[:5],
        "top_recommendations": recommendations[:5],
    }

    return summary_payload


def save_audit_results(
    website,
    audit_type,
    business_profile,
    visibility_data,
    ai_answer_results,
    competitor_data,
    content_gaps,
    question_coverage,
    audit_data,
    final_report,
    output_folder="outputs",
    client_id=None,
    client_name=None,
    user_id=None
):
    ensure_output_folder(output_folder)
    base_filename = build_base_filename(website, audit_type)

    full_filepath = os.path.join(output_folder, f"{base_filename}_full.json")
    summary_filepath = os.path.join(output_folder, f"{base_filename}_summary.json")

    full_payload = {
        "website": website,
        "audit_type": audit_type,
        "saved_at": datetime.now().isoformat(),
        "client_id": client_id,
        "client_name": client_name,
        "user_id": user_id,
        "business_profile": business_profile,
        "visibility_data": visibility_data,
        "ai_answer_results": ai_answer_results,
        "competitor_data": competitor_data,
        "content_gaps": content_gaps,
        "question_coverage": question_coverage,
        "audit_data": audit_data,
        "final_report": final_report,
    }

    summary_payload = build_client_summary(
        website=website,
        audit_type=audit_type,
        business_profile=business_profile,
        visibility_data=visibility_data,
        competitor_data=competitor_data,
        content_gaps=content_gaps,
        audit_data=audit_data,
        final_report=final_report,
        client_id=client_id,
        client_name=client_name,
        user_id=user_id,
    )

    save_json(full_payload, full_filepath)
    save_json(summary_payload, summary_filepath)

    return {
        "full_file": full_filepath,
        "summary_file": summary_filepath,
    }


if __name__ == "__main__":
    saved_files = save_audit_results(
        website="https://example.com",
        audit_type="free",
        business_profile={"title": "Example Co", "description": "Example description"},
        visibility_data={
            "queries_tested": 3,
            "appearances": 1,
            "average_query_score": 7.5,
            "visibility_score": 7.5
        },
        ai_answer_results=[
            {
                "query": "best example services",
                "brand_mentioned": True,
                "brand_position": 2,
                "competitors_mentioned": ["Competitor A"],
                "directories_mentioned": [],
                "media_mentioned": [],
                "answer_type": "list-led",
                "score": 12,
                "answer": "Example Co is one option."
            }
        ],
        competitor_data={
            "direct_competitors": [("competitor.com", 3)],
            "directory_sites": [],
            "media_sites": [],
            "spam_sites": []
        },
        content_gaps=["What does Example Co do?"],
        question_coverage={"results": [], "pages_used": []},
        audit_data={"content_score": 10, "schema_score": 5},
        final_report={
            "raw_score": 22,
            "normalized_score": 40.0,
            "summary": "Moderate opportunity",
            "verdict": "MODERATE OPPORTUNITY",
            "report_text": "1. Improve schema markup\n2. Publish more educational content"
        },
        client_id="example-co",
        client_name="Example Co",
        user_id=1
    )

    print("Saved full file to:", saved_files["full_file"])
    print("Saved summary file to:", saved_files["summary_file"])