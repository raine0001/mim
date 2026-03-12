from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.policy_experiment_service import (
    get_policy_experiment,
    list_policy_experiments,
    run_policy_experiment,
    to_policy_experiment_out,
)
from core.schemas import PolicyExperimentRunRequest

router = APIRouter()


@router.post("/improvement/experiments/run")
async def run_policy_experiment_endpoint(
    payload: PolicyExperimentRunRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        experiment = await run_policy_experiment(
            actor=payload.actor,
            source=payload.source,
            proposal_id=payload.proposal_id,
            experiment_type=payload.experiment_type,
            lookback_hours=payload.lookback_hours,
            sandbox_mode=payload.sandbox_mode,
            metadata_json=payload.metadata_json,
            db=db,
        )
    except ValueError as exc:
        if str(exc) == "improvement_proposal_not_found":
            raise HTTPException(status_code=404, detail="improvement_proposal_not_found")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="policy_experiment_completed",
        target_type="workspace_policy_experiment",
        target_id=str(experiment.id),
        summary=f"Policy experiment {experiment.id} completed",
        metadata_json={
            "proposal_id": experiment.proposal_id,
            "experiment_type": experiment.experiment_type,
            "sandbox_mode": experiment.sandbox_mode,
            "recommendation": experiment.recommendation,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "experiment": to_policy_experiment_out(experiment),
    }


@router.get("/improvement/experiments")
async def list_policy_experiments_endpoint(
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_policy_experiments(db=db, limit=limit)
    return {
        "experiments": [to_policy_experiment_out(item) for item in rows],
    }


@router.get("/improvement/experiments/{experiment_id}")
async def get_policy_experiment_endpoint(
    experiment_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_policy_experiment(experiment_id=experiment_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="policy_experiment_not_found")
    return {
        "experiment": to_policy_experiment_out(row),
    }
