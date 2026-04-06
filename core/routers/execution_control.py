from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
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
    ExecutionStabilityProfile,
    ExecutionTaskOrchestration,
)
from core.schemas import (
    ExecutionOverrideRequest,
    ExecutionRecoveryAttemptRequest,
    ExecutionRecoveryEvaluateRequest,
    ExecutionRecoveryLearningResetRequest,
    ExecutionRecoveryOutcomeEvaluateRequest,
    ExecutionStabilityEvaluateRequest,
)
from core.stability_monitor import evaluate_execution_stability, to_execution_stability_out
from core.task_orchestrator import to_execution_task_orchestration_out

router = APIRouter()


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