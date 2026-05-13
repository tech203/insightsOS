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

from dtutils import utcnow
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
    engines: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Run a prompt against every enabled AI engine and persist a snapshot
    per (prompt, engine).

    Returns a list of snapshot dicts (one per engine that ran). The
    PromptTracking row is updated to summarise across engines: best
    score wins; "mentioned" is Yes if any engine cited the brand."""
    if not engines:
        engines = ["chatgpt"]

    results: List[Dict[str, Any]] = []

    for engine in engines:
        try:
            result = simulate_ai_answer(
                query=prompt_row.prompt,
                company_name=brand_name,
                engine=engine,
            )
        except Exception:
            # Fall back to a single best-effort default if a registered
            # engine refuses (missing key, bad model name, etc.) — caller
            # already tolerates an empty result list.
            continue

        score = int(result.get("score") or 0)
        brand_mentioned = bool(result.get("brand_mentioned"))
        brand_position = result.get("brand_position")
        competitors = result.get("competitors_mentioned") or []
        answer_excerpt = _truncate(result.get("answer", ""))

        prev_snapshot = (
            db.session.query(PromptCheckSnapshot)
            .filter_by(prompt_tracking_id=prompt_row.id, engine=engine)
            .order_by(PromptCheckSnapshot.checked_at.desc())
            .first()
        )
        prev_mentioned = prev_snapshot.brand_mentioned if prev_snapshot else None

        snapshot = PromptCheckSnapshot(
            prompt_tracking_id=prompt_row.id,
            user_id=prompt_row.user_id,
            client_id=getattr(prompt_row, "client_id", None),
            engine=engine,
            brand_mentioned=brand_mentioned,
            brand_position=brand_position if isinstance(brand_position, int) else None,
            score=score,
            answer_type=result.get("answer_type"),
            competitors_mentioned=competitors,
            answer_excerpt=answer_excerpt,
            checked_at=utcnow(),
        )
        db.session.add(snapshot)
        db.session.flush()

        results.append(
            {
                "snapshot_id": snapshot.id,
                "engine": engine,
                "engine_label": result.get("engine_label", engine),
                "brand_mentioned": brand_mentioned,
                "brand_position": brand_position,
                "score": score,
                "answer_type": snapshot.answer_type,
                "competitors": competitors,
                "answer_excerpt": answer_excerpt,
                "change": _change_label(prev_mentioned, brand_mentioned),
                "checked_at": snapshot.checked_at.isoformat(),
            }
        )

    if results:
        # Roll-up across engines: best score wins, mentioned if any did,
        # change label leans toward Gained/Lost over Stable.
        best = max(results, key=lambda r: r["score"])
        any_mentioned = any(r["brand_mentioned"] for r in results)
        all_competitors: List[str] = []
        for r in results:
            for c in r["competitors"]:
                if c not in all_competitors:
                    all_competitors.append(c)
        change_priority = {"Gained": 0, "Lost": 1, "First check": 2, "Stable": 3}
        rolled_change = sorted(
            (r["change"] for r in results),
            key=lambda c: change_priority.get(c, 99),
        )[0]

        prompt_row.mentioned = "Yes" if any_mentioned else "No"
        prompt_row.visibility = _visibility_for_score(best["score"])
        prompt_row.score_band = _band_for_score(best["score"])
        prompt_row.prompt_score = best["score"]
        prompt_row.brand_position = (
            f"#{best['brand_position']}"
            if isinstance(best.get("brand_position"), int)
            else ("Not mentioned" if not any_mentioned else "Cited")
        )
        prompt_row.competitor_count = len(all_competitors)
        prompt_row.top_competitor = (
            all_competitors[0] if all_competitors else prompt_row.top_competitor
        )
        prompt_row.last_checked = utcnow().strftime("%d %b %Y, %H:%M UTC")
        prompt_row.change = rolled_change

    db.session.commit()
    return results


def load_history_for_prompts(
    *,
    db,
    PromptCheckSnapshot,
    prompt_ids: List[int],
    limit_per_engine: int = 10,
) -> Dict[int, Dict[str, List[Dict[str, Any]]]]:
    """Pull the last N snapshots per prompt id, grouped by engine.

    Returns: { prompt_id: { engine_slug: [snapshot, …oldest→newest] } }.
    Used to render per-engine sparklines on the monitor page."""
    out: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}
    for pid in prompt_ids:
        # Pull a generous window then bucket by engine so we can keep the
        # newest N per engine instead of the newest N overall (which would
        # bias toward whichever engine ran last).
        rows = (
            db.session.query(PromptCheckSnapshot)
            .filter_by(prompt_tracking_id=pid)
            .order_by(PromptCheckSnapshot.checked_at.desc())
            .limit(limit_per_engine * 6)
            .all()
        )
        by_engine: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            engine = r.engine or "ai-assistant"
            bucket = by_engine.setdefault(engine, [])
            if len(bucket) >= limit_per_engine:
                continue
            bucket.append(
                {
                    "id": r.id,
                    "engine": engine,
                    "brand_mentioned": r.brand_mentioned,
                    "brand_position": r.brand_position,
                    "score": r.score,
                    "answer_type": r.answer_type,
                    "competitors": r.competitors_mentioned or [],
                    "answer_excerpt": r.answer_excerpt or "",
                    "checked_at": r.checked_at,
                }
            )
        # Each bucket was filled newest-first; reverse for oldest→newest.
        out[pid] = {engine: list(reversed(bucket)) for engine, bucket in by_engine.items()}
    return out


def summarize_history(
    history_by_prompt: Dict[int, Dict[str, List[Dict[str, Any]]]],
) -> Dict[str, Any]:
    """Roll the per-engine raw history into top-level numbers.

    `cited_now` counts a prompt if any engine cited the brand in its
    most recent snapshot. `gained`/`lost` only fire when the prompt
    flipped state (across any engine) since the previous check."""
    total = 0
    cited_now = 0
    gained = 0
    lost = 0
    avg_score_total = 0
    avg_score_n = 0
    per_engine: Dict[str, Dict[str, int]] = {}

    for pid, by_engine in history_by_prompt.items():
        if not by_engine:
            continue
        total += 1

        any_cited_now = False
        any_cited_prev: Optional[bool] = None  # None means no prior across engines
        best_score_now = 0

        for engine, history in by_engine.items():
            if not history:
                continue
            latest = history[-1]
            stats = per_engine.setdefault(
                engine,
                {"checks": 0, "cited_now": 0},
            )
            stats["checks"] += 1
            if latest["brand_mentioned"]:
                stats["cited_now"] += 1
                any_cited_now = True
            best_score_now = max(best_score_now, latest["score"])
            if len(history) >= 2:
                prev = history[-2]
                if any_cited_prev is None:
                    any_cited_prev = prev["brand_mentioned"]
                else:
                    any_cited_prev = any_cited_prev or prev["brand_mentioned"]

        if any_cited_now:
            cited_now += 1
        avg_score_total += best_score_now
        avg_score_n += 1

        if any_cited_prev is not None:
            if not any_cited_prev and any_cited_now:
                gained += 1
            elif any_cited_prev and not any_cited_now:
                lost += 1

    avg_score = round(avg_score_total / avg_score_n, 1) if avg_score_n else 0.0

    return {
        "total_with_history": total,
        "cited_now": cited_now,
        "gained": gained,
        "lost": lost,
        "avg_score": avg_score,
        "per_engine": per_engine,
    }
