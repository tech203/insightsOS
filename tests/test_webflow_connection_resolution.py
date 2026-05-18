"""Per-client Webflow connection resolution.

Covers _resolve_client_db_id (slug / clients.id / junk -> clients.id)
and get_webflow_connection (client-scoped row preferred, user-default
fallback). Uses the real models against a throwaway SQLite file.

Note: SQLite does not enforce the users.id foreign key by default, so
Client / WebflowConnection rows are created without seeding a User.
"""

import os
import tempfile
import types

import pytest

app_module = pytest.importorskip("app")
flask_app = app_module.app
db = app_module.db
Client = app_module.Client
WebflowConnection = app_module.WebflowConnection

USER_ID = 1


@pytest.fixture
def ctx(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    flask_app.config["TESTING"] = True
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{path}"
    # Drop any engine cached from a previous test/config.
    getattr(db, "engines", {}).clear()

    monkeypatch.setattr(
        app_module, "current_user", types.SimpleNamespace(id=USER_ID)
    )

    with flask_app.app_context():
        db.create_all()
        try:
            yield
        finally:
            db.session.remove()
            db.drop_all()
            getattr(db, "engines", {}).clear()
    os.remove(path)


def _client(slug, user_id=USER_ID):
    row = Client(
        slug=slug,
        user_id=user_id,
        name=f"{slug} Inc",
        website=f"https://{slug}.example",
        website_normalized=f"{slug}.example",
    )
    db.session.add(row)
    db.session.flush()
    return row


def _conn(client_id, tag):
    row = WebflowConnection(
        user_id=USER_ID, client_id=client_id, site_id=f"site-{tag}"
    )
    row.api_token = f"token-{tag}"
    row.site_name = tag
    db.session.add(row)
    db.session.flush()
    return row


def test_resolve_slug_to_db_id(ctx):
    c = _client("acme")
    assert app_module._resolve_client_db_id("acme") == c.id


def test_resolve_is_idempotent_for_integer_ids(ctx):
    # The project-export path already passes clients.id directly.
    c = _client("acme")
    assert app_module._resolve_client_db_id(c.id) == c.id
    assert app_module._resolve_client_db_id(str(c.id)) == c.id


@pytest.mark.parametrize("ref", [None, "", "ghost-slug", "999999"])
def test_resolve_unresolvable_returns_none(ctx, ref):
    _client("acme")
    assert app_module._resolve_client_db_id(ref) is None


def test_resolve_is_scoped_to_current_user(ctx):
    other = _client("shared", user_id=USER_ID + 99)
    db.session.commit()
    # Slug exists but belongs to another user -> not resolved.
    assert app_module._resolve_client_db_id(other.slug) is None


def test_prefers_client_scoped_connection(ctx):
    c = _client("acme")
    _conn(c.id, "ACME")
    _conn(None, "DEFAULT")
    db.session.commit()

    by_slug = app_module.get_webflow_connection("acme")
    by_int = app_module.get_webflow_connection(c.id)
    assert by_slug.site_name == "ACME"
    assert by_int.site_name == "ACME"


def test_falls_back_to_user_default(ctx):
    c = _client("acme")
    _conn(None, "DEFAULT")  # no client-scoped row for acme
    db.session.commit()

    assert app_module.get_webflow_connection("acme").site_name == "DEFAULT"
    assert app_module.get_webflow_connection(None).site_name == "DEFAULT"
    assert app_module.get_webflow_connection("ghost").site_name == "DEFAULT"


def test_returns_none_when_no_connection_at_all(ctx):
    _client("acme")
    db.session.commit()
    assert app_module.get_webflow_connection("acme") is None
    assert app_module.get_webflow_connection(None) is None
