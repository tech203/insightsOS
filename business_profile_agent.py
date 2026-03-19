import requests
from bs4 import BeautifulSoup
import urllib3
import re

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def extract_business_profile(url):

    try:
        response = requests.get(url, verify=False, timeout=10)
        html = response.text
    except:
        return {}

    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.string if soup.title else ""

    meta_description = ""
    desc_tag = soup.find("meta", attrs={"name": "description"})
    if desc_tag:
        meta_description = desc_tag.get("content")

    text = soup.get_text(" ", strip=True)

    email = None
    phone = None

    email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    if email_match:
        email = email_match.group(0)

    phone_match = re.search(r"\+?\d[\d\s\-]{7,}", text)
    if phone_match:
        phone = phone_match.group(0)

    services = []

    keywords = [
        "renovation",
        "design",
        "consultation",
        "installation",
        "contractor",
        "service"
    ]

    for word in keywords:
        if word in text.lower():
            services.append(word)

    return {
        "title": title,
        "description": meta_description,
        "email": email,
        "phone": phone,
        "services_detected": list(set(services))
    }


if __name__ == "__main__":

    result = extract_business_profile("https://example.com")

    print(result)