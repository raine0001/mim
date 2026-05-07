from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.primitive_request_recovery_service import (
    dispatch_bounded_tod_bridge_warning_recommendation_request,
    dispatch_bounded_tod_bridge_warning_request,
    dispatch_bounded_tod_objective_summary_request,
    dispatch_bounded_tod_recent_changes_request,
    dispatch_bounded_tod_status_request,
    dispatch_bounded_tod_warnings_summary_request,
)


BoundedActionCallable = Callable[..., dict[str, object]]


SUPPORTED_BOUNDED_ACTIONS: dict[str, BoundedActionCallable] = {
    "bounded_status_request": dispatch_bounded_tod_status_request,
    "tod_status_check": dispatch_bounded_tod_status_request,
    "status_check": dispatch_bounded_tod_status_request,
    "bounded_objective_summary_request": dispatch_bounded_tod_objective_summary_request,
    "tod_objective_summary": dispatch_bounded_tod_objective_summary_request,
    "objective_summary": dispatch_bounded_tod_objective_summary_request,
    "bounded_recent_changes_request": dispatch_bounded_tod_recent_changes_request,
    "tod_recent_changes_summary": dispatch_bounded_tod_recent_changes_request,
    "recent_changes": dispatch_bounded_tod_recent_changes_request,
    "bounded_warnings_summary_request": dispatch_bounded_tod_warnings_summary_request,
    "tod_warnings_summary": dispatch_bounded_tod_warnings_summary_request,
    "warnings_summary": dispatch_bounded_tod_warnings_summary_request,
    "bounded_bridge_warning_request": dispatch_bounded_tod_bridge_warning_request,
    "tod_bridge_warning_explanation": dispatch_bounded_tod_bridge_warning_request,
    "bridge_warning_explanation": dispatch_bounded_tod_bridge_warning_request,
    "bounded_bridge_warning_recommendation_request": dispatch_bounded_tod_bridge_warning_recommendation_request,
    "tod_bridge_warning_recommendation": dispatch_bounded_tod_bridge_warning_recommendation_request,
    "bridge_warning_recommendation": dispatch_bounded_tod_bridge_warning_recommendation_request,
}


CANONICAL_BOUNDED_ACTION_NAMES: tuple[str, ...] = (
    "tod_status_check",
    "tod_objective_summary",
    "tod_recent_changes_summary",
    "tod_warnings_summary",
    "tod_bridge_warning_explanation",
    "tod_bridge_warning_recommendation",
)


def normalize_bounded_action_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def get_bounded_action(action_name: Any) -> BoundedActionCallable | None:
    return SUPPORTED_BOUNDED_ACTIONS.get(normalize_bounded_action_name(action_name))


def list_bounded_actions() -> dict[str, object]:
    return {
        "canonical_actions": list(CANONICAL_BOUNDED_ACTION_NAMES),
        "all_actions": sorted(SUPPORTED_BOUNDED_ACTIONS.keys()),
        "action_count": len(SUPPORTED_BOUNDED_ACTIONS),
    }