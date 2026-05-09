"""
Logo storage abstraction (workspace + agency logos).

Two backends behind one interface:

  * Local disk (default) — writes to static/uploads/{kind}/. Works
    out of the box, ideal for single-instance dev or self-hosted.
  * S3 — set S3_BUCKET (+ optional region / public base URL) and
    uploads land in s3://{bucket}/{kind}/{filename}. Required for
    multi-instance production deploys, since local-disk uploads
    don't survive a redeploy or instance restart.

Backend is chosen at call time via is_s3_configured(); the same
filename-on-DB pattern works for both. boto3 is lazy-imported so
deploys without it fall back gracefully.

Usage:
    from services.storage import logo_storage
    logo_storage.save("agency_logos", "agency-1-abc.png", file_obj)
    logo_storage.url("agency_logos", "agency-1-abc.png")  # → URL
    logo_storage.delete("agency_logos", "agency-1-abc.png")
"""

from __future__ import annotations

import logging
import os
from typing import Any, BinaryIO, Optional

logger = logging.getLogger(__name__)


def is_s3_configured() -> bool:
    """True when at minimum S3_BUCKET is set (real, not placeholder)."""
    bucket = (os.getenv("S3_BUCKET") or "").strip()
    return bool(bucket) and not bucket.startswith("your_")


def _s3_region() -> str:
    return (
        os.getenv("S3_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "us-east-1"
    )


def _s3_public_base() -> str:
    """Base URL for public reads. Override with S3_PUBLIC_BASE_URL when
    fronting via CloudFront / a CDN / a custom domain. Defaults to the
    AWS-hosted virtual-host URL."""
    override = (os.getenv("S3_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if override:
        return override
    bucket = os.getenv("S3_BUCKET")
    region = _s3_region()
    return f"https://{bucket}.s3.{region}.amazonaws.com"


def _s3_client():
    """Lazy-import boto3 so deploys without S3 don't pay the import
    cost. Returns None if boto3 isn't installed."""
    try:
        import boto3  # type: ignore
    except ImportError:
        logger.warning("boto3 isn't installed — falling back to local disk.")
        return None
    kwargs: dict[str, Any] = {"region_name": _s3_region()}
    endpoint = os.getenv("S3_ENDPOINT_URL")
    if endpoint:
        # Override for S3-compatible services (R2, Backblaze B2, MinIO).
        kwargs["endpoint_url"] = endpoint
    if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
        kwargs["aws_access_key_id"] = os.getenv("AWS_ACCESS_KEY_ID")
        kwargs["aws_secret_access_key"] = os.getenv("AWS_SECRET_ACCESS_KEY")
    return boto3.client("s3", **kwargs)


class LogoStorage:
    """Single-instance storage façade. Backend chosen per-call so flipping
    S3 on doesn't require an app restart."""

    LOCAL_BASE = os.path.join("static", "uploads")

    # `kind` is the subdirectory / S3 prefix. Matches the existing
    # filesystem layout: "workspace_logos" + "agency_logos".
    KNOWN_KINDS = {"workspace_logos", "agency_logos"}

    def _local_dir(self, kind: str) -> str:
        return os.path.join(self.LOCAL_BASE, kind)

    def save(self, kind: str, filename: str, file_obj: BinaryIO) -> None:
        """Write the upload either to S3 (when configured) or local disk."""
        if kind not in self.KNOWN_KINDS:
            raise ValueError(f"Unknown logo kind: {kind}")

        if is_s3_configured():
            client = _s3_client()
            if client:
                bucket = os.getenv("S3_BUCKET")
                key = f"{kind}/{filename}"
                content_type = self._guess_content_type(filename)
                # Rewind first — Werkzeug FileStorage can be partially
                # consumed after .stream operations elsewhere.
                try:
                    file_obj.seek(0)
                except (AttributeError, OSError):
                    pass
                client.upload_fileobj(
                    file_obj,
                    bucket,
                    key,
                    ExtraArgs={"ContentType": content_type, "ACL": "public-read"},
                )
                return

        # Local fallback.
        target_dir = self._local_dir(kind)
        os.makedirs(target_dir, exist_ok=True)
        full = os.path.join(target_dir, filename)
        # Werkzeug's FileStorage.save() handles the seek + chunked write
        # for us; raw file objects fall back to read+write.
        if hasattr(file_obj, "save"):
            file_obj.save(full)
        else:
            try:
                file_obj.seek(0)
            except (AttributeError, OSError):
                pass
            with open(full, "wb") as out:
                out.write(file_obj.read())

    def delete(self, kind: str, filename: str) -> None:
        """Best-effort delete on the active backend."""
        if not filename:
            return
        if is_s3_configured():
            client = _s3_client()
            if client:
                bucket = os.getenv("S3_BUCKET")
                key = f"{kind}/{filename}"
                try:
                    client.delete_object(Bucket=bucket, Key=key)
                    return
                except Exception as exc:
                    logger.warning("S3 delete failed for %s: %s", key, exc)
                    # fall through to local cleanup as well
        try:
            full = os.path.join(self._local_dir(kind), filename)
            if os.path.exists(full):
                os.remove(full)
        except OSError as exc:
            logger.warning("Local delete failed for %s: %s", filename, exc)

    def url(self, kind: str, filename: str) -> Optional[str]:
        """Public URL where the logo can be reached. None when filename
        is empty. Doesn't probe — assumes the file exists on whichever
        backend is active."""
        if not filename:
            return None
        if is_s3_configured():
            return f"{_s3_public_base()}/{kind}/{filename}"
        # Local — return a relative /static path; Flask serves it.
        return f"/static/uploads/{kind}/{filename}"

    @staticmethod
    def _guess_content_type(filename: str) -> str:
        ext = (filename.rsplit(".", 1)[-1] or "").lower()
        return {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "svg": "image/svg+xml",
            "webp": "image/webp",
            "gif": "image/gif",
        }.get(ext, "application/octet-stream")


# Module-level singleton — callers can import once and reuse.
logo_storage = LogoStorage()
