import json
import os

QUEUE_FILE = os.path.join("data", "content_queue.json")
USER_ID_TO_ASSIGN = 1


def main():
    if not os.path.exists(QUEUE_FILE):
        print("Queue file not found:", QUEUE_FILE)
        return

    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        items = json.load(f)

    if not isinstance(items, list):
        print("Queue file is not a list. Aborting.")
        return

    updated = 0

    for item in items:
        if item.get("user_id") is None:
            item["user_id"] = USER_ID_TO_ASSIGN
            updated += 1

        if "updated_at" not in item:
            item["updated_at"] = item.get("created_at")

    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)

    print(f"Done. Updated {updated} queue items.")


if __name__ == "__main__":
    main()