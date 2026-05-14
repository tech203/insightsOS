"""
GSC + GA → action recommendations.

Reads the cached payloads on a workspace's GoogleSearchConsoleConnection
(populated by /integrations/gsc/sync and /integrations/ga/sync) and
turns them into _make_action-shaped recommendations that flow into
the Growth Calendar. Tagged category_tag="google_data" so the UI
can pill them apart from generic recs.

Heuristics intentionally simple — these are entry-points to deeper
investigation, not auto-fix prescriptions:

  * Top query position > 10 with non-trivial impressions
      → "Build content for this query" (rank-but-not-on-page-1)
  * Top page CTR < industry-avg-ish (1.5%) with > 500 impressions
      → "Tighten title + meta for this page"
  * Top page in GA: high sessions but low engaged_sessions ratio
      → "Improve engagement on this page" (UX / content quality)

We cap at 5 recs total so the calendar doesn't get flooded.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from action_engine import _make_action


MAX_RECS_PER_SOURCE = 3
TOTAL_CAP = 5
MIN_IMPRESSIONS_FOR_QUERY_REC = 200
MIN_IMPRESSIONS_FOR_PAGE_CTR_REC = 500
LOW_CTR_THRESHOLD = 0.015
LOW_RANK_THRESHOLD = 10.0
LOW_ENGAGEMENT_RATIO = 0.55


def _tag(action: Dict[str, Any]) -> Dict[str, Any]:
    action["category_tag"] = "google_data"
    return action


def _gsc_query_recommendations(
    summary: Dict[str, Any], limit: int
) -> List[Dict[str, Any]]:
    """Queries with strong impressions but weak ranking — clear sign
    we have demand but the page isn't quite there yet."""
    rows = summary.get("top_queries") or []
    out: List[Dict[str, Any]] = []
    for row in rows:
        impressions = float(row.get("impressions") or 0)
        position = float(row.get("position") or 99)
        if impressions < MIN_IMPRESSIONS_FOR_QUERY_REC:
            continue
        if position <= LOW_RANK_THRESHOLD:
            continue
        query = (row.get("keys") or [None])[0] or "an emerging query"
        clicks = int(row.get("clicks") or 0)
        out.append(
            _tag(
                _make_action(
                    category="content_gap",
                    priority="high" if impressions >= 1000 else "medium",
                    title=f'Build content for "{query[:60]}" (Google avg pos {position:.1f})',
                    issue=(
                        f"Google shows you {int(impressions)} impressions on this query "
                        f"but only {clicks} clicks — you're ranking on page {int(position // 10) + 1}, "
                        "not page 1."
                    ),
                    why_it_matters=(
                        "Queries that already pull impressions are warmest demand. "
                        "Closing the rank gap is much cheaper than capturing brand-new intent."
                    ),
                    recommended_fix=(
                        f'Write or expand a focused page targeting "{query}" — '
                        "answer the search intent directly in the first 100 words, "
                        "add an FAQ block, and link from related pages with the "
                        "query as anchor text."
                    ),
                    linked_query=query,
                    suggested_content_type="guide",
                    impact_score=14 if impressions >= 1000 else 11,
                    difficulty="medium",
                )
            )
        )
        if len(out) >= limit:
            break
    return out


def _gsc_page_ctr_recommendations(
    summary: Dict[str, Any], limit: int
) -> List[Dict[str, Any]]:
    """Pages that get plenty of impressions but very low CTR — usually
    a title-tag + meta-description fix."""
    rows = summary.get("top_pages") or []
    out: List[Dict[str, Any]] = []
    for row in rows:
        impressions = float(row.get("impressions") or 0)
        ctr = float(row.get("ctr") or 0)
        if impressions < MIN_IMPRESSIONS_FOR_PAGE_CTR_REC:
            continue
        if ctr >= LOW_CTR_THRESHOLD:
            continue
        page = (row.get("keys") or [None])[0] or "this page"
        clicks = int(row.get("clicks") or 0)
        out.append(
            _tag(
                _make_action(
                    category="refresh_existing",
                    priority="medium",
                    title=f"Tighten title + meta for {page[:70]} ({ctr * 100:.1f}% CTR)",
                    issue=(
                        f"Google served this page {int(impressions)} times but only "
                        f"{clicks} clicked — CTR is {ctr * 100:.2f}%, well below the "
                        "1.5%+ you'd expect for the visibility it has."
                    ),
                    why_it_matters=(
                        "Low CTR at high impressions almost always means the title tag "
                        "or meta description doesn't match what searchers want. "
                        "Updating those is one of the cheapest wins available."
                    ),
                    recommended_fix=(
                        "Open this page, rewrite the title to lead with the user's "
                        "intent (not the brand), and rewrite the meta description "
                        "to spell out the unique value in <150 chars. Re-publish "
                        "and let Google re-crawl."
                    ),
                    linked_query=page,
                    impact_score=10,
                    difficulty="easy",
                )
            )
        )
        if len(out) >= limit:
            break
    return out


def _ga_engagement_recommendations(
    payload: Dict[str, Any], limit: int
) -> List[Dict[str, Any]]:
    """Pages with traffic but poor engagement — UX / content quality
    issue, not a discoverability one."""
    rows = payload.get("top_pages") or []
    out: List[Dict[str, Any]] = []
    for row in rows:
        sessions = float(row.get("sessions") or 0)
        engaged = float(row.get("engagedSessions") or 0)
        if sessions < 100:
            continue
        ratio = engaged / sessions if sessions else 0
        if ratio >= LOW_ENGAGEMENT_RATIO:
            continue
        page = row.get("pagePath") or "this page"
        out.append(
            _tag(
                _make_action(
                    category="refresh_existing",
                    priority="medium",
                    title=f"Improve engagement on {page[:70]} ({ratio * 100:.0f}% engaged)",
                    issue=(
                        f"GA shows {int(sessions)} sessions on this page but only "
                        f"{int(engaged)} were engaged ({ratio * 100:.0f}%) — visitors "
                        "land but don't stick."
                    ),
                    why_it_matters=(
                        "Low engagement on a high-traffic page means the page is "
                        "drawing visits but not earning attention. AI engines are "
                        "increasingly factoring engagement into trust signals."
                    ),
                    recommended_fix=(
                        "Audit this page's first scroll: does the headline match "
                        "the search intent? Is there a clear next action above "
                        "the fold? Add a TL;DR block, sharpen the value-prop, "
                        "and consider a more specific intro paragraph."
                    ),
                    linked_query=page,
                    impact_score=9,
                    difficulty="medium",
                )
            )
        )
        if len(out) >= limit:
            break
    return out


def build_google_data_recommendations(
    *,
    gsc_payload: Optional[Dict[str, Any]] = None,
    ga_payload: Optional[Dict[str, Any]] = None,
    cap: int = TOTAL_CAP,
) -> List[Dict[str, Any]]:
    """Combine GSC + GA recs into a ranked list capped at `cap`."""
    out: List[Dict[str, Any]] = []
    if gsc_payload:
        out.extend(_gsc_query_recommendations(gsc_payload, MAX_RECS_PER_SOURCE))
        out.extend(_gsc_page_ctr_recommendations(gsc_payload, MAX_RECS_PER_SOURCE))
    if ga_payload:
        out.extend(_ga_engagement_recommendations(ga_payload, MAX_RECS_PER_SOURCE))
    # Sort by impact_score desc, then by priority. Cap at the limit.
    out.sort(
        key=lambda a: (
            -int(a.get("impact_score") or 0),
            {"high": 0, "medium": 1, "low": 2}.get(a.get("priority"), 99),
        )
    )
    return out[:cap]
