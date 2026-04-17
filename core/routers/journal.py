from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.models import ExecutionJournal
from core.schemas import JournalCreate

router = APIRouter()


def _journal_entry_out(entry: ExecutionJournal) -> dict:
    metadata = entry.metadata_json if isinstance(entry.metadata_json, dict) else {}
    return {
        "entry_id": entry.id,
        "actor": entry.actor,
        "action": entry.action,
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "idempotency_key": entry.idempotency_key,
        "summary": entry.result,
        "metadata_json": metadata,
        "boundary_profile": metadata.get("boundary_profile", {}),
        "decision_basis": metadata.get("decision_basis", {}),
        "allowed_actions": metadata.get("allowed_actions", []),
        "approval_required": bool(metadata.get("approval_required", False)),
        "retry_policy": metadata.get("retry_policy", {}),
        "risk_level": str(metadata.get("risk_level") or "").strip(),
        "timestamp": entry.created_at,
    }


@router.post("")
async def create_journal(payload: JournalCreate, db: AsyncSession = Depends(get_db)) -> dict:
    if payload.idempotency_key:
        existing = (
            await db.execute(
                select(ExecutionJournal).where(ExecutionJournal.idempotency_key == payload.idempotency_key)
            )
        ).scalars().first()
        if existing:
            return _journal_entry_out(existing)

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
                return _journal_entry_out(existing)
        raise

    await db.refresh(entry)
    return _journal_entry_out(entry)


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
    return [_journal_entry_out(row) for row in rows]
