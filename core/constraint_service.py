from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.constraint_engine import evaluate_constraints
from core.execution_truth_service import summarize_execution_truth
from core.models import CapabilityExecution, ConstraintEvaluation


def _constraint_scope_ref(*, workspace_state: dict, action_plan: dict, metadata_json: dict) -> str:
    for payload in (workspace_state, action_plan, metadata_json):
        if not isinstance(payload, dict):
            continue
        for key in ("managed_scope", "target_scope", "scope", "zone", "scan_area"):
            value = str(payload.get(key, "")).strip()
            if value:
                return value
    return "global"


async def evaluate_and_record_constraints(
    *,
    actor: str,
    source: str,
    goal: dict,
    action_plan: dict,
    workspace_state: dict,
    system_state: dict,
    policy_state: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[ConstraintEvaluation, dict]:
    scope_ref = _constraint_scope_ref(
        workspace_state=workspace_state if isinstance(workspace_state, dict) else {},
        action_plan=action_plan if isinstance(action_plan, dict) else {},
        metadata_json=metadata_json if isinstance(metadata_json, dict) else {},
    )
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    execution_rows = (
        await db.execute(
            select(CapabilityExecution)
            .where(CapabilityExecution.created_at >= since)
            .order_by(CapabilityExecution.id.desc())
            .limit(100)
        )
    ).scalars().all()
    execution_truth_summary = summarize_execution_truth(
        execution_rows,
        managed_scope=scope_ref,
        max_age_hours=24,
    )
    effective_workspace_state = {
        **(workspace_state if isinstance(workspace_state, dict) else {}),
        "execution_truth_summary": execution_truth_summary,
        "managed_scope": scope_ref,
    }

    evaluation = evaluate_constraints(
        goal=goal,
        action_plan=action_plan,
        workspace_state=effective_workspace_state,
        system_state=system_state,
        policy_state=policy_state,
    )
    row = ConstraintEvaluation(
        source=source,
        actor=actor,
        goal_json=goal if isinstance(goal, dict) else {},
        action_plan_json=action_plan if isinstance(action_plan, dict) else {},
        workspace_state_json=effective_workspace_state,
        system_state_json=system_state if isinstance(system_state, dict) else {},
        policy_state_json=policy_state if isinstance(policy_state, dict) else {},
        decision=str(evaluation.get("decision", "allowed")),
        violations_json=evaluation.get("violations", []) if isinstance(evaluation.get("violations", []), list) else [],
        warnings_json=evaluation.get("warnings", []) if isinstance(evaluation.get("warnings", []), list) else [],
        recommended_next_step=str(evaluation.get("recommended_next_step", "execute")),
        confidence=float(evaluation.get("confidence", 0.0) or 0.0),
        explanation_json={
            **(evaluation.get("explanation", {}) if isinstance(evaluation.get("explanation", {}), dict) else {}),
            "metadata_json": metadata_json if isinstance(metadata_json, dict) else {},
        },
    )
    db.add(row)
    await db.flush()
    return row, evaluation


def to_constraint_evaluation_out(row: ConstraintEvaluation) -> dict:
    return {
        "evaluation_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "decision": row.decision,
        "violations": row.violations_json if isinstance(row.violations_json, list) else [],
        "warnings": row.warnings_json if isinstance(row.warnings_json, list) else [],
        "recommended_next_step": row.recommended_next_step,
        "confidence": float(row.confidence),
        "goal": row.goal_json if isinstance(row.goal_json, dict) else {},
        "action_plan": row.action_plan_json if isinstance(row.action_plan_json, dict) else {},
        "workspace_state": row.workspace_state_json if isinstance(row.workspace_state_json, dict) else {},
        "system_state": row.system_state_json if isinstance(row.system_state_json, dict) else {},
        "policy_state": row.policy_state_json if isinstance(row.policy_state_json, dict) else {},
        "outcome_result": row.outcome_result,
        "outcome_quality": float(row.outcome_quality),
        "outcome_recorded_at": row.outcome_recorded_at,
        "explanation": row.explanation_json if isinstance(row.explanation_json, dict) else {},
        "created_at": row.created_at,
    }


async def get_last_constraint_evaluation(db: AsyncSession) -> ConstraintEvaluation | None:
    return (await db.execute(select(ConstraintEvaluation).order_by(ConstraintEvaluation.id.desc()))).scalars().first()


async def list_constraint_evaluations(*, db: AsyncSession, limit: int = 50) -> list[ConstraintEvaluation]:
    rows = (await db.execute(select(ConstraintEvaluation).order_by(ConstraintEvaluation.id.desc()))).scalars().all()
    return rows[: max(1, min(limit, 500))]
