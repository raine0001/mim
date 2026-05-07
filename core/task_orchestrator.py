from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import CapabilityExecution, ExecutionTaskOrchestration


def _orchestration_step_for_execution(execution: CapabilityExecution) -> tuple[str, str]:
    if execution.status == "blocked":
        return "blocked", "blocked"
    if execution.status in {"pending", "pending_confirmation"}:
        return "operator_review", "awaiting_review"
    if execution.status in {"dispatched", "accepted", "running"}:
        return "executor_dispatch", "in_progress"
    if execution.status == "succeeded":
        return "completed", "completed"
    if execution.status == "failed":
        return "recovery", "degraded"
    return "created", "active"


async def ensure_execution_orchestration(
    *,
    db: AsyncSession,
    trace_id: str,
    intent_id: int | None,
    execution: CapabilityExecution,
    managed_scope: str,
    actor: str,
    source: str,
    metadata_json: dict | None = None,
) -> ExecutionTaskOrchestration:
    row = (
        (
            await db.execute(
                select(ExecutionTaskOrchestration)
                .where(ExecutionTaskOrchestration.trace_id == str(trace_id or "").strip())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    current_step_key, orchestration_status = _orchestration_step_for_execution(execution)
    step_state = [
        {
            "step_key": current_step_key,
            "execution_id": execution.id,
            "status": execution.status,
            "dispatch_decision": execution.dispatch_decision,
            "reason": execution.reason,
        }
    ]
    checkpoint = {
        "latest_execution_id": execution.id,
        "latest_status": execution.status,
        "latest_dispatch_decision": execution.dispatch_decision,
    }
    if row is None:
        row = ExecutionTaskOrchestration(
            trace_id=str(trace_id or "").strip(),
            intent_id=intent_id,
            execution_id=execution.id,
            source=source,
            actor=actor,
            managed_scope=str(managed_scope or "").strip() or "global",
            orchestration_status=orchestration_status,
            current_step_key=current_step_key,
            step_state_json=step_state,
            checkpoint_json=checkpoint,
            retry_count=int(
                (execution.feedback_json if isinstance(execution.feedback_json, dict) else {}).get(
                    "retry_count", 0
                )
                or 0
            ),
            rollback_state_json={},
            metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
        )
        db.add(row)
        await db.flush()
        return row

    row.intent_id = intent_id or row.intent_id
    row.execution_id = execution.id
    row.source = source
    row.actor = actor
    row.managed_scope = str(managed_scope or row.managed_scope or "global").strip() or "global"
    row.orchestration_status = orchestration_status
    row.current_step_key = current_step_key
    row.step_state_json = step_state
    row.checkpoint_json = checkpoint
    row.retry_count = int(
        (execution.feedback_json if isinstance(execution.feedback_json, dict) else {}).get(
            "retry_count", row.retry_count
        )
        or row.retry_count
        or 0
    )
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
    }
    await db.flush()
    return row


def to_execution_task_orchestration_out(row: ExecutionTaskOrchestration) -> dict:
    return {
        "orchestration_id": int(row.id),
        "trace_id": row.trace_id,
        "intent_id": row.intent_id,
        "execution_id": row.execution_id,
        "managed_scope": row.managed_scope,
        "orchestration_status": row.orchestration_status,
        "current_step_key": row.current_step_key,
        "step_state_json": row.step_state_json if isinstance(row.step_state_json, list) else [],
        "checkpoint_json": row.checkpoint_json if isinstance(row.checkpoint_json, dict) else {},
        "retry_count": int(row.retry_count or 0),
        "rollback_state_json": row.rollback_state_json if isinstance(row.rollback_state_json, dict) else {},
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }