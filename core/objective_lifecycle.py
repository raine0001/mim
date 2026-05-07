from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


SUCCESS_STATES = {"completed", "succeeded", "approved", "passed", "done", "reviewed"}
FAILURE_STATES = {"failed", "blocked", "rejected", "cancelled", "canceled"}
TERMINAL_STATES = SUCCESS_STATES | FAILURE_STATES


def _normalize(value: str | None) -> str:
    return str(value or "").strip().lower()


def _coerce_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def task_execution_tracking_snapshot(
    task: object,
    *,
    has_result: bool = False,
) -> dict[str, object]:
    metadata_json = _coerce_dict(getattr(task, "metadata_json", {}))
    tracking = _coerce_dict(metadata_json.get("execution_tracking"))
    dispatch_artifact = _coerce_dict(getattr(task, "dispatch_artifact_json", {}))
    request_id = str(
        tracking.get("request_id")
        or dispatch_artifact.get("request_id")
        or dispatch_artifact.get("handoff_id")
        or ""
    ).strip()
    task_created = bool(tracking.get("task_created", True))
    task_dispatched = bool(
        tracking.get("task_dispatched", False)
        or _normalize(getattr(task, "dispatch_status", ""))
        in {"queued", "running", "dispatched", "completed", "accepted"}
    )
    execution_started = bool(
        tracking.get("execution_started", False)
        or _normalize(getattr(task, "state", "")) in {"in_progress", "running", "accepted", "dispatched"}
        or has_result
    )
    execution_result = tracking.get("execution_result")
    if execution_result is None and has_result:
        execution_result = "result_recorded"
    execution_trace = str(
        tracking.get("execution_trace")
        or dispatch_artifact.get("task_path")
        or dispatch_artifact.get("status_path")
        or dispatch_artifact.get("trace_id")
        or ""
    ).strip()
    result_artifact = str(
        tracking.get("result_artifact")
        or dispatch_artifact.get("latest_result_path")
        or dispatch_artifact.get("latest_result_artifact")
        or dispatch_artifact.get("latest_broker_result_artifact")
        or dispatch_artifact.get("broker_result_artifact")
        or dispatch_artifact.get("result_artifact")
        or ""
    ).strip()
    return {
        "task_created": task_created,
        "task_dispatched": task_dispatched,
        "execution_started": execution_started,
        "execution_result": execution_result,
        "activity_started_at": str(
            tracking.get("activity_started_at")
            or tracking.get("resumed_at")
            or tracking.get("started_at")
            or ""
        ).strip(),
        "request_id": request_id,
        "execution_trace": execution_trace,
        "result_artifact": result_artifact,
        "has_result_record": bool(has_result),
    }


def task_has_completion_evidence(
    task: object,
    *,
    has_result: bool = False,
) -> bool:
    tracking = task_execution_tracking_snapshot(task, has_result=has_result)
    return bool(
        tracking["task_dispatched"]
        and (tracking["execution_started"] or tracking["execution_result"] is not None)
        and tracking["request_id"]
        and tracking["execution_trace"]
        and tracking["result_artifact"]
    )


def task_execution_state(
    task: object,
    *,
    has_result: bool = False,
) -> str:
    normalized_state = _normalize(getattr(task, "state", ""))
    tracking = task_execution_tracking_snapshot(task, has_result=has_result)
    if normalized_state in FAILURE_STATES:
        return normalized_state
    if normalized_state in SUCCESS_STATES and task_has_completion_evidence(task, has_result=has_result):
        return "completed"
    if tracking["task_dispatched"] and tracking["execution_started"]:
        return "executing"
    if tracking["task_dispatched"]:
        return "dispatched"
    if tracking["task_created"]:
        return "created"
    return "queued"


def derive_task_state_from_result(
    *, test_results: str | None, failures: list[str] | None
) -> str:
    normalized_test = _normalize(test_results)
    has_failures = bool(failures)
    failed_tests = normalized_test in {"fail", "failed", "error", "errored"}
    if has_failures or failed_tests:
        return "failed"
    return "completed"


def derive_task_state_from_review(
    *, decision: str | None, continue_allowed: bool
) -> str:
    normalized = _normalize(decision)
    if normalized in {
        "approved",
        "approve",
        "accepted",
        "accept",
        "pass",
        "passed",
        "succeeded",
        "success",
        "done",
    }:
        return "reviewed"
    if normalized in {"repeat_with_changes", "needs_iteration", "retry", "rework"}:
        return "queued" if continue_allowed else "blocked"
    if normalized in {"failed", "failure", "blocked", "rejected", "closed_no_action"}:
        return "failed"
    return "reviewed"


async def recompute_objective_state(
    db: AsyncSession, objective_id: int | None
) -> str | None:
    from sqlalchemy import select

    from core.models import Objective, Task, TaskResult

    if objective_id is None:
        return None

    objective = await db.get(Objective, objective_id)
    if not objective:
        return None

    tasks = list(
        (
            await db.execute(select(Task).where(Task.objective_id == objective_id))
        )
        .scalars()
        .all()
    )

    if not tasks:
        return objective.state

    result_task_ids = set(
        (
            await db.execute(
                select(TaskResult.task_id).where(
                    TaskResult.task_id.in_([task.id for task in tasks])
                )
            )
        )
        .scalars()
        .all()
    )

    normalized_states = {_normalize(task.state) for task in tasks}
    normalized_readiness = {_normalize(task.readiness) for task in tasks}
    all_tasks_succeeded_with_evidence = all(
        _normalize(task.state) in SUCCESS_STATES
        and task_has_completion_evidence(task, has_result=task.id in result_task_ids)
        for task in tasks
    )

    if normalized_states.issubset(TERMINAL_STATES):
        next_state = (
            "completed"
            if all_tasks_succeeded_with_evidence
            else "blocked"
            if bool(normalized_states & FAILURE_STATES)
            else "in_progress"
        )
    elif normalized_readiness & {"waiting_on_human", "blocked"} and not (
        normalized_readiness & {"ready", "in_progress", "queued", "waiting_on_tod"}
    ):
        next_state = "blocked"
    else:
        next_state = "in_progress"

    execution_snapshots = [
        task_execution_tracking_snapshot(task, has_result=task.id in result_task_ids)
        for task in tasks
    ]
    completed_task_count = sum(
        1
        for task in tasks
        if _normalize(task.state) in SUCCESS_STATES
        and task_has_completion_evidence(task, has_result=task.id in result_task_ids)
    )
    objective_execution_state = (
        "completed"
        if next_state == "completed"
        else "executing"
        if any(snapshot["task_dispatched"] and snapshot["execution_started"] for snapshot in execution_snapshots)
        else "dispatched"
        if any(snapshot["task_dispatched"] for snapshot in execution_snapshots)
        else "created"
        if tasks
        else "queued"
    )
    objective.metadata_json = {
        **_coerce_dict(getattr(objective, "metadata_json", {})),
        "execution_tracking": {
            "task_created": bool(tasks),
            "task_dispatched": any(snapshot["task_dispatched"] for snapshot in execution_snapshots),
            "execution_started": any(snapshot["execution_started"] for snapshot in execution_snapshots),
            "execution_result": (
                f"{completed_task_count}_tasks_completed"
                if completed_task_count
                else None
            ),
            "completed_task_count": completed_task_count,
            "task_count": len(tasks),
            "execution_state": objective_execution_state,
            "completion_requirements": {
                "request_id_required": True,
                "execution_trace_required": True,
                "result_artifact_required": True,
            },
        },
    }

    if objective.state != next_state:
        objective.state = next_state

    return objective.state
