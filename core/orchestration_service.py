from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.cross_domain_reasoning_service import build_cross_domain_reasoning_context, to_cross_domain_reasoning_out
from core.horizon_planning_service import create_horizon_plan
from core.improvement_service import generate_improvement_proposals
from core.models import Goal, WorkspaceCrossDomainReasoningContext, WorkspaceInquiryQuestion, WorkspaceTaskOrchestration


ORCHESTRATION_POLICIES = {"ask", "defer", "replan", "escalate"}


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _priority_label(score: float) -> str:
    if score >= 0.8:
        return "critical"
    if score >= 0.65:
        return "high"
    if score >= 0.45:
        return "normal"
    return "low"


def _run_id(metadata_json: dict) -> str:
    if not isinstance(metadata_json, dict):
        return ""
    return str(metadata_json.get("run_id", "")).strip()


def _match_run_id(metadata_json: dict, run_id: str) -> bool:
    if not run_id:
        return True
    if not isinstance(metadata_json, dict):
        return False
    return str(metadata_json.get("run_id", "")).strip() == run_id


def _domain_signals(context: dict) -> dict[str, int]:
    workspace = context.get("workspace_state", {}) if isinstance(context.get("workspace_state", {}), dict) else {}
    communication = context.get("communication_state", {}) if isinstance(context.get("communication_state", {}), dict) else {}
    external = context.get("external_information", {}) if isinstance(context.get("external_information", {}), dict) else {}
    development = context.get("development_state", {}) if isinstance(context.get("development_state", {}), dict) else {}
    self_improvement = context.get("self_improvement_state", {}) if isinstance(context.get("self_improvement_state", {}), dict) else {}

    return {
        "workspace_state": _safe_int(workspace.get("observation_count", 0)),
        "communication_state": _safe_int(communication.get("input_event_count", 0)) + _safe_int(communication.get("output_event_count", 0)),
        "external_information": _safe_int(external.get("external_item_count", 0)),
        "development_state": _safe_int(development.get("pattern_count", 0)),
        "self_improvement_state": _safe_int(self_improvement.get("backlog_item_count", 0)),
    }


def _priority_score(*, context_confidence: float, signals: dict[str, int]) -> float:
    weighted_signal = (
        (signals.get("workspace_state", 0) * 0.25)
        + (signals.get("communication_state", 0) * 0.15)
        + (signals.get("external_information", 0) * 0.2)
        + (signals.get("development_state", 0) * 0.2)
        + (signals.get("self_improvement_state", 0) * 0.2)
    )
    normalized_signal = _bounded(weighted_signal / 20.0)
    return _bounded((context_confidence * 0.6) + (normalized_signal * 0.4))


def _dependency_gaps(
    *,
    context_confidence: float,
    contributing_domains: list[str],
    min_context_confidence: float,
    min_domains_required: int,
    context_reasoning: dict,
) -> list[dict]:
    gaps: list[dict] = []
    if context_confidence < min_context_confidence:
        gaps.append(
            {
                "dependency": "context_confidence",
                "required": min_context_confidence,
                "observed": context_confidence,
                "reason": "cross_domain_confidence_below_threshold",
            }
        )

    if len(contributing_domains) < min_domains_required:
        gaps.append(
            {
                "dependency": "domain_coverage",
                "required": min_domains_required,
                "observed": len(contributing_domains),
                "reason": "insufficient_contributing_domains",
            }
        )

    links = context_reasoning.get("cross_domain_links", []) if isinstance(context_reasoning.get("cross_domain_links", []), list) else []
    if len(contributing_domains) >= 2 and not links:
        gaps.append(
            {
                "dependency": "cross_domain_linkage",
                "required": 1,
                "observed": 0,
                "reason": "missing_explainable_links_between_domains",
            }
        )

    return gaps


async def _latest_reasoning_context(*, run_id: str, db: AsyncSession) -> WorkspaceCrossDomainReasoningContext | None:
    rows = (
        await db.execute(
            select(WorkspaceCrossDomainReasoningContext)
            .order_by(WorkspaceCrossDomainReasoningContext.id.desc())
            .limit(100)
        )
    ).scalars().all()
    if not run_id:
        return rows[0] if rows else None
    for row in rows:
        if _match_run_id(row.metadata_json if isinstance(row.metadata_json, dict) else {}, run_id):
            return row
    return None


async def _get_or_create_orchestration_question(
    *,
    actor: str,
    source: str,
    context_id: int | None,
    dependency_gaps: list[dict],
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceInquiryQuestion:
    run_id = _run_id(metadata_json)
    dedupe_key = f"orchestration_dependency_unmet:context:{int(context_id or 0)}:run:{run_id or 'na'}"

    existing = (
        await db.execute(
            select(WorkspaceInquiryQuestion)
            .where(WorkspaceInquiryQuestion.dedupe_key == dedupe_key)
            .where(WorkspaceInquiryQuestion.status == "open")
            .order_by(WorkspaceInquiryQuestion.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if existing:
        return existing

    question = WorkspaceInquiryQuestion(
        source=source,
        actor=actor,
        status="open",
        dedupe_key=dedupe_key,
        trigger_type="orchestration_dependency_unmet",
        uncertainty_type="cross_domain_dependency_gap",
        origin_strategy_goal_id=None,
        origin_strategy_id=None,
        origin_plan_id=None,
        why_answer_matters="Orchestration cannot safely continue until unresolved cross-domain dependencies are clarified.",
        waiting_decision="Choose whether to defer, replan, or escalate blocked orchestration dependencies.",
        no_answer_behavior="System remains blocked and avoids autonomous downstream execution.",
        candidate_answer_paths_json=[
            {
                "path_id": "defer",
                "label": "Defer orchestration until dependencies are satisfied",
                "effect_type": "defer",
                "params": {},
            },
            {
                "path_id": "replan",
                "label": "Replan with expanded context window",
                "effect_type": "replan",
                "params": {"lookback_hours_delta": 24},
            },
            {
                "path_id": "escalate",
                "label": "Escalate to operator with dependency report",
                "effect_type": "escalate",
                "params": {},
            },
        ],
        urgency="high",
        priority="high",
        safe_default_if_unanswered="defer",
        trigger_evidence_json={
            "dependency_gaps": dependency_gaps,
            "origin_context_id": context_id,
        },
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective63_orchestration": True,
        },
    )
    db.add(question)
    await db.flush()
    return question


async def build_cross_domain_task_orchestration(
    *,
    actor: str,
    source: str,
    lookback_hours: int,
    max_items_per_domain: int,
    min_context_confidence: float,
    min_domains_required: int,
    dependency_resolution_policy: str,
    generate_goal: bool,
    generate_horizon_plan: bool,
    generate_improvement_proposals: bool,
    metadata_json: dict,
    db: AsyncSession,
) -> tuple[WorkspaceTaskOrchestration, WorkspaceCrossDomainReasoningContext]:
    policy = str(dependency_resolution_policy or "ask").strip().lower()
    if policy not in ORCHESTRATION_POLICIES:
        policy = "ask"

    run_id = _run_id(metadata_json)
    context = await _latest_reasoning_context(run_id=run_id, db=db)
    if not context:
        context = await build_cross_domain_reasoning_context(
            actor=actor,
            source=source,
            lookback_hours=max(1, int(lookback_hours)),
            max_items_per_domain=max(1, min(200, int(max_items_per_domain))),
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "objective63_orchestration_context_build": True,
            },
            db=db,
        )

    context_out = to_cross_domain_reasoning_out(context)
    context_confidence = _bounded(float(context_out.get("confidence", 0.0)))
    signals = _domain_signals(context_out)
    contributing_domains = [key for key, value in signals.items() if int(value) > 0]
    reasoning = context_out.get("reasoning", {}) if isinstance(context_out.get("reasoning", {}), dict) else {}

    dependency_gaps = _dependency_gaps(
        context_confidence=context_confidence,
        contributing_domains=contributing_domains,
        min_context_confidence=_bounded(min_context_confidence),
        min_domains_required=max(1, int(min_domains_required)),
        context_reasoning=reasoning,
    )

    priority_score = _priority_score(context_confidence=context_confidence, signals=signals)
    priority_label = _priority_label(priority_score)

    linked_goal_ids: list[int] = []
    linked_horizon_plan_ids: list[int] = []
    linked_improvement_proposal_ids: list[int] = []
    linked_inquiry_question_ids: list[int] = []
    downstream_artifacts: list[dict] = []

    status = "active"
    dependency_resolution = {
        "has_unmet_dependencies": bool(dependency_gaps),
        "path": "proceed",
        "unmet_dependencies": dependency_gaps,
    }

    if dependency_gaps:
        dependency_resolution["path"] = policy
        if policy == "ask":
            question = await _get_or_create_orchestration_question(
                actor=actor,
                source=source,
                context_id=int(context.id) if context else None,
                dependency_gaps=dependency_gaps,
                metadata_json=metadata_json,
                db=db,
            )
            linked_inquiry_question_ids.append(int(question.id))
            downstream_artifacts.append(
                {
                    "artifact_type": "inquiry_question",
                    "artifact_id": int(question.id),
                    "status": "open",
                }
            )
            status = "blocked_needs_input"
        elif policy == "defer":
            status = "deferred"
        elif policy == "replan":
            status = "replan_required"
        else:
            status = "escalated"

    if not dependency_gaps:
        if generate_goal:
            goal = Goal(
                objective_id=None,
                task_id=None,
                goal_type="cross_domain_orchestration",
                goal_description=(
                    "Coordinate cross-domain execution using synchronized workspace, communication, "
                    "external information, and developmental context."
                ),
                requested_by=actor,
                priority=priority_label,
                status="new",
            )
            db.add(goal)
            await db.flush()
            linked_goal_ids.append(int(goal.id))
            downstream_artifacts.append(
                {
                    "artifact_type": "goal",
                    "artifact_id": int(goal.id),
                    "status": "new",
                }
            )

        if generate_horizon_plan:
            horizon_goal_candidates = [
                {
                    "goal_key": f"orchestration:{int(context.id)}:primary",
                    "title": "Cross-domain orchestration execution",
                    "priority": priority_label,
                    "goal_type": "cross_domain_orchestration",
                    "dependencies": [],
                    "estimated_steps": 3,
                    "expected_value": _bounded(priority_score + 0.05),
                    "urgency": _bounded(priority_score),
                    "requires_fresh_map": False,
                    "requires_high_confidence": context_confidence >= 0.7,
                    "is_physical": False,
                    "metadata_json": {
                        "origin_context_id": int(context.id),
                        "linked_goal_id": linked_goal_ids[0] if linked_goal_ids else None,
                    },
                }
            ]
            plan, _ = await create_horizon_plan(
                actor=actor,
                source=source,
                planning_horizon_minutes=120,
                goal_candidates=horizon_goal_candidates,
                expected_future_constraints=[],
                priority_policy={
                    "min_target_confidence": max(0.4, _bounded(min_context_confidence)),
                    "map_freshness_limit_seconds": 900,
                },
                map_freshness_seconds=0,
                object_confidence=max(0.4, context_confidence),
                human_aware_state={},
                operator_preferences={},
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective63_orchestration": True,
                },
                db=db,
            )
            linked_horizon_plan_ids.append(int(plan.id))
            downstream_artifacts.append(
                {
                    "artifact_type": "horizon_plan",
                    "artifact_id": int(plan.id),
                    "status": str(plan.status or "planned"),
                }
            )

        if generate_improvement_proposals and ("development_state" in contributing_domains or "self_improvement_state" in contributing_domains):
            proposals = await generate_improvement_proposals(
                actor=actor,
                source=source,
                lookback_hours=max(1, int(lookback_hours)),
                min_occurrence_count=2,
                max_proposals=1,
                metadata_json={
                    **(metadata_json if isinstance(metadata_json, dict) else {}),
                    "objective63_orchestration": True,
                },
                db=db,
            )
            for item in proposals[:1]:
                linked_improvement_proposal_ids.append(int(item.id))
                downstream_artifacts.append(
                    {
                        "artifact_type": "improvement_proposal",
                        "artifact_id": int(item.id),
                        "status": str(item.status or "proposed"),
                    }
                )

    orchestration_reason = (
        f"Prioritized {len(contributing_domains)}/5 contributing domains with context confidence={context_confidence:.2f}; "
        f"dependency_policy={policy}"
    )
    reasoning_out = {
        "priority_reason": "Blends context confidence with weighted cross-domain signal volume.",
        "contributing_domains": contributing_domains,
        "domain_signal_counts": signals,
        "context_summary": context_out.get("reasoning_summary", ""),
        "cross_domain_links": reasoning.get("cross_domain_links", []),
    }

    row = WorkspaceTaskOrchestration(
        source=source,
        actor=actor,
        status=status,
        orchestration_type="cross_domain_task_orchestration",
        origin_context_id=int(context.id) if context else None,
        lookback_hours=max(1, int(lookback_hours)),
        priority_score=priority_score,
        priority_label=priority_label,
        contributing_domains_json=contributing_domains,
        dependency_resolution_json=dependency_resolution,
        orchestration_reason=orchestration_reason,
        reasoning_json=reasoning_out,
        linked_goal_ids_json=linked_goal_ids,
        linked_horizon_plan_ids_json=linked_horizon_plan_ids,
        linked_improvement_proposal_ids_json=linked_improvement_proposal_ids,
        linked_inquiry_question_ids_json=linked_inquiry_question_ids,
        downstream_artifacts_json=downstream_artifacts,
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective63_cross_domain_task_orchestration": True,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.add(row)
    await db.flush()
    return row, context


async def list_task_orchestrations(
    *,
    db: AsyncSession,
    status: str = "",
    source: str = "",
    limit: int = 50,
) -> list[WorkspaceTaskOrchestration]:
    rows = (
        await db.execute(
            select(WorkspaceTaskOrchestration)
            .order_by(WorkspaceTaskOrchestration.id.desc())
        )
    ).scalars().all()

    filtered = rows
    if status.strip():
        requested_status = status.strip().lower()
        filtered = [item for item in filtered if str(item.status or "").strip().lower() == requested_status]
    if source.strip():
        requested_source = source.strip().lower()
        filtered = [item for item in filtered if str(item.source or "").strip().lower() == requested_source]

    return filtered[: max(1, min(500, int(limit)))]


async def get_task_orchestration(*, orchestration_id: int, db: AsyncSession) -> WorkspaceTaskOrchestration | None:
    return (
        await db.execute(
            select(WorkspaceTaskOrchestration).where(WorkspaceTaskOrchestration.id == orchestration_id)
        )
    ).scalars().first()


def to_task_orchestration_out(row: WorkspaceTaskOrchestration) -> dict:
    return {
        "orchestration_id": int(row.id),
        "source": row.source,
        "actor": row.actor,
        "status": row.status,
        "orchestration_type": row.orchestration_type,
        "origin_context_id": int(row.origin_context_id) if row.origin_context_id is not None else None,
        "lookback_hours": int(row.lookback_hours or 0),
        "priority_score": float(row.priority_score or 0.0),
        "priority_label": row.priority_label,
        "contributing_domains": row.contributing_domains_json if isinstance(row.contributing_domains_json, list) else [],
        "dependency_resolution": row.dependency_resolution_json if isinstance(row.dependency_resolution_json, dict) else {},
        "orchestration_reason": row.orchestration_reason,
        "reasoning": row.reasoning_json if isinstance(row.reasoning_json, dict) else {},
        "linked_goal_ids": row.linked_goal_ids_json if isinstance(row.linked_goal_ids_json, list) else [],
        "linked_horizon_plan_ids": row.linked_horizon_plan_ids_json if isinstance(row.linked_horizon_plan_ids_json, list) else [],
        "linked_improvement_proposal_ids": row.linked_improvement_proposal_ids_json if isinstance(row.linked_improvement_proposal_ids_json, list) else [],
        "linked_inquiry_question_ids": row.linked_inquiry_question_ids_json if isinstance(row.linked_inquiry_question_ids_json, list) else [],
        "downstream_artifacts": row.downstream_artifacts_json if isinstance(row.downstream_artifacts_json, list) else [],
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }
