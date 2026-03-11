from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.decision_record_service import record_decision
from core.environment_strategy_service import (
    deactivate_environment_strategy,
    generate_environment_strategies,
    generate_environment_strategies_from_routines,
    get_environment_strategy,
    list_environment_strategies,
    resolve_environment_strategy,
    to_environment_strategy_out,
)
from core.journal import write_journal
from core.schemas import (
    EnvironmentStrategyDeactivateRequest,
    EnvironmentStrategyGenerateRequest,
    EnvironmentStrategyRoutineGenerateRequest,
    EnvironmentStrategyResolveRequest,
)

router = APIRouter()


@router.post("/planning/strategies/generate")
async def generate_environment_strategies_endpoint(
    payload: EnvironmentStrategyGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    created = await generate_environment_strategies(
        actor=payload.actor,
        source=payload.source,
        observed_conditions=[item.model_dump() for item in payload.observed_conditions],
        min_severity=payload.min_severity,
        max_strategies=payload.max_strategies,
        metadata_json=payload.metadata_json,
        db=db,
        user_id="operator",
    )

    for strategy in created:
        await record_decision(
            decision_type="strategy_selection",
            source_context={
                "source": payload.source,
                "endpoint": "/planning/strategies/generate",
            },
            relevant_state={
                "conditions": [item.model_dump() for item in payload.observed_conditions],
            },
            preferences_applied=(strategy.metadata_json or {}).get("preference_context", {}),
            constraints_applied=[],
            strategies_applied=[],
            options_considered=[item.model_dump() for item in payload.observed_conditions],
            selected_option={
                "strategy_id": strategy.id,
                "strategy_type": strategy.strategy_type,
                "target_scope": strategy.target_scope,
            },
            decision_reason=str(strategy.status_reason or ""),
            confidence=float(strategy.influence_weight),
            resulting_goal_or_plan_id=f"strategy:{strategy.id}",
            metadata_json={"objective": "48", **payload.metadata_json},
            db=db,
        )

    await write_journal(
        db,
        actor=payload.actor,
        action="environment_strategies_generated",
        target_type="workspace_environment_strategy",
        target_id="batch",
        summary=f"Generated {len(created)} environment strategy(ies)",
        metadata_json={
            "source": payload.source,
            "conditions": len(payload.observed_conditions),
            "min_severity": payload.min_severity,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "generated": len(created),
        "strategies": [to_environment_strategy_out(item) for item in created],
    }


@router.post("/planning/strategies/routines/generate")
async def generate_environment_strategies_from_routines_endpoint(
    payload: EnvironmentStrategyRoutineGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    created = await generate_environment_strategies_from_routines(
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        min_occurrence_count=payload.min_occurrence_count,
        max_strategies=payload.max_strategies,
        metadata_json=payload.metadata_json,
        db=db,
        user_id="operator",
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="environment_strategies_generated_from_routines",
        target_type="workspace_environment_strategy",
        target_id="routine_batch",
        summary=f"Generated {len(created)} routine environment strategy(ies)",
        metadata_json={
            "source": payload.source,
            "lookback_hours": payload.lookback_hours,
            "min_occurrence_count": payload.min_occurrence_count,
            **payload.metadata_json,
        },
    )

    for strategy in created:
        await record_decision(
            decision_type="strategy_selection",
            source_context={
                "source": payload.source,
                "endpoint": "/planning/strategies/routines/generate",
            },
            relevant_state={
                "lookback_hours": payload.lookback_hours,
                "min_occurrence_count": payload.min_occurrence_count,
            },
            preferences_applied=(strategy.metadata_json or {}).get("preference_context", {}),
            constraints_applied=[],
            strategies_applied=[],
            options_considered=[{"strategy_type": strategy.strategy_type, "target_scope": strategy.target_scope}],
            selected_option={
                "strategy_id": strategy.id,
                "strategy_type": strategy.strategy_type,
                "target_scope": strategy.target_scope,
            },
            decision_reason=str(strategy.status_reason or "routine_pattern"),
            confidence=float(strategy.influence_weight),
            resulting_goal_or_plan_id=f"strategy:{strategy.id}",
            metadata_json={"objective": "48", "routine_generated": True, **payload.metadata_json},
            db=db,
        )

    await db.commit()
    return {
        "generated": len(created),
        "strategies": [to_environment_strategy_out(item) for item in created],
    }


@router.get("/planning/strategies")
async def list_environment_strategies_endpoint(
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_environment_strategies(db=db, status=status, limit=limit)
    return {
        "strategies": [to_environment_strategy_out(item) for item in rows],
    }


@router.get("/planning/strategies/{strategy_id}")
async def get_environment_strategy_endpoint(
    strategy_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_environment_strategy(strategy_id=strategy_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="environment_strategy_not_found")
    return {
        "strategy": to_environment_strategy_out(row),
    }


@router.post("/planning/strategies/{strategy_id}/resolve")
async def resolve_environment_strategy_endpoint(
    strategy_id: int,
    payload: EnvironmentStrategyResolveRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_environment_strategy(strategy_id=strategy_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="environment_strategy_not_found")

    await resolve_environment_strategy(
        row=row,
        status=payload.status,
        reason=payload.reason,
        metadata_json=payload.metadata_json,
    )

    await record_decision(
        decision_type="strategy_selection",
        source_context={"endpoint": f"/planning/strategies/{strategy_id}/resolve"},
        relevant_state={"previous_status": row.current_status},
        preferences_applied=(row.metadata_json or {}).get("preference_context", {}),
        constraints_applied=[],
        strategies_applied=[{"strategy_id": row.id, "strategy_type": row.strategy_type}],
        options_considered=[{"status": payload.status}],
        selected_option={"status": payload.status},
        decision_reason=payload.reason,
        confidence=float(row.influence_weight),
        resulting_goal_or_plan_id=f"strategy:{row.id}",
        metadata_json={"objective": "48", **payload.metadata_json},
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="environment_strategy_resolved",
        target_type="workspace_environment_strategy",
        target_id=str(strategy_id),
        summary=f"Strategy {strategy_id} set to {payload.status}",
        metadata_json={
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "updated": True,
        "strategy": to_environment_strategy_out(row),
    }


@router.post("/planning/strategies/{strategy_id}/deactivate")
async def deactivate_environment_strategy_endpoint(
    strategy_id: int,
    payload: EnvironmentStrategyDeactivateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_environment_strategy(strategy_id=strategy_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="environment_strategy_not_found")

    await deactivate_environment_strategy(
        row=row,
        reason=payload.reason,
        metadata_json=payload.metadata_json,
    )

    await record_decision(
        decision_type="strategy_selection",
        source_context={"endpoint": f"/planning/strategies/{strategy_id}/deactivate"},
        relevant_state={"strategy_status": row.current_status},
        preferences_applied=(row.metadata_json or {}).get("preference_context", {}),
        constraints_applied=[],
        strategies_applied=[{"strategy_id": row.id, "strategy_type": row.strategy_type}],
        options_considered=[{"action": "deactivate"}],
        selected_option={"status": "superseded"},
        decision_reason=payload.reason,
        confidence=float(row.influence_weight),
        resulting_goal_or_plan_id=f"strategy:{row.id}",
        metadata_json={"objective": "48", **payload.metadata_json},
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="environment_strategy_deactivated",
        target_type="workspace_environment_strategy",
        target_id=str(strategy_id),
        summary=f"Strategy {strategy_id} deactivated",
        metadata_json={
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "updated": True,
        "strategy": to_environment_strategy_out(row),
    }
