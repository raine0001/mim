from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.schemas import (
    StateBusConsumerAckRequest,
    StateBusConsumerPollRequest,
    StateBusConsumerRegisterRequest,
    StateBusConsumerReplayRequest,
    StateBusEventCreateRequest,
    StateBusMimCoreStepRequest,
    StateBusSnapshotUpsertRequest,
)
from core.state_bus_consumer_service import (
    acknowledge_state_bus_consumer,
    get_state_bus_consumer,
    list_state_bus_consumers,
    poll_state_bus_consumer,
    replay_state_bus_consumer,
    run_mim_core_consumer_step,
    to_state_bus_consumer_event_out,
    to_state_bus_consumer_out,
    upsert_state_bus_consumer,
)
from core.state_bus_service import (
    append_state_bus_event,
    get_state_bus_event,
    get_state_bus_snapshot,
    list_state_bus_events,
    list_state_bus_snapshots,
    to_state_bus_event_out,
    to_state_bus_snapshot_out,
    upsert_state_bus_snapshot,
)

router = APIRouter()


@router.post("/state-bus/events")
async def append_state_bus_event_endpoint(
    payload: StateBusEventCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await append_state_bus_event(
        actor=payload.actor,
        source=payload.source,
        event_domain=payload.event_domain,
        event_type=payload.event_type,
        stream_key=payload.stream_key,
        occurred_at=payload.occurred_at,
        payload_json=payload.payload_json,
        metadata_json=payload.metadata_json,
        db=db,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="state_bus_event_appended",
        target_type="workspace_state_bus_event",
        target_id=str(row.id),
        summary=f"Appended state bus event {row.id} ({row.event_domain}:{row.event_type})",
        metadata_json={
            "event_domain": row.event_domain,
            "event_type": row.event_type,
            "stream_key": row.stream_key,
            "sequence_id": row.sequence_id,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {"event": to_state_bus_event_out(row)}


@router.get("/state-bus/events")
async def list_state_bus_events_endpoint(
    event_domain: str = Query(default=""),
    stream_key: str = Query(default=""),
    after_event_id: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_state_bus_events(
        event_domain=event_domain,
        stream_key=stream_key,
        after_event_id=after_event_id,
        limit=limit,
        db=db,
    )
    return {"events": [to_state_bus_event_out(item) for item in rows]}


@router.get("/state-bus/events/{event_id}")
async def get_state_bus_event_endpoint(
    event_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_state_bus_event(event_id=event_id, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="state_bus_event_not_found")
    return {"event": to_state_bus_event_out(row)}


@router.post("/state-bus/snapshots/{snapshot_scope}")
async def upsert_state_bus_snapshot_endpoint(
    snapshot_scope: str,
    payload: StateBusSnapshotUpsertRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await upsert_state_bus_snapshot(
        actor=payload.actor,
        source=payload.source,
        snapshot_scope=snapshot_scope,
        state_payload_json=payload.state_payload_json,
        last_event_id=payload.last_event_id,
        metadata_json=payload.metadata_json,
        db=db,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="state_bus_snapshot_upserted",
        target_type="workspace_state_bus_snapshot",
        target_id=row.snapshot_scope,
        summary=f"Upserted state bus snapshot for {row.snapshot_scope}",
        metadata_json={
            "state_version": row.state_version,
            "last_event_id": row.last_event_id,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {"snapshot": to_state_bus_snapshot_out(row)}


@router.get("/state-bus/snapshots")
async def list_state_bus_snapshots_endpoint(
    snapshot_scope: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_state_bus_snapshots(
        snapshot_scope=snapshot_scope,
        limit=limit,
        db=db,
    )
    return {"snapshots": [to_state_bus_snapshot_out(item) for item in rows]}


@router.get("/state-bus/snapshots/{snapshot_scope}")
async def get_state_bus_snapshot_endpoint(
    snapshot_scope: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_state_bus_snapshot(snapshot_scope=snapshot_scope, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="state_bus_snapshot_not_found")
    return {"snapshot": to_state_bus_snapshot_out(row)}


@router.post("/state-bus/consumers/{consumer_key}")
async def register_state_bus_consumer_endpoint(
    consumer_key: str,
    payload: StateBusConsumerRegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await upsert_state_bus_consumer(
        consumer_key=consumer_key,
        actor=payload.actor,
        source=payload.source,
        status=payload.status,
        subscription_json={
            "domains": payload.subscription_domains,
            "event_types": payload.subscription_event_types,
            "sources": payload.subscription_sources,
            "stream_keys": payload.subscription_stream_keys,
        },
        metadata_json=payload.metadata_json,
        db=db,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="state_bus_consumer_upserted",
        target_type="workspace_state_bus_consumer",
        target_id=row.consumer_key,
        summary=f"Registered state bus consumer {row.consumer_key}",
        metadata_json={
            "status": row.status,
            "subscription": row.subscription_json,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {"consumer": to_state_bus_consumer_out(row)}


@router.get("/state-bus/consumers")
async def list_state_bus_consumers_endpoint(
    status: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_state_bus_consumers(status=status, limit=limit, db=db)
    return {"consumers": [to_state_bus_consumer_out(item) for item in rows]}


@router.get("/state-bus/consumers/{consumer_key}")
async def get_state_bus_consumer_endpoint(
    consumer_key: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_state_bus_consumer(consumer_key=consumer_key, db=db)
    if not row:
        raise HTTPException(status_code=404, detail="state_bus_consumer_not_found")
    return {"consumer": to_state_bus_consumer_out(row)}


@router.post("/state-bus/consumers/{consumer_key}/poll")
async def poll_state_bus_consumer_endpoint(
    consumer_key: str,
    payload: StateBusConsumerPollRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        row, events = await poll_state_bus_consumer(
            consumer_key=consumer_key,
            limit=payload.limit,
            db=db,
        )
    except ValueError as exc:
        if str(exc) == "consumer_not_found":
            raise HTTPException(status_code=404, detail="state_bus_consumer_not_found")
        raise
    await db.commit()
    return {
        "consumer": to_state_bus_consumer_out(row),
        "events": to_state_bus_consumer_event_out(events),
    }


@router.post("/state-bus/consumers/{consumer_key}/ack")
async def ack_state_bus_consumer_endpoint(
    consumer_key: str,
    payload: StateBusConsumerAckRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        row, accepted = await acknowledge_state_bus_consumer(
            consumer_key=consumer_key,
            event_ids=payload.event_ids,
            db=db,
        )
    except ValueError as exc:
        if str(exc) == "consumer_not_found":
            raise HTTPException(status_code=404, detail="state_bus_consumer_not_found")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="state_bus_consumer_acked",
        target_type="workspace_state_bus_consumer",
        target_id=row.consumer_key,
        summary=f"Acknowledged {len(accepted)} events for {row.consumer_key}",
        metadata_json={
            "accepted_event_ids": accepted,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "consumer": to_state_bus_consumer_out(row),
        "accepted_event_ids": accepted,
    }


@router.post("/state-bus/consumers/{consumer_key}/replay")
async def replay_state_bus_consumer_endpoint(
    consumer_key: str,
    payload: StateBusConsumerReplayRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        row = await replay_state_bus_consumer(
            consumer_key=consumer_key,
            from_event_id=payload.from_event_id,
            from_snapshot_scope=payload.from_snapshot_scope,
            db=db,
        )
    except ValueError as exc:
        code = str(exc)
        if code == "consumer_not_found":
            raise HTTPException(status_code=404, detail="state_bus_consumer_not_found")
        if code == "snapshot_not_found":
            raise HTTPException(status_code=404, detail="state_bus_snapshot_not_found")
        raise

    await write_journal(
        db,
        actor=payload.actor,
        action="state_bus_consumer_replayed",
        target_type="workspace_state_bus_consumer",
        target_id=row.consumer_key,
        summary=f"Replayed consumer {row.consumer_key}",
        metadata_json={
            "from_event_id": payload.from_event_id,
            "from_snapshot_scope": payload.from_snapshot_scope,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "consumer": to_state_bus_consumer_out(row),
    }


@router.post("/state-bus/consumers/mim-core/step")
async def run_mim_core_consumer_step_endpoint(
    payload: StateBusMimCoreStepRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await run_mim_core_consumer_step(
        actor=payload.actor,
        limit=payload.limit,
        metadata_json=payload.metadata_json,
        db=db,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="state_bus_mim_core_step",
        target_type="workspace_state_bus_consumer",
        target_id="mim-core",
        summary=f"Ran mim-core state bus step consumed={result.get('consumed_count', 0)}",
        metadata_json={
            "consumed_count": result.get("consumed_count", 0),
            "memory_written": result.get("memory_written", 0),
            **payload.metadata_json,
        },
    )
    await db.commit()
    return result
