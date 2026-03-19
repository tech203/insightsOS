import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_content_brief(
    client_name,
    website,
    industry,
    location,
    target_query,
    content_type="service_page",
    brand_context="",
    model="gpt-4.1-mini"
):
    prompt = f"""
You are an expert AI visibility and SEO content strategist.

Create a practical content brief for a business that wants to improve AI visibility and search visibility.

Business name: {client_name}
Website: {website}
Industry: {industry}
Location: {location}
Target query: {target_query}
Requested content type: {content_type}
Additional brand context: {brand_context}

Return the brief in this exact structure:

1. Search Intent
2. Recommended Page Angle
3. Primary Keyword
4. Secondary Keywords
5. Suggested Title Ideas
6. Meta Title
7. Meta Description
8. Recommended Outline
9. FAQ Ideas
10. Trust / Proof Elements to Include
11. Internal Linking Suggestions
12. Schema Suggestions
13. CTA Direction

Keep it concise, specific, and agency-ready.
Use bullet points where helpful.
Avoid fluff.
"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a senior content strategist focused on AI visibility, SEO, and conversion-oriented website content."
            },
            {
                "role": "user",
                "content": prompt
            },
        ],
        temperature=0.3,
    )

    brief_text = response.choices[0].message.content or ""

    return {
        "client_name": client_name,
        "website": website,
        "industry": industry,
        "location": location,
        "target_query": target_query,
        "content_type": content_type,
        "brand_context": brand_context,
        "brief_text": brief_text.strip(),
    }


if __name__ == "__main__":
    result = generate_content_brief(
        client_name="Chung Will Writing Services",
        website="https://example.com",
        industry="Will Writing",
        location="Singapore",
        target_query="best will writing services in singapore",
        content_type="comparison_page",
        brand_context="Trusted will-writing provider targeting Singapore customers."
    )

    print(result["brief_text"])