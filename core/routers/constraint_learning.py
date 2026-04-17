from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.constraint_learning_service import (
    compute_constraint_learning_stats,
    generate_constraint_adjustment_proposals,
    list_constraint_adjustment_proposals,
    record_constraint_outcome,
    to_constraint_adjustment_proposal_out,
)
from core.db import get_db
from core.journal import write_journal
from core.schemas import ConstraintLearningGenerateProposalsRequest, ConstraintOutcomeRecordRequest

router = APIRouter()


@router.post("/constraints/outcomes")
async def record_constraint_outcome_endpoint(
    payload: ConstraintOutcomeRecordRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await record_constraint_outcome(
        evaluation_id=payload.evaluation_id,
        result=payload.result,
        outcome_quality=payload.outcome_quality,
        db=db,
    )
    if not row:
        return {
            "updated": False,
            "reason": "constraint_evaluation_not_found",
            "evaluation_id": payload.evaluation_id,
        }

    await write_journal(
        db,
        actor=payload.actor,
        action="constraint_outcome_recorded",
        target_type="constraint_evaluation",
        target_id=str(row.id),
        summary=f"Constraint evaluation {row.id} outcome={row.outcome_result}",
        metadata_json={
            "outcome_quality": float(row.outcome_quality),
            **payload.metadata_json,
        },
    )

    await db.commit()
    await db.refresh(row)
    return {
        "updated": True,
        "evaluation_id": row.id,
        "outcome_result": row.outcome_result,
        "outcome_quality": float(row.outcome_quality),
        "outcome_recorded_at": row.outcome_recorded_at,
    }


@router.get("/constraints/learning/stats")
async def get_constraint_learning_stats(
    constraint_key: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await compute_constraint_learning_stats(
        db=db,
        constraint_key=constraint_key,
        limit=limit,
    )
    return {
        "stats": rows,
    }


@router.post("/constraints/learning/proposals/generate")
async def generate_constraint_learning_proposals(
    payload: ConstraintLearningGenerateProposalsRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    proposals = await generate_constraint_adjustment_proposals(
        actor=payload.actor,
        source=payload.source,
        min_samples=payload.min_samples,
        success_rate_threshold=payload.success_rate_threshold,
        max_proposals=payload.max_proposals,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="constraint_learning_proposals_generated",
        target_type="constraint_learning",
        target_id="proposal_batch",
        summary=f"Generated {len(proposals)} constraint adjustment proposal(s)",
        metadata_json={
            "min_samples": payload.min_samples,
            "success_rate_threshold": payload.success_rate_threshold,
            "max_proposals": payload.max_proposals,
            **payload.metadata_json,
        },
    )

    await db.commit()
    for row in proposals:
        await db.refresh(row)

    return {
        "generated": len(proposals),
        "proposals": [to_constraint_adjustment_proposal_out(item) for item in proposals],
    }


@router.get("/constraints/learning/proposals")
async def list_constraint_learning_proposals(
    status: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_constraint_adjustment_proposals(db=db, status=status, limit=limit)
    return {
        "proposals": [to_constraint_adjustment_proposal_out(item) for item in rows],
    }
