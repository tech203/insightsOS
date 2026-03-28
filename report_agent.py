def build_report(
    audit_data,
    visibility_data,
    website,
    competitor_data,
    business_profile,
    content_gaps,
    audit_type,
    question_coverage=None,
    ai_answer_results=None
):
    max_content_score = 30
    max_schema_score = 15
    max_visibility_score = 20
    max_total_score = max_content_score + max_schema_score + max_visibility_score

    content_score = audit_data.get("content_score", 0)
    schema_score = audit_data.get("schema_score", 0)
    visibility_score = visibility_data.get("visibility_score", 0)

    raw_score = content_score + schema_score + visibility_score
    normalized_score = round((raw_score / max_total_score) * 100, 2) if max_total_score else 0

    if normalized_score < 30:
        summary = "Weak foundation"
        verdict = "LOW OPPORTUNITY"
    elif normalized_score < 60:
        summary = "Moderate opportunity"
        verdict = "MODERATE OPPORTUNITY"
    else:
        summary = "Strong opportunity"
        verdict = "HIGH OPPORTUNITY"

    recommendations = []

    if audit_data.get("faq_pages", 0) == 0:
        recommendations.append("Add at least one FAQ page or FAQ section.")

    if audit_data.get("blog_pages", 0) < 3:
        recommendations.append("Publish more educational articles to improve topical authority.")

    if schema_score < 8:
        recommendations.append("Add or improve schema markup such as Organization and FAQPage.")

    if visibility_score < 10:
        recommendations.append("Improve AI visibility through stronger content, entity signals, and authority mentions.")

    if not recommendations:
        recommendations.append("Maintain current momentum and continue monitoring visibility.")

    business_title = business_profile.get("title", "N/A")
    business_description = business_profile.get("description", "N/A")
    business_email = business_profile.get("email", "N/A")
    business_phone = business_profile.get("phone", "N/A")
    services_detected = business_profile.get("services_detected", [])
    services_text = ", ".join(services_detected) if services_detected else "None"

    queries_tested = visibility_data.get("queries_tested", 0)
    appearances = visibility_data.get("appearances", 0)
    average_query_score = visibility_data.get("average_query_score", 0)

    direct_competitors = competitor_data.get("direct_competitors", [])
    directory_sites = competitor_data.get("directory_sites", [])
    media_sites = competitor_data.get("media_sites", [])
    spam_sites = competitor_data.get("spam_sites", [])

    report_text = ""

    if audit_type == "quick":
        report_text = f"""
==================================================
             AI VISIBILITY SNAPSHOT
==================================================

Website: {website}

VERDICT
-------
{verdict}

SCORE
-----
Raw Score: {raw_score} / {max_total_score}
Normalized Score: {normalized_score} / 100
Opportunity Level: {summary}

WHAT THIS MEANS
---------------
This free snapshot gives a quick view of how prepared the website appears for
AI-powered discovery and recommendation systems. It is designed as a high-level
screening report, not a full implementation audit.

BUSINESS PROFILE
----------------
Title: {business_title}
Description: {business_description}
Email: {business_email}
Phone: {business_phone}
Detected Services: {services_text}

AI VISIBILITY SNAPSHOT
----------------------
Queries Tested: {queries_tested}
Brand Appearances in AI Answers: {appearances}
Average Query Score: {average_query_score} / 20
Visibility Score: {visibility_score} / {max_visibility_score}
"""

        report_text += "\nAI ANSWER EXAMPLES\n------------------\n"
        if ai_answer_results:
            for row in ai_answer_results[:3]:
                report_text += f"• Query: {row.get('query', 'N/A')}\n"
                report_text += f"  Brand Mentioned: {row.get('brand_mentioned', False)}\n"
                report_text += f"  Brand Position: {row.get('brand_position', 'N/A')}\n"
                report_text += f"  Answer Type: {row.get('answer_type', 'unknown')}\n"
                report_text += f"  Score: {row.get('score', 0)} / 20\n"
        else:
            report_text += "• No AI answer examples available.\n"

        report_text += "\nDIRECT COMPETITORS\n------------------\n"
        if direct_competitors:
            for domain, count in direct_competitors[:3]:
                report_text += f"• {domain} ({count} appearances in analyzed results)\n"
        else:
            report_text += "• No major direct competitors detected in this pass.\n"

        report_text += "\nDIRECTORY / LIST SITES\n----------------------\n"
        if directory_sites:
            for domain, count in directory_sites[:3]:
                report_text += f"• {domain} ({count} appearances in analyzed results)\n"
        else:
            report_text += "• No major directory/list sites detected in this pass.\n"

        report_text += "\nMEDIA / INFORMATION SITES\n-------------------------\n"
        if media_sites:
            for domain, count in media_sites[:3]:
                report_text += f"• {domain} ({count} appearances in analyzed results)\n"
        else:
            report_text += "• No major media/info sites detected in this pass.\n"

        report_text += "\nTOP CONTENT OPPORTUNITIES\n-------------------------\n"
        if content_gaps[:5]:
            for gap in content_gaps[:5]:
                report_text += f"• {gap}\n"
        else:
            report_text += "• No major content gaps detected in this quick pass.\n"

        report_text += "\nQUESTION COVERAGE SNAPSHOT\n--------------------------\n"
        if question_coverage and question_coverage.get("results"):
            for row in question_coverage["results"][:5]:
                report_text += f"• {row['question']} — {row['status']} ({row['score']})\n"
        else:
            report_text += "• No question coverage analysis available in this pass.\n"

        report_text += "\nTOP RECOMMENDATIONS\n-------------------\n"
        for i, rec in enumerate(recommendations[:3], start=1):
            report_text += f"{i}. {rec}\n"

        report_text += """
NEXT STEP
---------
Book a strategy session to review these findings and map a deeper
AI Visibility plan with more queries, more competitor analysis,
and a 90-day implementation roadmap.
"""

    else:
        report_text = f"""
==================================================
          AI VISIBILITY STRATEGY AUDIT
==================================================

Website: {website}

VERDICT
-------
{verdict}

SCORES
------
Content Score: {content_score} / {max_content_score}
Schema Score: {schema_score} / {max_schema_score}
Visibility Score: {visibility_score} / {max_visibility_score}

Raw Score: {raw_score} / {max_total_score}
Normalized Score: {normalized_score} / 100
Opportunity Level: {summary}

EXECUTIVE SUMMARY
-----------------
This detailed audit evaluates the website's current structural readiness for
AI-powered visibility, observed visibility signals, competitor landscape,
and content opportunities. It is intended to support a deeper implementation strategy.

BUSINESS PROFILE
----------------
Title: {business_title}
Description: {business_description}
Email: {business_email}
Phone: {business_phone}
Detected Services: {services_text}

AI VISIBILITY BREAKDOWN
-----------------------
Queries Tested: {queries_tested}
Brand Appearances in AI Answers: {appearances}
Average Query Score: {average_query_score} / 20
Visibility Score: {visibility_score} / {max_visibility_score}
"""

        report_text += "\nAI ANSWER DETAILS\n-----------------\n"
        if ai_answer_results:
            for row in ai_answer_results:
                report_text += f"• Query: {row.get('query', 'N/A')}\n"
                report_text += f"  Brand Mentioned: {row.get('brand_mentioned', False)}\n"
                report_text += f"  Brand Position: {row.get('brand_position', 'N/A')}\n"
                report_text += f"  Competitors Mentioned: {', '.join(row.get('competitors_mentioned', [])) if row.get('competitors_mentioned') else 'None'}\n"
                report_text += f"  Directories Mentioned: {', '.join(row.get('directories_mentioned', [])) if row.get('directories_mentioned') else 'None'}\n"
                report_text += f"  Media Mentioned: {', '.join(row.get('media_mentioned', [])) if row.get('media_mentioned') else 'None'}\n"
                report_text += f"  Answer Type: {row.get('answer_type', 'unknown')}\n"
                report_text += f"  Query Score: {row.get('score', 0)} / 20\n"
                report_text += f"  Answer: {row.get('answer', 'N/A')}\n\n"
        else:
            report_text += "• No AI answer details available.\n"

        report_text += f"""
WEBSITE STRUCTURE
-----------------
Pages Checked: {audit_data.get('pages_checked', 'N/A')}
Service Pages: {audit_data.get('service_pages', 0)}
Blog Pages: {audit_data.get('blog_pages', 0)}
FAQ Pages: {audit_data.get('faq_pages', 0)}
Question Headings: {audit_data.get('question_headings', 0)}
Schema Types: {", ".join(audit_data.get('schema_types', [])) if audit_data.get('schema_types') else "None"}

CONTENT SCORE BREAKDOWN
-----------------------
Service Score: {audit_data.get('content_score_breakdown', {}).get('service_score', 'N/A')} / 10
Blog Score: {audit_data.get('content_score_breakdown', {}).get('blog_score', 'N/A')} / 10
FAQ Score: {audit_data.get('content_score_breakdown', {}).get('faq_score', 'N/A')} / 6
Question Score: {audit_data.get('content_score_breakdown', {}).get('question_score', 'N/A')} / 4
"""

        report_text += "\nDIRECT COMPETITORS\n------------------\n"
        if direct_competitors:
            for domain, count in direct_competitors:
                report_text += f"• {domain} ({count} appearances in analyzed results)\n"
        else:
            report_text += "• No major direct competitors detected.\n"

        report_text += "\nDIRECTORY / LIST SITES\n----------------------\n"
        if directory_sites:
            for domain, count in directory_sites:
                report_text += f"• {domain} ({count} appearances in analyzed results)\n"
        else:
            report_text += "• No major directory/list sites detected.\n"

        report_text += "\nMEDIA / INFORMATION SITES\n-------------------------\n"
        if media_sites:
            for domain, count in media_sites:
                report_text += f"• {domain} ({count} appearances in analyzed results)\n"
        else:
            report_text += "• No major media/info sites detected.\n"

        report_text += "\nLOW-QUALITY / SPAM SITES\n------------------------\n"
        if spam_sites:
            for domain, count in spam_sites:
                report_text += f"• {domain} ({count} appearances in analyzed results)\n"
        else:
            report_text += "• No major spam-like domains detected.\n"

        report_text += "\nCONTENT GAP OPPORTUNITIES\n-------------------------\n"
        if content_gaps:
            for gap in content_gaps:
                report_text += f"• {gap}\n"
        else:
            report_text += "• No major question-based content gaps detected in this pass.\n"

        report_text += "\nQUESTION COVERAGE AUDIT\n-----------------------\n"
        if question_coverage and question_coverage.get("results"):
            for row in question_coverage["results"]:
                report_text += f"• {row['question']} — {row['status']} ({row['score']})\n"
        else:
            report_text += "• No question coverage analysis available in this pass.\n"

        report_text += "\nQUESTION COVERAGE PAGES USED\n----------------------------\n"
        if question_coverage and question_coverage.get("pages_used"):
            for page in question_coverage["pages_used"]:
                report_text += f"• {page}\n"
        else:
            report_text += "• No page list available.\n"

        report_text += "\nRECOMMENDATIONS\n---------------\n"
        for i, rec in enumerate(recommendations, start=1):
            report_text += f"{i}. {rec}\n"

        report_text += """
90-DAY IMPLEMENTATION DIRECTION
-------------------------------
Phase 1: Fix structural gaps (FAQ, schema, key pages)
Phase 2: Publish educational topic-cluster content
Phase 3: Improve entity signals and authority mentions
Phase 4: Monitor AI visibility and refine query coverage
"""

    return {
        "raw_score": raw_score,
        "max_total_score": max_total_score,
        "normalized_score": normalized_score,
        "summary": summary,
        "verdict": verdict,
        "report_text": report_text
    }