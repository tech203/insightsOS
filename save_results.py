"""Persist a completed audit run.

Migrated from outputs/<site>_<type>_<ts>_{summary,full}.json files
to the SQL `audits` table. See app.py Audit model.

The public function `save_audit_results` keeps the same signature
and return shape (a {"full_file", "summary_file"} dict) so the two
callers (main.py CLI + audit_runner.py) don't need to change. The
"file" values are now filename-identifiers, not on-disk paths —
URLs like /audit/<filename> continue to work because the audits
table is keyed on filename.
"""

import secrets
from datetime import datetime


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
    """Construct an audit filename like:

        enfactum-com_full_20260506_145038_xY3z9aBc

    The website slug + audit_type + timestamp keep the filename
    self-describing for ops and debugging. The trailing
    secrets.token_urlsafe(8) (~64 bits of entropy) is defence in
    depth against IDOR — even if a future regression bypasses the
    audit_belongs_to_current_user check, an attacker can't guess
    valid filenames from a target's domain alone. It also replaces
    the microsecond suffix previously used to avoid collisions on
    the `filename` primary key (the nonce subsumes that purpose).

    URL-safe characters (-, _, A-Z, a-z, 0-9) so the filename slots
    into /audit/<filename> without escaping, and into S3 paths
    without surprises.
    """
    clean_website = clean_website_name(website)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nonce = secrets.token_urlsafe(8)
    return f"{clean_website}_{audit_type}_{timestamp}_{nonce}"


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
    output_folder="outputs",  # kept for back-compat; unused
    client_id=None,
    client_name=None,
    user_id=None
):
    """Persist an audit run as a row in the `audits` table.

    Return shape preserved from the JSON-file era:
        {"full_file": "<filename>_full.json",
         "summary_file": "<filename>_summary.json"}
    so the two callers (main.py CLI, audit_runner.py) don't need
    to change. The "file" values are filename-identifiers stored
    on the Audit row, not on-disk paths — URLs like /audit/<filename>
    continue to resolve because the table is keyed on filename.
    """
    # Lazy import — save_results.py is imported during app.py's own
    # module load, so importing the model at top-level would race.
    from app import Audit, db

    base_filename = build_base_filename(website, audit_type)
    full_filename = f"{base_filename}_full.json"
    summary_filename = f"{base_filename}_summary.json"
    saved_at = datetime.now().isoformat()

    full_payload = {
        "website": website,
        "audit_type": audit_type,
        "saved_at": saved_at,
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

    # Pull out denormalized score columns so the audit-history list
    # page can sort without unpacking JSON. `_or_zero` is defensive
    # against payloads where the score didn't get computed (legacy
    # rows had None for some).
    scores = summary_payload.get("scores", {}) or {}

    audit = Audit(
        filename=summary_filename,
        user_id=user_id,
        client_id=str(client_id) if client_id is not None else None,
        client_name=client_name,
        website=website,
        audit_type=audit_type,
        saved_at=saved_at,
        normalized_score=_or_zero(scores.get("normalized_score")),
        visibility_score=_or_zero(scores.get("visibility_score")),
        content_score=_or_zero(scores.get("content_score")),
        schema_score=_or_zero(scores.get("schema_score")),
        summary_payload=summary_payload,
        full_payload=full_payload,
    )
    db.session.add(audit)
    db.session.commit()

    return {
        "full_file": full_filename,
        "summary_file": summary_filename,
    }


def _or_zero(v):
    """Coerce a possibly-None / possibly-string score field to float."""
    try:
        if v is None or v == "":
            return 0.0
        return float(v)
    except Exception:
        return 0.0


if __name__ == "__main__":
    saved_files = save_audit_results(
        website="https://example.com",
        audit_type="quick",
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