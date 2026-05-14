"""Seed a realistic audit + content queue for the Acme Bakery demo workspace.

The help-center capture script can already walk every page in the app,
but pages that depend on real audit output (Visibility, Competitors,
Audit history, Content queue, Growth calendar) show empty states because
the demo workspace has never had an audit run. This seeder writes a
realistic audit payload directly into the Audit table — plus a handful of
QueueItem rows — so re-capturing produces screenshots with actual
content.

Idempotent: deletes any prior demo audits / queue items for the workspace
before reseeding so successive runs produce a clean fixture.

Run with:
    PYTHONPATH=. python scripts/seed_help_audit_data.py
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone

from app import app, db, User, Client, Audit, QueueItem


EMAIL = "pro-test@example.com"
CLIENT_SLUG = "acme-bakery"

AUDIT_SAVED_AT = "2026-05-14T11:42:00"
AUDIT_FILENAME = "acmebakery-example-com_full_20260514_114200_summary.json"

PREVIOUS_SAVED_AT = "2026-04-23T09:15:00"
PREVIOUS_FILENAME = "acmebakery-example-com_full_20260423_091500_summary.json"


SCORES = {
    "normalized_score": 58.2,
    "visibility_score": 42.0,
    "content_score": 64.0,
    "schema_score": 48.0,
    "entity_score": 55.0,
    "technical_score": 62.0,
    "avg_query_score": 5.4,
    "brand_mention_rate": 41.7,
    "total_queries": 12,
    "brand_mentions": 5,
}

SUMMARY = {
    "verdict": "Moderate AEO foundation. Strong product pages, weak FAQ and entity signals.",
    "opportunity_level": "Moderate",
    "biggest_problem": "Acme Bakery is missing from 7 of 12 tracked AI answer prompts.",
    "biggest_opportunity": "Build comparison + FAQ pages for the 'best custom celebration cakes Brooklyn' cluster.",
    "top_3_actions": [
        "Add FAQ pages for the top 5 missed product queries",
        "Strengthen LocalBusiness + Bakery schema across location pages",
        "Publish a 'wedding cake guide' to capture the missed wedding cluster",
    ],
}

# 12 realistic bakery-relevant queries. Mix of mentioned / missed / improved.
QUERIES = [
    {"q": "best custom celebration cakes Brooklyn", "mentioned": True, "pos": 2, "score": 8.0, "delta": 1.0, "competitors": ["Magnolia Bakery", "Milk Bar", "Ladybird Bakery"]},
    {"q": "sourdough bakery near me Brooklyn",       "mentioned": True, "pos": 3, "score": 7.5, "delta": 2.0, "competitors": ["Sullivan Street Bakery", "She Wolf Bakery"]},
    {"q": "wedding cake shops Brooklyn NY",          "mentioned": False, "pos": None, "score": 2.0, "delta": -1.0, "competitors": ["Nine Cakes", "Lael Cakes", "Magnolia Bakery"]},
    {"q": "best birthday cakes Brooklyn",            "mentioned": True, "pos": 4, "score": 6.0, "delta": 0.0, "competitors": ["Ladybird Bakery", "Magnolia Bakery", "Confetti Cakes"]},
    {"q": "where to buy artisan pastries Brooklyn",  "mentioned": False, "pos": None, "score": 1.5, "delta": 0.0, "competitors": ["Bien Cuit", "Du's Donuts", "L'Imprimerie"]},
    {"q": "best gluten free bakery Brooklyn",        "mentioned": False, "pos": None, "score": 1.0, "delta": -2.0, "competitors": ["Erin McKenna's Bakery", "By the Way Bakery"]},
    {"q": "bakery for corporate gift orders NYC",    "mentioned": False, "pos": None, "score": 2.5, "delta": 0.0, "competitors": ["Magnolia Bakery", "Levain Bakery"]},
    {"q": "best chocolate croissants Brooklyn",      "mentioned": True, "pos": 1, "score": 9.5, "delta": 3.0, "competitors": ["L'Imprimerie", "Bien Cuit"]},
    {"q": "custom cookie platters Brooklyn delivery", "mentioned": False, "pos": None, "score": 1.5, "delta": 0.0, "competitors": ["Schmackary's", "Milk Bar", "Levain Bakery"]},
    {"q": "where to get a kids birthday cake Brooklyn", "mentioned": True, "pos": 3, "score": 7.0, "delta": 1.0, "competitors": ["Magnolia Bakery", "Ladybird Bakery"]},
    {"q": "vegan bakery Brooklyn",                   "mentioned": False, "pos": None, "score": 1.5, "delta": 0.0, "competitors": ["Erin McKenna's Bakery", "Pause Cafe"]},
    {"q": "best sourdough loaves Brooklyn",          "mentioned": False, "pos": None, "score": 2.0, "delta": 1.0, "competitors": ["She Wolf Bakery", "Sullivan Street Bakery", "Bien Cuit"]},
]


def query_analysis() -> list:
    return [
        {
            "query": q["q"],
            "brand_mentioned": q["mentioned"],
            "previous_brand_mentioned": q["mentioned"] and q["delta"] >= 0,
            "brand_position": q["pos"],
            "previous_brand_position": q["pos"],
            "score": q["score"],
            "previous_score": max(0.0, q["score"] - q["delta"]),
            "score_delta": q["delta"],
            "change_type": "improved" if q["delta"] > 0 else ("declined" if q["delta"] < 0 else "stable"),
            "priority": "high" if not q["mentioned"] else ("medium" if q["score"] < 7 else "low"),
            "competitors_mentioned": q["competitors"],
            "previous_competitors_mentioned": q["competitors"][:2],
            "answer_excerpt": (
                f"For {q['q']}, top recommendations include "
                + ", ".join(q["competitors"][:3]) + "."
            ),
        }
        for q in QUERIES
    ]


def competitor_analysis() -> dict:
    counts: dict[str, int] = {}
    for q in QUERIES:
        for c in q["competitors"]:
            counts[c] = counts.get(c, 0) + 1
    top = sorted(counts.items(), key=lambda x: -x[1])[:8]
    return {
        "top_competitors": [{"name": name, "mention_count": n} for name, n in top],
        "total_distinct_competitors": len(counts),
    }


def recommended_actions() -> list:
    return [
        {
            "category": "visibility_gap",
            "priority": "high",
            "title": "Build a 'best custom celebration cakes Brooklyn' comparison page",
            "issue": "Brand is missing from 4 of the 5 highest-volume celebration-cake queries.",
            "why_it_matters": "These prompts drive most of the lead intent in Acme Bakery's tracked set; competitors are dominating them.",
            "recommended_fix": "Publish a comparison/landing page with FAQ schema, customer photos, and clear pricing tiers.",
            "linked_query": "best custom celebration cakes Brooklyn",
            "suggested_content_type": "comparison_page",
            "support_signal": "Visibility score 42 with 7/12 queries missed.",
            "impact_score": 22,
            "difficulty": "medium",
            "credits_required": 12,
            "execution_type": "ai_executable",
            "source_url": "",
            "source_domain": "",
            "status": "open",
        },
        {
            "category": "entity_signals",
            "priority": "high",
            "title": "Strengthen LocalBusiness + Bakery schema on every location page",
            "issue": "Schema score is 48 — LocalBusiness is partially present but missing hours, areaServed, priceRange.",
            "why_it_matters": "Local prompts are decided heavily by structured data; missing fields means the bakery is invisible to AI answers in this category.",
            "recommended_fix": "Add Bakery + LocalBusiness JSON-LD to each of the three location pages, including geo, openingHoursSpecification, hasOfferCatalog.",
            "linked_query": "sourdough bakery near me Brooklyn",
            "suggested_content_type": "schema_update",
            "support_signal": "Schema score 48 / 100.",
            "impact_score": 18,
            "difficulty": "low",
            "credits_required": 4,
            "execution_type": "ai_executable",
            "source_url": "",
            "source_domain": "",
            "status": "open",
        },
        {
            "category": "content_coverage",
            "priority": "high",
            "title": "Publish a 'Brooklyn wedding cake guide' (FAQ + price tiers)",
            "issue": "Brand is not mentioned on any wedding-related prompt.",
            "why_it_matters": "Wedding cakes are the highest-AOV product line; missing this cluster has the biggest revenue impact.",
            "recommended_fix": "Long-form guide with sections for size, flavor, tasting, delivery, and pricing. Include FAQPage schema.",
            "linked_query": "wedding cake shops Brooklyn NY",
            "suggested_content_type": "long_form_guide",
            "support_signal": "Wedding cluster: 0/3 queries mentioned.",
            "impact_score": 20,
            "difficulty": "medium",
            "credits_required": 14,
            "execution_type": "ai_executable",
            "source_url": "",
            "source_domain": "",
            "status": "open",
        },
        {
            "category": "content_coverage",
            "priority": "medium",
            "title": "Add a gluten-free + vegan range page",
            "issue": "Missed for both 'best gluten free bakery Brooklyn' and 'vegan bakery Brooklyn'.",
            "why_it_matters": "Underserved dietary segments often have strong tracked-query volume but low local supply.",
            "recommended_fix": "Dedicated landing page covering the dietary range, ingredients, and substitution options. Strong FAQ section.",
            "linked_query": "best gluten free bakery Brooklyn",
            "suggested_content_type": "landing_page",
            "support_signal": "0/2 dietary queries mentioned.",
            "impact_score": 14,
            "difficulty": "low",
            "credits_required": 8,
            "execution_type": "ai_executable",
            "source_url": "",
            "source_domain": "",
            "status": "open",
        },
        {
            "category": "trust_signals",
            "priority": "medium",
            "title": "Expand About / Founders page with provenance + press mentions",
            "issue": "Entity score is 55 — the bakery's identity signals are thin.",
            "why_it_matters": "Provenance (years in business, awards, press) helps AI engines treat the brand as a trustworthy answer.",
            "recommended_fix": "Add a richer About page covering the family history, the sourdough starter, awards, and any press mentions.",
            "linked_query": "",
            "suggested_content_type": "about_page",
            "support_signal": "Entity score 55 / 100.",
            "impact_score": 10,
            "difficulty": "low",
            "credits_required": 6,
            "execution_type": "ai_executable",
            "source_url": "",
            "source_domain": "",
            "status": "open",
        },
    ]


def content_opportunities() -> list:
    return [
        {
            "title": "Best custom celebration cakes Brooklyn — comparison page",
            "target_query": "best custom celebration cakes Brooklyn",
            "content_type": "comparison_page",
            "priority": "high",
            "source_action_title": "Build a comparison page for the celebration-cake cluster",
            "reason": "Brand missing from the top celebration-cake prompt.",
            "status": "idea",
            "impact_score": 22,
            "credits_required": 12,
            "execution_type": "ai_executable",
            "source_url": "",
            "source_domain": "",
        },
        {
            "title": "Brooklyn wedding cake guide (FAQ + pricing)",
            "target_query": "wedding cake shops Brooklyn NY",
            "content_type": "long_form_guide",
            "priority": "high",
            "source_action_title": "Publish a wedding cake guide",
            "reason": "Highest-AOV cluster, currently 0/3 visibility.",
            "status": "idea",
            "impact_score": 20,
            "credits_required": 14,
            "execution_type": "ai_executable",
            "source_url": "",
            "source_domain": "",
        },
        {
            "title": "Gluten-free + vegan range page",
            "target_query": "best gluten free bakery Brooklyn",
            "content_type": "landing_page",
            "priority": "medium",
            "source_action_title": "Build a dietary-range landing page",
            "reason": "Two dietary prompts, both missing the brand.",
            "status": "idea",
            "impact_score": 14,
            "credits_required": 8,
            "execution_type": "ai_executable",
            "source_url": "",
            "source_domain": "",
        },
        {
            "title": "Corporate gift catalog — Brooklyn pickup + nationwide ship",
            "target_query": "bakery for corporate gift orders NYC",
            "content_type": "catalog_page",
            "priority": "medium",
            "source_action_title": "Add a corporate gift catalog",
            "reason": "B2B cluster the bakery already fulfils but doesn't market.",
            "status": "idea",
            "impact_score": 12,
            "credits_required": 10,
            "execution_type": "ai_executable",
            "source_url": "",
            "source_domain": "",
        },
        {
            "title": "Custom cookie platter ordering page",
            "target_query": "custom cookie platters Brooklyn delivery",
            "content_type": "service_page",
            "priority": "medium",
            "source_action_title": "Add a cookie platter service page",
            "reason": "Tracked query missing the brand.",
            "status": "idea",
            "impact_score": 10,
            "credits_required": 8,
            "execution_type": "ai_executable",
            "source_url": "",
            "source_domain": "",
        },
    ]


def site_findings() -> dict:
    return {
        "content_depth_score": 64.0,
        "schema_score": 48.0,
        "entity_score": 55.0,
        "technical_score": 62.0,
        "technical_issues": [
            "Image alt text missing on 12 product photos",
            "No FAQPage schema detected on the menu page",
        ],
        "content_gaps": [
            "No dedicated wedding cake guide",
            "Gluten-free / vegan products listed without a category page",
            "Corporate gifting flow has no landing page",
        ],
        "entity_gaps": [
            "About page does not mention founding year or awards",
            "No press / mentions section",
        ],
        "schema_gaps": [
            "LocalBusiness missing openingHoursSpecification",
            "Product schema missing offers.priceCurrency on cookies",
        ],
        "notes": [],
    }


def make_summary_payload() -> dict:
    return {
        "website": "https://acmebakery.example.com",
        "client_id": CLIENT_SLUG,
        "client_name": "Acme Bakery",
        "audit_type": "full",
        "saved_at": AUDIT_SAVED_AT,
        "scores": SCORES,
        "summary": SUMMARY,
        "recommended_actions": recommended_actions(),
        "content_opportunities": content_opportunities(),
        "visibility_snapshot": {
            "total_queries": SCORES["total_queries"],
            "queries_mentioned": SCORES["brand_mentions"],
            "queries_missed": SCORES["total_queries"] - SCORES["brand_mentions"],
            "mention_rate": SCORES["brand_mention_rate"],
            "avg_position": 2.6,
        },
        "top_competitors": competitor_analysis()["top_competitors"][:5],
        "top_content_gaps": [o["title"] for o in content_opportunities()[:3]],
        "top_recommendations": [r["title"] for r in recommended_actions()[:3]],
        "meta": {
            "website": "https://acmebakery.example.com",
            "website_normalized": "acmebakery.example.com",
            "industry": "Food & beverage",
            "location": "Brooklyn, NY",
            "audit_type": "full",
            "topic": "Bakery",
        },
        "schema_version": "2.0",
    }


def make_full_payload(user_id: int) -> dict:
    s = make_summary_payload()
    s.update({
        "user_id": user_id,
        "query_analysis": query_analysis(),
        "competitor_analysis": competitor_analysis(),
        "site_findings": site_findings(),
        "ai_answer_results": [
            {
                "query": q["q"],
                "brand_mentioned": q["mentioned"],
                "score": q["score"],
                "answer_text": (
                    f"For \"{q['q']}\", AI assistants typically recommend "
                    + ", ".join(q["competitors"][:3])
                    + (", with Acme Bakery cited as a strong alternative." if q["mentioned"] else ".")
                ),
            }
            for q in QUERIES
        ],
    })
    return s


def queue_items(user_id: int) -> list[QueueItem]:
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "title": "Brooklyn wedding cake guide (FAQ + pricing)",
            "target_query": "wedding cake shops Brooklyn NY",
            "item_type": "brief",
            "status": "approved",
            "priority": "high",
        },
        {
            "title": "Best custom celebration cakes Brooklyn — comparison page",
            "target_query": "best custom celebration cakes Brooklyn",
            "item_type": "draft",
            "status": "in_review",
            "priority": "high",
        },
        {
            "title": "Gluten-free + vegan range page",
            "target_query": "best gluten free bakery Brooklyn",
            "item_type": "brief",
            "status": "pending",
            "priority": "medium",
        },
        {
            "title": "Corporate gift catalog — pickup + nationwide ship",
            "target_query": "bakery for corporate gift orders NYC",
            "item_type": "idea",
            "status": "pending",
            "priority": "medium",
        },
        {
            "title": "Custom cookie platter ordering page",
            "target_query": "custom cookie platters Brooklyn delivery",
            "item_type": "draft",
            "status": "published",
            "priority": "low",
        },
    ]
    return [
        QueueItem(
            id=str(uuid.uuid4()),
            user_id=user_id,
            client_id=CLIENT_SLUG,
            client_name="Acme Bakery",
            target_query=r["target_query"],
            content_type="comparison_page",
            item_type=r["item_type"],
            title=r["title"],
            content="" if r["item_type"] == "idea" else
                    f"Draft content for {r['title']} would go here.",
            status=r["status"],
            priority=r["priority"],
            source="seeded",
            credits_required=10,
            execution_type="ai_executable",
            created_at=now,
            updated_at=now,
        )
        for r in rows
    ]


def main() -> int:
    with app.app_context():
        user = User.query.filter_by(email=EMAIL).first()
        if not user:
            print(f"user not found: {EMAIL}")
            return 1
        client = Client.query.filter_by(user_id=user.id, slug=CLIENT_SLUG).first()
        if not client:
            print(f"workspace not found: {CLIENT_SLUG}; run seed_help_workspace.py first")
            return 1

        # Wipe any prior demo audit / queue items for clean reseed.
        Audit.query.filter_by(user_id=user.id, client_id=CLIENT_SLUG).delete()
        QueueItem.query.filter_by(user_id=user.id, client_id=CLIENT_SLUG).delete()

        # Previous audit — weaker scores, fewer mentions. Enables the
        # query-level comparison table on the Visibility page.
        prev_summary = make_summary_payload()
        prev_summary["scores"] = {**SCORES,
                                  "normalized_score": 48.0,
                                  "visibility_score": 30.0,
                                  "content_score": 55.0,
                                  "schema_score": 36.0,
                                  "brand_mention_rate": 25.0,
                                  "brand_mentions": 3}
        prev_summary["saved_at"] = PREVIOUS_SAVED_AT
        prev_full = make_full_payload(user.id)
        prev_full["saved_at"] = PREVIOUS_SAVED_AT
        # Knock 1–2 points off each query score so deltas look real.
        for row in prev_full["ai_answer_results"]:
            row["score"] = max(0.0, row["score"] - 1.5)
        prev_audit = Audit(
            filename=PREVIOUS_FILENAME,
            user_id=user.id,
            client_id=CLIENT_SLUG,
            client_name="Acme Bakery",
            website="https://acmebakery.example.com",
            audit_type="full",
            saved_at=PREVIOUS_SAVED_AT,
            normalized_score=48.0,
            visibility_score=30.0,
            content_score=55.0,
            schema_score=36.0,
            summary_payload=prev_summary,
            full_payload=prev_full,
        )
        db.session.add(prev_audit)

        summary = make_summary_payload()
        full = make_full_payload(user.id)
        audit = Audit(
            filename=AUDIT_FILENAME,
            user_id=user.id,
            client_id=CLIENT_SLUG,
            client_name="Acme Bakery",
            website="https://acmebakery.example.com",
            audit_type="full",
            saved_at=AUDIT_SAVED_AT,
            normalized_score=SCORES["normalized_score"],
            visibility_score=SCORES["visibility_score"],
            content_score=SCORES["content_score"],
            schema_score=SCORES["schema_score"],
            summary_payload=summary,
            full_payload=full,
        )
        db.session.add(audit)
        for q in queue_items(user.id):
            db.session.add(q)
        db.session.commit()
        print(f"seeded 2 audits + {len(queue_items(user.id))} queue items "
              f"(latest score {SCORES['normalized_score']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
