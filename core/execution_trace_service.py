from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ExecutionTrace, ExecutionTraceEvent


SCOPE_HINT_KEYS = (
    "managed_scope",
    "scope",
    "target_scope",
    "related_zone",
    "target_zone",
    "scan_area",
    "zone",
    "session_id",
    "run_id",
)


def normalize_managed_scope(raw: object) -> str:
    return str(raw or "").strip() or "global"


def infer_managed_scope(*candidates: object) -> str:
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in SCOPE_HINT_KEYS:
                value = str(candidate.get(key) or "").strip()
                if value:
                    return value
            metadata = candidate.get("metadata_json", {})
            if isinstance(metadata, dict) and metadata and metadata is not candidate:
                inferred = infer_managed_scope(metadata)
                if inferred != "global":
                    return inferred
        else:
            value = str(candidate or "").strip()
            if value:
                return value
    return "global"


def new_trace_id() -> str:
    return f"trace-{uuid4().hex}"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_execution_trace(*, trace_id: str, db: AsyncSession) -> ExecutionTrace | None:
    normalized = str(trace_id or "").strip()
    if not normalized:
        return None
    return (
        (
            await db.execute(
                select(ExecutionTrace).where(ExecutionTrace.trace_id == normalized).limit(1)
            )
        )
        .scalars()
        .first()
    )


async def ensure_execution_trace(
    *,
    db: AsyncSession,
    trace_id: str,
    managed_scope: str,
    capability_name: str,
    actor: str,
    source: str,
    root_execution_id: int | None,
    root_intent_id: int | None = None,
    lifecycle_status: str = "active",
    current_stage: str = "created",
    metadata_json: dict | None = None,
) -> ExecutionTrace:
    row = await get_execution_trace(trace_id=trace_id, db=db)
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    causality_graph = {
        "trace_id": trace_id,
        "managed_scope": normalize_managed_scope(managed_scope),
        "root_execution_id": root_execution_id,
        "root_intent_id": root_intent_id,
        "last_stage": current_stage,
        "updated_at": utcnow_iso(),
    }
    if row is None:
        row = ExecutionTrace(
            trace_id=trace_id,
            source=source,
            actor=actor,
            managed_scope=normalize_managed_scope(managed_scope),
            capability_name=str(capability_name or "").strip(),
            lifecycle_status=lifecycle_status,
            current_stage=current_stage,
            root_execution_id=root_execution_id,
            root_intent_id=root_intent_id,
            causality_graph_json=causality_graph,
            metadata_json=metadata,
        )
        db.add(row)
        await db.flush()
        return row

    row.source = source
    row.actor = actor
    row.managed_scope = normalize_managed_scope(managed_scope)
    row.capability_name = str(capability_name or row.capability_name or "").strip()
    row.lifecycle_status = lifecycle_status
    row.current_stage = current_stage
    row.root_execution_id = root_execution_id or row.root_execution_id
    row.root_intent_id = root_intent_id or row.root_intent_id
    row.causality_graph_json = {
        **(row.causality_graph_json if isinstance(row.causality_graph_json, dict) else {}),
        **causality_graph,
    }
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        **metadata,
    }
    await db.flush()
    return row


async def append_execution_trace_event(
    *,
    db: AsyncSession,
    trace_id: str,
    execution_id: int | None,
    intent_id: int | None,
    event_type: str,
    event_stage: str,
    causality_role: str,
    summary: str,
    payload_json: dict | None = None,
) -> ExecutionTraceEvent:
    row = ExecutionTraceEvent(
        trace_id=str(trace_id or "").strip(),
        execution_id=execution_id,
        intent_id=intent_id,
        event_type=str(event_type or "").strip(),
        event_stage=str(event_stage or "").strip(),
        causality_role=str(causality_role or "").strip() or "effect",
        summary=str(summary or "").strip(),
        payload_json=payload_json if isinstance(payload_json, dict) else {},
    )
    db.add(row)
    await db.flush()
    return row


async def list_execution_trace_events(
    *,
    trace_id: str,
    db: AsyncSession,
    limit: int = 100,
) -> list[ExecutionTraceEvent]:
    normalized = str(trace_id or "").strip()
    if not normalized:
        return []
    return list(
        (
            await db.execute(
                select(ExecutionTraceEvent)
                .where(ExecutionTraceEvent.trace_id == normalized)
                .order_by(ExecutionTraceEvent.id.asc())
                .limit(max(1, min(int(limit), 500)))
            )
        )
        .scalars()
        .all()
    )


def to_execution_trace_event_out(row: ExecutionTraceEvent) -> dict[str, Any]:
    return {
        "trace_event_id": int(row.id),
        "trace_id": row.trace_id,
        "execution_id": row.execution_id,
        "intent_id": row.intent_id,
        "event_type": row.event_type,
        "event_stage": row.event_stage,
        "causality_role": row.causality_role,
        "summary": row.summary,
        "payload_json": row.payload_json if isinstance(row.payload_json, dict) else {},
        "created_at": row.created_at,
    }


def to_execution_trace_out(
    row: ExecutionTrace,
    *,
    events: list[dict] | None = None,
    intent: dict | None = None,
    orchestration: dict | None = None,
    stability: dict | None = None,
) -> dict[str, Any]:
    return {
        "trace_id": row.trace_id,
        "managed_scope": row.managed_scope,
        "capability_name": row.capability_name,
        "lifecycle_status": row.lifecycle_status,
        "current_stage": row.current_stage,
        "root_execution_id": row.root_execution_id,
        "root_intent_id": row.root_intent_id,
        "causality_graph_json": row.causality_graph_json if isinstance(row.causality_graph_json, dict) else {},
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
        "events": events or [],
        "intent": intent,
        "orchestration": orchestration,
        "stability": stability,
    }