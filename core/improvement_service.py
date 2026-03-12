from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.concept_memory_service import concept_ids_for_component
from core.development_memory_service import development_pattern_ids_for_component
from core.models import (
    ConstraintEvaluation,
    Task,
    WorkspaceDecisionRecord,
    WorkspaceEnvironmentStrategy,
    WorkspaceHorizonReplanEvent,
    WorkspaceImprovementArtifact,
    WorkspaceImprovementProposal,
)


PROPOSAL_STATUSES = {
    "proposed",
    "accepted",
    "rejected",
    "superseded",
}


def _candidate_confidence(count: int, *, base: float = 0.45) -> float:
    return max(0.0, min(0.95, base + (0.08 * float(max(0, count)))))


def _risk_for_type(proposal_type: str) -> str:
    low = {
        "operator_preference_suggestion",
        "routine_strategy_refinement",
    }
    medium = {
        "policy_adjustment",
        "priority_rule_refinement",
        "soft_constraint_weight_adjustment",
        "capability_workflow_improvement",
    }
    if proposal_type in low:
        return "low_risk_non_runtime_mutation"
    if proposal_type in medium:
        return "medium_risk_requires_review_and_test"
    return "medium_risk_requires_review"


def _test_for_type(proposal_type: str, affected_component: str) -> str:
    return (
        f"Create focused integration test for {proposal_type} affecting {affected_component}, "
        "then run backward regression before applying any runtime policy changes"
    )


async def _soft_constraint_friction_candidates(*, since: datetime, min_occurrence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(ConstraintEvaluation)
            .where(ConstraintEvaluation.created_at >= since)
            .where(ConstraintEvaluation.outcome_result == "succeeded")
            .where(ConstraintEvaluation.outcome_quality >= 0.7)
            .order_by(ConstraintEvaluation.id.desc())
            .limit(2000)
        )
    ).scalars().all()

    by_constraint: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        warnings = row.warnings_json if isinstance(row.warnings_json, list) else []
        for item in warnings:
            if not isinstance(item, dict):
                continue
            if bool(item.get("hard", False)):
                continue
            key = str(item.get("constraint", "soft_constraint")).strip() or "soft_constraint"
            by_constraint[key].append(int(row.id))

    candidates: list[dict] = []
    for key, evaluation_ids in by_constraint.items():
        count = len(evaluation_ids)
        if count < min_occurrence_count:
            continue
        candidates.append(
            {
                "proposal_type": "soft_constraint_weight_adjustment",
                "trigger_pattern": "successful_soft_constraint_exceptions",
                "affected_component": f"constraint:{key}",
                "suggested_change": (
                    f"Review soft constraint '{key}' threshold/weight because it repeatedly signaled friction "
                    "while outcomes still succeeded safely"
                ),
                "evidence_summary": (
                    f"{count} successful outcomes were recorded with soft warning '{key}' during the lookback window"
                ),
                "evidence_json": {
                    "constraint_key": key,
                    "sample_evaluation_ids": evaluation_ids[:10],
                    "count": count,
                },
                "confidence": _candidate_confidence(count),
            }
        )
    return candidates


async def _manual_override_candidates(*, since: datetime, min_occurrence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceDecisionRecord)
            .where(WorkspaceDecisionRecord.created_at >= since)
            .where(WorkspaceDecisionRecord.decision_type == "strategy_selection")
            .order_by(WorkspaceDecisionRecord.id.desc())
            .limit(3000)
        )
    ).scalars().all()

    override_rows: list[WorkspaceDecisionRecord] = []
    for row in rows:
        source_context = row.source_context_json if isinstance(row.source_context_json, dict) else {}
        endpoint = str(source_context.get("endpoint", "")).strip()
        if endpoint.endswith("/resolve") or endpoint.endswith("/deactivate"):
            override_rows.append(row)

    count = len(override_rows)
    if count < min_occurrence_count:
        return []

    return [
        {
            "proposal_type": "operator_preference_suggestion",
            "trigger_pattern": "repeated_manual_overrides",
            "affected_component": "environment_strategy_lifecycle",
            "suggested_change": "Propose preference or policy refinement to reduce repeated manual strategy overrides",
            "evidence_summary": f"Detected {count} manual strategy resolve/deactivate decisions in lookback window",
            "evidence_json": {
                "count": count,
                "sample_decision_ids": [int(item.id) for item in override_rows[:10]],
            },
            "confidence": _candidate_confidence(count, base=0.5),
        }
    ]


async def _replan_threshold_candidates(*, since: datetime, min_occurrence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceHorizonReplanEvent)
            .where(WorkspaceHorizonReplanEvent.created_at >= since)
            .order_by(WorkspaceHorizonReplanEvent.id.desc())
            .limit(2000)
        )
    ).scalars().all()

    by_drift: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        key = str(row.drift_type or "unknown").strip() or "unknown"
        by_drift[key].append(int(row.id))

    candidates: list[dict] = []
    for key, ids in by_drift.items():
        count = len(ids)
        if count < min_occurrence_count:
            continue
        candidates.append(
            {
                "proposal_type": "priority_rule_refinement",
                "trigger_pattern": "repeated_replans",
                "affected_component": f"horizon_planning:{key}",
                "suggested_change": f"Refine replan thresholds/priority weighting for drift type '{key}'",
                "evidence_summary": f"Observed {count} replan events for drift type '{key}'",
                "evidence_json": {
                    "drift_type": key,
                    "count": count,
                    "sample_replan_event_ids": ids[:10],
                },
                "confidence": _candidate_confidence(count),
            }
        )
    return candidates


async def _retry_friction_candidates(*, since: datetime, min_occurrence_count: int, db: AsyncSession) -> list[dict]:
    result = await db.execute(
        text(
            """
            SELECT id, action_type
            FROM actions
            WHERE retry_count > 0
              AND started_at >= :since
            ORDER BY id DESC
            LIMIT 3000
            """
        ),
        {"since": since},
    )

    by_action_type: dict[str, list[int]] = defaultdict(list)
    for row in result.fetchall():
        row_id = int(getattr(row, "id", 0) or 0)
        action_type = str(getattr(row, "action_type", "execute") or "execute").strip() or "execute"
        if row_id <= 0:
            continue
        by_action_type[action_type].append(row_id)

    candidates: list[dict] = []
    for key, ids in by_action_type.items():
        count = len(ids)
        if count < min_occurrence_count:
            continue
        candidates.append(
            {
                "proposal_type": "capability_workflow_improvement",
                "trigger_pattern": "repeated_retries",
                "affected_component": f"action:{key}",
                "suggested_change": f"Improve workflow for action type '{key}' to reduce retry churn",
                "evidence_summary": f"Detected {count} retried actions for '{key}'",
                "evidence_json": {
                    "action_type": key,
                    "count": count,
                    "sample_action_ids": ids[:10],
                },
                "confidence": _candidate_confidence(count),
            }
        )
    return candidates


async def _strategy_starvation_candidates(*, since: datetime, min_occurrence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(WorkspaceEnvironmentStrategy)
            .where(WorkspaceEnvironmentStrategy.created_at >= since)
            .where(WorkspaceEnvironmentStrategy.current_status.in_(["active", "stable"]))
            .order_by(WorkspaceEnvironmentStrategy.id.desc())
            .limit(3000)
        )
    ).scalars().all()

    by_strategy: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        influenced = row.influenced_plan_ids_json if isinstance(row.influenced_plan_ids_json, list) else []
        if influenced:
            continue
        key = str(row.strategy_type or "strategy").strip() or "strategy"
        by_strategy[key].append(int(row.id))

    candidates: list[dict] = []
    for key, ids in by_strategy.items():
        count = len(ids)
        if count < min_occurrence_count:
            continue
        candidates.append(
            {
                "proposal_type": "routine_strategy_refinement",
                "trigger_pattern": "strategy_starvation",
                "affected_component": f"environment_strategy:{key}",
                "suggested_change": f"Refine ranking/routine mapping so strategy type '{key}' is not persistently starved",
                "evidence_summary": f"Found {count} active/stable '{key}' strategies that never influenced a plan",
                "evidence_json": {
                    "strategy_type": key,
                    "count": count,
                    "sample_strategy_ids": ids[:10],
                },
                "confidence": _candidate_confidence(count),
            }
        )
    return candidates


async def _throttle_friction_candidates(*, since: datetime, min_occurrence_count: int, db: AsyncSession) -> list[dict]:
    rows = (
        await db.execute(
            select(ConstraintEvaluation)
            .where(ConstraintEvaluation.created_at >= since)
            .order_by(ConstraintEvaluation.id.desc())
            .limit(2000)
        )
    ).scalars().all()

    matching_ids: list[int] = []
    for row in rows:
        warnings = row.warnings_json if isinstance(row.warnings_json, list) else []
        if any(isinstance(item, dict) and str(item.get("constraint", "")) == "execution_throttle" for item in warnings):
            matching_ids.append(int(row.id))

    count = len(matching_ids)
    if count < min_occurrence_count:
        return []
    return [
        {
            "proposal_type": "policy_adjustment",
            "trigger_pattern": "throttle_cooldown_friction",
            "affected_component": "workspace_autonomy_policy",
            "suggested_change": "Review cooldown/throttle policy parameters to reduce recurring execution friction",
            "evidence_summary": f"Execution throttle warning observed {count} times in lookback window",
            "evidence_json": {
                "count": count,
                "sample_evaluation_ids": matching_ids[:10],
            },
            "confidence": _candidate_confidence(count),
        }
    ]


async def _existing_duplicate(
    *,
    proposal_type: str,
    trigger_pattern: str,
    affected_component: str,
    db: AsyncSession,
) -> WorkspaceImprovementProposal | None:
    return (
        await db.execute(
            select(WorkspaceImprovementProposal)
            .where(WorkspaceImprovementProposal.proposal_type == proposal_type)
            .where(WorkspaceImprovementProposal.trigger_pattern == trigger_pattern)
            .where(WorkspaceImprovementProposal.affected_component == affected_component)
            .where(WorkspaceImprovementProposal.status == "proposed")
            .order_by(WorkspaceImprovementProposal.id.desc())
        )
    ).scalars().first()


async def generate_improvement_proposals(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    min_occurrence_count: int,
    max_proposals: int,
    metadata_json: dict,
    db: AsyncSession,
) -> list[WorkspaceImprovementProposal]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    min_count = max(2, int(min_occurrence_count))

    candidates: list[dict] = []
    candidates.extend(await _soft_constraint_friction_candidates(since=since, min_occurrence_count=min_count, db=db))
    candidates.extend(await _manual_override_candidates(since=since, min_occurrence_count=min_count, db=db))
    candidates.extend(await _replan_threshold_candidates(since=since, min_occurrence_count=min_count, db=db))
    candidates.extend(await _retry_friction_candidates(since=since, min_occurrence_count=min_count, db=db))
    candidates.extend(await _strategy_starvation_candidates(since=since, min_occurrence_count=min_count, db=db))
    candidates.extend(await _throttle_friction_candidates(since=since, min_occurrence_count=min_count, db=db))

    created: list[WorkspaceImprovementProposal] = []
    for item in sorted(candidates, key=lambda entry: float(entry.get("confidence", 0.0)), reverse=True):
        if len(created) >= max(1, min(50, int(max_proposals))):
            break

        proposal_type = str(item.get("proposal_type", "policy_adjustment")).strip() or "policy_adjustment"
        trigger_pattern = str(item.get("trigger_pattern", "repeated_friction")).strip() or "repeated_friction"
        affected_component = str(item.get("affected_component", "system")).strip() or "system"

        duplicate = await _existing_duplicate(
            proposal_type=proposal_type,
            trigger_pattern=trigger_pattern,
            affected_component=affected_component,
            db=db,
        )
        if duplicate:
            continue

        related_concept_ids = await concept_ids_for_component(
            affected_component=affected_component,
            db=db,
        )
        related_development_pattern_ids = await development_pattern_ids_for_component(
            affected_component=affected_component,
            db=db,
        )

        confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0) or 0.0)))
        if related_development_pattern_ids:
            confidence = max(0.0, min(1.0, confidence + 0.06))

        row = WorkspaceImprovementProposal(
            source=source,
            actor=actor,
            proposal_type=proposal_type,
            trigger_pattern=trigger_pattern,
            evidence_summary=str(item.get("evidence_summary", "")),
            evidence_json=item.get("evidence_json", {}) if isinstance(item.get("evidence_json", {}), dict) else {},
            affected_component=affected_component,
            suggested_change=str(item.get("suggested_change", "")),
            confidence=confidence,
            safety_class="bounded_review",
            risk_summary=_risk_for_type(proposal_type),
            test_recommendation=_test_for_type(proposal_type, affected_component),
            status="proposed",
            metadata_json={
                "generator": "objective49",
                "lookback_hours": lookback_hours,
                "min_occurrence_count": min_count,
                "related_concept_ids": related_concept_ids,
                "related_development_pattern_ids": related_development_pattern_ids,
                **(metadata_json if isinstance(metadata_json, dict) else {}),
            },
        )
        db.add(row)
        created.append(row)

    await db.flush()
    return created


async def list_improvement_proposals(
    *,
    db: AsyncSession,
    status: str = "",
    proposal_type: str = "",
    limit: int = 50,
) -> list[WorkspaceImprovementProposal]:
    rows = (
        await db.execute(
            select(WorkspaceImprovementProposal).order_by(WorkspaceImprovementProposal.id.desc())
        )
    ).scalars().all()
    filtered = rows
    if status:
        requested = status.strip().lower()
        filtered = [item for item in filtered if str(item.status).strip().lower() == requested]
    if proposal_type:
        requested_type = proposal_type.strip().lower()
        filtered = [item for item in filtered if str(item.proposal_type).strip().lower() == requested_type]
    return filtered[: max(1, min(500, int(limit)))]


async def get_improvement_proposal(*, proposal_id: int, db: AsyncSession) -> WorkspaceImprovementProposal | None:
    return (
        await db.execute(
            select(WorkspaceImprovementProposal).where(WorkspaceImprovementProposal.id == proposal_id)
        )
    ).scalars().first()


async def list_improvement_artifacts_for_proposal(*, proposal_id: int, db: AsyncSession) -> list[WorkspaceImprovementArtifact]:
    return (
        await db.execute(
            select(WorkspaceImprovementArtifact)
            .where(WorkspaceImprovementArtifact.proposal_id == proposal_id)
            .order_by(WorkspaceImprovementArtifact.id.desc())
        )
    ).scalars().all()


def _artifact_type_for_proposal(proposal_type: str) -> str:
    if proposal_type in {"policy_adjustment", "soft_constraint_weight_adjustment", "priority_rule_refinement"}:
        return "policy_change_candidate"
    if proposal_type in {"capability_workflow_improvement", "routine_strategy_refinement"}:
        return "test_candidate"
    return "gated_workflow_item"


async def accept_improvement_proposal(
    *,
    proposal: WorkspaceImprovementProposal,
    actor: str,
    reason: str,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceImprovementArtifact:
    if proposal.status != "proposed":
        raise ValueError("proposal_not_open")

    artifact_type = _artifact_type_for_proposal(str(proposal.proposal_type or ""))
    candidate_payload = {
        "proposal_id": proposal.id,
        "proposal_type": proposal.proposal_type,
        "trigger_pattern": proposal.trigger_pattern,
        "affected_component": proposal.affected_component,
        "suggested_change": proposal.suggested_change,
        "evidence_summary": proposal.evidence_summary,
        "test_recommendation": proposal.test_recommendation,
    }

    if artifact_type == "gated_workflow_item":
        task = Task(
            objective_id=None,
            title=f"Review improvement proposal #{proposal.id}",
            details=(
                f"Proposed change: {proposal.suggested_change}\n"
                f"Evidence: {proposal.evidence_summary}\n"
                f"Risk: {proposal.risk_summary}\n"
                f"Test first: {proposal.test_recommendation}"
            ),
            dependencies=[],
            acceptance_criteria="Review proposal and approve an explicit policy/test change before rollout",
            assigned_to="operator",
            state="queued",
        )
        db.add(task)
        await db.flush()
        candidate_payload["task_id"] = task.id

    artifact = WorkspaceImprovementArtifact(
        proposal_id=proposal.id,
        artifact_type=artifact_type,
        status="pending_review",
        candidate_payload_json=candidate_payload,
        metadata_json={
            "accepted_by": actor,
            "accept_reason": reason,
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        },
    )
    db.add(artifact)

    proposal.status = "accepted"
    proposal.review_reason = reason or proposal.review_reason
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "accepted_by": actor,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "artifact_type": artifact_type,
        **(metadata_json if isinstance(metadata_json, dict) else {}),
    }
    await db.flush()
    return artifact


async def reject_improvement_proposal(
    *,
    proposal: WorkspaceImprovementProposal,
    actor: str,
    reason: str,
    metadata_json: dict,
) -> WorkspaceImprovementProposal:
    if proposal.status != "proposed":
        raise ValueError("proposal_not_open")
    proposal.status = "rejected"
    proposal.review_reason = reason or proposal.review_reason
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "rejected_by": actor,
        "rejected_at": datetime.now(timezone.utc).isoformat(),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
    }
    return proposal


def to_improvement_artifact_out(row: WorkspaceImprovementArtifact) -> dict:
    return {
        "artifact_id": row.id,
        "proposal_id": row.proposal_id,
        "artifact_type": row.artifact_type,
        "status": row.status,
        "candidate_payload": row.candidate_payload_json if isinstance(row.candidate_payload_json, dict) else {},
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


def to_improvement_proposal_out(
    row: WorkspaceImprovementProposal,
    *,
    latest_artifact: WorkspaceImprovementArtifact | None = None,
) -> dict:
    return {
        "proposal_id": row.id,
        "source": row.source,
        "actor": row.actor,
        "proposal_type": row.proposal_type,
        "trigger_pattern": row.trigger_pattern,
        "evidence_summary": row.evidence_summary,
        "evidence": row.evidence_json if isinstance(row.evidence_json, dict) else {},
        "affected_component": row.affected_component,
        "suggested_change": row.suggested_change,
        "confidence": float(row.confidence),
        "safety_class": row.safety_class,
        "risk_summary": row.risk_summary,
        "test_recommendation": row.test_recommendation,
        "status": row.status,
        "review_reason": row.review_reason,
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
        "latest_artifact": to_improvement_artifact_out(latest_artifact) if latest_artifact else None,
    }
