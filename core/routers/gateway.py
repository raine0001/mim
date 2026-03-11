import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.config import settings
from core.journal import write_journal
from core.models import CapabilityExecution, CapabilityRegistration, Goal, InputEvent, InputEventResolution, SpeechOutputAction, Task, WorkspaceMonitoringState, WorkspaceObservation, WorkspaceObjectMemory, WorkspaceObjectRelation, WorkspaceProposal, WorkspaceZone, WorkspaceZoneRelation
from core.voice_policy import evaluate_voice_policy, load_voice_policy, validate_voice_output
from core.vision_policy import evaluate_vision_policy
from core.vision_policy import load_vision_policy
from core.schemas import (
    ApiInputAdapterRequest,
    CapabilityRegistrationCreate,
    CapabilityExecutionHandoffOut,
    ExecutionFeedbackUpdateRequest,
    ExecutionDispatchRequest,
    NormalizedInputCreate,
    PromoteEventToGoalRequest,
    SpeechOutputRequest,
    TextInputAdapterRequest,
    UiInputAdapterRequest,
    VisionObservationRequest,
    VoiceInputAdapterRequest,
)

router = APIRouter()

OBSERVATION_DEDUPE_WINDOW_SECONDS = 300
OBSERVATION_RECENT_WINDOW_SECONDS = 600
OBSERVATION_OUTDATED_WINDOW_SECONDS = 3600
OBJECT_IDENTITY_WINDOW_SECONDS = 1800
OBJECT_STALE_WINDOW_SECONDS = 7200
OBJECT_MATCH_THRESHOLD = 0.65
PROPOSAL_DEDUPE_WINDOW_SECONDS = 900

AUTONOMY_PROPOSAL_POLICY_MAP: dict[str, str] = {
    "confirm_target_ready": "auto_safe",
    "rescan_zone": "auto_safe",
    "monitor_recheck_workspace": "auto_safe",
    "monitor_search_adjacent_zone": "auto_safe",
    "verify_moved_object": "operator_required",
    "target_confirmation": "manual_only",
    "target_reobserve": "manual_only",
    "execution_candidate": "operator_required",
}
AUTONOMY_PROPOSAL_RISK_SCORE: dict[str, float] = {
    "confirm_target_ready": 0.18,
    "rescan_zone": 0.12,
    "monitor_recheck_workspace": 0.1,
    "monitor_search_adjacent_zone": 0.14,
    "verify_moved_object": 0.55,
    "target_confirmation": 0.8,
    "target_reobserve": 0.78,
    "execution_candidate": 0.82,
}


def _default_autonomy_state() -> dict:
    return {
        "auto_execution_enabled": True,
        "force_manual_approval": False,
        "max_auto_actions_per_minute": 6,
        "cooldown_between_actions_seconds": 5,
        "zone_action_limits": {},
        "auto_safe_confidence_threshold": 0.8,
        "auto_preferred_confidence_threshold": 0.7,
        "low_risk_score_max": 0.3,
        "recent_auto_actions": [],
    }


async def _get_or_create_monitoring_state(db: AsyncSession) -> WorkspaceMonitoringState:
    row = (await db.execute(select(WorkspaceMonitoringState).order_by(WorkspaceMonitoringState.id.asc()))).scalars().first()
    if row:
        return row

    created = WorkspaceMonitoringState(
        desired_running=False,
        runtime_status="stopped",
        scan_trigger_mode="interval",
        interval_seconds=30,
        freshness_threshold_seconds=900,
        cooldown_seconds=10,
        max_scan_rate=6,
        priority_zones=[],
        last_scan_at=None,
        scan_count=0,
        last_scan_reason="",
        last_deltas_json=[],
        last_proposal_ids=[],
        last_snapshot_json={},
        metadata_json={"recent_scans": []},
    )
    db.add(created)
    await db.flush()
    return created


def _autonomy_state_from_monitoring(row: WorkspaceMonitoringState) -> dict:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    raw = metadata.get("autonomy", {}) if isinstance(metadata.get("autonomy", {}), dict) else {}
    defaults = _default_autonomy_state()
    merged = {
        **defaults,
        **raw,
    }
    merged["zone_action_limits"] = {
        str(key): max(1, int(value))
        for key, value in (merged.get("zone_action_limits", {}) if isinstance(merged.get("zone_action_limits", {}), dict) else {}).items()
        if str(key).strip()
    }
    return merged


def _store_autonomy_state(row: WorkspaceMonitoringState, autonomy_state: dict) -> None:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    row.metadata_json = {
        **metadata,
        "autonomy": autonomy_state,
    }


def _parse_iso_to_utc(raw_value: str) -> datetime | None:
    candidate = raw_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _autonomy_throttle_check(*, autonomy_state: dict, zone: str, now: datetime) -> tuple[bool, str, list[dict]]:
    recent_raw = autonomy_state.get("recent_auto_actions", [])
    recent_actions: list[dict] = []
    if isinstance(recent_raw, list):
        for item in recent_raw:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("timestamp", "")).strip()
            parsed = _parse_iso_to_utc(ts) if ts else None
            if not parsed:
                continue
            if (now - parsed).total_seconds() <= 60:
                recent_actions.append({**item, "timestamp": parsed.isoformat()})

    max_per_min = max(1, int(autonomy_state.get("max_auto_actions_per_minute", 6)))
    if len(recent_actions) >= max_per_min:
        return False, "max_auto_actions_per_minute", recent_actions

    cooldown = max(0, int(autonomy_state.get("cooldown_between_actions_seconds", 0)))
    if recent_actions:
        latest = max((_parse_iso_to_utc(str(item.get("timestamp", ""))) for item in recent_actions), default=None)
        if latest and (now - latest).total_seconds() < cooldown:
            return False, "cooldown_between_actions", recent_actions

    zone_limits = autonomy_state.get("zone_action_limits", {}) if isinstance(autonomy_state.get("zone_action_limits", {}), dict) else {}
    if zone.strip() and zone in zone_limits:
        zone_count = sum(1 for item in recent_actions if str(item.get("zone", "")).strip() == zone)
        if zone_count >= max(1, int(zone_limits[zone])):
            return False, "zone_based_limit", recent_actions
    return True, "allowed", recent_actions


async def _is_safe_zone(*, related_zone: str, db: AsyncSession) -> bool:
    zone_name = related_zone.strip()
    if not zone_name:
        return True
    base_zone = zone_name
    for candidate in ["front-left", "front-center", "front-right", "rear-left", "rear-center", "rear-right"]:
        if zone_name == candidate or zone_name.startswith(f"{candidate}-"):
            base_zone = candidate
            break
    zone = (await db.execute(select(WorkspaceZone).where(WorkspaceZone.zone_name == base_zone))).scalars().first()
    if not zone:
        return True
    return int(zone.hazard_level) <= 0


async def _maybe_auto_execute_workspace_proposal(
    *,
    proposal: WorkspaceProposal,
    trigger_reason: str,
    db: AsyncSession,
) -> tuple[bool, str]:
    monitoring = await _get_or_create_monitoring_state(db)
    autonomy = _autonomy_state_from_monitoring(monitoring)

    tier = AUTONOMY_PROPOSAL_POLICY_MAP.get(proposal.proposal_type, "operator_required")
    if not autonomy.get("auto_execution_enabled", True):
        return False, "auto_execution_disabled"
    if autonomy.get("force_manual_approval", False):
        return False, "force_manual_approval"
    if tier in {"manual_only", "operator_required"}:
        return False, f"policy_{tier}"

    threshold = float(autonomy.get("auto_preferred_confidence_threshold", 0.7)) if tier == "auto_preferred" else float(autonomy.get("auto_safe_confidence_threshold", 0.8))
    if float(proposal.confidence) < threshold:
        return False, "confidence_below_threshold"

    if not await _is_safe_zone(related_zone=proposal.related_zone, db=db):
        return False, "unsafe_zone"

    risk_score = float(AUTONOMY_PROPOSAL_RISK_SCORE.get(proposal.proposal_type, 1.0))
    if risk_score > float(autonomy.get("low_risk_score_max", 0.3)):
        return False, "risk_score_too_high"

    trigger = proposal.trigger_json if isinstance(proposal.trigger_json, dict) else {}
    pre = trigger.get("preconditions", {}) if isinstance(trigger.get("preconditions", {}), dict) else {}
    simulation_result = str(trigger.get("simulation_outcome") or pre.get("simulation_outcome") or "not_required")
    if simulation_result not in {"not_required", "", "plan_safe"}:
        return False, "simulation_not_safe"

    now = datetime.now(timezone.utc)
    allowed, reason, recent_actions = _autonomy_throttle_check(autonomy_state=autonomy, zone=proposal.related_zone, now=now)
    if not allowed:
        return False, reason

    task = Task(
        title=proposal.title,
        details=proposal.description,
        dependencies=[],
        acceptance_criteria="proposal auto-executed under objective35 safety policy",
        assigned_to="tod",
        state="queued",
        objective_id=None,
    )
    db.add(task)
    await db.flush()

    proposal.status = "accepted"
    proposal.metadata_json = {
        **(proposal.metadata_json if isinstance(proposal.metadata_json, dict) else {}),
        "accepted_by": "system-auto",
        "accept_reason": "objective35_auto_execute",
        "linked_task_id": task.id,
        "auto_execution": {
            "trigger_reason": trigger_reason,
            "policy_rule_used": tier,
            "confidence_score": float(proposal.confidence),
            "risk_score": risk_score,
            "simulation_result": simulation_result,
            "execution_outcome": "queued_task",
        },
    }

    recent_actions.append(
        {
            "timestamp": now.isoformat(),
            "proposal_id": proposal.id,
            "proposal_type": proposal.proposal_type,
            "zone": proposal.related_zone,
            "task_id": task.id,
        }
    )
    autonomy["recent_auto_actions"] = recent_actions
    _store_autonomy_state(monitoring, autonomy)

    await write_journal(
        db,
        actor="system-auto",
        action="workspace_proposal_auto_execute",
        target_type="workspace_proposal",
        target_id=str(proposal.id),
        summary=f"Auto-executed workspace proposal {proposal.id}",
        metadata_json={
            "trigger_reason": trigger_reason,
            "policy_rule_used": tier,
            "confidence_score": float(proposal.confidence),
            "simulation_result": simulation_result,
            "execution_outcome": "queued_task",
            "linked_task_id": task.id,
        },
    )
    return True, "auto_executed"


def _to_input_out(row: InputEvent) -> dict:
    return {
        "input_id": row.id,
        "source": row.source,
        "raw_input": row.raw_input,
        "parsed_intent": row.parsed_intent,
        "confidence": row.confidence,
        "target_system": row.target_system,
        "requested_goal": row.requested_goal,
        "safety_flags": row.safety_flags,
        "metadata_json": row.metadata_json,
        "normalized": row.normalized,
        "created_at": row.created_at,
    }


def _to_resolution_out(row: InputEventResolution) -> dict:
    return {
        "resolution_id": row.id,
        "input_event_id": row.input_event_id,
        "internal_intent": row.internal_intent,
        "confidence_tier": row.confidence_tier,
        "outcome": row.outcome,
        "resolution_status": row.resolution_status,
        "safety_decision": row.safety_decision,
        "reason": row.reason,
        "clarification_prompt": row.clarification_prompt,
        "escalation_reasons": row.escalation_reasons,
        "capability_name": row.capability_name,
        "capability_registered": row.capability_registered,
        "capability_enabled": row.capability_enabled,
        "goal_id": row.goal_id,
        "proposed_goal_description": row.proposed_goal_description,
        "proposed_actions": row.proposed_actions,
        "metadata_json": row.metadata_json,
        "created_at": row.created_at,
    }


def _to_execution_out(row: CapabilityExecution) -> dict:
    return {
        "execution_id": row.id,
        "input_event_id": row.input_event_id,
        "resolution_id": row.resolution_id,
        "goal_id": row.goal_id,
        "capability_name": row.capability_name,
        "arguments_json": row.arguments_json,
        "safety_mode": row.safety_mode,
        "requested_executor": row.requested_executor,
        "dispatch_decision": row.dispatch_decision,
        "status": row.status,
        "reason": row.reason,
        "feedback_json": row.feedback_json,
        "handoff_endpoint": f"/gateway/capabilities/executions/{row.id}/handoff",
        "created_at": row.created_at,
    }


ALLOWED_EXECUTION_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"accepted", "running", "failed", "blocked"},
    "pending_confirmation": {"accepted", "running", "failed", "blocked"},
    "dispatched": {"accepted", "running", "failed", "blocked", "succeeded"},
    "accepted": {"running", "failed", "blocked", "succeeded"},
    "running": {"succeeded", "failed", "blocked"},
    "blocked": set(),
    "succeeded": set(),
    "failed": set(),
}


RUNTIME_OUTCOME_STATUS_MAP: dict[str, tuple[str, str]] = {
    "executor_unavailable": ("failed", "executor unavailable"),
    "guardrail_blocked": ("blocked", "guardrail blocked"),
    "retry_in_progress": ("running", "retry in progress"),
    "fallback_used": ("running", "fallback path in use"),
    "recovered": ("succeeded", "execution recovered"),
    "unrecovered_failure": ("failed", "unrecovered execution failure"),
}


def _allowed_feedback_actors() -> set[str]:
    return {
        item.strip().lower()
        for item in settings.execution_feedback_allowed_actors.split(",")
        if item.strip()
    }


def _enforce_feedback_boundary(actor: str, feedback_key: str | None) -> None:
    actor_normalized = actor.strip().lower()
    if not actor_normalized:
        raise HTTPException(status_code=422, detail="feedback actor is required")

    if actor_normalized not in _allowed_feedback_actors():
        raise HTTPException(status_code=403, detail="feedback actor is not allowed")

    configured_key = settings.execution_feedback_api_key.strip()
    if configured_key and feedback_key != configured_key:
        raise HTTPException(status_code=403, detail="invalid feedback API key")


def _resolve_feedback_status(payload: ExecutionFeedbackUpdateRequest) -> tuple[str, str, str]:
    requested_status = payload.status.strip().lower()
    runtime_outcome = payload.runtime_outcome.strip().lower()
    resolved_reason = payload.reason

    if runtime_outcome:
        mapped = RUNTIME_OUTCOME_STATUS_MAP.get(runtime_outcome)
        if not mapped:
            raise HTTPException(status_code=422, detail=f"unsupported runtime_outcome: {runtime_outcome}")
        mapped_status, mapped_reason = mapped
        if requested_status and requested_status != mapped_status:
            raise HTTPException(
                status_code=422,
                detail=f"status/runtime_outcome mismatch: {requested_status} != {mapped_status}",
            )
        requested_status = mapped_status
        if not resolved_reason.strip():
            resolved_reason = mapped_reason

    if not requested_status:
        raise HTTPException(status_code=422, detail="status or runtime_outcome is required")

    if not resolved_reason.strip():
        resolved_reason = "executor feedback update"

    return requested_status, resolved_reason, runtime_outcome


def _infer_intent(event: InputEvent) -> str:
    if event.parsed_intent and event.parsed_intent not in {"unknown", "vision_observation"}:
        mapping = {
            "speak": "speak_response",
            "voice_output": "speak_response",
            "workspace_check": "observe_workspace",
            "observe_workspace": "observe_workspace",
            "identify_object": "identify_object",
            "task_execute": "execute_capability",
            "execute_capability": "execute_capability",
            "create_goal": "create_goal",
            "clarify": "request_clarification",
        }
        lowered = event.parsed_intent.lower()
        for key, mapped in mapping.items():
            if key in lowered:
                return mapped

    raw = event.raw_input.lower()
    if "speak" in raw or "say" in raw:
        return "speak_response"
    if "scan" in raw or "observe" in raw or "workspace check" in raw:
        return "observe_workspace"
    if "table" in raw or "workspace" in raw:
        return "observe_workspace"
    if "identify" in raw or "look for" in raw or "detect" in raw:
        return "identify_object"
    if "run" in raw or "execute" in raw or "invoke" in raw:
        return "execute_capability"
    if "create goal" in raw or "plan" in raw:
        return "create_goal"
    if event.source == "vision":
        return "identify_object"
    return "request_clarification"


def _intent_capability(event: InputEvent, internal_intent: str) -> str:
    explicit = str(event.metadata_json.get("capability", "")).strip()
    if explicit:
        return explicit

    if internal_intent == "speak_response":
        return "speech_output"
    if internal_intent == "observe_workspace":
        return "workspace_scan"
    if internal_intent == "identify_object":
        return "observation_capability"
    if internal_intent == "execute_capability":
        return "workspace_check"
    return ""


def _default_execution_arguments(event: InputEvent, capability_name: str) -> dict:
    if capability_name != "workspace_scan":
        return {}

    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    return {
        "scan_mode": str(metadata.get("scan_mode", "standard")),
        "scan_area": str(metadata.get("scan_area", "workspace")),
        "confidence_threshold": float(metadata.get("confidence_threshold", 0.6)),
    }


def _proposed_actions(internal_intent: str, capability_name: str, goal_description: str) -> list[dict]:
    if internal_intent == "request_clarification":
        return [{"step": 1, "action_type": "request_clarification", "details": goal_description}]
    if internal_intent == "create_goal":
        return [{"step": 1, "action_type": "create_goal", "details": goal_description}]
    if capability_name:
        return [{"step": 1, "action_type": "execute_capability", "capability": capability_name, "details": goal_description}]
    return [{"step": 1, "action_type": internal_intent, "details": goal_description}]


def _goal_description(event: InputEvent, internal_intent: str) -> str:
    requested = event.requested_goal.strip()
    if requested:
        return requested
    return f"{internal_intent}: {event.raw_input.strip()}"


def _parse_observed_at(raw_value: object) -> datetime | None:
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None

    candidate = raw_value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _workspace_freshness_state(last_seen_at: datetime, now: datetime) -> str:
    age_seconds = max((now - last_seen_at).total_seconds(), 0.0)
    if age_seconds <= OBSERVATION_RECENT_WINDOW_SECONDS:
        return "recent"
    if age_seconds <= OBSERVATION_OUTDATED_WINDOW_SECONDS:
        return "aging"
    return "stale"


def _workspace_effective_confidence(confidence: float, freshness_state: str) -> float:
    bounded = min(max(confidence, 0.0), 1.0)
    if freshness_state == "recent":
        return bounded
    if freshness_state == "aging":
        return min(max(bounded * 0.75, 0.0), 1.0)
    return min(max(bounded * 0.4, 0.0), 1.0)


async def _workspace_memory_signal(event: InputEvent, db: AsyncSession) -> dict:
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    zone = str(metadata.get("scan_area", "workspace")).strip() or "workspace"

    rows = (
        await db.execute(
            select(WorkspaceObservation)
            .where(WorkspaceObservation.zone == zone)
            .where(WorkspaceObservation.lifecycle_status != "superseded")
            .order_by(WorkspaceObservation.last_seen_at.desc(), WorkspaceObservation.id.desc())
            .limit(20)
        )
    ).scalars().all()

    now = datetime.now(timezone.utc)
    recent_count = 0
    stale_count = 0
    best_effective_confidence = 0.0
    dominant_label = ""
    label_counts: dict[str, int] = {}

    for row in rows:
        freshness = _workspace_freshness_state(row.last_seen_at, now)
        effective = _workspace_effective_confidence(row.confidence, freshness)
        if freshness == "recent":
            recent_count += 1
        if freshness == "stale":
            stale_count += 1
        if effective > best_effective_confidence:
            best_effective_confidence = effective
        label_counts[row.label] = label_counts.get(row.label, 0) + int(row.observation_count or 1)

    if label_counts:
        dominant_label = max(label_counts.items(), key=lambda item: item[1])[0]

    object_rows = (
        await db.execute(
            select(WorkspaceObjectMemory)
            .where(WorkspaceObjectMemory.zone == zone)
            .order_by(WorkspaceObjectMemory.last_seen_at.desc(), WorkspaceObjectMemory.id.desc())
            .limit(20)
        )
    ).scalars().all()

    object_recent_strong_count = 0
    object_uncertain_count = 0
    object_stale_missing_count = 0
    moved_recent_count = 0
    dominant_object = ""
    strongest_object_confidence = 0.0
    for row in object_rows:
        freshness = _workspace_freshness_state(row.last_seen_at, now)
        effective = _workspace_effective_confidence(row.confidence, freshness)
        if row.status in {"uncertain"}:
            object_uncertain_count += 1
        if row.status in {"stale", "missing"}:
            object_stale_missing_count += 1
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if bool(metadata.get("moved")) and freshness == "recent":
            moved_recent_count += 1
        if freshness == "recent" and effective >= 0.75 and row.status == "active":
            object_recent_strong_count += 1
        if effective > strongest_object_confidence:
            strongest_object_confidence = effective
            dominant_object = row.canonical_name

    zone_row = (
        await db.execute(select(WorkspaceZone).where(WorkspaceZone.zone_name == zone))
    ).scalars().first()
    hazard_level = int(zone_row.hazard_level) if zone_row else 0

    return {
        "zone": zone,
        "recent_count": recent_count,
        "stale_count": stale_count,
        "best_effective_confidence": round(best_effective_confidence, 4),
        "dominant_label": dominant_label,
        "object_recent_strong_count": object_recent_strong_count,
        "object_uncertain_count": object_uncertain_count,
        "object_stale_missing_count": object_stale_missing_count,
        "moved_recent_count": moved_recent_count,
        "dominant_object": dominant_object,
        "strongest_object_confidence": round(strongest_object_confidence, 4),
        "hazard_level": hazard_level,
        "unsafe_zone": hazard_level > 0,
    }


def _labels_similar(left: str, right: str) -> bool:
    def _normalize(value: str) -> str:
        cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in value)
        return " ".join(cleaned.split())

    a = _normalize(left)
    b = _normalize(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        shorter = min(len(a), len(b))
        longer = max(len(a), len(b))
        if longer > 0 and (shorter / longer) >= 0.8 and shorter >= 6:
            return True
        return False
    return False


async def _match_object_identity(
    *,
    db: AsyncSession,
    label: str,
    zone: str,
    observed_at: datetime,
) -> WorkspaceObjectMemory | None:
    candidates = (
        await db.execute(
            select(WorkspaceObjectMemory)
            .where(WorkspaceObjectMemory.status != "stale")
            .order_by(WorkspaceObjectMemory.last_seen_at.desc(), WorkspaceObjectMemory.id.desc())
            .limit(200)
        )
    ).scalars().all()

    best: WorkspaceObjectMemory | None = None
    best_score = 0.0
    for candidate in candidates:
        aliases = candidate.candidate_labels if isinstance(candidate.candidate_labels, list) else []
        labels = [candidate.canonical_name, *[str(item) for item in aliases]]
        label_match = any(_labels_similar(label, existing_label) for existing_label in labels)
        if not label_match:
            continue

        age_seconds = max((observed_at - candidate.last_seen_at).total_seconds(), 0.0)
        time_score = 1.0 if age_seconds <= OBJECT_IDENTITY_WINDOW_SECONDS else 0.4
        zone_score = 1.0 if candidate.zone == zone else 0.55
        score = (0.55 * 1.0) + (0.25 * zone_score) + (0.20 * time_score)
        if score > best_score:
            best_score = score
            best = candidate

    if best and best_score >= OBJECT_MATCH_THRESHOLD:
        return best
    return None


async def _upsert_object_identity(
    *,
    db: AsyncSession,
    execution: CapabilityExecution,
    observation_item: dict,
) -> WorkspaceObjectMemory | None:
    label = str(observation_item.get("label", "")).strip()
    if not label:
        return None

    execution_args = execution.arguments_json if isinstance(execution.arguments_json, dict) else {}
    zone = str(observation_item.get("zone") or execution_args.get("scan_area") or "workspace").strip() or "workspace"
    observed_at = _parse_observed_at(observation_item.get("observed_at")) or datetime.now(timezone.utc)

    raw_confidence = observation_item.get("confidence", 0.0)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    now = datetime.now(timezone.utc)
    age_seconds = max((now - observed_at).total_seconds(), 0.0)
    observed_status = "stale" if age_seconds > OBJECT_STALE_WINDOW_SECONDS else "active"

    matched = await _match_object_identity(db=db, label=label, zone=zone, observed_at=observed_at)
    if matched:
        previous_zone = matched.zone
        history = matched.location_history if isinstance(matched.location_history, list) else []
        moved = previous_zone != zone
        if moved:
            history.append(
                {
                    "from": previous_zone,
                    "to": zone,
                    "moved_at": observed_at.isoformat(),
                    "execution_id": execution.id,
                }
            )

        labels = matched.candidate_labels if isinstance(matched.candidate_labels, list) else []
        labels_set = {str(item).strip().lower() for item in labels if str(item).strip()}
        labels_set.add(matched.canonical_name.strip().lower())
        labels_set.add(label.lower())

        matched.candidate_labels = sorted(labels_set)
        matched.confidence = max(matched.confidence, confidence)
        matched.zone = zone
        matched.last_seen_at = observed_at
        matched.last_execution_id = execution.id
        matched.location_history = history
        matched.status = "uncertain" if moved else observed_status
        matched.metadata_json = {
            **(matched.metadata_json if isinstance(matched.metadata_json, dict) else {}),
            "last_matched_label": label,
            "last_observation": observation_item,
            "moved": moved,
            "moved_from": previous_zone if moved else zone,
        }
        return matched

    object_memory = WorkspaceObjectMemory(
        canonical_name=label,
        candidate_labels=[label.lower()],
        confidence=confidence,
        zone=zone,
        status=observed_status,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
        last_execution_id=execution.id,
        location_history=[
            {
                "from": None,
                "to": zone,
                "moved_at": observed_at.isoformat(),
                "execution_id": execution.id,
            }
        ],
        metadata_json={
            "last_matched_label": label,
            "last_observation": observation_item,
            "moved": False,
        },
    )
    db.add(object_memory)
    await db.flush()
    return object_memory


async def _update_missing_object_identities(
    *,
    db: AsyncSession,
    execution: CapabilityExecution,
    observed_labels_by_zone: dict[str, set[str]],
) -> None:
    zone_rows = (await db.execute(select(WorkspaceZone))).scalars().all()
    zone_ids = {row.zone_name: row.id for row in zone_rows}
    adjacent_map: dict[str, set[str]] = {}
    if zone_ids:
        relations = (
            await db.execute(select(WorkspaceZoneRelation).where(WorkspaceZoneRelation.relation_type == "adjacent_to"))
        ).scalars().all()
        reverse_ids = {value: key for key, value in zone_ids.items()}
        for relation in relations:
            from_zone = reverse_ids.get(relation.from_zone_id)
            to_zone = reverse_ids.get(relation.to_zone_id)
            if not from_zone or not to_zone:
                continue
            adjacent_map.setdefault(from_zone, set()).add(to_zone)

    now = datetime.now(timezone.utc)
    for zone, labels in observed_labels_by_zone.items():
        rows = (
            await db.execute(
                select(WorkspaceObjectMemory)
                .where(WorkspaceObjectMemory.zone == zone)
                .where(WorkspaceObjectMemory.status.in_(["active", "uncertain", "missing"]))
            )
        ).scalars().all()

        for row in rows:
            known_names = {row.canonical_name.lower()}
            aliases = row.candidate_labels if isinstance(row.candidate_labels, list) else []
            known_names.update(str(item).strip().lower() for item in aliases if str(item).strip())

            is_observed = any(name in labels for name in known_names)
            if is_observed:
                if row.status == "missing":
                    row.status = "active"
                continue

            age_seconds = max((now - row.last_seen_at).total_seconds(), 0.0)
            if age_seconds > OBJECT_STALE_WINDOW_SECONDS:
                row.status = "stale"
                row.confidence = max(row.confidence * 0.6, 0.05)
            else:
                row.status = "missing"
                row.confidence = max(row.confidence * 0.8, 0.1)

            likely_moved_to = ""
            for candidate_zone in adjacent_map.get(zone, set()):
                nearby = (
                    await db.execute(
                        select(WorkspaceObjectMemory)
                        .where(WorkspaceObjectMemory.zone == candidate_zone)
                        .where(WorkspaceObjectMemory.status.in_(["active", "uncertain"]))
                        .where(WorkspaceObjectMemory.canonical_name == row.canonical_name)
                    )
                ).scalars().first()
                if nearby:
                    likely_moved_to = candidate_zone
                    break

            row.metadata_json = {
                **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
                "missing_update_execution_id": execution.id,
                "missing_update_at": now.isoformat(),
                "likely_moved_to": likely_moved_to,
            }


async def _update_object_relations_for_scan(
    *,
    db: AsyncSession,
    execution: CapabilityExecution,
    object_memories: list[WorkspaceObjectMemory],
) -> list[int]:
    relation_ids: list[int] = []
    if len(object_memories) < 2:
        return relation_ids

    now = datetime.now(timezone.utc)
    for index, left in enumerate(object_memories):
        for right in object_memories[index + 1 :]:
            if left.id == right.id:
                continue

            subject_id = min(left.id, right.id)
            object_id = max(left.id, right.id)
            relation_type = "near" if left.zone == right.zone else "far"
            relation_status = "active" if relation_type == "near" else "inconsistent"
            confidence = min(max((left.confidence + right.confidence) / 2.0, 0.0), 1.0)

            existing = (
                await db.execute(
                    select(WorkspaceObjectRelation)
                    .where(WorkspaceObjectRelation.subject_object_id == subject_id)
                    .where(WorkspaceObjectRelation.object_object_id == object_id)
                )
            ).scalars().first()

            if existing:
                existing.relation_type = relation_type
                existing.relation_status = relation_status
                existing.confidence = confidence
                existing.last_seen_at = now
                existing.source_execution_id = execution.id
                existing.metadata_json = {
                    **(existing.metadata_json if isinstance(existing.metadata_json, dict) else {}),
                    "left_zone": left.zone,
                    "right_zone": right.zone,
                }
                relation_ids.append(existing.id)
            else:
                relation = WorkspaceObjectRelation(
                    subject_object_id=subject_id,
                    object_object_id=object_id,
                    relation_type=relation_type,
                    relation_status=relation_status,
                    confidence=confidence,
                    last_seen_at=now,
                    source_execution_id=execution.id,
                    metadata_json={
                        "left_zone": left.zone,
                        "right_zone": right.zone,
                    },
                )
                db.add(relation)
                await db.flush()
                relation_ids.append(relation.id)

    return relation_ids


async def _proposal_exists_recently(
    *,
    db: AsyncSession,
    proposal_type: str,
    related_object_id: int | None,
    related_zone: str,
    now: datetime,
) -> bool:
    window_start = now - timedelta(seconds=PROPOSAL_DEDUPE_WINDOW_SECONDS)
    stmt = (
        select(WorkspaceProposal)
        .where(WorkspaceProposal.proposal_type == proposal_type)
        .where(WorkspaceProposal.status == "pending")
        .where(WorkspaceProposal.created_at >= window_start)
    )
    if related_object_id is not None:
        stmt = stmt.where(WorkspaceProposal.related_object_id == related_object_id)
    if related_zone:
        stmt = stmt.where(WorkspaceProposal.related_zone == related_zone)

    row = (await db.execute(stmt.order_by(WorkspaceProposal.id.desc()))).scalars().first()
    return row is not None


async def _create_workspace_proposal(
    *,
    db: AsyncSession,
    execution: CapabilityExecution,
    proposal_type: str,
    title: str,
    description: str,
    confidence: float,
    related_zone: str,
    related_object_id: int | None,
    trigger_json: dict,
) -> WorkspaceProposal | None:
    now = datetime.now(timezone.utc)
    if await _proposal_exists_recently(
        db=db,
        proposal_type=proposal_type,
        related_object_id=related_object_id,
        related_zone=related_zone,
        now=now,
    ):
        return None

    proposal = WorkspaceProposal(
        proposal_type=proposal_type,
        title=title,
        description=description,
        status="pending",
        confidence=min(max(confidence, 0.0), 1.0),
        source="workspace_state",
        related_zone=related_zone,
        related_object_id=related_object_id,
        source_execution_id=execution.id,
        trigger_json=trigger_json,
        metadata_json={"generated_by": "objective28"},
    )
    db.add(proposal)
    await db.flush()

    await write_journal(
        db,
        actor="gateway",
        action="workspace_proposal_generated",
        target_type="workspace_proposal",
        target_id=str(proposal.id),
        summary=title,
        metadata_json={
            "proposal_type": proposal_type,
            "related_zone": related_zone,
            "related_object_id": related_object_id,
            "source_execution_id": execution.id,
        },
    )
    await _maybe_auto_execute_workspace_proposal(
        proposal=proposal,
        trigger_reason="objective28_workspace_state",
        db=db,
    )
    return proposal


async def _generate_workspace_state_proposals(
    *,
    db: AsyncSession,
    execution: CapabilityExecution,
    workspace_object_ids: list[int],
) -> list[int]:
    proposal_ids: list[int] = []
    for object_id in workspace_object_ids:
        object_row = await db.get(WorkspaceObjectMemory, object_id)
        if not object_row:
            continue

        metadata = object_row.metadata_json if isinstance(object_row.metadata_json, dict) else {}
        if object_row.status == "missing":
            proposal = await _create_workspace_proposal(
                db=db,
                execution=execution,
                proposal_type="rescan_zone",
                title=f"Rescan zone for missing object: {object_row.canonical_name}",
                description=f"Object {object_row.canonical_name} is missing from expected zone {object_row.zone}.",
                confidence=max(object_row.confidence, 0.55),
                related_zone=object_row.zone,
                related_object_id=object_row.id,
                trigger_json={"status": object_row.status, "likely_moved_to": metadata.get("likely_moved_to", "")},
            )
            if proposal:
                proposal_ids.append(proposal.id)

        if object_row.status == "uncertain" and bool(metadata.get("moved")):
            proposal = await _create_workspace_proposal(
                db=db,
                execution=execution,
                proposal_type="verify_moved_object",
                title=f"Verify moved object location: {object_row.canonical_name}",
                description=f"Object {object_row.canonical_name} moved and now requires reconfirmation in {object_row.zone}.",
                confidence=max(object_row.confidence, 0.6),
                related_zone=object_row.zone,
                related_object_id=object_row.id,
                trigger_json={"status": object_row.status, "moved_from": metadata.get("moved_from", "")},
            )
            if proposal:
                proposal_ids.append(proposal.id)

        if object_row.status == "active" and object_row.confidence >= 0.85:
            proposal = await _create_workspace_proposal(
                db=db,
                execution=execution,
                proposal_type="confirm_target_ready",
                title=f"Target appears ready: {object_row.canonical_name}",
                description=f"Object {object_row.canonical_name} is confidently observed in {object_row.zone}.",
                confidence=object_row.confidence,
                related_zone=object_row.zone,
                related_object_id=object_row.id,
                trigger_json={"status": object_row.status},
            )
            if proposal:
                proposal_ids.append(proposal.id)

    return proposal_ids


async def _upsert_workspace_observation(
    *,
    db: AsyncSession,
    execution: CapabilityExecution,
    observation_item: dict,
) -> WorkspaceObservation | None:
    label = str(observation_item.get("label", "")).strip()
    if not label:
        return None

    execution_args = execution.arguments_json if isinstance(execution.arguments_json, dict) else {}
    zone = str(observation_item.get("zone") or execution_args.get("scan_area") or "workspace").strip() or "workspace"
    source = str(observation_item.get("source") or "vision").strip() or "vision"

    raw_confidence = observation_item.get("confidence", 0.0)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    now = datetime.now(timezone.utc)
    observed_at = _parse_observed_at(observation_item.get("observed_at")) or now
    freshness = _workspace_freshness_state(observed_at, now)
    effective_confidence = _workspace_effective_confidence(confidence, freshness)
    lifecycle_status = "active" if freshness == "recent" else "outdated"

    window_start = observed_at - timedelta(seconds=OBSERVATION_DEDUPE_WINDOW_SECONDS)
    existing = (
        await db.execute(
            select(WorkspaceObservation)
            .where(WorkspaceObservation.label == label)
            .where(WorkspaceObservation.zone == zone)
            .where(WorkspaceObservation.lifecycle_status != "superseded")
            .where(WorkspaceObservation.last_seen_at >= window_start)
            .order_by(WorkspaceObservation.last_seen_at.desc(), WorkspaceObservation.id.desc())
        )
    ).scalars().first()

    details = {
        "effective_confidence": effective_confidence,
        "freshness_state": freshness,
        "raw_observation": observation_item,
    }

    if existing:
        existing.observed_at = observed_at
        existing.last_seen_at = observed_at
        existing.execution_id = execution.id
        existing.source = source
        existing.confidence = max(existing.confidence, confidence)
        existing.lifecycle_status = lifecycle_status
        existing.observation_count = int(existing.observation_count or 1) + 1
        existing.metadata_json = {
            **(existing.metadata_json if isinstance(existing.metadata_json, dict) else {}),
            **details,
            "dedupe_merged": True,
        }
        return existing

    observation = WorkspaceObservation(
        observed_at=observed_at,
        zone=zone,
        label=label,
        confidence=confidence,
        source=source,
        execution_id=execution.id,
        lifecycle_status=lifecycle_status,
        first_seen_at=observed_at,
        last_seen_at=observed_at,
        observation_count=1,
        metadata_json={
            **details,
            "dedupe_merged": False,
        },
    )
    db.add(observation)
    await db.flush()
    return observation


async def _resolve_event(event: InputEvent, db: AsyncSession) -> InputEventResolution:
    internal_intent = _infer_intent(event)
    capability_name = _intent_capability(event, internal_intent)
    capability_registered = False
    capability_enabled = False
    capability_requires_confirmation = False

    capability: CapabilityRegistration | None = None
    if capability_name:
        capability = (
            await db.execute(select(CapabilityRegistration).where(CapabilityRegistration.capability_name == capability_name))
        ).scalars().first()
        if capability:
            capability_registered = True
            capability_enabled = capability.enabled
            capability_requires_confirmation = capability.requires_confirmation

    if internal_intent == "observe_workspace" and (not capability_registered or not capability_enabled):
        fallback_name = "workspace_check"
        fallback = (
            await db.execute(
                select(CapabilityRegistration).where(CapabilityRegistration.capability_name == fallback_name)
            )
        ).scalars().first()
        if fallback and fallback.enabled:
            capability_name = fallback_name
            capability_registered = True
            capability_enabled = True
            capability_requires_confirmation = fallback.requires_confirmation

    safety_flags = set(event.safety_flags)
    reason = "default_confirmation"
    safety_decision = "requires_confirmation"
    confidence_tier = "n/a"
    outcome = "requires_confirmation"
    clarification_prompt = ""
    escalation_reasons: list[str] = []
    memory_signal = {
        "zone": "",
        "recent_count": 0,
        "stale_count": 0,
        "best_effective_confidence": 0.0,
        "dominant_label": "",
        "object_recent_strong_count": 0,
        "object_uncertain_count": 0,
        "object_stale_missing_count": 0,
        "dominant_object": "",
        "strongest_object_confidence": 0.0,
        "moved_recent_count": 0,
        "hazard_level": 0,
        "unsafe_zone": False,
    }

    if event.source == "vision":
        detected_labels_raw = event.metadata_json.get("detected_labels", [])
        detected_labels = [str(label) for label in detected_labels_raw] if isinstance(detected_labels_raw, list) else []
        policy_eval = evaluate_vision_policy(
            confidence=event.confidence,
            internal_intent=internal_intent,
            raw_observation=event.raw_input,
            detected_labels=detected_labels,
            target_capability=capability_name,
            metadata_json=event.metadata_json,
        )
        confidence_tier = policy_eval["confidence_tier"]
        outcome = policy_eval["outcome"]
        escalation_reasons = policy_eval["escalation_reasons"]
        safety_decision = outcome
        reason = escalation_reasons[0] if escalation_reasons else "vision_policy_outcome"
    elif event.source == "voice":
        policy_eval = evaluate_voice_policy(
            transcript=event.raw_input,
            confidence=event.confidence,
            internal_intent=internal_intent,
            target_capability=capability_name,
        )
        confidence_tier = policy_eval["confidence_tier"]
        outcome = policy_eval["outcome"]
        escalation_reasons = policy_eval["escalation_reasons"]
        clarification_prompt = policy_eval["clarification_prompt"]
        safety_decision = outcome
        reason = escalation_reasons[0] if escalation_reasons else "voice_policy_outcome"
    else:
        if "deny_execution" in safety_flags or "blocked" in safety_flags:
            safety_decision = "blocked"
            reason = "safety_flag_blocked"
        elif internal_intent == "request_clarification":
            safety_decision = "requires_confirmation"
            reason = "intent_requires_clarification"
        elif capability_name and (not capability_registered or not capability_enabled):
            safety_decision = "blocked"
            reason = "capability_unavailable"
        elif "requires_confirmation" in safety_flags:
            safety_decision = "requires_confirmation"
            reason = "safety_flag_requires_confirmation"
        elif event.source in {"voice", "vision"} and event.confidence < 0.75:
            safety_decision = "requires_confirmation"
            reason = "low_confidence_signal"
        elif capability_requires_confirmation:
            safety_decision = "requires_confirmation"
            reason = "capability_policy_requires_confirmation"
        else:
            safety_decision = "auto_execute"
            reason = "policy_allows_auto_execute"
        outcome = safety_decision

    if internal_intent == "observe_workspace":
        memory_signal = await _workspace_memory_signal(event, db)
        if outcome not in {"blocked", "store_only"}:
            if memory_signal["unsafe_zone"]:
                if outcome == "auto_execute":
                    safety_decision = "requires_confirmation"
                    outcome = "requires_confirmation"
                    reason = "unsafe_zone_requires_confirmation"
                    if "unsafe_zone" not in escalation_reasons:
                        escalation_reasons.append("unsafe_zone")
            elif (
                memory_signal["stale_count"] > 0
                or memory_signal["object_stale_missing_count"] > 0
            ) and memory_signal["object_recent_strong_count"] == 0:
                if outcome == "auto_execute":
                    safety_decision = "requires_confirmation"
                    outcome = "requires_confirmation"
                    reason = "memory_stale_requires_reconfirm"
                    if "stale_observation_needs_reconfirm" not in escalation_reasons:
                        escalation_reasons.append("stale_observation_needs_reconfirm")
            elif memory_signal["object_uncertain_count"] > 0:
                if outcome == "auto_execute":
                    safety_decision = "requires_confirmation"
                    outcome = "requires_confirmation"
                    reason = "memory_object_uncertain_requires_reconfirm"
                    if "object_identity_uncertain" not in escalation_reasons:
                        escalation_reasons.append("object_identity_uncertain")
            elif memory_signal["moved_recent_count"] > 0:
                if outcome == "auto_execute":
                    safety_decision = "requires_confirmation"
                    outcome = "requires_confirmation"
                    reason = "memory_spatial_change_requires_reconfirm"
                    if "spatial_relation_changed" not in escalation_reasons:
                        escalation_reasons.append("spatial_relation_changed")
            elif memory_signal["recent_count"] > 0 and (
                memory_signal["best_effective_confidence"] >= 0.75
                or memory_signal["object_recent_strong_count"] > 0
            ):
                if "requires_confirmation" not in safety_flags and not capability_requires_confirmation:
                    safety_decision = "auto_execute"
                    outcome = "auto_execute"
                    reason = "memory_confident_recent_identity"

    if capability_name and (not capability_registered or not capability_enabled) and outcome != "blocked":
        outcome = "blocked"
        safety_decision = "blocked"
        reason = "capability_unavailable"
        if "requires_clarification" not in escalation_reasons:
            escalation_reasons.append("requires_clarification")
        if not clarification_prompt and event.source == "voice":
            clarification_prompt = "I cannot run that capability right now. Please choose an available capability."

    requires_clarification_only = event.source == "voice" and (
        outcome in {"store_only", "requires_confirmation", "blocked"}
        and (
            "requires_clarification" in escalation_reasons
            or "ambiguous_command" in escalation_reasons
            or "missing_target" in escalation_reasons
        )
    )

    goal_id: int | None = None
    goal_description = _goal_description(event, internal_intent)
    if outcome not in {"blocked", "store_only"} and internal_intent != "request_clarification" and not requires_clarification_only:
        goal = Goal(
            objective_id=None,
            task_id=None,
            goal_type=f"gateway_{internal_intent}",
            goal_description=goal_description,
            requested_by="gateway",
            priority="normal",
            status="new" if outcome == "auto_execute" else "proposed",
        )
        db.add(goal)
        await db.flush()
        goal_id = goal.id

    resolution = InputEventResolution(
        input_event_id=event.id,
        internal_intent=internal_intent,
        confidence_tier=confidence_tier,
        outcome=outcome,
        resolution_status=outcome,
        safety_decision=safety_decision,
        reason=reason,
        clarification_prompt=clarification_prompt,
        escalation_reasons=escalation_reasons,
        capability_name=capability_name,
        capability_registered=capability_registered,
        capability_enabled=capability_enabled,
        goal_id=goal_id,
        proposed_goal_description=goal_description,
        proposed_actions=_proposed_actions(internal_intent, capability_name, goal_description),
        metadata_json={
            "source": event.source,
            "confidence": event.confidence,
            "safety_flags": event.safety_flags,
            "target_system": event.target_system,
            "memory_signal": memory_signal,
        },
    )
    db.add(resolution)
    await db.flush()

    await write_journal(
        db,
        actor="gateway",
        action="resolve_input_event",
        target_type="input_event",
        target_id=str(event.id),
        summary=f"Resolved input event {event.id} -> {internal_intent} ({safety_decision})",
        metadata_json={
            "resolution_id": resolution.id,
            "goal_id": goal_id,
            "capability_name": capability_name,
            "reason": reason,
            "outcome": outcome,
            "confidence_tier": confidence_tier,
            "escalation_reasons": escalation_reasons,
            "clarification_prompt": clarification_prompt,
        },
    )
    return resolution


async def _create_or_update_execution_binding(
    *,
    event: InputEvent,
    resolution: InputEventResolution,
    capability_name: str,
    db: AsyncSession,
    force_dispatch: bool = False,
    arguments_json: dict | None = None,
    safety_mode: str = "standard",
    requested_executor: str = "tod",
) -> CapabilityExecution | None:
    if not capability_name:
        return None

    existing = (
        await db.execute(select(CapabilityExecution).where(CapabilityExecution.input_event_id == event.id))
    ).scalars().first()

    blocked_like = resolution.outcome in {"blocked", "store_only"}
    if blocked_like and not force_dispatch:
        decision = "blocked"
        status = "blocked"
        reason = resolution.reason or "resolution_blocked"
    elif resolution.outcome == "auto_execute" or force_dispatch:
        decision = "auto_dispatch"
        status = "dispatched"
        reason = "approved_for_dispatch"
    else:
        decision = "requires_confirmation"
        status = "pending_confirmation"
        reason = resolution.reason or "confirmation_required"

    payload_args = arguments_json or {}
    metadata_feedback = {
        "resolution_outcome": resolution.outcome,
        "escalation_reasons": resolution.escalation_reasons,
    }

    if existing is None:
        execution = CapabilityExecution(
            input_event_id=event.id,
            resolution_id=resolution.id,
            goal_id=resolution.goal_id,
            capability_name=capability_name,
            arguments_json=payload_args,
            safety_mode=safety_mode,
            requested_executor=requested_executor,
            dispatch_decision=decision,
            status=status,
            reason=reason,
            feedback_json=metadata_feedback,
        )
        db.add(execution)
        await db.flush()
    else:
        existing.resolution_id = resolution.id
        existing.goal_id = resolution.goal_id
        existing.capability_name = capability_name
        existing.arguments_json = payload_args or existing.arguments_json
        existing.safety_mode = safety_mode
        existing.requested_executor = requested_executor
        existing.dispatch_decision = decision
        existing.status = status
        existing.reason = reason
        existing.feedback_json = {
            **(existing.feedback_json or {}),
            **metadata_feedback,
        }
        execution = existing

    await write_journal(
        db,
        actor="gateway",
        action="bind_capability_execution",
        target_type="input_event",
        target_id=str(event.id),
        summary=f"Execution binding {execution.id} for {capability_name}: {status}",
        metadata_json={
            "execution_id": execution.id,
            "dispatch_decision": execution.dispatch_decision,
            "status": execution.status,
            "requested_executor": execution.requested_executor,
        },
    )
    return execution


async def _store_normalized(payload: NormalizedInputCreate, db: AsyncSession) -> dict:
    event = InputEvent(
        source=payload.source,
        raw_input=payload.raw_input,
        parsed_intent=payload.parsed_intent,
        confidence=payload.confidence,
        target_system=payload.target_system,
        requested_goal=payload.requested_goal,
        safety_flags=payload.safety_flags,
        metadata_json=payload.metadata_json,
        normalized=True,
    )
    db.add(event)
    await db.flush()

    await write_journal(
        db,
        actor="gateway",
        action="normalize_input",
        target_type="input_event",
        target_id=str(event.id),
        summary=f"Normalized input from source={event.source} intent={event.parsed_intent}",
        metadata_json={
            "source": event.source,
            "target_system": event.target_system,
            "confidence": event.confidence,
            "safety_flags": event.safety_flags,
        },
    )

    resolution = await _resolve_event(event, db)
    execution = await _create_or_update_execution_binding(
        event=event,
        resolution=resolution,
        capability_name=resolution.capability_name,
        db=db,
        arguments_json=_default_execution_arguments(event, resolution.capability_name),
    )

    await db.commit()
    await db.refresh(event)
    await db.refresh(resolution)
    event_out = _to_input_out(event)
    event_out["resolution"] = _to_resolution_out(resolution)
    if execution is not None:
        await db.refresh(execution)
        event_out["execution"] = _to_execution_out(execution)
    return event_out


@router.post("/intake")
async def intake_normalized(payload: NormalizedInputCreate, db: AsyncSession = Depends(get_db)) -> dict:
    return await _store_normalized(payload, db)


@router.post("/intake/text")
async def intake_text(payload: TextInputAdapterRequest, db: AsyncSession = Depends(get_db)) -> dict:
    normalized = NormalizedInputCreate(
        source="text",
        raw_input=payload.text,
        parsed_intent=payload.parsed_intent,
        confidence=payload.confidence,
        target_system=payload.target_system,
        requested_goal=payload.requested_goal,
        safety_flags=payload.safety_flags,
        metadata_json={**payload.metadata_json, "adapter": "text"},
    )
    return await _store_normalized(normalized, db)


@router.post("/intake/ui")
async def intake_ui(payload: UiInputAdapterRequest, db: AsyncSession = Depends(get_db)) -> dict:
    normalized = NormalizedInputCreate(
        source="ui",
        raw_input=payload.command,
        parsed_intent=payload.parsed_intent,
        confidence=payload.confidence,
        target_system=payload.target_system,
        requested_goal=payload.requested_goal,
        safety_flags=payload.safety_flags,
        metadata_json={**payload.metadata_json, "adapter": "ui"},
    )
    return await _store_normalized(normalized, db)


@router.post("/intake/api")
async def intake_api(payload: ApiInputAdapterRequest, db: AsyncSession = Depends(get_db)) -> dict:
    raw_input = payload.raw_input or json.dumps(payload.payload, sort_keys=True)
    normalized = NormalizedInputCreate(
        source="api",
        raw_input=raw_input,
        parsed_intent=payload.parsed_intent,
        confidence=payload.confidence,
        target_system=payload.target_system,
        requested_goal=payload.requested_goal,
        safety_flags=payload.safety_flags,
        metadata_json={**payload.metadata_json, "adapter": "api", "payload": payload.payload},
    )
    return await _store_normalized(normalized, db)


@router.post("/voice/input")
async def voice_input(payload: VoiceInputAdapterRequest, db: AsyncSession = Depends(get_db)) -> dict:
    normalized = NormalizedInputCreate(
        source="voice",
        raw_input=payload.transcript,
        parsed_intent=payload.parsed_intent,
        confidence=payload.confidence,
        target_system=payload.target_system,
        requested_goal=payload.requested_goal,
        safety_flags=payload.safety_flags,
        metadata_json={**payload.metadata_json, "adapter": "voice_transcript"},
    )
    return await _store_normalized(normalized, db)


@router.post("/voice/output")
async def voice_output(payload: SpeechOutputRequest, db: AsyncSession = Depends(get_db)) -> dict:
    output_validation = validate_voice_output(payload.message, payload.priority)
    delivery_status = "queued" if output_validation["allowed"] else "blocked"
    failure_reason = "" if output_validation["allowed"] else output_validation["reason"]

    output_action = SpeechOutputAction(
        requested_text=payload.message,
        voice_profile=payload.voice_profile,
        channel=payload.channel,
        priority=payload.priority,
        delivery_status=delivery_status,
        failure_reason=failure_reason,
        metadata_json={
            **payload.metadata_json,
            "validation": output_validation,
        },
    )
    db.add(output_action)
    await db.flush()

    await write_journal(
        db,
        actor="gateway",
        action="speech_output_execute",
        target_type="voice",
        target_id=str(output_action.id),
        summary=payload.message,
        metadata_json={
            "voice_profile": payload.voice_profile,
            "channel": payload.channel,
            "priority": payload.priority,
            "delivery_status": delivery_status,
            "failure_reason": failure_reason,
            **payload.metadata_json,
        },
    )
    await db.commit()
    return {
        "status": delivery_status,
        "spoken_text": payload.message,
        "output_action_id": output_action.id,
        "requested_text": payload.message,
        "voice_profile": payload.voice_profile,
        "channel": payload.channel,
        "priority": payload.priority,
        "delivery_status": delivery_status,
        "failure_reason": failure_reason,
        "metadata_json": payload.metadata_json,
    }


@router.post("/vision/observations")
async def vision_observation(payload: VisionObservationRequest, db: AsyncSession = Depends(get_db)) -> dict:
    normalized = NormalizedInputCreate(
        source="vision",
        raw_input=payload.raw_observation,
        parsed_intent="vision_observation",
        confidence=payload.confidence,
        target_system=payload.target_system,
        requested_goal=payload.proposed_goal,
        safety_flags=payload.safety_flags,
        metadata_json={
            **payload.metadata_json,
            "detected_labels": payload.detected_labels,
            "adapter": "vision",
        },
    )
    return await _store_normalized(normalized, db)


@router.get("/intake")
async def list_intake(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(InputEvent).order_by(InputEvent.id.desc()))).scalars().all()
    return [_to_input_out(item) for item in rows]


@router.get("/events")
async def list_events(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(InputEvent).order_by(InputEvent.id.desc()))).scalars().all()
    return [_to_input_out(item) for item in rows]


@router.get("/events/{event_id}")
async def get_event(event_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    event = await db.get(InputEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    return _to_input_out(event)


@router.get("/events/{event_id}/resolution")
async def get_event_resolution(event_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    resolution = (
        await db.execute(select(InputEventResolution).where(InputEventResolution.input_event_id == event_id))
    ).scalars().first()
    if not resolution:
        raise HTTPException(status_code=404, detail="event resolution not found")
    return _to_resolution_out(resolution)


@router.post("/events/{event_id}/promote-to-goal")
async def promote_event_to_goal(
    event_id: int,
    payload: PromoteEventToGoalRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    event = await db.get(InputEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")

    resolution = (
        await db.execute(select(InputEventResolution).where(InputEventResolution.input_event_id == event_id))
    ).scalars().first()
    if not resolution:
        raise HTTPException(status_code=404, detail="event resolution not found")
    if resolution.outcome in {"blocked", "store_only"} and not payload.force:
        raise HTTPException(status_code=422, detail="blocked resolution cannot be promoted without force")

    goal: Goal | None = None
    if resolution.goal_id is not None:
        goal = await db.get(Goal, resolution.goal_id)

    if goal is None:
        goal = Goal(
            objective_id=None,
            task_id=None,
            goal_type=f"gateway_{resolution.internal_intent}",
            goal_description=resolution.proposed_goal_description,
            requested_by=payload.requested_by,
            priority="normal",
            status="new",
        )
        db.add(goal)
        await db.flush()
        resolution.goal_id = goal.id
    else:
        goal.status = "new"
        goal.requested_by = payload.requested_by

    resolution.outcome = "auto_execute"
    resolution.resolution_status = "auto_execute"
    resolution.safety_decision = "auto_execute"
    resolution.reason = "promoted_by_user"
    resolution.metadata_json = {
        **resolution.metadata_json,
        "promoted": True,
        "promoted_by": payload.requested_by,
    }

    await write_journal(
        db,
        actor=payload.requested_by,
        action="promote_event_to_goal",
        target_type="input_event",
        target_id=str(event_id),
        summary=f"Promoted event {event_id} to goal {resolution.goal_id}",
        metadata_json={
            "resolution_id": resolution.id,
            "goal_id": resolution.goal_id,
            "forced": payload.force,
        },
    )

    bound_execution = await _create_or_update_execution_binding(
        event=event,
        resolution=resolution,
        capability_name=resolution.capability_name,
        db=db,
        force_dispatch=True,
    )

    await db.commit()
    await db.refresh(resolution)
    response = _to_resolution_out(resolution)
    if bound_execution is not None:
        await db.refresh(bound_execution)
        response["execution"] = _to_execution_out(bound_execution)
    return response


@router.post("/events/{event_id}/execution/dispatch")
async def dispatch_event_execution(
    event_id: int,
    payload: ExecutionDispatchRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    event = await db.get(InputEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")

    resolution = (
        await db.execute(select(InputEventResolution).where(InputEventResolution.input_event_id == event_id))
    ).scalars().first()
    if not resolution:
        raise HTTPException(status_code=404, detail="event resolution not found")
    if not resolution.capability_name:
        raise HTTPException(status_code=422, detail="resolution has no executable capability")

    execution = await _create_or_update_execution_binding(
        event=event,
        resolution=resolution,
        capability_name=resolution.capability_name,
        db=db,
        force_dispatch=payload.force,
        arguments_json=payload.arguments_json,
        safety_mode=payload.safety_mode,
        requested_executor=payload.requested_executor,
    )
    if execution is None:
        raise HTTPException(status_code=422, detail="execution binding unavailable")

    await db.commit()
    await db.refresh(execution)
    return _to_execution_out(execution)


@router.get("/events/{event_id}/execution")
async def get_event_execution(event_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    execution = (
        await db.execute(select(CapabilityExecution).where(CapabilityExecution.input_event_id == event_id))
    ).scalars().first()
    if not execution:
        raise HTTPException(status_code=404, detail="event execution not found")
    return _to_execution_out(execution)


@router.get("/capabilities/executions/{execution_id}")
async def get_capability_execution(execution_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")
    return _to_execution_out(execution)


@router.get("/capabilities/executions/{execution_id}/handoff")
async def get_capability_execution_handoff(
    execution_id: int,
    db: AsyncSession = Depends(get_db),
) -> CapabilityExecutionHandoffOut:
    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")

    event = await db.get(InputEvent, execution.input_event_id)
    resolution = await db.get(InputEventResolution, execution.resolution_id) if execution.resolution_id else None

    action_step = None
    if resolution and isinstance(resolution.proposed_actions, list) and len(resolution.proposed_actions) > 0:
        action_step = resolution.proposed_actions[0]

    return CapabilityExecutionHandoffOut(
        execution_id=execution.id,
        goal_ref={
            "goal_id": execution.goal_id,
            "goal_type": "gateway_goal" if execution.goal_id else "none",
        },
        action_ref={
            "resolution_id": execution.resolution_id,
            "action_step": action_step,
            "action_ref": f"resolution:{execution.resolution_id}:step:1" if execution.resolution_id else "",
        },
        capability_name=execution.capability_name,
        arguments_json=execution.arguments_json,
        safety_mode=execution.safety_mode,
        requested_executor=execution.requested_executor,
        dispatch_decision=execution.dispatch_decision,
        status=execution.status,
        correlation_metadata={
            "input_event_id": execution.input_event_id,
            "event_source": event.source if event else "unknown",
            "target_system": event.target_system if event else "unknown",
            "requested_goal": event.requested_goal if event else "",
            "event_metadata": event.metadata_json if event else {},
            "resolution_outcome": resolution.outcome if resolution else "unknown",
            "escalation_reasons": resolution.escalation_reasons if resolution else [],
        },
    )


@router.get("/capabilities/executions/{execution_id}/feedback")
async def get_capability_execution_feedback(execution_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")
    return {
        "execution_id": execution.id,
        "status": execution.status,
        "reason": execution.reason,
        "feedback_json": execution.feedback_json,
    }


@router.post("/capabilities/executions/{execution_id}/feedback")
async def update_capability_execution_feedback(
    execution_id: int,
    payload: ExecutionFeedbackUpdateRequest,
    x_mim_feedback_key: str | None = Header(default=None, alias="X-MIM-Feedback-Key"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")

    _enforce_feedback_boundary(payload.actor, x_mim_feedback_key)

    current_status = execution.status
    next_status, resolved_reason, runtime_outcome = _resolve_feedback_status(payload)
    if next_status != current_status:
        allowed = ALLOWED_EXECUTION_TRANSITIONS.get(current_status, set())
        if next_status not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"invalid execution status transition: {current_status} -> {next_status}",
            )

    history = list(execution.feedback_json.get("history", [])) if isinstance(execution.feedback_json, dict) else []
    history.append(
        {
            "from": current_status,
            "to": next_status,
            "reason": resolved_reason,
            "actor": payload.actor,
            "runtime_outcome": runtime_outcome,
            "recovery_state": payload.recovery_state,
            "correlation": payload.correlation_json,
            "feedback": payload.feedback_json,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )

    merged_feedback = {
        **(execution.feedback_json if isinstance(execution.feedback_json, dict) else {}),
        **payload.feedback_json,
    }
    if runtime_outcome:
        merged_feedback["runtime_outcome"] = runtime_outcome
    if payload.recovery_state:
        merged_feedback["recovery_state"] = payload.recovery_state
    if payload.correlation_json:
        merged_feedback["correlation_json"] = payload.correlation_json

    if execution.capability_name == "workspace_scan":
        observations = payload.feedback_json.get("observations") if isinstance(payload.feedback_json, dict) else None
        if isinstance(observations, list) and observations:
            detected_labels: list[str] = []
            workspace_observation_ids: list[int] = []
            workspace_object_ids: list[int] = []
            workspace_object_relation_ids: list[int] = []
            scanned_object_memories: list[WorkspaceObjectMemory] = []
            observed_labels_by_zone: dict[str, set[str]] = {}
            execution_args = execution.arguments_json if isinstance(execution.arguments_json, dict) else {}
            for item in observations:
                if isinstance(item, dict):
                    label = str(item.get("label", "")).strip()
                    if label:
                        detected_labels.append(label)
                    zone = str(item.get("zone") or execution_args.get("scan_area") or "workspace").strip() or "workspace"
                    observed_labels_by_zone.setdefault(zone, set()).add(label.lower())

                    observation = await _upsert_workspace_observation(
                        db=db,
                        execution=execution,
                        observation_item=item,
                    )
                    if observation:
                        workspace_observation_ids.append(observation.id)

                    object_memory = await _upsert_object_identity(
                        db=db,
                        execution=execution,
                        observation_item=item,
                    )
                    if object_memory:
                        workspace_object_ids.append(object_memory.id)
                        scanned_object_memories.append(object_memory)

            await _update_missing_object_identities(
                db=db,
                execution=execution,
                observed_labels_by_zone=observed_labels_by_zone,
            )

            workspace_object_relation_ids = await _update_object_relations_for_scan(
                db=db,
                execution=execution,
                object_memories=scanned_object_memories,
            )

            workspace_proposal_ids = await _generate_workspace_state_proposals(
                db=db,
                execution=execution,
                workspace_object_ids=workspace_object_ids,
            )

            observation_event = InputEvent(
                source="vision",
                raw_input=f"workspace_scan observation set from execution {execution.id}",
                parsed_intent="vision_observation",
                confidence=float(payload.feedback_json.get("observation_confidence", 0.8)),
                target_system="mim",
                requested_goal="",
                safety_flags=["requires_confirmation"],
                metadata_json={
                    "adapter": "workspace_scan",
                    "execution_id": execution.id,
                    "detected_labels": detected_labels,
                    "observations": observations,
                },
                normalized=True,
            )
            db.add(observation_event)
            await db.flush()
            merged_feedback["observation_event_id"] = observation_event.id
            merged_feedback["observations"] = observations
            if detected_labels:
                merged_feedback["detected_labels"] = detected_labels
            if workspace_observation_ids:
                merged_feedback["workspace_observation_ids"] = workspace_observation_ids
            if workspace_object_ids:
                merged_feedback["workspace_object_ids"] = workspace_object_ids
            if workspace_object_relation_ids:
                merged_feedback["workspace_object_relation_ids"] = workspace_object_relation_ids
            if workspace_proposal_ids:
                merged_feedback["workspace_proposal_ids"] = workspace_proposal_ids

    execution.status = next_status
    execution.reason = resolved_reason
    execution.feedback_json = {
        **merged_feedback,
        "last_actor": payload.actor,
        "last_reason": resolved_reason,
        "history": history,
    }

    await write_journal(
        db,
        actor=payload.actor,
        action="update_capability_execution_feedback",
        target_type="capability_execution",
        target_id=str(execution.id),
        summary=f"Execution {execution.id} status {current_status}->{next_status}",
        metadata_json={
            "from": current_status,
            "to": next_status,
            "reason": resolved_reason,
            "runtime_outcome": runtime_outcome,
            "recovery_state": payload.recovery_state,
        },
    )

    await db.commit()
    await db.refresh(execution)
    return {
        "execution_id": execution.id,
        "status": execution.status,
        "reason": execution.reason,
        "feedback_json": execution.feedback_json,
    }


@router.post("/capabilities")
async def register_capability(payload: CapabilityRegistrationCreate, db: AsyncSession = Depends(get_db)) -> dict:
    existing = (
        await db.execute(select(CapabilityRegistration).where(CapabilityRegistration.capability_name == payload.capability_name))
    ).scalars().first()
    if existing:
        existing.category = payload.category
        existing.description = payload.description
        existing.requires_confirmation = payload.requires_confirmation
        existing.enabled = payload.enabled
        existing.safety_policy = payload.safety_policy
        await write_journal(
            db,
            actor="gateway",
            action="update_capability",
            target_type="capability",
            target_id=str(existing.id),
            summary=f"Updated capability {existing.capability_name}",
        )
        await db.commit()
        await db.refresh(existing)
        return {
            "capability_id": existing.id,
            "capability_name": existing.capability_name,
            "category": existing.category,
            "description": existing.description,
            "requires_confirmation": existing.requires_confirmation,
            "enabled": existing.enabled,
            "safety_policy": existing.safety_policy,
            "created_at": existing.created_at,
        }

    capability = CapabilityRegistration(
        capability_name=payload.capability_name,
        category=payload.category,
        description=payload.description,
        requires_confirmation=payload.requires_confirmation,
        enabled=payload.enabled,
        safety_policy=payload.safety_policy,
    )
    db.add(capability)
    await db.flush()

    await write_journal(
        db,
        actor="gateway",
        action="register_capability",
        target_type="capability",
        target_id=str(capability.id),
        summary=f"Registered capability {capability.capability_name}",
    )

    await db.commit()
    await db.refresh(capability)
    return {
        "capability_id": capability.id,
        "capability_name": capability.capability_name,
        "category": capability.category,
        "description": capability.description,
        "requires_confirmation": capability.requires_confirmation,
        "enabled": capability.enabled,
        "safety_policy": capability.safety_policy,
        "created_at": capability.created_at,
    }


@router.get("/capabilities")
async def list_capabilities(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(CapabilityRegistration).order_by(CapabilityRegistration.id.desc()))).scalars().all()
    return [
        {
            "capability_id": row.id,
            "capability_name": row.capability_name,
            "category": row.category,
            "description": row.description,
            "requires_confirmation": row.requires_confirmation,
            "enabled": row.enabled,
            "safety_policy": row.safety_policy,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/vision-policy")
async def get_vision_policy() -> dict:
    policy = load_vision_policy()
    thresholds = policy.get("thresholds", {})
    return {
        "policy_version": "vision-policy-v1",
        "policy_path": settings.vision_policy_path,
        "thresholds": {
            "high": float(thresholds.get("high", 0.85)),
            "medium": float(thresholds.get("medium", 0.6)),
        },
        "allow_auto_propose": bool(policy.get("allow_auto_propose", True)),
        "auto_execute_safe_intents": list(policy.get("auto_execute_safe_intents", [])),
        "blocked_capability_implications": list(policy.get("blocked_capability_implications", [])),
        "label_overrides": policy.get("label_overrides", {}),
    }


@router.get("/voice-policy")
async def get_voice_policy() -> dict:
    policy = load_voice_policy()
    thresholds = policy.get("thresholds", {})
    return {
        "policy_version": "voice-policy-v1",
        "policy_path": settings.voice_policy_path,
        "thresholds": {
            "high": float(thresholds.get("high", 0.85)),
            "medium": float(thresholds.get("medium", 0.6)),
        },
        "low_confidence_behavior": str(policy.get("low_confidence_behavior", "store_only")),
        "require_confirmation_intents": list(policy.get("require_confirmation_intents", [])),
        "blocked_capability_implications": list(policy.get("blocked_capability_implications", [])),
        "ambiguous_keywords": list(policy.get("ambiguous_keywords", [])),
        "unsafe_keywords": list(policy.get("unsafe_keywords", [])),
        "target_required_verbs": list(policy.get("target_required_verbs", [])),
        "max_output_chars": int(policy.get("max_output_chars", 240)),
        "allowed_output_priorities": list(policy.get("allowed_output_priorities", ["low", "normal", "high"])),
    }
