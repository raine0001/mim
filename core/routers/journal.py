from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.models import ExecutionJournal
from core.schemas import JournalCreate

router = APIRouter()


@router.post("")
async def create_journal(payload: JournalCreate, db: AsyncSession = Depends(get_db)) -> dict:
    if payload.idempotency_key:
        existing = (
            await db.execute(
                select(ExecutionJournal).where(ExecutionJournal.idempotency_key == payload.idempotency_key)
            )
        ).scalars().first()
        if existing:
            return {
                "entry_id": existing.id,
                "actor": existing.actor,
                "action": existing.action,
                "target_type": existing.target_type,
                "target_id": existing.target_id,
                "idempotency_key": existing.idempotency_key,
                "summary": existing.result,
                "timestamp": existing.created_at,
            }

    entry = ExecutionJournal(
        actor=payload.actor,
        action=payload.action,
        target_type=payload.target_type,
        target_id=payload.target_id,
        idempotency_key=payload.idempotency_key,
        result=payload.summary,
        metadata_json=payload.metadata_json,
    )
    db.add(entry)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if payload.idempotency_key:
            existing = (
                await db.execute(
                    select(ExecutionJournal).where(ExecutionJournal.idempotency_key == payload.idempotency_key)
                )
            ).scalars().first()
            if existing:
                return {
                    "entry_id": existing.id,
                    "actor": existing.actor,
                    "action": existing.action,
                    "target_type": existing.target_type,
                    "target_id": existing.target_id,
                    "idempotency_key": existing.idempotency_key,
                    "summary": existing.result,
                    "timestamp": existing.created_at,
                }
        raise

    await db.refresh(entry)
    return {
        "entry_id": entry.id,
        "actor": entry.actor,
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "idempotency_key": entry.idempotency_key,
        "summary": entry.result,
        "timestamp": entry.created_at,
    }


@router.get("")
async def list_journal(
    limit: int = Query(default=500, ge=1, le=5000),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = (
        await db.execute(
            select(ExecutionJournal)
            .order_by(ExecutionJournal.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "entry_id": row.id,
            "actor": row.actor,
            "action": row.action,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "idempotency_key": row.idempotency_key,
            "summary": row.result,
            "timestamp": row.created_at,
        }
        for row in rows
    ]
