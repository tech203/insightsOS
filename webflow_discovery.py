import argparse
import os
import sys

from dotenv import load_dotenv

from webflow_integration import (
    WEBFLOW_API_BASE,
    WebflowAPIError,
    WebflowConfigError,
    _webflow_request,
)


def get_token():
    token = os.getenv("WEBFLOW_API_TOKEN")
    if not token or token.strip() in {"", "..."}:
        raise WebflowConfigError("Set a real WEBFLOW_API_TOKEN in .env first.")
    return token.strip().strip('"').strip("'")


def request_with_token(path):
    config = {"token": get_token()}
    return _webflow_request(config, "GET", f"{WEBFLOW_API_BASE}{path}")


def list_sites():
    result = request_with_token("/sites")
    sites = result.get("sites") or []

    if not sites:
        print("No Webflow sites returned for this token.")
        return

    print("\nWebflow sites\n")
    for site in sites:
        site_id = site.get("id")
        display_name = site.get("displayName") or site.get("shortName") or "Untitled site"
        preview_url = site.get("previewUrl") or ""
        print(f"- {display_name}")
        print(f"  WEBFLOW_SITE_ID={site_id}")
        if preview_url:
            print(f"  Preview: {preview_url}")


def list_collections(site_id):
    if not site_id:
        raise WebflowConfigError("Pass --site-id to list collections.")

    result = request_with_token(f"/sites/{site_id}/collections")
    collections = result.get("collections") or []

    if not collections:
        print("No CMS collections returned for this site.")
        return

    print("\nWebflow collections\n")
    for collection in collections:
        collection_id = collection.get("id")
        display_name = (
            collection.get("displayName")
            or collection.get("singularName")
            or "Untitled collection"
        )
        print(f"- {display_name}")
        print(f"  WEBFLOW_COLLECTION_ID={collection_id}")


def main():
    parser = argparse.ArgumentParser(
        description="List Webflow site and CMS collection IDs for DarInsights."
    )
    parser.add_argument(
        "--site-id",
        help="When provided, list CMS collections for this Webflow site.",
    )
    args = parser.parse_args()

    load_dotenv(".env")

    try:
        if args.site_id:
            list_collections(args.site_id)
        else:
            list_sites()
    except (WebflowConfigError, WebflowAPIError) as exc:
        print(f"Webflow discovery failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
