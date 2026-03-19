import requests
from bs4 import BeautifulSoup
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def analyze_entity_signals(url):

    try:
        response = requests.get(url, verify=False, timeout=10)
        html = response.text
    except:
        return {}

    soup = BeautifulSoup(html, "html.parser")

    schema_types = []

    for script in soup.find_all("script", type="application/ld+json"):
        text = script.get_text()

        if "Organization" in text:
            schema_types.append("Organization")

        if "LocalBusiness" in text:
            schema_types.append("LocalBusiness")

    address_found = False

    if soup.find(string=lambda x: x and "singapore" in x.lower()):
        address_found = True

    entity_score = 0

    if "Organization" in schema_types:
        entity_score += 5

    if "LocalBusiness" in schema_types:
        entity_score += 5

    if address_found:
        entity_score += 5

    entity_score = min(entity_score, 15)

    return {
        "schema_types": schema_types,
        "address_signal": address_found,
        "entity_score": entity_score
    }


if __name__ == "__main__":

    result = analyze_entity_signals("https://example.com")

    print(result)