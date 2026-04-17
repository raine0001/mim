from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import WorkspaceStateBusEvent, WorkspaceStateBusSnapshot

STATE_BUS_SOURCE = "objective71"
STATE_BUS_EVENT_DOMAINS = {
    "tod.runtime",
    "mim.perception",
    "mim.strategy",
    "mim.improvement",
    "mim.assist",
}


def _coerce_timestamp(value: object) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(timezone.utc)
    normalized = raw
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _event_domain_or_default(value: str) -> str:
    domain = str(value or "").strip().lower()
    if domain in STATE_BUS_EVENT_DOMAINS:
        return domain
    return "mim.strategy"


async def append_state_bus_event(
    *,
    actor: str,
    source: str,
    event_domain: str,
    event_type: str,
    stream_key: str,
    payload_json: dict,
    metadata_json: dict,
    occurred_at: str = "",
    db: AsyncSession,
) -> WorkspaceStateBusEvent:
    normalized_stream = str(stream_key or "global").strip() or "global"
    latest = (
        await db.execute(
            select(WorkspaceStateBusEvent)
            .where(WorkspaceStateBusEvent.stream_key == normalized_stream)
            .order_by(WorkspaceStateBusEvent.sequence_id.desc(), WorkspaceStateBusEvent.id.desc())
            .limit(1)
        )
    ).scalars().first()
    next_sequence = int(latest.sequence_id) + 1 if latest else 1

    row = WorkspaceStateBusEvent(
        source=str(source or STATE_BUS_SOURCE),
        actor=str(actor or "workspace"),
        event_domain=_event_domain_or_default(event_domain),
        event_type=str(event_type or "state.updated").strip() or "state.updated",
        stream_key=normalized_stream,
        sequence_id=next_sequence,
        occurred_at=_coerce_timestamp(occurred_at),
        payload_json=payload_json if isinstance(payload_json, dict) else {},
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    db.add(row)
    await db.flush()
    return row


async def list_state_bus_events(
    *,
    event_domain: str = "",
    stream_key: str = "",
    after_event_id: int = 0,
    limit: int = 100,
    db: AsyncSession,
) -> list[WorkspaceStateBusEvent]:
    stmt = select(WorkspaceStateBusEvent).order_by(WorkspaceStateBusEvent.id.desc())
    if event_domain.strip():
        stmt = stmt.where(WorkspaceStateBusEvent.event_domain == event_domain.strip().lower())
    if stream_key.strip():
        stmt = stmt.where(WorkspaceStateBusEvent.stream_key == stream_key.strip())
    if after_event_id > 0:
        stmt = stmt.where(WorkspaceStateBusEvent.id > int(after_event_id))
        stmt = stmt.order_by(WorkspaceStateBusEvent.id.asc())
    stmt = stmt.limit(max(1, min(int(limit), 500)))
    return list((await db.execute(stmt)).scalars().all())


async def get_state_bus_event(*, event_id: int, db: AsyncSession) -> WorkspaceStateBusEvent | None:
    return (
        await db.execute(
            select(WorkspaceStateBusEvent).where(WorkspaceStateBusEvent.id == int(event_id))
        )
    ).scalars().first()


async def upsert_state_bus_snapshot(
    *,
    actor: str,
    source: str,
    snapshot_scope: str,
    state_payload_json: dict,
    last_event_id: int | None,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceStateBusSnapshot:
    scope = str(snapshot_scope or "global").strip() or "global"
    row = (
        await db.execute(
            select(WorkspaceStateBusSnapshot)
            .where(WorkspaceStateBusSnapshot.snapshot_scope == scope)
            .limit(1)
        )
    ).scalars().first()

    linked_event: WorkspaceStateBusEvent | None = None
    if last_event_id and int(last_event_id) > 0:
        linked_event = await get_state_bus_event(event_id=int(last_event_id), db=db)

    if row is None:
        row = WorkspaceStateBusSnapshot(
            source=str(source or STATE_BUS_SOURCE),
            actor=str(actor or "workspace"),
            snapshot_scope=scope,
            state_version=1,
            state_payload_json=state_payload_json if isinstance(state_payload_json, dict) else {},
            last_event_id=int(last_event_id) if last_event_id else None,
            last_event_sequence=int(linked_event.sequence_id) if linked_event else 0,
            last_event_domain=str(linked_event.event_domain) if linked_event else "",
            last_event_type=str(linked_event.event_type) if linked_event else "",
            metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
            updated_at=datetime.now(timezone.utc),
        )
        db.add(row)
        await db.flush()
        return row

    row.source = str(source or row.source or STATE_BUS_SOURCE)
    row.actor = str(actor or row.actor or "workspace")
    row.state_version = int(row.state_version or 0) + 1
    row.state_payload_json = state_payload_json if isinstance(state_payload_json, dict) else {}
    row.last_event_id = int(last_event_id) if last_event_id else None
    row.last_event_sequence = int(linked_event.sequence_id) if linked_event else 0
    row.last_event_domain = str(linked_event.event_domain) if linked_event else ""
    row.last_event_type = str(linked_event.event_type) if linked_event else ""
    row.metadata_json = metadata_json if isinstance(metadata_json, dict) else {}
    row.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return row


async def list_state_bus_snapshots(
    *,
    snapshot_scope: str = "",
    limit: int = 100,
    db: AsyncSession,
) -> list[WorkspaceStateBusSnapshot]:
    stmt = select(WorkspaceStateBusSnapshot).order_by(WorkspaceStateBusSnapshot.id.desc())
    if snapshot_scope.strip():
        stmt = stmt.where(WorkspaceStateBusSnapshot.snapshot_scope == snapshot_scope.strip())
    stmt = stmt.limit(max(1, min(int(limit), 500)))
    return list((await db.execute(stmt)).scalars().all())


async def get_state_bus_snapshot(*, snapshot_scope: str, db: AsyncSession) -> WorkspaceStateBusSnapshot | None:
    return (
        await db.execute(
            select(WorkspaceStateBusSnapshot)
            .where(WorkspaceStateBusSnapshot.snapshot_scope == snapshot_scope.strip())
            .limit(1)
        )
    ).scalars().first()


def to_state_bus_event_out(row: WorkspaceStateBusEvent) -> dict:
    return {
        "event_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "event_domain": row.event_domain,
        "event_type": row.event_type,
        "stream_key": row.stream_key,
        "sequence_id": row.sequence_id,
        "occurred_at": row.occurred_at,
        "payload_json": row.payload_json if isinstance(row.payload_json, dict) else {},
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


def to_state_bus_snapshot_out(row: WorkspaceStateBusSnapshot) -> dict:
    updated_at = row.__dict__.get("updated_at")
    return {
        "snapshot_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "snapshot_scope": row.snapshot_scope,
        "state_version": row.state_version,
        "state_payload_json": row.state_payload_json if isinstance(row.state_payload_json, dict) else {},
        "last_event_id": row.last_event_id,
        "last_event_sequence": row.last_event_sequence,
        "last_event_domain": row.last_event_domain,
        "last_event_type": row.last_event_type,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "updated_at": updated_at if updated_at is not None else row.created_at,
        "created_at": row.created_at,
    }
