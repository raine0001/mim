from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.models import Task
from core.objective_lifecycle import recompute_objective_state
from core.schemas import TaskCreate

router = APIRouter()


@router.post("")
async def create_task(payload: TaskCreate, db: AsyncSession = Depends(get_db)) -> dict:
    task = Task(
        title=payload.title,
        details=payload.scope,
        dependencies=payload.dependencies,
        acceptance_criteria=payload.acceptance_criteria,
        assigned_to=payload.assigned_to,
        state=payload.status,
        objective_id=payload.objective_id,
        readiness=payload.readiness,
        boundary_mode=payload.boundary_mode,
        start_now=payload.start_now,
        human_prompt_required=payload.human_prompt_required,
        execution_scope=payload.execution_scope,
        expected_outputs_json=payload.expected_outputs,
        verification_commands_json=payload.verification_commands,
        dispatch_status=payload.dispatch_status,
        dispatch_artifact_json=payload.dispatch_artifact_json,
        metadata_json=payload.metadata_json,
    )
    db.add(task)
    await db.flush()
    await recompute_objective_state(db, payload.objective_id)
    await write_journal(
        db,
        actor="tod",
        action="create_task",
        target_type="task",
        target_id=str(task.id),
        summary=f"Task created: {task.title}",
    )
    await db.commit()
    await db.refresh(task)
    return {
        "task_id": task.id,
        "objective_id": task.objective_id,
        "title": task.title,
        "scope": task.details,
        "dependencies": task.dependencies,
        "acceptance_criteria": task.acceptance_criteria,
        "status": task.state,
        "assigned_to": task.assigned_to,
        "readiness": task.readiness,
        "boundary_mode": task.boundary_mode,
        "start_now": task.start_now,
        "human_prompt_required": task.human_prompt_required,
        "execution_scope": task.execution_scope,
        "expected_outputs": task.expected_outputs_json,
        "verification_commands": task.verification_commands_json,
        "dispatch_status": task.dispatch_status,
        "dispatch_artifact_json": task.dispatch_artifact_json,
        "metadata_json": task.metadata_json,
        "created_at": task.created_at,
    }


@router.get("")
async def list_tasks(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Task).order_by(Task.id.desc()))).scalars().all()
    return [
        {
            "task_id": item.id,
            "objective_id": item.objective_id,
            "title": item.title,
            "scope": item.details,
            "dependencies": item.dependencies,
            "acceptance_criteria": item.acceptance_criteria,
            "status": item.state,
            "assigned_to": item.assigned_to,
            "readiness": item.readiness,
            "boundary_mode": item.boundary_mode,
            "start_now": item.start_now,
            "human_prompt_required": item.human_prompt_required,
            "execution_scope": item.execution_scope,
            "expected_outputs": item.expected_outputs_json,
            "verification_commands": item.verification_commands_json,
            "dispatch_status": item.dispatch_status,
            "dispatch_artifact_json": item.dispatch_artifact_json,
            "metadata_json": item.metadata_json,
            "created_at": item.created_at,
        }
        for item in rows
    ]
