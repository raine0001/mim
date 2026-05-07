from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.autonomy_driver_service import continue_initiative
from core.db import get_db
from core.journal import write_journal
from core.models import Task, TaskResult
from core.objective_lifecycle import (
    derive_task_state_from_result,
    recompute_objective_state,
)
from core.schemas import ResultCreate

router = APIRouter()


def _coerce_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


@router.post("")
async def create_result(
    payload: ResultCreate, db: AsyncSession = Depends(get_db)
) -> dict:
    task = await db.get(Task, payload.task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")

    task.state = derive_task_state_from_result(
        test_results=payload.test_results,
        failures=payload.failures,
    )

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
    metadata_json = _coerce_dict(getattr(task, "metadata_json", {}))
    tracking = _coerce_dict(metadata_json.get("execution_tracking"))
    request_id = str(tracking.get("request_id") or f"task-result-{task.id}-{task_result.id}").strip()
    task.metadata_json = {
        **metadata_json,
        "execution_tracking": {
            "task_created": True,
            "task_dispatched": bool(
                tracking.get("task_dispatched")
                or str(getattr(task, "dispatch_status", "")).strip().lower()
                in {"queued", "running", "dispatched", "completed", "accepted"}
            ),
            "execution_started": True,
            "execution_result": payload.summary,
            "request_id": request_id,
            "execution_trace": str(
                tracking.get("execution_trace") or f"results_route:create_result:{task_result.id}"
            ).strip(),
            "result_artifact": str(
                tracking.get("result_artifact") or f"task_result:{task_result.id}"
            ).strip(),
        },
    }
    await write_journal(
        db,
        actor="tod",
        action="create_result",
        target_type="task_result",
        target_id=str(task_result.id),
        summary=f"Result recorded for task {payload.task_id}",
    )
    await recompute_objective_state(db, task.objective_id)
    continuation = await continue_initiative(
        db,
        objective_id=task.objective_id,
        actor="tod",
        source="results_route",
        max_auto_steps=3,
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
        "initiative": continuation,
        "created_at": task_result.created_at,
    }


@router.get("")
async def list_results(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (await db.execute(select(TaskResult).order_by(TaskResult.id.desc())))
        .scalars()
        .all()
    )
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
