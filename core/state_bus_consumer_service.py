from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import MemoryEntry, WorkspaceStateBusConsumer, WorkspaceStateBusEvent, WorkspaceStateBusSnapshot
from core.state_bus_service import append_state_bus_event, to_state_bus_event_out

STATE_BUS_CONSUMER_SOURCE = "objective72"


def _normalize_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        token = str(item or "").strip()
        if token and token not in cleaned:
            cleaned.append(token)
    return cleaned


def _normalized_subscription(payload: dict | None) -> dict:
    raw = payload if isinstance(payload, dict) else {}
    return {
        "domains": _normalize_list(raw.get("domains", [])),
        "event_types": _normalize_list(raw.get("event_types", [])),
        "sources": _normalize_list(raw.get("sources", [])),
        "stream_keys": _normalize_list(raw.get("stream_keys", [])),
    }


def _event_matches_subscription(event: WorkspaceStateBusEvent, subscription: dict) -> bool:
    domains = _normalize_list(subscription.get("domains", []))
    if domains and str(event.event_domain or "") not in domains:
        return False

    event_types = _normalize_list(subscription.get("event_types", []))
    if event_types and str(event.event_type or "") not in event_types:
        return False

    sources = _normalize_list(subscription.get("sources", []))
    if sources and str(event.source or "") not in sources:
        return False

    stream_keys = _normalize_list(subscription.get("stream_keys", []))
    if stream_keys and str(event.stream_key or "") not in stream_keys:
        return False

    return True


async def _lag_for_consumer(*, consumer: WorkspaceStateBusConsumer, db: AsyncSession) -> int:
    subscription = _normalized_subscription(consumer.subscription_json if isinstance(consumer.subscription_json, dict) else {})
    rows = (
        await db.execute(
            select(WorkspaceStateBusEvent)
            .where(WorkspaceStateBusEvent.id > int(consumer.cursor_event_id or 0))
            .order_by(WorkspaceStateBusEvent.id.asc())
            .limit(5000)
        )
    ).scalars().all()
    return sum(1 for row in rows if _event_matches_subscription(row, subscription))


async def upsert_state_bus_consumer(
    *,
    consumer_key: str,
    actor: str,
    source: str,
    status: str,
    subscription_json: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceStateBusConsumer:
    key = str(consumer_key or "").strip()
    if not key:
        raise ValueError("consumer_key_required")

    row = (
        await db.execute(
            select(WorkspaceStateBusConsumer)
            .where(WorkspaceStateBusConsumer.consumer_key == key)
            .limit(1)
        )
    ).scalars().first()

    normalized_subscription = _normalized_subscription(subscription_json if isinstance(subscription_json, dict) else {})
    now = datetime.now(timezone.utc)

    if row is None:
        row = WorkspaceStateBusConsumer(
            source=str(source or STATE_BUS_CONSUMER_SOURCE),
            actor=str(actor or "workspace"),
            consumer_key=key,
            status=str(status or "active"),
            subscription_json=normalized_subscription,
            metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
            updated_at=now,
        )
        db.add(row)
        await db.flush()
        row.lag_count = await _lag_for_consumer(consumer=row, db=db)
        return row

    row.source = str(source or row.source or STATE_BUS_CONSUMER_SOURCE)
    row.actor = str(actor or row.actor or "workspace")
    row.status = str(status or row.status or "active")
    row.subscription_json = normalized_subscription
    row.metadata_json = metadata_json if isinstance(metadata_json, dict) else {}
    row.updated_at = now
    await db.flush()
    row.lag_count = await _lag_for_consumer(consumer=row, db=db)
    return row


async def list_state_bus_consumers(
    *,
    status: str = "",
    limit: int = 100,
    db: AsyncSession,
) -> list[WorkspaceStateBusConsumer]:
    stmt = select(WorkspaceStateBusConsumer).order_by(WorkspaceStateBusConsumer.id.desc())
    if status.strip():
        stmt = stmt.where(WorkspaceStateBusConsumer.status == status.strip())
    stmt = stmt.limit(max(1, min(int(limit), 500)))
    rows = list((await db.execute(stmt)).scalars().all())
    for row in rows:
        row.lag_count = await _lag_for_consumer(consumer=row, db=db)
    return rows


async def get_state_bus_consumer(*, consumer_key: str, db: AsyncSession) -> WorkspaceStateBusConsumer | None:
    row = (
        await db.execute(
            select(WorkspaceStateBusConsumer)
            .where(WorkspaceStateBusConsumer.consumer_key == str(consumer_key or "").strip())
            .limit(1)
        )
    ).scalars().first()
    if row:
        row.lag_count = await _lag_for_consumer(consumer=row, db=db)
    return row


async def poll_state_bus_consumer(
    *,
    consumer_key: str,
    limit: int,
    db: AsyncSession,
) -> tuple[WorkspaceStateBusConsumer, list[WorkspaceStateBusEvent]]:
    row = await get_state_bus_consumer(consumer_key=consumer_key, db=db)
    if not row:
        raise ValueError("consumer_not_found")

    subscription = _normalized_subscription(row.subscription_json if isinstance(row.subscription_json, dict) else {})
    rows = (
        await db.execute(
            select(WorkspaceStateBusEvent)
            .where(WorkspaceStateBusEvent.id > int(row.cursor_event_id or 0))
            .order_by(WorkspaceStateBusEvent.id.asc())
            .limit(5000)
        )
    ).scalars().all()

    matched = [item for item in rows if _event_matches_subscription(item, subscription)]
    delivered = matched[: max(1, min(int(limit), 500))]

    row.poll_count = int(row.poll_count or 0) + 1
    row.last_polled_at = datetime.now(timezone.utc)
    row.lag_count = max(len(matched) - len(delivered), 0)
    row.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return row, delivered


async def acknowledge_state_bus_consumer(
    *,
    consumer_key: str,
    event_ids: list[int],
    db: AsyncSession,
) -> tuple[WorkspaceStateBusConsumer, list[int]]:
    row = await get_state_bus_consumer(consumer_key=consumer_key, db=db)
    if not row:
        raise ValueError("consumer_not_found")

    ack_ids = sorted({int(item) for item in event_ids if int(item) > 0})
    if not ack_ids:
        row.lag_count = await _lag_for_consumer(consumer=row, db=db)
        await db.flush()
        return row, []

    subscription = _normalized_subscription(row.subscription_json if isinstance(row.subscription_json, dict) else {})
    events = (
        await db.execute(
            select(WorkspaceStateBusEvent)
            .where(WorkspaceStateBusEvent.id.in_(ack_ids))
            .order_by(WorkspaceStateBusEvent.id.asc())
        )
    ).scalars().all()

    processed_set = {int(item) for item in (row.processed_event_ids_json if isinstance(row.processed_event_ids_json, list) else [])}
    accepted: list[int] = []
    accepted_events: list[WorkspaceStateBusEvent] = []
    for event in events:
        if int(event.id) in processed_set:
            continue
        if not _event_matches_subscription(event, subscription):
            continue
        accepted.append(int(event.id))
        accepted_events.append(event)

    if accepted:
        max_event = accepted_events[-1]
        row.cursor_event_id = max(int(row.cursor_event_id or 0), int(max_event.id))
        row.cursor_occurred_at = max_event.occurred_at
        merged = list(processed_set.union(set(accepted)))
        merged.sort()
        if len(merged) > 500:
            merged = merged[-500:]
        row.processed_event_ids_json = merged
        row.ack_count = int(row.ack_count or 0) + len(accepted)
        row.last_acked_at = datetime.now(timezone.utc)

    row.lag_count = await _lag_for_consumer(consumer=row, db=db)
    row.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return row, accepted


async def replay_state_bus_consumer(
    *,
    consumer_key: str,
    from_event_id: int | None,
    from_snapshot_scope: str,
    db: AsyncSession,
) -> WorkspaceStateBusConsumer:
    row = await get_state_bus_consumer(consumer_key=consumer_key, db=db)
    if not row:
        raise ValueError("consumer_not_found")

    replay_cursor = 0
    replay_scope = str(from_snapshot_scope or "").strip()
    if replay_scope:
        snapshot = (
            await db.execute(
                select(WorkspaceStateBusSnapshot)
                .where(WorkspaceStateBusSnapshot.snapshot_scope == replay_scope)
                .limit(1)
            )
        ).scalars().first()
        if not snapshot:
            raise ValueError("snapshot_not_found")
        replay_cursor = int(snapshot.last_event_id or 0)
    elif from_event_id and int(from_event_id) >= 0:
        replay_cursor = int(from_event_id)

    row.cursor_event_id = replay_cursor
    row.cursor_occurred_at = None
    row.processed_event_ids_json = []
    row.replay_from_snapshot_scope = replay_scope
    row.last_replayed_at = datetime.now(timezone.utc)
    row.updated_at = datetime.now(timezone.utc)
    row.lag_count = await _lag_for_consumer(consumer=row, db=db)
    await db.flush()
    return row


async def run_mim_core_consumer_step(
    *,
    actor: str,
    limit: int,
    metadata_json: dict,
    db: AsyncSession,
) -> dict:
    consumer = await upsert_state_bus_consumer(
        consumer_key="mim-core",
        actor=actor,
        source=STATE_BUS_CONSUMER_SOURCE,
        status="active",
        subscription_json={
            "domains": ["tod.runtime"],
            "event_types": ["execution.completed", "execution.failed"],
            "sources": [],
            "stream_keys": [],
        },
        metadata_json={
            "managed_by": "objective72",
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        },
        db=db,
    )

    consumer, events = await poll_state_bus_consumer(
        consumer_key=consumer.consumer_key,
        limit=max(1, min(int(limit), 200)),
        db=db,
    )

    consumed_ids: list[int] = []
    strategy_event_ids: list[int] = []
    memory_ids: list[int] = []

    for event in events:
        payload = event.payload_json if isinstance(event.payload_json, dict) else {}
        memory = MemoryEntry(
            memory_class="external_signal",
            content=f"TOD runtime event {event.event_type} consumed from state bus",
            summary=f"Consumed TOD runtime event {event.id} via Objective 72 state bus consumer",
            metadata_json={
                "event_id": int(event.id),
                "event_type": str(event.event_type),
                "event_domain": str(event.event_domain),
                "event_source": str(event.source),
                "stream_key": str(event.stream_key),
                "payload": payload,
                "consumer_key": consumer.consumer_key,
                **(metadata_json if isinstance(metadata_json, dict) else {}),
            },
        )
        db.add(memory)
        await db.flush()
        memory_ids.append(int(memory.id))

        strategy_event = await append_state_bus_event(
            actor=actor,
            source=STATE_BUS_CONSUMER_SOURCE,
            event_domain="mim.strategy",
            event_type="tod.execution.ingested",
            stream_key=f"mim-core:{consumer.consumer_key}",
            payload_json={
                "consumed_event_id": int(event.id),
                "consumed_event_type": str(event.event_type),
                "derived_memory_id": int(memory.id),
                "consumer_key": consumer.consumer_key,
            },
            metadata_json={
                "objective": "objective72",
                **(metadata_json if isinstance(metadata_json, dict) else {}),
            },
            db=db,
        )
        strategy_event_ids.append(int(strategy_event.id))
        consumed_ids.append(int(event.id))

    consumer, accepted_ids = await acknowledge_state_bus_consumer(
        consumer_key=consumer.consumer_key,
        event_ids=consumed_ids,
        db=db,
    )

    return {
        "consumer": to_state_bus_consumer_out(consumer),
        "consumed_event_ids": accepted_ids,
        "memory_ids": memory_ids,
        "strategy_event_ids": strategy_event_ids,
        "consumed_count": len(accepted_ids),
        "memory_written": len(memory_ids),
    }


def to_state_bus_consumer_out(row: WorkspaceStateBusConsumer) -> dict:
    updated_at = row.__dict__.get("updated_at")
    return {
        "consumer_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "consumer_key": row.consumer_key,
        "status": row.status,
        "subscription": _normalized_subscription(row.subscription_json if isinstance(row.subscription_json, dict) else {}),
        "cursor_event_id": int(row.cursor_event_id or 0),
        "cursor_occurred_at": row.cursor_occurred_at,
        "processed_event_ids": row.processed_event_ids_json if isinstance(row.processed_event_ids_json, list) else [],
        "poll_count": int(row.poll_count or 0),
        "ack_count": int(row.ack_count or 0),
        "lag_count": int(row.lag_count or 0),
        "replay_from_snapshot_scope": row.replay_from_snapshot_scope,
        "last_polled_at": row.last_polled_at,
        "last_acked_at": row.last_acked_at,
        "last_replayed_at": row.last_replayed_at,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "updated_at": updated_at if updated_at is not None else row.created_at,
        "created_at": row.created_at,
    }


def to_state_bus_consumer_event_out(events: list[WorkspaceStateBusEvent]) -> list[dict]:
    return [to_state_bus_event_out(item) for item in events]
