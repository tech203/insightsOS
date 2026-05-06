import os
import json
from openai import OpenAI

client = OpenAI()


def generate_structured_website_page(
    client_name,
    industry,
    location,
    target_query,
    content_type="service_page",
    brand_context="",
):
    prompt = f"""
Create a premium website page as valid JSON only.

Business:
Name: {client_name}
Industry: {industry}
Location: {location}
Target query: {target_query}
Content type: {content_type}
Brand context: {brand_context}

Return JSON with:
page_type, title, slug, seo, sections.

Sections must include:
hero, problem, services, proof, faq, cta.
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Return only valid JSON. No markdown."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.7,
    )

    raw = response.choices[0].message.content.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()

    return json.loads(raw)