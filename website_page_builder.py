import os
import json
import logging
from openai import OpenAI

client = OpenAI()
logger = logging.getLogger(__name__)


def generate_structured_website_page(
    client_name,
    industry,
    location,
    target_query,
    content_type="service_page",
    brand_context="",
):
    """
    Generate a structured premium website page using AI.
    
    Args:
        client_name: Business name
        industry: Industry/business type
        location: Geographic location
        target_query: Search query/topic the page targets
        content_type: Type of page (service_page, landing_page, faq_page, etc.)
        brand_context: Additional brand info, tone, or requirements
    
    Returns:
        dict: Structured page JSON with sections
    
    Raises:
        ValueError: If content generation or parsing fails
    """
    
    # Build comprehensive, detailed prompt for high-quality output
    prompt = f"""You are a premium SaaS/enterprise web copywriter with 15+ years experience.
You specialize in creating conversion-optimized landing pages and service pages that rank in search results.
You understand buyer psychology, search intent, and high-conversion copy techniques.

BUSINESS BRIEFING:
- Company: {client_name}
- Industry: {industry}
- Market: {location}
- Primary Target: Users searching "{target_query}"
- Page Type: {content_type}
{f'- Brand Voice & Context: {brand_context}' if brand_context else ''}

YOUR TASK:
Create a sophisticated, professional website page targeting "{target_query}" search intent.
The page must be compelling, detailed, and designed to convert.

RETURN ONLY VALID JSON (no markdown, no code blocks, no explanations) matching this exact structure:

{{
  "page_type": "{content_type}",
  "title": "Compelling, specific 60-char headline optimized for '{target_query}'",
  "slug": "descriptive-url-slug",
  "meta_title": "SEO-optimized title (60 chars) targeting '{target_query}'",
  "meta_description": "Compelling 155-char description with search intent match and CTA",
  "seo": {{
    "meta_description": "...",
    "keywords": ["primary keyword", "long-tail variant", "intent-based keyword"],
    "og_title": "Shareable social media headline",
    "og_description": "Shareable social description"
  }},
  "sections": [
    {{
      "type": "hero",
      "layout": "split",
      "eyebrow": "Attention-grabbing subheading (10 words max)",
      "headline": "Powerful main value proposition addressing specific '{target_query}' pain point",
      "subtext": "3-4 sentences explaining transformation, outcome, and why {client_name} is different",
      "primary_cta": "Action-oriented CTA text",
      "secondary_cta": "Secondary CTA (alternative action)",
      "visual_note": "Professional, industry-relevant imagery recommended"
    }},
    {{
      "type": "value_prop",
      "headline": "Why {client_name} for {target_query}",
      "items": [
        {{
          "title": "Unique differentiator #1",
          "description": "2-3 sentence explanation of why this matters to the buyer"
        }},
        {{
          "title": "Unique differentiator #2",
          "description": "2-3 sentence explanation of competitive advantage"
        }},
        {{
          "title": "Unique differentiator #3",
          "description": "2-3 sentence explanation of transformation they'll experience"
        }}
      ]
    }},
    {{
      "type": "problem",
      "headline": "The Real Cost of Poor {target_query}",
      "subheading": "Understand the impact on your business",
      "body": "4-5 detailed paragraphs explaining: the problem, why it matters, common mistakes, cost of inaction, opportunity cost. Use specific numbers and real consequences.",
      "pain_points": [
        "Specific pain point 1 faced by target audience",
        "Specific pain point 2 with business impact",
        "Specific pain point 3 affecting growth"
      ]
    }},
    {{
      "type": "solution",
      "headline": "How {client_name} Solves {target_query}",
      "subheading": "Our proven methodology",
      "steps": [
        {{
          "number": 1,
          "title": "Step/Process 1",
          "description": "Detailed description of what happens and why it matters"
        }},
        {{
          "number": 2,
          "title": "Step/Process 2",
          "description": "How this builds on step 1 and moves toward solution"
        }},
        {{
          "number": 3,
          "title": "Step/Process 3",
          "description": "Final step and the transformation achieved"
        }}
      ]
    }},
    {{
      "type": "services",
      "headline": "Our {industry} Expertise in {target_query}",
      "subheading": "Comprehensive solutions designed for your needs",
      "items": [
        {{
          "title": "Detailed Service/Feature 1",
          "description": "2-3 sentences on specific benefit, outcome, and ROI impact",
          "benefit": "Primary benefit statement"
        }},
        {{
          "title": "Detailed Service/Feature 2",
          "description": "2-3 sentences on specific benefit, outcome, and ROI impact",
          "benefit": "Primary benefit statement"
        }},
        {{
          "title": "Detailed Service/Feature 3",
          "description": "2-3 sentences on specific benefit, outcome, and ROI impact",
          "benefit": "Primary benefit statement"
        }},
        {{
          "title": "Detailed Service/Feature 4",
          "description": "2-3 sentences on specific benefit, outcome, and ROI impact",
          "benefit": "Primary benefit statement"
        }}
      ]
    }},
    {{
      "type": "proof",
      "headline": "Proven Results With {client_name}",
      "subheading": "Real outcomes from businesses like yours",
      "stats": [
        {{
          "metric": "X% improvement in {target_query} metric",
          "context": "Specific outcome or transformation"
        }},
        {{
          "metric": "X% increase in relevant business metric",
          "context": "What this means for the customer"
        }},
        {{
          "metric": "X months to see measurable {target_query} results",
          "context": "Timeline and realistic expectation"
        }}
      ],
      "testimonials": [
        {{
          "quote": "Specific, detailed testimonial addressing {target_query} transformation",
          "author": "Title/role, Company type",
          "context": "Their situation before and after"
        }}
      ]
    }},
    {{
      "type": "comparison",
      "headline": "Why {client_name} Stands Out",
      "subheading": "How we compare to traditional approaches or competitors",
      "advantages": [
        {{
          "factor": "Specific competitive advantage",
          "our_approach": "How {client_name} does it differently",
          "traditional": "How others handle it"
        }},
        {{
          "factor": "Innovation or unique methodology",
          "our_approach": "Our unique value add",
          "traditional": "Standard industry approach"
        }},
        {{
          "factor": "Support or customer success element",
          "our_approach": "Our commitment to client success",
          "traditional": "What other providers offer"
        }}
      ]
    }},
    {{
      "type": "faq",
      "headline": "Frequently Asked Questions About {target_query}",
      "subheading": "Here's what clients want to know",
      "items": [
        {{
          "question": "How long does it take to see results with {client_name}'s {target_query} solution?",
          "answer": "Detailed answer with realistic timeline, factors that affect speed, and early wins to expect"
        }},
        {{
          "question": "What's the investment required for {target_query} at {client_name}?",
          "answer": "Transparent pricing discussion, what's included, ROI justification, options available"
        }},
        {{
          "question": "Is {target_query} solution suitable for businesses like ours?",
          "answer": "Clear answer on who benefits most, specific industry/size suitability, customization options"
        }},
        {{
          "question": "How does {client_name}'s {target_query} approach differ from DIY or other vendors?",
          "answer": "Clear competitive differentiation, specific advantages, why the difference matters"
        }},
        {{
          "question": "What kind of support and onboarding do we get?",
          "answer": "Detailed support structure, training, success metrics, ongoing relationship"
        }}
      ]
    }},
    {{
      "type": "trust",
      "headline": "Why {client_name} is Trusted for {target_query}",
      "items": [
        {{
          "element": "Years of experience or specific credential",
          "detail": "Specific evidence or example"
        }},
        {{
          "element": "Industry recognition, certification, or award",
          "detail": "Credibility indicator"
        }},
        {{
          "element": "Client portfolio or case study highlight",
          "detail": "Proof of execution in similar contexts"
        }}
      ]
    }},
    {{
      "type": "cta_block",
      "headline": "Transform Your {target_query} Strategy With {client_name}",
      "subtext": "Join companies in the {industry} space who've improved their {target_query} results",
      "primary_cta": "Schedule a free consultation",
      "secondary_cta": "Explore case studies",
      "cta_note": "No long-term commitment. Learn what's possible for your business."
    }}
  ]
}}

CRITICAL QUALITY REQUIREMENTS:
1. Content must be SPECIFIC to {client_name}, "{target_query}", and {industry} — NO generic placeholders
2. All copy must directly address buyer pain points and transformation they want
3. Every section must have a specific conversion goal
4. Use specific numbers, metrics, and timeframes (not "significant" or "better")
5. Tone must be professional, authoritative, and trustworthy
6. Include psychological persuasion elements: social proof, scarcity, specificity, transformation
7. Each section must move the buyer through the decision journey
8. All CTAs must be specific and action-oriented
9. Demonstrate deep understanding of {industry} and {target_query} challenges
10. Highlight unique methodology, not just features

STRUCTURE REQUIREMENTS:
- Total: 10+ comprehensive sections
- Each section must have 3+ sub-elements/items
- FAQ: 5 detailed questions addressing real buyer concerns
- Proof section: Include specific metrics and testimonials
- Every major claim must have supporting detail
"""


    try:
        # Use lower temperature for consistency and better JSON structure
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are an expert web copywriter. Always return valid JSON exactly as specified. No markdown, no code blocks, no additional text."
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,  # Lower for consistency
            max_tokens=2000,
        )

        raw = response.choices[0].message.content.strip()
        
        # Clean up common formatting issues
        raw = raw.replace("```json", "").replace("```", "").strip()
        
        # Parse JSON
        try:
            page_json = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}. Raw content: {raw[:200]}")
            raise ValueError(f"Failed to parse AI response as JSON: {str(e)}")
        
        # Validate structure
        page_json = _validate_page_structure(
            page_json, 
            client_name, 
            target_query, 
            content_type
        )
        
        return page_json

    except Exception as e:
        logger.error(f"Website generation error: {str(e)}")
        # Return minimal valid structure as fallback
        return _create_fallback_page(
            client_name, 
            industry, 
            location, 
            target_query, 
            content_type
        )


def _validate_page_structure(page_json, client_name, target_query, content_type):
    """Validate and fix the page JSON structure."""
    
    # Ensure required fields exist
    if not isinstance(page_json, dict):
        raise ValueError("Page JSON must be a dictionary")
    
    # Set defaults for missing fields
    page_json.setdefault("page_type", content_type)
    page_json.setdefault("title", f"{client_name} - {target_query}")
    page_json.setdefault("slug", target_query.lower().replace(" ", "-")[:50])
    
    # Ensure SEO object
    if "seo" not in page_json:
        page_json["seo"] = {
            "meta_description": f"Learn how {client_name} can help with {target_query}",
            "keywords": [target_query],
            "og_title": page_json.get("title", f"{client_name} - {target_query}"),
            "og_description": f"Discover {client_name}'s solution for {target_query}"
        }
    
    # Ensure sections array exists
    if "sections" not in page_json or not isinstance(page_json["sections"], list):
        page_json["sections"] = []
    
    # Validate each section has required fields
    required_section_types = ["hero", "problem", "services", "proof", "faq", "cta"]
    existing_types = {s.get("type") for s in page_json["sections"] if s.get("type")}
    
    for section_type in required_section_types:
        if section_type not in existing_types:
            logger.warning(f"Missing {section_type} section, skipping validation")
    
    return page_json


def _create_fallback_page(client_name, industry, location, target_query, content_type):
    """Create a minimal valid page structure when generation fails."""
    
    logger.warning(
        f"Using fallback page for {client_name} - {target_query}"
    )
    
    return {
        "page_type": content_type,
        "title": f"{client_name} - {target_query}",
        "slug": target_query.lower().replace(" ", "-")[:50],
        "seo": {
            "meta_description": f"Learn how {client_name} in {location} can help with {target_query}",
            "keywords": [target_query, client_name, industry],
            "og_title": f"{client_name} - {target_query}",
            "og_description": f"{client_name}'s solution for {target_query} in {location}"
        },
        "sections": [
            {
                "type": "hero",
                "eyebrow": f"Solution for {target_query}",
                "headline": f"{client_name}: Your Answer to {target_query}",
                "subtext": f"Based in {location}, we specialize in {industry}. We help businesses solve {target_query} challenges.",
                "primary_cta": "Learn More",
                "secondary_cta": "Contact Us"
            },
            {
                "type": "problem",
                "headline": f"The Challenge with {target_query}",
                "body": f"Many businesses in the {industry} space struggle with {target_query}. This impacts their visibility and growth. At {client_name}, we understand these challenges and have developed proven solutions."
            },
            {
                "type": "services",
                "headline": "How We Help",
                "items": [
                    {
                        "title": f"Expert {target_query} Solutions",
                        "description": f"We provide specialized expertise in {target_query} for {industry} businesses"
                    },
                    {
                        "title": "Customized Approach",
                        "description": "Every solution is tailored to your specific needs and goals"
                    },
                    {
                        "title": "Proven Results",
                        "description": "Our methodology is tested and proven to deliver results"
                    }
                ]
            },
            {
                "type": "proof",
                "headline": "Trusted by Businesses",
                "items": [
                    {
                        "stat": "Established",
                        "description": f"{client_name} is a trusted name in {industry}"
                    },
                    {
                        "stat": "Located",
                        "description": f"Serving {location} and surrounding areas"
                    }
                ]
            },
            {
                "type": "faq",
                "headline": f"About {target_query}",
                "items": [
                    {
                        "question": f"Why is {target_query} important?",
                        "answer": f"{target_query} is crucial for {industry} businesses to stay competitive and visible in their market."
                    },
                    {
                        "question": f"How can {client_name} help?",
                        "answer": f"We offer comprehensive solutions tailored to your {target_query} needs."
                    }
                ]
            },
            {
                "type": "cta",
                "headline": f"Ready to solve {target_query}?",
                "subtext": f"Let {client_name} help you succeed",
                "button_text": "Get Started Today"
            }
        ]
    }