from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import CapabilityExecution, ExecutionOverride, ExecutionStabilityProfile


async def evaluate_execution_stability(
    *,
    db: AsyncSession,
    managed_scope: str,
    actor: str,
    source: str,
    trace_id: str = "",
    metadata_json: dict | None = None,
) -> ExecutionStabilityProfile:
    scope = str(managed_scope or "").strip() or "global"
    recent_executions = list(
        (
            await db.execute(
                select(CapabilityExecution)
                .where(CapabilityExecution.managed_scope == scope)
                .order_by(CapabilityExecution.id.desc())
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    override_rows = list(
        (
            await db.execute(
                select(ExecutionOverride)
                .where(ExecutionOverride.managed_scope == scope)
                .where(ExecutionOverride.status == "active")
                .order_by(ExecutionOverride.id.desc())
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    total = max(1, len(recent_executions))
    blocked = sum(1 for row in recent_executions if row.status == "blocked")
    failed = sum(1 for row in recent_executions if row.status == "failed")
    pending_review = sum(
        1
        for row in recent_executions
        if row.status in {"pending", "pending_confirmation"}
        or row.dispatch_decision in {"requires_confirmation", "queued_for_executor"}
    )
    status_sequence = [row.status for row in recent_executions]
    oscillations = sum(
        1
        for current, previous in zip(status_sequence, status_sequence[1:])
        if current != previous
    )
    drift_score = round(min(1.0, (blocked + failed) / float(total)), 6)
    oscillation_score = round(min(1.0, oscillations / float(max(1, total - 1))), 6)
    degradation_score = round(min(1.0, pending_review / float(total)), 6)
    mitigation_state = "monitor_only"
    status = "stable"
    if any(row.override_type == "hard_stop" for row in override_rows):
        mitigation_state = "hard_stop_active"
        status = "constrained"
    elif drift_score >= 0.35 or oscillation_score >= 0.5:
        mitigation_state = "review_required"
        status = "degraded"
    elif degradation_score >= 0.4:
        mitigation_state = "stabilizing"
        status = "degraded"

    metrics = {
        "execution_count": len(recent_executions),
        "blocked_count": blocked,
        "failed_count": failed,
        "pending_review_count": pending_review,
        "active_override_count": len(override_rows),
    }
    triggers = []
    if blocked:
        triggers.append({"trigger": "blocked_execution_detected", "count": blocked})
    if failed:
        triggers.append({"trigger": "failed_execution_detected", "count": failed})
    if oscillations:
        triggers.append({"trigger": "status_oscillation_detected", "count": oscillations})
    if override_rows:
        triggers.append(
            {
                "trigger": "operator_override_active",
                "count": len(override_rows),
                "override_types": [row.override_type for row in override_rows],
            }
        )

    row = ExecutionStabilityProfile(
        trace_id=str(trace_id or "").strip(),
        source=source,
        actor=actor,
        managed_scope=scope,
        status=status,
        mitigation_state=mitigation_state,
        drift_score=drift_score,
        oscillation_score=oscillation_score,
        degradation_score=degradation_score,
        metrics_json=metrics,
        triggers_json=triggers,
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    db.add(row)
    await db.flush()
    return row


def to_execution_stability_out(row: ExecutionStabilityProfile) -> dict:
    return {
        "stability_id": int(row.id),
        "trace_id": row.trace_id,
        "managed_scope": row.managed_scope,
        "status": row.status,
        "mitigation_state": row.mitigation_state,
        "drift_score": float(row.drift_score or 0.0),
        "oscillation_score": float(row.oscillation_score or 0.0),
        "degradation_score": float(row.degradation_score or 0.0),
        "metrics_json": row.metrics_json if isinstance(row.metrics_json, dict) else {},
        "triggers_json": row.triggers_json if isinstance(row.triggers_json, list) else [],
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }