from app import app, db, User, Wallet
from werkzeug.security import generate_password_hash


test_users = [
    {
        "email": "free-test@example.com",
        "name": "Free Test",
        "password": "test12345",
        "plan": "free",
        "credits": 3,
    },
    {
        "email": "pro-test@example.com",
        "name": "Pro Test",
        "password": "test12345",
        "plan": "pro",
        "credits": 20,
    },
    {
        "email": "growth-test@example.com",
        "name": "Growth Test",
        "password": "test12345",
        "plan": "growth",
        "credits": 75,
    },
]


with app.app_context():
    for data in test_users:
        existing = User.query.filter_by(email=data["email"]).first()

        if existing:
            print(f"Already exists: {data['email']}")

            existing.name = data["name"]
            existing.password_hash = generate_password_hash(data["password"])
            existing.plan = data["plan"]
            existing.role = "user"

            if not existing.wallet:
                wallet = Wallet(
                    user_id=existing.id,
                    balance=data["credits"],
                )
                db.session.add(wallet)
            else:
                existing.wallet.balance = data["credits"]

            continue

        user = User(
            email=data["email"],
            name=data["name"],
            password_hash=generate_password_hash(data["password"]),
            plan=data["plan"],
            role="user",
        )

        db.session.add(user)
        db.session.flush()

        wallet = Wallet(
            user_id=user.id,
            balance=data["credits"],
        )

        db.session.add(wallet)

        print(
            f"Created: {data['email']} | "
            f"plan={data['plan']} | "
            f"password={data['password']}"
        )

    db.session.commit()

print("Done creating/updating test users.")