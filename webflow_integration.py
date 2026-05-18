import json
import os
from dtutils import utcnow
from urllib.parse import urlencode

import requests


WEBFLOW_API_BASE = "https://api.webflow.com/v2"

DEFAULT_FIELD_MAP = {
    "page_type": "page-type",
    "meta_title": "meta-title",
    "meta_description": "meta-description",
    "hero_headline": "hero-headline",
    "hero_subtext": "hero-subtext",
    "sections_json": "sections-json",
    "aeo_focus": "aeo-focus",
    "darinsights_json": "darinsights-json",
}

OPTIONAL_FIELD_MAP = {
    "summary": "summary",
    "content": "content",
    "cta_text": "cta-text",
    "faq_schema": "faq-schema",
    "ai_prompt_target": "ai-prompt-target",
}

PLACEHOLDER_VALUES = {"", "...", "xxx", "your_token_here", "your_site_id", "your_collection_id"}


class WebflowConfigError(RuntimeError):
    pass


class WebflowAPIError(RuntimeError):
    pass


def is_webflow_configured():
    return get_webflow_setup_status()["configured"]


def get_webflow_setup_status():
    required_values = {
        "WEBFLOW_API_TOKEN": os.getenv("WEBFLOW_API_TOKEN"),
        "WEBFLOW_SITE_ID": os.getenv("WEBFLOW_SITE_ID"),
        "WEBFLOW_COLLECTION_ID": os.getenv("WEBFLOW_COLLECTION_ID"),
    }
    missing = [
        name
        for name, value in required_values.items()
        if not _has_real_value(value)
    ]

    return {
        "configured": not missing,
        "missing": missing,
        "publish_on_export": os.getenv("WEBFLOW_PUBLISH_ON_EXPORT", "false")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        "field_map": get_field_map() if not missing else DEFAULT_FIELD_MAP,
    }


def get_webflow_config():
    token = os.getenv("WEBFLOW_API_TOKEN")
    site_id = os.getenv("WEBFLOW_SITE_ID")
    collection_id = os.getenv("WEBFLOW_COLLECTION_ID")

    missing = [
        name
        for name, value in {
            "WEBFLOW_API_TOKEN": token,
            "WEBFLOW_SITE_ID": site_id,
            "WEBFLOW_COLLECTION_ID": collection_id,
        }.items()
        if not _has_real_value(value)
    ]

    if missing:
        raise WebflowConfigError(
            "Set real Webflow values for: " + ", ".join(missing) + "."
        )

    return {
        "token": token,
        "site_id": site_id,
        "collection_id": collection_id,
        "publish_on_export": os.getenv("WEBFLOW_PUBLISH_ON_EXPORT", "false")
        .strip()
        .lower()
        in {"1", "true", "yes"},
        "field_map": get_field_map(),
    }


def get_field_map():
    raw_map = os.getenv("WEBFLOW_PAGE_FIELD_MAP")
    if not raw_map:
        return DEFAULT_FIELD_MAP

    try:
        parsed = json.loads(raw_map)
    except json.JSONDecodeError as exc:
        raise WebflowConfigError("WEBFLOW_PAGE_FIELD_MAP must be valid JSON.") from exc

    field_map = DEFAULT_FIELD_MAP.copy()
    field_map.update({k: v for k, v in parsed.items() if v})
    return field_map


def export_project_to_webflow(project, pages, publish=None):
    config = get_webflow_config()
    schema_result = validate_webflow_collection_schema(config)
    if not schema_result["valid"]:
        raise WebflowConfigError(
            "Webflow collection is missing required CMS fields: "
            + ", ".join(schema_result["missing_fields"])
            + "."
        )
    config["available_field_slugs"] = set(schema_result.get("available_fields") or [])
    config["field_types"] = schema_result.get("field_types") or {}
    site_info = get_webflow_site_details(config)
    site_base_url = get_site_base_url(site_info)
    collection_slug = schema_result.get("collection_slug") or "darinsights-pages"

    should_publish = config["publish_on_export"] if publish is None else publish

    exported_pages = []
    item_ids = []

    for page in pages:
        existing_item_id = get_existing_webflow_item_id(config, page)

        if existing_item_id:
            result = update_webflow_page_item(
                config,
                project,
                page,
                existing_item_id,
            )
            action = "updated"
        else:
            result = create_webflow_page_item(config, project, page)
            action = "created"

        item_id = result.get("id") or _extract_first_item_id(result)
        item_id = item_id or existing_item_id

        if item_id:
            item_ids.append(item_id)

        exported_pages.append(
            {
                "local_page_id": page.id,
                "slug": page.slug,
                "webflow_item_id": item_id,
                "action": action,
                "live_url": build_live_item_url(
                    site_base_url,
                    collection_slug,
                    page.slug,
                ),
                "response": result,
            }
        )

    publish_result = None
    if should_publish and item_ids:
        publish_result = publish_webflow_items(config, item_ids)

    return {
        "site_id": config["site_id"],
        "collection_id": config["collection_id"],
        "site_base_url": site_base_url,
        "collection_slug": collection_slug,
        "published": bool(should_publish),
        "exported_at": utcnow().isoformat(timespec="seconds") + "Z",
        "schema": schema_result,
        "pages": exported_pages,
        "publish_result": publish_result,
    }


def verify_webflow_connection():
    config = get_webflow_config()
    url = f"{WEBFLOW_API_BASE}/sites/{config['site_id']}/collections"
    result = _webflow_request(config, "GET", url)
    collections = result.get("collections") or []
    matching_collection = next(
        (
            collection
            for collection in collections
            if collection.get("id") == config["collection_id"]
        ),
        None,
    )

    if not matching_collection:
        raise WebflowConfigError(
            "Connected to Webflow, but WEBFLOW_COLLECTION_ID was not found on this site."
        )

    schema_result = validate_webflow_collection_schema(config)

    return {
        "site_id": config["site_id"],
        "collection_id": config["collection_id"],
        "collection_name": matching_collection.get("displayName")
        or matching_collection.get("singularName")
        or "Webflow collection",
        "field_map": config["field_map"],
        "schema": schema_result,
    }


def validate_webflow_collection_schema(config=None):
    config = config or get_webflow_config()
    collection = get_webflow_collection_details(config)
    fields = collection.get("fields") or []
    available_field_slugs = {field.get("slug") for field in fields}
    required_field_slugs = {"name", "slug", *config["field_map"].values()}
    missing_field_slugs = sorted(required_field_slugs - available_field_slugs)

    return {
        "valid": not missing_field_slugs,
        "collection_name": collection.get("displayName")
        or collection.get("singularName")
        or "Webflow collection",
        "collection_slug": collection.get("slug"),
        "required_fields": sorted(required_field_slugs),
        "available_fields": sorted(slug for slug in available_field_slugs if slug),
        "field_types": {
            field.get("slug"): field.get("type")
            for field in fields
            if field.get("slug")
        },
        "missing_fields": missing_field_slugs,
    }


def get_webflow_collection_details(config):
    url = f"{WEBFLOW_API_BASE}/collections/{config['collection_id']}"
    return _webflow_request(config, "GET", url)


def get_webflow_site_details(config):
    url = f"{WEBFLOW_API_BASE}/sites/{config['site_id']}"
    return _webflow_request(config, "GET", url)


def get_site_base_url(site_info):
    custom_domains = site_info.get("customDomains") or []
    for domain in custom_domains:
        url = domain.get("url") or domain.get("domain")
        if url:
            return _ensure_https(url)

    short_name = site_info.get("shortName")
    if short_name:
        return f"https://{short_name}.webflow.io"

    preview_url = site_info.get("previewUrl")
    if preview_url:
        return preview_url.split("?")[0].rstrip("/")

    return ""


def build_live_item_url(site_base_url, collection_slug, item_slug):
    if not site_base_url:
        return ""
    return f"{site_base_url.rstrip('/')}/{collection_slug}/{item_slug}"


def _ensure_https(url):
    value = str(url).strip().rstrip("/")
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://{value}"


def create_webflow_page_item(config, project, page):
    url = (
        f"{WEBFLOW_API_BASE}/collections/"
        f"{config['collection_id']}/items/bulk?skipInvalidFiles=true"
    )
    payload = {
        "isArchived": False,
        "isDraft": True,
        "fieldData": build_webflow_field_data(
            config["field_map"],
            project,
            page,
            config.get("available_field_slugs"),
            config.get("field_types"),
        ),
    }

    return _webflow_request(config, "POST", url, payload)


def update_webflow_page_item(config, project, page, item_id):
    url = (
        f"{WEBFLOW_API_BASE}/collections/"
        f"{config['collection_id']}/items?skipInvalidFiles=true"
    )
    payload = {
        "items": [
            {
                "id": item_id,
                "isArchived": False,
                "isDraft": True,
                "fieldData": build_webflow_field_data(
                    config["field_map"],
                    project,
                    page,
                    config.get("available_field_slugs"),
                    config.get("field_types"),
                ),
            }
        ]
    }

    return _webflow_request(config, "PATCH", url, payload)


def publish_webflow_items(config, item_ids):
    url = f"{WEBFLOW_API_BASE}/collections/{config['collection_id']}/items/publish"
    return _webflow_request(config, "POST", url, {"itemIds": item_ids})


def get_existing_webflow_item_id(config, page):
    stored_item_id = ((page.page_json or {}).get("webflow") or {}).get("item_id")
    if stored_item_id:
        return stored_item_id

    matching_item = find_webflow_item_by_slug(config, page.slug)
    if matching_item:
        return matching_item.get("id")

    return None


def find_webflow_item_by_slug(config, slug):
    query = urlencode({"slug": slug, "limit": 100, "offset": 0})
    url = f"{WEBFLOW_API_BASE}/collections/{config['collection_id']}/items?{query}"
    result = _webflow_request(config, "GET", url)

    for item in result.get("items") or []:
        field_data = item.get("fieldData") or {}
        if field_data.get("slug") == slug:
            return item

    return None


def build_webflow_field_data(
    field_map,
    project,
    page,
    available_field_slugs=None,
    field_types=None,
):
    page_json = page.page_json or {}
    blueprint = project.blueprint_json or {}
    seo = page_json.get("seo") or {}
    hero = _first_section(page_json, "hero")
    sections = page_json.get("sections", []) or []
    available_field_slugs = set(available_field_slugs or [])
    field_types = field_types or {}

    values = {
        "page_type": page.page_type or page_json.get("page_type"),
        "meta_title": page_json.get("meta_title") or seo.get("og_title") or page.title,
        "meta_description": page_json.get("meta_description")
        or seo.get("meta_description")
        or seo.get("description")
        or seo.get("og_description"),
        "hero_headline": hero.get("headline"),
        "hero_subtext": hero.get("subtext"),
        "sections_json": json.dumps(sections, ensure_ascii=False),
        "aeo_focus": ", ".join(blueprint.get("aeo_focus") or []),
        "darinsights_json": json.dumps(page_json, ensure_ascii=False),
        "summary": hero.get("subtext")
        or seo.get("meta_description")
        or page_json.get("meta_description"),
        "content": build_rich_text_page_content(page_json),
        "cta_text": hero.get("primary_cta") or blueprint.get("primary_cta"),
        "faq_schema": build_faq_schema(page_json),
        "ai_prompt_target": ", ".join(blueprint.get("aeo_focus") or []),
    }

    field_data = {
        "name": page.title,
        "slug": page.slug,
    }

    for internal_key, webflow_field_slug in field_map.items():
        value = values.get(internal_key)
        if value not in (None, ""):
            field_data[webflow_field_slug] = value

    for internal_key, webflow_field_slug in OPTIONAL_FIELD_MAP.items():
        if available_field_slugs and webflow_field_slug not in available_field_slugs:
            continue
        if not _optional_field_is_compatible(webflow_field_slug, field_types):
            continue
        value = values.get(internal_key)
        if webflow_field_slug == "content" and value == "":
            field_data[webflow_field_slug] = ""
        elif value not in (None, ""):
            field_data[webflow_field_slug] = value

    return field_data


def build_rich_text_page_content(page_json):
    sections_html = []

    for section in page_json.get("sections") or []:
        section_type = section.get("type")
        if section_type == "hero":
            continue

        headline = section.get("headline")
        section_parts = []

        if headline:
            section_parts.append(
                f"<h2>{_escape_html(headline)}</h2>"
            )

        for key in ["subtext", "body"]:
            if section.get(key):
                section_parts.append(
                    f"<p>{_escape_html(section[key])}</p>"
                )

        items = section.get("items")
        if isinstance(items, list):
            item_parts = []
            for item in items:
                if isinstance(item, dict):
                    title = item.get("title")
                    description = item.get("description")
                    if title and description:
                        item_parts.append(
                            "<li>"
                            f"<strong>{_escape_html(title)}</strong><br>"
                            f"{_escape_html(description)}"
                            "</li>"
                        )
                    elif title:
                        item_parts.append(f"<li>{_escape_html(title)}</li>")
                    elif description:
                        item_parts.append(f"<li>{_escape_html(description)}</li>")
                elif item:
                    item_parts.append(f"<li>{_escape_html(item)}</li>")

            if item_parts:
                section_parts.append("<ul>" + "".join(item_parts) + "</ul>")

        if section_type == "faq":
            faq_parts = []
            # FAQ Q&A pairs live in section["items"] — that's what the
            # AI generator, the renderer, and the in-app edit form all
            # write. This used to read section["questions"], a key that
            # is never populated, so EVERY Webflow-exported page lost
            # all its FAQ content from the body (the FAQPage JSON-LD in
            # build_faq_schema was unaffected — it already reads
            # `items or questions` — so structured data survived but
            # the human-visible Q&A did not). `questions` kept only as
            # a defensive back-compat fallback, mirroring
            # build_faq_schema.
            for item in section.get("items") or section.get("questions") or []:
                if not isinstance(item, dict):
                    continue
                question = item.get("question")
                answer = item.get("answer")
                if question and answer:
                    faq_parts.append(
                        "<h3>"
                        f"{_escape_html(question)}"
                        "</h3>"
                        f"<p>{_escape_html(answer)}</p>"
                    )
            if faq_parts:
                section_parts.extend(faq_parts)

        if section_parts:
            sections_html.append("".join(section_parts))

    return "".join(sections_html)


def _optional_field_is_compatible(field_slug, field_types):
    field_type = field_types.get(field_slug)
    if not field_type:
        return True
    return field_type in {"PlainText", "RichText"}


def _escape_html(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_faq_schema(page_json):
    existing_schema = page_json.get("schema_json") or []
    for schema_item in existing_schema:
        if isinstance(schema_item, dict) and schema_item.get("@type") == "FAQPage":
            return json.dumps(schema_item, ensure_ascii=False)

    faq_items = []
    for section in page_json.get("sections") or []:
        if section.get("type") == "faq":
            for item in section.get("items") or section.get("questions") or []:
                question = item.get("question")
                answer = item.get("answer")
                if question and answer:
                    faq_items.append(
                        {
                            "@type": "Question",
                            "name": question,
                            "acceptedAnswer": {
                                "@type": "Answer",
                                "text": answer,
                            },
                        }
                    )

    if not faq_items:
        return ""

    return json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": faq_items,
        },
        ensure_ascii=False,
    )


def _first_section(page_json, section_type):
    for section in page_json.get("sections") or []:
        if section.get("type") == section_type:
            return section
    return {}


def _extract_first_item_id(result):
    items = result.get("items") or []
    if items and isinstance(items[0], dict):
        return items[0].get("id")
    return None


def _has_real_value(value):
    normalized = (value or "").strip().strip('"').strip("'").lower()
    return normalized not in PLACEHOLDER_VALUES


def _webflow_request(config, method, url, payload=None):
    headers = {
        "Authorization": f"Bearer {config['token']}",
        "Content-Type": "application/json",
    }
    response = requests.request(
        method,
        url,
        headers=headers,
        json=payload,
        timeout=30,
    )

    try:
        data = response.json()
    except ValueError:
        data = {"message": response.text}

    if response.status_code >= 400:
        message = data.get("message") or data.get("msg") or response.text
        raise WebflowAPIError(f"Webflow API error {response.status_code}: {message}")

    return data
