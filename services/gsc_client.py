"""
Google Search Console integration helpers.

Three surfaces:

1. OAuth — authorize URL builder, code → tokens exchange, refresh-token
   flow when the access token expires.
2. Search Analytics — pull aggregate KPIs (clicks, impressions, CTR,
   average position) and top queries / pages for a verified site.
3. Site list — list every property the connected Google account has
   verified, so the user can pick which one this workspace tracks.

Scope: `https://www.googleapis.com/auth/webmasters.readonly`. We never
write back to GSC; the connector is read-only by design.

Tokens live on the GoogleSearchConsoleConnection row in plain text for
MVP. Production deploys should encrypt at rest or move to a secrets
manager. Refresh tokens stay valid until the user revokes the grant
in their Google account.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests


AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEARCH_CONSOLE_API = "https://searchconsole.googleapis.com/v1"
# Both Search Console + Analytics requested in one consent so users
# only see one Google permission dialog. Analytics admin scope lists
# accounts/properties; analytics.readonly serves runReport queries.
DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/webmasters.readonly "
    "https://www.googleapis.com/auth/analytics.readonly"
)
DEFAULT_TIMEOUT = 30


class GSCConfigError(Exception):
    """Raised when GOOGLE_CLIENT_ID / SECRET aren't configured."""


class GSCAPIError(Exception):
    """Raised on any non-2xx response from Google."""


def is_gsc_configured() -> bool:
    """True only when both client id and secret are real (not placeholders)."""
    cid = os.getenv("GOOGLE_CLIENT_ID") or ""
    secret = os.getenv("GOOGLE_CLIENT_SECRET") or ""
    return (
        bool(cid)
        and bool(secret)
        and not cid.startswith("your_")
        and not secret.startswith("your_")
    )


def build_install_url(*, redirect_uri: str, state: str) -> str:
    """Build the Google OAuth consent URL the user gets redirected to.

    `access_type=offline` + `prompt=consent` ensure we always receive a
    refresh token — without those, repeat consents skip the refresh
    grant and we lose the ability to renew the access token."""
    cid = os.getenv("GOOGLE_CLIENT_ID")
    if not cid:
        raise GSCConfigError("GOOGLE_CLIENT_ID is not set.")
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": DEFAULT_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def exchange_code_for_token(*, code: str, redirect_uri: str) -> Dict[str, Any]:
    """Trade the auth code from the OAuth callback for access + refresh
    tokens. Google returns expires_in as seconds; the caller is
    responsible for converting it to an absolute datetime to store."""
    cid = os.getenv("GOOGLE_CLIENT_ID")
    secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not cid or not secret:
        raise GSCConfigError("Google OAuth credentials are not configured.")
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise GSCAPIError(f"Token exchange failed → {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """Use the long-lived refresh token to mint a new access token."""
    cid = os.getenv("GOOGLE_CLIENT_ID")
    secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not cid or not secret:
        raise GSCConfigError("Google OAuth credentials are not configured.")
    if not refresh_token:
        raise GSCAPIError("No refresh token available.")
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise GSCAPIError(f"Refresh failed → {resp.status_code}: {resp.text[:200]}")
    return resp.json()


class GSCClient:
    """Tiny Search Console wrapper. One client per (site, access_token)."""

    def __init__(self, access_token: str):
        self.access_token = access_token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def list_sites(self) -> List[Dict[str, Any]]:
        """Every property the authorised account has verified. Returned
        in Google's order; UI can sort by site URL."""
        resp = requests.get(
            f"{SEARCH_CONSOLE_API}/sites",
            headers=self._headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise GSCAPIError(f"list_sites → {resp.status_code}: {resp.text[:200]}")
        data = resp.json() or {}
        return data.get("siteEntry") or []

    def query_search_analytics(
        self,
        *,
        site_url: str,
        start_date: str,
        end_date: str,
        dimensions: Optional[List[str]] = None,
        row_limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """Run a Search Analytics query.

        `site_url` must be url-encoded by the caller side via the path
        — we encode here. Dimensions can be ['query'] for top queries,
        ['page'] for top pages, or empty for site-wide totals."""
        path = urllib.parse.quote(site_url, safe="")
        body: Dict[str, Any] = {
            "startDate": start_date,
            "endDate": end_date,
            "rowLimit": int(row_limit),
        }
        if dimensions:
            body["dimensions"] = dimensions
        resp = requests.post(
            f"{SEARCH_CONSOLE_API}/sites/{path}/searchAnalytics/query",
            headers={**self._headers(), "Content-Type": "application/json"},
            json=body,
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise GSCAPIError(
                f"searchAnalytics → {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json() or {}
        return data.get("rows") or []
