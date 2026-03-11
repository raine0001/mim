from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.environment_strategy_service import (
    deactivate_environment_strategy,
    generate_environment_strategies,
    get_environment_strategy,
    list_environment_strategies,
    resolve_environment_strategy,
    to_environment_strategy_out,
)
from core.journal import write_journal
from core.schemas import (
    EnvironmentStrategyDeactivateRequest,
    EnvironmentStrategyGenerateRequest,
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
