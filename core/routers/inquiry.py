from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.inquiry_service import (
    answer_inquiry_question,
    generate_inquiry_questions,
    get_inquiry_question,
    list_inquiry_questions,
    to_inquiry_question_out,
)
from core.journal import write_journal
from core.schemas import InquiryQuestionAnswerRequest, InquiryQuestionGenerateRequest

router = APIRouter()


@router.post("/inquiry/questions/generate")
async def generate_inquiry_questions_endpoint(
    payload: InquiryQuestionGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await generate_inquiry_questions(
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        max_questions=payload.max_questions,
        min_soft_friction_count=payload.min_soft_friction_count,
        metadata_json=payload.metadata_json,
        db=db,
    )

    await write_journal(
        db,
        actor=payload.actor,
        action="inquiry_questions_generated",
        target_type="workspace_inquiry_question",
        target_id="batch",
        summary=f"Generated/loaded {len(rows)} inquiry question(s)",
        metadata_json={
            "source": payload.source,
            "lookback_hours": payload.lookback_hours,
            "max_questions": payload.max_questions,
            "min_soft_friction_count": payload.min_soft_friction_count,
            **payload.metadata_json,
        },
    )

    await db.commit()
    return {
        "generated": len(rows),
        "questions": [to_inquiry_question_out(item) for item in rows],
    }


@router.get("/inquiry/questions")
async def list_inquiry_questions_endpoint(
    status: str = Query(default=""),
    uncertainty_type: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_inquiry_questions(
        db=db,
        status=status,
        uncertainty_type=uncertainty_type,
        limit=limit,
    )
    return {
        "questions": [to_inquiry_question_out(item) for item in rows],
    }


@router.get("/inquiry/questions/{question_id}")
async def get_inquiry_question_endpoint(
    question_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_inquiry_question(question_id=question_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="inquiry_question_not_found")
    return {"question": to_inquiry_question_out(row)}


@router.post("/inquiry/questions/{question_id}/answer")
async def answer_inquiry_question_endpoint(
    question_id: int,
    payload: InquiryQuestionAnswerRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_inquiry_question(question_id=question_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="inquiry_question_not_found")

    try:
        updated, applied_effect = await answer_inquiry_question(
            row=row,
            actor=payload.actor,
            selected_path_id=payload.selected_path_id,
            answer_json=payload.answer_json,
            metadata_json=payload.metadata_json,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await write_journal(
        db,
        actor=payload.actor,
        action="inquiry_question_answered",
        target_type="workspace_inquiry_question",
        target_id=str(question_id),
        summary=f"Answered inquiry question {question_id} path={payload.selected_path_id}",
        metadata_json={
            "selected_path_id": payload.selected_path_id,
            "applied_effect": applied_effect,
            **payload.metadata_json,
        },
    )

    await db.commit()
    return {
        "answered": True,
        "applied_effect": applied_effect,
        "question": to_inquiry_question_out(updated),
    }
