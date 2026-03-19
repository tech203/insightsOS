import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_content_draft(
    client_name,
    website,
    industry,
    location,
    target_query,
    content_type="service_page",
    brief_context="",
    brand_context="",
    model="gpt-4.1-mini"
):
    prompt = f"""
You are an expert SEO and AI-visibility copywriter.

Write a strong first draft for website content.

Business name: {client_name}
Website: {website}
Industry: {industry}
Location: {location}
Target query: {target_query}
Content type: {content_type}
Brief context: {brief_context}
Brand context: {brand_context}

Requirements:
- Make the content practical and conversion-aware.
- Write in a professional but clear tone.
- Include helpful headings.
- Include a short FAQ section.
- Include a CTA at the end.
- Do not use fake claims or fake statistics.
- Make the content suitable as a website draft, not a blog essay unless content type is blog_post.

Return in this exact structure:

1. Page Title
2. Meta Title
3. Meta Description
4. Draft Content
5. FAQ Section
6. CTA

Keep it concise but usable as a first draft.
"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a senior content strategist and website copywriter focused on SEO, AI visibility, and conversion."
            },
            {
                "role": "user",
                "content": prompt
            },
        ],
        temperature=0.4,
    )

    draft_text = response.choices[0].message.content or ""

    return {
        "client_name": client_name,
        "website": website,
        "industry": industry,
        "location": location,
        "target_query": target_query,
        "content_type": content_type,
        "brief_context": brief_context,
        "brand_context": brand_context,
        "draft_text": draft_text.strip(),
    }


if __name__ == "__main__":
    result = generate_content_draft(
        client_name="Chung Will Writing Services",
        website="https://example.com",
        industry="Will Writing",
        location="Singapore",
        target_query="best will writing services in singapore",
        content_type="service_page",
        brief_context="Comparison-style page with FAQ and trust signals.",
        brand_context="Trusted Singapore will-writing provider."
    )

    print(result["draft_text"])