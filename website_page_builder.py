import json
import logging
from openai import OpenAI

client = OpenAI()
logger = logging.getLogger(__name__)


def build_business_context(
    business_name="",
    industry="",
    location="",
    services="",
    website_url="",
    research_results=None,
):
    name = (business_name or "").strip()
    industry_text = (industry or "").strip().lower()
    services_text = (services or "").strip().lower()
    combined = f"{industry_text} {services_text}"

    if any(word in combined for word in [
        "ice cream", "dessert", "confectionery", "food", "cafe",
        "restaurant", "bakery", "f&b", "beverage"
    ]):
        business_type = "food_and_beverage"
        primary_cta = "Shop Now"
        secondary_cta = "View Products"
        tone = "playful, warm, appetising, trustworthy"
        suggested_sections = [
            "Featured Products",
            "Best Sellers",
            "Why Customers Love Us",
            "Delivery and Ordering",
            "FAQ",
        ]
        avoid_terms = [
            "Book a Consultation",
            "professional services",
            "client advisory",
            "consulting",
            "service consultation",
        ]
        homepage_angle = (
            f"{name} offers nostalgic ice cream, desserts, confectionery, "
            f"and merchandise for customers in {location or 'Singapore'}."
        )

    elif any(word in combined for word in [
        "ecommerce", "online store", "shop", "merchandise", "products"
    ]):
        business_type = "ecommerce"
        primary_cta = "Shop Now"
        secondary_cta = "View Products"
        tone = "clear, product-focused, trustworthy, conversion-friendly"
        suggested_sections = [
            "Featured Products",
            "Popular Items",
            "Why Buy From Us",
            "Delivery Information",
            "FAQ",
        ]
        avoid_terms = [
            "Book a Consultation",
            "professional services",
            "client advisory",
            "consulting",
            "service consultation",
        ]
        homepage_angle = (
            f"{name} helps customers discover and buy products online "
            f"in {location or 'Singapore'}."
        )

    elif any(word in combined for word in [
        "dental", "clinic", "doctor", "medical", "aesthetic", "health"
    ]):
        business_type = "clinic"
        primary_cta = "Book an Appointment"
        secondary_cta = "View Services"
        tone = "professional, reassuring, clear, trustworthy"
        suggested_sections = [
            "Services",
            "Why Choose Us",
            "Treatment Information",
            "Patient FAQs",
            "Contact",
        ]
        avoid_terms = ["Shop Now", "Order Now", "Add to Cart"]
        homepage_angle = (
            f"{name} helps patients in {location or 'Singapore'} understand "
            f"available services and book appointments with confidence."
        )

    elif any(word in combined for word in [
        "tuition", "education", "enrichment", "school", "learning",
        "psle", "math", "english"
    ]):
        business_type = "education"
        primary_cta = "Enquire Now"
        secondary_cta = "View Programmes"
        tone = "supportive, clear, parent-friendly, encouraging"
        suggested_sections = [
            "Programmes",
            "Who It Helps",
            "Why Parents Choose Us",
            "Learning Approach",
            "FAQ",
        ]
        avoid_terms = ["Shop Now", "Order Now", "Add to Cart"]
        homepage_angle = (
            f"{name} helps students and parents in {location or 'Singapore'} "
            f"find suitable learning support."
        )

    else:
        business_type = "general_business"
        primary_cta = "Enquire Now"
        secondary_cta = "View Services"
        tone = "professional, clear, trustworthy"
        suggested_sections = [
            "Services",
            "Why Choose Us",
            "How It Works",
            "FAQ",
            "Contact",
        ]
        avoid_terms = []
        homepage_angle = (
            f"{name} helps customers in {location or 'Singapore'} understand "
            f"its services clearly."
        )

    products_or_services = []
    if services:
        products_or_services = [
            item.strip()
            for item in services.replace("\n", ",").split(",")
            if item.strip()
        ]

    return {
        "business_name": name,
        "industry": industry or "",
        "location": location or "",
        "website_url": website_url or "",
        "business_type": business_type,
        "products_or_services": products_or_services,
        "target_audience": [],
        "tone": tone,
        "primary_cta": primary_cta,
        "secondary_cta": secondary_cta,
        "suggested_sections": suggested_sections,
        "avoid_terms": avoid_terms,
        "homepage_angle": homepage_angle,
    }


def _page_spec_for_content_type(
    content_type, client_name, industry, primary_cta, secondary_cta
):
    """Return per-page-type prompt fragments: label, slug, focus rule,
    and the JSON-string section schema the model should produce.

    The legacy prompt always asked for the homepage 5-section shape
    (hero / services / value_prop / faq / cta_block) regardless of
    page_type. For a contact page, "services" + "value_prop" are
    wrong; for FAQ, having a "faq" section nested inside is awkward.
    Per-type shapes give the model better guidance and let downstream
    rendering rely on the right section list.
    """
    homepage_sections = f"""[
    {{
      "type": "hero",
      "eyebrow": "{industry}",
      "headline": "Specific headline for {client_name} based on its real products/services",
      "subtext": "Specific 2-3 sentence intro based on the business context.",
      "primary_cta": "{primary_cta}",
      "secondary_cta": "{secondary_cta}"
    }},
    {{
      "type": "services",
      "headline": "What We Offer",
      "items": [
        {{"title": "Real product/service 1", "description": "Specific description linked to {client_name}."}},
        {{"title": "Real product/service 2", "description": "Specific description linked to {client_name}."}},
        {{"title": "Real product/service 3", "description": "Specific description linked to {client_name}."}}
      ]
    }},
    {{
      "type": "value_prop",
      "headline": "Why Customers Choose {client_name}",
      "items": [
        {{"title": "Real reason 1", "description": "Specific explanation."}},
        {{"title": "Real reason 2", "description": "Specific explanation."}},
        {{"title": "Real reason 3", "description": "Specific explanation."}}
      ]
    }},
    {{
      "type": "faq",
      "headline": "Frequently Asked Questions",
      "items": [
        {{"question": "Question specific to this business.", "answer": "Helpful answer."}},
        {{"question": "Question about ordering/services/enquiries.", "answer": "Helpful answer."}},
        {{"question": "Question about location, delivery, or availability.", "answer": "Helpful answer."}}
      ]
    }},
    {{
      "type": "cta_block",
      "headline": "Ready to explore {client_name}?",
      "subtext": "Take the next step with {client_name}.",
      "primary_cta": "{primary_cta}",
      "secondary_cta": "{secondary_cta}"
    }}
  ]"""

    contact_sections = f"""[
    {{
      "type": "hero",
      "eyebrow": "Contact",
      "headline": "Get in touch with {client_name}",
      "subtext": "Specific 1-2 sentence intro about why a customer would contact them.",
      "primary_cta": "{primary_cta}",
      "secondary_cta": "{secondary_cta}"
    }},
    {{
      "type": "contact_details",
      "headline": "How to reach us",
      "items": [
        {{"title": "Phone or WhatsApp", "description": "Realistic placeholder for {client_name}."}},
        {{"title": "Email", "description": "Realistic placeholder for {client_name}."}},
        {{"title": "Address or location", "description": "Realistic placeholder based on {industry}."}},
        {{"title": "Opening hours or response time", "description": "Realistic for this business type."}}
      ]
    }},
    {{
      "type": "cta_block",
      "headline": "Looking forward to hearing from you",
      "subtext": "Reach out and the {client_name} team will respond.",
      "primary_cta": "{primary_cta}",
      "secondary_cta": "{secondary_cta}"
    }}
  ]"""

    about_sections = f"""[
    {{
      "type": "hero",
      "eyebrow": "About",
      "headline": "Who {client_name} is and why it exists",
      "subtext": "Brand-credibility-focused intro — what the business stands for.",
      "primary_cta": "{primary_cta}",
      "secondary_cta": "{secondary_cta}"
    }},
    {{
      "type": "story",
      "headline": "The story behind {client_name}",
      "body": "2-3 paragraphs about the origin, mission, and what makes this business credible."
    }},
    {{
      "type": "value_prop",
      "headline": "What customers can trust",
      "items": [
        {{"title": "Trust signal 1", "description": "Specific to this business."}},
        {{"title": "Trust signal 2", "description": "Specific to this business."}},
        {{"title": "Trust signal 3", "description": "Specific to this business."}}
      ]
    }},
    {{
      "type": "cta_block",
      "headline": "Get to know {client_name}",
      "subtext": "Take the next step.",
      "primary_cta": "{primary_cta}",
      "secondary_cta": "{secondary_cta}"
    }}
  ]"""

    faq_sections = f"""[
    {{
      "type": "hero",
      "eyebrow": "FAQ",
      "headline": "Common questions about {client_name}",
      "subtext": "Brief intro framing the answers customers will find below.",
      "primary_cta": "{primary_cta}",
      "secondary_cta": "{secondary_cta}"
    }},
    {{
      "type": "faq",
      "headline": "Frequently asked questions",
      "items": [
        {{"question": "Real question a customer would ask.", "answer": "Helpful, specific answer."}},
        {{"question": "Real question a customer would ask.", "answer": "Helpful, specific answer."}},
        {{"question": "Real question a customer would ask.", "answer": "Helpful, specific answer."}},
        {{"question": "Real question a customer would ask.", "answer": "Helpful, specific answer."}},
        {{"question": "Real question a customer would ask.", "answer": "Helpful, specific answer."}}
      ]
    }},
    {{
      "type": "cta_block",
      "headline": "Still have questions?",
      "subtext": "Reach out to {client_name}.",
      "primary_cta": "{primary_cta}",
      "secondary_cta": "{secondary_cta}"
    }}
  ]"""

    services_sections = f"""[
    {{
      "type": "hero",
      "eyebrow": "Services",
      "headline": "What {client_name} offers",
      "subtext": "1-2 sentence intro to the offering.",
      "primary_cta": "{primary_cta}",
      "secondary_cta": "{secondary_cta}"
    }},
    {{
      "type": "services",
      "headline": "Core offerings",
      "items": [
        {{"title": "Real offering 1", "description": "What it includes, who it's for."}},
        {{"title": "Real offering 2", "description": "What it includes, who it's for."}},
        {{"title": "Real offering 3", "description": "What it includes, who it's for."}}
      ]
    }},
    {{
      "type": "faq",
      "headline": "Questions about our services",
      "items": [
        {{"question": "Real question.", "answer": "Helpful answer."}},
        {{"question": "Real question.", "answer": "Helpful answer."}},
        {{"question": "Real question.", "answer": "Helpful answer."}}
      ]
    }},
    {{
      "type": "cta_block",
      "headline": "Ready to begin?",
      "subtext": "Take the next step with {client_name}.",
      "primary_cta": "{primary_cta}",
      "secondary_cta": "{secondary_cta}"
    }}
  ]"""

    specs = {
        "home": {
            "page_label": "homepage",
            "slug": "home",
            "page_focus_rule": "Lead with the homepage angle and product/service overview.",
            "sections_schema": homepage_sections,
        },
        "landing_page": {
            "page_label": "homepage",
            "slug": "home",
            "page_focus_rule": "Lead with the homepage angle and product/service overview.",
            "sections_schema": homepage_sections,
        },
        "contact": {
            "page_label": "contact page",
            "slug": "contact",
            "page_focus_rule": "Focus on how customers reach the business — no services/value-prop blocks.",
            "sections_schema": contact_sections,
        },
        "about": {
            "page_label": "about page",
            "slug": "about",
            "page_focus_rule": "Focus on credibility, story, and trust — not product listing.",
            "sections_schema": about_sections,
        },
        "faq": {
            "page_label": "FAQ page",
            "slug": "faq",
            "page_focus_rule": "Focus on 5+ specific question/answer pairs customers would actually ask.",
            "sections_schema": faq_sections,
        },
        "services": {
            "page_label": "services page",
            "slug": "services",
            "page_focus_rule": "Focus on detailed service descriptions and service-specific FAQs.",
            "sections_schema": services_sections,
        },
    }

    return specs.get(content_type, specs["home"])


def generate_structured_website_page(
    client_name,
    industry,
    location,
    target_query,
    content_type="landing_page",
    brand_context="",
):
    business_context = build_business_context(
        business_name=client_name,
        industry=industry,
        location=location,
        services=target_query,
        website_url="",
        research_results=None,
    )

    primary_cta = business_context.get("primary_cta", "Enquire Now")
    secondary_cta = business_context.get("secondary_cta", "View Services")

    page_spec = _page_spec_for_content_type(
        content_type, client_name, industry, primary_cta, secondary_cta
    )

    prompt = f"""
You are an expert website strategist and conversion copywriter.

Create a {page_spec["page_label"]} for the actual business below.

BUSINESS CONTEXT:
{json.dumps(business_context, ensure_ascii=False, indent=2)}

BUSINESS DETAILS:
- Business name: {client_name}
- Industry: {industry}
- Location: {location}
- Main offerings/products/services: {target_query}
- Page type: {content_type}
{f"- Brand context: {brand_context}" if brand_context else ""}

STRICT RULES:
- The page must be specific to this business.
- Use the exact business type from the business context.
- Use product-focused copy for food_and_beverage and ecommerce businesses.
- Do not use generic professional-services wording for product/ecommerce/F&B brands.
- Do not use any of these avoided terms: {business_context.get("avoid_terms", [])}
- Primary CTA must be: {primary_cta}
- Secondary CTA must be: {secondary_cta}
- {page_spec["page_focus_rule"]}

Return ONLY valid JSON. No markdown. No code block.

Use this exact JSON structure:

{{
  "page_type": "{content_type}",
  "title": "Specific {page_spec["page_label"]} title for {client_name}",
  "slug": "{page_spec["slug"]}",
  "meta_title": "SEO title for {client_name}",
  "meta_description": "SEO description for {client_name}",
  "seo": {{
    "meta_description": "SEO description for {client_name}",
    "keywords": ["{client_name}", "{industry}", "{location}"],
    "og_title": "Social title for {client_name}",
    "og_description": "Social description for {client_name}"
  }},
  "sections": {page_spec["sections_schema"]}
}}
"""

    # Model + response_format align with content_brief_generator and
    # content_draft_generator (the other user-facing copy generators).
    # `json_object` lets us drop the brittle ```json``` strip-and-pray
    # logic the older version needed.
    system_prompt = (
        "You are a senior conversion copywriter for AI-answer-engine-"
        "optimised (AEO) websites. Write copy that is specific to the "
        "actual business given — never use template phrases like "
        "'discover X's products and sweet treats' or 'we help "
        "customers understand their options'. Every section must "
        "reference real products, real services, or real concerns of "
        "this business's customers. Return a JSON object that exactly "
        "matches the schema in the user message — no extra keys, no "
        "markdown, no commentary."
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.4,
            max_tokens=3000,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content
        page_json = json.loads(raw)

        page_json = _validate_page_structure(
            page_json,
            client_name,
            target_query,
            content_type,
            business_context,
        )

        return page_json

    except Exception as e:
        logger.error(f"Website generation error: {str(e)}")
        return _create_fallback_page(
            client_name,
            industry,
            location,
            target_query,
            content_type,
            business_context,
        )


def _validate_page_structure(
    page_json,
    client_name,
    target_query,
    content_type,
    business_context=None,
):
    if not isinstance(page_json, dict):
        raise ValueError("Page JSON must be a dictionary")

    business_context = business_context or {}

    page_json.setdefault("page_type", content_type)
    page_json.setdefault("title", f"{client_name} - {target_query}")
    page_json.setdefault("slug", "home")

    if "seo" not in page_json or not isinstance(page_json["seo"], dict):
        page_json["seo"] = {
            "meta_description": f"Learn more about {client_name}.",
            "keywords": [client_name, target_query],
            "og_title": page_json.get("title", client_name),
            "og_description": f"Discover {client_name}.",
        }

    if "sections" not in page_json or not isinstance(page_json["sections"], list):
        page_json["sections"] = []

    primary_cta = business_context.get("primary_cta", "Enquire Now")
    secondary_cta = business_context.get("secondary_cta", "View Services")
    avoid_terms = business_context.get("avoid_terms", [])

    for section in page_json["sections"]:
        if not isinstance(section, dict):
            continue

        if section.get("type") in ["hero", "cta_block", "cta"]:
            section["primary_cta"] = primary_cta
            section["secondary_cta"] = secondary_cta

        for bad_term in avoid_terms:
            for key, value in list(section.items()):
                if isinstance(value, str) and bad_term.lower() in value.lower():
                    section[key] = value.replace(bad_term, primary_cta)

    return page_json


def _create_fallback_page(
    client_name,
    industry,
    location,
    target_query,
    content_type,
    business_context=None,
):
    business_context = business_context or build_business_context(
        business_name=client_name,
        industry=industry,
        location=location,
        services=target_query,
    )

    primary_cta = business_context.get("primary_cta", "Enquire Now")
    secondary_cta = business_context.get("secondary_cta", "View Services")
    suggested_sections = business_context.get("suggested_sections", ["What We Offer"])

    return {
        "page_type": content_type,
        "title": f"{client_name} - {industry}",
        "slug": "home",
        "seo": {
            "meta_description": business_context.get(
                "homepage_angle", f"Learn more about {client_name}."
            ),
            "keywords": [client_name, industry, location, target_query],
            "og_title": f"{client_name} - {industry}",
            "og_description": business_context.get(
                "homepage_angle", f"Discover {client_name}."
            ),
        },
        "sections": [
            {
                "type": "hero",
                "eyebrow": industry,
                "headline": business_context.get(
                    "homepage_angle", f"Welcome to {client_name}"
                ),
                "subtext": (
                    f"{client_name} serves customers in {location}. "
                    f"Explore our {target_query}."
                ),
                "primary_cta": primary_cta,
                "secondary_cta": secondary_cta,
            },
            {
                "type": "services",
                "headline": suggested_sections[0],
                "items": [
                    {
                        "title": item,
                        "description": f"Explore {item} from {client_name}.",
                    }
                    for item in business_context.get(
                        "products_or_services", [target_query]
                    )[:3]
                ],
            },
            {
                "type": "faq",
                "headline": "Frequently Asked Questions",
                "items": [
                    {
                        "question": f"What does {client_name} offer?",
                        "answer": business_context.get(
                            "homepage_angle",
                            f"{client_name} offers {target_query}.",
                        ),
                    },
                    {
                        "question": f"Where is {client_name} based?",
                        "answer": f"{client_name} serves customers in {location}.",
                    },
                ],
            },
            {
                "type": "cta_block",
                "headline": f"Explore {client_name}",
                "subtext": business_context.get(
                    "homepage_angle", f"Take the next step with {client_name}."
                ),
                "primary_cta": primary_cta,
                "secondary_cta": secondary_cta,
            },
        ],
    }