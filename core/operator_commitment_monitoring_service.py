from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import (
    WorkspaceExecutionTruthGovernanceProfile,
    WorkspaceInquiryQuestion,
    WorkspaceMaintenanceRun,
    WorkspaceOperatorResolutionCommitment,
    WorkspaceOperatorResolutionCommitmentMonitoringProfile,
    WorkspaceStewardshipCycle,
)
from core.operator_resolution_service import (
    commitment_downstream_effects,
    commitment_is_active,
    commitment_is_expired,
    commitment_snapshot,
    normalize_scope,
    scope_value,
    sync_commitment_expiration,
)
from core.proposal_arbitration_learning_service import workspace_proposal_arbitration_family_influence


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return int(default)


def _json_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _json_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _commitment_ref_matches(payload: dict, *, commitment_id: int) -> bool:
    commitment = payload.get("operator_resolution_commitment", {})
    if not isinstance(commitment, dict):
        return False
    return _safe_int(commitment.get("commitment_id", 0)) == int(commitment_id)


def _scope_matches(*, requested_scope: str, payload: dict) -> bool:
    scope = normalize_scope(requested_scope)
    if scope == "global":
        return True
    for key in ("managed_scope", "target_scope", "scope"):
        value = scope_value(payload.get(key))
        if value and value == scope:
            return True
    return False


def _stewardship_cycle_matches(
    row: WorkspaceStewardshipCycle,
    *,
    commitment_id: int,
    managed_scope: str,
) -> bool:
    metadata = _json_dict(row.metadata_json)
    verification = _json_dict(metadata.get("verification", {}))
    decision = _json_dict(row.decision_json)
    integration = _json_dict(row.integration_evidence_json)
    for bucket in (verification, decision, integration, metadata):
        if _commitment_ref_matches(bucket, commitment_id=commitment_id):
            return True
        if _scope_matches(requested_scope=managed_scope, payload=bucket):
            return True
    return False


def _maintenance_run_matches(
    row: WorkspaceMaintenanceRun,
    *,
    commitment_id: int,
    managed_scope: str,
) -> bool:
    metadata = _json_dict(row.metadata_json)
    outcomes = _json_dict(row.maintenance_outcomes_json)
    for bucket in (metadata, outcomes):
        if _commitment_ref_matches(bucket, commitment_id=commitment_id):
            return True
        if _scope_matches(requested_scope=managed_scope, payload=bucket):
            return True
    return False


def _inquiry_question_matches(
    row: WorkspaceInquiryQuestion,
    *,
    commitment_id: int,
    managed_scope: str,
) -> bool:
    trigger_evidence = _json_dict(row.trigger_evidence_json)
    metadata = _json_dict(row.metadata_json)
    if _safe_int(trigger_evidence.get("commitment_id", 0)) == int(commitment_id):
        return True
    if _safe_int(trigger_evidence.get("monitoring_commitment_id", 0)) == int(commitment_id):
        return True
    for bucket in (trigger_evidence, metadata):
        if _scope_matches(requested_scope=managed_scope, payload=bucket):
            return True
    return False


def _latest_governance_conflict(
    row: WorkspaceExecutionTruthGovernanceProfile | None,
    *,
    commitment: WorkspaceOperatorResolutionCommitment,
) -> float:
    if row is None:
        return 0.0
    decision = scope_value(row.governance_decision)
    commitment_decision = scope_value(commitment.decision_type)
    if not decision or not commitment_decision:
        return 0.0
    aligned_pairs = {
        ("require_additional_evidence", "increase_visibility"),
        ("require_additional_evidence", "escalate_to_operator"),
        ("defer_action", "escalate_to_operator"),
        ("lower_autonomy_for_scope", "lower_autonomy_boundary"),
        ("elevate_remediation_priority", "prioritize_improvement"),
    }
    if (commitment_decision, decision) in aligned_pairs:
        return 0.0
    if decision == "monitor_only":
        return 0.35
    return 0.6


def _recommended_actions(
    *,
    governance_state: str,
    governance_decision: str,
    commitment: WorkspaceOperatorResolutionCommitment,
) -> list[dict]:
    commitment_id = int(commitment.id)
    actions: list[dict] = []
    if governance_decision == "maintain_commitment":
        actions.append(
            {
                "action": "maintain_commitment",
                "label": "Keep commitment active",
                "effect_type": "no_action",
                "params": {"commitment_id": commitment_id},
            }
        )
        return actions
    if governance_state in {"expired", "inactive"}:
        actions.append(
            {
                "action": "close_monitoring",
                "label": "Commitment is no longer active",
                "effect_type": "no_action",
                "params": {"commitment_id": commitment_id},
            }
        )
        return actions
    actions.append(
        {
            "action": "maintain_commitment",
            "label": "Keep commitment active for now",
            "effect_type": "no_action",
            "params": {"commitment_id": commitment_id},
        }
    )
    actions.append(
        {
            "action": "revoke_commitment",
            "label": "Revoke commitment and allow governance to re-evaluate",
            "effect_type": "update_commitment_status",
            "params": {
                "commitment_id": commitment_id,
                "target_status": "revoked",
            },
        }
    )
    if governance_decision in {"operator_review_required", "replace_commitment"}:
        actions.append(
            {
                "action": "expire_commitment",
                "label": "Expire commitment now and request fresh operator guidance",
                "effect_type": "update_commitment_status",
                "params": {
                    "commitment_id": commitment_id,
                    "target_status": "expired",
                },
            }
        )
    return actions


def _commitment_related_proposal_types(*, commitment: WorkspaceOperatorResolutionCommitment) -> list[str]:
    decision_type = scope_value(commitment.decision_type)
    commitment_effects = commitment_downstream_effects(commitment)
    proposal_types: list[str] = []
    if decision_type in {"require_additional_evidence", "defer_action"}:
        proposal_types.extend(
            [
                "rescan_zone",
                "confirm_target_ready",
                "verify_moved_object",
                "monitor_recheck_workspace",
            ]
        )
    if decision_type == "lower_autonomy_for_scope":
        proposal_types.extend(["rescan_zone", "confirm_target_ready"])
    if scope_value(commitment_effects.get("maintenance_mode")) == "deferred":
        proposal_types.extend(["monitor_recheck_workspace", "monitor_search_adjacent_zone"])
    if bool(commitment_effects.get("stewardship_defer_actions", False)):
        proposal_types.extend(["rescan_zone", "verify_moved_object"])

    seen: set[str] = set()
    normalized: list[str] = []
    for item in proposal_types:
        proposal_type = str(item or "").strip()
        if not proposal_type or proposal_type in seen:
            continue
        seen.add(proposal_type)
        normalized.append(proposal_type)
    return normalized


async def _proposal_arbitration_commitment_expectation(
    *,
    commitment: WorkspaceOperatorResolutionCommitment,
    managed_scope: str,
    db: AsyncSession,
) -> dict:
    proposal_types = _commitment_related_proposal_types(commitment=commitment)
    influence = await workspace_proposal_arbitration_family_influence(
        proposal_types=proposal_types,
        related_zone=managed_scope,
        db=db,
        max_abs_bias=0.08,
    )
    sample_count = int(influence.get("sample_count", 0) or 0)
    expectation_weight = float(influence.get("aggregate_priority_bias", 0.0) or 0.0)
    if not proposal_types or abs(expectation_weight) < 1e-9:
        return {
            "expectation_weight": 0.0,
            "rationale": "",
            "related_zone": str(managed_scope or "").strip() or "global",
            "proposal_types": proposal_types,
            "sample_count": sample_count,
            "learning": influence.get("learning", []),
            "applied": False,
        }

    direction = "reinforced" if expectation_weight > 0 else "weakened"
    rationale = (
        f"Proposal arbitration outcomes {direction} the expectation that this commitment posture is appropriate "
        f"for scope {str(managed_scope or '').strip() or 'global'}."
    )
    return {
        "expectation_weight": round(expectation_weight, 6),
        "rationale": rationale,
        "related_zone": str(influence.get("related_zone", managed_scope) or "").strip() or "global",
        "proposal_types": influence.get("proposal_types", proposal_types),
        "sample_count": sample_count,
        "learning": influence.get("learning", []),
        "applied": sample_count >= 2 and abs(expectation_weight) >= 0.01,
    }


def to_operator_resolution_commitment_monitoring_out(
    row: WorkspaceOperatorResolutionCommitmentMonitoringProfile,
) -> dict:
    return {
        "monitoring_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "commitment_id": int(row.commitment_id),
        "managed_scope": row.managed_scope,
        "status": row.status,
        "commitment_status": row.commitment_status,
        "monitoring_window_hours": int(row.monitoring_window_hours or 0),
        "evidence_count": int(row.evidence_count or 0),
        "stewardship_cycle_count": int(row.stewardship_cycle_count or 0),
        "maintenance_run_count": int(row.maintenance_run_count or 0),
        "inquiry_question_count": int(row.inquiry_question_count or 0),
        "blocked_auto_execution_count": int(row.blocked_auto_execution_count or 0),
        "allowed_auto_execution_count": int(row.allowed_auto_execution_count or 0),
        "potential_violation_count": int(row.potential_violation_count or 0),
        "drift_score": round(float(row.drift_score or 0.0), 6),
        "compliance_score": round(float(row.compliance_score or 0.0), 6),
        "health_score": round(float(row.health_score or 0.0), 6),
        "governance_state": row.governance_state,
        "governance_decision": row.governance_decision,
        "governance_reason": row.governance_reason,
        "trigger_counts": _json_dict(row.trigger_counts_json),
        "trigger_evidence": _json_dict(row.trigger_evidence_json),
        "recommended_actions": _json_list(row.recommended_actions_json),
        "reasoning": _json_dict(row.reasoning_json),
        "metadata_json": _json_dict(row.metadata_json),
        "created_at": row.created_at,
    }


async def latest_commitment_monitoring_profile(
    *,
    commitment_id: int,
    db: AsyncSession,
) -> WorkspaceOperatorResolutionCommitmentMonitoringProfile | None:
    return (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitmentMonitoringProfile)
                .where(
                    WorkspaceOperatorResolutionCommitmentMonitoringProfile.commitment_id
                    == int(commitment_id)
                )
                .order_by(WorkspaceOperatorResolutionCommitmentMonitoringProfile.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def list_commitment_monitoring_profiles(
    *,
    commitment_id: int,
    limit: int,
    db: AsyncSession,
) -> list[WorkspaceOperatorResolutionCommitmentMonitoringProfile]:
    return (
        (
            await db.execute(
                select(WorkspaceOperatorResolutionCommitmentMonitoringProfile)
                .where(
                    WorkspaceOperatorResolutionCommitmentMonitoringProfile.commitment_id
                    == int(commitment_id)
                )
                .order_by(WorkspaceOperatorResolutionCommitmentMonitoringProfile.id.desc())
                .limit(max(1, int(limit)))
            )
        )
        .scalars()
        .all()
    )


async def get_commitment_monitoring_profile(
    *,
    monitoring_id: int,
    db: AsyncSession,
) -> WorkspaceOperatorResolutionCommitmentMonitoringProfile | None:
    return await db.get(
        WorkspaceOperatorResolutionCommitmentMonitoringProfile,
        int(monitoring_id),
    )


async def evaluate_operator_resolution_commitment_monitoring(
    *,
    commitment: WorkspaceOperatorResolutionCommitment,
    actor: str,
    source: str,
    lookback_hours: int,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceOperatorResolutionCommitmentMonitoringProfile:
    now = datetime.now(timezone.utc)
    sync_commitment_expiration(commitment, now=now)
    since = now - timedelta(hours=max(1, int(lookback_hours)))
    managed_scope = normalize_scope(commitment.managed_scope)
    commitment_effects = commitment_downstream_effects(commitment)

    stewardship_rows = (
        (
            await db.execute(
                select(WorkspaceStewardshipCycle)
                .where(WorkspaceStewardshipCycle.created_at >= since)
                .order_by(WorkspaceStewardshipCycle.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    stewardship_rows = [
        row
        for row in stewardship_rows
        if _stewardship_cycle_matches(
            row,
            commitment_id=int(commitment.id),
            managed_scope=managed_scope,
        )
    ]

    maintenance_rows = (
        (
            await db.execute(
                select(WorkspaceMaintenanceRun)
                .where(WorkspaceMaintenanceRun.created_at >= since)
                .order_by(WorkspaceMaintenanceRun.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    maintenance_rows = [
        row
        for row in maintenance_rows
        if _maintenance_run_matches(
            row,
            commitment_id=int(commitment.id),
            managed_scope=managed_scope,
        )
    ]

    inquiry_rows = (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion)
                .where(WorkspaceInquiryQuestion.created_at >= since)
                .order_by(WorkspaceInquiryQuestion.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    inquiry_rows = [
        row
        for row in inquiry_rows
        if _inquiry_question_matches(
            row,
            commitment_id=int(commitment.id),
            managed_scope=managed_scope,
        )
    ]

    governance_rows = (
        (
            await db.execute(
                select(WorkspaceExecutionTruthGovernanceProfile)
                .where(
                    WorkspaceExecutionTruthGovernanceProfile.managed_scope
                    == managed_scope
                )
                .order_by(WorkspaceExecutionTruthGovernanceProfile.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    latest_governance = governance_rows[0] if governance_rows else None

    stewardship_blocked = sum(
        1
        for row in stewardship_rows
        if bool(
            _json_dict(_json_dict(row.metadata_json).get("verification", {})).get(
                "operator_resolution_blocked_auto_execution", False
            )
        )
    )
    maintenance_blocked = sum(
        1
        for row in maintenance_rows
        if bool(
            _json_dict(row.maintenance_outcomes_json).get(
                "operator_resolution_blocked_auto_execution", False
            )
        )
    )
    blocked_auto_execution_count = stewardship_blocked + maintenance_blocked

    stewardship_actions_executed = sum(
        _safe_int(_json_dict(row.metadata_json).get("verification", {}).get("actions_executed", 0))
        for row in stewardship_rows
    )
    maintenance_actions_executed = sum(
        _safe_int(_json_dict(row.maintenance_outcomes_json).get("actions_executed", 0))
        for row in maintenance_rows
    )
    allowed_auto_execution_count = 0
    allowed_auto_execution_count += sum(
        1
        for row in stewardship_rows
        if bool(_json_dict(row.decision_json).get("allow_auto_execution", False))
    )
    allowed_auto_execution_count += sum(
        1
        for row in maintenance_rows
        if bool(_json_dict(row.metadata_json).get("auto_execute", False))
    )

    block_expected = bool(
        scope_value(commitment.decision_type)
        in {"require_additional_evidence", "defer_action", "lower_autonomy_for_scope"}
        or bool(commitment_effects.get("stewardship_defer_actions", False))
        or scope_value(commitment_effects.get("maintenance_mode")) == "deferred"
    )
    potential_violation_count = 0
    if block_expected:
        potential_violation_count += stewardship_actions_executed
        potential_violation_count += maintenance_actions_executed

    evidence_count = len(stewardship_rows) + len(maintenance_rows) + len(inquiry_rows)
    pressure_count = sum(
        1
        for row in stewardship_rows
        if bool(
            _json_dict(_json_dict(row.metadata_json).get("verification", {})).get(
                "persistent_degradation", False
            )
        )
    )
    pressure_count += sum(
        _safe_int(_json_dict(row.maintenance_outcomes_json).get("execution_truth_signal_count", 0))
        for row in maintenance_rows
    )
    governance_conflict = _latest_governance_conflict(
        latest_governance,
        commitment=commitment,
    )
    proposal_arbitration_expectation = await _proposal_arbitration_commitment_expectation(
        commitment=commitment,
        managed_scope=managed_scope,
        db=db,
    )

    compliance_score = 1.0
    if evidence_count > 0:
        compliance_score = _bounded(
            1.0 - (potential_violation_count / float(max(1, evidence_count)))
        )
    elif block_expected:
        compliance_score = 0.55

    drift_score = 0.0
    if commitment_is_expired(commitment, now=now):
        drift_score = 1.0
    else:
        drift_score += min(0.55, pressure_count * 0.12)
        drift_score += governance_conflict
        if block_expected and blocked_auto_execution_count > 0:
            drift_score += min(0.2, blocked_auto_execution_count * 0.05)
        if evidence_count == 0 and commitment_is_active(commitment, now=now):
            drift_score += 0.1
        drift_score = _bounded(drift_score)

    raw_drift_score = drift_score
    drift_score = _bounded(
        drift_score
        - float(proposal_arbitration_expectation.get("expectation_weight", 0.0) or 0.0)
    )

    freshness_score = 1.0 if evidence_count > 0 else 0.45
    health_score = _bounded(
        (compliance_score * 0.55) + ((1.0 - drift_score) * 0.35) + (freshness_score * 0.10)
    )

    commitment_status = scope_value(commitment.status) or "active"
    if commitment_status in {"revoked", "superseded"}:
        governance_state = "inactive"
        governance_decision = "monitor_only"
        governance_reason = "Commitment is no longer active and only remains for audit visibility."
    elif commitment_status == "expired" or commitment_is_expired(commitment, now=now):
        governance_state = "expired"
        governance_decision = "operator_review_required"
        governance_reason = "Commitment expired and should be reviewed before it continues shaping downstream behavior."
    elif potential_violation_count > 0:
        governance_state = "violating"
        governance_decision = "replace_commitment"
        governance_reason = "Observed downstream behavior violated the active commitment's execution posture."
    elif drift_score >= 0.75 or health_score <= 0.35:
        governance_state = "drifting"
        governance_decision = "operator_review_required"
        governance_reason = "Commitment is drifting far enough from current workspace evidence that it needs fresh operator review."
    elif drift_score >= 0.45 or health_score <= 0.6:
        governance_state = "watch"
        governance_decision = "inquiry_recalibration"
        governance_reason = "Commitment is still active but evidence suggests it may now be inefficient or overly restrictive."
    else:
        governance_state = "stable"
        governance_decision = "maintain_commitment"
        governance_reason = "Commitment remains aligned with current evidence and bounded downstream behavior."

    trigger_counts = {
        "stewardship_cycles": len(stewardship_rows),
        "maintenance_runs": len(maintenance_rows),
        "inquiry_questions": len(inquiry_rows),
        "blocked_auto_execution": blocked_auto_execution_count,
        "allowed_auto_execution": allowed_auto_execution_count,
        "potential_violations": potential_violation_count,
        "pressure_count": pressure_count,
    }
    trigger_evidence = {
        "commitment": commitment_snapshot(commitment),
        "latest_execution_truth_governance": {
            "governance_id": int(latest_governance.id),
            "governance_decision": latest_governance.governance_decision,
            "governance_state": latest_governance.governance_state,
        }
        if latest_governance is not None
        else {},
        "stewardship_cycle_ids": [int(row.id) for row in stewardship_rows[:20]],
        "maintenance_run_ids": [int(row.id) for row in maintenance_rows[:20]],
        "inquiry_question_ids": [int(row.id) for row in inquiry_rows[:20]],
    }
    reasoning = {
        "block_expected": block_expected,
        "governance_conflict": round(governance_conflict, 6),
        "stewardship_actions_executed": stewardship_actions_executed,
        "maintenance_actions_executed": maintenance_actions_executed,
        "freshness_score": round(freshness_score, 6),
        "raw_drift_score": round(raw_drift_score, 6),
        "proposal_arbitration_expectation": proposal_arbitration_expectation,
    }

    row = WorkspaceOperatorResolutionCommitmentMonitoringProfile(
        source=source,
        actor=actor,
        commitment_id=int(commitment.id),
        managed_scope=managed_scope,
        status="evaluated",
        commitment_status=commitment_status,
        monitoring_window_hours=max(1, int(lookback_hours)),
        evidence_count=evidence_count,
        stewardship_cycle_count=len(stewardship_rows),
        maintenance_run_count=len(maintenance_rows),
        inquiry_question_count=len(inquiry_rows),
        blocked_auto_execution_count=blocked_auto_execution_count,
        allowed_auto_execution_count=allowed_auto_execution_count,
        potential_violation_count=potential_violation_count,
        drift_score=round(drift_score, 6),
        compliance_score=round(compliance_score, 6),
        health_score=round(health_score, 6),
        governance_state=governance_state,
        governance_decision=governance_decision,
        governance_reason=governance_reason,
        trigger_counts_json=trigger_counts,
        trigger_evidence_json=trigger_evidence,
        recommended_actions_json=_recommended_actions(
            governance_state=governance_state,
            governance_decision=governance_decision,
            commitment=commitment,
        ),
        reasoning_json=reasoning,
        metadata_json={
            **(_json_dict(metadata_json)),
            "objective86_commitment_monitoring": True,
        },
    )
    db.add(row)
    await db.flush()
    return row