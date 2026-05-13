"""Seed a workspace for the help-screenshots walkthrough.

Creates an `Acme Bakery` workspace for `pro-test@example.com` (idempotent).
Also pre-fills brand context so the brand-context page screenshot has
real content.
"""

from app import app, db, User, Client, add_client
from datetime import datetime, timezone


EMAIL = "pro-test@example.com"
NAME = "Acme Bakery"
WEBSITE = "https://acmebakery.example.com"


def main() -> int:
    with app.app_context():
        user = User.query.filter_by(email=EMAIL).first()
        if not user:
            print(f"user not found: {EMAIL}")
            return 1

        existing = Client.query.filter_by(user_id=user.id, name=NAME).first()
        if existing:
            client = existing
            print(f"reusing client: id={client.id} slug={client.slug}")
        else:
            client_dict = add_client(
                {
                    "name": NAME,
                    "website": WEBSITE,
                    "industry": "Food & beverage",
                    "location": "Brooklyn, NY",
                    "owner_type": "company",
                    "notes": "Family-owned bakery, three locations.",
                },
                user_id=user.id,
            )
            client = Client.query.filter_by(slug=client_dict["slug"]).first()
            print(f"created client: id={client.id} slug={client.slug}")

        client.brand_audience = "Home bakers, neighborhood foodies, gift-givers."
        client.brand_services = "Custom celebration cakes, daily pastries, wedding desserts."
        client.brand_differentiators = "Sourdough starter passed down three generations; all butter, no shortcuts."
        client.brand_voice = "Warm, neighbourly, never corporate."
        client.brand_personality = "Friendly, dependable, proud of craft."
        client.brand_avoid = "Buzzwords, jargon, anything that sounds mass-produced."
        client.brand_primary_color = "#7C3AED"
        client.brand_secondary_color = "#F59E0B"
        client.brand_accent_color = "#10B981"
        client.brand_typography = "Serif headings, humanist sans body."
        client.brand_imagery_direction = "Warm interiors, hands at work, golden crust."
        client.brand_kit_updated_at = datetime.now(timezone.utc)

        db.session.commit()
        print(f"slug = {client.slug}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
