from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.maintenance_service import (
    get_maintenance_run,
    list_maintenance_actions_for_run,
    list_maintenance_runs,
    run_environment_maintenance_cycle,
    to_maintenance_run_out,
)
from core.models import WorkspaceEnvironmentStrategy
from core.schemas import MaintenanceCycleRequest

router = APIRouter()


@router.post("/maintenance/cycle")
async def run_maintenance_cycle_endpoint(
    payload: MaintenanceCycleRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    run, actions, strategies, memory_entries_created = await run_environment_maintenance_cycle(
        actor=payload.actor,
        source=payload.source,
        stale_after_seconds=payload.stale_after_seconds,
        max_strategies=payload.max_strategies,
        max_actions=payload.max_actions,
        auto_execute=payload.auto_execute,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="maintenance_cycle_completed",
        target_type="workspace_maintenance_run",
        target_id=str(run.id),
        summary=f"Maintenance run {run.id} completed",
        metadata_json={
            "degraded_signal_count": len(run.detected_signals_json if isinstance(run.detected_signals_json, list) else []),
            "strategies_created": len(strategies),
            "actions_executed": len(actions),
            "memory_entries_created": memory_entries_created,
            "stabilized": bool(run.stabilized),
            **payload.metadata_json,
        },
    )

    await db.commit()
    return {
        "run": to_maintenance_run_out(run, actions=actions, strategies=strategies),
    }


@router.get("/maintenance/runs")
async def list_maintenance_runs_endpoint(
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_maintenance_runs(db=db, limit=limit)
    return {
        "runs": [to_maintenance_run_out(item) for item in rows],
    }


@router.get("/maintenance/runs/{run_id}")
async def get_maintenance_run_endpoint(
    run_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_maintenance_run(run_id=run_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="maintenance_run_not_found")

    actions = await list_maintenance_actions_for_run(run_id=run_id, db=db)
    strategy_ids = row.created_strategy_ids_json if isinstance(row.created_strategy_ids_json, list) else []
    strategies = []
    if strategy_ids:
        strategy_rows = (
            await db.execute(
                select(WorkspaceEnvironmentStrategy).where(WorkspaceEnvironmentStrategy.id.in_(strategy_ids))
            )
        ).scalars().all()
        by_id = {int(item.id): item for item in strategy_rows}
        strategies = [by_id[item_id] for item_id in strategy_ids if int(item_id) in by_id]

    return {
        "run": to_maintenance_run_out(row, actions=actions, strategies=strategies),
    }
