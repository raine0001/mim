from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.execution_truth_service import derive_execution_truth_signals
from core.models import (
    CapabilityExecution,
    ConstraintEvaluation,
    InputEvent,
    MemoryEntry,
    WorkspaceAutonomyBoundaryProfile,
    WorkspaceEnvironmentStrategy,
    WorkspaceHorizonPlan,
    WorkspaceImprovementProposal,
    WorkspaceInquiryQuestion,
    WorkspacePerceptionSource,
    WorkspaceProposal,
    WorkspaceStewardshipCycle,
    WorkspaceStewardshipState,
    WorkspaceStrategyGoal,
)

QUESTION_STATUSES = {"open", "answered", "dismissed", "expired"}


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _run_id(metadata_json: dict) -> str:
    if not isinstance(metadata_json, dict):
        return ""
    return str(metadata_json.get("run_id", "")).strip()


def _match_run_id(data: dict, run_id: str) -> bool:
    if not run_id:
        return True
    if not isinstance(data, dict):
        return False
    return str(data.get("run_id", "")).strip() == run_id


def _extract_warning_keys(row: ConstraintEvaluation) -> list[str]:
    warnings = row.warnings_json if isinstance(row.warnings_json, list) else []
    keys: list[str] = []
    for item in warnings:
        if not isinstance(item, dict):
            continue
        key = str(item.get("constraint", "")).strip()
        if key:
            keys.append(key)
    return keys


def _constraint_run_metadata(row: ConstraintEvaluation) -> dict:
    explanation = row.explanation_json if isinstance(row.explanation_json, dict) else {}
    metadata = explanation.get("metadata_json", {})
    return metadata if isinstance(metadata, dict) else {}


def _metadata_json(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _merge_unique_strings(existing: object, additions: object) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for collection in [existing, additions]:
        if not isinstance(collection, list):
            continue
        for item in collection:
            value = str(item or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            merged.append(value)
    return merged


def _execution_run_id(
    *, row: CapabilityExecution, input_event: InputEvent | None
) -> str:
    feedback = row.feedback_json if isinstance(row.feedback_json, dict) else {}
    feedback_run_id = str(feedback.get("run_id", "")).strip()
    if feedback_run_id:
        return feedback_run_id
    if input_event and isinstance(input_event.metadata_json, dict):
        return str(input_event.metadata_json.get("run_id", "")).strip()
    return ""


async def _recent_stewardship_cycles(
    *,
    since: datetime,
    run_id: str,
    db: AsyncSession,
) -> list[WorkspaceStewardshipCycle]:
    rows = (
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
    if not run_id:
        return rows
    return [
        item
        for item in rows
        if _match_run_id(_metadata_json(item.metadata_json), run_id)
    ]


async def _get_stewardship_state(
    *, stewardship_id: int, db: AsyncSession
) -> WorkspaceStewardshipState | None:
    if stewardship_id <= 0:
        return None
    return (
        (
            await db.execute(
                select(WorkspaceStewardshipState)
                .where(WorkspaceStewardshipState.id == stewardship_id)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _existing_open_question(
    *, dedupe_key: str, db: AsyncSession
) -> WorkspaceInquiryQuestion | None:
    return (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion)
                .where(WorkspaceInquiryQuestion.dedupe_key == dedupe_key)
                .where(WorkspaceInquiryQuestion.status == "open")
                .order_by(WorkspaceInquiryQuestion.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _latest_horizon_plan(
    *, run_id: str, db: AsyncSession
) -> WorkspaceHorizonPlan | None:
    rows = (
        (
            await db.execute(
                select(WorkspaceHorizonPlan)
                .order_by(WorkspaceHorizonPlan.id.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    if not run_id:
        return rows[0] if rows else None
    for row in rows:
        if _match_run_id(
            row.metadata_json if isinstance(row.metadata_json, dict) else {}, run_id
        ):
            return row
    return None


def _question_payload(row: WorkspaceInquiryQuestion) -> dict:
    return {
        "question_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "status": row.status,
        "trigger_type": row.trigger_type,
        "uncertainty_type": row.uncertainty_type,
        "originating_goal_id": int(row.origin_strategy_goal_id)
        if row.origin_strategy_goal_id is not None
        else None,
        "originating_strategy_id": int(row.origin_strategy_id)
        if row.origin_strategy_id is not None
        else None,
        "originating_plan_id": int(row.origin_plan_id)
        if row.origin_plan_id is not None
        else None,
        "why_answer_matters": row.why_answer_matters,
        "waiting_decision": row.waiting_decision,
        "no_answer_behavior": row.no_answer_behavior,
        "candidate_answer_paths": row.candidate_answer_paths_json
        if isinstance(row.candidate_answer_paths_json, list)
        else [],
        "urgency": row.urgency,
        "priority": row.priority,
        "safe_default_if_unanswered": row.safe_default_if_unanswered,
        "trigger_evidence": row.trigger_evidence_json
        if isinstance(row.trigger_evidence_json, dict)
        else {},
        "selected_path_id": row.selected_path_id,
        "answer_json": row.answer_json if isinstance(row.answer_json, dict) else {},
        "applied_effect_json": row.applied_effect_json
        if isinstance(row.applied_effect_json, dict)
        else {},
        "answered_by": row.answered_by,
        "answered_at": row.answered_at,
        "metadata_json": row.metadata_json
        if isinstance(row.metadata_json, dict)
        else {},
        "created_at": row.created_at,
    }


async def generate_inquiry_questions(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    max_questions: int,
    min_soft_friction_count: int,
    metadata_json: dict,
    db: AsyncSession,
) -> list[WorkspaceInquiryQuestion]:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))
    run_id = _run_id(metadata_json)

    strategy_goals = (
        (
            await db.execute(
                select(WorkspaceStrategyGoal)
                .where(WorkspaceStrategyGoal.created_at >= since)
                .order_by(WorkspaceStrategyGoal.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    strategies = (
        (
            await db.execute(
                select(WorkspaceEnvironmentStrategy)
                .where(WorkspaceEnvironmentStrategy.current_status == "active")
                .order_by(
                    WorkspaceEnvironmentStrategy.influence_weight.desc(),
                    WorkspaceEnvironmentStrategy.id.desc(),
                )
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    plan = await _latest_horizon_plan(run_id=run_id, db=db)

    if run_id:
        strategy_goals = [
            item
            for item in strategy_goals
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]
        strategies = [
            item
            for item in strategies
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]

    constraint_rows = (
        (
            await db.execute(
                select(ConstraintEvaluation)
                .where(ConstraintEvaluation.created_at >= since)
                .order_by(ConstraintEvaluation.id.desc())
                .limit(2000)
            )
        )
        .scalars()
        .all()
    )
    if run_id:
        constraint_rows = [
            item
            for item in constraint_rows
            if _match_run_id(_constraint_run_metadata(item), run_id)
        ]

    input_rows = (
        (
            await db.execute(
                select(InputEvent)
                .where(InputEvent.created_at >= since)
                .order_by(InputEvent.id.desc())
                .limit(1500)
            )
        )
        .scalars()
        .all()
    )
    external_memory = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.created_at >= since)
                .order_by(MemoryEntry.id.desc())
                .limit(1500)
            )
        )
        .scalars()
        .all()
    )
    if run_id:
        input_rows = [
            item
            for item in input_rows
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]
        external_memory = [
            item
            for item in external_memory
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]
    external_memory = [
        item
        for item in external_memory
        if str(item.memory_class or "").lower().startswith("external")
    ]

    source_rows = (
        (
            await db.execute(
                select(WorkspacePerceptionSource)
                .order_by(WorkspacePerceptionSource.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    stewardship_cycles = await _recent_stewardship_cycles(
        since=since,
        run_id=run_id,
        db=db,
    )
    input_rows_by_id = {int(item.id): item for item in input_rows}

    execution_rows = (
        (
            await db.execute(
                select(CapabilityExecution)
                .where(CapabilityExecution.created_at >= since)
                .order_by(CapabilityExecution.id.desc())
                .limit(300)
            )
        )
        .scalars()
        .all()
    )
    execution_rows = [
        item
        for item in execution_rows
        if isinstance(item.execution_truth_json, dict)
        and str(item.execution_truth_json.get("contract", "")).strip()
        == "execution_truth_v1"
    ]
    if run_id:
        execution_rows = [
            item
            for item in execution_rows
            if _execution_run_id(
                row=item,
                input_event=input_rows_by_id.get(int(item.input_event_id or 0)),
            )
            == run_id
        ]
    if run_id:
        source_rows = [
            item
            for item in source_rows
            if _match_run_id(
                item.metadata_json if isinstance(item.metadata_json, dict) else {},
                run_id,
            )
        ]

    max_count = max(1, min(100, int(max_questions)))
    min_friction = max(2, int(min_soft_friction_count))

    created: list[WorkspaceInquiryQuestion] = []

    low_confidence_warnings = [
        item
        for item in constraint_rows
        if "target_confidence_threshold" in _extract_warning_keys(item)
    ]
    if (
        low_confidence_warnings
        and len(low_confidence_warnings) >= min_friction
        and (strategies or plan)
    ):
        origin_strategy = strategies[0] if strategies else None
        origin_goal = strategy_goals[0] if strategy_goals else None
        candidate_paths = [
            {
                "path_id": "shift_strategy_and_unblock",
                "label": "Bias strategy toward reobserve and continue with updated confidence guardrails",
                "effect_type": "shift_strategy_and_unblock",
                "params": {
                    "influence_delta": 0.15,
                },
            },
            {
                "path_id": "trigger_rescan",
                "label": "Trigger immediate adjacent-zone reobserve before next action",
                "effect_type": "trigger_rescan",
                "params": {"proposal_type": "rescan_zone"},
            },
            {
                "path_id": "hold_manual_confirmation",
                "label": "Hold and require manual confirmation until confidence improves",
                "effect_type": "no_action",
                "params": {},
            },
        ]
        dedupe_key = (
            f"target_confidence_too_low:goal:{int(origin_goal.id) if origin_goal else 0}:"
            f"strategy:{int(origin_strategy.id) if origin_strategy else 0}:plan:{int(plan.id) if plan else 0}"
        )
        existing = await _existing_open_question(dedupe_key=dedupe_key, db=db)
        if existing:
            created.append(existing)
        else:
            row = WorkspaceInquiryQuestion(
                source=source,
                actor=actor,
                status="open",
                dedupe_key=dedupe_key,
                trigger_type="target_confidence_too_low",
                uncertainty_type="perception_confidence",
                origin_strategy_goal_id=int(origin_goal.id) if origin_goal else None,
                origin_strategy_id=int(origin_strategy.id) if origin_strategy else None,
                origin_plan_id=int(plan.id) if plan else None,
                why_answer_matters="Low-confidence target evidence can invalidate the current strategy ordering and action sequence.",
                waiting_decision="Whether to continue current plan sequencing or reobserve before execution.",
                no_answer_behavior="System keeps manual confirmation gating and avoids autonomous progression.",
                candidate_answer_paths_json=candidate_paths,
                urgency="high",
                priority="high",
                safe_default_if_unanswered="hold_manual_confirmation",
                trigger_evidence_json={
                    "warning_count": len(low_confidence_warnings),
                    "sample_evaluation_ids": [
                        int(item.id) for item in low_confidence_warnings[:10]
                    ],
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective62": True,
                },
            )
            db.add(row)
            created.append(row)

    if len(created) < max_count:
        communication_count = len(input_rows)
        external_count = len(external_memory)
        if communication_count >= 2 and external_count >= 1:
            origin_goal = strategy_goals[0] if strategy_goals else None
            origin_strategy = strategies[0] if strategies else None
            candidate_paths = [
                {
                    "path_id": "prioritize_workspace_stability",
                    "label": "Prioritize workspace-state stability checks before external-context actions",
                    "effect_type": "shift_strategy",
                    "params": {"influence_delta": 0.1},
                },
                {
                    "path_id": "trigger_context_rescan",
                    "label": "Gather additional perception evidence to resolve domain disagreement",
                    "effect_type": "trigger_rescan",
                    "params": {"proposal_type": "monitor_search_adjacent_zone"},
                },
                {
                    "path_id": "defer_external_context",
                    "label": "Defer external-context branch and keep current safe prioritization",
                    "effect_type": "no_action",
                    "params": {},
                },
            ]
            dedupe_key = (
                f"conflicting_domain_evidence:goal:{int(origin_goal.id) if origin_goal else 0}:"
                f"strategy:{int(origin_strategy.id) if origin_strategy else 0}:run:{run_id or 'global'}"
            )
            existing = await _existing_open_question(dedupe_key=dedupe_key, db=db)
            if existing:
                created.append(existing)
            else:
                row = WorkspaceInquiryQuestion(
                    source=source,
                    actor=actor,
                    status="open",
                    dedupe_key=dedupe_key,
                    trigger_type="conflicting_domain_evidence",
                    uncertainty_type="cross_domain_conflict",
                    origin_strategy_goal_id=int(origin_goal.id)
                    if origin_goal
                    else None,
                    origin_strategy_id=int(origin_strategy.id)
                    if origin_strategy
                    else None,
                    origin_plan_id=int(plan.id) if plan else None,
                    why_answer_matters="Communication and external context signals suggest different priorities for the same planning window.",
                    waiting_decision="Which domain should dominate near-term strategy ranking and action sequencing.",
                    no_answer_behavior="Default to workspace stability and operator confirmation over aggressive reprioritization.",
                    candidate_answer_paths_json=candidate_paths,
                    urgency="medium",
                    priority="high",
                    safe_default_if_unanswered="defer_external_context",
                    trigger_evidence_json={
                        "communication_event_count": communication_count,
                        "external_memory_count": external_count,
                        "sample_input_event_ids": [
                            int(item.id) for item in input_rows[:10]
                        ],
                        "sample_external_memory_ids": [
                            int(item.id) for item in external_memory[:10]
                        ],
                    },
                    metadata_json={
                        **(metadata_json if isinstance(metadata_json, dict) else {}),
                        "objective62": True,
                    },
                )
                db.add(row)
                created.append(row)

    if len(created) < max_count and strategies:
        blocked = [
            item
            for item in strategies
            if not (
                item.influenced_plan_ids_json
                if isinstance(item.influenced_plan_ids_json, list)
                else []
            )
        ]
        if blocked:
            strategy = blocked[0]
            candidate_paths = [
                {
                    "path_id": "request_scope_rescan",
                    "label": "Request targeted rescan for blocked strategy scope",
                    "effect_type": "trigger_rescan",
                    "params": {
                        "proposal_type": "rescan_zone",
                        "related_zone": strategy.target_scope,
                    },
                },
                {
                    "path_id": "lower_strategy_weight",
                    "label": "Temporarily lower blocked strategy influence",
                    "effect_type": "shift_strategy",
                    "params": {"influence_delta": -0.12},
                },
            ]
            dedupe_key = (
                f"strategy_blocked_by_missing_information:strategy:{int(strategy.id)}"
            )
            existing = await _existing_open_question(dedupe_key=dedupe_key, db=db)
            if existing:
                created.append(existing)
            else:
                row = WorkspaceInquiryQuestion(
                    source=source,
                    actor=actor,
                    status="open",
                    dedupe_key=dedupe_key,
                    trigger_type="strategy_blocked_by_missing_information",
                    uncertainty_type="strategy_blocked",
                    origin_strategy_goal_id=None,
                    origin_strategy_id=int(strategy.id),
                    origin_plan_id=int(plan.id) if plan else None,
                    why_answer_matters="Active strategy has not influenced planning outcomes and appears blocked by missing information.",
                    waiting_decision="Whether to gather missing evidence or down-rank the blocked strategy.",
                    no_answer_behavior="Strategy remains active but no autonomous ranking boost is applied.",
                    candidate_answer_paths_json=candidate_paths,
                    urgency="medium",
                    priority="normal",
                    safe_default_if_unanswered="lower_strategy_weight",
                    trigger_evidence_json={
                        "strategy_id": int(strategy.id),
                        "strategy_type": strategy.strategy_type,
                        "target_scope": strategy.target_scope,
                    },
                    metadata_json={
                        **(metadata_json if isinstance(metadata_json, dict) else {}),
                        "objective62": True,
                    },
                )
                db.add(row)
                created.append(row)

    if len(created) < max_count and stewardship_cycles:
        for cycle in stewardship_cycles:
            cycle_meta = _metadata_json(cycle.metadata_json)
            assessment = (
                cycle_meta.get("assessment", {})
                if isinstance(cycle_meta.get("assessment", {}), dict)
                else {}
            )
            post = (
                assessment.get("post", {})
                if isinstance(assessment.get("post", {}), dict)
                else {}
            )
            system_metrics = (
                post.get("system_metrics", {})
                if isinstance(post.get("system_metrics", {}), dict)
                else {}
            )
            execution_truth_summary = (
                post.get("execution_truth_summary", {})
                if isinstance(post.get("execution_truth_summary", {}), dict)
                else {}
            )
            inquiry_candidates = (
                post.get("inquiry_candidates", [])
                if isinstance(post.get("inquiry_candidates", []), list)
                else []
            )
            degraded_signals = (
                post.get("deviation_signals", [])
                if isinstance(post.get("deviation_signals", []), list)
                else []
            )
            key_objects = (
                post.get("scope_metrics", {}).get("key_objects", [])
                if isinstance(post.get("scope_metrics", {}), dict)
                else []
            )
            if not inquiry_candidates and not degraded_signals:
                continue

            managed_scope = (
                str(cycle_meta.get("managed_scope", "global")).strip() or "global"
            )
            stewardship_id = int(cycle.stewardship_id or 0)
            missing_key_objects = [
                str(item.get("object_name", "")).strip()
                for item in key_objects
                if isinstance(item, dict)
                and not bool(item.get("is_known", False))
                and str(item.get("object_name", "")).strip()
            ]
            candidate_paths = [
                {
                    "path_id": "stabilize_scope_now",
                    "label": "Trigger targeted rescan for the degraded stewardship scope",
                    "effect_type": "trigger_rescan",
                    "params": {
                        "proposal_type": "rescan_zone",
                        "related_zone": managed_scope,
                    },
                },
                {
                    "path_id": "tighten_scope_tracking",
                    "label": "Tighten stewardship tracking thresholds for this scope",
                    "effect_type": "update_stewardship_target",
                    "params": {
                        "stewardship_id": stewardship_id,
                        "zone_freshness_seconds": 300,
                        "max_system_drift_rate": 0.2,
                        "proactive_drift_monitoring": True,
                        "add_key_objects": missing_key_objects,
                    },
                },
                {
                    "path_id": "request_stewardship_improvement",
                    "label": "Create bounded improvement review for stewardship policy",
                    "effect_type": "create_proposal",
                    "params": {
                        "proposal_type": "capability_workflow_improvement",
                        "affected_component": "environment_stewardship",
                    },
                },
                {
                    "path_id": "keep_monitoring",
                    "label": "Keep monitoring and do not mutate stewardship policy yet",
                    "effect_type": "no_action",
                    "params": {},
                },
            ]
            dedupe_key = f"stewardship_persistent_degradation:stewardship:{stewardship_id}:scope:{managed_scope}:run:{run_id or 'global'}"
            existing = await _existing_open_question(dedupe_key=dedupe_key, db=db)
            if existing:
                created.append(existing)
                break

            row = WorkspaceInquiryQuestion(
                source=source,
                actor=actor,
                status="open",
                dedupe_key=dedupe_key,
                trigger_type="stewardship_persistent_degradation",
                uncertainty_type="environment_stability",
                origin_strategy_goal_id=None,
                origin_strategy_id=None,
                origin_plan_id=None,
                why_answer_matters="Stewardship is repeatedly detecting degraded environment state, so the maintenance loop may need stronger tracking or a different corrective path.",
                waiting_decision="Whether to immediately stabilize the degraded scope, tighten stewardship tracking, or keep monitoring without policy changes.",
                no_answer_behavior="Stewardship keeps monitoring and uses conservative corrective behavior only.",
                candidate_answer_paths_json=candidate_paths,
                urgency="high",
                priority="high",
                safe_default_if_unanswered="keep_monitoring",
                trigger_evidence_json={
                    "stewardship_id": stewardship_id,
                    "cycle_id": int(cycle.id),
                    "managed_scope": managed_scope,
                    "system_metrics": system_metrics,
                    "execution_truth_signal_count": int(
                        execution_truth_summary.get("signal_count", 0) or 0
                    ),
                    "execution_truth_signal_types": execution_truth_summary.get(
                        "signal_types", []
                    ),
                    "inquiry_candidates": inquiry_candidates,
                    "degraded_signal_count": len(degraded_signals),
                    "missing_key_objects": missing_key_objects,
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective62": True,
                    "objective60_source": True,
                    "objective80_execution_truth": bool(
                        execution_truth_summary.get("signal_count", 0)
                    ),
                },
            )
            db.add(row)
            created.append(row)
            break

    if len(created) < max_count and execution_rows:
        for execution in execution_rows:
            truth = (
                execution.execution_truth_json
                if isinstance(execution.execution_truth_json, dict)
                else {}
            )
            signal_rows = derive_execution_truth_signals(truth)
            if not signal_rows:
                continue

            capability_name = (
                str(execution.capability_name or "workspace capability").strip()
                or "workspace capability"
            )
            signal_types = [
                str(item.get("signal_type", "")).strip()
                for item in signal_rows
                if isinstance(item, dict) and str(item.get("signal_type", "")).strip()
            ]
            if not signal_types:
                continue

            candidate_paths = [
                {
                    "path_id": "request_execution_truth_review",
                    "label": "Create a bounded improvement review for the execution-truth deviation pattern",
                    "effect_type": "create_proposal",
                    "params": {
                        "proposal_type": "capability_workflow_improvement",
                        "affected_component": "execution_truth_bridge",
                    },
                },
                {
                    "path_id": "trigger_execution_rescan",
                    "label": "Trigger a bounded rescan or retry-observation before trusting this runtime pattern",
                    "effect_type": "trigger_rescan",
                    "params": {
                        "proposal_type": "rescan_zone",
                        "related_zone": capability_name,
                    },
                },
                {
                    "path_id": "keep_monitoring_execution_truth",
                    "label": "Keep monitoring execution truth without changing policy yet",
                    "effect_type": "no_action",
                    "params": {},
                },
            ]
            dedupe_key = f"execution_truth_runtime_mismatch:execution:{int(execution.id)}:run:{run_id or 'global'}"
            existing = await _existing_open_question(dedupe_key=dedupe_key, db=db)
            if existing:
                created.append(existing)
                break

            row = WorkspaceInquiryQuestion(
                source=source,
                actor=actor,
                status="open",
                dedupe_key=dedupe_key,
                trigger_type="execution_truth_runtime_mismatch",
                uncertainty_type="execution_runtime_truth",
                origin_strategy_goal_id=None,
                origin_strategy_id=None,
                origin_plan_id=None,
                why_answer_matters="Observed execution truth diverged from expected runtime behavior, so downstream planning assumptions may be too optimistic.",
                waiting_decision="Whether to request a bounded execution-truth improvement review, gather more runtime evidence, or keep monitoring the drift pattern.",
                no_answer_behavior="System keeps execution truth as reasoning evidence only and does not expand the adaptation scope automatically.",
                candidate_answer_paths_json=candidate_paths,
                urgency="high",
                priority="high",
                safe_default_if_unanswered="keep_monitoring_execution_truth",
                trigger_evidence_json={
                    "execution_id": int(execution.id),
                    "capability_name": capability_name,
                    "execution_status": str(execution.status or "").strip(),
                    "runtime_outcome": str(truth.get("runtime_outcome", "")).strip(),
                    "signal_types": signal_types,
                    "signal_count": len(signal_rows),
                    "execution_truth": truth,
                },
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective62": True,
                    "objective80_execution_truth": True,
                },
            )
            db.add(row)
            created.append(row)
            break

    if len(created) < max_count:
        soft_warning_rows = []
        for item in constraint_rows:
            warnings = (
                item.warnings_json if isinstance(item.warnings_json, list) else []
            )
            if any(
                isinstance(warning, dict) and not bool(warning.get("hard", False))
                for warning in warnings
            ):
                soft_warning_rows.append(item)
        if len(soft_warning_rows) >= min_friction:
            dedupe_key = f"repeated_soft_constraint_friction:run:{run_id or 'global'}"
            existing = await _existing_open_question(dedupe_key=dedupe_key, db=db)
            if existing:
                created.append(existing)
            else:
                candidate_paths = [
                    {
                        "path_id": "propose_soft_constraint_adjustment",
                        "label": "Create bounded proposal to adjust soft policy/constraint weighting",
                        "effect_type": "create_proposal",
                        "params": {
                            "proposal_type": "soft_constraint_weight_adjustment",
                            "affected_component": "constraint_engine",
                        },
                    },
                    {
                        "path_id": "keep_policy_and_monitor",
                        "label": "Keep policy unchanged and continue monitoring",
                        "effect_type": "no_action",
                        "params": {},
                    },
                ]
                row = WorkspaceInquiryQuestion(
                    source=source,
                    actor=actor,
                    status="open",
                    dedupe_key=dedupe_key,
                    trigger_type="repeated_soft_constraint_friction",
                    uncertainty_type="constraint_friction",
                    origin_strategy_goal_id=int(strategy_goals[0].id)
                    if strategy_goals
                    else None,
                    origin_strategy_id=int(strategies[0].id) if strategies else None,
                    origin_plan_id=int(plan.id) if plan else None,
                    why_answer_matters="Repeated soft-constraint friction indicates policy uncertainty that can change future action quality.",
                    waiting_decision="Whether to propose a bounded policy adjustment or continue observation-only mode.",
                    no_answer_behavior="No policy mutation is applied; friction remains review-only.",
                    candidate_answer_paths_json=candidate_paths,
                    urgency="medium",
                    priority="normal",
                    safe_default_if_unanswered="keep_policy_and_monitor",
                    trigger_evidence_json={
                        "soft_warning_count": len(soft_warning_rows),
                        "sample_evaluation_ids": [
                            int(item.id) for item in soft_warning_rows[:10]
                        ],
                    },
                    metadata_json={
                        **(metadata_json if isinstance(metadata_json, dict) else {}),
                        "objective62": True,
                    },
                )
                db.add(row)
                created.append(row)

    if len(created) < max_count and strategy_goals and source_rows:
        noisy = [
            item for item in source_rows if int(item.low_confidence_count or 0) >= 2
        ]
        if noisy:
            source_row = noisy[0]
            dedupe_key = (
                f"low_confidence_perception_blocking_goal:source:{int(source_row.id)}"
            )
            existing = await _existing_open_question(dedupe_key=dedupe_key, db=db)
            if existing:
                created.append(existing)
            else:
                candidate_paths = [
                    {
                        "path_id": "set_operator_required_boundary",
                        "label": "Temporarily enforce operator-required autonomy for this uncertainty region",
                        "effect_type": "change_autonomy",
                        "params": {"target_level": "operator_required"},
                    },
                    {
                        "path_id": "reobserve_then_continue",
                        "label": "Trigger reobserve proposal and continue after evidence refresh",
                        "effect_type": "trigger_rescan",
                        "params": {"proposal_type": "target_reobserve"},
                    },
                ]
                row = WorkspaceInquiryQuestion(
                    source=source,
                    actor=actor,
                    status="open",
                    dedupe_key=dedupe_key,
                    trigger_type="low_confidence_perception_blocking_strategic_goal",
                    uncertainty_type="perception_blocking_goal",
                    origin_strategy_goal_id=int(strategy_goals[0].id),
                    origin_strategy_id=int(strategies[0].id) if strategies else None,
                    origin_plan_id=int(plan.id) if plan else None,
                    why_answer_matters="Sustained low-confidence perception is blocking strategic-goal execution safety.",
                    waiting_decision="Whether to tighten autonomy or refresh evidence before continuing.",
                    no_answer_behavior="Autonomy remains conservative and execution waits for stronger evidence.",
                    candidate_answer_paths_json=candidate_paths,
                    urgency="high",
                    priority="high",
                    safe_default_if_unanswered="set_operator_required_boundary",
                    trigger_evidence_json={
                        "source_id": int(source_row.id),
                        "source_type": source_row.source_type,
                        "low_confidence_count": int(
                            source_row.low_confidence_count or 0
                        ),
                    },
                    metadata_json={
                        **(metadata_json if isinstance(metadata_json, dict) else {}),
                        "objective62": True,
                    },
                )
                db.add(row)
                created.append(row)

    if len(created) < max_count and plan is not None:
        ranked = (
            plan.ranked_goals_json if isinstance(plan.ranked_goals_json, list) else []
        )
        if len(ranked) >= 2:
            first = ranked[0] if isinstance(ranked[0], dict) else {}
            second = ranked[1] if isinstance(ranked[1], dict) else {}
            score_first = float(first.get("score", 0.0) or 0.0)
            score_second = float(second.get("score", 0.0) or 0.0)
            if abs(score_first - score_second) <= 0.05:
                dedupe_key = f"ambiguous_next_action:plan:{int(plan.id)}"
                existing = await _existing_open_question(dedupe_key=dedupe_key, db=db)
                if existing:
                    created.append(existing)
                else:
                    candidate_paths = [
                        {
                            "path_id": "unblock_current_plan",
                            "label": "Proceed with top-ranked path and unblock current plan",
                            "effect_type": "unblock_plan",
                            "params": {},
                        },
                        {
                            "path_id": "adjust_strategy_ranking",
                            "label": "Apply strategy-rank shift before selecting next action",
                            "effect_type": "shift_strategy",
                            "params": {"influence_delta": 0.08},
                        },
                        {
                            "path_id": "hold_for_operator_priority",
                            "label": "Keep plan blocked until operator priority preference is provided",
                            "effect_type": "no_action",
                            "params": {},
                        },
                    ]
                    row = WorkspaceInquiryQuestion(
                        source=source,
                        actor=actor,
                        status="open",
                        dedupe_key=dedupe_key,
                        trigger_type="ambiguous_next_action_under_multiple_valid_paths",
                        uncertainty_type="action_path_ambiguity",
                        origin_strategy_goal_id=int(strategy_goals[0].id)
                        if strategy_goals
                        else None,
                        origin_strategy_id=int(strategies[0].id)
                        if strategies
                        else None,
                        origin_plan_id=int(plan.id),
                        why_answer_matters="Multiple valid next actions are near-tied; answer selection can materially change plan quality.",
                        waiting_decision="Which near-tied path should be chosen for the next action stage.",
                        no_answer_behavior="Plan remains conservative and avoids autonomous tie-breaking.",
                        candidate_answer_paths_json=candidate_paths,
                        urgency="medium",
                        priority="normal",
                        safe_default_if_unanswered="hold_for_operator_priority",
                        trigger_evidence_json={
                            "score_top_1": round(score_first, 6),
                            "score_top_2": round(score_second, 6),
                            "goal_key_top_1": str(first.get("goal_key", "")),
                            "goal_key_top_2": str(second.get("goal_key", "")),
                        },
                        metadata_json={
                            **(
                                metadata_json if isinstance(metadata_json, dict) else {}
                            ),
                            "objective62": True,
                        },
                    )
                    db.add(row)
                    created.append(row)

    if len(created) > max_count:
        created = created[:max_count]

    await db.flush()
    return created


async def list_inquiry_questions(
    *,
    db: AsyncSession,
    status: str = "",
    uncertainty_type: str = "",
    limit: int = 50,
) -> list[WorkspaceInquiryQuestion]:
    rows = (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion).order_by(
                    WorkspaceInquiryQuestion.id.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    if status:
        requested = status.strip().lower()
        rows = [
            item for item in rows if str(item.status or "").strip().lower() == requested
        ]
    if uncertainty_type:
        requested_uncertainty = uncertainty_type.strip().lower()
        rows = [
            item
            for item in rows
            if str(item.uncertainty_type or "").strip().lower() == requested_uncertainty
        ]
    return rows[: max(1, min(500, int(limit)))]


async def get_inquiry_question(
    *, question_id: int, db: AsyncSession
) -> WorkspaceInquiryQuestion | None:
    return (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion).where(
                    WorkspaceInquiryQuestion.id == question_id
                )
            )
        )
        .scalars()
        .first()
    )


async def answer_inquiry_question(
    *,
    row: WorkspaceInquiryQuestion,
    actor: str,
    selected_path_id: str,
    answer_json: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceInquiryQuestion, dict]:
    if str(row.status or "") != "open":
        raise ValueError("inquiry_question_not_open")

    candidate_paths = (
        row.candidate_answer_paths_json
        if isinstance(row.candidate_answer_paths_json, list)
        else []
    )
    selected = None
    for item in candidate_paths:
        if not isinstance(item, dict):
            continue
        if str(item.get("path_id", "")).strip() == selected_path_id:
            selected = item
            break
    if not selected:
        raise ValueError("inquiry_path_not_found")

    effect_type = str(selected.get("effect_type", "no_action")).strip() or "no_action"
    params = (
        selected.get("params", {})
        if isinstance(selected.get("params", {}), dict)
        else {}
    )

    applied_effect: dict = {
        "effect_type": effect_type,
        "selected_path_id": selected_path_id,
        "applied": False,
    }

    if (
        effect_type in {"unblock_plan", "shift_strategy_and_unblock"}
        and row.origin_plan_id is not None
    ):
        plan = await db.get(WorkspaceHorizonPlan, int(row.origin_plan_id))
        if plan:
            plan.status = "active"
            plan.metadata_json = {
                **(plan.metadata_json if isinstance(plan.metadata_json, dict) else {}),
                "inquiry_resolution": {
                    "question_id": int(row.id),
                    "selected_path_id": selected_path_id,
                    "answered_by": actor,
                },
            }
            applied_effect["plan_unblocked"] = True
            applied_effect["plan_id"] = int(plan.id)
            applied_effect["applied"] = True

    if (
        effect_type in {"shift_strategy", "shift_strategy_and_unblock"}
        and row.origin_strategy_id is not None
    ):
        strategy = await db.get(
            WorkspaceEnvironmentStrategy, int(row.origin_strategy_id)
        )
        if strategy:
            delta = float(params.get("influence_delta", 0.1) or 0.1)
            strategy.influence_weight = _bounded(
                float(strategy.influence_weight or 0.0) + delta
            )
            strategy.current_status = "active"
            strategy.status_reason = f"inquiry_answer:{selected_path_id}"
            strategy.metadata_json = {
                **(
                    strategy.metadata_json
                    if isinstance(strategy.metadata_json, dict)
                    else {}
                ),
                "inquiry_resolution": {
                    "question_id": int(row.id),
                    "selected_path_id": selected_path_id,
                    "influence_delta": delta,
                },
            }
            applied_effect["strategy_shifted"] = True
            applied_effect["strategy_id"] = int(strategy.id)
            applied_effect["strategy_influence_weight"] = float(
                strategy.influence_weight
            )
            applied_effect["applied"] = True

    if effect_type == "trigger_rescan":
        proposal_type = (
            str(params.get("proposal_type", "rescan_zone")).strip() or "rescan_zone"
        )
        related_zone = (
            str(params.get("related_zone", "workspace")).strip() or "workspace"
        )
        proposal = WorkspaceProposal(
            proposal_type=proposal_type,
            title=f"Inquiry-triggered {proposal_type.replace('_', ' ')}",
            description=(
                f"Inquiry question {row.id} selected path '{selected_path_id}' requested additional observation evidence."
            ),
            status="proposed",
            confidence=0.72,
            priority_score=0.7,
            priority_reason="inquiry_unresolved_uncertainty",
            source="inquiry",
            related_zone=related_zone,
            related_object_id=None,
            source_execution_id=None,
            trigger_json={
                "question_id": int(row.id),
                "selected_path_id": selected_path_id,
                "uncertainty_type": row.uncertainty_type,
            },
            metadata_json={
                "objective62": True,
                "inquiry_question_id": int(row.id),
                **(metadata_json if isinstance(metadata_json, dict) else {}),
            },
        )
        db.add(proposal)
        await db.flush()
        applied_effect["workspace_proposal_created"] = True
        applied_effect["workspace_proposal_id"] = int(proposal.id)
        applied_effect["applied"] = True

    if effect_type == "change_autonomy":
        target_level = (
            str(params.get("target_level", "operator_required")).strip()
            or "operator_required"
        )
        profile = (
            (
                await db.execute(
                    select(WorkspaceAutonomyBoundaryProfile)
                    .order_by(WorkspaceAutonomyBoundaryProfile.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if profile:
            profile.current_level = target_level
            profile.profile_status = "applied"
            profile.adjustment_reason = f"inquiry_answer:{selected_path_id}"
            profile.metadata_json = {
                **(
                    profile.metadata_json
                    if isinstance(profile.metadata_json, dict)
                    else {}
                ),
                "inquiry_resolution": {
                    "question_id": int(row.id),
                    "selected_path_id": selected_path_id,
                    "target_level": target_level,
                },
            }
            applied_effect["autonomy_changed"] = True
            applied_effect["autonomy_boundary_id"] = int(profile.id)
            applied_effect["autonomy_level"] = target_level
            applied_effect["applied"] = True

    if effect_type == "create_proposal":
        proposal_type = (
            str(params.get("proposal_type", "policy_adjustment")).strip()
            or "policy_adjustment"
        )
        affected_component = (
            str(params.get("affected_component", "inquiry")).strip() or "inquiry"
        )
        trigger_evidence = (
            row.trigger_evidence_json
            if isinstance(row.trigger_evidence_json, dict)
            else {}
        )
        proposal_evidence = {
            "question_id": int(row.id),
            "selected_path_id": selected_path_id,
            "uncertainty_type": row.uncertainty_type,
        }
        proposal_metadata = {
            "objective62": True,
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "inquiry_trigger_type": str(row.trigger_type or "").strip(),
        }
        if str(row.trigger_type or "").strip() == "execution_truth_runtime_mismatch":
            proposal_evidence.update(
                {
                    "execution_id": int(trigger_evidence.get("execution_id", 0) or 0),
                    "capability_name": str(
                        trigger_evidence.get("capability_name", "")
                    ).strip(),
                    "signal_types": trigger_evidence.get("signal_types", []),
                    "signal_count": int(trigger_evidence.get("signal_count", 0) or 0),
                }
            )
            proposal_metadata["objective80_execution_truth"] = True
        proposal = WorkspaceImprovementProposal(
            source="objective62",
            actor=actor,
            proposal_type=proposal_type,
            trigger_pattern="inquiry_answer_generated",
            evidence_summary=f"Created from answered inquiry question {row.id}",
            evidence_json=proposal_evidence,
            affected_component=affected_component,
            suggested_change=f"Investigate uncertainty path '{selected_path_id}' and apply bounded improvement.",
            confidence=0.65,
            safety_class="bounded_review",
            risk_summary="generated_from_inquiry_requires_review",
            test_recommendation="Run focused + full integration regression before promotion",
            status="proposed",
            review_reason="",
            metadata_json=proposal_metadata,
        )
        db.add(proposal)
        await db.flush()
        applied_effect["improvement_proposal_created"] = True
        applied_effect["improvement_proposal_id"] = int(proposal.id)
        applied_effect["applied"] = True

    if effect_type == "update_stewardship_target":
        stewardship_id = int(params.get("stewardship_id", 0) or 0)
        if stewardship_id <= 0:
            trigger_evidence = (
                row.trigger_evidence_json
                if isinstance(row.trigger_evidence_json, dict)
                else {}
            )
            stewardship_id = int(trigger_evidence.get("stewardship_id", 0) or 0)
        stewardship = await _get_stewardship_state(stewardship_id=stewardship_id, db=db)
        if stewardship:
            current_target = (
                stewardship.target_environment_state_json
                if isinstance(stewardship.target_environment_state_json, dict)
                else {}
            )
            updated_target = {
                **current_target,
            }
            scalar_keys = [
                "zone_freshness_seconds",
                "max_system_drift_rate",
                "max_zone_drift_rate",
                "max_zone_uncertainty_score",
                "max_object_uncertainty_score",
                "max_missing_key_objects",
                "proactive_drift_monitoring",
            ]
            for key in scalar_keys:
                if key in params:
                    updated_target[key] = params.get(key)
            if "add_key_objects" in params:
                updated_target["key_objects"] = _merge_unique_strings(
                    current_target.get("key_objects", []),
                    params.get("add_key_objects", []),
                )

            stewardship.target_environment_state_json = updated_target
            stewardship.last_decision_summary = f"Inquiry {row.id} updated stewardship target via path '{selected_path_id}'."
            stewardship.metadata_json = {
                **(
                    stewardship.metadata_json
                    if isinstance(stewardship.metadata_json, dict)
                    else {}
                ),
                "last_inquiry_resolution": {
                    "question_id": int(row.id),
                    "selected_path_id": selected_path_id,
                    "answered_by": actor,
                    "updated_target_environment_state": updated_target,
                },
            }
            applied_effect["stewardship_target_updated"] = True
            applied_effect["stewardship_id"] = int(stewardship.id)
            applied_effect["updated_target_environment_state"] = updated_target
            applied_effect["applied"] = True

    row.status = "answered"
    row.selected_path_id = selected_path_id
    row.answer_json = {
        **(answer_json if isinstance(answer_json, dict) else {}),
        "selected_path": selected,
    }
    row.applied_effect_json = applied_effect
    row.answered_by = actor
    row.answered_at = datetime.now(timezone.utc)
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        **(metadata_json if isinstance(metadata_json, dict) else {}),
        "objective62_answered": True,
    }

    await db.flush()
    return row, applied_effect


def to_inquiry_question_out(row: WorkspaceInquiryQuestion) -> dict:
    return _question_payload(row)
