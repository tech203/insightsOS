from __future__ import annotations

from typing import Any, Dict, List, Optional


PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _clean(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() or default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(round(float(value)))
    except Exception:
        return default


def _client_id(client: Optional[Dict[str, Any]]) -> str:
    return _clean((client or {}).get("id"))


def _endpoint(
    endpoint: str,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {"endpoint": endpoint, "params": params or {}}


def _mode(value: Any, default: str = "assisted") -> str:
    raw = _clean(value, default).lower()
    if raw in {"manual", "manual_only"}:
        return "manual"
    if raw in {"autonomous", "ai_autonomous"}:
        return "autonomous"
    return "assisted"


def _action(
    *,
    action_id: str,
    title: str,
    reason: str,
    label: str,
    mode: str = "assisted",
    credits_required: int = 0,
    stage: str = "planning",
    primary: Dict[str, Any],
    secondary: Optional[Dict[str, Any]] = None,
    secondary_label: str = "Review First",
    source: str = "system",
) -> Dict[str, Any]:
    return {
        "id": action_id,
        "title": title,
        "reason": reason,
        "label": label,
        "mode": _mode(mode),
        "credits_required": credits_required,
        "stage": stage,
        "primary": primary,
        "secondary": secondary,
        "secondary_label": secondary_label,
        "source": source,
    }


def _first_open_queue_item(
    queue_items: Optional[List[Dict[str, Any]]],
) -> Optional[Dict[str, Any]]:
    open_items = [
        item for item in queue_items or []
        if _clean(item.get("status"), "pending") != "published"
    ]

    def sort_key(item: Dict[str, Any]):
        return (
            PRIORITY_ORDER.get(_clean(item.get("priority"), "medium"), 1),
            item.get("created_at", ""),
        )

    return sorted(open_items, key=sort_key)[0] if open_items else None


def _top_action(client: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    actions = (client or {}).get("recommended_actions") or []
    if not actions:
        return None
    return actions[0]


def build_next_best_action(
    *,
    client: Optional[Dict[str, Any]] = None,
    queue_items: Optional[List[Dict[str, Any]]] = None,
    has_clients: bool = False,
    total_audits: int = 0,
    total_prompts: int = 0,
    user_plan: str = "free",
    credit_balance: int = 0,
) -> Dict[str, Any]:
    """
    Chooses one user-facing next action for the current account or workspace.

    The output intentionally uses endpoint names and params instead of Flask URLs
    so the decision logic remains framework-light and easy to reuse.
    """

    plan = _clean(user_plan, "free")
    is_free = plan == "free"
    client_id = _client_id(client)
    latest_audit = (client or {}).get("latest_audit")

    if not client:
        if not has_clients:
            return _action(
                action_id="create_workspace",
                title="Create your first workspace",
                reason=(
                    "DarInsights needs a business workspace before it can run "
                    "audits, track prompts, or recommend growth actions."
                ),
                label="Add Workspace",
                mode="manual",
                stage="setup",
                primary=_endpoint("create_client"),
            )

        return _action(
            action_id="open_workspace",
            title="Open a workspace",
            reason=(
                "Start from a specific business so the next recommendation is "
                "based on its audit, queue, and visibility state."
            ),
            label="Open Workspaces",
            mode="manual",
            stage="setup",
            primary=_endpoint("clients_page"),
        )

    if not latest_audit:
        if is_free:
            return _action(
                action_id="upgrade_for_audit",
                title="Unlock the first audit",
                reason=(
                    "The workspace does not have a measured visibility baseline yet."
                ),
                label="Upgrade to Continue",
                mode="manual",
                stage="setup",
                primary=_endpoint("pricing_page"),
                secondary=_endpoint("edit_client", {"client_id": client_id}),
                secondary_label="Edit Workspace",
            )

        return _action(
            action_id="run_first_audit",
            title="Run the first visibility audit",
            reason=(
                "Start with a measured baseline before generating content or "
                "automation workflows."
            ),
            label="Run Audit",
            mode="assisted",
            credits_required=3,
            stage="audit",
            primary=_endpoint("run_client_audit", {"client_id": client_id}),
            secondary=_endpoint("edit_client", {"client_id": client_id}),
            secondary_label="Edit Workspace",
        )

    next_queue_item = _first_open_queue_item(queue_items)
    if next_queue_item:
        status = _clean(next_queue_item.get("status"), "pending")
        item_id = _clean(next_queue_item.get("id"))
        title = _clean(
            next_queue_item.get("title")
            or next_queue_item.get("target_query"),
            "Continue queued content",
        )
        credits = _safe_int(next_queue_item.get("credits_required"))

        if is_free:
            return _action(
                action_id="upgrade_for_queue",
                title="Unlock queued execution",
                reason=(
                    "There is queued growth work ready, but generation is locked "
                    "on the free layer."
                ),
                label="Upgrade to Continue",
                mode="manual",
                credits_required=credits,
                stage="execution",
                primary=_endpoint("pricing_page"),
                secondary=_endpoint(
                    "content_queue_page", {"client_id": client_id}
                ),
                secondary_label="Open Queue",
                source="queue",
            )

        if status in {"pending", "queued"}:
            return _action(
                action_id="generate_queue_brief",
                title=f"Generate the brief for {title}",
                reason=(
                    "This queued opportunity is ready for the first assisted "
                    "production step."
                ),
                label="Generate Brief",
                mode=next_queue_item.get("execution_type"),
                credits_required=credits or 1,
                stage="execution",
                primary=_endpoint("generate_brief_from_queue", {"item_id": item_id}),
                secondary=_endpoint(
                    "content_queue_page", {"client_id": client_id}
                ),
                secondary_label="Open Queue",
                source="queue",
            )

        if status == "brief_generated":
            return _action(
                action_id="generate_queue_draft",
                title=f"Turn the approved brief into a draft",
                reason=(
                    "The brief exists, so the next step is to create an editable "
                    "draft for review."
                ),
                label="Generate Draft",
                mode=next_queue_item.get("execution_type"),
                credits_required=credits or 1,
                stage="execution",
                primary=_endpoint("generate_draft_from_queue", {"item_id": item_id}),
                secondary=_endpoint(
                    "content_queue_page", {"client_id": client_id}
                ),
                secondary_label="Open Queue",
                source="queue",
            )

        return _action(
            action_id="review_queue_item",
            title="Review the next queued output",
            reason=(
                "A generated item is waiting for human review before it is marked "
                "published or converted into a page."
            ),
            label="Open Queue",
            mode="manual",
            credits_required=0,
            stage="approval",
            primary=_endpoint("content_queue_page", {"client_id": client_id}),
            source="queue",
        )

    action = _top_action(client)
    if action:
        linked_query = _clean(action.get("linked_query"))
        suggested_content_type = _clean(action.get("suggested_content_type"))
        credits = _safe_int(action.get("credits_required"))

        if credits > credit_balance and plan not in {"dev_unlimited"}:
            return _action(
                action_id="top_up_for_action",
                title="Top up credits for the recommended action",
                reason=(
                    "The next recommended workflow is clear, but the account "
                    "does not have enough credits to run it."
                ),
                label="Top Up Credits",
                mode="manual",
                credits_required=credits,
                stage="billing",
                primary=_endpoint("pricing_page"),
                secondary=_endpoint(
                    "client_actions_page", {"client_id": client_id}
                ),
                secondary_label="Open Action Plan",
                source="action_plan",
            )

        if linked_query and suggested_content_type:
            return _action(
                action_id="generate_action_brief",
                title=f"Create content for {linked_query}",
                reason=_clean(
                    action.get("issue"),
                    "This is the highest-priority visibility gap from the latest audit.",
                ),
                label="Generate Brief",
                mode=action.get("execution_type"),
                credits_required=credits,
                stage="execution",
                primary=_endpoint(
                    "generate_client_content_brief",
                    {
                        "client_id": client_id,
                        "target_query": linked_query,
                        "content_type": suggested_content_type,
                    },
                ),
                secondary=_endpoint(
                    "client_actions_page", {"client_id": client_id}
                ),
                secondary_label="Open Action Plan",
                source="action_plan",
            )

        return _action(
            action_id="review_action_plan",
            title=_clean(action.get("title"), "Review the action plan"),
            reason=_clean(
                action.get("recommended_action") or action.get("recommended_fix"),
                "Review the highest-priority recommendation from the latest audit.",
            ),
            label="Open Action Plan",
            mode=action.get("execution_type"),
            credits_required=credits,
            stage="planning",
            primary=_endpoint("client_actions_page", {"client_id": client_id}),
            secondary=_endpoint(
                "client_visibility_page", {"client_id": client_id}
            ),
            secondary_label="Review Signals",
            source="action_plan",
        )

    if total_prompts <= 0:
        return _action(
            action_id="setup_prompt_tracking",
            title="Set up prompt tracking",
            reason=(
                "The audit exists, but prompt tracking is needed to monitor AI "
                "visibility over time."
            ),
            label="Open Prompt Tracking",
            mode="manual",
            stage="measurement",
            primary=_endpoint("position_tracking_page", {"client_id": client_id}),
            secondary=_endpoint("client_detail", {"client_id": client_id}),
            secondary_label="Workspace Overview",
        )

    return _action(
        action_id="review_visibility",
        title="Review visibility signals",
        reason=(
            "The workspace has a baseline. Review current signals before choosing "
            "the next growth workflow."
        ),
        label="Review Visibility",
        mode="manual",
        stage="measurement",
        primary=_endpoint("client_visibility_page", {"client_id": client_id}),
        secondary=_endpoint("client_actions_page", {"client_id": client_id}),
        secondary_label="Open Action Plan",
    )
