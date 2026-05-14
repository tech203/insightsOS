"""
Stale content detection.

Scans a workspace's published content (Webflow exports + published
queue items) and surfaces items that haven't been refreshed in a long
time as `_make_action`-compatible recommendations. Those flow into
`recommended_actions` and the Growth Calendar picks them up
automatically — turning the calendar into a self-feeding retention
loop instead of a one-shot output of the latest audit.

Tagged `category_tag="stale_refresh"` so the calendar UI can pill
them apart from generic content recs.
"""

from __future__ import annotations

from datetime import datetime
from dtutils import utcnow
from typing import Any, Dict, Iterable, List, Optional

from action_engine import _make_action


DEFAULT_STALE_THRESHOLD_DAYS = 60
HARD_STALE_THRESHOLD_DAYS = 120
MAX_RECOMMENDATIONS = 5


def _days_since(when: Optional[datetime]) -> Optional[int]:
    if not when:
        return None
    return (utcnow() - when).days


def _tag(action: Dict[str, Any]) -> Dict[str, Any]:
    action["category_tag"] = "stale_refresh"
    return action


def _content_type_label(content_type: str) -> str:
    return {
        "blog": "blog post",
        "faq": "FAQ",
        "service": "service page",
        "location": "location page",
        "page": "page",
    }.get((content_type or "").lower(), "content")


def find_stale_webflow_actions(
    *,
    webflow_exports: Iterable[Any],
    threshold_days: int = DEFAULT_STALE_THRESHOLD_DAYS,
    limit: int = MAX_RECOMMENDATIONS,
) -> List[Dict[str, Any]]:
    """Build refresh recommendations for published Webflow items that
    haven't been updated in `threshold_days`. We rank by age (oldest
    first) so the user always sees the most decayed content first."""
    candidates: List[Dict[str, Any]] = []
    for export in webflow_exports or []:
        status = (getattr(export, "status", "") or "").lower()
        if status not in {"published", "exported"}:
            continue
        # Use updated_at if it's been touched, otherwise created_at.
        last_touched = getattr(export, "updated_at", None) or getattr(
            export, "created_at", None
        )
        age = _days_since(last_touched)
        if age is None or age < threshold_days:
            continue

        content_type = getattr(export, "content_type", "") or "page"
        label = _content_type_label(content_type)
        # Bump priority for very stale content that's likely losing
        # AI-answer relevance as the topic moves on.
        priority = "high" if age >= HARD_STALE_THRESHOLD_DAYS else "medium"

        candidates.append(
            (
                age,
                _tag(
                    _make_action(
                        category="refresh_existing",
                        priority=priority,
                        title=f"Refresh stale {label} (last updated {age}d ago)",
                        issue=(
                            f"This {label} hasn't been touched in {age} days — "
                            "AI engines reward freshness, and topic relevance drifts."
                        ),
                        why_it_matters=(
                            "Stale published pages often slip in AI-answer rankings "
                            "as competitors publish more recent takes. Even small "
                            "updates (new examples, refreshed dates, expanded FAQ) "
                            "signal currency to crawlers."
                        ),
                        recommended_fix=(
                            f"Open this {label}, update the most time-sensitive "
                            "section, add 1–2 new paragraphs of recent context, "
                            "and re-publish. Target a >25% content delta."
                        ),
                        suggested_content_type=(
                            "service_page" if content_type == "service"
                            else "faq_page" if content_type == "faq"
                            else "guide"
                        ),
                        impact_score=12 if age >= HARD_STALE_THRESHOLD_DAYS else 9,
                        difficulty="easy",
                    )
                ),
            )
        )

    candidates.sort(key=lambda c: -c[0])  # oldest first
    return [a for _age, a in candidates[:limit]]


def find_stale_queue_actions(
    *,
    queue_items: Iterable[Dict[str, Any]],
    threshold_days: int = DEFAULT_STALE_THRESHOLD_DAYS,
    limit: int = MAX_RECOMMENDATIONS,
) -> List[Dict[str, Any]]:
    """Same idea but for queue items the workspace marked as
    published. Callers pass already-loaded dicts (the content_queue
    module returns dicts, not ORM rows, for back-compat with templates)."""
    candidates: List[Dict[str, Any]] = []
    for item in queue_items or []:
        status = (item.get("status") or "").lower()
        if status != "published":
            continue
        published_at_str = item.get("published_at") or item.get("created_at")
        published_at: Optional[datetime] = None
        if published_at_str:
            try:
                published_at = datetime.fromisoformat(
                    str(published_at_str).replace("Z", "")
                )
            except ValueError:
                published_at = None
        age = _days_since(published_at)
        if age is None or age < threshold_days:
            continue

        title = item.get("title") or item.get("target_query") or "Published content"
        priority = "high" if age >= HARD_STALE_THRESHOLD_DAYS else "medium"

        candidates.append(
            (
                age,
                _tag(
                    _make_action(
                        category="refresh_existing",
                        priority=priority,
                        title=f"Refresh: {title[:80]} ({age}d old)",
                        issue=(
                            f"Published {age} days ago and hasn't been refreshed. "
                            "AI answer relevance decays faster than traditional SEO."
                        ),
                        why_it_matters=(
                            "Content that ranked or got cited at publish time tends "
                            "to lose ground as competitors publish more recent takes. "
                            "A small refresh recovers that ground cheaply."
                        ),
                        recommended_fix=(
                            "Reopen this item, update timestamps + outdated examples, "
                            "expand the FAQ section, and re-publish. Aim for a "
                            "meaningful content delta, not just a date bump."
                        ),
                        linked_query=item.get("target_query") or "",
                        suggested_content_type=item.get("content_type") or "guide",
                        impact_score=10 if age >= HARD_STALE_THRESHOLD_DAYS else 8,
                        difficulty="easy",
                    )
                ),
            )
        )

    candidates.sort(key=lambda c: -c[0])
    return [a for _age, a in candidates[:limit]]


def find_stale_actions(
    *,
    webflow_exports: Optional[Iterable[Any]] = None,
    queue_items: Optional[Iterable[Dict[str, Any]]] = None,
    threshold_days: int = DEFAULT_STALE_THRESHOLD_DAYS,
) -> List[Dict[str, Any]]:
    """Combined: returns up to MAX_RECOMMENDATIONS stale actions across
    Webflow exports + queue items, oldest first across both sources."""
    wf = find_stale_webflow_actions(
        webflow_exports=webflow_exports or [],
        threshold_days=threshold_days,
        limit=MAX_RECOMMENDATIONS,
    )
    queue = find_stale_queue_actions(
        queue_items=queue_items or [],
        threshold_days=threshold_days,
        limit=MAX_RECOMMENDATIONS,
    )
    # Naive merge: just combine and cap. The two sources rarely
    # represent the same artefact (Webflow exports = published to CMS;
    # queue items = drafts marked published in our system).
    return (wf + queue)[:MAX_RECOMMENDATIONS]
