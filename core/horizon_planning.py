from __future__ import annotations

from dataclasses import dataclass


PRIORITY_BASE = {
    "critical": 1.0,
    "high": 0.85,
    "normal": 0.6,
    "low": 0.35,
}


DEFAULT_WEIGHTS = {
    "priority": 0.24,
    "expected_value": 0.22,
    "urgency": 0.18,
    "constraint_learning": 0.1,
    "map_freshness": 0.1,
    "object_confidence": 0.08,
    "operator_preference": 0.08,
}


@dataclass
class PlanningContext:
    map_freshness_seconds: int
    object_confidence: float
    human_in_workspace: bool
    shared_workspace_active: bool
    operator_preferences: dict
    learned_constraint_factor: float
    map_freshness_limit_seconds: int
    min_target_confidence: float


def _priority_value(priority: str) -> float:
    return PRIORITY_BASE.get(str(priority or "normal").strip().lower(), PRIORITY_BASE["normal"])


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def score_goal_candidate(goal: dict, context: PlanningContext, weights: dict) -> dict:
    preference_key = str(goal.get("goal_type", "")).strip()
    preference_boost = _bounded(float(context.operator_preferences.get(preference_key, 0.0) or 0.0))

    map_freshness_score = 1.0 - _bounded(
        float(context.map_freshness_seconds) / float(max(1, context.map_freshness_limit_seconds))
    )
    confidence_score = _bounded(context.object_confidence)
    priority_score = _priority_value(str(goal.get("priority", "normal")))
    expected_value_score = _bounded(float(goal.get("expected_value", 0.5) or 0.0))
    urgency_score = _bounded(float(goal.get("urgency", 0.5) or 0.0))

    base = (
        float(weights.get("priority", 0.0)) * priority_score
        + float(weights.get("expected_value", 0.0)) * expected_value_score
        + float(weights.get("urgency", 0.0)) * urgency_score
        + float(weights.get("constraint_learning", 0.0)) * _bounded(context.learned_constraint_factor)
        + float(weights.get("map_freshness", 0.0)) * map_freshness_score
        + float(weights.get("object_confidence", 0.0)) * confidence_score
        + float(weights.get("operator_preference", 0.0)) * preference_boost
    )

    deferred = False
    defer_reason = ""
    if bool(goal.get("is_physical", False)) and priority_score <= PRIORITY_BASE["normal"]:
        if context.human_in_workspace or context.shared_workspace_active:
            deferred = True
            defer_reason = "human_presence_active_defer_low_priority_physical"
            base *= 0.55

    return {
        "goal_key": str(goal.get("goal_key", "")),
        "title": str(goal.get("title", "")),
        "goal_type": str(goal.get("goal_type", "general")),
        "priority": str(goal.get("priority", "normal")),
        "dependencies": goal.get("dependencies", []) if isinstance(goal.get("dependencies", []), list) else [],
        "estimated_steps": int(goal.get("estimated_steps", 1) or 1),
        "expected_value": expected_value_score,
        "urgency": urgency_score,
        "is_physical": bool(goal.get("is_physical", False)),
        "deferred": deferred,
        "defer_reason": defer_reason,
        "metadata_json": goal.get("metadata_json", {}) if isinstance(goal.get("metadata_json", {}), dict) else {},
        "score": round(_bounded(base), 6),
        "score_breakdown": {
            "priority": priority_score,
            "expected_value": expected_value_score,
            "urgency": urgency_score,
            "constraint_learning": _bounded(context.learned_constraint_factor),
            "map_freshness": map_freshness_score,
            "object_confidence": confidence_score,
            "operator_preference": preference_boost,
        },
    }


def build_staged_action_graph(*, ranked_goals: list[dict], context: PlanningContext) -> list[dict]:
    stages: list[dict] = []
    stage_index = 0

    needs_refresh = context.map_freshness_seconds > context.map_freshness_limit_seconds
    needs_rescan = context.object_confidence < context.min_target_confidence

    if needs_refresh:
        stage_index += 1
        stages.append(
            {
                "stage_id": f"stage-{stage_index}",
                "stage_type": "refresh_workspace_zone",
                "title": "Refresh stale workspace map",
                "depends_on": [],
                "applies_to": "all",
                "reason": "map_freshness_exceeded",
            }
        )

    if needs_rescan:
        stage_index += 1
        stages.append(
            {
                "stage_id": f"stage-{stage_index}",
                "stage_type": "rescan_target_area",
                "title": "Rescan target area before continuation",
                "depends_on": [item["stage_id"] for item in stages[-1:]],
                "applies_to": "all",
                "reason": "target_confidence_below_threshold",
            }
        )

    stage_ids_by_goal: dict[str, str] = {}
    for goal in ranked_goals:
        stage_index += 1
        deps: list[str] = []
        for dependency_goal in goal.get("dependencies", []) if isinstance(goal.get("dependencies", []), list) else []:
            stage_id = stage_ids_by_goal.get(str(dependency_goal))
            if stage_id:
                deps.append(stage_id)
        if stages:
            deps.extend([item["stage_id"] for item in stages if item["stage_type"] in {"refresh_workspace_zone", "rescan_target_area"}])

        stage = {
            "stage_id": f"stage-{stage_index}",
            "stage_type": "goal_execution",
            "goal_key": goal.get("goal_key"),
            "title": goal.get("title"),
            "deferred": bool(goal.get("deferred", False)),
            "depends_on": sorted(set(deps)),
            "reason": goal.get("defer_reason", "") if bool(goal.get("deferred", False)) else "",
        }
        stages.append(stage)
        stage_ids_by_goal[str(goal.get("goal_key", ""))] = stage["stage_id"]

    return stages
