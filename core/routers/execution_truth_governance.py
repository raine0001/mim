from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.execution_truth_governance_service import (
    evaluate_execution_truth_governance,
    get_execution_truth_governance_profile,
    list_execution_truth_governance_profiles,
    to_execution_truth_governance_out,
)
from core.journal import write_journal
from core.schemas import ExecutionTruthGovernanceEvaluateRequest

router = APIRouter()


@router.post("/execution-truth/governance/evaluate")
async def evaluate_execution_truth_governance_endpoint(
    payload: ExecutionTruthGovernanceEvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await evaluate_execution_truth_governance(
        actor=payload.actor,
        source=payload.source,
        managed_scope=payload.managed_scope,
        lookback_hours=payload.lookback_hours,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="execution_truth_governance_evaluated",
        target_type="workspace_execution_truth_governance_profile",
        target_id=str(row.id),
        summary=f"Execution-truth governance {row.id} evaluated for scope {row.managed_scope}",
        metadata_json={
            "managed_scope": row.managed_scope,
            "governance_decision": row.governance_decision,
            "signal_count": int(row.signal_count or 0),
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {"governance": to_execution_truth_governance_out(row)}


@router.get("/execution-truth/governance")
async def list_execution_truth_governance_endpoint(
    managed_scope: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_execution_truth_governance_profiles(
        managed_scope=managed_scope,
        limit=limit,
        db=db,
    )
    return {"governance": [to_execution_truth_governance_out(row) for row in rows]}


@router.get("/execution-truth/governance/{governance_id}")
async def get_execution_truth_governance_endpoint(
    governance_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_execution_truth_governance_profile(
        governance_id=governance_id,
        db=db,
    )
    if not row:
        raise HTTPException(status_code=404, detail="execution_truth_governance_not_found")
    return {"governance": to_execution_truth_governance_out(row)}