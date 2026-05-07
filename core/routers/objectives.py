from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.models import Objective
from core.schemas import ObjectiveCreate

router = APIRouter()


@router.post("")
async def create_objective(payload: ObjectiveCreate, db: AsyncSession = Depends(get_db)) -> dict:
    objective = Objective(
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        constraints_json=payload.constraints,
        success_criteria=payload.success_criteria,
        state=payload.status,
        owner=payload.owner,
        execution_mode=payload.execution_mode,
        auto_continue=payload.auto_continue,
        boundary_mode=payload.boundary_mode,
        metadata_json=payload.metadata_json,
    )
    db.add(objective)
    await db.flush()
    await write_journal(
        db,
        actor="tod",
        action="create_objective",
        target_type="objective",
        target_id=str(objective.id),
        summary=f"Objective created: {objective.title}",
    )
    await db.commit()
    await db.refresh(objective)
    return {
        "objective_id": objective.id,
        "title": objective.title,
        "description": objective.description,
        "priority": objective.priority,
        "constraints": objective.constraints_json,
        "success_criteria": objective.success_criteria,
        "status": objective.state,
        "owner": objective.owner,
        "execution_mode": objective.execution_mode,
        "auto_continue": objective.auto_continue,
        "boundary_mode": objective.boundary_mode,
        "metadata_json": objective.metadata_json,
        "created_at": objective.created_at,
    }


@router.get("")
async def list_objectives(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Objective).order_by(Objective.id.desc()))).scalars().all()
    return [
        {
            "objective_id": item.id,
            "title": item.title,
            "description": item.description,
            "priority": item.priority,
            "constraints": item.constraints_json,
            "success_criteria": item.success_criteria,
            "status": item.state,
            "owner": item.owner,
            "execution_mode": item.execution_mode,
            "auto_continue": item.auto_continue,
            "boundary_mode": item.boundary_mode,
            "metadata_json": item.metadata_json,
            "created_at": item.created_at,
        }
        for item in rows
    ]
