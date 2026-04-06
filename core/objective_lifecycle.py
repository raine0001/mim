from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


SUCCESS_STATES = {"completed", "succeeded", "approved", "passed", "done", "reviewed"}
FAILURE_STATES = {"failed", "blocked", "rejected", "cancelled", "canceled"}
TERMINAL_STATES = SUCCESS_STATES | FAILURE_STATES


def _normalize(value: str | None) -> str:
    return str(value or "").strip().lower()


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
        return "succeeded"
    if normalized in {"repeat_with_changes", "needs_iteration", "retry", "rework"}:
        return "queued" if continue_allowed else "blocked"
    if normalized in {"failed", "failure", "blocked", "rejected", "closed_no_action"}:
        return "failed"
    return "reviewed"


async def recompute_objective_state(
    db: AsyncSession, objective_id: int | None
) -> str | None:
    from sqlalchemy import select

    from core.models import Objective, Task

    if objective_id is None:
        return None

    objective = await db.get(Objective, objective_id)
    if not objective:
        return None

    task_states = (
        (await db.execute(select(Task.state).where(Task.objective_id == objective_id)))
        .scalars()
        .all()
    )

    if not task_states:
        return objective.state

    normalized_states = {_normalize(state) for state in task_states}

    if normalized_states.issubset(TERMINAL_STATES):
        next_state = (
            "blocked" if bool(normalized_states & FAILURE_STATES) else "completed"
        )
    else:
        next_state = "in_progress"

    if objective.state != next_state:
        objective.state = next_state

    return objective.state
