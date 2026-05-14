"""
Google Analytics 4 (Data API) helpers.

Reuses the OAuth flow from gsc_client.py — Search Console + Analytics
share one Google grant so the user only sees one consent dialog. The
helpers here just call the GA4 endpoints with the access token already
on a GoogleSearchConsoleConnection row.

Two endpoints we use:

  GET  https://analyticsadmin.googleapis.com/v1beta/accountSummaries
       → list every GA4 property the user can access
  POST https://analyticsdata.googleapis.com/v1beta/properties/{id}:runReport
       → run a Data API report (sessions, users, conversions, top pages, etc.)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

# Drop-in replacement for the deprecated datetime.utcnow() — see
# dtutils.py for the migration rationale.
from dtutils import utcnow


ADMIN_API = "https://analyticsadmin.googleapis.com/v1beta"
DATA_API = "https://analyticsdata.googleapis.com/v1beta"
DEFAULT_TIMEOUT = 30


class GAAPIError(Exception):
    """Raised on any non-2xx response from Google Analytics."""


class GA4Client:
    """Tiny GA4 wrapper. One client per access token."""

    def __init__(self, access_token: str):
        self.access_token = access_token

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        h = {"Authorization": f"Bearer {self.access_token}"}
        if extra:
            h.update(extra)
        return h

    def list_properties(self) -> List[Dict[str, Any]]:
        """Flat list of GA4 properties across every account the user
        has access to. Each entry: {id, display_name, account_name}."""
        resp = requests.get(
            f"{ADMIN_API}/accountSummaries",
            headers=self._headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise GAAPIError(
                f"accountSummaries → {resp.status_code}: {resp.text[:200]}"
            )
        out: List[Dict[str, Any]] = []
        for account in (resp.json() or {}).get("accountSummaries") or []:
            account_name = account.get("displayName") or account.get("account") or ""
            for prop in account.get("propertySummaries") or []:
                # property is like "properties/123456789"
                pid = (prop.get("property") or "").split("/")[-1]
                out.append(
                    {
                        "id": pid,
                        "display_name": prop.get("displayName") or pid,
                        "account_name": account_name,
                        "property": prop.get("property"),
                    }
                )
        return out

    def run_report(
        self,
        *,
        property_id: str,
        start_date: str,
        end_date: str,
        metrics: List[str],
        dimensions: Optional[List[str]] = None,
        limit: int = 25,
    ) -> Dict[str, Any]:
        """Run a Data API report. Returns the raw response — caller
        unpacks rows / metricHeaders / dimensionHeaders as needed."""
        body: Dict[str, Any] = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "metrics": [{"name": m} for m in metrics],
            "limit": int(limit),
        }
        if dimensions:
            body["dimensions"] = [{"name": d} for d in dimensions]
        resp = requests.post(
            f"{DATA_API}/properties/{property_id}:runReport",
            headers=self._headers({"Content-Type": "application/json"}),
            json=body,
            timeout=DEFAULT_TIMEOUT,
        )
        if resp.status_code >= 400:
            raise GAAPIError(
                f"runReport → {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json() or {}


def summarize_property(client: GA4Client, *, property_id: str) -> Dict[str, Any]:
    """Pull the canonical 28-day overview for a GA4 property: site
    totals, top pages, top sources. Caller persists this on the
    connection so the dashboard renders HTTP-free between syncs."""
    from datetime import timedelta

    end = utcnow().date()
    start = end - timedelta(days=28)
    start_s = start.isoformat()
    end_s = end.isoformat()

    totals = client.run_report(
        property_id=property_id,
        start_date=start_s,
        end_date=end_s,
        metrics=["sessions", "totalUsers", "engagedSessions", "averageSessionDuration"],
        dimensions=None,
        limit=1,
    )
    top_pages = client.run_report(
        property_id=property_id,
        start_date=start_s,
        end_date=end_s,
        metrics=["sessions", "engagedSessions"],
        dimensions=["pagePath"],
        limit=10,
    )
    top_sources = client.run_report(
        property_id=property_id,
        start_date=start_s,
        end_date=end_s,
        metrics=["sessions"],
        dimensions=["sessionDefaultChannelGroup"],
        limit=10,
    )
    return {
        "property_id": property_id,
        "range_start": start_s,
        "range_end": end_s,
        "totals": _flatten_single_row(totals),
        "top_pages": _flatten_rows(top_pages),
        "top_sources": _flatten_rows(top_sources),
        "fetched_at": utcnow().isoformat(),
    }


def _flatten_single_row(report: Dict[str, Any]) -> Dict[str, Any]:
    """Turn the first row of a report into a {metric_name: value} dict."""
    metric_headers = [h.get("name") for h in (report.get("metricHeaders") or [])]
    rows = report.get("rows") or []
    if not rows:
        return {h: 0 for h in metric_headers}
    values = rows[0].get("metricValues") or []
    out: Dict[str, Any] = {}
    for i, header in enumerate(metric_headers):
        try:
            out[header] = float(values[i].get("value") or 0)
        except (ValueError, TypeError):
            out[header] = 0
    return out


def _flatten_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    dim_headers = [h.get("name") for h in (report.get("dimensionHeaders") or [])]
    metric_headers = [h.get("name") for h in (report.get("metricHeaders") or [])]
    out: List[Dict[str, Any]] = []
    for row in report.get("rows") or []:
        entry: Dict[str, Any] = {}
        for i, header in enumerate(dim_headers):
            entry[header] = (row.get("dimensionValues") or [{}])[i].get("value", "")
        for i, header in enumerate(metric_headers):
            try:
                entry[header] = float((row.get("metricValues") or [{}])[i].get("value") or 0)
            except (ValueError, TypeError):
                entry[header] = 0
        out.append(entry)
    return out
