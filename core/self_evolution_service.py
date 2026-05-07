from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import re

from sqlalchemy.ext.asyncio import AsyncSession

from core.improvement_governance_service import (
    get_improvement_backlog_item,
    list_improvement_backlog,
    refresh_improvement_backlog,
    to_improvement_backlog_out,
)
from core.improvement_recommendation_service import (
    get_improvement_recommendation,
    list_improvement_recommendations,
    to_improvement_recommendation_out_resolved,
)
from core.improvement_service import list_improvement_proposals, to_improvement_proposal_out
from core.improvement_service import get_improvement_proposal
from core.state_bus_service import (
    append_state_bus_event,
    get_state_bus_snapshot,
    to_state_bus_snapshot_out,
    upsert_state_bus_snapshot,
)


SELF_EVOLUTION_PROGRESS_SOURCE = "objective173"
SELF_EVOLUTION_PROGRESS_POLICY_ID = "natural_language_development_v1"
SELF_EVOLUTION_PROGRESS_EVENT_DOMAIN = "mim.improvement"


def _count_values(values: list[str]) -> dict[str, int]:
    counts = Counter(item for item in values if item)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _snapshot_status(
    *,
    proposal_counts: dict[str, int],
    recommendation_counts: dict[str, int],
    backlog_counts: dict[str, int],
    risk_counts: dict[str, int],
    governance_counts: dict[str, int],
) -> str:
    operator_review_count = int(governance_counts.get("request_operator_review", 0) or 0)
    open_recommendations = int(recommendation_counts.get("proposed", 0) or 0)
    open_proposals = int(proposal_counts.get("proposed", 0) or 0)
    active_backlog = sum(
        int(backlog_counts.get(key, 0) or 0)
        for key in ["queued", "experimenting", "evaluating", "recommended"]
    )
    high_risk_items = int(risk_counts.get("high", 0) or 0)

    if operator_review_count > 0 or (high_risk_items > 0 and active_backlog > 0):
        return "operator_review_required"
    if open_proposals > 0 or open_recommendations > 0 or active_backlog > 0:
        return "active"
    return "quiet"


def _snapshot_summary(
    *,
    status: str,
    proposal_counts: dict[str, int],
    recommendation_counts: dict[str, int],
    backlog_counts: dict[str, int],
    governance_counts: dict[str, int],
    top_priority_type: str,
) -> str:
    open_proposals = int(proposal_counts.get("proposed", 0) or 0)
    open_recommendations = int(recommendation_counts.get("proposed", 0) or 0)
    queued_items = int(backlog_counts.get("queued", 0) or 0)
    recommended_items = int(backlog_counts.get("recommended", 0) or 0)
    operator_review_count = int(governance_counts.get("request_operator_review", 0) or 0)

    if status == "operator_review_required":
        return (
            f"Self-evolution is active with {operator_review_count} backlog item(s) awaiting operator review; "
            f"open proposals={open_proposals}, open recommendations={open_recommendations}, "
            f"top priority type={top_priority_type or 'none'}."
        )
    if status == "active":
        return (
            f"Self-evolution is active with {queued_items + recommended_items} ranked backlog item(s), "
            f"open proposals={open_proposals}, open recommendations={open_recommendations}, "
            f"top priority type={top_priority_type or 'none'}."
        )
    return (
        "Self-evolution is quiet; no active ranked backlog pressure is present and the current loop is "
        f"holding at proposals={open_proposals}, recommendations={open_recommendations}."
    )


async def build_self_evolution_snapshot(
    *,
    actor: str,
    source: str,
    refresh: bool,
    lookback_hours: int,
    min_occurrence_count: int,
    auto_experiment_limit: int,
    limit: int,
    db: AsyncSession,
) -> dict:
    fetch_limit = max(25, min(500, int(limit) * 5))
    if refresh:
        backlog_rows = await refresh_improvement_backlog(
            actor=actor,
            source=source,
            lookback_hours=lookback_hours,
            min_occurrence_count=min_occurrence_count,
            max_items=fetch_limit,
            auto_experiment_limit=auto_experiment_limit,
            metadata_json={
                "objective164_self_evolution": True,
                "refresh_via_self_evolution": True,
            },
            db=db,
        )
    else:
        backlog_rows = await list_improvement_backlog(
            db=db,
            status="",
            risk_level="",
            limit=fetch_limit,
        )

    proposal_rows = await list_improvement_proposals(
        db=db,
        status="",
        proposal_type="",
        limit=fetch_limit,
    )
    recommendation_rows = await list_improvement_recommendations(
        db=db,
        status="",
        recommendation_type="",
        limit=fetch_limit,
    )

    proposal_by_id = {int(row.id): row for row in proposal_rows}
    proposal_counts = _count_values([str(row.status or "").strip().lower() for row in proposal_rows])
    recommendation_counts = _count_values(
        [str(row.status or "").strip().lower() for row in recommendation_rows]
    )
    backlog_counts = _count_values([str(row.status or "").strip().lower() for row in backlog_rows])
    risk_counts = _count_values([str(row.risk_level or "").strip().lower() for row in backlog_rows])
    governance_counts = _count_values(
        [str(row.governance_decision or "").strip().lower() for row in backlog_rows]
    )

    top_backlog = backlog_rows[0] if backlog_rows else None
    top_priority_score = float(getattr(top_backlog, "priority_score", 0.0) or 0.0)
    top_priority_type = str(getattr(top_backlog, "proposal_type", "") or "").strip()
    top_proposal = proposal_by_id.get(int(getattr(top_backlog, "proposal_id", 0) or 0)) if top_backlog else None
    top_affected_component = str(getattr(top_proposal, "affected_component", "") or "").strip()

    status = _snapshot_status(
        proposal_counts=proposal_counts,
        recommendation_counts=recommendation_counts,
        backlog_counts=backlog_counts,
        risk_counts=risk_counts,
        governance_counts=governance_counts,
    )
    summary = _snapshot_summary(
        status=status,
        proposal_counts=proposal_counts,
        recommendation_counts=recommendation_counts,
        backlog_counts=backlog_counts,
        governance_counts=governance_counts,
        top_priority_type=top_priority_type,
    )

    top_proposals = [to_improvement_proposal_out(row) for row in proposal_rows[: max(1, int(limit))]]
    top_recommendations = [
        await to_improvement_recommendation_out_resolved(row=row, db=db)
        for row in recommendation_rows[: max(1, int(limit))]
    ]
    top_backlog_items = [
        to_improvement_backlog_out(row) for row in backlog_rows[: max(1, int(limit))]
    ]

    return {
        "status": status,
        "summary": summary,
        "proposal_counts": proposal_counts,
        "recommendation_counts": recommendation_counts,
        "backlog_counts": backlog_counts,
        "risk_counts": risk_counts,
        "governance_decision_counts": governance_counts,
        "top_priority_score": top_priority_score,
        "top_priority_type": top_priority_type,
        "top_affected_component": top_affected_component,
        "proposals": top_proposals,
        "recommendations": top_recommendations,
        "backlog": top_backlog_items,
        "metadata_json": {
            "actor": actor,
            "source": source,
            "refresh_requested": refresh,
            "lookback_hours": int(lookback_hours),
            "min_occurrence_count": int(min_occurrence_count),
            "auto_experiment_limit": int(auto_experiment_limit),
            "limit": int(limit),
            "objective164_self_evolution": True,
        },
        "created_at": datetime.now(timezone.utc),
    }


def _decision_payload(
    *,
    decision_type: str,
    priority: str,
    rationale: str,
    target_kind: str,
    target_id: int | None,
    action_method: str,
    action_path: str,
    action_payload: dict,
    summary: str,
    snapshot: dict,
    metadata_json: dict,
) -> dict:
    return {
        "decision_type": decision_type,
        "priority": priority,
        "rationale": rationale,
        "target_kind": target_kind,
        "target_id": target_id,
        "action": {
            "method": action_method,
            "path": action_path,
            "payload": action_payload,
        },
        "summary": summary,
        "snapshot_status": str(snapshot.get("status", "") or ""),
        "snapshot_summary": str(snapshot.get("summary", "") or ""),
        "metadata_json": metadata_json,
        "created_at": datetime.now(timezone.utc),
    }


def _natural_language_development_metric(
    *,
    name: str,
    minimum: float | None = None,
    maximum: float | None = None,
    stage: str = "smoke",
    source: str = "conversation_eval_runner.py",
    failure_absent: list[str] | None = None,
) -> dict:
    payload = {
        "name": name,
        "stage": stage,
        "source": source,
    }
    if minimum is not None:
        payload["minimum"] = float(minimum)
    if maximum is not None:
        payload["maximum"] = float(maximum)
    if failure_absent:
        payload["failure_absent"] = list(failure_absent)
    return payload


def _natural_language_development_skill(
    *,
    skill_id: str,
    title: str,
    priority_band: str,
    policy_score: float,
    why_now: str,
    development_goal: str,
    build_methods: list[str],
    evaluation_methods: list[str],
    pass_metrics: list[dict],
) -> dict:
    return {
        "skill_id": skill_id,
        "title": title,
        "priority_band": priority_band,
        "policy_score": float(policy_score),
        "why_now": why_now,
        "development_goal": development_goal,
        "build_methods": list(build_methods),
        "evaluation_methods": list(evaluation_methods),
        "pass_metrics": list(pass_metrics),
    }


def _natural_language_development_task(
    *,
    task_id: str,
    summary: str,
    category: str,
    proof_target: str,
) -> dict:
    return {
        "task_id": task_id,
        "summary": summary,
        "category": category,
        "proof_target": proof_target,
    }


def _natural_language_skill_map(skills: list[dict]) -> dict[str, dict]:
    return {
        str(skill.get("skill_id") or "").strip(): skill
        for skill in skills
        if isinstance(skill, dict) and str(skill.get("skill_id") or "").strip()
    }


def _natural_language_slice_pass_metrics(*, focus_skills: list[dict]) -> list[dict]:
    metrics: list[dict] = []
    seen: set[tuple] = set()
    for skill in focus_skills:
        if not isinstance(skill, dict):
            continue
        skill_metrics = skill.get("pass_metrics", []) if isinstance(skill.get("pass_metrics", []), list) else []
        for metric in skill_metrics:
            if not isinstance(metric, dict):
                continue
            key = (
                str(metric.get("name") or "").strip().lower(),
                str(metric.get("stage") or "smoke").strip().lower(),
                metric.get("minimum"),
                metric.get("maximum"),
                tuple(
                    sorted(
                        str(tag).strip().lower()
                        for tag in metric.get("failure_absent", [])
                        if str(tag).strip()
                    )
                ),
            )
            if not key[0] or key in seen:
                continue
            seen.add(key)
            metrics.append(metric)
    return metrics


def _self_evolution_progress_scope(*, actor: str, source: str) -> str:
    def _slug(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower())
        return normalized.strip("-") or "default"

    return (
        "self-evolution:natural-language-development:"
        f"{_slug(actor)}:{_slug(source)}"
    )


def _slice_index_by_id(slices: list[dict]) -> dict[str, int]:
    return {
        str(slice_payload.get("slice_id") or "").strip(): index
        for index, slice_payload in enumerate(slices)
        if isinstance(slice_payload, dict) and str(slice_payload.get("slice_id") or "").strip()
    }


def _default_natural_language_progress_state(*, slices: list[dict]) -> dict:
    index_by_id = _slice_index_by_id(slices)
    first_slice = slices[0] if slices else {}
    active_slice_id = str(first_slice.get("slice_id") or "").strip()
    return {
        "policy_id": SELF_EVOLUTION_PROGRESS_POLICY_ID,
        "status": "running",
        "active_slice_id": active_slice_id,
        "active_slice_index": int(index_by_id.get(active_slice_id, 0) + 1 if active_slice_id else 0),
        "active_cycle": 1,
        "completed_cycle_count": 0,
        "completed_slice_ids": [],
        "promotion_count": 0,
        "repair_count": 0,
        "evaluation_count": 0,
        "last_outcome": "not_started",
        "last_outcome_at": "",
        "last_evaluation": {},
        "blocked_reason": "",
        "stop_reason": "",
        "proof_log": [],
        "discovered_skill_candidates": [],
    }


def _normalize_natural_language_progress_state(*, payload: dict, slices: list[dict]) -> dict:
    base = _default_natural_language_progress_state(slices=slices)
    if not isinstance(payload, dict):
        return base

    index_by_id = _slice_index_by_id(slices)
    valid_slice_ids = set(index_by_id)
    active_slice_id = str(payload.get("active_slice_id") or base.get("active_slice_id") or "").strip()
    if active_slice_id not in valid_slice_ids:
        active_slice_id = str(base.get("active_slice_id") or "").strip()

    payload_completed_slice_ids = (
        payload.get("completed_slice_ids", [])
        if isinstance(payload.get("completed_slice_ids", []), list)
        else []
    )
    completed_slice_ids = [
        slice_id
        for slice_id in payload_completed_slice_ids
        if str(slice_id).strip() in valid_slice_ids and str(slice_id).strip() != active_slice_id
    ]
    payload_proof_log = payload.get("proof_log", []) if isinstance(payload.get("proof_log", []), list) else []
    proof_log = [
        item
        for item in payload_proof_log
        if isinstance(item, dict)
    ][-12:]
    payload_discovered_candidates = (
        payload.get("discovered_skill_candidates", [])
        if isinstance(payload.get("discovered_skill_candidates", []), list)
        else []
    )
    discovered_skill_candidates = list(dict.fromkeys(
        str(item).strip()
        for item in payload_discovered_candidates
        if str(item).strip()
    ))

    normalized = {
        **base,
        "policy_id": SELF_EVOLUTION_PROGRESS_POLICY_ID,
        "status": str(payload.get("status") or base.get("status") or "running").strip().lower() or "running",
        "active_slice_id": active_slice_id,
        "active_slice_index": int(index_by_id.get(active_slice_id, 0) + 1 if active_slice_id else 0),
        "active_cycle": max(1, int(payload.get("active_cycle", base.get("active_cycle", 1)) or 1)),
        "completed_cycle_count": max(
            0,
            int(payload.get("completed_cycle_count", base.get("completed_cycle_count", 0)) or 0),
        ),
        "promotion_count": max(0, int(payload.get("promotion_count", base.get("promotion_count", 0)) or 0)),
        "repair_count": max(0, int(payload.get("repair_count", base.get("repair_count", 0)) or 0)),
        "evaluation_count": max(0, int(payload.get("evaluation_count", base.get("evaluation_count", 0)) or 0)),
        "completed_slice_ids": completed_slice_ids,
        "last_outcome": str(payload.get("last_outcome") or base.get("last_outcome") or "not_started").strip().lower() or "not_started",
        "last_outcome_at": str(payload.get("last_outcome_at") or base.get("last_outcome_at") or "").strip(),
        "last_evaluation": payload.get("last_evaluation", {}) if isinstance(payload.get("last_evaluation", {}), dict) else {},
        "blocked_reason": str(payload.get("blocked_reason") or "").strip(),
        "stop_reason": str(payload.get("stop_reason") or "").strip(),
        "proof_log": proof_log,
        "discovered_skill_candidates": discovered_skill_candidates,
    }
    return normalized


def _slice_metric_actual_value(*, metrics_json: dict, stage: str, name: str) -> float | None:
    stage_key = str(stage or "smoke").strip().lower()
    metric_name = str(name or "").strip()
    candidates = [
        metrics_json,
        metrics_json.get(stage_key, {}) if isinstance(metrics_json.get(stage_key, {}), dict) else {},
        metrics_json.get("summary", {}) if isinstance(metrics_json.get("summary", {}), dict) else {},
        metrics_json.get("metrics", {}) if isinstance(metrics_json.get("metrics", {}), dict) else {},
    ]
    for candidate in candidates:
        raw = candidate.get(metric_name)
        if raw is None:
            continue
        try:
            return float(raw)
        except Exception:
            return None
    return None


def evaluate_natural_language_slice_pass(
    *,
    slice_payload: dict,
    metrics_json: dict,
    failure_tags: list[str],
) -> dict:
    pass_metrics = slice_payload.get("pass_metrics", []) if isinstance(slice_payload.get("pass_metrics", []), list) else []
    normalized_metrics = metrics_json if isinstance(metrics_json, dict) else {}
    normalized_failure_tags = sorted(
        {
            str(tag).strip().lower()
            for tag in failure_tags
            if str(tag).strip()
        }
    )
    passed_checks: list[dict] = []
    failed_checks: list[dict] = []

    for metric in pass_metrics:
        if not isinstance(metric, dict):
            continue
        name = str(metric.get("name") or "").strip()
        if not name:
            continue
        stage = str(metric.get("stage") or "smoke").strip().lower() or "smoke"
        actual_value = _slice_metric_actual_value(
            metrics_json=normalized_metrics,
            stage=stage,
            name=name,
        )
        minimum = metric.get("minimum")
        maximum = metric.get("maximum")
        absent_failures = sorted(
            {
                str(tag).strip().lower()
                for tag in metric.get("failure_absent", [])
                if str(tag).strip()
            }
        )
        violating_failures = [tag for tag in normalized_failure_tags if tag in absent_failures]

        metric_failed = False
        reasons: list[str] = []
        if minimum is not None and (actual_value is None or float(actual_value) < float(minimum)):
            metric_failed = True
            reasons.append(
                f"{name} expected >= {float(minimum):.2f} at {stage}, got {actual_value if actual_value is not None else 'missing'}"
            )
        if maximum is not None and (actual_value is None or float(actual_value) > float(maximum)):
            metric_failed = True
            reasons.append(
                f"{name} expected <= {float(maximum):.2f} at {stage}, got {actual_value if actual_value is not None else 'missing'}"
            )
        if violating_failures:
            metric_failed = True
            reasons.append(f"forbidden failure tags present: {', '.join(violating_failures)}")

        record = {
            "name": name,
            "stage": stage,
            "minimum": minimum,
            "maximum": maximum,
            "actual": actual_value,
            "failure_absent": absent_failures,
            "violating_failures": violating_failures,
            "reasons": reasons,
        }
        if metric_failed:
            failed_checks.append(record)
        else:
            passed_checks.append(record)

    return {
        "passed": len(failed_checks) == 0,
        "passed_checks": passed_checks,
        "failed_checks": failed_checks,
        "metrics_json": normalized_metrics,
        "failure_tags": normalized_failure_tags,
        "slice_id": str(slice_payload.get("slice_id") or "").strip(),
    }


def _apply_natural_language_progress_to_packet(*, packet: dict, progress_state: dict) -> dict:
    hydrated = dict(packet)
    skills = hydrated.get("skills", []) if isinstance(hydrated.get("skills", []), list) else []
    slices = hydrated.get("slices", []) if isinstance(hydrated.get("slices", []), list) else []
    skill_map = _natural_language_skill_map(skills)
    active_slice_id = str(progress_state.get("active_slice_id") or "").strip()
    active_slice = next(
        (
            dict(slice_payload)
            for slice_payload in slices
            if isinstance(slice_payload, dict)
            and str(slice_payload.get("slice_id") or "").strip() == active_slice_id
        ),
        dict(slices[0]) if slices else {},
    )
    if not active_slice and slices:
        active_slice = dict(slices[0])
        active_slice_id = str(active_slice.get("slice_id") or "").strip()

    progress_status = str(progress_state.get("status") or "running").strip().lower() or "running"
    completed_slice_ids = {
        str(slice_id).strip()
        for slice_id in progress_state.get("completed_slice_ids", [])
        if str(slice_id).strip()
    }
    active_slice_title = str(active_slice.get("title") or "").strip()
    next_slice_title = str(active_slice.get("next_slice_title") or "the next ranked slice").strip() or "the next ranked slice"
    focus_skill_ids = active_slice.get("focus_skill_ids", []) if isinstance(active_slice.get("focus_skill_ids", []), list) else []
    selected_skill = next(
        (
            skill_map.get(str(skill_id).strip())
            for skill_id in focus_skill_ids
            if skill_map.get(str(skill_id).strip()) is not None
        ),
        hydrated.get("selected_skill", {}) if isinstance(hydrated.get("selected_skill", {}), dict) else {},
    )
    selected_skill = selected_skill if isinstance(selected_skill, dict) else {}
    selected_skill_title = str(selected_skill.get("title") or hydrated.get("selected_skill_title") or "").strip()

    hydrated_slices: list[dict] = []
    for slice_payload in slices:
        if not isinstance(slice_payload, dict):
            continue
        slice_id = str(slice_payload.get("slice_id") or "").strip()
        slice_state = "pending"
        if slice_id in completed_slice_ids:
            slice_state = "completed"
        elif slice_id == active_slice_id:
            if progress_status == "blocked":
                slice_state = "blocked"
            elif progress_status == "stopped":
                slice_state = "stopped"
            elif progress_status == "repairing":
                slice_state = "repairing"
            else:
                slice_state = "active"
        hydrated_slices.append({**slice_payload, "state": slice_state})

    active_slice_summary = str(active_slice.get("summary") or "").strip()
    if progress_status == "repairing" and active_slice_summary:
        active_slice_summary = (
            f"{active_slice_summary} Status: repair in place after a failed pass check; rerun the gate before promotion."
        )
    elif progress_status == "blocked" and active_slice_summary:
        blocked_reason = str(progress_state.get("blocked_reason") or "hard block recorded").strip()
        active_slice_summary = f"{active_slice_summary} Status: blocked. Reason: {blocked_reason}."
    elif progress_status == "stopped" and active_slice_summary:
        stop_reason = str(progress_state.get("stop_reason") or "stop requested").strip()
        active_slice_summary = f"{active_slice_summary} Status: stopped. Reason: {stop_reason}."

    next_step_summary = (
        f"Continue {active_slice_title or 'the active slice'} now, complete its remaining bounded tasks, clear the pass bar, "
        f"then auto-continue into {next_slice_title}."
    )
    if progress_status == "repairing":
        next_step_summary = (
            f"Repair {active_slice_title or 'the active slice'}, rerun the pass bar, and only then auto-promote into {next_slice_title}."
        )
    elif progress_status == "blocked":
        next_step_summary = (
            f"Resolve the block on {active_slice_title or 'the active slice'}, restore trustworthy validation, and then continue into {next_slice_title}."
        )
    elif progress_status == "stopped":
        next_step_summary = (
            f"Hold at {active_slice_title or 'the active slice'} until the stop condition clears, then resume the six-hour loop from this slice."
        )

    progress_summary = (
        f"Cycle {int(progress_state.get('active_cycle', 1) or 1)} running with "
        f"{len(completed_slice_ids)}/{len(hydrated_slices)} slices completed this cycle. "
        f"Active slice: {active_slice_title or 'none'}. Status: {progress_status}."
    )

    hydrated.update(
        {
            "summary": (
                "Natural-language development is running under an autonomy-first policy. "
                f"Current focus: {selected_skill_title or 'the active slice'} so MIM can keep moving "
                "without avoidable approval loops."
            ),
            "selected_skill": selected_skill,
            "selected_skill_id": str(selected_skill.get("skill_id") or hydrated.get("selected_skill_id") or "").strip(),
            "selected_skill_title": selected_skill_title,
            "selected_skill_pass_bar_summary": str(
                active_slice.get("pass_bar_summary") or hydrated.get("selected_skill_pass_bar_summary") or ""
            ).strip(),
            "active_slice": active_slice,
            "active_slice_id": active_slice_id,
            "active_slice_summary": active_slice_summary,
            "next_step_summary": next_step_summary,
            "progress": {
                **progress_state,
                "active_slice_title": active_slice_title,
                "progress_summary": progress_summary,
                "completed_slice_count": len(completed_slice_ids),
                "slice_count": len(hydrated_slices),
            },
            "progress_summary": progress_summary,
            "slices": hydrated_slices,
        }
    )
    metadata_json = hydrated.get("metadata_json", {}) if isinstance(hydrated.get("metadata_json", {}), dict) else {}
    hydrated["metadata_json"] = {
        **metadata_json,
        "active_slice_id": active_slice_id,
        "active_cycle": int(progress_state.get("active_cycle", 1) or 1),
        "progress_status": progress_status,
    }
    return hydrated


async def get_natural_language_development_progress(
    *,
    actor: str,
    source: str,
    slices: list[dict],
    db: AsyncSession,
) -> dict:
    snapshot_scope = _self_evolution_progress_scope(actor=actor, source=source)
    row = await get_state_bus_snapshot(snapshot_scope=snapshot_scope, db=db)
    payload = row.state_payload_json if row is not None and isinstance(row.state_payload_json, dict) else {}
    progress_state = _normalize_natural_language_progress_state(payload=payload, slices=slices)
    progress_state["snapshot_scope"] = snapshot_scope
    progress_state["snapshot"] = to_state_bus_snapshot_out(row) if row is not None else None
    return progress_state


async def reset_natural_language_development_progress(
    *,
    actor: str,
    source: str,
    db: AsyncSession,
) -> dict:
    packet = _build_natural_language_development_packet(snapshot={"status": "active"})
    slices = packet.get("slices", []) if isinstance(packet.get("slices", []), list) else []
    state = _default_natural_language_progress_state(slices=slices)
    snapshot_scope = _self_evolution_progress_scope(actor=actor, source=source)
    event = await append_state_bus_event(
        actor=actor,
        source=source or SELF_EVOLUTION_PROGRESS_SOURCE,
        event_domain=SELF_EVOLUTION_PROGRESS_EVENT_DOMAIN,
        event_type="natural_language_progress_reset",
        stream_key=snapshot_scope,
        payload_json={"progress": state},
        metadata_json={
            "objective173_self_evolution_progress": True,
            "policy_id": SELF_EVOLUTION_PROGRESS_POLICY_ID,
        },
        db=db,
    )
    snapshot = await upsert_state_bus_snapshot(
        actor=actor,
        source=source or SELF_EVOLUTION_PROGRESS_SOURCE,
        snapshot_scope=snapshot_scope,
        state_payload_json=state,
        last_event_id=int(event.id),
        metadata_json={
            "objective173_self_evolution_progress": True,
            "policy_id": SELF_EVOLUTION_PROGRESS_POLICY_ID,
        },
        db=db,
    )
    progress = _normalize_natural_language_progress_state(payload=state, slices=slices)
    progress["snapshot_scope"] = snapshot_scope
    progress["snapshot"] = to_state_bus_snapshot_out(snapshot)
    return progress


async def evaluate_natural_language_development_progress(
    *,
    actor: str,
    source: str,
    slice_id: str,
    outcome_mode: str,
    metrics_json: dict,
    failure_tags: list[str],
    proof_summary: str,
    discovered_skill_candidates: list[str],
    blocked_reason: str,
    metadata_json: dict,
    db: AsyncSession,
) -> dict:
    packet = _build_natural_language_development_packet(snapshot={"status": "active"})
    slices = packet.get("slices", []) if isinstance(packet.get("slices", []), list) else []
    index_by_id = _slice_index_by_id(slices)
    progress_state = await get_natural_language_development_progress(
        actor=actor,
        source=source,
        slices=slices,
        db=db,
    )
    active_slice_id = str(progress_state.get("active_slice_id") or "").strip()
    target_slice_id = str(slice_id or active_slice_id).strip() or active_slice_id
    if target_slice_id != active_slice_id or target_slice_id not in index_by_id:
        raise ValueError("active_slice_mismatch")

    current_slice = slices[index_by_id[target_slice_id]]
    normalized_mode = str(outcome_mode or "auto").strip().lower() or "auto"
    evaluation = evaluate_natural_language_slice_pass(
        slice_payload=current_slice,
        metrics_json=metrics_json if isinstance(metrics_json, dict) else {},
        failure_tags=failure_tags if isinstance(failure_tags, list) else [],
    )
    if normalized_mode == "pass":
        evaluation["passed"] = True
        evaluation["failed_checks"] = []
    elif normalized_mode == "fail":
        evaluation["passed"] = False
        if not evaluation["failed_checks"]:
            evaluation["failed_checks"] = [{"name": "forced_fail", "reasons": ["forced failure outcome"]}]

    next_state = dict(progress_state)
    next_state["evaluation_count"] = int(progress_state.get("evaluation_count", 0) or 0) + 1
    next_state["last_outcome_at"] = datetime.now(timezone.utc).isoformat()
    next_state["last_evaluation"] = {
        **evaluation,
        "proof_summary": str(proof_summary or "").strip(),
        "discovered_skill_candidates": [str(item).strip() for item in discovered_skill_candidates if str(item).strip()],
    }

    if normalized_mode == "blocked":
        outcome = "blocked"
        next_state["status"] = "blocked"
        next_state["last_outcome"] = "blocked"
        next_state["blocked_reason"] = str(blocked_reason or "validation blocked").strip()
        event_type = "natural_language_slice_blocked"
    elif normalized_mode == "stop_requested":
        outcome = "stop_requested"
        next_state["status"] = "stopped"
        next_state["last_outcome"] = "stop_requested"
        next_state["stop_reason"] = str(blocked_reason or "stop requested").strip()
        event_type = "natural_language_progress_stopped"
    elif evaluation.get("passed"):
        outcome = "pass"
        next_index = index_by_id[target_slice_id] + 1
        completed_slice_ids = [
            str(item).strip()
            for item in progress_state.get("completed_slice_ids", [])
            if str(item).strip()
        ]
        if target_slice_id not in completed_slice_ids:
            completed_slice_ids.append(target_slice_id)
        if next_index >= len(slices):
            next_state["completed_cycle_count"] = int(progress_state.get("completed_cycle_count", 0) or 0) + 1
            next_state["active_cycle"] = int(progress_state.get("active_cycle", 1) or 1) + 1
            next_state["completed_slice_ids"] = []
            next_state["active_slice_id"] = str(slices[0].get("slice_id") or "").strip() if slices else ""
        else:
            next_state["completed_slice_ids"] = completed_slice_ids
            next_state["active_slice_id"] = str(slices[next_index].get("slice_id") or "").strip()
        next_state["status"] = "running"
        next_state["last_outcome"] = "pass"
        next_state["promotion_count"] = int(progress_state.get("promotion_count", 0) or 0) + 1
        next_state["blocked_reason"] = ""
        next_state["stop_reason"] = ""
        event_type = "natural_language_slice_passed"
    else:
        outcome = "fail"
        next_state["status"] = "repairing"
        next_state["last_outcome"] = "fail"
        next_state["repair_count"] = int(progress_state.get("repair_count", 0) or 0) + 1
        next_state["blocked_reason"] = ""
        next_state["stop_reason"] = ""
        event_type = "natural_language_slice_failed"

    proof_entry = {
        "slice_id": target_slice_id,
        "outcome": outcome,
        "proof_summary": str(proof_summary or "").strip(),
        "recorded_at": next_state.get("last_outcome_at", ""),
    }
    existing_proof_log = [
        item
        for item in progress_state.get("proof_log", [])
        if isinstance(item, dict)
    ]
    next_state["proof_log"] = [*existing_proof_log, proof_entry][-12:]
    next_state["discovered_skill_candidates"] = list(dict.fromkeys(
        [
            *[
                str(item).strip()
                for item in progress_state.get("discovered_skill_candidates", [])
                if str(item).strip()
            ],
            *[
                str(item).strip()
                for item in discovered_skill_candidates
                if str(item).strip()
            ],
        ]
    ))

    next_state = _normalize_natural_language_progress_state(payload=next_state, slices=slices)
    snapshot_scope = _self_evolution_progress_scope(actor=actor, source=source)
    event = await append_state_bus_event(
        actor=actor,
        source=source or SELF_EVOLUTION_PROGRESS_SOURCE,
        event_domain=SELF_EVOLUTION_PROGRESS_EVENT_DOMAIN,
        event_type=event_type,
        stream_key=snapshot_scope,
        payload_json={
            "progress": next_state,
            "evaluation": next_state.get("last_evaluation", {}),
            "slice": current_slice,
        },
        metadata_json={
            "objective173_self_evolution_progress": True,
            "policy_id": SELF_EVOLUTION_PROGRESS_POLICY_ID,
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        },
        db=db,
    )
    snapshot = await upsert_state_bus_snapshot(
        actor=actor,
        source=source or SELF_EVOLUTION_PROGRESS_SOURCE,
        snapshot_scope=snapshot_scope,
        state_payload_json=next_state,
        last_event_id=int(event.id),
        metadata_json={
            "objective173_self_evolution_progress": True,
            "policy_id": SELF_EVOLUTION_PROGRESS_POLICY_ID,
            **(metadata_json if isinstance(metadata_json, dict) else {}),
        },
        db=db,
    )
    hydrated_packet = _apply_natural_language_progress_to_packet(packet=packet, progress_state=next_state)
    progress = hydrated_packet.get("progress", {}) if isinstance(hydrated_packet.get("progress", {}), dict) else {}
    progress["snapshot_scope"] = snapshot_scope
    progress["snapshot"] = to_state_bus_snapshot_out(snapshot)
    return {
        "outcome": outcome,
        "progress": progress,
        "active_slice": hydrated_packet.get("active_slice", {}),
        "active_slice_summary": str(hydrated_packet.get("active_slice_summary") or "").strip(),
        "next_step_summary": str(hydrated_packet.get("next_step_summary") or "").strip(),
        "evaluation": next_state.get("last_evaluation", {}),
    }


def _build_natural_language_development_slice(
    *,
    slice_index: int,
    total_slices: int,
    title: str,
    objective: str,
    focus_skills: list[dict],
    next_slice_title: str,
    pass_bar_summary: str,
) -> dict:
    focus_titles = [str(skill.get("title") or "").strip() for skill in focus_skills if str(skill.get("title") or "").strip()]
    focus_label = ", ".join(focus_titles) or title
    build_methods = [
        str(method).strip()
        for skill in focus_skills
        for method in (skill.get("build_methods") if isinstance(skill.get("build_methods"), list) else [])
        if str(method).strip()
    ]
    evaluation_methods = [
        str(method).strip()
        for skill in focus_skills
        for method in (skill.get("evaluation_methods") if isinstance(skill.get("evaluation_methods"), list) else [])
        if str(method).strip()
    ]
    task_summaries = [
        ("alignment", f"Frame the slice objective for {title} and restate the pass gate before implementation.", "The active slice objective and pass gate are explicit in the self-evolution loop."),
        ("implementation", f"Apply build method 1 for {focus_label}: {build_methods[0] if len(build_methods) > 0 else objective}", "One concrete implementation seam for the slice is updated."),
        ("implementation", f"Apply build method 2 for {focus_label}: {build_methods[1] if len(build_methods) > 1 else 'Harden the weakest continuity seam exposed by the current slice.'}", "A second bounded improvement is added for the active slice."),
        ("implementation", f"Apply build method 3 for {focus_label}: {build_methods[2] if len(build_methods) > 2 else 'Extend the next-step conversation path so the active slice stays visible after each implementation.'}", "The slice remains visible in direct-answer and next-step surfaces."),
        ("traceability", f"Mirror the active {title} slice, proof target, and next-step cadence into operator-visible state.", "UI/operator state exposes the current slice and proof target."),
        ("validation", f"Run focused validation for {focus_label}: {evaluation_methods[0] if len(evaluation_methods) > 0 else 'Run focused lifecycle and gateway coverage for the active slice.'}", "Focused validation completes with trustworthy results for the slice."),
        ("validation", f"Run secondary evaluation for {focus_label}: {evaluation_methods[1] if len(evaluation_methods) > 1 else 'Review top failures and bucket averages to remove drift before promotion.'}", "Secondary evaluation confirms no hidden regression blocks promotion."),
        ("continuity", f"Probe the constant what's-next framework live so {title} still advances after a short implementation acknowledgement.", "Live or simulated follow-up continuity stays attached to the active slice."),
        ("discovery", f"Record any new skill candidates or failure tags discovered during {title} and queue them behind the current curriculum.", "Newly discovered skills are captured without stalling the active slice."),
        ("autonomy", f"If the {title} pass bar clears, auto-promote immediately to {next_slice_title or 'the next ranked slice'} without requesting operator approval.", "Promotion happens automatically on pass, or the slice recycles into one bounded repair on fail."),
    ]
    tasks = [
        _natural_language_development_task(
            task_id=f"task_{task_index:02d}",
            summary=summary,
            category=category,
            proof_target=proof_target,
        )
        for task_index, (category, summary, proof_target) in enumerate(task_summaries, start=1)
    ]
    summary = (
        f"Slice {slice_index}/{total_slices}: {title}. Duration: 60 minutes with 10 bounded tasks. "
        f"Focus: {focus_label}. On pass, MIM continues directly to {next_slice_title or 'the next ranked slice'}."
    )
    return {
        "slice_id": f"slice_{slice_index:02d}",
        "slice_index": int(slice_index),
        "title": title,
        "duration_minutes": 60,
        "objective": objective,
        "focus_skill_ids": [str(skill.get("skill_id") or "").strip() for skill in focus_skills],
        "focus_skill_titles": focus_titles,
        "pass_metrics": _natural_language_slice_pass_metrics(focus_skills=focus_skills),
        "task_count": len(tasks),
        "tasks": tasks,
        "pass_bar_summary": pass_bar_summary,
        "continuation_trigger": "auto_continue_on_pass",
        "next_slice_title": next_slice_title,
        "summary": summary,
    }


def _build_natural_language_development_packet(*, snapshot: dict) -> dict:
    skills = [
        _natural_language_development_skill(
            skill_id="intention_grounding",
            title="Intentions",
            priority_band="immediate",
            policy_score=1.0,
            why_now=(
                "Autonomous next-step selection is only trustworthy when MIM consistently grounds the"
                " latest operator intent without falling back to repeated approval prompts."
            ),
            development_goal=(
                "Resolve the active operator intent on the current turn and preserve it across short"
                " follow-ups so downstream next-step selection is reliable."
            ),
            build_methods=[
                "Add conversation simulations that pivot between status, priority, correction, and execution-planning asks in the same session.",
                "Expand focused gateway tests for short approvals, corrections, and scope changes so intent handoff stays explicit.",
                "Use live gateway probes after each change to confirm the direct answer follows the latest instruction rather than stale thread state.",
            ],
            evaluation_methods=[
                "Run conversation_eval_runner.py with stage=smoke and bucket allowlists for continuity and understanding scenarios.",
                "Review summary.top_failures and bucket_average for context drift, stale instruction, and question-answering regressions.",
            ],
            pass_metrics=[
                _natural_language_development_metric(
                    name="relevance",
                    minimum=0.85,
                    failure_absent=["context_drift", "new_intent_not_followed", "question_not_answered"],
                ),
                _natural_language_development_metric(name="task_completion", minimum=0.8),
                _natural_language_development_metric(name="overall", minimum=0.82),
            ],
        ),
        _natural_language_development_skill(
            skill_id="decision_flow_control",
            title="Decision Flow",
            priority_band="immediate",
            policy_score=0.98,
            why_now=(
                "The weekly objective is to remove the operator as the routine approval bottleneck, which"
                " requires a repeatable policy for choosing the next bounded step."
            ),
            development_goal=(
                "Choose a bounded next action, explain why it is next, and keep moving when state is"
                " sufficient instead of asking for avoidable confirmation."
            ),
            build_methods=[
                "Teach self-evolution and conversation surfaces to expose one concrete next step, one reason, and one proof target.",
                "Add regression cases where MIM must choose between status review, planning, recovery, or implementation follow-through.",
                "Verify that direct-answer responses for priority and next-step prompts remain evidence-rich under the communication composer.",
            ],
            evaluation_methods=[
                "Run conversation_eval_runner.py with stage=expanded after decision-flow changes.",
                "Inspect whether summary.top_failures stays clear of explicit_request_missed, stale_instruction_followed, and unclear_state_transition.",
            ],
            pass_metrics=[
                _natural_language_development_metric(
                    name="overall",
                    minimum=0.85,
                    stage="expanded",
                    failure_absent=["explicit_request_missed", "stale_instruction_followed", "unclear_state_transition"],
                ),
                _natural_language_development_metric(name="task_completion", minimum=0.85, stage="expanded"),
                _natural_language_development_metric(name="initiative", minimum=0.8, stage="expanded"),
            ],
        ),
        _natural_language_development_skill(
            skill_id="planning_continuity",
            title="Planning",
            priority_band="immediate",
            policy_score=0.96,
            why_now=(
                "Once MIM can identify intent and pick the next step, it needs to hold multi-step plans"
                " together across follow-ups without scope drift."
            ),
            development_goal=(
                "Turn the chosen next step into a bounded implementation plan that survives short"
                " acknowledgements, restarts, and follow-up questions."
            ),
            build_methods=[
                "Add planning-focused conversation scenarios that require a next step, pass bar, and validation method in the same answer.",
                "Extend self-evolution briefing outputs so the selected language-development skill carries build and test instructions.",
                "Probe live sessions with 'what is next', 'continue', and 'execute that plan' turns to confirm continuity.",
            ],
            evaluation_methods=[
                "Run conversation_eval_runner.py with stage=expanded and planning-heavy buckets.",
                "Review smoothness, non_repetition, and bucket-average changes to catch plan resets and repeated clarifiers.",
            ],
            pass_metrics=[
                _natural_language_development_metric(name="overall", minimum=0.84, stage="expanded"),
                _natural_language_development_metric(name="smoothness", minimum=0.8, stage="expanded"),
                _natural_language_development_metric(
                    name="non_repetition",
                    minimum=0.8,
                    stage="expanded",
                    failure_absent=["repeated_clarifier_pattern", "response_loop_risk"],
                ),
            ],
        ),
        _natural_language_development_skill(
            skill_id="escalation_and_recovery",
            title="Escalation And Recovery",
            priority_band="immediate",
            policy_score=0.94,
            why_now=(
                "The user explicitly wants MIM to recover from stalls, log mistakes, and keep going with"
                " a defensible next action."
            ),
            development_goal=(
                "Detect stalls or uncertainty, acknowledge the issue, recover into one bounded next step,"
                " and preserve safety boundaries while doing it."
            ),
            build_methods=[
                "Add ambiguous, misheard, and correction-heavy scenarios where MIM must recover without looping.",
                "Tighten responses so recovery language includes the observed issue, the corrected direction, and the immediate next action.",
                "Exercise live probes for 'you are off track', 'that is wrong', and 'what is next now' sequences.",
            ],
            evaluation_methods=[
                "Run conversation_eval_runner.py with stage=expanded before and after recovery-logic changes.",
                "Fail the slice if missing_safety_boundary, mishear_recovery_failed, or clarification_spam appears in top failures.",
            ],
            pass_metrics=[
                _natural_language_development_metric(
                    name="safety",
                    minimum=1.0,
                    stage="expanded",
                    failure_absent=["missing_safety_boundary", "mishear_recovery_failed", "clarification_spam"],
                ),
                _natural_language_development_metric(name="overall", minimum=0.82, stage="expanded"),
            ],
        ),
        _natural_language_development_skill(
            skill_id="accountability_trace",
            title="Accountability",
            priority_band="follow_on",
            policy_score=0.9,
            why_now="Autonomy improves only if MIM can explain which decision it made and why the chosen path was bounded.",
            development_goal="Expose the active decision, rationale, and validation expectation so operator review is lightweight instead of supervisory.",
            build_methods=[
                "Mirror active decision rationale and pass bars into operator-visible and conversation-safe state.",
                "Add tests that ensure decision summaries remain aligned with the selected self-evolution target.",
                "Check live status answers for a clear why/next/proof structure.",
            ],
            evaluation_methods=[
                "Use conversation_eval_runner.py stage=expanded and inspect question-answer quality around 'why' and 'what is next' asks.",
            ],
            pass_metrics=[
                _natural_language_development_metric(name="overall", minimum=0.83, stage="expanded"),
                _natural_language_development_metric(name="task_completion", minimum=0.82, stage="expanded"),
            ],
        ),
        _natural_language_development_skill(
            skill_id="reporting_clarity",
            title="Reporting",
            priority_band="follow_on",
            policy_score=0.88,
            why_now="MIM-directed work still needs concise status packets that explain progress, blockers, and proof without over-explaining.",
            development_goal="Return concise status, priority, and completion summaries that are direct, grounded, and easy to act on.",
            build_methods=[
                "Add scenario coverage for catch-up, return-briefing, and status-report prompts.",
                "Constrain long responses with direct-answer-first packaging and explicit next-step lines.",
                "Use live probes on status and summary queries after each change.",
            ],
            evaluation_methods=[
                "Run conversation_eval_runner.py with stage=smoke and expanded, focusing on brevity and answer_plainly behaviors.",
            ],
            pass_metrics=[
                _natural_language_development_metric(name="brevity", minimum=0.85),
                _natural_language_development_metric(name="overall", minimum=0.82),
            ],
        ),
        _natural_language_development_skill(
            skill_id="leadership_tone",
            title="Leadership Tone",
            priority_band="follow_on",
            policy_score=0.86,
            why_now="A stronger leadership voice matters after intent, planning, and recovery are grounded enough to support it.",
            development_goal="Speak with direct, calm leadership that guides the next bounded action without sounding vague or performative.",
            build_methods=[
                "Audit repeated soft or hedged phrasing in status and planning replies.",
                "Add style-sensitive conversation scenarios that reward direct guidance over filler.",
                "Review live answers for concise authority without overclaiming execution."
            ],
            evaluation_methods=[
                "Use conversation_eval_runner.py summary plus manual inspection of a small live probe set.",
            ],
            pass_metrics=[
                _natural_language_development_metric(name="smoothness", minimum=0.82),
                _natural_language_development_metric(name="overall", minimum=0.82),
            ],
        ),
        _natural_language_development_skill(
            skill_id="initiative_with_boundaries",
            title="Initiative",
            priority_band="follow_on",
            policy_score=0.84,
            why_now="Proactive suggestions help only after bounded decision flow is dependable and traceable.",
            development_goal="Offer the next bounded idea or warning proactively without hallucinating actions or bypassing approval rules.",
            build_methods=[
                "Add scenarios where MIM should volunteer one useful next step after answering the direct question.",
                "Keep proactive guidance scoped to review, inspect, refresh, or implementable bounded work.",
                "Verify live probes do not overstate completed actions.",
            ],
            evaluation_methods=[
                "Run conversation_eval_runner.py with expanded scenarios and inspect initiative plus safety deltas.",
            ],
            pass_metrics=[
                _natural_language_development_metric(
                    name="initiative",
                    minimum=0.8,
                    stage="expanded",
                    failure_absent=["hallucinated_action"],
                ),
                _natural_language_development_metric(name="overall", minimum=0.82, stage="expanded"),
            ],
        ),
        _natural_language_development_skill(
            skill_id="after_action_reflection",
            title="Afterthought",
            priority_band="later",
            policy_score=0.82,
            why_now="Reflection adds leverage after the core loop can already move, recover, and report reliably.",
            development_goal="Briefly capture what went wrong, what improved, and what the next refinement should target after each bounded slice.",
            build_methods=[
                "Add post-task reflection fields to self-evolution summaries where the loop already has evidence.",
                "Create tests for concise mistake logging and bounded next-refinement suggestions.",
                "Check that reflections point to one fixable behavior instead of generic prose.",
            ],
            evaluation_methods=[
                "Use regression reports and self-evolution briefings to confirm failure tags are turned into explicit next improvements.",
            ],
            pass_metrics=[
                _natural_language_development_metric(name="overall", minimum=0.8, stage="expanded"),
            ],
        ),
        _natural_language_development_skill(
            skill_id="memory_usage",
            title="Memory Usage",
            priority_band="later",
            policy_score=0.8,
            why_now="Cross-session memory matters, but it compounds earlier gains in intent grounding and planning rather than replacing them.",
            development_goal="Carry operator identity, preferences, and active commitments across sessions without drifting from the live request.",
            build_methods=[
                "Expand memory-focused scenarios for remembered people, preferences, and prior commitments.",
                "Add tests that verify remembered preferences alter brevity and style in later turns.",
                "Probe live sessions that return after a break and ask for direct continuation.",
            ],
            evaluation_methods=[
                "Run conversation_eval_runner.py with continuity and memory scenarios in smoke and expanded stages.",
            ],
            pass_metrics=[
                _natural_language_development_metric(
                    name="overall",
                    minimum=0.82,
                    stage="expanded",
                    failure_absent=["memory_preference_not_applied", "context_drift"],
                ),
            ],
        ),
    ]

    selected_skill = skills[0]
    follow_on_skills = [skill for skill in skills[1:4]]
    slice_titles = [
        "Intentions Stabilization",
        "Decision Flow Control",
        "Planning Continuity",
        "Escalation And Recovery",
        "Accountability And Reporting",
        "Initiative, Reflection, And Memory Expansion",
    ]
    summary = (
        "Natural-language development is running under an autonomy-first policy. "
        f"Current focus: {selected_skill['title']} so MIM can resolve live intent before selecting "
        "and executing the next bounded step without avoidable approval loops."
    )
    pass_bar_summary = (
        "Pass bar: smoke overall >= 0.82, relevance >= 0.85, task_completion >= 0.80, and no context_drift, "
        "new_intent_not_followed, or question_not_answered failures in the selected skill slice."
    )
    next_step_summary = (
        "Start slice 1/6 now: Intentions Stabilization. Complete its 10 bounded tasks, clear the "
        "pass bar, then auto-continue into slice 2/6: Decision Flow Control without waiting for operator approval."
    )
    continuation_policy_summary = (
        "No operator interaction is required during the active six-hour run. When the current slice "
        "passes, MIM selects the next 10-task slice immediately and continues until stopped, blocked by "
        "hard safety, or unable to trust the validation state."
    )
    whats_next_framework_summary = (
        "Finish the active 10-task slice, run the pass check, record proof plus any new skill candidates, "
        "choose the next ranked slice, and start it immediately."
    )
    six_hour_plan_summary = (
        "Six one-hour slices are queued with 10 bounded tasks each so the natural-language curriculum keeps "
        "moving with no idle time between passing slices."
    )
    slices = [
        _build_natural_language_development_slice(
            slice_index=1,
            total_slices=6,
            title=slice_titles[0],
            objective="Ground live operator intent and preserve it across short follow-ups before any downstream next-step decision.",
            focus_skills=[skills[0]],
            next_slice_title=slice_titles[1],
            pass_bar_summary=pass_bar_summary,
        ),
        _build_natural_language_development_slice(
            slice_index=2,
            total_slices=6,
            title=slice_titles[1],
            objective="Select one bounded next action after each completed implementation and explain why it is next.",
            focus_skills=[skills[1]],
            next_slice_title=slice_titles[2],
            pass_bar_summary=pass_bar_summary,
        ),
        _build_natural_language_development_slice(
            slice_index=3,
            total_slices=6,
            title=slice_titles[2],
            objective="Keep the plan coherent across continue, execute, and what-is-next follow-ups with no approval drift.",
            focus_skills=[skills[2]],
            next_slice_title=slice_titles[3],
            pass_bar_summary=pass_bar_summary,
        ),
        _build_natural_language_development_slice(
            slice_index=4,
            total_slices=6,
            title=slice_titles[3],
            objective="Recover from mistakes or stalls, then move directly into the next bounded action without idle time.",
            focus_skills=[skills[3]],
            next_slice_title=slice_titles[4],
            pass_bar_summary=pass_bar_summary,
        ),
        _build_natural_language_development_slice(
            slice_index=5,
            total_slices=6,
            title=slice_titles[4],
            objective="Expose proof, status, and operator-visible command context while keeping the current slice moving.",
            focus_skills=[skills[4], skills[5]],
            next_slice_title=slice_titles[5],
            pass_bar_summary=pass_bar_summary,
        ),
        _build_natural_language_development_slice(
            slice_index=6,
            total_slices=6,
            title=slice_titles[5],
            objective="Close the loop with proactive next-step guidance, concise reflection, memory carry-over, and new-skill discovery for the next cycle.",
            focus_skills=[skills[6], skills[7], skills[8], skills[9]],
            next_slice_title=slice_titles[0],
            pass_bar_summary=pass_bar_summary,
        ),
    ]
    active_slice = slices[0]
    whats_next_framework = [
        {
            "step": 1,
            "name": "finish_active_slice",
            "summary": "Complete the current 10-task slice for the selected skill.",
        },
        {
            "step": 2,
            "name": "run_pass_check",
            "summary": "Run the defined pass bar and trust the result only if the validation state is clean.",
        },
        {
            "step": 3,
            "name": "record_proof_and_discovery",
            "summary": "Store proof plus any newly discovered skills or failure tags before promotion.",
        },
        {
            "step": 4,
            "name": "select_next_slice",
            "summary": "Choose the next ranked 10-task slice from the six-hour plan without asking for routine approval.",
        },
        {
            "step": 5,
            "name": "continue_immediately",
            "summary": "Start the next slice immediately and keep going until stopped or blocked by a hard stop condition.",
        },
    ]

    return {
        "policy_id": "natural_language_development_v1",
        "policy_summary": (
            "Pick the highest-leverage natural-language skill that reduces approval friction first, "
            "prove it with the conversation evaluation harness, and only then advance to the next skill."
        ),
        "summary": summary,
        "six_hour_plan_summary": six_hour_plan_summary,
        "next_step_summary": next_step_summary,
        "active_slice": active_slice,
        "active_slice_id": str(active_slice.get("slice_id") or "").strip(),
        "active_slice_summary": str(active_slice.get("summary") or "").strip(),
        "slices": slices,
        "continuation_policy": {
            "policy_id": "continuous_autonomy_v1",
            "operator_interaction_mode": "none_until_stopped",
            "auto_continue_on_pass": True,
            "no_idle_time": True,
            "decision_authority": "mim_self_evolution",
            "continue_until_stopped": True,
            "stop_conditions": [
                "operator_stop_requested",
                "hard_safety_block",
                "untrustworthy_validation_state",
            ],
            "summary": continuation_policy_summary,
        },
        "continuation_policy_summary": continuation_policy_summary,
        "whats_next_framework": whats_next_framework,
        "whats_next_framework_summary": whats_next_framework_summary,
        "selected_skill": selected_skill,
        "selected_skill_id": str(selected_skill.get("skill_id") or "").strip(),
        "selected_skill_title": str(selected_skill.get("title") or "").strip(),
        "selected_skill_pass_bar_summary": pass_bar_summary,
        "skills": skills,
        "wave_order": {
            "immediate": [skill["skill_id"] for skill in skills[:4]],
            "follow_on": [skill["skill_id"] for skill in skills[4:8]],
            "later": [skill["skill_id"] for skill in skills[8:]],
        },
        "snapshot_status": str(snapshot.get("status") or "").strip(),
        "metadata_json": {
            "objective171_self_evolution_natural_language_development": True,
            "selected_skill_id": str(selected_skill.get("skill_id") or "").strip(),
            "skill_count": len(skills),
            "slice_count": len(slices),
            "auto_continue_on_pass": True,
        },
    }


async def build_self_evolution_next_action(
    *,
    actor: str,
    source: str,
    refresh: bool,
    lookback_hours: int,
    min_occurrence_count: int,
    auto_experiment_limit: int,
    limit: int,
    db: AsyncSession,
) -> dict:
    snapshot = await build_self_evolution_snapshot(
        actor=actor,
        source=source,
        refresh=refresh,
        lookback_hours=lookback_hours,
        min_occurrence_count=min_occurrence_count,
        auto_experiment_limit=auto_experiment_limit,
        limit=limit,
        db=db,
    )

    backlog = snapshot.get("backlog", []) if isinstance(snapshot.get("backlog", []), list) else []
    recommendations = (
        snapshot.get("recommendations", []) if isinstance(snapshot.get("recommendations", []), list) else []
    )
    proposal_counts = snapshot.get("proposal_counts", {}) if isinstance(snapshot.get("proposal_counts", {}), dict) else {}
    recommendation_counts = (
        snapshot.get("recommendation_counts", {})
        if isinstance(snapshot.get("recommendation_counts", {}), dict)
        else {}
    )
    backlog_counts = snapshot.get("backlog_counts", {}) if isinstance(snapshot.get("backlog_counts", {}), dict) else {}
    governance_counts = (
        snapshot.get("governance_decision_counts", {})
        if isinstance(snapshot.get("governance_decision_counts", {}), dict)
        else {}
    )

    top_backlog = backlog[0] if backlog and isinstance(backlog[0], dict) else {}
    top_recommendation = recommendations[0] if recommendations and isinstance(recommendations[0], dict) else {}

    operator_review_count = int(governance_counts.get("request_operator_review", 0) or 0)
    open_recommendations = int(recommendation_counts.get("proposed", 0) or 0)
    queued_backlog = int(backlog_counts.get("queued", 0) or 0)
    open_proposals = int(proposal_counts.get("proposed", 0) or 0)

    if operator_review_count > 0 and int(top_backlog.get("recommendation_id", 0) or 0) > 0:
        recommendation_id = int(top_backlog.get("recommendation_id", 0) or 0)
        recommendation_row = await get_improvement_recommendation(
            recommendation_id=recommendation_id,
            db=db,
        )
        recommendation = (
            await to_improvement_recommendation_out_resolved(row=recommendation_row, db=db)
            if recommendation_row is not None
            else top_recommendation
        )
        proposal_type = str(top_backlog.get("proposal_type", "") or "")
        return {
            "decision": _decision_payload(
                decision_type="approve_ranked_recommendation",
                priority="high",
                rationale=(
                    "The current self-evolution loop is blocked behind operator review on the highest-ranked "
                    "backlog item, so the next bounded action is to review the linked recommendation."
                ),
                target_kind="recommendation",
                target_id=recommendation_id,
                action_method="POST",
                action_path=f"/improvement/recommendations/{recommendation_id}/approve",
                action_payload={
                    "actor": actor,
                    "reason": "objective165 guided approval for the highest-ranked self-evolution backlog item",
                    "metadata_json": {
                        "objective165_self_evolution_next_action": True,
                        "source": source,
                    },
                },
                summary=(
                    f"Review recommendation {recommendation_id} for the top-ranked {proposal_type or 'improvement'} "
                    "item before continuing the loop."
                ),
                snapshot=snapshot,
                metadata_json={
                    "objective165_self_evolution_next_action": True,
                    "operator_review_count": operator_review_count,
                    "recommendation": recommendation,
                    "backlog_item": top_backlog,
                },
            )
        }

    if open_recommendations > 0 and int(top_recommendation.get("recommendation_id", 0) or 0) > 0:
        recommendation_id = int(top_recommendation.get("recommendation_id", 0) or 0)
        recommendation_type = str(top_recommendation.get("recommendation_type", "") or "")
        return {
            "decision": _decision_payload(
                decision_type="review_open_recommendation",
                priority="medium",
                rationale=(
                    "Open recommendations already exist, so the next bounded action is to review the newest "
                    "recommendation before generating additional loop pressure."
                ),
                target_kind="recommendation",
                target_id=recommendation_id,
                action_method="GET",
                action_path=f"/improvement/recommendations/{recommendation_id}",
                action_payload={},
                summary=(
                    f"Inspect open recommendation {recommendation_id} ({recommendation_type or 'pending'}) "
                    "before creating more backlog churn."
                ),
                snapshot=snapshot,
                metadata_json={
                    "objective165_self_evolution_next_action": True,
                    "open_recommendations": open_recommendations,
                    "recommendation": top_recommendation,
                },
            )
        }

    if queued_backlog > 0 and int(top_backlog.get("improvement_id", 0) or 0) > 0:
        improvement_id = int(top_backlog.get("improvement_id", 0) or 0)
        return {
            "decision": _decision_payload(
                decision_type="inspect_ranked_backlog_item",
                priority="medium",
                rationale=(
                    "There is ranked backlog pressure but no open operator-review recommendation at the top, so the "
                    "next bounded action is to inspect the highest-priority backlog item."
                ),
                target_kind="backlog_item",
                target_id=improvement_id,
                action_method="GET",
                action_path=f"/improvement/backlog/{improvement_id}",
                action_payload={},
                summary=(
                    f"Inspect backlog item {improvement_id} with priority score "
                    f"{float(top_backlog.get('priority_score', 0.0) or 0.0):.4f}."
                ),
                snapshot=snapshot,
                metadata_json={
                    "objective165_self_evolution_next_action": True,
                    "queued_backlog": queued_backlog,
                    "backlog_item": top_backlog,
                },
            )
        }

    if open_proposals > 0:
        return {
            "decision": _decision_payload(
                decision_type="generate_recommendations",
                priority="medium",
                rationale=(
                    "Open proposals exist without enough downstream recommendation pressure, so the next bounded action "
                    "is to generate recommendations from the current proposal set."
                ),
                target_kind="proposal_batch",
                target_id=None,
                action_method="POST",
                action_path="/improvement/recommendations/generate",
                action_payload={
                    "actor": actor,
                    "source": source,
                    "lookback_hours": int(lookback_hours),
                    "min_occurrence_count": int(min_occurrence_count),
                    "max_recommendations": max(1, min(10, int(limit))),
                    "include_existing_open_proposals": True,
                    "metadata_json": {
                        "objective165_self_evolution_next_action": True,
                        "source": source,
                    },
                },
                summary=(
                    f"Generate recommendations for {open_proposals} open proposal(s) to keep the self-evolution "
                    "loop moving into governed review."
                ),
                snapshot=snapshot,
                metadata_json={
                    "objective165_self_evolution_next_action": True,
                    "open_proposals": open_proposals,
                },
            )
        }

    return {
        "decision": _decision_payload(
            decision_type="refresh_self_evolution_state",
            priority="low",
            rationale=(
                "No active proposal, recommendation, or backlog pressure is currently visible, so the next bounded "
                "action is to refresh the self-evolution loop state."
            ),
            target_kind="self_evolution",
            target_id=None,
            action_method="GET",
            action_path="/improvement/self-evolution",
            action_payload={
                "refresh": True,
                "actor": actor,
                "source": source,
                "lookback_hours": int(lookback_hours),
                "min_occurrence_count": int(min_occurrence_count),
                "auto_experiment_limit": int(auto_experiment_limit),
                "limit": int(limit),
            },
            summary="Refresh the self-evolution snapshot to look for new governed improvement pressure.",
            snapshot=snapshot,
            metadata_json={
                "objective165_self_evolution_next_action": True,
            },
        )
    }


async def build_self_evolution_briefing(
    *,
    actor: str,
    source: str,
    refresh: bool,
    lookback_hours: int,
    min_occurrence_count: int,
    auto_experiment_limit: int,
    limit: int,
    db: AsyncSession,
) -> dict:
    decision_result = await build_self_evolution_next_action(
        actor=actor,
        source=source,
        refresh=refresh,
        lookback_hours=lookback_hours,
        min_occurrence_count=min_occurrence_count,
        auto_experiment_limit=auto_experiment_limit,
        limit=limit,
        db=db,
    )
    decision = decision_result.get("decision", {}) if isinstance(decision_result, dict) else {}
    target_kind = str(decision.get("target_kind", "") or "")
    target_id = decision.get("target_id")

    target_payload: dict = {
        "target_kind": target_kind,
        "target_id": target_id,
        "proposal": None,
        "recommendation": None,
        "backlog_item": None,
    }

    if target_kind == "recommendation" and int(target_id or 0) > 0:
        recommendation_row = await get_improvement_recommendation(
            recommendation_id=int(target_id or 0),
            db=db,
        )
        if recommendation_row is not None:
            target_payload["recommendation"] = await to_improvement_recommendation_out_resolved(
                row=recommendation_row,
                db=db,
            )
            proposal_row = await get_improvement_proposal(
                proposal_id=int(recommendation_row.proposal_id),
                db=db,
            )
            if proposal_row is not None:
                target_payload["proposal"] = to_improvement_proposal_out(proposal_row)

    if target_kind == "backlog_item" and int(target_id or 0) > 0:
        backlog_row = await get_improvement_backlog_item(
            backlog_id=int(target_id or 0),
            db=db,
        )
        if backlog_row is not None:
            target_payload["backlog_item"] = to_improvement_backlog_out(backlog_row)
            proposal_row = await get_improvement_proposal(
                proposal_id=int(backlog_row.proposal_id),
                db=db,
            )
            if proposal_row is not None:
                target_payload["proposal"] = to_improvement_proposal_out(proposal_row)
            if backlog_row.recommendation_id is not None:
                recommendation_row = await get_improvement_recommendation(
                    recommendation_id=int(backlog_row.recommendation_id),
                    db=db,
                )
                if recommendation_row is not None:
                    target_payload["recommendation"] = await to_improvement_recommendation_out_resolved(
                        row=recommendation_row,
                        db=db,
                    )

    snapshot = await build_self_evolution_snapshot(
        actor=actor,
        source=source,
        refresh=False,
        lookback_hours=lookback_hours,
        min_occurrence_count=min_occurrence_count,
        auto_experiment_limit=auto_experiment_limit,
        limit=limit,
        db=db,
    )
    natural_language_development = _build_natural_language_development_packet(snapshot=snapshot)
    natural_language_progress = await get_natural_language_development_progress(
        actor=actor,
        source=source,
        slices=natural_language_development.get("slices", []) if isinstance(natural_language_development.get("slices", []), list) else [],
        db=db,
    )
    natural_language_development = _apply_natural_language_progress_to_packet(
        packet=natural_language_development,
        progress_state=natural_language_progress,
    )

    return {
        "briefing": {
            "snapshot": snapshot,
            "decision": decision,
            "target": target_payload,
            "natural_language_development": natural_language_development,
            "metadata_json": {
                "objective166_self_evolution_briefing": True,
                "objective171_self_evolution_natural_language_development": True,
                "actor": actor,
                "source": source,
            },
            "created_at": datetime.now(timezone.utc),
        }
    }