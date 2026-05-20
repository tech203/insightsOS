"""Initialize the configured application database."""

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app, db


def main() -> None:
    with app.app_context():
        db.create_all()
    print("Database tables are initialized.")


if __name__ == "__main__":
    main()
