from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import WorkspaceDecisionRecord


async def record_decision(
    *,
    decision_type: str,
    source_context: dict,
    relevant_state: dict,
    preferences_applied: dict,
    constraints_applied: list[dict],
    strategies_applied: list[dict],
    options_considered: list[dict],
    selected_option: dict,
    decision_reason: str,
    confidence: float,
    resulting_goal_or_plan_id: str,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceDecisionRecord:
    row = WorkspaceDecisionRecord(
        decision_type=str(decision_type or "unknown").strip() or "unknown",
        source_context_json=source_context if isinstance(source_context, dict) else {},
        relevant_state_json=relevant_state if isinstance(relevant_state, dict) else {},
        preferences_applied_json=preferences_applied if isinstance(preferences_applied, dict) else {},
        constraints_applied_json=constraints_applied if isinstance(constraints_applied, list) else [],
        strategies_applied_json=strategies_applied if isinstance(strategies_applied, list) else [],
        options_considered_json=options_considered if isinstance(options_considered, list) else [],
        selected_option_json=selected_option if isinstance(selected_option, dict) else {},
        decision_reason=str(decision_reason or ""),
        confidence=max(0.0, min(1.0, float(confidence or 0.0))),
        resulting_goal_or_plan_id=str(resulting_goal_or_plan_id or ""),
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    db.add(row)
    await db.flush()
    return row


async def list_decision_records(
    *,
    db: AsyncSession,
    decision_type: str = "",
    limit: int = 50,
) -> list[WorkspaceDecisionRecord]:
    rows = (
        await db.execute(select(WorkspaceDecisionRecord).order_by(WorkspaceDecisionRecord.id.desc()))
    ).scalars().all()
    if decision_type:
        requested = decision_type.strip().lower()
        rows = [item for item in rows if str(item.decision_type).strip().lower() == requested]
    return rows[: max(1, min(limit, 500))]


async def get_decision_record(*, decision_id: int, db: AsyncSession) -> WorkspaceDecisionRecord | None:
    return (
        await db.execute(
            select(WorkspaceDecisionRecord).where(WorkspaceDecisionRecord.id == decision_id)
        )
    ).scalars().first()


def to_decision_record_out(row: WorkspaceDecisionRecord) -> dict:
    return {
        "decision_id": row.id,
        "decision_type": row.decision_type,
        "source_context": row.source_context_json if isinstance(row.source_context_json, dict) else {},
        "relevant_state": row.relevant_state_json if isinstance(row.relevant_state_json, dict) else {},
        "preferences_applied": row.preferences_applied_json if isinstance(row.preferences_applied_json, dict) else {},
        "constraints_applied": row.constraints_applied_json if isinstance(row.constraints_applied_json, list) else [],
        "strategies_applied": row.strategies_applied_json if isinstance(row.strategies_applied_json, list) else [],
        "options_considered": row.options_considered_json if isinstance(row.options_considered_json, list) else [],
        "selected_option": row.selected_option_json if isinstance(row.selected_option_json, dict) else {},
        "decision_reason": row.decision_reason,
        "confidence": float(row.confidence),
        "resulting_goal_or_plan_id": row.resulting_goal_or_plan_id,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
