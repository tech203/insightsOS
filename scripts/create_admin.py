"""Create or update an admin user in the configured database."""

import os
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app, db, User, Wallet, utcnow


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        raise SystemExit(2)
    return value


def main() -> None:
    email = _required_env("ADMIN_EMAIL").lower()
    password = _required_env("ADMIN_PASSWORD")
    name = (os.getenv("ADMIN_NAME") or "Admin").strip() or "Admin"
    credits = int(os.getenv("ADMIN_CREDITS") or "999")

    with app.app_context():
        db.create_all()

        user = User.query.filter_by(email=email).first()
        created = user is None

        if user is None:
            user = User(
                email=email,
                name=name,
                password_hash=generate_password_hash(password),
                role="admin",
                plan="dev_unlimited",
            )
            db.session.add(user)
            db.session.flush()
        else:
            user.name = name
            user.password_hash = generate_password_hash(password)
            user.role = "admin"
            user.plan = "dev_unlimited"

        if hasattr(user, "email_verified_at") and user.email_verified_at is None:
            user.email_verified_at = utcnow()

        if not user.wallet:
            db.session.add(Wallet(user_id=user.id, balance=credits))
        else:
            user.wallet.balance = max(user.wallet.balance or 0, credits)

        db.session.commit()

    action = "Created" if created else "Updated"
    print(f"{action} admin user: {email}")


if __name__ == "__main__":
    main()
