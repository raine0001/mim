from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.cross_domain_reasoning_service import to_cross_domain_reasoning_out
from core.db import get_db
from core.journal import write_journal
from core.orchestration_service import (
    build_cross_domain_task_orchestration,
    get_task_orchestration,
    inspect_collaboration_state,
    list_task_orchestrations,
    set_collaboration_mode_preference,
    to_task_orchestration_out,
)
from core.schemas import CollaborationModePreferenceRequest, CrossDomainTaskOrchestrationBuildRequest

router = APIRouter()


@router.post("/orchestration/build")
async def build_cross_domain_task_orchestration_endpoint(
    payload: CrossDomainTaskOrchestrationBuildRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row, context = await build_cross_domain_task_orchestration(
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        max_items_per_domain=payload.max_items_per_domain,
        min_context_confidence=payload.min_context_confidence,
        min_domains_required=payload.min_domains_required,
        dependency_resolution_policy=payload.dependency_resolution_policy,
        collaboration_mode_preference=payload.collaboration_mode_preference,
        task_kind=payload.task_kind,
        action_risk_level=payload.action_risk_level,
        communication_urgency_override=payload.communication_urgency_override,
        use_human_aware_signals=payload.use_human_aware_signals,
        generate_goal=payload.generate_goal,
        generate_horizon_plan=payload.generate_horizon_plan,
        generate_improvement_proposals=payload.generate_improvement_proposals,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="cross_domain_orchestration_built",
        target_type="workspace_task_orchestration",
        target_id=str(row.id),
        summary=f"Built cross-domain orchestration {row.id}",
        metadata_json={
            "source": payload.source,
            "lookback_hours": payload.lookback_hours,
            "min_context_confidence": payload.min_context_confidence,
            "min_domains_required": payload.min_domains_required,
            "dependency_resolution_policy": payload.dependency_resolution_policy,
            "collaboration_mode_preference": payload.collaboration_mode_preference,
            "task_kind": payload.task_kind,
            "action_risk_level": payload.action_risk_level,
            **payload.metadata_json,
        },
    )

    await db.commit()
    return {
        "orchestration": to_task_orchestration_out(row),
        "origin_context": to_cross_domain_reasoning_out(context),
    }


@router.get("/orchestration")
async def list_task_orchestrations_endpoint(
    status: str = Query(default=""),
    source: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_task_orchestrations(
        db=db,
        status=status,
        source=source,
        limit=limit,
    )
    return {
        "orchestrations": [to_task_orchestration_out(item) for item in rows],
    }


@router.get("/orchestration/{orchestration_id}")
async def get_task_orchestration_endpoint(
    orchestration_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_task_orchestration(orchestration_id=orchestration_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="orchestration_not_found")
    return {
        "orchestration": to_task_orchestration_out(row),
    }


@router.get("/orchestration/collaboration/state")
async def get_orchestration_collaboration_state_endpoint(
    lookback_hours: int = Query(default=24, ge=1, le=720),
    communication_urgency_override: float | None = Query(default=None, ge=0.0, le=1.0),
    db: AsyncSession = Depends(get_db),
) -> dict:
    state = await inspect_collaboration_state(
        lookback_hours=lookback_hours,
        communication_urgency_override=communication_urgency_override,
        db=db,
    )
    return {
        "collaboration": state,
    }


@router.post("/orchestration/collaboration/mode")
async def set_orchestration_collaboration_mode_endpoint(
    payload: CollaborationModePreferenceRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        row = await set_collaboration_mode_preference(
            actor=payload.actor,
            mode=payload.mode,
            reason=payload.reason,
            metadata_json=payload.metadata_json,
            db=db,
        )
    except ValueError as exc:
        if str(exc) == "invalid_collaboration_mode":
            raise HTTPException(status_code=422, detail="invalid_collaboration_mode")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="orchestration_collaboration_mode_set",
        target_type="user_preference",
        target_id=str(row.id),
        summary=f"Set orchestration collaboration mode preference to {payload.mode}",
        metadata_json={
            "mode": payload.mode,
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "updated": True,
        "preference_id": int(row.id),
        "mode": payload.mode,
    }
