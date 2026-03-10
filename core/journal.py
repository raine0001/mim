from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ExecutionJournal


async def write_journal(
    db: AsyncSession,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    summary: str,
    metadata_json: dict | None = None,
) -> ExecutionJournal:
    entry = ExecutionJournal(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result=summary,
        metadata_json=metadata_json or {},
    )
    db.add(entry)
    await db.flush()
    return entry
