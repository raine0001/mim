from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.horizon_planning import DEFAULT_WEIGHTS, PlanningContext, build_staged_action_graph, score_goal_candidate
from core.environment_strategy_service import (
    get_active_environment_strategies,
    mark_strategy_influenced_plan,
    strategy_influence_for_goal,
)
from core.models import ConstraintAdjustmentProposal, WorkspaceHorizonCheckpoint, WorkspaceHorizonPlan, WorkspaceHorizonReplanEvent


CHECKPOINT_STATES = {
    "planned",
    "active",
    "checkpoint_reached",
    "needs_re_evaluation",
    "replanned",
    "complete",
}


def _normalized_weights(policy: dict) -> dict:
    configured = policy.get("weights", {}) if isinstance(policy.get("weights", {}), dict) else {}
    merged = {
        **DEFAULT_WEIGHTS,
        **{str(key): float(value) for key, value in configured.items()},
    }
    total = sum(max(0.0, float(value)) for value in merged.values())
    if total <= 0:
        return DEFAULT_WEIGHTS.copy()
    return {key: max(0.0, float(value)) / total for key, value in merged.items()}


async def _learned_constraint_factor(db: AsyncSession) -> float:
    value = (
        await db.execute(
            select(func.avg(ConstraintAdjustmentProposal.success_rate)).where(
                ConstraintAdjustmentProposal.status == "proposed"
            )
        )
    ).scalar_one_or_none()
    if value is None:
        return 0.5
    return max(0.0, min(1.0, float(value)))


async def create_horizon_plan(
    *,
    actor: str,
    source: str,
    planning_horizon_minutes: int,
    goal_candidates: list[dict],
    expected_future_constraints: list[dict],
    priority_policy: dict,
    map_freshness_seconds: int,
    object_confidence: float,
    human_aware_state: dict,
    operator_preferences: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceHorizonPlan, list[WorkspaceHorizonCheckpoint]]:
    learned_factor = await _learned_constraint_factor(db)
    weights = _normalized_weights(priority_policy)
    map_limit = int(priority_policy.get("map_freshness_limit_seconds", 900) or 900)
    confidence_min = float(priority_policy.get("min_target_confidence", 0.75) or 0.75)

    context = PlanningContext(
        map_freshness_seconds=max(0, int(map_freshness_seconds)),
        object_confidence=max(0.0, min(1.0, float(object_confidence))),
        human_in_workspace=bool(human_aware_state.get("human_in_workspace", False)),
        shared_workspace_active=bool(human_aware_state.get("shared_workspace_active", False)),
        operator_preferences=operator_preferences if isinstance(operator_preferences, dict) else {},
        learned_constraint_factor=learned_factor,
        map_freshness_limit_seconds=max(1, map_limit),
        min_target_confidence=max(0.0, min(1.0, confidence_min)),
    )

    scored = [score_goal_candidate(item, context, weights) for item in goal_candidates]
    active_strategies = await get_active_environment_strategies(db=db, limit=50)
    influenced_strategy_ids: set[int] = set()
    strategy_context_rows: list[dict] = []

    for goal in scored:
        bonus = 0.0
        reasons: list[str] = []
        strategy_ids: list[int] = []
        for strategy in active_strategies:
            influence, reason = strategy_influence_for_goal(goal=goal, strategy=strategy)
            if influence <= 0:
                continue
            bonus += float(influence)
            strategy_ids.append(int(strategy.id))
            influenced_strategy_ids.add(int(strategy.id))
            if reason:
                reasons.append(f"{strategy.id}:{reason}")

        if bonus > 0:
            goal["score"] = round(max(0.0, min(1.0, float(goal.get("score", 0.0)) + bonus)), 6)
            score_breakdown = goal.get("score_breakdown", {}) if isinstance(goal.get("score_breakdown", {}), dict) else {}
            goal["score_breakdown"] = {
                **score_breakdown,
                "strategy": round(min(1.0, bonus), 6),
            }
            goal["strategy_influence"] = {
                "bonus": round(bonus, 6),
                "strategy_ids": strategy_ids,
                "reasons": reasons,
            }

    if active_strategies:
        strategy_context_rows = [
            {
                "strategy_id": int(item.id),
                "strategy_type": item.strategy_type,
                "target_scope": item.target_scope,
                "priority": item.priority,
                "influence_weight": float(item.influence_weight),
            }
            for item in active_strategies
        ]

    ranked = sorted(scored, key=lambda item: (bool(item.get("deferred", False)), -float(item.get("score", 0.0))))
    stages = build_staged_action_graph(ranked_goals=ranked, context=context)

    expected_constraints = list(expected_future_constraints if isinstance(expected_future_constraints, list) else [])
    if not expected_constraints:
        expected_constraints = [
            {
                "constraint_key": "map_freshness_seconds",
                "operator": "<=",
                "expected_value": context.map_freshness_limit_seconds,
                "replan_on_break": True,
            },
            {
                "constraint_key": "object_confidence",
                "operator": ">=",
                "expected_value": context.min_target_confidence,
                "replan_on_break": True,
            },
        ]

    explanation = {
        "selected_plan_reason": "multi_goal_ranked_by_future_state_scoring",
        "weights": weights,
        "learned_constraint_factor": learned_factor,
        "deferral_policy": "defer_low_priority_physical_when_human_presence_active",
        "replan_triggers": expected_constraints,
        "strategy_context": strategy_context_rows,
        "influenced_strategy_ids": sorted(influenced_strategy_ids),
    }

    plan = WorkspaceHorizonPlan(
        actor=actor,
        source=source,
        status="active" if stages else "planned",
        planning_horizon_minutes=max(10, min(1440, int(planning_horizon_minutes))),
        ranked_goals_json=ranked,
        staged_action_graph_json=stages,
        expected_future_constraints_json=expected_constraints,
        scoring_context_json={
            "map_freshness_seconds": context.map_freshness_seconds,
            "object_confidence": context.object_confidence,
            "human_aware_state": {
                "human_in_workspace": context.human_in_workspace,
                "shared_workspace_active": context.shared_workspace_active,
            },
            "operator_preferences": context.operator_preferences,
            "strategy_context": strategy_context_rows,
        },
        explanation_json=explanation,
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    db.add(plan)
    await db.flush()

    if influenced_strategy_ids:
        index_by_id = {int(item.id): item for item in active_strategies}
        for strategy_id in sorted(influenced_strategy_ids):
            strategy = index_by_id.get(strategy_id)
            if strategy:
                await mark_strategy_influenced_plan(strategy=strategy, plan_id=plan.id)

    checkpoints: list[WorkspaceHorizonCheckpoint] = []
    for index, stage in enumerate(stages, start=1):
        checkpoint = WorkspaceHorizonCheckpoint(
            plan_id=plan.id,
            checkpoint_key=str(stage.get("stage_id", f"stage-{index}")),
            sequence_index=index,
            checkpoint_type=str(stage.get("stage_type", "goal_step")),
            status="active" if index == 1 else "planned",
            related_goal_key=str(stage.get("goal_key", "")),
            trigger_conditions_json={
                "depends_on": stage.get("depends_on", []) if isinstance(stage.get("depends_on", []), list) else [],
            },
            replan_if_json=expected_constraints,
            explanation=str(stage.get("reason", "")),
            metadata_json=stage,
        )
        db.add(checkpoint)
        checkpoints.append(checkpoint)

    await db.flush()
    return plan, checkpoints


async def get_horizon_plan(*, plan_id: int, db: AsyncSession) -> WorkspaceHorizonPlan | None:
    return (
        await db.execute(select(WorkspaceHorizonPlan).where(WorkspaceHorizonPlan.id == plan_id))
    ).scalars().first()


async def list_horizon_checkpoints(*, plan_id: int, db: AsyncSession) -> list[WorkspaceHorizonCheckpoint]:
    return (
        await db.execute(
            select(WorkspaceHorizonCheckpoint)
            .where(WorkspaceHorizonCheckpoint.plan_id == plan_id)
            .order_by(WorkspaceHorizonCheckpoint.sequence_index.asc(), WorkspaceHorizonCheckpoint.id.asc())
        )
    ).scalars().all()


async def get_current_horizon_plan(*, db: AsyncSession) -> WorkspaceHorizonPlan | None:
    return (
        await db.execute(
            select(WorkspaceHorizonPlan)
            .order_by(WorkspaceHorizonPlan.id.desc())
        )
    ).scalars().first()


async def advance_horizon_checkpoint(
    *,
    plan: WorkspaceHorizonPlan,
    outcome: str,
    actor: str,
    reason: str,
    checkpoint_id: int | None,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceHorizonCheckpoint | None, WorkspaceHorizonCheckpoint | None]:
    checkpoints = await list_horizon_checkpoints(plan_id=plan.id, db=db)
    if not checkpoints:
        plan.status = "complete"
        await db.flush()
        return None, None

    selected = None
    if checkpoint_id:
        for item in checkpoints:
            if item.id == checkpoint_id:
                selected = item
                break

    if selected is None:
        selected = next((item for item in checkpoints if item.status == "active"), checkpoints[0])

    if outcome not in CHECKPOINT_STATES:
        outcome = "checkpoint_reached"

    selected.status = outcome
    selected.metadata_json = {
        **(selected.metadata_json if isinstance(selected.metadata_json, dict) else {}),
        "last_actor": actor,
        "last_reason": reason,
        **(metadata_json if isinstance(metadata_json, dict) else {}),
    }

    next_checkpoint = None
    if outcome in {"checkpoint_reached", "complete"}:
        pending = [item for item in checkpoints if item.sequence_index > selected.sequence_index and item.status == "planned"]
        if pending:
            next_checkpoint = pending[0]
            next_checkpoint.status = "active"
            plan.status = "active"
        else:
            plan.status = "complete"
    elif outcome in {"needs_re_evaluation", "replanned"}:
        plan.status = outcome
    else:
        plan.status = "active"

    await db.flush()
    return selected, next_checkpoint


def _drift_breaks_constraint(*, drift_type: str, observed_value: str, constraint: dict) -> bool:
    key = str(constraint.get("constraint_key", "")).strip()
    if key != drift_type:
        return False

    operator = str(constraint.get("operator", "==")).strip()
    expected = constraint.get("expected_value")
    observed = observed_value

    try:
        expected_num = float(expected)
        observed_num = float(observed)
        if operator == "<=":
            return observed_num > expected_num
        if operator == ">=":
            return observed_num < expected_num
        if operator == "<":
            return observed_num >= expected_num
        if operator == ">":
            return observed_num <= expected_num
        if operator == "==":
            return observed_num != expected_num
    except Exception:
        if operator == "==":
            return str(observed) != str(expected)
        if operator == "!=":
            return str(observed) == str(expected)
    return False


async def register_future_drift_and_replan(
    *,
    plan: WorkspaceHorizonPlan,
    actor: str,
    reason: str,
    drift_type: str,
    observed_value: str,
    metadata_json: dict,
    db: AsyncSession,
) -> dict:
    constraints = (
        plan.expected_future_constraints_json
        if isinstance(plan.expected_future_constraints_json, list)
        else []
    )

    broken = next((item for item in constraints if _drift_breaks_constraint(drift_type=drift_type, observed_value=observed_value, constraint=item)), None)
    if not broken:
        return {
            "replanned": False,
            "reason": "future_assumption_not_broken",
            "drift_type": drift_type,
            "observed_value": observed_value,
        }

    row = WorkspaceHorizonReplanEvent(
        plan_id=plan.id,
        actor=actor,
        reason=reason,
        drift_type=drift_type,
        observed_value=str(observed_value),
        expected_value=str(broken.get("expected_value", "")),
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    db.add(row)
    plan.status = "replanned"
    plan.explanation_json = {
        **(plan.explanation_json if isinstance(plan.explanation_json, dict) else {}),
        "last_replan": {
            "at": datetime.now(timezone.utc).isoformat(),
            "drift_type": drift_type,
            "observed_value": observed_value,
            "expected_value": broken.get("expected_value"),
            "reason": reason,
        },
    }

    checkpoints = await list_horizon_checkpoints(plan_id=plan.id, db=db)
    active = next((item for item in checkpoints if item.status == "active"), None)
    if active:
        active.status = "needs_re_evaluation"
    await db.flush()

    return {
        "replanned": True,
        "drift_type": drift_type,
        "observed_value": observed_value,
        "expected_value": broken.get("expected_value"),
    }


def to_horizon_checkpoint_out(row: WorkspaceHorizonCheckpoint) -> dict:
    return {
        "checkpoint_id": row.id,
        "plan_id": row.plan_id,
        "checkpoint_key": row.checkpoint_key,
        "sequence_index": row.sequence_index,
        "checkpoint_type": row.checkpoint_type,
        "status": row.status,
        "related_goal_key": row.related_goal_key,
        "trigger_conditions_json": row.trigger_conditions_json if isinstance(row.trigger_conditions_json, dict) else {},
        "replan_if_json": row.replan_if_json if isinstance(row.replan_if_json, list) else [],
        "explanation": row.explanation,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


def to_horizon_plan_out(row: WorkspaceHorizonPlan, checkpoints: list[WorkspaceHorizonCheckpoint]) -> dict:
    next_checkpoint = next((item for item in checkpoints if item.status == "active"), None)
    return {
        "plan_id": row.id,
        "actor": row.actor,
        "source": row.source,
        "status": row.status,
        "planning_horizon_minutes": row.planning_horizon_minutes,
        "ranked_goals": row.ranked_goals_json if isinstance(row.ranked_goals_json, list) else [],
        "staged_action_graph": row.staged_action_graph_json if isinstance(row.staged_action_graph_json, list) else [],
        "expected_future_constraints": row.expected_future_constraints_json if isinstance(row.expected_future_constraints_json, list) else [],
        "scoring_context": row.scoring_context_json if isinstance(row.scoring_context_json, dict) else {},
        "explanation": row.explanation_json if isinstance(row.explanation_json, dict) else {},
        "checkpoints": [to_horizon_checkpoint_out(item) for item in checkpoints],
        "next_checkpoint": to_horizon_checkpoint_out(next_checkpoint) if next_checkpoint else None,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
