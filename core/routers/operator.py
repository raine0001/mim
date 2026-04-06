from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.models import (
    CapabilityExecution,
    Goal,
    InputEvent,
    InputEventResolution,
    WorkspaceOperatorResolutionCommitment,
)
from core.operator_commitment_monitoring_service import (
    evaluate_operator_resolution_commitment_monitoring,
    get_commitment_monitoring_profile,
    list_commitment_monitoring_profiles,
    to_operator_resolution_commitment_monitoring_out,
)
from core.operator_commitment_outcome_service import (
    evaluate_operator_resolution_commitment_outcome,
    get_commitment_outcome_profile,
    list_commitment_outcome_profiles,
    to_operator_resolution_commitment_outcome_out,
)
from core.operator_preference_convergence_service import (
    converge_learned_preferences,
    get_learned_preference,
    list_learned_preferences,
)
from core.operator_resolution_service import commitment_is_expired, commitment_snapshot, sync_commitment_expiration
from core.preferences import apply_learning_signal
from core.schemas import (
    OperatorLearnedPreferenceConvergeRequest,
    OperatorLearnedPreferenceOut,
    OperatorExecutionActionRequest,
    OperatorResolutionCommitmentCreateRequest,
    OperatorResolutionCommitmentOutcomeEvaluateRequest,
    OperatorResolutionCommitmentResolveRequest,
    OperatorResolutionCommitmentMonitoringEvaluateRequest,
)

router = APIRouter(tags=["operator"])


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_commitment_family(*, commitment_family: str, decision_type: str) -> str:
    normalized = str(commitment_family or "").strip().lower()
    if normalized:
        return normalized
    decision = str(decision_type or "").strip().lower()
    family_map = {
        "approve_current_path": "path_disposition",
        "override_recommendation": "path_disposition",
        "defer_action": "action_timing",
        "require_additional_evidence": "evidence_gate",
        "lower_autonomy_for_scope": "autonomy_posture",
        "elevate_remediation_priority": "remediation_priority",
    }
    return family_map.get(decision, decision or "general")


def _to_operator_resolution_commitment(
    row: WorkspaceOperatorResolutionCommitment,
) -> dict:
    snapshot = commitment_snapshot(row)
    return {
        **snapshot,
        "source": row.source,
        "created_by": row.created_by,
        "reason": row.reason,
        "recommendation_snapshot_json": row.recommendation_snapshot_json if isinstance(row.recommendation_snapshot_json, dict) else {},
        "provenance_json": row.provenance_json if isinstance(row.provenance_json, dict) else {},
        "downstream_effects_json": row.downstream_effects_json if isinstance(row.downstream_effects_json, dict) else {},
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


async def _apply_due_commitment_expirations(*, db: AsyncSession) -> int:
    rows = (
        await db.execute(
            select(WorkspaceOperatorResolutionCommitment)
            .where(WorkspaceOperatorResolutionCommitment.status == "active")
            .order_by(WorkspaceOperatorResolutionCommitment.id.desc())
        )
    ).scalars().all()
    changed = 0
    now = _utcnow()
    for row in rows:
        if sync_commitment_expiration(row, now=now):
            changed += 1
    return changed


async def _get_resolution_commitment_or_404(
    *,
    commitment_id: int,
    db: AsyncSession,
) -> WorkspaceOperatorResolutionCommitment:
    row = await db.get(WorkspaceOperatorResolutionCommitment, commitment_id)
    if not row:
        raise HTTPException(status_code=404, detail="operator_resolution_commitment_not_found")
    changed = sync_commitment_expiration(row)
    if changed:
        await db.commit()
        await db.refresh(row)
    return row


def _normalize_exception_reason(
    execution: CapabilityExecution,
    resolution: InputEventResolution | None,
    event: InputEvent | None,
) -> str:
    reason_blob = " ".join(
        [
            str(execution.reason or "").lower(),
            str(resolution.reason if resolution else "").lower(),
            str(execution.feedback_json if isinstance(execution.feedback_json, dict) else {}).lower(),
        ]
    )

    if "feedback actor is not allowed" in reason_blob or "invalid feedback api key" in reason_blob:
        return "auth_rejected"

    if "dependency" in reason_blob:
        return "dependency_blocked"

    if execution.status == "blocked":
        if "capability_unavailable" in reason_blob:
            return "missing_capability"
        if event and event.source == "voice" and (
            (resolution and resolution.confidence_tier == "low")
            or "low_transcript_confidence" in reason_blob
            or "low_confidence_signal" in reason_blob
        ):
            return "low_voice_confidence"
        if event and event.source == "vision" and (
            (resolution and resolution.confidence_tier == "low")
            or "low_confidence_detection" in reason_blob
            or "low_confidence_signal" in reason_blob
        ):
            return "low_vision_confidence"
        return "blocked_by_policy"

    if execution.status == "failed":
        return "runtime_failure"

    return ""


def _to_operator_execution(
    execution: CapabilityExecution,
    resolution: InputEventResolution | None,
    event: InputEvent | None,
) -> dict:
    feedback = execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    return {
        "execution_id": execution.id,
        "input_event_id": execution.input_event_id,
        "resolution_id": execution.resolution_id,
        "goal_id": execution.goal_id,
        "capability_name": execution.capability_name,
        "status": execution.status,
        "dispatch_decision": execution.dispatch_decision,
        "reason": execution.reason,
        "exception_reason": _normalize_exception_reason(execution, resolution, event),
        "requested_executor": execution.requested_executor,
        "safety_mode": execution.safety_mode,
        "trace_id": execution.trace_id,
        "managed_scope": execution.managed_scope,
        "arguments_json": execution.arguments_json,
        "feedback_json": feedback,
        "replan_required": bool(feedback.get("replan_required", False)),
        "latest_replan_outcome": str(feedback.get("latest_replan_outcome", "")),
        "latest_predictive_signal_id": feedback.get("latest_predictive_signal_id"),
        "event_source": event.source if event else "unknown",
        "resolution_outcome": resolution.outcome if resolution else "unknown",
        "created_at": execution.created_at,
    }


def _to_operator_execution_summary(execution: CapabilityExecution) -> dict:
    return _to_operator_execution(execution, None, None)


async def _load_operator_context(
    execution_id: int,
    db: AsyncSession,
) -> tuple[CapabilityExecution, InputEventResolution | None, InputEvent | None]:
    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")

    resolution = await db.get(InputEventResolution, execution.resolution_id) if execution.resolution_id else None
    event = await db.get(InputEvent, execution.input_event_id)
    return execution, resolution, event


async def _serialize_operator_executions(
    executions: list[CapabilityExecution],
    db: AsyncSession,
) -> list[dict]:
    resolution_ids = sorted(
        {
            execution.resolution_id
            for execution in executions
            if execution.resolution_id is not None
        }
    )
    event_ids = sorted(
        {
            execution.input_event_id
            for execution in executions
            if execution.input_event_id is not None
        }
    )

    resolutions_by_id: dict[int, InputEventResolution] = {}
    if resolution_ids:
        resolution_rows = (
            await db.execute(
                select(InputEventResolution).where(
                    InputEventResolution.id.in_(resolution_ids)
                )
            )
        ).scalars().all()
        resolutions_by_id = {row.id: row for row in resolution_rows}

    events_by_id: dict[int, InputEvent] = {}
    if event_ids:
        event_rows = (
            await db.execute(select(InputEvent).where(InputEvent.id.in_(event_ids)))
        ).scalars().all()
        events_by_id = {row.id: row for row in event_rows}

    return [
        _to_operator_execution(
            execution,
            resolutions_by_id.get(execution.resolution_id),
            events_by_id.get(execution.input_event_id),
        )
        for execution in executions
    ]


async def _list_operator_executions_for_statuses(
    *,
    statuses: tuple[str, ...],
    limit: int,
    db: AsyncSession,
) -> list[dict]:
    rows = (
        await db.execute(
            select(CapabilityExecution)
            .where(CapabilityExecution.status.in_(statuses))
            .order_by(CapabilityExecution.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_to_operator_execution_summary(row) for row in rows]


async def _count_operator_executions_by_status(db: AsyncSession) -> dict[str, int]:
    rows = (
        await db.execute(
            select(CapabilityExecution.status, func.count())
            .where(
                CapabilityExecution.status.in_(
                    [
                        "pending_confirmation",
                        "blocked",
                        "failed",
                        "paused",
                        "dispatched",
                        "accepted",
                        "running",
                        "succeeded",
                    ]
                )
            )
            .group_by(CapabilityExecution.status)
        )
    ).all()
    return {str(status): int(count) for status, count in rows}


def _append_operator_action(execution: CapabilityExecution, action: str, actor: str, reason: str, metadata_json: dict) -> None:
    feedback = execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    history = list(feedback.get("operator_actions", []))
    history.append(
        {
            "action": action,
            "actor": actor,
            "reason": reason,
            "metadata": metadata_json,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    execution.feedback_json = {
        **feedback,
        "operator_actions": history,
    }


async def _journal_operator_action(
    *,
    db: AsyncSession,
    actor: str,
    action: str,
    execution: CapabilityExecution,
    prior_status: str,
    reason: str,
    metadata_json: dict,
) -> None:
    await write_journal(
        db,
        actor=actor,
        action=f"operator_{action}",
        target_type="capability_execution",
        target_id=str(execution.id),
        summary=f"Operator {action} execution {execution.id}: {prior_status}->{execution.status}",
        metadata_json={
            "execution_id": execution.id,
            "goal_id": execution.goal_id,
            "prior_status": prior_status,
            "new_status": execution.status,
            "reason": reason,
            **metadata_json,
        },
    )


@router.get("/inbox")
async def get_operator_inbox(
    limit: int = Query(default=25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    counts_by_status = await _count_operator_executions_by_status(db)
    pending = await _list_operator_executions_for_statuses(
        statuses=("pending_confirmation",), limit=limit, db=db
    )
    blocked = await _list_operator_executions_for_statuses(
        statuses=("blocked",), limit=limit, db=db
    )
    failed = await _list_operator_executions_for_statuses(
        statuses=("failed",), limit=limit, db=db
    )
    paused = await _list_operator_executions_for_statuses(
        statuses=("paused",), limit=limit, db=db
    )
    active = await _list_operator_executions_for_statuses(
        statuses=("dispatched", "accepted", "running"), limit=limit, db=db
    )
    succeeded_recent = await _list_operator_executions_for_statuses(
        statuses=("succeeded",), limit=limit, db=db
    )

    return {
        "generated_at": datetime.now(timezone.utc),
        "counts": {
            "pending_confirmations": counts_by_status.get("pending_confirmation", 0),
            "blocked": counts_by_status.get("blocked", 0),
            "failed": counts_by_status.get("failed", 0),
            "paused": counts_by_status.get("paused", 0),
            "active": sum(
                counts_by_status.get(status, 0)
                for status in ("dispatched", "accepted", "running")
            ),
            "succeeded_recent": counts_by_status.get("succeeded", 0),
        },
        "pending_confirmations": pending,
        "blocked": blocked,
        "failed": failed,
        "paused": paused,
        "active": active,
        "succeeded_recent": succeeded_recent,
    }


@router.post("/resolution-commitments")
async def create_operator_resolution_commitment(
    payload: OperatorResolutionCommitmentCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    expires_at = payload.expires_at
    if expires_at is None and payload.duration_seconds is not None:
        expires_at = _utcnow() + timedelta(seconds=int(payload.duration_seconds))

    commitment_family = _normalize_commitment_family(
        commitment_family=payload.commitment_family,
        decision_type=payload.decision_type,
    )

    await _apply_due_commitment_expirations(db=db)

    active_rows = (
        await db.execute(
            select(WorkspaceOperatorResolutionCommitment)
            .where(WorkspaceOperatorResolutionCommitment.managed_scope == payload.managed_scope.strip())
            .where(WorkspaceOperatorResolutionCommitment.commitment_family == commitment_family)
            .where(WorkspaceOperatorResolutionCommitment.status == "active")
            .order_by(WorkspaceOperatorResolutionCommitment.id.desc())
        )
    ).scalars().all()

    for existing in active_rows:
        if (
            str(existing.decision_type or "") == payload.decision_type.strip()
            and str(existing.reason or "") == str(payload.reason or "")
            and (existing.recommendation_snapshot_json if isinstance(existing.recommendation_snapshot_json, dict) else {})
            == payload.recommendation_snapshot_json
            and str(existing.authority_level or "") == payload.authority_level.strip()
        ):
            if existing.status != "active":
                existing.status = "active"
            await db.commit()
            await db.refresh(existing)
            return {
                "commitment": _to_operator_resolution_commitment(existing),
                "duplicate_suppressed": True,
                "superseded_commitment_ids": [],
            }

    row = WorkspaceOperatorResolutionCommitment(
        source="objective85",
        created_by=payload.actor,
        managed_scope=payload.managed_scope.strip(),
        commitment_family=commitment_family,
        decision_type=payload.decision_type.strip(),
        status="active",
        reason=payload.reason,
        recommendation_snapshot_json=payload.recommendation_snapshot_json,
        authority_level=payload.authority_level.strip(),
        confidence=float(payload.confidence or 0.0),
        provenance_json=payload.provenance_json,
        expires_at=expires_at,
        downstream_effects_json=payload.downstream_effects_json,
        metadata_json={
            **payload.metadata_json,
            "objective85_resolution_commitment": True,
        },
    )
    db.add(row)
    await db.flush()

    superseded_ids: list[int] = []
    for existing in active_rows:
        existing.status = "superseded"
        existing.superseded_by_commitment_id = row.id
        superseded_ids.append(int(existing.id))

    await write_journal(
        db,
        actor=payload.actor,
        action="operator_resolution_commitment_created",
        target_type="workspace_operator_resolution_commitment",
        target_id=str(row.id),
        summary=f"Created operator resolution commitment {row.id} for {row.managed_scope}",
        metadata_json={
            "managed_scope": row.managed_scope,
            "decision_type": row.decision_type,
            "commitment_family": row.commitment_family,
            "superseded_commitment_ids": superseded_ids,
            **payload.metadata_json,
        },
    )
    await db.commit()
    await db.refresh(row)
    return {
        "commitment": _to_operator_resolution_commitment(row),
        "duplicate_suppressed": False,
        "superseded_commitment_ids": superseded_ids,
    }


@router.get("/resolution-commitments")
async def list_operator_resolution_commitments(
    managed_scope: str = Query(default=""),
    status: str = Query(default=""),
    commitment_family: str = Query(default=""),
    active_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> dict:
    changed = await _apply_due_commitment_expirations(db=db)
    if changed:
        await db.commit()

    rows = (
        await db.execute(
            select(WorkspaceOperatorResolutionCommitment)
            .order_by(WorkspaceOperatorResolutionCommitment.id.desc())
        )
    ).scalars().all()

    filtered: list[WorkspaceOperatorResolutionCommitment] = []
    requested_status = str(status or "").strip().lower()
    requested_scope = str(managed_scope or "").strip()
    requested_family = str(commitment_family or "").strip().lower()
    for row in rows:
        if requested_scope and str(row.managed_scope or "").strip() != requested_scope:
            continue
        if requested_family and str(row.commitment_family or "").strip().lower() != requested_family:
            continue
        if requested_status and str(row.status or "").strip().lower() != requested_status:
            continue
        if active_only and str(row.status or "").strip().lower() != "active":
            continue
        filtered.append(row)
        if len(filtered) >= limit:
            break

    return {"commitments": [_to_operator_resolution_commitment(item) for item in filtered]}


@router.get("/resolution-commitments/{commitment_id}")
async def get_operator_resolution_commitment(
    commitment_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _get_resolution_commitment_or_404(commitment_id=commitment_id, db=db)
    return {"commitment": _to_operator_resolution_commitment(row)}


@router.post("/preferences/converge")
async def converge_operator_learned_preferences(
    payload: OperatorLearnedPreferenceConvergeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    preferences = await converge_learned_preferences(
        actor=payload.actor,
        source=payload.source,
        db=db,
        managed_scope=payload.managed_scope,
        decision_type=payload.decision_type,
        commitment_family=payload.commitment_family,
        lookback_hours=payload.lookback_hours,
        min_evidence=payload.min_evidence,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="operator_learned_preferences_converged",
        target_type="user_preference",
        target_id=str(payload.managed_scope or "global"),
        summary=(
            f"Converged {len(preferences)} learned operator preferences for "
            f"{payload.managed_scope or 'all_scopes'}"
        ),
        metadata_json={
            "managed_scope": payload.managed_scope,
            "decision_type": payload.decision_type,
            "commitment_family": payload.commitment_family,
            "lookback_hours": payload.lookback_hours,
            "min_evidence": payload.min_evidence,
            "preference_keys": [item.get("preference_key") for item in preferences],
        },
    )
    await db.commit()
    return {
        "preferences": [OperatorLearnedPreferenceOut(**item).model_dump() for item in preferences]
    }


@router.get("/preferences")
async def list_operator_learned_preferences_endpoint(
    managed_scope: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    preferences = await list_learned_preferences(
        db=db,
        managed_scope=managed_scope,
        limit=limit,
    )
    return {
        "preferences": [OperatorLearnedPreferenceOut(**item).model_dump() for item in preferences]
    }


@router.get("/preferences/{preference_key}")
async def get_operator_learned_preference_endpoint(
    preference_key: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    payload = await get_learned_preference(db=db, preference_key=preference_key)
    if payload is None:
        raise HTTPException(status_code=404, detail="operator_learned_preference_not_found")
    return {"preference": OperatorLearnedPreferenceOut(**payload).model_dump()}


@router.post("/resolution-commitments/{commitment_id}/monitoring/evaluate")
async def evaluate_operator_resolution_commitment_monitoring_endpoint(
    commitment_id: int,
    payload: OperatorResolutionCommitmentMonitoringEvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    commitment = await _get_resolution_commitment_or_404(
        commitment_id=commitment_id,
        db=db,
    )
    row = await evaluate_operator_resolution_commitment_monitoring(
        commitment=commitment,
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        metadata_json=payload.metadata_json,
        db=db,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="operator_resolution_commitment_monitoring_evaluated",
        target_type="workspace_operator_resolution_commitment_monitoring_profile",
        target_id=str(row.id),
        summary=(
            f"Evaluated operator resolution commitment monitoring {row.id} for commitment {commitment.id}"
        ),
        metadata_json={
            "commitment_id": int(commitment.id),
            "managed_scope": commitment.managed_scope,
            "governance_state": row.governance_state,
            "governance_decision": row.governance_decision,
            "health_score": float(row.health_score or 0.0),
            **payload.metadata_json,
        },
    )
    await db.commit()
    await db.refresh(row)
    return {
        "monitoring": to_operator_resolution_commitment_monitoring_out(row),
        "commitment": _to_operator_resolution_commitment(commitment),
    }


@router.get("/resolution-commitments/{commitment_id}/monitoring")
async def list_operator_resolution_commitment_monitoring(
    commitment_id: int,
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_resolution_commitment_or_404(commitment_id=commitment_id, db=db)
    rows = await list_commitment_monitoring_profiles(
        commitment_id=commitment_id,
        limit=limit,
        db=db,
    )
    return {
        "monitoring": [
            to_operator_resolution_commitment_monitoring_out(row) for row in rows
        ]
    }


@router.get("/resolution-commitments/{commitment_id}/monitoring/{monitoring_id}")
async def get_operator_resolution_commitment_monitoring(
    commitment_id: int,
    monitoring_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_resolution_commitment_or_404(commitment_id=commitment_id, db=db)
    row = await get_commitment_monitoring_profile(monitoring_id=monitoring_id, db=db)
    if not row or int(row.commitment_id or 0) != int(commitment_id):
        raise HTTPException(
            status_code=404,
            detail="operator_resolution_commitment_monitoring_not_found",
        )
    return {"monitoring": to_operator_resolution_commitment_monitoring_out(row)}


@router.post("/resolution-commitments/{commitment_id}/outcomes/evaluate")
async def evaluate_operator_resolution_commitment_outcome_endpoint(
    commitment_id: int,
    payload: OperatorResolutionCommitmentOutcomeEvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    commitment = await _get_resolution_commitment_or_404(
        commitment_id=commitment_id,
        db=db,
    )
    row = await evaluate_operator_resolution_commitment_outcome(
        commitment=commitment,
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        target_status=payload.target_status,
        outcome_reason=str(payload.metadata_json.get("outcome_reason", "") or ""),
        metadata_json=payload.metadata_json,
        db=db,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="operator_resolution_commitment_outcome_evaluated",
        target_type="workspace_operator_resolution_commitment_outcome_profile",
        target_id=str(row.id),
        summary=(
            f"Evaluated operator resolution commitment outcome {row.id} for commitment {commitment.id}"
        ),
        metadata_json={
            "commitment_id": int(commitment.id),
            "managed_scope": commitment.managed_scope,
            "outcome_status": row.outcome_status,
            "stability_score": float(row.stability_score or 0.0),
            "effectiveness_score": float(row.effectiveness_score or 0.0),
            **payload.metadata_json,
        },
    )
    await db.commit()
    await db.refresh(commitment)
    await db.refresh(row)
    return {
        "outcome": to_operator_resolution_commitment_outcome_out(row),
        "commitment": _to_operator_resolution_commitment(commitment),
    }


@router.get("/resolution-commitments/{commitment_id}/outcomes")
async def list_operator_resolution_commitment_outcomes(
    commitment_id: int,
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_resolution_commitment_or_404(commitment_id=commitment_id, db=db)
    rows = await list_commitment_outcome_profiles(
        commitment_id=commitment_id,
        limit=limit,
        db=db,
    )
    return {
        "outcomes": [to_operator_resolution_commitment_outcome_out(row) for row in rows]
    }


@router.get("/resolution-commitments/{commitment_id}/outcomes/{outcome_id}")
async def get_operator_resolution_commitment_outcome(
    commitment_id: int,
    outcome_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_resolution_commitment_or_404(commitment_id=commitment_id, db=db)
    row = await get_commitment_outcome_profile(outcome_id=outcome_id, db=db)
    if not row or int(row.commitment_id or 0) != int(commitment_id):
        raise HTTPException(
            status_code=404,
            detail="operator_resolution_commitment_outcome_not_found",
        )
    return {"outcome": to_operator_resolution_commitment_outcome_out(row)}


@router.post("/resolution-commitments/{commitment_id}/resolve")
async def resolve_operator_resolution_commitment(
    commitment_id: int,
    payload: OperatorResolutionCommitmentResolveRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    commitment = await _get_resolution_commitment_or_404(
        commitment_id=commitment_id,
        db=db,
    )
    row = await evaluate_operator_resolution_commitment_outcome(
        commitment=commitment,
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        target_status=payload.target_status,
        outcome_reason=payload.reason,
        metadata_json={
            **payload.metadata_json,
            "manual_resolution": True,
        },
        db=db,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="operator_resolution_commitment_resolved",
        target_type="workspace_operator_resolution_commitment",
        target_id=str(commitment.id),
        summary=(
            f"Resolved operator resolution commitment {commitment.id} as {payload.target_status}"
        ),
        metadata_json={
            "outcome_id": int(row.id),
            "target_status": payload.target_status,
            "reason": payload.reason,
            **payload.metadata_json,
        },
    )
    await db.commit()
    await db.refresh(commitment)
    await db.refresh(row)
    return {
        "outcome": to_operator_resolution_commitment_outcome_out(row),
        "commitment": _to_operator_resolution_commitment(commitment),
    }


@router.post("/resolution-commitments/{commitment_id}/revoke")
async def revoke_operator_resolution_commitment(
    commitment_id: int,
    payload: OperatorExecutionActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _get_resolution_commitment_or_404(commitment_id=commitment_id, db=db)
    prior_status = row.status
    row.status = "revoked"
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "revoked_by": payload.actor,
        "revoked_reason": payload.reason,
        **payload.metadata_json,
    }
    await write_journal(
        db,
        actor=payload.actor,
        action="operator_resolution_commitment_revoked",
        target_type="workspace_operator_resolution_commitment",
        target_id=str(row.id),
        summary=f"Revoked operator resolution commitment {row.id}: {prior_status}->revoked",
        metadata_json={"prior_status": prior_status, **payload.metadata_json},
    )
    await db.commit()
    await db.refresh(row)
    return {"commitment": _to_operator_resolution_commitment(row)}


@router.post("/resolution-commitments/{commitment_id}/expire")
async def expire_operator_resolution_commitment(
    commitment_id: int,
    payload: OperatorExecutionActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await _get_resolution_commitment_or_404(commitment_id=commitment_id, db=db)
    prior_status = row.status
    row.status = "expired"
    row.expires_at = _utcnow()
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        "expired_by": payload.actor,
        "expired_reason": payload.reason,
        **payload.metadata_json,
    }
    await write_journal(
        db,
        actor=payload.actor,
        action="operator_resolution_commitment_expired",
        target_type="workspace_operator_resolution_commitment",
        target_id=str(row.id),
        summary=f"Expired operator resolution commitment {row.id}: {prior_status}->expired",
        metadata_json={"prior_status": prior_status, **payload.metadata_json},
    )
    await db.commit()
    await db.refresh(row)
    return {"commitment": _to_operator_resolution_commitment(row)}


@router.get("/executions")
async def list_operator_executions(
    status: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    requested = {item.strip() for item in status.split(",") if item.strip()}
    rows_query = select(CapabilityExecution).order_by(CapabilityExecution.id.desc()).limit(limit)
    if requested:
        rows_query = rows_query.where(CapabilityExecution.status.in_(requested))
    rows = (await db.execute(rows_query)).scalars().all()
    return [_to_operator_execution_summary(row) for row in rows]


@router.get("/executions/{execution_id}")
async def get_operator_execution(execution_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    execution, resolution, event = await _load_operator_context(execution_id, db)
    return _to_operator_execution(execution, resolution, event)


@router.get("/executions/{execution_id}/observations")
async def get_operator_execution_observations(execution_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    execution, _, _ = await _load_operator_context(execution_id, db)
    feedback = execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    observations = feedback.get("observations", [])
    return {
        "execution_id": execution.id,
        "capability_name": execution.capability_name,
        "observation_event_id": feedback.get("observation_event_id"),
        "observations": observations if isinstance(observations, list) else [],
        "detected_labels": feedback.get("detected_labels", []),
    }


@router.post("/executions/{execution_id}/approve")
async def approve_execution(
    execution_id: int,
    payload: OperatorExecutionActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution, resolution, event = await _load_operator_context(execution_id, db)
    if execution.status not in {"pending_confirmation", "blocked"}:
        raise HTTPException(status_code=422, detail="execution is not in an approvable state")

    prior = execution.status
    execution.status = "dispatched"
    execution.dispatch_decision = "operator_approved"
    execution.reason = payload.reason or "operator_approved"
    _append_operator_action(execution, "approve", payload.actor, execution.reason, payload.metadata_json)

    await _journal_operator_action(
        db=db,
        actor=payload.actor,
        action="approve",
        execution=execution,
        prior_status=prior,
        reason=execution.reason,
        metadata_json=payload.metadata_json,
    )
    await apply_learning_signal(db=db, signal="operator_approve", user_id=payload.actor)
    await db.commit()
    await db.refresh(execution)
    return _to_operator_execution(execution, resolution, event)


@router.post("/executions/{execution_id}/reject")
async def reject_execution(
    execution_id: int,
    payload: OperatorExecutionActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution, resolution, event = await _load_operator_context(execution_id, db)
    if execution.status not in {"pending_confirmation", "dispatched", "accepted", "running"}:
        raise HTTPException(status_code=422, detail="execution is not in a rejectable state")

    prior = execution.status
    execution.status = "blocked"
    execution.dispatch_decision = "operator_rejected"
    execution.reason = payload.reason or "operator_rejected"
    _append_operator_action(execution, "reject", payload.actor, execution.reason, payload.metadata_json)

    await _journal_operator_action(
        db=db,
        actor=payload.actor,
        action="reject",
        execution=execution,
        prior_status=prior,
        reason=execution.reason,
        metadata_json=payload.metadata_json,
    )
    await apply_learning_signal(db=db, signal="operator_reject", user_id=payload.actor)
    await db.commit()
    await db.refresh(execution)
    return _to_operator_execution(execution, resolution, event)


@router.post("/executions/{execution_id}/retry")
async def retry_execution(
    execution_id: int,
    payload: OperatorExecutionActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution, resolution, event = await _load_operator_context(execution_id, db)
    if execution.status not in {"failed", "blocked"}:
        raise HTTPException(status_code=422, detail="execution is not in a retryable state")

    prior = execution.status
    feedback = execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    retry_count = int(feedback.get("operator_retry_count", 0)) + 1

    execution.status = "dispatched"
    execution.dispatch_decision = "operator_retry"
    execution.reason = payload.reason or "operator_retry"
    execution.feedback_json = {
        **feedback,
        "operator_retry_count": retry_count,
    }
    _append_operator_action(execution, "retry", payload.actor, execution.reason, payload.metadata_json)

    await _journal_operator_action(
        db=db,
        actor=payload.actor,
        action="retry",
        execution=execution,
        prior_status=prior,
        reason=execution.reason,
        metadata_json={"retry_count": retry_count, **payload.metadata_json},
    )
    await db.commit()
    await db.refresh(execution)
    return _to_operator_execution(execution, resolution, event)


@router.post("/executions/{execution_id}/resume")
async def resume_execution(
    execution_id: int,
    payload: OperatorExecutionActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution, resolution, event = await _load_operator_context(execution_id, db)
    if execution.status not in {"blocked", "failed", "pending_confirmation"}:
        raise HTTPException(status_code=422, detail="execution is not in a resumable state")

    prior = execution.status
    execution.status = "running"
    execution.dispatch_decision = "operator_resume"
    execution.reason = payload.reason or "operator_resume"
    _append_operator_action(execution, "resume", payload.actor, execution.reason, payload.metadata_json)

    await _journal_operator_action(
        db=db,
        actor=payload.actor,
        action="resume",
        execution=execution,
        prior_status=prior,
        reason=execution.reason,
        metadata_json=payload.metadata_json,
    )
    await db.commit()
    await db.refresh(execution)
    return _to_operator_execution(execution, resolution, event)


@router.post("/executions/{execution_id}/cancel")
async def cancel_execution(
    execution_id: int,
    payload: OperatorExecutionActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution, resolution, event = await _load_operator_context(execution_id, db)
    if execution.status not in {"pending_confirmation", "dispatched", "accepted", "running"}:
        raise HTTPException(status_code=422, detail="execution is not in a cancellable state")

    prior = execution.status
    execution.status = "blocked"
    execution.dispatch_decision = "operator_cancelled"
    execution.reason = payload.reason or "operator_cancelled"
    _append_operator_action(execution, "cancel", payload.actor, execution.reason, payload.metadata_json)

    await _journal_operator_action(
        db=db,
        actor=payload.actor,
        action="cancel",
        execution=execution,
        prior_status=prior,
        reason=execution.reason,
        metadata_json=payload.metadata_json,
    )
    await db.commit()
    await db.refresh(execution)
    return _to_operator_execution(execution, resolution, event)


@router.post("/executions/{execution_id}/ignore")
async def ignore_execution(
    execution_id: int,
    payload: OperatorExecutionActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution, resolution, event = await _load_operator_context(execution_id, db)
    prior = execution.status
    execution.status = "succeeded"
    execution.dispatch_decision = "operator_ignored"
    execution.reason = payload.reason or "operator_ignored"
    _append_operator_action(execution, "ignore", payload.actor, execution.reason, payload.metadata_json)

    await _journal_operator_action(
        db=db,
        actor=payload.actor,
        action="ignore",
        execution=execution,
        prior_status=prior,
        reason=execution.reason,
        metadata_json=payload.metadata_json,
    )
    await db.commit()
    await db.refresh(execution)
    return _to_operator_execution(execution, resolution, event)


@router.post("/executions/{execution_id}/request-rescan")
async def request_rescan_execution(
    execution_id: int,
    payload: OperatorExecutionActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution, resolution, event = await _load_operator_context(execution_id, db)
    prior = execution.status
    execution.status = "dispatched"
    execution.dispatch_decision = "operator_rescan"
    execution.reason = payload.reason or "operator_requested_rescan"

    feedback = execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    rescan_count = int(feedback.get("operator_rescan_count", 0)) + 1
    execution.feedback_json = {
        **feedback,
        "operator_rescan_count": rescan_count,
    }
    _append_operator_action(execution, "request_rescan", payload.actor, execution.reason, payload.metadata_json)

    await _journal_operator_action(
        db=db,
        actor=payload.actor,
        action="request_rescan",
        execution=execution,
        prior_status=prior,
        reason=execution.reason,
        metadata_json={"rescan_count": rescan_count, **payload.metadata_json},
    )
    await db.commit()
    await db.refresh(execution)
    return _to_operator_execution(execution, resolution, event)


@router.post("/executions/{execution_id}/promote-to-goal")
async def promote_execution_to_goal(
    execution_id: int,
    payload: OperatorExecutionActionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution, resolution, event = await _load_operator_context(execution_id, db)
    if not resolution:
        raise HTTPException(status_code=422, detail="execution has no resolution context")

    goal: Goal | None = await db.get(Goal, resolution.goal_id) if resolution.goal_id else None
    if goal is None:
        goal = Goal(
            objective_id=None,
            task_id=None,
            goal_type=f"gateway_{resolution.internal_intent}",
            goal_description=resolution.proposed_goal_description,
            requested_by=payload.actor,
            priority="normal",
            status="new",
        )
        db.add(goal)
        await db.flush()
        resolution.goal_id = goal.id
        execution.goal_id = goal.id
    else:
        goal.status = "new"
        goal.requested_by = payload.actor

    resolution.outcome = "auto_execute"
    resolution.resolution_status = "auto_execute"
    resolution.safety_decision = "auto_execute"
    resolution.reason = "promoted_by_operator"
    resolution.metadata_json = {
        **(resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}),
        "promoted": True,
        "promoted_by": payload.actor,
    }

    prior = execution.status
    execution.status = "dispatched"
    execution.dispatch_decision = "operator_promoted"
    execution.reason = payload.reason or "operator_promoted_to_goal"
    _append_operator_action(execution, "promote_to_goal", payload.actor, execution.reason, payload.metadata_json)

    await _journal_operator_action(
        db=db,
        actor=payload.actor,
        action="promote_to_goal",
        execution=execution,
        prior_status=prior,
        reason=execution.reason,
        metadata_json={"goal_id": execution.goal_id, **payload.metadata_json},
    )
    await db.commit()
    await db.refresh(execution)
    return _to_operator_execution(execution, resolution, event)