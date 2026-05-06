import argparse
from collections import defaultdict

from content_queue import get_queue_items, delete_queue_item


STATUS_RANK = {
    "published": 7,
    "ready": 6,
    "draft_generated": 5,
    "draft generated": 5,
    "brief_generated": 4,
    "brief generated": 4,
    "brief ready": 4,
    "in_progress": 3,
    "in-progress": 3,
    "pending": 2,
    "queued": 2,
    "": 1,
    None: 1,
}


def clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def normalize_query(value):
    return " ".join(clean_text(value).lower().split())


def status_score(item):
    status = normalize_query(item.get("status"))
    return STATUS_RANK.get(status, 1)


def has_saved_content(item):
    return bool(
        clean_text(item.get("content"))
        or clean_text(item.get("brief"))
        or clean_text(item.get("draft"))
    )


def title_quality_score(item):
    """
    Prefer more useful titles if status is tied.
    """
    title = clean_text(item.get("title")).lower()

    if title.startswith("draft:"):
        return 4
    if title.startswith("brief:"):
        return 3
    if title.startswith("suggested query:"):
        return 2
    if title:
        return 1
    return 0


def choose_item_to_keep(items):
    """
    Keep the most advanced item:
    1. highest status rank
    2. has saved content
    3. better title
    """
    return sorted(
        items,
        key=lambda item: (
            status_score(item),
            1 if has_saved_content(item) else 0,
            title_quality_score(item),
        ),
        reverse=True,
    )[0]


def cleanup_duplicates(user_id, client_id=None, apply=False):
    items = get_queue_items(
        client_id=client_id,
        user_id=user_id,
    )

    groups = defaultdict(list)

    for item in items:
        target_query = normalize_query(item.get("target_query"))

        if not target_query:
            continue

        key = (
            clean_text(item.get("client_id")),
            target_query,
        )

        groups[key].append(item)

    duplicate_groups = {
        key: group
        for key, group in groups.items()
        if len(group) > 1
    }

    if not duplicate_groups:
        print("No duplicate queue items found.")
        return

    total_to_delete = 0

    print("\nDuplicate groups found:\n")

    for key, group in duplicate_groups.items():
        client_key, query_key = key
        keep_item = choose_item_to_keep(group)

        print("=" * 80)
        print(f"Client: {client_key}")
        print(f"Query:  {query_key}")
        print(f"Keep:   {keep_item.get('id')} | {keep_item.get('status')} | {keep_item.get('title')}")

        for item in group:
            if item.get("id") == keep_item.get("id"):
                continue

            total_to_delete += 1
            print(f"Delete: {item.get('id')} | {item.get('status')} | {item.get('title')}")

            if apply:
                deleted = delete_queue_item(
                    item_id=item.get("id"),
                    user_id=user_id,
                )

                if deleted:
                    print("        deleted")
                else:
                    print("        delete failed")

    print("\nSummary")
    print("-" * 80)
    print(f"Duplicate groups: {len(duplicate_groups)}")
    print(f"Items to delete:  {total_to_delete}")

    if not apply:
        print("\nPreview only. Nothing was deleted.")
        print("Run again with --apply to delete duplicates.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Remove duplicate content queue items by client_id + target_query."
    )

    parser.add_argument(
        "--user-id",
        type=int,
        required=True,
        help="User ID to clean queue items for."
    )

    parser.add_argument(
        "--client-id",
        type=str,
        default=None,
        help="Optional workspace/client slug, e.g. astra-collective."
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete duplicate items. Without this, script only previews."
    )

    args = parser.parse_args()

    cleanup_duplicates(
        user_id=args.user_id,
        client_id=args.client_id,
        apply=args.apply,
    )