from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.schemas import StewardshipCycleRequest
from core.stewardship_service import (
    get_stewardship_state,
    list_stewardship_history,
    list_stewardship_states,
    run_stewardship_cycle,
    to_stewardship_cycle_out,
    to_stewardship_out,
)

router = APIRouter()


@router.post("/stewardship/cycle")
async def run_stewardship_cycle_endpoint(
    payload: StewardshipCycleRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    stewardship, cycle, summary = await run_stewardship_cycle(
        actor=payload.actor,
        source=payload.source,
        managed_scope=payload.managed_scope,
        stale_after_seconds=payload.stale_after_seconds,
        lookback_hours=payload.lookback_hours,
        max_strategies=payload.max_strategies,
        max_actions=payload.max_actions,
        auto_execute=payload.auto_execute,
        force_degraded=payload.force_degraded,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="stewardship_cycle_completed",
        target_type="workspace_stewardship_state",
        target_id=str(stewardship.id),
        summary=f"Stewardship cycle {cycle.id} completed for stewardship {stewardship.id}",
        metadata_json={
            "stewardship_id": int(stewardship.id),
            "cycle_id": int(cycle.id),
            **summary,
            **payload.metadata_json,
        },
    )

    await db.commit()
    return {
        "stewardship": to_stewardship_out(stewardship),
        "cycle": to_stewardship_cycle_out(cycle),
        "summary": summary,
    }


@router.get("/stewardship")
async def list_stewardship_endpoint(
    managed_scope: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_stewardship_states(managed_scope=managed_scope, limit=limit, db=db)
    return {
        "stewardship": [to_stewardship_out(item) for item in rows],
    }


@router.get("/stewardship/history")
async def list_stewardship_history_endpoint(
    stewardship_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_stewardship_history(stewardship_id=stewardship_id, limit=limit, db=db)
    return {
        "history": [to_stewardship_cycle_out(item) for item in rows],
    }


@router.get("/stewardship/{stewardship_id}")
async def get_stewardship_endpoint(
    stewardship_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_stewardship_state(stewardship_id=stewardship_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="stewardship_not_found")
    return {
        "stewardship": to_stewardship_out(row),
    }
