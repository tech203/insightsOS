import json
import os
from datetime import datetime

from app import app, db, Client, normalize_website, slugify

CLIENTS_FILE = os.path.join("data", "clients.json")


def get_unique_client_slug(user_id, name):
    base_slug = slugify(name) or "client"
    slug = base_slug
    counter = 2

    while Client.query.filter_by(user_id=user_id, slug=slug).first():
        slug = f"{base_slug}-{counter}"
        counter += 1

    return slug


def main():
    if not os.path.exists(CLIENTS_FILE):
        print("No clients.json found.")
        return

    with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
        clients = json.load(f)

    if not isinstance(clients, list):
        print("clients.json is not a list. Aborting.")
        return

    created = 0
    skipped = 0

    with app.app_context():
        db.create_all()

        for item in clients:
            user_id = item.get("user_id")
            name = (item.get("name") or "").strip()
            website = (item.get("website") or "").strip()

            if not user_id or not name or not website:
                skipped += 1
                continue

            existing = Client.query.filter_by(
                user_id=user_id,
                website_normalized=normalize_website(website)
            ).first()

            if existing:
                skipped += 1
                continue

            slug = item.get("id") or get_unique_client_slug(user_id, name)

            if Client.query.filter_by(user_id=user_id, slug=slug).first():
                slug = get_unique_client_slug(user_id, name)

            row = Client(
                slug=slug,
                user_id=user_id,
                name=name,
                website=website,
                website_normalized=normalize_website(website),
                industry=(item.get("industry") or "").strip() or None,
                location=(item.get("location") or "").strip() or None,
                owner_type=(item.get("owner_type") or "company").strip(),
                notes=(item.get("notes") or "").strip() or None,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )

            db.session.add(row)
            created += 1

        db.session.commit()

    print(f"Done. Created {created} clients, skipped {skipped}.")


if __name__ == "__main__":
    main()