from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.autonomy_driver_service import continue_initiative
from core.db import get_db
from core.journal import write_journal
from core.models import Task, TaskReview
from core.objective_lifecycle import (
    derive_task_state_from_review,
    recompute_objective_state,
)
from core.schemas import ReviewCreate

router = APIRouter()


@router.post("")
async def create_review(
    payload: ReviewCreate, db: AsyncSession = Depends(get_db)
) -> dict:
    task = await db.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    review = TaskReview(
        task_id=payload.task_id,
        status=payload.decision,
        notes=payload.rationale,
        continue_allowed=payload.continue_allowed,
        escalate_to_user=payload.escalate_to_user,
    )
    db.add(review)

    task.state = derive_task_state_from_review(
        decision=payload.decision,
        continue_allowed=payload.continue_allowed,
    )
    await db.flush()
    await write_journal(
        db,
        actor="tod",
        action="create_review",
        target_type="task_review",
        target_id=str(review.id),
        summary=f"Review recorded for task {payload.task_id}: {payload.decision}",
    )
    await recompute_objective_state(db, task.objective_id)
    continuation = await continue_initiative(
        db,
        objective_id=task.objective_id,
        actor="tod",
        source="reviews_route",
        max_auto_steps=3,
    )
    await db.commit()
    await db.refresh(review)
    return {
        "review_id": review.id,
        "task_id": review.task_id,
        "decision": review.status,
        "rationale": review.notes,
        "continue_allowed": review.continue_allowed,
        "escalate_to_user": review.escalate_to_user,
        "initiative": continuation,
        "created_at": review.created_at,
    }


@router.get("")
async def list_reviews(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (await db.execute(select(TaskReview).order_by(TaskReview.id.desc())))
        .scalars()
        .all()
    )
    return [
        {
            "review_id": row.id,
            "task_id": row.task_id,
            "decision": row.status,
            "rationale": row.notes,
            "continue_allowed": row.continue_allowed,
            "escalate_to_user": row.escalate_to_user,
            "created_at": row.created_at,
        }
        for row in rows
    ]
