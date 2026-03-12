from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.models import CapabilityExecution, Goal, InputEvent, InputEventResolution
from core.preferences import apply_learning_signal
from core.schemas import OperatorExecutionActionRequest

router = APIRouter(tags=["operator"])


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
        "arguments_json": execution.arguments_json,
        "feedback_json": feedback,
        "replan_required": bool(feedback.get("replan_required", False)),
        "latest_replan_outcome": str(feedback.get("latest_replan_outcome", "")),
        "latest_predictive_signal_id": feedback.get("latest_predictive_signal_id"),
        "event_source": event.source if event else "unknown",
        "resolution_outcome": resolution.outcome if resolution else "unknown",
        "created_at": execution.created_at,
    }


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
    rows = (await db.execute(select(CapabilityExecution).order_by(CapabilityExecution.id.desc()))).scalars().all()

    items: list[dict] = []
    for execution in rows:
        resolution = await db.get(InputEventResolution, execution.resolution_id) if execution.resolution_id else None
        event = await db.get(InputEvent, execution.input_event_id)
        items.append(_to_operator_execution(execution, resolution, event))

    pending = [item for item in items if item["status"] == "pending_confirmation"][:limit]
    blocked = [item for item in items if item["status"] == "blocked"][:limit]
    failed = [item for item in items if item["status"] == "failed"][:limit]
    paused = [item for item in items if item["status"] == "paused"][:limit]
    active = [item for item in items if item["status"] in {"dispatched", "accepted", "running"}][:limit]
    succeeded_recent = [item for item in items if item["status"] == "succeeded"][:limit]

    return {
        "generated_at": datetime.now(timezone.utc),
        "counts": {
            "pending_confirmations": len(pending),
            "blocked": len(blocked),
            "failed": len(failed),
            "paused": len(paused),
            "active": len(active),
            "succeeded_recent": len(succeeded_recent),
        },
        "pending_confirmations": pending,
        "blocked": blocked,
        "failed": failed,
        "paused": paused,
        "active": active,
        "succeeded_recent": succeeded_recent,
    }


@router.get("/executions")
async def list_operator_executions(
    status: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = (await db.execute(select(CapabilityExecution).order_by(CapabilityExecution.id.desc()))).scalars().all()
    requested = {item.strip() for item in status.split(",") if item.strip()}

    result: list[dict] = []
    for execution in rows:
        if requested and execution.status not in requested:
            continue
        resolution = await db.get(InputEventResolution, execution.resolution_id) if execution.resolution_id else None
        event = await db.get(InputEvent, execution.input_event_id)
        result.append(_to_operator_execution(execution, resolution, event))
        if len(result) >= limit:
            break
    return result


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