from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.constraint_engine import evaluate_constraints
from core.models import ConstraintEvaluation


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
    evaluation = evaluate_constraints(
        goal=goal,
        action_plan=action_plan,
        workspace_state=workspace_state,
        system_state=system_state,
        policy_state=policy_state,
    )
    row = ConstraintEvaluation(
        source=source,
        actor=actor,
        goal_json=goal if isinstance(goal, dict) else {},
        action_plan_json=action_plan if isinstance(action_plan, dict) else {},
        workspace_state_json=workspace_state if isinstance(workspace_state, dict) else {},
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
        "explanation": row.explanation_json if isinstance(row.explanation_json, dict) else {},
        "created_at": row.created_at,
    }


async def get_last_constraint_evaluation(db: AsyncSession) -> ConstraintEvaluation | None:
    return (await db.execute(select(ConstraintEvaluation).order_by(ConstraintEvaluation.id.desc()))).scalars().first()


async def list_constraint_evaluations(*, db: AsyncSession, limit: int = 50) -> list[ConstraintEvaluation]:
    rows = (await db.execute(select(ConstraintEvaluation).order_by(ConstraintEvaluation.id.desc()))).scalars().all()
    return rows[: max(1, min(limit, 500))]
