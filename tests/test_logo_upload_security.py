"""Logo upload security regression tests.

Two upload endpoints accept user file uploads:

  POST /settings/white-label/upload-logo  → stored as agency_logos/...
  POST /client/<id>/upload-logo           → stored as workspace_logos/...

Both are served back from the same origin (or a public S3 URL with
the file's claimed MIME type). If SVG is in the allowlist, an
attacker can upload <svg><script>alert(document.cookie)</script></svg>
and trigger stored XSS when any user views the logo.

These tests assert:
1. SVG uploads are rejected on both endpoints (extension allowlist).
2. The storage layer's _guess_content_type() never returns
   image/svg+xml — defence in depth so a legacy .svg filename can't
   be served with a script-executing MIME type.
3. PNG/JPG/WEBP uploads still work (we didn't break the happy path).
"""
import io
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import patch
from werkzeug.security import generate_password_hash

from app import app as flask_app, db, User, Wallet, Client
from services.storage import LogoStorage


@pytest.fixture(autouse=True)
def _isolate_storage(tmp_path, monkeypatch):
    """Force local-disk storage in a temp dir so tests never hit S3
    (real AWS creds in .env would otherwise fight with the test)."""
    monkeypatch.setenv("S3_BUCKET", "")
    monkeypatch.setattr(LogoStorage, "LOCAL_BASE", str(tmp_path), raising=False)
    yield


# A real SVG payload that would trigger XSS if rendered.
MALICIOUS_SVG = (
    b'<?xml version="1.0"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">\n'
    b'  <script>alert(document.cookie)</script>\n'
    b'  <rect width="100" height="100" fill="red"/>\n'
    b'</svg>\n'
)

# Minimal valid 1×1 PNG so happy-path tests have something real to upload.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63f8cffa1f00040301027f0bb6c40000000049454e44ae426082"
)


@pytest.fixture
def logged_in_user(app_ctx):
    u = User(
        email="upload@test.com",
        password_hash=generate_password_hash("xx"),
        name="Uploader",
        plan="growth",
        email_verified_at=datetime.now(timezone.utc),
    )
    db.session.add(u)
    db.session.flush()
    u.wallet = Wallet(user_id=u.id, balance=100)
    db.session.add(u.wallet)
    ws = Client(
        slug="up-co", user_id=u.id, name="Up Co",
        website="https://up.example.com",
        website_normalized="up.example.com",
    )
    db.session.add(ws)
    db.session.commit()

    c = flask_app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(u.id)
        s["_fresh"] = True
    return c, u, ws


def _post_logo(client, url, *, filename: str, content: bytes):
    return client.post(
        url,
        data={"logo": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
        follow_redirects=False,
    )


def test_agency_logo_rejects_svg_by_extension(logged_in_user):
    c, u, _ = logged_in_user
    resp = _post_logo(
        c, "/settings/white-label/upload-logo",
        filename="evil.svg", content=MALICIOUS_SVG,
    )
    # Should redirect with a flash, not store the file.
    assert resp.status_code == 302
    db.session.refresh(u)
    assert u.agency_logo_filename is None, (
        "SVG upload was accepted: agency_logo_filename was set to "
        f"{u.agency_logo_filename!r}"
    )


def test_workspace_logo_rejects_svg_by_extension(logged_in_user):
    c, _, ws = logged_in_user
    resp = _post_logo(
        c, f"/client/{ws.id}/upload-logo",
        filename="evil.svg", content=MALICIOUS_SVG,
    )
    assert resp.status_code == 302
    db.session.refresh(ws)
    assert ws.logo_filename is None, (
        "SVG upload was accepted: workspace logo_filename was set to "
        f"{ws.logo_filename!r}"
    )


@pytest.mark.parametrize("disguised_name", [
    "evil.SVG",          # uppercase
    "evil.Svg",          # mixed case
    "evil.svg.png.svg",  # double extension
])
def test_agency_logo_rejects_svg_case_variants(logged_in_user, disguised_name):
    c, u, _ = logged_in_user
    resp = _post_logo(
        c, "/settings/white-label/upload-logo",
        filename=disguised_name, content=MALICIOUS_SVG,
    )
    assert resp.status_code == 302
    db.session.refresh(u)
    assert u.agency_logo_filename is None, (
        f"SVG variant {disguised_name!r} was accepted"
    )


def test_storage_guess_content_type_never_returns_svg():
    """Defence in depth: the storage layer must not serve any filename
    with image/svg+xml even if a legacy .svg slipped through earlier."""
    s = LogoStorage()
    for name in ["legacy.svg", "evil.svg", "x.SVG", "y.svG"]:
        ct = s._guess_content_type(name)
        assert "svg" not in ct.lower(), (
            f"Storage layer would serve {name!r} with content-type {ct!r} "
            f"— browsers would execute embedded <script>"
        )
        # .svg should fall through to octet-stream (browser downloads).
        if name.lower().endswith(".svg"):
            assert ct == "application/octet-stream"


def test_png_upload_still_works(logged_in_user):
    """Sanity check: we didn't accidentally block legitimate uploads."""
    c, u, _ = logged_in_user
    resp = _post_logo(
        c, "/settings/white-label/upload-logo",
        filename="brand.png", content=TINY_PNG,
    )
    assert resp.status_code == 302
    db.session.refresh(u)
    assert u.agency_logo_filename is not None, (
        "PNG upload was rejected — likely a regression in the allowlist"
    )
    assert u.agency_logo_filename.endswith(".png")


def test_s3_upload_retries_without_acl_when_bucket_disallows():
    """Modern S3 buckets (created post-April 2023, default Bucket Owner
    Enforced) reject ACL: public-read with AccessControlListNotSupported.
    Storage layer must catch that and retry without the ACL — otherwise
    every logo upload silently fails with "Could not save the logo"."""
    s = LogoStorage()

    class _FakeS3:
        def __init__(self):
            self.calls = []

        def upload_fileobj(self, file_obj, bucket, key, ExtraArgs=None):
            self.calls.append(dict(ExtraArgs or {}))
            if "ACL" in (ExtraArgs or {}):
                # First call with ACL — modern bucket rejects.
                raise Exception(
                    "An error occurred (AccessControlListNotSupported) when "
                    "calling the PutObject operation: The bucket does not allow ACLs"
                )
            # Second call without ACL — succeeds.

    fake = _FakeS3()
    file_obj = io.BytesIO(TINY_PNG)
    file_obj.filename = "brand.png"

    with patch.dict(os.environ, {"S3_BUCKET": "my-test-bucket"}, clear=False):
        with patch("services.storage._s3_client", return_value=fake):
            # Should not raise — fallback path should kick in.
            s.save("agency_logos", "brand.png", file_obj)

    assert len(fake.calls) == 2, (
        f"Expected 2 upload attempts (with-ACL → fallback), got {len(fake.calls)}: {fake.calls}"
    )
    assert "ACL" in fake.calls[0]
    assert "ACL" not in fake.calls[1], (
        "Second attempt should drop the ACL — the bucket rejected it."
    )
