from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.decision_record_service import get_decision_record, list_decision_records, to_decision_record_out

router = APIRouter()


@router.get("/planning/decisions")
async def list_planning_decisions(
    decision_type: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_decision_records(db=db, decision_type=decision_type, limit=limit)
    return {
        "decisions": [to_decision_record_out(item) for item in rows],
    }


@router.get("/planning/decisions/{decision_id}")
async def get_planning_decision(
    decision_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_decision_record(decision_id=decision_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="decision_record_not_found")
    return {
        "decision": to_decision_record_out(row),
    }
