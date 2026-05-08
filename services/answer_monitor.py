"""
AI Answer Monitor.

Persistence + view helpers for tracking how a brand shows up across
re-runs of the same prompt over time. The snapshot rows feed the
sparkline history on the monitor page; the latest values are also
written back to PromptTracking so existing surfaces (visibility chart,
position-tracking) keep showing the most recent state.

Kept side-effect-free w/r/t Flask routing — call sites pass the SQLAlchemy
session and model classes in. That makes this module trivially unit-
testable and avoids a circular import with app.py where the models live.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional


def _truncate(text: str, max_len: int = 600) -> str:
    if not text:
        return ""
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _band_for_score(score: int) -> str:
    if score >= 12:
        return "Strong"
    if score >= 6:
        return "Mixed"
    return "Weak"


def _visibility_for_score(score: int) -> str:
    if score >= 12:
        return "High"
    if score >= 6:
        return "Medium"
    return "Low"


def _change_label(prev: Optional[bool], current: bool) -> str:
    if prev is None:
        return "First check"
    if prev and not current:
        return "Lost"
    if current and not prev:
        return "Gained"
    return "Stable"


def run_answer_check(
    *,
    db,
    PromptTracking,
    PromptCheckSnapshot,
    simulate_ai_answer,
    prompt_row,
    brand_name: str,
) -> Dict[str, Any]:
    """Run one prompt against the AI answer engine and persist a snapshot.

    Returns the snapshot fields as a dict so callers can show a flash
    or render straight away without round-tripping the DB."""
    result = simulate_ai_answer(query=prompt_row.prompt, company_name=brand_name)

    score = int(result.get("score") or 0)
    brand_mentioned = bool(result.get("brand_mentioned"))
    brand_position = result.get("brand_position")
    competitors = result.get("competitors_mentioned") or []
    answer_excerpt = _truncate(result.get("answer", ""))

    prev_snapshot = (
        db.session.query(PromptCheckSnapshot)
        .filter_by(prompt_tracking_id=prompt_row.id)
        .order_by(PromptCheckSnapshot.checked_at.desc())
        .first()
    )
    prev_mentioned = prev_snapshot.brand_mentioned if prev_snapshot else None

    snapshot = PromptCheckSnapshot(
        prompt_tracking_id=prompt_row.id,
        user_id=prompt_row.user_id,
        client_id=getattr(prompt_row, "client_id", None),
        engine=result.get("engine") or "ai-assistant",
        brand_mentioned=brand_mentioned,
        brand_position=brand_position if isinstance(brand_position, int) else None,
        score=score,
        answer_type=result.get("answer_type"),
        competitors_mentioned=competitors,
        answer_excerpt=answer_excerpt,
        checked_at=datetime.utcnow(),
    )
    db.session.add(snapshot)

    prompt_row.mentioned = "Yes" if brand_mentioned else "No"
    prompt_row.visibility = _visibility_for_score(score)
    prompt_row.score_band = _band_for_score(score)
    prompt_row.prompt_score = score
    prompt_row.brand_position = (
        f"#{brand_position}" if isinstance(brand_position, int) else "Not mentioned"
    )
    prompt_row.competitor_count = len(competitors)
    prompt_row.top_competitor = competitors[0] if competitors else prompt_row.top_competitor
    prompt_row.last_checked = snapshot.checked_at.strftime("%d %b %Y, %H:%M UTC")
    prompt_row.change = _change_label(prev_mentioned, brand_mentioned)

    db.session.commit()

    return {
        "snapshot_id": snapshot.id,
        "engine": snapshot.engine,
        "brand_mentioned": brand_mentioned,
        "brand_position": brand_position,
        "score": score,
        "answer_type": snapshot.answer_type,
        "competitors": competitors,
        "answer_excerpt": answer_excerpt,
        "change": prompt_row.change,
        "checked_at": snapshot.checked_at.isoformat(),
    }


def load_history_for_prompts(
    *,
    db,
    PromptCheckSnapshot,
    prompt_ids: List[int],
    limit_per_prompt: int = 10,
) -> Dict[int, List[Dict[str, Any]]]:
    """Pull the last N snapshots per prompt id into a dict keyed by id.

    Cheap-and-correct: one query per prompt. Callers usually have at
    most a few dozen tracked prompts per workspace, and each query is
    indexed on prompt_tracking_id, so this stays well under page-render
    budget even on the slowest dyno."""
    out: Dict[int, List[Dict[str, Any]]] = {}
    for pid in prompt_ids:
        rows = (
            db.session.query(PromptCheckSnapshot)
            .filter_by(prompt_tracking_id=pid)
            .order_by(PromptCheckSnapshot.checked_at.desc())
            .limit(limit_per_prompt)
            .all()
        )
        out[pid] = [
            {
                "id": r.id,
                "engine": r.engine,
                "brand_mentioned": r.brand_mentioned,
                "brand_position": r.brand_position,
                "score": r.score,
                "answer_type": r.answer_type,
                "competitors": r.competitors_mentioned or [],
                "answer_excerpt": r.answer_excerpt or "",
                "checked_at": r.checked_at,
            }
            for r in reversed(rows)
        ]
    return out


def summarize_history(
    history_by_prompt: Dict[int, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Roll the raw history into top-level numbers for the monitor page."""
    total = 0
    cited_now = 0
    gained = 0
    lost = 0
    avg_score_total = 0
    avg_score_n = 0

    for pid, history in history_by_prompt.items():
        if not history:
            continue
        total += 1
        latest = history[-1]
        if latest["brand_mentioned"]:
            cited_now += 1
        avg_score_total += latest["score"]
        avg_score_n += 1
        if len(history) >= 2:
            prev = history[-2]
            if not prev["brand_mentioned"] and latest["brand_mentioned"]:
                gained += 1
            elif prev["brand_mentioned"] and not latest["brand_mentioned"]:
                lost += 1

    avg_score = round(avg_score_total / avg_score_n, 1) if avg_score_n else 0.0

    return {
        "total_with_history": total,
        "cited_now": cited_now,
        "gained": gained,
        "lost": lost,
        "avg_score": avg_score,
    }
