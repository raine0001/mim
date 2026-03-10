from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.models import Task, TaskResult
from core.schemas import ResultCreate

router = APIRouter()


@router.post("")
async def create_result(payload: ResultCreate, db: AsyncSession = Depends(get_db)) -> dict:
    task = await db.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    task_result = TaskResult(
        task_id=payload.task_id,
        result=payload.summary,
        files_changed=payload.files_changed,
        tests_run=payload.tests_run,
        test_results=payload.test_results,
        failures=payload.failures,
        recommendations=payload.recommendations,
    )
    db.add(task_result)
    await db.flush()
    await write_journal(
        db,
        actor="tod",
        action="create_result",
        target_type="task_result",
        target_id=str(task_result.id),
        summary=f"Result recorded for task {payload.task_id}",
    )
    await db.commit()
    await db.refresh(task_result)
    return {
        "result_id": task_result.id,
        "task_id": task_result.task_id,
        "summary": task_result.result,
        "files_changed": task_result.files_changed,
        "tests_run": task_result.tests_run,
        "test_results": task_result.test_results,
        "failures": task_result.failures,
        "recommendations": task_result.recommendations,
        "created_at": task_result.created_at,
    }


@router.get("")
async def list_results(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(TaskResult).order_by(TaskResult.id.desc()))).scalars().all()
    return [
        {
            "result_id": r.id,
            "task_id": r.task_id,
            "summary": r.result,
            "files_changed": r.files_changed,
            "tests_run": r.tests_run,
            "test_results": r.test_results,
            "failures": r.failures,
            "recommendations": r.recommendations,
            "created_at": r.created_at,
        }
        for r in rows
    ]
