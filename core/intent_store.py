from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ExecutionIntent


async def ensure_execution_intent(
    *,
    db: AsyncSession,
    trace_id: str,
    managed_scope: str,
    intent_key: str,
    intent_type: str,
    requested_goal: str,
    capability_name: str,
    arguments_json: dict | None,
    context_json: dict | None,
    actor: str,
    source: str,
    execution_id: int | None,
    lifecycle_status: str = "active",
) -> ExecutionIntent:
    normalized_key = str(intent_key or "").strip()
    row = None
    if normalized_key:
        row = (
            (
                await db.execute(
                    select(ExecutionIntent)
                    .where(ExecutionIntent.intent_key == normalized_key)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
    if row is None:
        row = ExecutionIntent(
            trace_id=str(trace_id or "").strip(),
            source=source,
            actor=actor,
            managed_scope=str(managed_scope or "").strip() or "global",
            intent_key=normalized_key,
            lifecycle_status=lifecycle_status,
            intent_type=str(intent_type or "execution_request").strip(),
            requested_goal=str(requested_goal or "").strip(),
            capability_name=str(capability_name or "").strip(),
            arguments_json=arguments_json if isinstance(arguments_json, dict) else {},
            context_json=context_json if isinstance(context_json, dict) else {},
            last_execution_id=execution_id,
        )
        db.add(row)
        await db.flush()
        return row

    row.trace_id = str(trace_id or row.trace_id or "").strip()
    row.source = source
    row.actor = actor
    row.managed_scope = str(managed_scope or row.managed_scope or "global").strip() or "global"
    row.lifecycle_status = lifecycle_status
    row.intent_type = str(intent_type or row.intent_type or "execution_request").strip()
    row.requested_goal = str(requested_goal or row.requested_goal or "").strip()
    row.capability_name = str(capability_name or row.capability_name or "").strip()
    row.arguments_json = arguments_json if isinstance(arguments_json, dict) else {}
    row.context_json = {
        **(row.context_json if isinstance(row.context_json, dict) else {}),
        **(context_json if isinstance(context_json, dict) else {}),
    }
    row.last_execution_id = execution_id or row.last_execution_id
    if lifecycle_status == "archived" and row.archived_at is None:
        row.archived_at = datetime.now(timezone.utc)
    await db.flush()
    return row


def to_execution_intent_out(row: ExecutionIntent) -> dict:
    return {
        "intent_id": int(row.id),
        "trace_id": row.trace_id,
        "managed_scope": row.managed_scope,
        "intent_key": row.intent_key,
        "lifecycle_status": row.lifecycle_status,
        "intent_type": row.intent_type,
        "requested_goal": row.requested_goal,
        "capability_name": row.capability_name,
        "arguments_json": row.arguments_json if isinstance(row.arguments_json, dict) else {},
        "context_json": row.context_json if isinstance(row.context_json, dict) else {},
        "last_execution_id": row.last_execution_id,
        "resumption_count": int(row.resumption_count or 0),
        "archived_at": row.archived_at,
        "created_at": row.created_at,
    }