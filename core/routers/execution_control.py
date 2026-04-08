from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.execution_recovery_service import (
    evaluate_execution_recovery,
    evaluate_execution_recovery_learning,
    evaluate_execution_recovery_outcome,
    get_latest_execution_recovery_attempt,
    get_latest_execution_recovery_learning_profile,
    get_latest_execution_recovery_outcome,
    list_execution_recovery_learning_profiles,
    list_execution_recovery_attempts,
    list_execution_recovery_outcomes,
    record_execution_recovery_attempt,
    reset_execution_recovery_learning_profiles,
    summarize_execution_recovery_learning_telemetry,
    to_execution_recovery_attempt_out,
    to_execution_recovery_learning_out,
    to_execution_recovery_outcome_out,
)
from core.execution_policy_gate import build_intent_key, sync_execution_control_state
from core.execution_strategy_service import (
    advance_execution_strategy_plan,
    ensure_execution_strategy_plan,
    get_execution_strategy_plan,
    list_execution_strategy_plans,
    to_execution_strategy_plan_out,
)
from core.execution_trace_service import (
    get_execution_trace,
    list_execution_trace_events,
    to_execution_trace_event_out,
    to_execution_trace_out,
)
from core.intent_store import to_execution_intent_out
from core.journal import write_journal
from core.models import (
    CapabilityExecution,
    ExecutionIntent,
    ExecutionOverride,
    ExecutionRecoveryAttempt,
    ExecutionStrategyPlan,
    ExecutionStabilityProfile,
    ExecutionTaskOrchestration,
    WorkspaceAutonomousChain,
    WorkspaceOperatorResolutionCommitment,
    WorkspaceCapabilityChain,
    WorkspacePolicyConflictProfile,
)
from core.operator_commitment_monitoring_service import (
    evaluate_operator_resolution_commitment_monitoring,
    latest_commitment_monitoring_profile,
    to_operator_resolution_commitment_monitoring_out,
)
from core.operator_commitment_outcome_service import (
    evaluate_operator_resolution_commitment_outcome,
    latest_commitment_outcome_profile,
    to_operator_resolution_commitment_outcome_out,
)
from core.operator_resolution_service import (
    commitment_is_active,
    commitment_manual_reset,
    commitment_is_recovery_policy_tuning_derived,
    commitment_policy_source,
    commitment_reapplication_source_id,
    commitment_scope_application,
    create_operator_resolution_commitment_record,
    latest_recovery_policy_commitment,
    operator_resolution_commitment_out,
    recovery_commitment_lifecycle_state,
    scope_hierarchy,
)
from core.schemas import (
    ExecutionOverrideRequest,
    ExecutionRecoveryAttemptRequest,
    ExecutionRecoveryEvaluateRequest,
    ExecutionRecoveryLearningResetRequest,
    ExecutionRecoveryOutcomeEvaluateRequest,
    ExecutionRecoveryPolicyCommitmentPreviewRequest,
    ExecutionRecoveryPolicyCommitmentEvaluateRequest,
    ExecutionRecoveryPolicyTuningApplyRequest,
    ExecutionStrategyPlanAdvanceRequest,
    ExecutionStrategyPlanCreateRequest,
    ExecutionStabilityEvaluateRequest,
)
from core.stability_monitor import evaluate_execution_stability, to_execution_stability_out
from core.task_orchestrator import to_execution_task_orchestration_out

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_dict(raw: object) -> dict:
    return raw if isinstance(raw, dict) else {}


def _json_list(raw: object) -> list:
    return raw if isinstance(raw, list) else []


def _normalized_scope(raw: object) -> str:
    value = str(raw or "").strip()
    return value or "global"


def _commitment_payload_for_scope(
    commitment: WorkspaceOperatorResolutionCommitment | None,
    *,
    requested_scope: str,
) -> dict:
    if commitment is None:
        return {}
    payload = operator_resolution_commitment_out(commitment)
    payload["scope_application"] = commitment_scope_application(
        commitment,
        requested_scope=requested_scope,
    )
    payload["policy_source"] = commitment_policy_source(commitment)
    payload["lifecycle_state"] = recovery_commitment_lifecycle_state(commitment)
    return payload


def _policy_conflict_out(row: WorkspacePolicyConflictProfile | None) -> dict:
    if row is None:
        return {}
    return {
        "conflict_id": int(row.id),
        "managed_scope": str(row.managed_scope or "").strip(),
        "decision_family": str(row.decision_family or "").strip(),
        "proposal_type": str(row.proposal_type or "").strip(),
        "conflict_state": str(row.conflict_state or "").strip(),
        "winning_policy_source": str(row.winning_policy_source or "").strip(),
        "losing_policy_sources": [
            str(item).strip() for item in _json_list(row.losing_policy_sources_json) if str(item).strip()
        ],
        "precedence_rule": str(row.precedence_rule or "").strip(),
        "conflict_confidence": round(float(row.conflict_confidence or 0.0), 6),
        "resolution_reason": _json_dict(row.resolution_reason_json),
        "evidence_summary": _json_dict(row.evidence_summary_json),
        "policy_effects_json": _json_dict(row.policy_effects_json),
        "metadata_json": _json_dict(row.metadata_json),
        "created_at": row.created_at,
    }


async def _latest_recovery_commitment_conflict(
    *,
    managed_scope: str,
    db: AsyncSession,
) -> dict:
    scope = _normalized_scope(managed_scope)
    rows = (
        (
            await db.execute(
                select(WorkspacePolicyConflictProfile)
                .where(WorkspacePolicyConflictProfile.managed_scope == scope)
                .order_by(WorkspacePolicyConflictProfile.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        losing_sources = [
            str(item).strip() for item in _json_list(row.losing_policy_sources_json) if str(item).strip()
        ]
        winner = str(row.winning_policy_source or "").strip()
        if "execution_recovery_commitment" == winner or "execution_recovery_commitment" in losing_sources:
            return _policy_conflict_out(row)
    return {}


async def _count_active_scope_executions(*, managed_scope: str, db: AsyncSession) -> int:
    scope = _normalized_scope(managed_scope)
    rows = (
        (
            await db.execute(
                select(CapabilityExecution)
                .where(
                    or_(
                        CapabilityExecution.managed_scope == scope,
                        CapabilityExecution.managed_scope.like(f"{scope}/%"),
                    )
                )
                .order_by(CapabilityExecution.id.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return sum(
        1
        for row in rows
        if str(row.status or "").strip() in {"pending_confirmation", "dispatched", "accepted", "running", "blocked"}
    )


def _row_managed_scope(row: object) -> str:
    metadata = getattr(row, "metadata_json", {}) if isinstance(getattr(row, "metadata_json", {}), dict) else {}
    return _normalized_scope(metadata.get("managed_scope") or getattr(row, "managed_scope", ""))


async def _count_active_scope_chains(*, managed_scope: str, db: AsyncSession) -> dict:
    scope = _normalized_scope(managed_scope)
    task_rows = (
        (
            await db.execute(
                select(WorkspaceAutonomousChain)
                .order_by(WorkspaceAutonomousChain.id.desc())
                .limit(80)
            )
        )
        .scalars()
        .all()
    )
    capability_rows = (
        (
            await db.execute(
                select(WorkspaceCapabilityChain)
                .order_by(WorkspaceCapabilityChain.id.desc())
                .limit(80)
            )
        )
        .scalars()
        .all()
    )
    task_count = sum(
        1
        for row in task_rows
        if _row_managed_scope(row) in scope_hierarchy(scope)[:1] or _row_managed_scope(row).startswith(f"{scope}/") or scope.startswith(f"{_row_managed_scope(row)}/")
        if str(getattr(row, "status", "") or "").strip() in {"pending_approval", "active", "pending_confirmation"}
    )
    capability_count = sum(
        1
        for row in capability_rows
        if _row_managed_scope(row) in scope_hierarchy(scope)[:1] or _row_managed_scope(row).startswith(f"{scope}/") or scope.startswith(f"{_row_managed_scope(row)}/")
        if str(getattr(row, "status", "") or "").strip() in {"pending", "active", "pending_confirmation", "confirmed"}
    )
    return {
        "active_task_chain_count": task_count,
        "active_capability_chain_count": capability_count,
    }


def _recommended_recovery_governance_action(
    *,
    commitment: dict,
    monitoring: dict,
    conflict: dict,
    admission_posture: str,
) -> dict:
    expiry_signal = _json_dict(monitoring.get("expiry_signal", {}))
    reapply_signal = _json_dict(monitoring.get("reapply_signal", {}))
    if str(expiry_signal.get("state") or "").strip() == "ready_to_expire":
        return {
            "action": "expire_commitment",
            "reason": str(expiry_signal.get("reason") or "").strip(),
        }
    if str(reapply_signal.get("state") or "").strip() == "recommended":
        return {
            "action": "reapply_commitment",
            "reason": str(reapply_signal.get("reason") or "").strip(),
        }
    if str(conflict.get("conflict_state") or "").strip() in {"active_conflict", "cooldown_held"}:
        return {
            "action": "review_conflict",
            "reason": str(_json_dict(conflict.get("resolution_reason", {})).get("why_policy_a_overrode_policy_b") or "").strip(),
        }
    if admission_posture == "operator_required":
        return {
            "action": "operator_review_required",
            "reason": "Admission control currently requires operator confirmation in this scope.",
        }
    if commitment:
        return {
            "action": "maintain_commitment",
            "reason": "Recovery governance is active and stable for this scope.",
        }
    return {
        "action": "monitor_only",
        "reason": "No active recovery-derived commitment is currently shaping this scope.",
    }


async def _build_recovery_governance_rollup(
    *,
    managed_scope: str,
    execution_recovery: dict,
    commitment: WorkspaceOperatorResolutionCommitment | None,
    db: AsyncSession,
) -> dict:
    scope = _normalized_scope(managed_scope or execution_recovery.get("managed_scope") or "global")
    monitoring_row = None
    outcome_row = None
    if commitment is not None:
        monitoring_row = await latest_commitment_monitoring_profile(
            commitment_id=int(commitment.id),
            db=db,
        )
        outcome_row = await latest_commitment_outcome_profile(
            commitment_id=int(commitment.id),
            db=db,
        )
    commitment_payload = _commitment_payload_for_scope(commitment, requested_scope=scope)
    monitoring_payload = (
        to_operator_resolution_commitment_monitoring_out(monitoring_row)
        if monitoring_row is not None
        else {}
    )
    outcome_payload = (
        to_operator_resolution_commitment_outcome_out(outcome_row)
        if outcome_row is not None
        else {}
    )
    conflict_payload = await _latest_recovery_commitment_conflict(
        managed_scope=scope,
        db=db,
    )
    active_execution_count = await _count_active_scope_executions(managed_scope=scope, db=db)
    chain_counts = await _count_active_scope_chains(managed_scope=scope, db=db)
    scope_application = _json_dict(commitment_payload.get("scope_application", {}))
    downstream_effects = _json_dict(commitment_payload.get("downstream_effects_json", {}))
    admission_posture = "open"
    if commitment_payload and bool(commitment_payload.get("active", False)):
        requested_level = str(
            downstream_effects.get("autonomy_level") or downstream_effects.get("autonomy_level_cap") or ""
        ).strip()
        if requested_level in {"operator_required", "manual_only"}:
            admission_posture = "operator_required"
        elif requested_level:
            admission_posture = "advisory"
    preview = {
        "scope_hierarchy": scope_hierarchy(scope),
        "impacted_scope_count": len(scope_hierarchy(scope)),
        "active_execution_count": active_execution_count,
        **chain_counts,
        "rollout_risk": (
            "high"
            if active_execution_count > 0 or chain_counts.get("active_task_chain_count", 0) > 0 or chain_counts.get("active_capability_chain_count", 0) > 0
            else "low"
        ),
    }
    recommended_next_action = _recommended_recovery_governance_action(
        commitment=commitment_payload,
        monitoring=monitoring_payload,
        conflict=conflict_payload,
        admission_posture=admission_posture,
    )
    return {
        "managed_scope": scope,
        "tuning": _json_dict(execution_recovery.get("recovery_policy_tuning", {})),
        "commitment": commitment_payload,
        "monitoring": monitoring_payload,
        "outcome": outcome_payload,
        "conflict": conflict_payload,
        "preview": preview,
        "scope_application": scope_application,
        "admission_posture": admission_posture,
        "recommended_next_action": recommended_next_action,
        "summary": (
            f"scope={scope}; commitment={str(commitment_payload.get('lifecycle_state') or 'inactive')}; "
            f"admission={admission_posture}; next={str(recommended_next_action.get('action') or 'monitor_only')}"
        ),
    }


def _to_execution_override_out(row: ExecutionOverride) -> dict:
    return {
        "override_id": int(row.id),
        "trace_id": row.trace_id,
        "execution_id": row.execution_id,
        "managed_scope": row.managed_scope,
        "override_type": row.override_type,
        "reason": row.reason,
        "status": row.status,
        "priority": row.priority,
        "scope_json": row.scope_json if isinstance(row.scope_json, dict) else {},
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


@router.get("/execution/traces/{trace_id}")
async def get_execution_trace_endpoint(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    trace = await get_execution_trace(trace_id=trace_id, db=db)
    if trace is None:
        raise HTTPException(status_code=404, detail="execution_trace_not_found")
    events = [
        to_execution_trace_event_out(row)
        for row in await list_execution_trace_events(trace_id=trace_id, db=db)
    ]
    intent = (
        (
            await db.execute(
                select(ExecutionIntent)
                .where(ExecutionIntent.trace_id == trace_id)
                .order_by(ExecutionIntent.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    orchestration = (
        (
            await db.execute(
                select(ExecutionTaskOrchestration)
                .where(ExecutionTaskOrchestration.trace_id == trace_id)
                .order_by(ExecutionTaskOrchestration.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    stability = (
        (
            await db.execute(
                select(ExecutionStabilityProfile)
                .where(ExecutionStabilityProfile.trace_id == trace_id)
                .order_by(ExecutionStabilityProfile.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    strategy_plan = (
        (
            await db.execute(
                select(ExecutionStrategyPlan)
                .where(ExecutionStrategyPlan.trace_id == trace_id)
                .order_by(ExecutionStrategyPlan.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return {
        "trace": to_execution_trace_out(
            trace,
            events=events,
            intent=to_execution_intent_out(intent) if intent is not None else None,
            orchestration=(
                to_execution_task_orchestration_out(orchestration)
                if orchestration is not None
                else None
            ),
            stability=to_execution_stability_out(stability) if stability is not None else None,
            strategy_plan=(
                to_execution_strategy_plan_out(strategy_plan)
                if strategy_plan is not None
                else None
            ),
        )
    }


@router.get("/execution/intents")
async def list_execution_intents_endpoint(
    managed_scope: str = Query(default=""),
    trace_id: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(ExecutionIntent).order_by(ExecutionIntent.id.desc()).limit(limit)
    if str(managed_scope or "").strip():
        stmt = stmt.where(ExecutionIntent.managed_scope == str(managed_scope).strip())
    if str(trace_id or "").strip():
        stmt = stmt.where(ExecutionIntent.trace_id == str(trace_id).strip())
    rows = list((await db.execute(stmt)).scalars().all())
    return {"intents": [to_execution_intent_out(row) for row in rows]}


@router.get("/execution/orchestration/{trace_id}")
async def get_execution_orchestration_endpoint(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (
        (
            await db.execute(
                select(ExecutionTaskOrchestration)
                .where(ExecutionTaskOrchestration.trace_id == trace_id)
                .order_by(ExecutionTaskOrchestration.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="execution_orchestration_not_found")
    return {"orchestration": to_execution_task_orchestration_out(row)}


@router.get("/execution/strategy-plans")
async def list_execution_strategy_plans_endpoint(
    managed_scope: str = Query(default=""),
    trace_id: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    rows = await list_execution_strategy_plans(
        db=db,
        managed_scope=managed_scope,
        trace_id=trace_id,
        limit=limit,
    )
    return {"strategy_plans": [to_execution_strategy_plan_out(row) for row in rows]}


@router.get("/execution/strategy-plans/{plan_id}")
async def get_execution_strategy_plan_endpoint(
    plan_id: int,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_execution_strategy_plan(plan_id=plan_id, db=db)
    if row is None:
        raise HTTPException(status_code=404, detail="execution_strategy_plan_not_found")
    return {"strategy_plan": to_execution_strategy_plan_out(row)}


@router.post("/execution/strategy-plans")
async def create_execution_strategy_plan_endpoint(
    payload: ExecutionStrategyPlanCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution = None
    if payload.execution_id is not None:
        execution = await db.get(CapabilityExecution, payload.execution_id)
        if execution is None:
            raise HTTPException(status_code=404, detail="capability_execution_not_found")
    intent = None
    if payload.intent_id is not None:
        intent = await db.get(ExecutionIntent, payload.intent_id)
    if intent is None and execution is not None:
        intent = (
            (
                await db.execute(
                    select(ExecutionIntent)
                    .where(ExecutionIntent.trace_id == str(execution.trace_id or "").strip())
                    .order_by(ExecutionIntent.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
    if intent is None and str(payload.trace_id or "").strip():
        intent = (
            (
                await db.execute(
                    select(ExecutionIntent)
                    .where(ExecutionIntent.trace_id == str(payload.trace_id).strip())
                    .order_by(ExecutionIntent.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
    if intent is None:
        raise HTTPException(status_code=404, detail="execution_intent_not_found")

    orchestration = None
    if payload.orchestration_id is not None:
        orchestration = await db.get(ExecutionTaskOrchestration, payload.orchestration_id)
    if orchestration is None:
        orchestration = (
            (
                await db.execute(
                    select(ExecutionTaskOrchestration)
                    .where(ExecutionTaskOrchestration.trace_id == str(intent.trace_id or payload.trace_id or "").strip())
                    .order_by(ExecutionTaskOrchestration.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
    if orchestration is None:
        raise HTTPException(status_code=404, detail="execution_orchestration_not_found")

    row = await ensure_execution_strategy_plan(
        db=db,
        trace_id=str(payload.trace_id or intent.trace_id or "").strip(),
        intent=intent,
        orchestration=orchestration,
        execution_id=(int(execution.id) if execution is not None else payload.execution_id),
        actor=payload.actor,
        source=payload.source,
    )
    await db.commit()
    return {"strategy_plan": to_execution_strategy_plan_out(row)}


@router.post("/execution/strategy-plans/{plan_id}/advance")
async def advance_execution_strategy_plan_endpoint(
    plan_id: int,
    payload: ExecutionStrategyPlanAdvanceRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await get_execution_strategy_plan(plan_id=plan_id, db=db)
    if row is None:
        raise HTTPException(status_code=404, detail="execution_strategy_plan_not_found")
    row = await advance_execution_strategy_plan(
        plan=row,
        actor=payload.actor,
        source=payload.source,
        completed_step_key=payload.completed_step_key,
        outcome=payload.outcome,
        observed_confidence=payload.observed_confidence,
        metadata_json=payload.metadata_json,
        db=db,
    )
    await db.commit()
    return {"strategy_plan": to_execution_strategy_plan_out(row)}


@router.post("/execution/overrides")
async def create_execution_override_endpoint(
    payload: ExecutionOverrideRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution = None
    trace_id = str(payload.trace_id or "").strip()
    managed_scope = str(payload.managed_scope or "").strip() or "global"
    if payload.execution_id is not None:
        execution = await db.get(CapabilityExecution, payload.execution_id)
        if execution is None:
            raise HTTPException(status_code=404, detail="capability execution not found")
        trace_id = trace_id or str(execution.trace_id or "").strip()
        managed_scope = str(execution.managed_scope or managed_scope).strip() or "global"

    row = ExecutionOverride(
        trace_id=trace_id,
        execution_id=execution.id if execution is not None else None,
        source="operator",
        actor=payload.actor,
        managed_scope=managed_scope,
        override_type=payload.override_type,
        reason=payload.reason,
        status="active",
        priority=payload.priority,
        scope_json={"managed_scope": managed_scope},
        metadata_json=payload.metadata_json,
    )
    db.add(row)
    await db.flush()

    execution_out = None
    if execution is not None:
        feedback = execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
        if payload.override_type == "hard_stop":
            execution.dispatch_decision = "blocked"
            execution.status = "blocked"
            execution.reason = payload.reason or "operator_override_hard_stop"
        elif payload.override_type == "pause":
            execution.dispatch_decision = "requires_confirmation"
            execution.status = "pending_confirmation"
            execution.reason = payload.reason or "operator_override_pause"
        elif payload.override_type == "redirect":
            redirected_executor = str(payload.metadata_json.get("requested_executor") or "").strip()
            if redirected_executor:
                execution.requested_executor = redirected_executor
            execution.reason = payload.reason or "operator_override_redirect"
        execution.feedback_json = {
            **feedback,
            "operator_override": {
                "override_id": row.id,
                "override_type": row.override_type,
                "reason": row.reason,
            },
        }
        control_state = await sync_execution_control_state(
            db=db,
            execution=execution,
            actor=payload.actor,
            source="execution_override",
            requested_goal=str(feedback.get("requested_goal") or "").strip(),
            intent_key=build_intent_key(
                execution_source="execution",
                subject_id=execution.id,
                capability_name=execution.capability_name,
            ),
            intent_type="execution_override",
            context_json=payload.metadata_json,
            gate_result={
                "override_id": int(row.id),
                "override_type": row.override_type,
                "reason": row.reason,
            },
        )
        execution_out = {
            "execution_id": execution.id,
            "status": execution.status,
            "dispatch_decision": execution.dispatch_decision,
            "requested_executor": execution.requested_executor,
            "trace_id": control_state["trace_id"],
            "managed_scope": control_state["managed_scope"],
        }

    await write_journal(
        db,
        actor=payload.actor,
        action="execution_override_created",
        target_type="execution_override",
        target_id=str(row.id),
        summary=f"Execution override {row.id} created for scope {managed_scope}",
        metadata_json={
            "override_type": row.override_type,
            "execution_id": row.execution_id,
            "trace_id": row.trace_id,
        },
    )
    await db.commit()
    return {"override": _to_execution_override_out(row), "execution": execution_out}


@router.get("/execution/overrides")
async def list_execution_overrides_endpoint(
    managed_scope: str = Query(default=""),
    trace_id: str = Query(default=""),
    execution_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(ExecutionOverride).order_by(ExecutionOverride.id.desc()).limit(limit)
    if str(managed_scope or "").strip():
        stmt = stmt.where(ExecutionOverride.managed_scope == str(managed_scope).strip())
    if str(trace_id or "").strip():
        stmt = stmt.where(ExecutionOverride.trace_id == str(trace_id).strip())
    if execution_id is not None:
        stmt = stmt.where(ExecutionOverride.execution_id == execution_id)
    rows = list((await db.execute(stmt)).scalars().all())
    return {"overrides": [_to_execution_override_out(row) for row in rows]}


@router.post("/execution/stability/evaluate")
async def evaluate_execution_stability_endpoint(
    payload: ExecutionStabilityEvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await evaluate_execution_stability(
        db=db,
        managed_scope=payload.managed_scope,
        actor=payload.actor,
        source=payload.source,
        trace_id=payload.trace_id,
        metadata_json=payload.metadata_json,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="execution_stability_evaluated",
        target_type="execution_stability_profile",
        target_id=str(row.id),
        summary=f"Execution stability {row.id} evaluated for scope {payload.managed_scope}",
        metadata_json={
            "trace_id": payload.trace_id,
            "status": row.status,
            "mitigation_state": row.mitigation_state,
        },
    )
    await db.commit()
    return {"stability": to_execution_stability_out(row)}


@router.get("/execution/stability")
async def list_execution_stability_endpoint(
    managed_scope: str = Query(default=""),
    trace_id: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(ExecutionStabilityProfile).order_by(ExecutionStabilityProfile.id.desc()).limit(limit)
    if str(managed_scope or "").strip():
        stmt = stmt.where(ExecutionStabilityProfile.managed_scope == str(managed_scope).strip())
    if str(trace_id or "").strip():
        stmt = stmt.where(ExecutionStabilityProfile.trace_id == str(trace_id).strip())
    rows = list((await db.execute(stmt)).scalars().all())
    return {"stability": [to_execution_stability_out(row) for row in rows]}


@router.post("/execution/recovery/evaluate")
async def evaluate_execution_recovery_endpoint(
    payload: ExecutionRecoveryEvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    evaluation = await evaluate_execution_recovery(
        trace_id=payload.trace_id,
        execution_id=payload.execution_id,
        managed_scope=payload.managed_scope,
        environment_shift_detected=bool(
            (payload.metadata_json if isinstance(payload.metadata_json, dict) else {}).get(
                "environment_shift_detected", False
            )
        ),
        db=db,
    )
    if evaluation is None:
        raise HTTPException(status_code=404, detail="execution_recovery_target_not_found")
    latest_attempt = await get_latest_execution_recovery_attempt(
        trace_id=str(evaluation.get("trace_id") or ""),
        db=db,
    )
    return {
        "recovery": {
            **evaluation,
            "latest_attempt": (
                to_execution_recovery_attempt_out(latest_attempt) if latest_attempt is not None else {}
            ),
        }
    }


@router.post("/execution/recovery/attempt")
async def attempt_execution_recovery_endpoint(
    payload: ExecutionRecoveryAttemptRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await record_execution_recovery_attempt(
        trace_id=payload.trace_id,
        execution_id=payload.execution_id,
        managed_scope=payload.managed_scope,
        requested_decision=payload.requested_decision,
        actor=payload.actor,
        source=payload.source,
        reason=payload.reason,
        operator_ack=payload.operator_ack,
        metadata_json=payload.metadata_json,
        db=db,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="execution_recovery_target_not_found")
    await write_journal(
        db,
        actor=payload.actor,
        action="execution_recovery_attempt_recorded",
        target_type="execution_recovery_attempt",
        target_id=str(row.id),
        summary=f"Execution recovery attempt {row.id} recorded for trace {row.trace_id}",
        metadata_json={
            "execution_id": row.execution_id,
            "managed_scope": row.managed_scope,
            "recovery_decision": row.recovery_decision,
            "status": row.status,
            "recovery_classification": str(
                (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                    "recovery_classification", ""
                )
                or ""
            ).strip(),
            "recovery_taxonomy": (
                (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                    "recovery_taxonomy", {}
                )
                if isinstance(
                    (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                        "recovery_taxonomy", {}
                    ),
                    dict,
                )
                else {}
            ),
            "recovery_policy_tuning": (
                (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                    "recovery_policy_tuning", {}
                )
                if isinstance(
                    (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                        "recovery_policy_tuning", {}
                    ),
                    dict,
                )
                else {}
            ),
            **(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
        },
    )
    await db.commit()
    return {"attempt": to_execution_recovery_attempt_out(row)}


@router.get("/execution/recovery/{trace_id}")
async def get_execution_recovery_endpoint(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    evaluation = await evaluate_execution_recovery(
        trace_id=trace_id,
        execution_id=None,
        managed_scope="",
        db=db,
    )
    if evaluation is None:
        raise HTTPException(status_code=404, detail="execution_recovery_target_not_found")
    attempts = await list_execution_recovery_attempts(trace_id=trace_id, db=db, limit=20)
    outcomes = await list_execution_recovery_outcomes(trace_id=trace_id, db=db, limit=20)
    latest_attempt = attempts[0] if attempts else None
    latest_outcome = outcomes[0] if outcomes else None
    return {
        "recovery": {
            **evaluation,
            "latest_attempt": (
                to_execution_recovery_attempt_out(latest_attempt) if latest_attempt is not None else {}
            ),
            "latest_outcome": (
                to_execution_recovery_outcome_out(latest_outcome) if latest_outcome is not None else {}
            ),
            "attempts": [to_execution_recovery_attempt_out(row) for row in attempts],
            "outcomes": [to_execution_recovery_outcome_out(row) for row in outcomes],
        }
    }


@router.post("/execution/recovery/outcomes/evaluate")
async def evaluate_execution_recovery_outcome_endpoint(
    payload: ExecutionRecoveryOutcomeEvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = await evaluate_execution_recovery_outcome(
        trace_id=payload.trace_id,
        execution_id=payload.execution_id,
        managed_scope=payload.managed_scope,
        actor=payload.actor,
        source=payload.source,
        metadata_json=payload.metadata_json,
        db=db,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="execution_recovery_outcome_target_not_found")
    await write_journal(
        db,
        actor=payload.actor,
        action="execution_recovery_outcome_evaluated",
        target_type="execution_recovery_outcome",
        target_id=str(row.id),
        summary=f"Execution recovery outcome {row.id} evaluated for trace {row.trace_id}",
        metadata_json={
            "execution_id": row.execution_id,
            "managed_scope": row.managed_scope,
            "outcome_status": row.outcome_status,
            "recovery_classification": str(
                (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                    "recovery_classification", ""
                )
                or ""
            ).strip(),
            "recovery_taxonomy": (
                (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                    "recovery_taxonomy", {}
                )
                if isinstance(
                    (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                        "recovery_taxonomy", {}
                    ),
                    dict,
                )
                else {}
            ),
            "recovery_policy_tuning": (
                (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                    "recovery_policy_tuning", {}
                )
                if isinstance(
                    (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                        "recovery_policy_tuning", {}
                    ),
                    dict,
                )
                else {}
            ),
            "recovery_outcome_classification": str(
                (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                    "recovery_outcome_classification", ""
                )
                or ""
            ).strip(),
            "recovery_outcome_taxonomy": (
                (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                    "recovery_outcome_taxonomy", {}
                )
                if isinstance(
                    (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get(
                        "recovery_outcome_taxonomy", {}
                    ),
                    dict,
                )
                else {}
            ),
            **(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
        },
    )
    await db.commit()
    return {"outcome": to_execution_recovery_outcome_out(row)}


@router.get("/execution/recovery/outcomes/{trace_id}")
async def list_execution_recovery_outcomes_endpoint(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    outcomes = await list_execution_recovery_outcomes(trace_id=trace_id, db=db, limit=20)
    latest_outcome = await get_latest_execution_recovery_outcome(trace_id=trace_id, db=db)
    return {
        "outcomes": [to_execution_recovery_outcome_out(row) for row in outcomes],
        "latest_outcome": (
            to_execution_recovery_outcome_out(latest_outcome) if latest_outcome is not None else {}
        ),
    }


@router.get("/execution/recovery/learning/profiles")
async def list_execution_recovery_learning_profiles_endpoint(
    managed_scope: str = Query(..., min_length=1),
    capability_family: str = Query(""),
    recovery_decision: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    profiles = await list_execution_recovery_learning_profiles(
        managed_scope=managed_scope,
        db=db,
        capability_family=capability_family,
        recovery_decision=recovery_decision,
        limit=limit,
    )
    latest_profile = await get_latest_execution_recovery_learning_profile(
        managed_scope=managed_scope,
        db=db,
        capability_family=str(capability_family or "").strip(),
        recovery_decision=str(recovery_decision or "").strip(),
    )
    return {
        "profiles": [to_execution_recovery_learning_out(row) for row in profiles],
        "latest_profile": (
            to_execution_recovery_learning_out(latest_profile) if latest_profile is not None else {}
        ),
    }


@router.post("/execution/recovery/learning/reset")
async def reset_execution_recovery_learning_profiles_endpoint(
    payload: ExecutionRecoveryLearningResetRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    result = await reset_execution_recovery_learning_profiles(
        managed_scope=payload.managed_scope,
        actor=payload.actor,
        reason=payload.reason,
        capability_family=payload.capability_family,
        recovery_decision=payload.recovery_decision,
        db=db,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="execution_recovery_learning_reset",
        target_type="execution_recovery_learning_profile",
        target_id=str(payload.managed_scope),
        summary=f"Reset execution recovery learning for scope {payload.managed_scope}",
        metadata_json={
            "managed_scope": payload.managed_scope,
            "capability_family": payload.capability_family,
            "recovery_decision": payload.recovery_decision,
            "updated_profiles": int(result.get("updated", 0) or 0),
            **(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
        },
    )
    await db.commit()
    return result


@router.post("/execution/recovery/policy-tuning/apply")
async def apply_execution_recovery_policy_tuning_endpoint(
    payload: ExecutionRecoveryPolicyTuningApplyRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    evaluation = await evaluate_execution_recovery(
        trace_id=payload.trace_id,
        execution_id=payload.execution_id,
        managed_scope=payload.managed_scope,
        db=db,
    )
    if evaluation is None:
        raise HTTPException(status_code=404, detail="execution_recovery_target_not_found")

    tuning = (
        evaluation.get("recovery_policy_tuning", {})
        if isinstance(evaluation.get("recovery_policy_tuning", {}), dict)
        else {}
    )
    policy_action = str(tuning.get("policy_action") or "").strip()
    if not policy_action or policy_action == "maintain_current_recovery_autonomy":
        raise HTTPException(
            status_code=422,
            detail="execution_recovery_policy_tuning_not_actionable",
        )

    managed_scope = str(evaluation.get("managed_scope") or payload.managed_scope or "global").strip() or "global"
    recommended_boundary_level = str(
        tuning.get("recommended_boundary_level") or tuning.get("current_boundary_level") or "operator_required"
    ).strip() or "operator_required"
    recovery_learning = (
        evaluation.get("recovery_learning", {})
        if isinstance(evaluation.get("recovery_learning", {}), dict)
        else {}
    )
    recommendation_snapshot = {
        "source": "execution_recovery_policy_tuning",
        "trace_id": str(evaluation.get("trace_id") or payload.trace_id or "").strip(),
        "execution_id": evaluation.get("execution_id"),
        "managed_scope": managed_scope,
        "policy_action": policy_action,
        "current_boundary_level": str(tuning.get("current_boundary_level") or "").strip(),
        "recommended_boundary_level": recommended_boundary_level,
        "boundary_floor_applied": bool(tuning.get("boundary_floor_applied", False)),
        "summary": str(tuning.get("summary") or "").strip(),
        "rationale": str(tuning.get("rationale") or "").strip(),
        "recovery_decision": str(tuning.get("recovery_decision") or evaluation.get("recovery_decision") or "").strip(),
        "recommended_attempt_decision": str(
            tuning.get("recommended_attempt_decision") or evaluation.get("recommended_attempt_decision") or ""
        ).strip(),
        "recovery_classification": str(tuning.get("recovery_classification") or evaluation.get("recovery_classification") or "").strip(),
        "escalation_decision": str(recovery_learning.get("escalation_decision") or "").strip(),
    }
    downstream_effects = {
        "autonomy_level": recommended_boundary_level,
        "recovery_policy_action": policy_action,
        "recovery_boundary_scope": managed_scope,
    }
    expires_at = None
    if payload.duration_seconds is not None:
        expires_at = _utcnow() + timedelta(seconds=int(payload.duration_seconds))

    prior_recovery_commitment = await latest_recovery_policy_commitment(
        scope=managed_scope,
        db=db,
        include_inherited=False,
        require_active=False,
        limit=20,
    )
    reapplication_source_id = None
    if prior_recovery_commitment is not None and not commitment_is_active(prior_recovery_commitment):
        reapplication_source_id = int(prior_recovery_commitment.id)

    result = await create_operator_resolution_commitment_record(
        actor=payload.actor,
        source=str(payload.source or "execution_control").strip() or "execution_control",
        managed_scope=managed_scope,
        decision_type="lower_autonomy_for_scope",
        reason=str(payload.reason or tuning.get("rationale") or tuning.get("summary") or evaluation.get("recovery_reason") or "").strip(),
        recommendation_snapshot_json=recommendation_snapshot,
        authority_level=str(payload.authority_level or "operator_required").strip() or "operator_required",
        confidence=float(recovery_learning.get("confidence") or 0.0),
        provenance_json={
            "source": str(payload.source or "execution_control").strip() or "execution_control",
            "trace_id": str(evaluation.get("trace_id") or payload.trace_id or "").strip(),
            "execution_id": evaluation.get("execution_id"),
            "policy_action": policy_action,
        },
        downstream_effects_json=downstream_effects,
        metadata_json={
            **(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
            "objective121_recovery_policy_commitment": True,
            "commitment_family": "autonomy_posture",
            "policy_action": policy_action,
            **(
                {"reapplied_from_commitment_id": reapplication_source_id}
                if reapplication_source_id is not None
                else {}
            ),
        },
        expires_at=expires_at,
        db=db,
    )
    commitment = result.get("commitment", {}) if isinstance(result.get("commitment", {}), dict) else {}
    await write_journal(
        db,
        actor=payload.actor,
        action="execution_recovery_policy_tuning_applied",
        target_type="workspace_operator_resolution_commitment",
        target_id=str(commitment.get("commitment_id") or ""),
        summary=f"Applied recovery policy tuning for scope {managed_scope}",
        metadata_json={
            "trace_id": str(evaluation.get("trace_id") or payload.trace_id or "").strip(),
            "execution_id": evaluation.get("execution_id"),
            "managed_scope": managed_scope,
            "policy_action": policy_action,
            "recommended_boundary_level": recommended_boundary_level,
            "duplicate_suppressed": bool(result.get("duplicate_suppressed", False)),
            "reapplied_from_commitment_id": reapplication_source_id,
            **(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
        },
    )
    await db.commit()
    commitment_id = int(commitment.get("commitment_id", 0) or 0)
    row = await db.get(WorkspaceOperatorResolutionCommitment, commitment_id) if commitment_id > 0 else None
    rollup = await _build_recovery_governance_rollup(
        managed_scope=managed_scope,
        execution_recovery=evaluation,
        commitment=row,
        db=db,
    )
    return {
        "recovery": evaluation,
        "applied_tuning": tuning,
        "reapplication": {
            "reapplied": reapplication_source_id is not None and not bool(result.get("duplicate_suppressed", False)),
            "source_commitment_id": reapplication_source_id,
        },
        "recovery_governance": rollup,
        **result,
    }


@router.post("/execution/recovery/policy-tuning/commitment/evaluate")
async def evaluate_execution_recovery_policy_commitment_endpoint(
    payload: ExecutionRecoveryPolicyCommitmentEvaluateRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    evaluation = await evaluate_execution_recovery(
        trace_id=payload.trace_id,
        execution_id=payload.execution_id,
        managed_scope=payload.managed_scope,
        db=db,
    )
    if evaluation is None:
        raise HTTPException(status_code=404, detail="execution_recovery_target_not_found")

    managed_scope = str(evaluation.get("managed_scope") or payload.managed_scope or "global").strip() or "global"
    commitment = await latest_recovery_policy_commitment(
        scope=managed_scope,
        db=db,
        include_inherited=True,
        require_active=False,
        limit=40,
    )
    if commitment is None:
        raise HTTPException(status_code=404, detail="execution_recovery_policy_commitment_not_found")

    monitoring = await evaluate_operator_resolution_commitment_monitoring(
        commitment=commitment,
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        metadata_json={
            **(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
            "objective122_recovery_commitment_monitoring": True,
        },
        db=db,
    )
    outcome = await evaluate_operator_resolution_commitment_outcome(
        commitment=commitment,
        actor=payload.actor,
        source=payload.source,
        lookback_hours=payload.lookback_hours,
        target_status=payload.target_status,
        outcome_reason=str(payload.metadata_json.get("outcome_reason", "") or ""),
        metadata_json={
            **(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
            "objective122_recovery_commitment_outcome": True,
        },
        db=db,
    )
    await write_journal(
        db,
        actor=payload.actor,
        action="execution_recovery_policy_commitment_evaluated",
        target_type="workspace_operator_resolution_commitment",
        target_id=str(commitment.id),
        summary=f"Evaluated recovery policy commitment {commitment.id} for scope {managed_scope}",
        metadata_json={
            "commitment_id": int(commitment.id),
            "managed_scope": managed_scope,
            "monitoring_id": int(monitoring.id),
            "outcome_id": int(outcome.id),
            **(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
        },
    )
    await db.commit()
    await db.refresh(commitment)
    await db.refresh(monitoring)
    await db.refresh(outcome)
    rollup = await _build_recovery_governance_rollup(
        managed_scope=managed_scope,
        execution_recovery=evaluation,
        commitment=commitment,
        db=db,
    )
    return {
        "recovery": evaluation,
        "commitment": operator_resolution_commitment_out(commitment),
        "monitoring": to_operator_resolution_commitment_monitoring_out(monitoring),
        "outcome": to_operator_resolution_commitment_outcome_out(outcome),
        "recovery_governance": rollup,
    }


@router.post("/execution/recovery/policy-tuning/commitment/preview")
async def preview_execution_recovery_policy_commitment_endpoint(
    payload: ExecutionRecoveryPolicyCommitmentPreviewRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    evaluation = await evaluate_execution_recovery(
        trace_id=payload.trace_id,
        execution_id=payload.execution_id,
        managed_scope=payload.managed_scope,
        db=db,
    ) or {}
    managed_scope = _normalized_scope(
        payload.managed_scope or evaluation.get("managed_scope") or "global"
    )
    commitment = None
    if payload.commitment_id is not None:
        candidate = await db.get(WorkspaceOperatorResolutionCommitment, int(payload.commitment_id))
        if candidate is not None and commitment_is_recovery_policy_tuning_derived(candidate):
            commitment = candidate
    if commitment is None:
        commitment = await latest_recovery_policy_commitment(
            scope=managed_scope,
            db=db,
            include_inherited=True,
            require_active=payload.action == "apply",
            limit=40,
        )
    rollup = await _build_recovery_governance_rollup(
        managed_scope=managed_scope,
        execution_recovery=evaluation,
        commitment=commitment,
        db=db,
    )
    preview = _json_dict(rollup.get("preview", {}))
    preview.update(
        {
            "action": payload.action,
            "managed_scope": managed_scope,
            "commitment": _json_dict(rollup.get("commitment", {})),
            "admission_posture": str(rollup.get("admission_posture") or "open").strip(),
        }
    )
    if payload.action in {"revoke", "reset"}:
        preview["expected_transition"] = "inactive"
    elif payload.action == "expire":
        preview["expected_transition"] = "expired"
    elif payload.action == "reapply":
        preview["expected_transition"] = "reapplied"
    else:
        preview["expected_transition"] = "active"
    return {
        "preview": preview,
        "recovery_governance": rollup,
    }


@router.get("/execution/recovery/policy-tuning/governance")
async def get_execution_recovery_governance_rollup_endpoint(
    managed_scope: str = Query(default=""),
    trace_id: str = Query(default=""),
    execution_id: int | None = Query(default=None, ge=1),
    db: AsyncSession = Depends(get_db),
) -> dict:
    evaluation = await evaluate_execution_recovery(
        trace_id=trace_id,
        execution_id=execution_id,
        managed_scope=managed_scope,
        db=db,
    ) or {}
    scope = _normalized_scope(managed_scope or evaluation.get("managed_scope") or "global")
    commitment = await latest_recovery_policy_commitment(
        scope=scope,
        db=db,
        include_inherited=True,
        require_active=False,
        limit=40,
    )
    rollup = await _build_recovery_governance_rollup(
        managed_scope=scope,
        execution_recovery=evaluation,
        commitment=commitment,
        db=db,
    )
    return {"recovery_governance": rollup}


@router.get("/execution/recovery/learning/telemetry")
async def execution_recovery_learning_telemetry_endpoint(
    managed_scope: str = Query(default=""),
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await summarize_execution_recovery_learning_telemetry(
        db=db,
        managed_scope=managed_scope,
        limit=limit,
    )