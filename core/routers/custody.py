from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.journal import write_journal
from core.models import Action, Goal, GoalPlan, Objective, StateSnapshot, Task, ValidationResult
from core.schemas import ActionCreate, ActionReplaceCreate, ActionRetryCreate, ActionSkipCreate, GoalCreate, GoalPlanUpsert, GoalResumeCreate

router = APIRouter()


def _serialize_goal(goal: Goal) -> dict:
    return {
        "goal_id": goal.id,
        "objective_id": goal.objective_id,
        "task_id": goal.task_id,
        "goal_type": goal.goal_type,
        "goal_description": goal.goal_description,
        "requested_by": goal.requested_by,
        "priority": goal.priority,
        "status": goal.status,
        "created_at": goal.created_at,
    }


def _serialize_action(action: Action) -> dict:
    return {
        "action_id": action.id,
        "goal_id": action.goal_id,
        "engine": action.engine,
        "action_type": action.action_type,
        "input_ref": action.input_ref,
        "expected_state_delta": action.expected_state_delta,
        "validation_method": action.validation_method,
        "sequence_index": action.sequence_index,
        "depends_on_action_id": action.depends_on_action_id,
        "parent_action_id": action.parent_action_id,
        "retry_of_action_id": action.retry_of_action_id,
        "retry_count": action.retry_count,
        "replaced_action_id": action.replaced_action_id,
        "replacement_action_id": action.replacement_action_id,
        "recovery_classification": action.recovery_classification,
        "chain_event": action.chain_event,
        "started_at": action.started_at,
        "completed_at": action.completed_at,
        "status": action.status,
    }


def _serialize_snapshot(snapshot: StateSnapshot) -> dict:
    return {
        "snapshot_id": snapshot.id,
        "goal_id": snapshot.goal_id,
        "action_id": snapshot.action_id,
        "snapshot_phase": snapshot.snapshot_phase,
        "state_type": snapshot.state_type,
        "state_payload": snapshot.state_payload,
        "captured_at": snapshot.captured_at,
    }


def _serialize_validation(validation: ValidationResult) -> dict:
    return {
        "validation_id": validation.id,
        "goal_id": validation.goal_id,
        "action_id": validation.action_id,
        "validation_method": validation.validation_method,
        "validation_status": validation.validation_status,
        "validation_details": validation.validation_details,
        "validated_at": validation.validated_at,
    }


def _to_num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _calculate_observed_delta(pre_state: dict, post_state: dict) -> dict:
    observed: dict = {}
    keys = set(pre_state.keys()) | set(post_state.keys())
    for key in keys:
        pre_value = pre_state.get(key)
        post_value = post_state.get(key)
        pre_num = _to_num(pre_value)
        post_num = _to_num(post_value)
        if pre_num is not None and post_num is not None:
            observed[key] = round(post_num - pre_num, 6)
        elif pre_value != post_value:
            observed[key] = post_value
    return observed


def _classify_validation(expected_delta: dict, observed_delta: dict, action_status: str) -> tuple[str, dict]:
    normalized_status = action_status.lower()
    if normalized_status == "blocked":
        return "blocked", {"reason": "action_status_blocked"}

    checks: dict[str, dict] = {}
    matched = 0
    total = len(expected_delta)

    for key, expected in expected_delta.items():
        observed = observed_delta.get(key)
        ok = observed == expected
        checks[key] = {
            "expected": expected,
            "observed": observed,
            "match": ok,
        }
        if ok:
            matched += 1

    if total == 0:
        if normalized_status in {"completed", "success"}:
            return "achieved", {"checks": checks, "reason": "no_expected_delta"}
        return "unknown", {"checks": checks, "reason": "no_expected_delta_and_non_success_status"}

    if matched == total:
        status = "achieved"
    elif matched > 0:
        status = "partial"
    else:
        status = "failed"

    details = {
        "checks": checks,
        "matched": matched,
        "total": total,
    }
    return status, details


def _derive_chain_status(actions: list[Action]) -> tuple[str, dict]:
    if not actions:
        return "unknown", {
            "total_steps": 0,
            "completed_steps": 0,
            "failed_steps": 0,
            "blocked_steps": 0,
            "retried_steps": 0,
            "skipped_steps": 0,
            "recovered_steps": 0,
            "manual_intervention_steps": 0,
        }

    completed_steps = sum(1 for action in actions if action.status in {"completed", "success", "achieved"})
    failed_steps = sum(1 for action in actions if action.status == "failed")
    blocked_steps = sum(1 for action in actions if action.status == "blocked")
    retried_steps = sum(1 for action in actions if action.status == "retried")
    skipped_steps = sum(1 for action in actions if action.status == "skipped")
    recovered_steps = sum(1 for action in actions if action.recovery_classification in {"recovered", "recovered_partial", "recovered_failed"})
    manual_intervention_steps = sum(1 for action in actions if action.recovery_classification == "manual_intervention")
    total_steps = len(actions)

    if blocked_steps > 0:
        status = "blocked"
    elif any(action.recovery_classification == "recovered_failed" for action in actions):
        status = "failed"
    elif manual_intervention_steps > 0:
        status = "failed"
    elif failed_steps > 0 and completed_steps > 0:
        status = "partial"
    elif failed_steps > 0 and completed_steps == 0:
        status = "failed"
    elif any(action.recovery_classification == "recovered_partial" for action in actions):
        status = "partial"
    elif recovered_steps > 0 and failed_steps == 0 and blocked_steps == 0:
        status = "recovered"
    elif completed_steps == total_steps:
        status = "achieved"
    elif completed_steps > 0 or skipped_steps > 0 or retried_steps > 0:
        status = "partial"
    else:
        status = "unknown"

    return status, {
        "total_steps": total_steps,
        "completed_steps": completed_steps,
        "failed_steps": failed_steps,
        "blocked_steps": blocked_steps,
        "retried_steps": retried_steps,
        "skipped_steps": skipped_steps,
        "recovered_steps": recovered_steps,
        "manual_intervention_steps": manual_intervention_steps,
    }


async def _validate_goal_graph(db: AsyncSession, goal_id: int) -> None:
    actions = (
        await db.execute(
            select(Action)
            .where(Action.goal_id == goal_id)
            .order_by(Action.sequence_index.asc(), Action.id.asc())
        )
    ).scalars().all()

    seq_seen: set[int] = set()
    for action in actions:
        if action.depends_on_action_id == action.id:
            raise HTTPException(status_code=422, detail=f"self dependency forbidden for action {action.id}")
        if action.sequence_index in seq_seen:
            raise HTTPException(status_code=422, detail=f"duplicate sequence_index: {action.sequence_index}")
        seq_seen.add(action.sequence_index)

    deps = {action.id: action.depends_on_action_id for action in actions if action.depends_on_action_id is not None}

    visited: set[int] = set()
    stack: set[int] = set()

    def dfs(node: int) -> None:
        if node in stack:
            raise HTTPException(status_code=422, detail="circular dependency detected")
        if node in visited:
            return
        stack.add(node)
        dep = deps.get(node)
        if dep is not None:
            dfs(dep)
        stack.remove(node)
        visited.add(node)

    for action_id in deps:
        dfs(action_id)


async def _sync_goal_plan(db: AsyncSession, goal_id: int) -> GoalPlan:
    actions = (
        await db.execute(
            select(Action)
            .where(Action.goal_id == goal_id)
            .order_by(Action.sequence_index.asc(), Action.id.asc())
        )
    ).scalars().all()

    derived_status, _ = _derive_chain_status(actions)
    ordered_action_ids = [action.id for action in actions]

    completed_positions = [idx for idx, action in enumerate(actions) if action.status in {"completed", "success", "achieved"}]
    current_step_index = (max(completed_positions) + 1) if completed_positions else 0
    if current_step_index >= len(ordered_action_ids):
        current_step_index = max(0, len(ordered_action_ids) - 1)

    plan = (
        await db.execute(select(GoalPlan).where(GoalPlan.goal_id == goal_id))
    ).scalar_one_or_none()

    if plan is None:
        plan = GoalPlan(
            goal_id=goal_id,
            ordered_action_ids=ordered_action_ids,
            current_step_index=current_step_index,
            derived_status=derived_status,
        )
        db.add(plan)
    else:
        plan.ordered_action_ids = ordered_action_ids
        plan.current_step_index = current_step_index
        plan.derived_status = derived_status

    return plan


@router.post("/goals")
async def create_goal(payload: GoalCreate, db: AsyncSession = Depends(get_db)) -> dict:
    if payload.objective_id is not None:
        objective = await db.get(Objective, payload.objective_id)
        if not objective:
            raise HTTPException(status_code=404, detail="objective not found")

    if payload.task_id is not None:
        task = await db.get(Task, payload.task_id)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")

    goal = Goal(
        objective_id=payload.objective_id,
        task_id=payload.task_id,
        goal_type=payload.goal_type,
        goal_description=payload.goal_description,
        requested_by=payload.requested_by,
        priority=payload.priority,
        status=payload.status,
    )
    db.add(goal)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=404, detail="referenced objective or task not found")
    await write_journal(
        db,
        actor=payload.requested_by,
        action="create_goal",
        target_type="goal",
        target_id=str(goal.id),
        summary=f"Goal created: {goal.goal_type}",
    )
    await db.commit()
    await db.refresh(goal)
    return _serialize_goal(goal)


@router.get("/goals")
async def list_goals(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Goal).order_by(Goal.id.desc()))).scalars().all()
    return [_serialize_goal(goal) for goal in rows]


@router.get("/goals/{goal_id}")
async def get_goal(goal_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    goal = await db.get(Goal, goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")
    return _serialize_goal(goal)


@router.post("/actions")
async def create_action(payload: ActionCreate, db: AsyncSession = Depends(get_db)) -> dict:
    goal = await db.get(Goal, payload.goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")

    if payload.depends_on_action_id is not None:
        depends_on = await db.get(Action, payload.depends_on_action_id)
        if not depends_on:
            raise HTTPException(status_code=404, detail="depends_on_action not found")
        if depends_on.goal_id != goal.id:
            raise HTTPException(status_code=422, detail="depends_on_action must belong to same goal")

    if payload.parent_action_id is not None:
        parent = await db.get(Action, payload.parent_action_id)
        if not parent:
            raise HTTPException(status_code=404, detail="parent_action not found")
        if parent.goal_id != goal.id:
            raise HTTPException(status_code=422, detail="parent_action must belong to same goal")

    existing_same_sequence = (
        await db.execute(select(Action).where(Action.goal_id == goal.id, Action.sequence_index == payload.sequence_index))
    ).scalar_one_or_none()
    if existing_same_sequence is not None:
        raise HTTPException(status_code=422, detail=f"duplicate sequence_index: {payload.sequence_index}")

    action = Action(
        goal_id=payload.goal_id,
        engine=payload.engine,
        action_type=payload.action_type,
        input_ref=payload.input_ref,
        expected_state_delta=payload.expected_state_delta,
        validation_method=payload.validation_method,
        sequence_index=payload.sequence_index,
        depends_on_action_id=payload.depends_on_action_id,
        parent_action_id=payload.parent_action_id,
        retry_of_action_id=None,
        retry_count=0,
        replaced_action_id=None,
        replacement_action_id=None,
        recovery_classification="",
        chain_event="",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        status=payload.status,
    )
    db.add(action)
    await db.flush()

    pre_snapshot = StateSnapshot(
        goal_id=goal.id,
        action_id=action.id,
        snapshot_phase="pre",
        state_type=payload.pre_state.state_type,
        state_payload=payload.pre_state.state_payload,
    )
    post_snapshot = StateSnapshot(
        goal_id=goal.id,
        action_id=action.id,
        snapshot_phase="post",
        state_type=payload.post_state.state_type,
        state_payload=payload.post_state.state_payload,
    )
    db.add(pre_snapshot)
    db.add(post_snapshot)
    await db.flush()

    observed_delta = _calculate_observed_delta(pre_snapshot.state_payload, post_snapshot.state_payload)
    validation_status, validation_details = _classify_validation(
        action.expected_state_delta,
        observed_delta,
        action.status,
    )

    validation = ValidationResult(
        goal_id=goal.id,
        action_id=action.id,
        validation_method=payload.validation_method,
        validation_status=validation_status,
        validation_details={
            "expected_state_delta": action.expected_state_delta,
            "observed_state_delta": observed_delta,
            **validation_details,
        },
    )
    db.add(validation)

    await _validate_goal_graph(db, goal.id)

    plan = await _sync_goal_plan(db, goal.id)
    goal.status = plan.derived_status

    await write_journal(
        db,
        actor=goal.requested_by,
        action="create_action",
        target_type="action",
        target_id=str(action.id),
        summary=f"Action recorded for goal {goal.id}: {validation_status}",
        metadata_json={
            "goal_id": goal.id,
            "validation_status": validation_status,
        },
    )
    await db.commit()
    await db.refresh(action)
    return _serialize_action(action)


def _next_sequence_index(actions: list[Action]) -> int:
    if not actions:
        return 1
    return max(action.sequence_index for action in actions) + 1


@router.post("/actions/{action_id}/retry")
async def retry_action(action_id: int, payload: ActionRetryCreate, db: AsyncSession = Depends(get_db)) -> dict:
    original = await db.get(Action, action_id)
    if not original:
        raise HTTPException(status_code=404, detail="action not found")
    goal = await db.get(Goal, original.goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")

    if original.status not in {"failed", "blocked"}:
        raise HTTPException(status_code=422, detail="retry requires failed or blocked source action")

    goal_actions = (
        await db.execute(
            select(Action)
            .where(Action.goal_id == goal.id)
            .order_by(Action.sequence_index.asc(), Action.id.asc())
        )
    ).scalars().all()

    prior_retries = [action for action in goal_actions if action.retry_of_action_id == original.id]
    retry = Action(
        goal_id=goal.id,
        engine=payload.engine or original.engine,
        action_type=payload.action_type or original.action_type,
        input_ref=payload.input_ref,
        expected_state_delta=payload.expected_state_delta,
        validation_method=payload.validation_method,
        sequence_index=_next_sequence_index(goal_actions),
        depends_on_action_id=original.depends_on_action_id,
        parent_action_id=original.parent_action_id,
        retry_of_action_id=original.id,
        retry_count=len(prior_retries) + 1,
        replaced_action_id=None,
        replacement_action_id=None,
        recovery_classification=payload.recovery_classification,
        chain_event="retry",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        status=payload.status,
    )
    db.add(retry)
    await db.flush()

    pre_snapshot = StateSnapshot(
        goal_id=goal.id,
        action_id=retry.id,
        snapshot_phase="pre",
        state_type=payload.pre_state.state_type,
        state_payload=payload.pre_state.state_payload,
    )
    post_snapshot = StateSnapshot(
        goal_id=goal.id,
        action_id=retry.id,
        snapshot_phase="post",
        state_type=payload.post_state.state_type,
        state_payload=payload.post_state.state_payload,
    )
    db.add(pre_snapshot)
    db.add(post_snapshot)
    await db.flush()

    observed_delta = _calculate_observed_delta(pre_snapshot.state_payload, post_snapshot.state_payload)
    validation_status, validation_details = _classify_validation(retry.expected_state_delta, observed_delta, retry.status)
    validation = ValidationResult(
        goal_id=goal.id,
        action_id=retry.id,
        validation_method=retry.validation_method,
        validation_status=validation_status,
        validation_details={
            "expected_state_delta": retry.expected_state_delta,
            "observed_state_delta": observed_delta,
            **validation_details,
            "timeline_marker": "retry",
            "retry_of_action_id": original.id,
        },
    )
    db.add(validation)

    await _validate_goal_graph(db, goal.id)
    plan = await _sync_goal_plan(db, goal.id)
    goal.status = plan.derived_status

    await write_journal(
        db,
        actor=goal.requested_by,
        action="retry_action",
        target_type="action",
        target_id=str(retry.id),
        summary=f"Retried action {original.id} for goal {goal.id}",
        metadata_json={"retry_of_action_id": original.id, "retry_count": retry.retry_count},
    )
    await db.commit()
    await db.refresh(retry)
    return _serialize_action(retry)


@router.post("/actions/{action_id}/skip")
async def skip_action(action_id: int, payload: ActionSkipCreate, db: AsyncSession = Depends(get_db)) -> dict:
    action = await db.get(Action, action_id)
    if not action:
        raise HTTPException(status_code=404, detail="action not found")
    goal = await db.get(Goal, action.goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")

    action.status = "skipped"
    action.chain_event = "skip"
    action.completed_at = datetime.now(timezone.utc)

    validation = ValidationResult(
        goal_id=goal.id,
        action_id=action.id,
        validation_method="manual_skip",
        validation_status="skipped",
        validation_details={
            "timeline_marker": "skip",
            "reason": payload.reason,
            "continue_to_next_step": payload.continue_to_next_step,
        },
    )
    db.add(validation)

    plan = await _sync_goal_plan(db, goal.id)
    if payload.continue_to_next_step and plan.current_step_index < len(plan.ordered_action_ids) - 1:
        plan.current_step_index += 1
    goal.status = plan.derived_status

    await write_journal(
        db,
        actor=goal.requested_by,
        action="skip_action",
        target_type="action",
        target_id=str(action.id),
        summary=f"Skipped action {action.id} for goal {goal.id}",
        metadata_json={"reason": payload.reason},
    )
    await db.commit()
    await db.refresh(action)
    return _serialize_action(action)


@router.post("/actions/{action_id}/replace")
async def replace_action(action_id: int, payload: ActionReplaceCreate, db: AsyncSession = Depends(get_db)) -> dict:
    replaced = await db.get(Action, action_id)
    if not replaced:
        raise HTTPException(status_code=404, detail="action not found")
    goal = await db.get(Goal, replaced.goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")

    goal_actions = (
        await db.execute(
            select(Action)
            .where(Action.goal_id == goal.id)
            .order_by(Action.sequence_index.asc(), Action.id.asc())
        )
    ).scalars().all()

    replacement = Action(
        goal_id=goal.id,
        engine=payload.engine,
        action_type=payload.action_type,
        input_ref=payload.input_ref,
        expected_state_delta=payload.expected_state_delta,
        validation_method=payload.validation_method,
        sequence_index=_next_sequence_index(goal_actions),
        depends_on_action_id=replaced.depends_on_action_id,
        parent_action_id=replaced.parent_action_id,
        retry_of_action_id=None,
        retry_count=0,
        replaced_action_id=replaced.id,
        replacement_action_id=None,
        recovery_classification=payload.recovery_classification,
        chain_event="replace",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        status=payload.status,
    )
    db.add(replacement)
    await db.flush()
    replaced.replacement_action_id = replacement.id

    pre_snapshot = StateSnapshot(
        goal_id=goal.id,
        action_id=replacement.id,
        snapshot_phase="pre",
        state_type=payload.pre_state.state_type,
        state_payload=payload.pre_state.state_payload,
    )
    post_snapshot = StateSnapshot(
        goal_id=goal.id,
        action_id=replacement.id,
        snapshot_phase="post",
        state_type=payload.post_state.state_type,
        state_payload=payload.post_state.state_payload,
    )
    db.add(pre_snapshot)
    db.add(post_snapshot)
    await db.flush()

    observed_delta = _calculate_observed_delta(pre_snapshot.state_payload, post_snapshot.state_payload)
    validation_status, validation_details = _classify_validation(
        replacement.expected_state_delta,
        observed_delta,
        replacement.status,
    )
    validation = ValidationResult(
        goal_id=goal.id,
        action_id=replacement.id,
        validation_method=replacement.validation_method,
        validation_status=validation_status,
        validation_details={
            "expected_state_delta": replacement.expected_state_delta,
            "observed_state_delta": observed_delta,
            **validation_details,
            "timeline_marker": "replace",
            "replaced_action_id": replaced.id,
        },
    )
    db.add(validation)

    await _validate_goal_graph(db, goal.id)
    plan = await _sync_goal_plan(db, goal.id)
    goal.status = plan.derived_status

    await write_journal(
        db,
        actor=goal.requested_by,
        action="replace_action",
        target_type="action",
        target_id=str(replacement.id),
        summary=f"Replaced action {replaced.id} for goal {goal.id}",
        metadata_json={"replaced_action_id": replaced.id, "replacement_action_id": replacement.id},
    )
    await db.commit()
    await db.refresh(replacement)
    return _serialize_action(replacement)


@router.post("/goals/{goal_id}/resume")
async def resume_goal(goal_id: int, payload: GoalResumeCreate, db: AsyncSession = Depends(get_db)) -> dict:
    goal = await db.get(Goal, goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")

    goal_actions = (
        await db.execute(
            select(Action)
            .where(Action.goal_id == goal.id)
            .order_by(Action.sequence_index.asc(), Action.id.asc())
        )
    ).scalars().all()
    next_idx = _next_sequence_index(goal_actions)

    resume_action = Action(
        goal_id=goal.id,
        engine="system",
        action_type="resume_chain",
        input_ref="resume://goal",
        expected_state_delta={},
        validation_method="manual_resume",
        sequence_index=next_idx,
        depends_on_action_id=goal_actions[-1].id if goal_actions else None,
        parent_action_id=None,
        retry_of_action_id=None,
        retry_count=0,
        replaced_action_id=None,
        replacement_action_id=None,
        recovery_classification=payload.recovery_classification,
        chain_event="resume",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        status="completed",
    )
    db.add(resume_action)
    await db.flush()

    db.add(
        StateSnapshot(
            goal_id=goal.id,
            action_id=resume_action.id,
            snapshot_phase="pre",
            state_type="resume",
            state_payload={"goal_id": goal.id},
        )
    )
    db.add(
        StateSnapshot(
            goal_id=goal.id,
            action_id=resume_action.id,
            snapshot_phase="post",
            state_type="resume",
            state_payload={"goal_id": goal.id, "resumed": True},
        )
    )
    db.add(
        ValidationResult(
            goal_id=goal.id,
            action_id=resume_action.id,
            validation_method="manual_resume",
            validation_status="achieved",
            validation_details={"timeline_marker": "resume", "recovery_classification": payload.recovery_classification},
        )
    )

    await _validate_goal_graph(db, goal.id)
    plan = await _sync_goal_plan(db, goal.id)
    goal.status = plan.derived_status

    await write_journal(
        db,
        actor=goal.requested_by,
        action="resume_goal",
        target_type="goal",
        target_id=str(goal.id),
        summary=f"Resumed chain for goal {goal.id}",
        metadata_json={"resume_action_id": resume_action.id},
    )
    await db.commit()
    await db.refresh(resume_action)
    return {
        "goal_id": goal.id,
        "resume_action_id": resume_action.id,
        "derived_status": plan.derived_status,
        "current_step_index": plan.current_step_index,
    }


@router.post("/goals/{goal_id}/plan")
async def upsert_goal_plan(goal_id: int, payload: GoalPlanUpsert, db: AsyncSession = Depends(get_db)) -> dict:
    goal = await db.get(Goal, goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")

    existing_actions = (
        await db.execute(
            select(Action)
            .where(Action.goal_id == goal_id)
            .order_by(Action.sequence_index.asc(), Action.id.asc())
        )
    ).scalars().all()
    existing_ids = {action.id for action in existing_actions}

    if payload.ordered_action_ids:
        ordered_ids = payload.ordered_action_ids
        for action_id in ordered_ids:
            if action_id not in existing_ids:
                raise HTTPException(status_code=404, detail=f"action not found in goal: {action_id}")

        for idx, action_id in enumerate(ordered_ids, start=1):
            action = next(action for action in existing_actions if action.id == action_id)
            action.sequence_index = idx
    else:
        ordered_ids = [action.id for action in existing_actions]

    plan = (
        await db.execute(select(GoalPlan).where(GoalPlan.goal_id == goal_id))
    ).scalar_one_or_none()

    chain_status, _ = _derive_chain_status(existing_actions)

    current_step_index = payload.current_step_index
    if ordered_ids:
        current_step_index = min(current_step_index, len(ordered_ids) - 1)
    else:
        current_step_index = 0

    if plan is None:
        plan = GoalPlan(
            goal_id=goal_id,
            ordered_action_ids=ordered_ids,
            current_step_index=current_step_index,
            derived_status=chain_status,
        )
        db.add(plan)
    else:
        plan.ordered_action_ids = ordered_ids
        plan.current_step_index = current_step_index
        plan.derived_status = chain_status

    goal.status = chain_status

    await write_journal(
        db,
        actor=goal.requested_by,
        action="upsert_goal_plan",
        target_type="goal_plan",
        target_id=str(goal_id),
        summary=f"Goal plan updated for goal {goal_id}",
        metadata_json={
            "ordered_action_ids": ordered_ids,
            "current_step_index": current_step_index,
            "derived_status": chain_status,
        },
    )

    await db.commit()
    await db.refresh(plan)

    return {
        "goal_id": goal_id,
        "ordered_action_ids": plan.ordered_action_ids,
        "current_step_index": plan.current_step_index,
        "derived_status": plan.derived_status,
    }


@router.get("/goals/{goal_id}/plan")
async def get_goal_plan(goal_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    goal = await db.get(Goal, goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")

    plan = (
        await db.execute(select(GoalPlan).where(GoalPlan.goal_id == goal_id))
    ).scalar_one_or_none()

    actions = (
        await db.execute(
            select(Action)
            .where(Action.goal_id == goal_id)
            .order_by(Action.sequence_index.asc(), Action.id.asc())
        )
    ).scalars().all()

    if plan is None:
        plan = await _sync_goal_plan(db, goal_id)
        goal.status = plan.derived_status
        await db.commit()
        await db.refresh(plan)

    return {
        "goal_id": goal_id,
        "ordered_action_ids": plan.ordered_action_ids,
        "current_step_index": plan.current_step_index,
        "derived_status": plan.derived_status,
        "actions": [_serialize_action(action) for action in actions],
    }


@router.get("/goals/{goal_id}/timeline")
async def get_goal_timeline(goal_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    goal = await db.get(Goal, goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")

    actions = (
        await db.execute(
            select(Action)
            .where(Action.goal_id == goal_id)
            .order_by(Action.sequence_index.asc(), Action.id.asc())
        )
    ).scalars().all()

    action_ids = [action.id for action in actions]
    snapshots: list[StateSnapshot] = []
    validations: list[ValidationResult] = []

    if action_ids:
        snapshots = (
            await db.execute(
                select(StateSnapshot)
                .where(StateSnapshot.action_id.in_(action_ids))
                .order_by(StateSnapshot.action_id.asc(), StateSnapshot.id.asc())
            )
        ).scalars().all()
        validations = (
            await db.execute(
                select(ValidationResult)
                .where(ValidationResult.action_id.in_(action_ids))
                .order_by(ValidationResult.action_id.asc(), ValidationResult.id.asc())
            )
        ).scalars().all()

    snapshots_by_action: dict[int, list[StateSnapshot]] = {}
    for snapshot in snapshots:
        snapshots_by_action.setdefault(snapshot.action_id, []).append(snapshot)

    validations_by_action: dict[int, list[ValidationResult]] = {}
    for validation in validations:
        validations_by_action.setdefault(validation.action_id, []).append(validation)

    items = []
    for action in actions:
        items.append(
            {
                "action": _serialize_action(action),
                "snapshots": [_serialize_snapshot(snapshot) for snapshot in snapshots_by_action.get(action.id, [])],
                "validations": [_serialize_validation(validation) for validation in validations_by_action.get(action.id, [])],
            }
        )

    return {
        "goal": _serialize_goal(goal),
        "timeline": items,
    }


@router.get("/goals/{goal_id}/status")
async def get_goal_status(goal_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    goal = await db.get(Goal, goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")

    actions = (
        await db.execute(
            select(Action)
            .where(Action.goal_id == goal_id)
            .order_by(Action.sequence_index.asc(), Action.id.asc())
        )
    ).scalars().all()

    derived_status, stats = _derive_chain_status(actions)
    goal.status = derived_status

    plan = (
        await db.execute(select(GoalPlan).where(GoalPlan.goal_id == goal_id))
    ).scalar_one_or_none()
    if plan is None:
        plan = GoalPlan(
            goal_id=goal_id,
            ordered_action_ids=[action.id for action in actions],
            current_step_index=0,
            derived_status=derived_status,
        )
        db.add(plan)
    else:
        plan.derived_status = derived_status

    await db.commit()

    return {
        "goal_id": goal_id,
        "derived_status": derived_status,
        **stats,
    }


@router.get("/actions/{action_id}")
async def get_action(action_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    action = await db.get(Action, action_id)
    if not action:
        raise HTTPException(status_code=404, detail="action not found")

    snapshots = (
        await db.execute(
            select(StateSnapshot)
            .where(StateSnapshot.action_id == action_id)
            .order_by(StateSnapshot.id.asc())
        )
    ).scalars().all()

    validations = (
        await db.execute(
            select(ValidationResult)
            .where(ValidationResult.action_id == action_id)
            .order_by(ValidationResult.id.asc())
        )
    ).scalars().all()

    return {
        "action": _serialize_action(action),
        "snapshots": [_serialize_snapshot(snapshot) for snapshot in snapshots],
        "validations": [_serialize_validation(validation) for validation in validations],
    }


@router.get("/goals/{goal_id}/custody")
async def get_goal_custody(goal_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    goal = await db.get(Goal, goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="goal not found")

    actions = (
        await db.execute(
            select(Action)
            .where(Action.goal_id == goal_id)
            .order_by(Action.sequence_index.asc(), Action.id.asc())
        )
    ).scalars().all()

    snapshots = (
        await db.execute(
            select(StateSnapshot)
            .where(StateSnapshot.goal_id == goal_id)
            .order_by(StateSnapshot.id.asc())
        )
    ).scalars().all()

    validations = (
        await db.execute(
            select(ValidationResult)
            .where(ValidationResult.goal_id == goal_id)
            .order_by(ValidationResult.id.asc())
        )
    ).scalars().all()

    return {
        "goal": _serialize_goal(goal),
        "actions": [_serialize_action(action) for action in actions],
        "snapshots": [_serialize_snapshot(snapshot) for snapshot in snapshots],
        "validations": [_serialize_validation(validation) for validation in validations],
    }


@router.get("/tasks/{task_id}/custody")
async def get_task_custody(task_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    goals = (
        await db.execute(select(Goal).where(Goal.task_id == task_id).order_by(Goal.id.asc()))
    ).scalars().all()

    goal_ids = [goal.id for goal in goals]
    actions: list[Action] = []
    snapshots: list[StateSnapshot] = []
    validations: list[ValidationResult] = []

    if goal_ids:
        actions = (
            await db.execute(
                select(Action)
                .where(Action.goal_id.in_(goal_ids))
                .order_by(Action.goal_id.asc(), Action.sequence_index.asc(), Action.id.asc())
            )
        ).scalars().all()
        snapshots = (
            await db.execute(
                select(StateSnapshot)
                .where(StateSnapshot.goal_id.in_(goal_ids))
                .order_by(StateSnapshot.id.asc())
            )
        ).scalars().all()
        validations = (
            await db.execute(
                select(ValidationResult)
                .where(ValidationResult.goal_id.in_(goal_ids))
                .order_by(ValidationResult.id.asc())
            )
        ).scalars().all()

    return {
        "task_id": task_id,
        "goals": [_serialize_goal(goal) for goal in goals],
        "actions": [_serialize_action(action) for action in actions],
        "snapshots": [_serialize_snapshot(snapshot) for snapshot in snapshots],
        "validations": [_serialize_validation(validation) for validation in validations],
    }
