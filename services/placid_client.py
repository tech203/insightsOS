"""
Placid REST API client.

Placid is a templated image-generation service. You design templates in their
web UI (each layer has a stable name like "headline", "subhead", "image"),
then POST to /api/rest/images with template_uuid + layer values to render
a finished image.

Docs: https://placid.app/docs/2.0/rest

The client is intentionally tiny: build a request, send it, retry briefly
while Placid renders the image, return the final image_url.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import requests


PLACID_BASE = "https://api.placid.app/api/rest"
DEFAULT_TIMEOUT = 30
RENDER_POLL_INTERVAL = 2  # seconds between polls when status is "queued"
RENDER_MAX_POLLS = 12  # ~24 seconds before we give up

logger = logging.getLogger(__name__)


class PlacidConfigError(Exception):
    """Raised when Placid env vars are missing or look like placeholders."""


class PlacidAPIError(Exception):
    """Raised when Placid returns a non-2xx status."""


def is_placid_configured() -> bool:
    token = os.getenv("PLACID_API_TOKEN")
    return bool(token) and not token.startswith("your_")


class PlacidClient:
    """Thin wrapper around Placid's image-generation REST API."""

    def __init__(self, api_token: Optional[str] = None):
        self.api_token = api_token or os.getenv("PLACID_API_TOKEN")
        if not self.api_token or self.api_token.startswith("your_"):
            raise PlacidConfigError(
                "PLACID_API_TOKEN is not set or still has the placeholder value."
            )

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> Dict[str, Any]:
        url = f"{PLACID_BASE}{path}"
        resp = requests.get(url, headers=self._headers(), timeout=DEFAULT_TIMEOUT)
        if resp.status_code >= 400:
            raise PlacidAPIError(f"GET {path} → {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{PLACID_BASE}{path}"
        resp = requests.post(
            url, headers=self._headers(), json=payload, timeout=DEFAULT_TIMEOUT
        )
        if resp.status_code >= 400:
            raise PlacidAPIError(
                f"POST {path} → {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    def generate_image(
        self,
        template_uuid: str,
        layers: Dict[str, Dict[str, Any]],
        wait: bool = True,
    ) -> Dict[str, Any]:
        """Render an image from a template.

        Args:
          template_uuid: The Placid template UUID.
          layers: Map of layer_name → {"text": ..., "image": ..., etc.}
                  Layer names are defined in the Placid template editor.
          wait: If True, poll until status=="finished" or RENDER_MAX_POLLS
                exhausted. If False, return the initial response (may have
                status="queued" with no image_url yet).

        Returns:
          {
            "id": "...",
            "status": "finished" | "queued" | "error",
            "image_url": "https://placid.app/...png",  # when finished
            ...
          }
        """
        if not template_uuid:
            raise PlacidConfigError("template_uuid is required.")

        payload = {
            "template_uuid": template_uuid,
            "layers": layers,
        }

        result = self._post("/images", payload)
        if not wait:
            return result

        # Placid returns 202 with status=queued initially, then we poll.
        status = result.get("status")
        image_id = result.get("id")
        if status == "finished" and result.get("image_url"):
            return result

        if not image_id:
            return result

        for _ in range(RENDER_MAX_POLLS):
            time.sleep(RENDER_POLL_INTERVAL)
            polled = self._get(f"/images/{image_id}")
            status = polled.get("status")
            if status in {"finished", "error"}:
                return polled

        return polled
