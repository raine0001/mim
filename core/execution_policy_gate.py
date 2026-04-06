from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.execution_readiness_service import (
    execution_readiness_confidence,
    execution_readiness_policy_effects,
    execution_readiness_precedence,
    execution_readiness_posture,
    load_latest_execution_readiness,
    publish_execution_readiness_state,
)
from core.execution_trace_service import (
    infer_managed_scope,
    new_trace_id,
    ensure_execution_trace,
    append_execution_trace_event,
)
from core.execution_truth_governance_service import latest_execution_truth_governance_snapshot
from core.intent_store import ensure_execution_intent
from core.models import CapabilityExecution, ExecutionOverride, ExecutionStabilityProfile
from core.operator_resolution_service import (
    commitment_downstream_effects,
    commitment_is_active,
    commitment_snapshot,
    latest_active_operator_resolution_commitment,
)
from core.policy_conflict_resolution_service import _candidate_payload, _resolve_policy_conflict_profile
from core.stability_monitor import evaluate_execution_stability, to_execution_stability_out
from core.task_orchestrator import ensure_execution_orchestration, to_execution_task_orchestration_out


CONFLICT_DECISION_FAMILY_EXECUTION = "execution_policy_gate"
ACTIVE_OVERRIDE_STATUSES = {"active"}


def _json_safe(raw: object) -> object:
    if isinstance(raw, datetime):
        value = raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(raw, dict):
        return {str(key): _json_safe(value) for key, value in raw.items()}
    if isinstance(raw, list):
        return [_json_safe(item) for item in raw]
    if isinstance(raw, tuple):
        return [_json_safe(item) for item in raw]
    return raw


def build_intent_key(*, execution_source: str, subject_id: int | None, capability_name: str) -> str:
    identifier = int(subject_id or 0)
    if identifier > 0:
        return f"{execution_source}:{identifier}"
    return f"capability:{str(capability_name or '').strip() or 'unknown'}"


async def list_active_execution_overrides(
    *,
    db: AsyncSession,
    managed_scope: str,
    execution_id: int | None = None,
    trace_id: str = "",
) -> list[ExecutionOverride]:
    scope = str(managed_scope or "").strip() or "global"
    rows = list(
        (
            await db.execute(
                select(ExecutionOverride)
                .where(ExecutionOverride.status.in_(ACTIVE_OVERRIDE_STATUSES))
                .order_by(ExecutionOverride.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    filtered: list[ExecutionOverride] = []
    for row in rows:
        row_scope = str(row.managed_scope or "").strip() or "global"
        if row.execution_id is not None and execution_id is not None and int(row.execution_id) == int(execution_id):
            filtered.append(row)
            continue
        if row.trace_id and trace_id and str(row.trace_id).strip() == str(trace_id).strip():
            filtered.append(row)
            continue
        if row_scope == scope:
            filtered.append(row)
    return filtered


def _governance_requires_review(governance: dict) -> bool:
    decision = str(governance.get("governance_decision") or "").strip()
    actions = governance.get("downstream_actions", {}) if isinstance(governance.get("downstream_actions", {}), dict) else {}
    return decision in {
        "lower_autonomy_boundary",
        "require_sandbox_experiment",
        "escalate_to_operator",
    } or bool(actions.get("visibility_only", False)) or not bool(
        actions.get("stewardship_auto_execute_allowed", True)
    )


def _commitment_requires_review(commitment: object) -> bool:
    if commitment is None or not commitment_is_active(commitment):
        return False
    effects = commitment_downstream_effects(commitment)
    requested_level = str(effects.get("autonomy_level") or effects.get("autonomy_level_cap") or "").strip()
    return str(getattr(commitment, "decision_type", "") or "").strip() in {
        "defer_action",
        "require_additional_evidence",
        "lower_autonomy_for_scope",
    } or requested_level in {"manual_only", "operator_required"}


def _override_policy_effects(row: ExecutionOverride) -> dict:
    if row.override_type == "hard_stop":
        return {
            "hard_stop": True,
            "target_status": "blocked",
            "target_dispatch_decision": "blocked",
            "reason": "operator_override_hard_stop",
            "why_policy_prevailed": "Active operator override hard-stopped execution in this scope.",
        }
    if row.override_type == "pause":
        return {
            "require_operator_confirmation": True,
            "target_status": "pending_confirmation",
            "target_dispatch_decision": "requires_confirmation",
            "reason": "operator_override_pause",
            "why_policy_prevailed": "Active operator override paused automatic execution in this scope.",
        }
    redirected_executor = str(
        (row.metadata_json if isinstance(row.metadata_json, dict) else {}).get("requested_executor")
        or (row.scope_json if isinstance(row.scope_json, dict) else {}).get("requested_executor")
        or ""
    ).strip()
    return {
        "redirect_executor": redirected_executor,
        "reason": "operator_override_redirect",
        "why_policy_prevailed": "Active operator override redirected execution to a different executor.",
    }


async def evaluate_execution_policy_gate(
    *,
    db: AsyncSession,
    capability_name: str,
    requested_decision: str,
    requested_status: str,
    requested_reason: str,
    requested_executor: str,
    safety_mode: str,
    managed_scope: str,
    actor: str,
    source: str,
    metadata_json: dict | None = None,
    execution_id: int | None = None,
    trace_id: str = "",
) -> dict:
    scope = str(managed_scope or "").strip() or "global"
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    readiness = load_latest_execution_readiness(
        action=str(capability_name or requested_executor or "execution").strip(),
        capability_name=capability_name,
        managed_scope=scope,
        requested_executor=requested_executor,
        metadata_json=metadata,
    )
    readiness_state = await publish_execution_readiness_state(
        db=db,
        actor=actor,
        source=source,
        readiness=readiness,
        metadata_json={
            "capability_name": str(capability_name or "").strip(),
            "requested_executor": str(requested_executor or "").strip(),
            "managed_scope": scope,
        },
    )
    commitment = await latest_active_operator_resolution_commitment(scope=scope, db=db, limit=20)
    governance = await latest_execution_truth_governance_snapshot(managed_scope=scope, db=db)
    overrides = await list_active_execution_overrides(
        db=db,
        managed_scope=scope,
        execution_id=execution_id,
        trace_id=trace_id,
    )
    candidates = [
        _candidate_payload(
            source="requested_execution",
            posture="promote",
            precedence_rank=20.0,
            confidence=0.75,
            freshness_weight=1.0,
            rationale=str(requested_reason or "requested_execution").strip(),
            snapshot={
                "capability_name": str(capability_name or "").strip(),
                "requested_executor": str(requested_executor or "").strip(),
                "requested_decision": str(requested_decision or "").strip(),
                "requested_status": str(requested_status or "").strip(),
                "safety_mode": str(safety_mode or "").strip(),
            },
            policy_effects_json={
                "target_dispatch_decision": requested_decision,
                "target_status": requested_status,
                "requested_executor": requested_executor,
            },
        )
    ]
    candidates.append(
        _candidate_payload(
            source="execution_readiness",
            posture=execution_readiness_posture(readiness),
            precedence_rank=execution_readiness_precedence(
                readiness,
                blocking_rank=110.0,
                advisory_rank=70.0,
                ready_rank=50.0,
            ),
            confidence=execution_readiness_confidence(readiness),
            freshness_weight=1.0,
            rationale=str(readiness.get("detail") or "execution readiness policy enforced").strip(),
            snapshot=readiness,
            policy_effects_json=execution_readiness_policy_effects(
                readiness=readiness,
                surface="execution",
            ),
        )
    )
    if commitment is not None and commitment_is_active(commitment):
        candidates.append(
            _candidate_payload(
                source="operator_commitment",
                posture="caution" if _commitment_requires_review(commitment) else "promote",
                precedence_rank=100.0,
                confidence=float(getattr(commitment, "confidence", 0.0) or 0.0),
                freshness_weight=1.0,
                rationale=str(getattr(commitment, "reason", "") or "").strip(),
                snapshot=commitment_snapshot(commitment),
                policy_effects_json={
                    "require_operator_confirmation": _commitment_requires_review(commitment),
                    "reason": "operator_commitment_requires_review",
                    "why_policy_prevailed": "Active operator commitment gated execution for this scope.",
                },
            )
        )
    if governance and str(governance.get("status") or "").strip() != "inactive":
        candidates.append(
            _candidate_payload(
                source="execution_truth_governance",
                posture="caution" if _governance_requires_review(governance) else "promote",
                precedence_rank=80.0,
                confidence=float(governance.get("confidence", 0.0) or 0.0),
                freshness_weight=1.0,
                rationale=str(governance.get("governance_reason") or "").strip(),
                snapshot=governance,
                policy_effects_json={
                    "require_operator_confirmation": _governance_requires_review(governance),
                    "reason": "execution_truth_governance_requires_review",
                    "why_policy_prevailed": "Execution-truth governance requested slower execution in this scope.",
                },
            )
        )
    for override in overrides:
        candidates.append(
            _candidate_payload(
                source="operator_override",
                posture="caution" if override.override_type in {"hard_stop", "pause"} else "promote",
                precedence_rank=120.0,
                confidence=1.0,
                freshness_weight=1.0,
                rationale=str(override.reason or "").strip() or f"operator override {override.override_type}",
                snapshot={
                    "override_id": int(override.id),
                    "override_type": override.override_type,
                    "managed_scope": override.managed_scope,
                    "execution_id": override.execution_id,
                    "trace_id": override.trace_id,
                },
                policy_effects_json=_override_policy_effects(override),
            )
        )

    conflict = await _resolve_policy_conflict_profile(
        db=db,
        managed_scope=scope,
        decision_family=CONFLICT_DECISION_FAMILY_EXECUTION,
        proposal_type=str(capability_name or requested_executor or "execution").strip(),
        actor=actor,
        proposal_id=execution_id,
        candidates=candidates,
        metadata_json={
            "source": source,
            "requested_decision": requested_decision,
            "requested_status": requested_status,
            **metadata,
        },
        effect_mode="winner_always",
    )
    effects = conflict.get("policy_effects_json", {}) if isinstance(conflict.get("policy_effects_json", {}), dict) else {}
    final_decision = requested_decision
    final_status = requested_status
    final_reason = str(requested_reason or "").strip()
    final_executor = str(requested_executor or "").strip()
    target_dispatch_decision = str(effects.get("target_dispatch_decision") or "").strip()
    target_status = str(effects.get("target_status") or "").strip()
    if bool(effects.get("hard_stop", False)):
        final_decision = target_dispatch_decision or "blocked"
        final_status = target_status or "blocked"
        final_reason = str(effects.get("reason") or "operator_override_hard_stop").strip()
    elif target_dispatch_decision and target_status:
        final_decision = target_dispatch_decision
        final_status = target_status
        final_reason = str(effects.get("reason") or final_reason or "execution_policy_applied").strip()
    elif bool(effects.get("require_operator_confirmation", False)) and final_status != "blocked":
        final_decision = "requires_confirmation"
        final_status = "pending_confirmation"
        final_reason = str(effects.get("reason") or "execution_policy_review_required").strip()
    redirected = str(effects.get("redirect_executor") or "").strip()
    if redirected:
        final_executor = redirected

    return {
        "managed_scope": scope,
        "dispatch_decision": final_decision,
        "status": final_status,
        "reason": final_reason,
        "requested_executor": final_executor,
        "policy_conflict": conflict,
        "execution_readiness": readiness,
        "readiness_state_bus": readiness_state,
        "operator_commitment": commitment_snapshot(commitment) if commitment is not None else {},
        "execution_truth_governance": governance if isinstance(governance, dict) else {},
        "active_overrides": [
            {
                "override_id": int(row.id),
                "override_type": row.override_type,
                "execution_id": row.execution_id,
                "trace_id": row.trace_id,
            }
            for row in overrides
        ],
    }


async def sync_execution_control_state(
    *,
    db: AsyncSession,
    execution: CapabilityExecution,
    actor: str,
    source: str,
    requested_goal: str,
    intent_key: str,
    intent_type: str,
    context_json: dict | None,
    gate_result: dict | None = None,
) -> dict:
    feedback = execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
    readiness = (
        _json_safe(_json_safe(gate_result.get("execution_readiness", {})))
        if isinstance(gate_result, dict)
        else {}
    )
    managed_scope = infer_managed_scope(
        execution.managed_scope,
        feedback,
        execution.arguments_json,
        context_json,
    )
    execution.managed_scope = managed_scope
    trace_id = str(execution.trace_id or "").strip() or str(feedback.get("trace_id") or "").strip() or new_trace_id()
    execution.trace_id = trace_id
    trace = await ensure_execution_trace(
        db=db,
        trace_id=trace_id,
        managed_scope=managed_scope,
        capability_name=execution.capability_name,
        actor=actor,
        source=source,
        root_execution_id=int(execution.id),
        lifecycle_status="active" if execution.status not in {"failed", "blocked", "succeeded"} else execution.status,
        current_stage=execution.status,
        metadata_json={
            "requested_goal": str(requested_goal or "").strip(),
            "dispatch_decision": execution.dispatch_decision,
            "execution_readiness": readiness if isinstance(readiness, dict) else {},
        },
    )
    intent = await ensure_execution_intent(
        db=db,
        trace_id=trace.trace_id,
        managed_scope=managed_scope,
        intent_key=intent_key,
        intent_type=intent_type,
        requested_goal=requested_goal,
        capability_name=execution.capability_name,
        arguments_json=execution.arguments_json,
        context_json=context_json,
        actor=actor,
        source=source,
        execution_id=int(execution.id),
        lifecycle_status="archived" if execution.status in {"failed", "blocked", "succeeded"} else "active",
    )
    trace.root_intent_id = int(intent.id)
    await db.flush()
    orchestration = await ensure_execution_orchestration(
        db=db,
        trace_id=trace.trace_id,
        intent_id=int(intent.id),
        execution=execution,
        managed_scope=managed_scope,
        actor=actor,
        source=source,
        metadata_json={
            "requested_goal": str(requested_goal or "").strip(),
            "policy_gate_status": execution.status,
        },
    )
    stability = await evaluate_execution_stability(
        db=db,
        managed_scope=managed_scope,
        actor=actor,
        source=source,
        trace_id=trace.trace_id,
        metadata_json={
            "execution_id": execution.id,
            "dispatch_decision": execution.dispatch_decision,
        },
    )
    await append_execution_trace_event(
        db=db,
        trace_id=trace.trace_id,
        execution_id=int(execution.id),
        intent_id=int(intent.id),
        event_type="execution_bound",
        event_stage=execution.status,
        causality_role="effect",
        summary=f"Execution {execution.id} bound for {execution.capability_name}",
        payload_json={
            "dispatch_decision": execution.dispatch_decision,
            "reason": execution.reason,
            "managed_scope": managed_scope,
            "execution_readiness": readiness if isinstance(readiness, dict) else {},
            "gate_result": _json_safe(gate_result if isinstance(gate_result, dict) else {}),
        },
    )
    trace.current_stage = execution.status
    trace.lifecycle_status = execution.status if execution.status in {"failed", "blocked", "succeeded"} else "active"
    trace.metadata_json = {
        **(trace.metadata_json if isinstance(trace.metadata_json, dict) else {}),
        "execution_readiness": readiness if isinstance(readiness, dict) else {},
    }
    trace.causality_graph_json = {
        **(trace.causality_graph_json if isinstance(trace.causality_graph_json, dict) else {}),
        "root_execution_id": execution.id,
        "root_intent_id": intent.id,
        "orchestration_id": orchestration.id,
        "latest_stability_id": stability.id,
    }
    execution.feedback_json = {
        **feedback,
        "trace_id": trace.trace_id,
        "managed_scope": managed_scope,
        "intent_id": int(intent.id),
        "orchestration_id": int(orchestration.id),
        "execution_policy_gate": _json_safe(gate_result if isinstance(gate_result, dict) else {}),
        "execution_readiness": readiness if isinstance(readiness, dict) else {},
        "readiness_state_bus": _json_safe(
            gate_result.get("readiness_state_bus", {}) if isinstance(gate_result, dict) else {}
        ),
        "stability": _json_safe(to_execution_stability_out(stability)),
    }
    await db.flush()
    return {
        "trace_id": trace.trace_id,
        "managed_scope": managed_scope,
        "intent_id": int(intent.id),
        "orchestration": to_execution_task_orchestration_out(orchestration),
        "stability": to_execution_stability_out(stability),
    }