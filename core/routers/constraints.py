from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.constraint_service import (
    evaluate_and_record_constraints,
    get_last_constraint_evaluation,
    list_constraint_evaluations,
    to_constraint_evaluation_out,
)
from core.db import get_db
from core.journal import write_journal
from core.schemas import ConstraintEvaluateRequest

router = APIRouter()


@router.post("/constraints/evaluate")
async def evaluate_constraints_endpoint(
    payload: ConstraintEvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row, evaluation = await evaluate_and_record_constraints(
        actor=payload.actor,
        source=payload.source,
        goal=payload.goal,
        action_plan=payload.action_plan,
        workspace_state=payload.workspace_state,
        system_state=payload.system_state,
        policy_state=payload.policy_state,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="constraint_evaluation",
        target_type="constraint_evaluation",
        target_id=str(row.id),
        summary=f"Constraint evaluation {row.id} decision={row.decision}",
        metadata_json={
            "source": payload.source,
            "decision": row.decision,
            "recommended_next_step": row.recommended_next_step,
            "hard_violations": len(row.violations_json if isinstance(row.violations_json, list) else []),
            "soft_warnings": len(row.warnings_json if isinstance(row.warnings_json, list) else []),
            **payload.metadata_json,
        },
    )

    await db.commit()
    await db.refresh(row)
    return {
        **to_constraint_evaluation_out(row),
        "decision": evaluation.get("decision", row.decision),
    }


@router.get("/constraints/last-evaluation")
async def get_last_constraints_evaluation(db: AsyncSession = Depends(get_db)) -> dict:
    row = await get_last_constraint_evaluation(db)
    if not row:
        return {
            "evaluation": None,
        }
    return {
        "evaluation": to_constraint_evaluation_out(row),
    }


@router.get("/constraints/history")
async def get_constraints_history(
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_constraint_evaluations(db=db, limit=limit)
    return {
        "evaluations": [to_constraint_evaluation_out(item) for item in rows],
    }
