"""
Growth Calendar — weekly recommendation planner.

Takes a client's existing `recommended_actions` (already produced by
action_engine.build_recommended_actions during audit) and the live
content queue, and lays them out across the next four weeks so the
user has a clear "what to ship this week" view.

This module is intentionally side-effect-free: it doesn't write to
any database. Persistence (scheduled_for, etc.) is layered on later;
for now the calendar is a derived view over existing state.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
PLAN_WEEKS = 4


def _clean(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() or default


def _start_of_week(d: date) -> date:
    """Monday of the week containing d."""
    return d - timedelta(days=d.weekday())


def _parse_date(value: Any) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    return None


def _week_label(week_start: date, today: date) -> str:
    today_week = _start_of_week(today)
    delta = (week_start - today_week).days // 7
    if delta == 0:
        return "This week"
    if delta == 1:
        return "Next week"
    if delta == -1:
        return "Last week"
    if delta > 0:
        return f"In {delta} weeks"
    return f"{abs(delta)} weeks ago"


def _bucket_priority(action: Dict[str, Any]) -> int:
    return PRIORITY_ORDER.get(_clean(action.get("priority"), "medium"), 1)


def _suggest_day_offset(priority: str, slot: int) -> int:
    """High-priority actions go earlier in the week; low-priority later."""
    if priority == "high":
        return min(slot, 2)
    if priority == "low":
        return min(4 + slot, 6)
    return min(2 + slot, 5)


def _action_to_card(action: Dict[str, Any], offset_in_week: int) -> Dict[str, Any]:
    return {
        "kind": "recommendation",
        "title": _clean(
            action.get("title") or action.get("recommended_action"),
            "Visibility action",
        ),
        "subtitle": _clean(
            action.get("recommended_action") or action.get("recommended_fix")
            or action.get("issue"),
            "",
        ),
        "linked_query": _clean(action.get("linked_query")),
        "content_type": _clean(action.get("suggested_content_type")),
        "priority": _clean(action.get("priority"), "medium"),
        "credits_required": action.get("credits_required") or 0,
        "category": _clean(action.get("category"), "growth"),
        "category_tag": _clean(action.get("category_tag")),
        "day_offset": offset_in_week,
    }


def _queue_to_card(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "queue",
        "id": item.get("id"),
        "title": _clean(item.get("title") or item.get("target_query"), "Queue item"),
        "subtitle": _clean(item.get("content_type"), ""),
        "status": _clean(item.get("status"), "pending"),
        "priority": _clean(item.get("priority"), "medium"),
        "item_type": _clean(item.get("item_type"), "brief"),
        "scheduled_for": _clean(item.get("scheduled_for")),
    }


def weekly_growth_recommendations(
    *,
    client: Optional[Dict[str, Any]] = None,
    queue_items: Optional[List[Dict[str, Any]]] = None,
    today: Optional[date] = None,
    weeks: int = PLAN_WEEKS,
) -> Dict[str, Any]:
    """
    Build a 4-week growth calendar view for a workspace.

    The current-week column is filled with up to ~5 recommendations from
    the client's recommended_actions list, sorted by priority and
    distributed across the work-week. Existing queue items are grouped
    into the week they were created in (or the current week if newer
    than the planning horizon's start).
    """
    today = today or date.today()
    week_start = _start_of_week(today)
    horizon_end = week_start + timedelta(weeks=weeks)

    weeks_out: List[Dict[str, Any]] = []
    for w in range(weeks):
        ws = week_start + timedelta(weeks=w)
        weeks_out.append(
            {
                "start": ws,
                "end": ws + timedelta(days=6),
                "label": _week_label(ws, today),
                "iso": ws.isoformat(),
                "is_current": w == 0,
                "cards": [],
            }
        )

    # 1. Spread the top recommended actions across the current week.
    actions = sorted(
        (client or {}).get("recommended_actions") or [],
        key=_bucket_priority,
    )

    # Dedupe: hide any recommendation already pinned to the queue.
    # We match on source_action_title (which add_queue_item stores when a
    # recommendation is pinned) and on linked_query as a fallback for the
    # target-query path.
    pinned_titles = set()
    pinned_queries = set()
    for q in queue_items or []:
        sat = _clean(q.get("source_action_title"))
        if sat:
            pinned_titles.add(sat.lower())
        tq = _clean(q.get("target_query"))
        if tq:
            pinned_queries.add(tq.lower())

    def _already_pinned(action: Dict[str, Any]) -> bool:
        title = _clean(action.get("title")).lower()
        if title and title in pinned_titles:
            return True
        linked = _clean(action.get("linked_query")).lower()
        if linked and linked in pinned_queries:
            return True
        return False

    open_actions = [
        a for a in actions
        if _clean(a.get("status"), "open") not in {"completed", "dismissed"}
        and not _already_pinned(a)
    ][:6]

    slot_counters = {"high": 0, "medium": 0, "low": 0}
    for a in open_actions:
        prio = _clean(a.get("priority"), "medium")
        slot = slot_counters.get(prio, 0)
        slot_counters[prio] = slot + 1
        offset = _suggest_day_offset(prio, slot)
        weeks_out[0]["cards"].append(_action_to_card(a, offset))

    # 2. Place existing queue items into their scheduled week.
    # Prefer scheduled_for if set, otherwise fall back to created_at.
    for item in queue_items or []:
        anchor = _parse_date(item.get("scheduled_for")) or _parse_date(item.get("created_at"))
        if not anchor:
            target_week = week_start
        else:
            anchor_ws = _start_of_week(anchor)
            if anchor_ws < week_start:
                target_week = week_start
            elif anchor_ws >= horizon_end:
                continue
            else:
                target_week = anchor_ws

        index = (target_week - week_start).days // 7
        if 0 <= index < weeks:
            weeks_out[index]["cards"].append(_queue_to_card(item))

    # 3. Stable sort within each week: queue items first, then recs by priority.
    for week in weeks_out:
        week["cards"].sort(
            key=lambda c: (
                0 if c["kind"] == "queue" else 1,
                PRIORITY_ORDER.get(c.get("priority", "medium"), 1),
            )
        )
        week["counts"] = {
            "total": len(week["cards"]),
            "queue": sum(1 for c in week["cards"] if c["kind"] == "queue"),
            "recommended": sum(1 for c in week["cards"] if c["kind"] == "recommendation"),
        }

    summary = {
        "total_actions": sum(w["counts"]["total"] for w in weeks_out),
        "current_week_actions": weeks_out[0]["counts"]["total"] if weeks_out else 0,
        "open_recommendations": len(open_actions),
    }

    # Convenience list of (iso, label) pairs for "Schedule for…" pickers.
    week_options = [
        {"iso": w["iso"], "label": f"{w['label']} ({w['start'].strftime('%d %b')})"}
        for w in weeks_out
    ]

    return {
        "today": today,
        "week_start": week_start,
        "weeks": weeks_out,
        "summary": summary,
        "week_options": week_options,
    }
