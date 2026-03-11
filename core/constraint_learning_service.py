from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.constraint_learning import aggregate_constraint_outcomes, build_adjustment_proposals
from core.models import ConstraintAdjustmentProposal, ConstraintEvaluation


async def record_constraint_outcome(
    *,
    evaluation_id: int,
    result: str,
    outcome_quality: float,
    db: AsyncSession,
) -> ConstraintEvaluation | None:
    row = (
        await db.execute(select(ConstraintEvaluation).where(ConstraintEvaluation.id == evaluation_id))
    ).scalars().first()
    if not row:
        return None

    row.outcome_result = (result or "unknown").strip().lower()
    row.outcome_quality = float(max(0.0, min(1.0, outcome_quality)))
    row.outcome_recorded_at = datetime.now(timezone.utc)
    await db.flush()
    return row


async def compute_constraint_learning_stats(
    *,
    db: AsyncSession,
    constraint_key: str = "",
    limit: int = 200,
) -> list[dict]:
    rows = (
        await db.execute(
            select(ConstraintEvaluation).order_by(ConstraintEvaluation.id.desc())
        )
    ).scalars().all()

    sliced = rows[: max(1, min(limit, 1000))]
    packed = [
        {
            "id": item.id,
            "decision": item.decision,
            "warnings_json": item.warnings_json if isinstance(item.warnings_json, list) else [],
            "violations_json": item.violations_json if isinstance(item.violations_json, list) else [],
            "policy_state_json": item.policy_state_json if isinstance(item.policy_state_json, dict) else {},
            "workspace_state_json": item.workspace_state_json if isinstance(item.workspace_state_json, dict) else {},
            "outcome_result": item.outcome_result,
            "outcome_quality": float(item.outcome_quality),
        }
        for item in sliced
    ]

    stats_rows = aggregate_constraint_outcomes(packed, max_constraints=500)
    if constraint_key:
        requested = constraint_key.strip()
        stats_rows = [item for item in stats_rows if item.get("constraint_key") == requested]
    return stats_rows


async def generate_constraint_adjustment_proposals(
    *,
    actor: str,
    source: str,
    min_samples: int,
    success_rate_threshold: float,
    max_proposals: int,
    metadata_json: dict,
    db: AsyncSession,
) -> list[ConstraintAdjustmentProposal]:
    stats_rows = await compute_constraint_learning_stats(db=db, limit=1000)
    candidate_proposals = build_adjustment_proposals(
        stats_rows,
        min_samples=max(1, min(min_samples, 1000)),
        success_rate_threshold=max(0.0, min(success_rate_threshold, 1.0)),
        max_proposals=max(1, min(max_proposals, 50)),
    )

    created: list[ConstraintAdjustmentProposal] = []
    for candidate in candidate_proposals:
        constraint_key = str(candidate.get("constraint_key", "")).strip()
        proposed_value = candidate.get("proposed_value")

        existing = (
            await db.execute(
                select(ConstraintAdjustmentProposal)
                .where(ConstraintAdjustmentProposal.constraint_key == constraint_key)
                .where(ConstraintAdjustmentProposal.status == "proposed")
                .order_by(ConstraintAdjustmentProposal.id.desc())
            )
        ).scalars().first()
        if existing and existing.proposed_value == (None if proposed_value is None else str(proposed_value)):
            continue

        row = ConstraintAdjustmentProposal(
            source=source,
            actor=actor,
            constraint_key=constraint_key,
            proposal_type=str(candidate.get("proposal_type", "soft_weight_adjustment")),
            current_value=(None if candidate.get("current_value") is None else str(candidate.get("current_value"))),
            proposed_value=(None if proposed_value is None else str(proposed_value)),
            sample_size=int(candidate.get("sample_size", 0) or 0),
            success_rate=float(candidate.get("success_rate", 0.0) or 0.0),
            hard_constraint=bool(candidate.get("hard_constraint", False)),
            rationale=str(candidate.get("rationale", "")),
            status="proposed",
            metadata_json={
                **(candidate.get("metadata_json", {}) if isinstance(candidate.get("metadata_json", {}), dict) else {}),
                "generator": "objective45",
                "source": source,
                "actor": actor,
                **(metadata_json if isinstance(metadata_json, dict) else {}),
            },
        )
        db.add(row)
        created.append(row)

    await db.flush()
    return created


async def list_constraint_adjustment_proposals(
    *,
    db: AsyncSession,
    status: str = "",
    limit: int = 50,
) -> list[ConstraintAdjustmentProposal]:
    rows = (await db.execute(select(ConstraintAdjustmentProposal).order_by(ConstraintAdjustmentProposal.id.desc()))).scalars().all()
    filtered = rows
    if status:
        requested = status.strip().lower()
        filtered = [item for item in rows if str(item.status).strip().lower() == requested]
    return filtered[: max(1, min(limit, 500))]


def to_constraint_adjustment_proposal_out(row: ConstraintAdjustmentProposal) -> dict:
    return {
        "proposal_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "constraint_key": row.constraint_key,
        "proposal_type": row.proposal_type,
        "current_value": row.current_value,
        "proposed_value": row.proposed_value,
        "sample_size": row.sample_size,
        "success_rate": float(row.success_rate),
        "hard_constraint": bool(row.hard_constraint),
        "rationale": row.rationale,
        "status": row.status,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
