from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.decision_record_service import record_decision
from core.horizon_planning_service import (
    advance_horizon_checkpoint,
    create_horizon_plan,
    get_current_horizon_plan,
    get_horizon_plan,
    list_horizon_checkpoints,
    register_future_drift_and_replan,
    to_horizon_plan_out,
)
from core.journal import write_journal
from core.schemas import HorizonCheckpointAdvanceRequest, HorizonFutureDriftRequest, HorizonPlanCreateRequest

router = APIRouter()


@router.post("/planning/horizon/plans")
async def create_horizon_plan_endpoint(
    payload: HorizonPlanCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    plan, checkpoints = await create_horizon_plan(
        actor=payload.actor,
        source=payload.source,
        planning_horizon_minutes=payload.planning_horizon_minutes,
        goal_candidates=[item.model_dump() for item in payload.goal_candidates],
        expected_future_constraints=payload.expected_future_constraints,
        priority_policy=payload.priority_policy,
        map_freshness_seconds=payload.map_freshness_seconds,
        object_confidence=payload.object_confidence,
        human_aware_state=payload.human_aware_state,
        operator_preferences=payload.operator_preferences,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="horizon_plan_created",
        target_type="workspace_horizon_plan",
        target_id=str(plan.id),
        summary=f"Created horizon plan {plan.id} with {len(checkpoints)} checkpoint(s)",
        metadata_json={
            "source": payload.source,
            "planning_horizon_minutes": payload.planning_horizon_minutes,
            "goal_candidates": len(payload.goal_candidates),
            **payload.metadata_json,
        },
    )

    await record_decision(
        decision_type="plan_selection",
        source_context={
            "endpoint": "/planning/horizon/plans",
            "source": payload.source,
        },
        relevant_state={
            "planning_horizon_minutes": payload.planning_horizon_minutes,
            "map_freshness_seconds": payload.map_freshness_seconds,
            "object_confidence": payload.object_confidence,
            "human_aware_state": payload.human_aware_state,
        },
        preferences_applied=payload.operator_preferences if isinstance(payload.operator_preferences, dict) else {},
        constraints_applied=plan.expected_future_constraints_json if isinstance(plan.expected_future_constraints_json, list) else [],
        strategies_applied=(plan.explanation_json or {}).get("strategy_context", []) if isinstance(plan.explanation_json, dict) else [],
        options_considered=[item.model_dump() for item in payload.goal_candidates],
        selected_option={
            "plan_id": plan.id,
            "top_ranked_goal": (plan.ranked_goals_json[0] if isinstance(plan.ranked_goals_json, list) and plan.ranked_goals_json else {}),
        },
        decision_reason=str((plan.explanation_json or {}).get("selected_plan_reason", "")),
        confidence=float((plan.ranked_goals_json[0] or {}).get("score", 0.0)) if isinstance(plan.ranked_goals_json, list) and plan.ranked_goals_json else 0.0,
        resulting_goal_or_plan_id=f"plan:{plan.id}",
        metadata_json={"objective": "48", **payload.metadata_json},
        db=db,
    )

    await db.commit()
    return to_horizon_plan_out(plan, checkpoints)


@router.get("/planning/horizon/plans/current")
async def get_current_horizon_plan_endpoint(
    db: AsyncSession = Depends(get_db),
) -> dict:
    plan = await get_current_horizon_plan(db=db)
    if not plan:
        return {"plan": None}
    checkpoints = await list_horizon_checkpoints(plan_id=plan.id, db=db)
    return {"plan": to_horizon_plan_out(plan, checkpoints)}


@router.get("/planning/horizon/plans/{plan_id}")
async def get_horizon_plan_endpoint(
    plan_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    plan = await get_horizon_plan(plan_id=plan_id, db=db)
    if not plan:
        raise HTTPException(status_code=404, detail="horizon_plan_not_found")
    checkpoints = await list_horizon_checkpoints(plan_id=plan_id, db=db)
    return {"plan": to_horizon_plan_out(plan, checkpoints)}


@router.post("/planning/horizon/plans/{plan_id}/checkpoints/advance")
async def advance_horizon_checkpoint_endpoint(
    plan_id: int,
    payload: HorizonCheckpointAdvanceRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    plan = await get_horizon_plan(plan_id=plan_id, db=db)
    if not plan:
        raise HTTPException(status_code=404, detail="horizon_plan_not_found")

    reached, next_checkpoint = await advance_horizon_checkpoint(
        plan=plan,
        outcome=payload.outcome,
        actor=payload.actor,
        reason=payload.reason,
        checkpoint_id=payload.checkpoint_id,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="horizon_checkpoint_advanced",
        target_type="workspace_horizon_plan",
        target_id=str(plan_id),
        summary=f"Advanced horizon plan {plan_id} checkpoint outcome={payload.outcome}",
        metadata_json={
            "checkpoint_id": reached.id if reached else None,
            "next_checkpoint_id": next_checkpoint.id if next_checkpoint else None,
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )

    checkpoints = await list_horizon_checkpoints(plan_id=plan_id, db=db)
    await db.commit()
    return {
        "advanced": True,
        "outcome": payload.outcome,
        "plan": to_horizon_plan_out(plan, checkpoints),
    }


@router.post("/planning/horizon/plans/{plan_id}/future-drift")
async def report_horizon_future_drift_endpoint(
    plan_id: int,
    payload: HorizonFutureDriftRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    plan = await get_horizon_plan(plan_id=plan_id, db=db)
    if not plan:
        raise HTTPException(status_code=404, detail="horizon_plan_not_found")

    drift = await register_future_drift_and_replan(
        plan=plan,
        actor=payload.actor,
        reason=payload.reason,
        drift_type=payload.drift_type,
        observed_value=payload.observed_value,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="horizon_future_drift_reported",
        target_type="workspace_horizon_plan",
        target_id=str(plan_id),
        summary=f"Horizon plan {plan_id} future drift observed for {payload.drift_type}",
        metadata_json={
            "reason": payload.reason,
            **drift,
            **payload.metadata_json,
        },
    )

    checkpoints = await list_horizon_checkpoints(plan_id=plan_id, db=db)
    await db.commit()
    return {
        **drift,
        "plan": to_horizon_plan_out(plan, checkpoints),
    }
