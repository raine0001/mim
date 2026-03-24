from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.execution_truth_service import summarize_execution_truth
from core.models import ConstraintEvaluation, ExecutionJournal, WorkspaceAutonomyBoundaryProfile, WorkspaceDevelopmentPattern, WorkspaceInterruptionEvent, WorkspaceMonitoringState, WorkspacePolicyExperiment, WorkspaceProposal, WorkspaceReplanSignal
from core.models import CapabilityExecution


PROFILE_STATUSES = {
    "evaluated",
    "applied",
    "rejected",
}
AUTONOMY_LEVELS = [
    "manual_only",
    "operator_required",
    "bounded_auto",
    "trusted_auto",
]


def _bounded(value: float, *, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _default_autonomy() -> dict:
    return {
        "auto_execution_enabled": True,
        "force_manual_approval": False,
        "max_auto_actions_per_minute": 6,
        "max_auto_tasks_per_window": 6,
        "auto_window_seconds": 60,
        "cooldown_between_actions_seconds": 5,
        "capability_cooldown_seconds": {},
        "zone_action_limits": {},
        "restricted_zones": [],
        "auto_safe_confidence_threshold": 0.8,
        "auto_preferred_confidence_threshold": 0.7,
        "low_risk_score_max": 0.3,
        "max_autonomy_retries": 1,
        "recent_auto_actions": [],
    }


def _hard_ceiling_defaults() -> dict:
    return {
        "human_safety": True,
        "legality": True,
        "system_integrity": True,
    }


def _autonomy_from_monitoring(row: WorkspaceMonitoringState | None) -> dict:
    if not row:
        return _default_autonomy()
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    raw = metadata.get("autonomy", {}) if isinstance(metadata.get("autonomy", {}), dict) else {}
    return {
        **_default_autonomy(),
        **raw,
    }


def _current_level_from_autonomy(boundaries: dict) -> str:
    if not bool(boundaries.get("auto_execution_enabled", True)):
        return "manual_only"
    if bool(boundaries.get("force_manual_approval", False)):
        return "operator_required"
    max_auto = int(boundaries.get("max_auto_tasks_per_window", 6) or 6)
    low_risk = float(boundaries.get("low_risk_score_max", 0.3) or 0.3)
    if max_auto <= 5 or low_risk <= 0.32:
        return "bounded_auto"
    return "trusted_auto"


def _autonomy_for_level(current: dict, level: str) -> dict:
    out = {
        **current,
    }
    if level == "manual_only":
        out["auto_execution_enabled"] = False
        out["force_manual_approval"] = True
        out["max_auto_tasks_per_window"] = max(1, min(2, int(out.get("max_auto_tasks_per_window", 2) or 2)))
        out["max_auto_actions_per_minute"] = max(1, min(2, int(out.get("max_auto_actions_per_minute", 2) or 2)))
        out["cooldown_between_actions_seconds"] = max(8, int(out.get("cooldown_between_actions_seconds", 8) or 8))
        out["low_risk_score_max"] = min(0.22, float(out.get("low_risk_score_max", 0.22) or 0.22))
    elif level == "operator_required":
        out["auto_execution_enabled"] = True
        out["force_manual_approval"] = True
        out["max_auto_tasks_per_window"] = max(1, min(3, int(out.get("max_auto_tasks_per_window", 3) or 3)))
        out["max_auto_actions_per_minute"] = max(1, min(3, int(out.get("max_auto_actions_per_minute", 3) or 3)))
        out["cooldown_between_actions_seconds"] = max(7, int(out.get("cooldown_between_actions_seconds", 7) or 7))
        out["low_risk_score_max"] = min(0.24, float(out.get("low_risk_score_max", 0.24) or 0.24))
    elif level == "bounded_auto":
        out["auto_execution_enabled"] = True
        out["force_manual_approval"] = False
        out["max_auto_tasks_per_window"] = max(3, min(8, int(out.get("max_auto_tasks_per_window", 6) or 6)))
        out["max_auto_actions_per_minute"] = max(3, min(8, int(out.get("max_auto_actions_per_minute", 6) or 6)))
        out["cooldown_between_actions_seconds"] = max(3, min(8, int(out.get("cooldown_between_actions_seconds", 5) or 5)))
        out["low_risk_score_max"] = max(0.28, min(0.36, float(out.get("low_risk_score_max", 0.3) or 0.3)))
    else:
        out["auto_execution_enabled"] = True
        out["force_manual_approval"] = False
        out["max_auto_tasks_per_window"] = max(6, min(20, int(out.get("max_auto_tasks_per_window", 10) or 10)))
        out["max_auto_actions_per_minute"] = max(6, min(20, int(out.get("max_auto_actions_per_minute", 10) or 10)))
        out["cooldown_between_actions_seconds"] = max(0, min(5, int(out.get("cooldown_between_actions_seconds", 2) or 2)))
        out["low_risk_score_max"] = max(0.34, min(0.5, float(out.get("low_risk_score_max", 0.4) or 0.4)))
    return out


def _shift_level(level: str, delta: int) -> str:
    try:
        index = AUTONOMY_LEVELS.index(level)
    except ValueError:
        index = AUTONOMY_LEVELS.index("operator_required")
    next_index = max(0, min(len(AUTONOMY_LEVELS) - 1, index + int(delta)))
    return AUTONOMY_LEVELS[next_index]


def _effective_hard_ceiling(overrides: dict) -> dict:
    base = _hard_ceiling_defaults()
    if isinstance(overrides, dict):
        for key in base:
            if key in overrides:
                base[key] = bool(overrides.get(key))
    return base


def _quality_from_sample(sample_count: int, min_samples: int) -> float:
    return _bounded(float(sample_count) / float(max(1, min_samples)))


def _store_autonomy_to_monitoring(row: WorkspaceMonitoringState, autonomy_state: dict) -> None:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    row.metadata_json = {
        **metadata,
        "autonomy": autonomy_state,
    }


def _recommended_boundaries(
    *,
    current: dict,
    current_level: str,
    sample_count: int,
    min_samples: int,
    success_rate: float,
    escalation_rate: float,
    interruption_rate: float,
    retry_rate: float,
    memory_delta_rate: float,
    override_rate: float,
    replan_rate: float,
    environment_stability: float,
    development_confidence: float,
    constraint_reliability: float,
    experiment_confidence: float,
    hard_ceiling_state: dict,
    hard_ceiling_violations: dict,
) -> tuple[dict, str, str, float, dict]:
    evidence_quality = _quality_from_sample(sample_count, min_samples)
    gain_score = (
        (success_rate * 0.35)
        + ((1.0 - escalation_rate) * 0.1)
        + ((1.0 - interruption_rate) * 0.1)
        + (environment_stability * 0.15)
        + (development_confidence * 0.1)
        + (constraint_reliability * 0.1)
        + (experiment_confidence * 0.1)
    )
    risk_score = (
        (override_rate * 0.35)
        + (interruption_rate * 0.3)
        + (replan_rate * 0.2)
        + (retry_rate * 0.1)
        + ((1.0 - memory_delta_rate) * 0.05)
    )
    net = gain_score - risk_score

    decision = "hold"
    if evidence_quality < 0.65:
        decision = "hold_low_quality_evidence"
        target_level = current_level
    elif net >= 0.2:
        decision = "raise_autonomy_level"
        target_level = _shift_level(current_level, 1)
    elif net <= -0.2:
        decision = "lower_autonomy_level"
        target_level = _shift_level(current_level, -1)
    else:
        decision = "hold_mixed_evidence"
        target_level = current_level

    if (
        (hard_ceiling_state.get("human_safety", True) and hard_ceiling_violations.get("human_safety", False))
        or (hard_ceiling_state.get("legality", True) and hard_ceiling_violations.get("legality", False))
        or (hard_ceiling_state.get("system_integrity", True) and hard_ceiling_violations.get("system_integrity", False))
    ):
        target_level = "operator_required"
        decision = "hard_ceiling_enforced"

    recommended = _autonomy_for_level(current, target_level)
    confidence = _bounded((abs(net) * 0.6) + (evidence_quality * 0.4))
    summary = f"{decision}; level={target_level}; net={net:.3f}"
    reasoning = {
        "decision": decision,
        "current_level": current_level,
        "target_level": target_level,
        "scores": {
            "gain_score": round(gain_score, 6),
            "risk_score": round(risk_score, 6),
            "net": round(net, 6),
            "evidence_quality": round(evidence_quality, 6),
        },
        "hard_ceiling": {
            "state": hard_ceiling_state,
            "violations": hard_ceiling_violations,
        },
        "thresholds": {
            "raise_if_net_gte": 0.2,
            "lower_if_net_lte": -0.2,
            "minimum_evidence_quality": 0.65,
        },
    }
    return recommended, target_level, summary, confidence, reasoning


async def evaluate_adaptive_autonomy_boundaries(
    *,
    actor: str,
    source: str,
    scope: str,
    lookback_hours: int,
    min_samples: int,
    apply_recommended_boundaries: bool,
    hard_ceiling_overrides: dict,
    evidence_inputs_override: dict,
    metadata_json: dict,
    db: AsyncSession,
) -> WorkspaceAutonomyBoundaryProfile:
    since = datetime.now(timezone.utc) - timedelta(hours=max(1, int(lookback_hours)))

    proposals = (
        await db.execute(
            select(WorkspaceProposal)
            .where(WorkspaceProposal.created_at >= since)
            .order_by(WorkspaceProposal.id.desc())
            .limit(2000)
        )
    ).scalars().all()

    auto_rows = [
        row
        for row in proposals
        if isinstance(row.metadata_json, dict)
        and bool(row.metadata_json.get("auto_execution", False))
    ]

    sample_count = len(auto_rows)
    if sample_count == 0:
        success_rate = 0.0
        escalation_rate = 0.0
        retry_rate = 0.0
        interruption_rate = 0.0
        memory_delta_rate = 0.0
        verification_counts: dict[str, int] = {}
    else:
        verification_counts = Counter(
            str(
                (row.metadata_json.get("verification", {}) if isinstance(row.metadata_json.get("verification", {}), dict) else {}).get("result", "unknown")
            )
            for row in auto_rows
        )
        success_rate = _bounded(float(verification_counts.get("success", 0)) / float(sample_count))
        escalation_rate = _bounded(float(verification_counts.get("escalate_to_operator", 0)) / float(sample_count))
        retry_rate = _bounded(float(verification_counts.get("retry", 0)) / float(sample_count))
        interruption_rate = _bounded(
            float(
                sum(
                    1
                    for row in auto_rows
                    if isinstance(row.metadata_json, dict)
                    and str(row.metadata_json.get("trigger_reason", "")).startswith("interruption")
                )
            )
            / float(sample_count)
        )
        memory_delta_rate = _bounded(
            float(
                sum(
                    1
                    for row in auto_rows
                    if isinstance(row.metadata_json, dict)
                    and isinstance(row.metadata_json.get("memory_delta", {}), dict)
                    and (
                        bool((row.metadata_json.get("memory_delta", {}) if isinstance(row.metadata_json.get("memory_delta", {}), dict) else {}).get("workspace_observation_ids", []))
                        or int((row.metadata_json.get("memory_delta", {}) if isinstance(row.metadata_json.get("memory_delta", {}), dict) else {}).get("observation_count", 0) or 0) > 0
                    )
                )
            )
            / float(sample_count)
        )

    overrides = evidence_inputs_override if isinstance(evidence_inputs_override, dict) else {}

    interruptions = (
        await db.execute(
            select(WorkspaceInterruptionEvent).where(WorkspaceInterruptionEvent.created_at >= since)
        )
    ).scalars().all()
    interruption_count = len(interruptions)
    human_presence_count = sum(1 for item in interruptions if str(item.interruption_type or "").strip() == "human_detected_in_workspace")

    replans = (
        await db.execute(
            select(WorkspaceReplanSignal).where(WorkspaceReplanSignal.created_at >= since)
        )
    ).scalars().all()
    replan_count = len(replans)

    overrides_rows = (
        await db.execute(
            select(ExecutionJournal)
            .where(ExecutionJournal.created_at >= since)
            .where(ExecutionJournal.action == "workspace_autonomy_override")
        )
    ).scalars().all()
    override_count = len(overrides_rows)

    constraint_rows = (
        await db.execute(
            select(ConstraintEvaluation).where(ConstraintEvaluation.created_at >= since)
        )
    ).scalars().all()
    constraint_total = len(constraint_rows)
    constraint_allowed = sum(1 for row in constraint_rows if str(row.decision or "") in {"allowed", "allow_with_warning"})
    constraint_reliability = _bounded(float(constraint_allowed) / float(constraint_total)) if constraint_total else 0.5

    hard_ceiling_violations = {
        "human_safety": any("human" in str(getattr(row, "decision", "")).lower() for row in constraint_rows),
        "legality": any("legal" in str(getattr(row, "decision", "")).lower() for row in constraint_rows),
        "system_integrity": any("integrity" in str(getattr(row, "decision", "")).lower() for row in constraint_rows),
    }

    pattern_rows = (
        await db.execute(
            select(WorkspaceDevelopmentPattern).where(WorkspaceDevelopmentPattern.created_at >= since)
        )
    ).scalars().all()
    if pattern_rows:
        development_confidence = _bounded(sum(float(row.confidence or 0.0) for row in pattern_rows) / float(len(pattern_rows)))
    else:
        development_confidence = 0.0

    experiment_rows = (
        await db.execute(
            select(WorkspacePolicyExperiment).where(WorkspacePolicyExperiment.created_at >= since)
        )
    ).scalars().all()
    experiment_total = len(experiment_rows)
    experiment_promote = sum(1 for row in experiment_rows if str(row.recommendation or "").strip() == "promote")
    experiment_reject = sum(1 for row in experiment_rows if str(row.recommendation or "").strip() in {"reject", "rollback"})
    if experiment_total:
        experiment_confidence = _bounded((float(experiment_promote) - float(experiment_reject)) / float(experiment_total) * 0.5 + 0.5)
    else:
        experiment_confidence = 0.5

    execution_rows = (
        await db.execute(
            select(CapabilityExecution)
            .where(CapabilityExecution.created_at >= since)
            .order_by(CapabilityExecution.id.desc())
            .limit(200)
        )
    ).scalars().all()
    execution_truth_summary = summarize_execution_truth(
        execution_rows,
        managed_scope=(scope.strip() if str(scope).strip() else "global"),
        max_age_hours=max(1, int(lookback_hours)),
    )

    monitoring = (
        await db.execute(select(WorkspaceMonitoringState).order_by(WorkspaceMonitoringState.id.asc()))
    ).scalars().first()
    current = _autonomy_from_monitoring(monitoring)
    current_level = _current_level_from_autonomy(current)

    if monitoring and isinstance(monitoring.last_deltas_json, list):
        environment_stability = _bounded(1.0 - min(1.0, float(len(monitoring.last_deltas_json)) / 10.0))
    else:
        environment_stability = 0.5

    override_rate = _bounded(float(override_count) / float(max(1, sample_count)))
    replan_rate = _bounded(float(replan_count) / float(max(1, sample_count)))
    interruption_rate = _bounded(max(interruption_rate, float(interruption_count) / float(max(1, sample_count))))

    hard_ceiling_state = _effective_hard_ceiling(hard_ceiling_overrides if isinstance(hard_ceiling_overrides, dict) else {})

    # Deterministic override path for focused tests; production defaults still derive from workspace evidence.
    if "sample_count" in overrides:
        sample_count = max(0, int(overrides.get("sample_count") or 0))
    if "success_rate" in overrides:
        success_rate = _bounded(float(overrides.get("success_rate") or 0.0))
    if "escalation_rate" in overrides:
        escalation_rate = _bounded(float(overrides.get("escalation_rate") or 0.0))
    if "retry_rate" in overrides:
        retry_rate = _bounded(float(overrides.get("retry_rate") or 0.0))
    if "interruption_rate" in overrides:
        interruption_rate = _bounded(float(overrides.get("interruption_rate") or 0.0))
    if "memory_delta_rate" in overrides:
        memory_delta_rate = _bounded(float(overrides.get("memory_delta_rate") or 0.0))
    if "override_rate" in overrides:
        override_rate = _bounded(float(overrides.get("override_rate") or 0.0))
    if "replan_rate" in overrides:
        replan_rate = _bounded(float(overrides.get("replan_rate") or 0.0))
    if "environment_stability" in overrides:
        environment_stability = _bounded(float(overrides.get("environment_stability") or 0.0))
    if "development_confidence" in overrides:
        development_confidence = _bounded(float(overrides.get("development_confidence") or 0.0))
    if "constraint_reliability" in overrides:
        constraint_reliability = _bounded(float(overrides.get("constraint_reliability") or 0.0))
    if "experiment_confidence" in overrides:
        experiment_confidence = _bounded(float(overrides.get("experiment_confidence") or 0.0))

    if "hard_ceiling_violations" in overrides and isinstance(overrides.get("hard_ceiling_violations"), dict):
        supplied = overrides.get("hard_ceiling_violations")
        hard_ceiling_violations = {
            "human_safety": bool(supplied.get("human_safety", hard_ceiling_violations.get("human_safety", False))),
            "legality": bool(supplied.get("legality", hard_ceiling_violations.get("legality", False))),
            "system_integrity": bool(supplied.get("system_integrity", hard_ceiling_violations.get("system_integrity", False))),
        }

    recommended, current_level, adaptation_summary, boundary_confidence, adaptation_reasoning = _recommended_boundaries(
        current=current,
        current_level=current_level,
        min_samples=min_samples,
        success_rate=success_rate,
        escalation_rate=escalation_rate,
        interruption_rate=interruption_rate,
        retry_rate=retry_rate,
        memory_delta_rate=memory_delta_rate,
        override_rate=override_rate,
        replan_rate=replan_rate,
        environment_stability=environment_stability,
        development_confidence=development_confidence,
        constraint_reliability=constraint_reliability,
        experiment_confidence=experiment_confidence,
        hard_ceiling_state=hard_ceiling_state,
        hard_ceiling_violations=hard_ceiling_violations,
        sample_count=sample_count,
    )

    evidence_inputs = {
        "success_rate": success_rate,
        "escalation_rate": escalation_rate,
        "retry_rate": retry_rate,
        "interruption_rate": interruption_rate,
        "memory_delta_rate": memory_delta_rate,
        "override_rate": override_rate,
        "replan_rate": replan_rate,
        "environment_stability": environment_stability,
        "development_confidence": development_confidence,
        "constraint_reliability": constraint_reliability,
        "experiment_confidence": experiment_confidence,
        "human_presence_rate": _bounded(float(human_presence_count) / float(max(1, interruption_count))),
        "counts": {
            "sample_count": sample_count,
            "interruptions": interruption_count,
            "human_presence_interruptions": human_presence_count,
            "replans": replan_count,
            "operator_overrides": override_count,
            "constraint_evaluations": constraint_total,
            "development_patterns": len(pattern_rows),
            "policy_experiments": experiment_total,
        },
        "execution_truth": {
            "execution_count": int(execution_truth_summary.get("execution_count", 0) or 0),
            "signal_count": int(execution_truth_summary.get("deviation_signal_count", 0) or 0),
            "signal_types": execution_truth_summary.get("signal_types", []),
            "freshness": execution_truth_summary.get("freshness", {}),
            "managed_scope": execution_truth_summary.get("managed_scope", "global"),
        },
    }

    applied: dict = {}
    profile_status = "evaluated"
    if apply_recommended_boundaries and sample_count >= max(1, int(min_samples)) and monitoring is not None:
        _store_autonomy_to_monitoring(monitoring, recommended)
        applied = recommended
        profile_status = "applied"

    row = WorkspaceAutonomyBoundaryProfile(
        scope=scope.strip() if str(scope).strip() else "global",
        source=source,
        actor=actor,
        profile_status=profile_status,
        current_level=current_level,
        confidence=boundary_confidence,
        evidence_inputs_json=evidence_inputs,
        last_adjusted=datetime.now(timezone.utc),
        adjustment_reason=adaptation_summary,
        lookback_hours=max(1, int(lookback_hours)),
        sample_count=sample_count,
        success_rate=success_rate,
        escalation_rate=escalation_rate,
        retry_rate=retry_rate,
        interruption_rate=interruption_rate,
        memory_delta_rate=memory_delta_rate,
        current_boundaries_json={k: v for k, v in current.items() if k != "recent_auto_actions"},
        recommended_boundaries_json={k: v for k, v in recommended.items() if k != "recent_auto_actions"},
        applied_boundaries_json={k: v for k, v in applied.items() if k != "recent_auto_actions"},
        adaptation_summary=adaptation_summary,
        adaptation_reasoning_json={
            **adaptation_reasoning,
            "verification_counts": verification_counts,
            "min_samples": max(1, int(min_samples)),
            "apply_requested": bool(apply_recommended_boundaries),
            "applied": profile_status == "applied",
            "hard_ceiling": hard_ceiling_state,
            "hard_ceiling_violations": hard_ceiling_violations,
            "scope": scope,
            "execution_truth_influence": {
                **execution_truth_summary,
                "review_only": True,
            },
        },
        metadata_json={
            **(metadata_json if isinstance(metadata_json, dict) else {}),
            "objective58_adaptive_autonomy_boundaries": True,
            "objective80_execution_truth_review": bool(
                int(execution_truth_summary.get("deviation_signal_count", 0) or 0)
            ),
        },
    )
    db.add(row)
    await db.flush()
    return row


async def list_autonomy_boundary_profiles(
    *,
    db: AsyncSession,
    status: str = "",
    limit: int = 50,
) -> list[WorkspaceAutonomyBoundaryProfile]:
    rows = (
        await db.execute(
            select(WorkspaceAutonomyBoundaryProfile)
            .order_by(WorkspaceAutonomyBoundaryProfile.id.desc())
        )
    ).scalars().all()
    filtered = rows
    if status:
        requested = status.strip().lower()
        if requested in PROFILE_STATUSES:
            filtered = [item for item in filtered if str(item.profile_status or "").strip().lower() == requested]
    return filtered[: max(1, min(500, int(limit)))]


async def get_autonomy_boundary_profile(*, profile_id: int, db: AsyncSession) -> WorkspaceAutonomyBoundaryProfile | None:
    return (
        await db.execute(
            select(WorkspaceAutonomyBoundaryProfile).where(WorkspaceAutonomyBoundaryProfile.id == profile_id)
        )
    ).scalars().first()


def to_autonomy_boundary_profile_out(row: WorkspaceAutonomyBoundaryProfile) -> dict:
    adaptation_reasoning = (
        row.adaptation_reasoning_json
        if isinstance(row.adaptation_reasoning_json, dict)
        else {}
    )
    return {
        "boundary_id": int(row.id),
        "profile_id": int(row.id),
        "scope": row.scope,
        "source": row.source,
        "actor": row.actor,
        "profile_status": row.profile_status,
        "current_level": row.current_level,
        "confidence": float(row.confidence or 0.0),
        "evidence_inputs": row.evidence_inputs_json if isinstance(row.evidence_inputs_json, dict) else {},
        "last_adjusted": row.last_adjusted,
        "adjustment_reason": row.adjustment_reason,
        "lookback_hours": int(row.lookback_hours or 0),
        "sample_count": int(row.sample_count or 0),
        "success_rate": float(row.success_rate or 0.0),
        "escalation_rate": float(row.escalation_rate or 0.0),
        "retry_rate": float(row.retry_rate or 0.0),
        "interruption_rate": float(row.interruption_rate or 0.0),
        "memory_delta_rate": float(row.memory_delta_rate or 0.0),
        "current_boundaries": row.current_boundaries_json if isinstance(row.current_boundaries_json, dict) else {},
        "recommended_boundaries": row.recommended_boundaries_json if isinstance(row.recommended_boundaries_json, dict) else {},
        "applied_boundaries": row.applied_boundaries_json if isinstance(row.applied_boundaries_json, dict) else {},
        "adaptation_summary": row.adaptation_summary,
        "adaptation_reasoning": adaptation_reasoning,
        "execution_truth_influence": (
            adaptation_reasoning.get("execution_truth_influence", {})
            if isinstance(adaptation_reasoning.get("execution_truth_influence", {}), dict)
            else {}
        ),
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }