import os
import re
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

DIRECTORY_HINTS = [
    "yelp", "threebestrated", "yellowpages", "clutch", "goodfirms",
    "trustpilot", "tripadvisor", "glassdoor", "indeed", "sortlist"
]

MEDIA_HINTS = [
    "forbes", "techcrunch", "cnn", "bbc", "straitstimes", "channelnewsasia",
    "businessinsider", "hubspot", "nerdwallet", "investopedia"
]


def normalize_text(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip())


def extract_list_items(answer):
    if not answer:
        return []

    lines = answer.split("\n")
    items = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith(("-", "•", "*")):
            cleaned = line.lstrip("-•* ").strip()
            if cleaned:
                items.append(cleaned)
            continue

        if re.match(r"^\d+\.", line):
            cleaned = re.sub(r"^\d+\.\s*", "", line).strip()
            if cleaned:
                items.append(cleaned)

    return items


def detect_brand_mentioned(answer, brand_name):
    if not answer or not brand_name:
        return False
    return brand_name.lower() in answer.lower()


def detect_brand_position(answer, brand_name):
    if not answer or not brand_name:
        return None

    lines = extract_list_items(answer)

    # Try list position first
    for idx, line in enumerate(lines, start=1):
        if brand_name.lower() in line.lower():
            return idx

    # Fallback: detect first occurrence in normal paragraph text
    lower_answer = answer.lower()
    lower_brand = brand_name.lower()

    if lower_brand in lower_answer:
        return 1

    return None


def extract_named_entities(answer, brand_name):
    """
    Very simple heuristic extractor from bulleted / numbered lists.
    Later you can replace this with LLM-based structured extraction.
    """
    items = extract_list_items(answer)
    competitors = []
    directories = []
    media = []

    for item in items:
        lower_item = item.lower()

        if brand_name and brand_name.lower() in lower_item:
            continue

        if any(hint in lower_item for hint in DIRECTORY_HINTS):
            directories.append(item)
        elif any(hint in lower_item for hint in MEDIA_HINTS):
            media.append(item)
        else:
            competitors.append(item)

    return {
        "competitors_mentioned": competitors[:5],
        "directories_mentioned": directories[:5],
        "media_mentioned": media[:5],
    }


def classify_answer_type(answer, brand_name, competitors, directories, media):
    if not answer:
        return "unknown"

    lower_answer = answer.lower()
    brand_in_answer = brand_name.lower() in lower_answer if brand_name else False

    if brand_in_answer and not competitors and not directories:
        return "brand-led"

    if directories:
        return "directory-led"

    if media:
        return "editorial-led"

    if competitors:
        return "list-led"

    return "general"


def score_ai_visibility(
    brand_mentioned,
    brand_position,
    competitors_mentioned,
    directories_mentioned,
    answer_type
):
    score = 0

    if brand_mentioned:
        score += 8

    if brand_position is not None:
        if brand_position == 1:
            score += 5
        elif brand_position <= 3:
            score += 4
        elif brand_position <= 5:
            score += 2

    if not competitors_mentioned:
        score += 3

    if not directories_mentioned:
        score += 2

    if answer_type == "brand-led":
        score += 5
    elif answer_type == "list-led":
        score += 2
    elif answer_type == "directory-led":
        score += 1

    return min(score, 20)


def simulate_ai_answer(query, company_name, model="gpt-4.1-mini"):
    prompt = f"""
You are an AI search assistant.

Answer the user's query naturally and helpfully.
If relevant, mention real companies, brands, or providers.
If you mention multiple providers, prefer a short bullet list.
Keep the answer concise and useful.

User query: {query}
"""

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful AI search assistant."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

    answer = response.choices[0].message.content or ""
    clean_answer = normalize_text(answer)

    brand_mentioned = detect_brand_mentioned(clean_answer, company_name)
    brand_position = detect_brand_position(answer, company_name)

    entity_data = extract_named_entities(answer, company_name)
    competitors_mentioned = entity_data["competitors_mentioned"]
    directories_mentioned = entity_data["directories_mentioned"]
    media_mentioned = entity_data["media_mentioned"]

    answer_type = classify_answer_type(
        answer=answer,
        brand_name=company_name,
        competitors=competitors_mentioned,
        directories=directories_mentioned,
        media=media_mentioned
    )

    score = score_ai_visibility(
        brand_mentioned=brand_mentioned,
        brand_position=brand_position,
        competitors_mentioned=competitors_mentioned,
        directories_mentioned=directories_mentioned,
        answer_type=answer_type
    )

    return {
        "engine": "chatgpt",
        "query": query,
        "brand_name": company_name,
        "brand_mentioned": brand_mentioned,
        "brand_position": brand_position,
        "competitors_mentioned": competitors_mentioned,
        "directories_mentioned": directories_mentioned,
        "media_mentioned": media_mentioned,
        "answer_type": answer_type,
        "answer": clean_answer,
        "score": score,
    }


def run_ai_answer_test(queries_to_test, business_profile, model="gpt-4.1-mini"):
    company_name = business_profile.get("title", "")
    results = []

    for q in queries_to_test:
        result = simulate_ai_answer(
            query=q,
            company_name=company_name,
            model=model
        )
        results.append(result)

    return results


if __name__ == "__main__":
    business_profile = {"title": "Chung Will Writing Services"}

    queries_to_test = [
        "best will writing services in singapore",
        "which company provides will writing in singapore",
        "affordable estate planning services singapore"
    ]

    ai_answer_results = run_ai_answer_test(queries_to_test, business_profile)

    print("\nAI ANSWER TEST:")
    for row in ai_answer_results:
        print("-", row["query"])
        print("  Brand mentioned:", row["brand_mentioned"])
        print("  Brand position:", row["brand_position"])
        print("  Competitors:", row["competitors_mentioned"])
        print("  Directories:", row["directories_mentioned"])
        print("  Media:", row["media_mentioned"])
        print("  Answer type:", row["answer_type"])
        print("  Score:", row["score"])
        print("  Answer:", row["answer"])
        print()