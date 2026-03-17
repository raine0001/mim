import json
import base64
import io
import asyncio
import logging
import os
import importlib
import uuid
import re
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.config import settings
from core.journal import write_journal
from core.models import CapabilityExecution, CapabilityRegistration, Goal, InputEvent, InputEventResolution, MemoryEntry, SpeechOutputAction, Task, WorkspaceMonitoringState, WorkspaceObservation, WorkspaceObjectMemory, WorkspaceObjectRelation, WorkspacePerceptionSource, WorkspaceProposal, WorkspaceZone, WorkspaceZoneRelation
from core.voice_policy import evaluate_voice_policy, load_voice_policy, validate_voice_output
from core.vision_policy import evaluate_vision_policy
from core.vision_policy import load_vision_policy
from core.schemas import (
    ApiInputAdapterRequest,
    CapabilityRegistrationCreate,
    CapabilityExecutionHandoffOut,
    ExecutionFeedbackUpdateRequest,
    ExecutionDispatchRequest,
    LiveCameraAdapterRequest,
    LiveMicAdapterRequest,
    NormalizedInputCreate,
    PerceptionSourceOut,
    PromoteEventToGoalRequest,
    SpeechOutputRequest,
    TextInputAdapterRequest,
    UiInputAdapterRequest,
    VisionObservationRequest,
    VoiceInputAdapterRequest,
)

router = APIRouter()
logger = logging.getLogger(__name__)

try:
    edge_tts = importlib.import_module("edge_tts")
except Exception:  # pragma: no cover - optional runtime dependency
    edge_tts = None

OBSERVATION_DEDUPE_WINDOW_SECONDS = 300
OBSERVATION_RECENT_WINDOW_SECONDS = 600
OBSERVATION_OUTDATED_WINDOW_SECONDS = 3600
OBJECT_IDENTITY_WINDOW_SECONDS = 1800
OBJECT_STALE_WINDOW_SECONDS = 7200
OBJECT_MATCH_THRESHOLD = 0.65
PROPOSAL_DEDUPE_WINDOW_SECONDS = 900

PERCEPTION_STALE_SECONDS = 60
MIC_TRANSCRIBE_DEBUG_LOG = Path(__file__).resolve().parents[2] / "runtime" / "logs" / "mic_transcribe_debug.jsonl"


def _mic_debug_enabled(payload: dict) -> bool:
    if str(os.getenv("MIM_MIC_DEBUG", "")).strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return bool(payload.get("debug"))


def _append_mic_debug_event(event: dict) -> None:
    try:
        MIC_TRANSCRIBE_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MIC_TRANSCRIBE_DEBUG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        logger.debug("mic_debug_event_write_failed: %s", exc)


def _mic_debug_detail(message: str, trace_id: str) -> dict:
    return {
        "message": message,
        "trace_id": trace_id,
        "debug_log_path": str(MIC_TRANSCRIBE_DEBUG_LOG),
    }


def _classify_provider_error(detail: str) -> dict:
    lowered = detail.lower()
    forbidden_hint = "forbidden" in lowered or " 403" in lowered or "status 403" in lowered
    unauthorized_hint = "unauthorized" in lowered or " 401" in lowered or "status 401" in lowered
    quota_hint = "quota" in lowered or "rate" in lowered or "429" in lowered
    blocked_hint = "blocked" in lowered or "denied" in lowered
    return {
        "forbidden_hint": forbidden_hint,
        "unauthorized_hint": unauthorized_hint,
        "quota_or_rate_hint": quota_hint,
        "blocked_hint": blocked_hint,
        "upstream_status_hint": 403 if forbidden_hint else (401 if unauthorized_hint else (429 if quota_hint else None)),
    }


def _resolve_mic_provider_mode(payload: dict) -> str:
    raw = str(payload.get("provider") or os.getenv("MIM_MIC_PROVIDER") or "").strip().lower()
    if raw in {"local", "pocketsphinx", "offline"}:
        return "local"
    if raw in {"openai", "whisper", "gpt4o", "gpt-4o-mini-transcribe"}:
        return "openai"
    if raw in {"google", "google_web_speech", "cloud"}:
        return "google"
    if raw == "auto":
        return "auto"
    return "auto" if settings.allow_web_access else "local"


def _local_stt_min_confidence() -> float:
    raw = str(os.getenv("MIM_LOCAL_STT_MIN_CONFIDENCE") or "0.55").strip()
    try:
        value = float(raw)
    except ValueError:
        value = 0.55
    return max(0.0, min(1.0, value))


def _openai_auto_stt_enabled() -> bool:
    raw = str(os.getenv("MIM_MIC_OPENAI_AUTO") or "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _is_low_quality_local_transcript(text: str) -> bool:
    normalized = re.sub(r"[^a-z\s]", " ", str(text or "").lower())
    tokens = [token for token in normalized.split() if token]
    if not tokens:
        return True

    compact = "".join(tokens)
    if len(compact) < 5:
        return True

    if len(tokens) >= 2 and all(len(token) <= 2 for token in tokens):
        return True

    unique_ratio = len(set(tokens)) / float(len(tokens))
    if len(tokens) >= 4 and unique_ratio < 0.5:
        return True

    filler_words = {"um", "uh", "erm", "hmm", "mm", "ah", "eh"}
    if all(token in filler_words for token in tokens):
        return True

    return False


def _normalize_prompt_key(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _is_clarification_driven(escalation_reasons: list[str], outcome: str) -> bool:
    reasons = {str(item).strip().lower() for item in (escalation_reasons or []) if str(item).strip()}
    clarification_reasons = {
        "requires_clarification",
        "ambiguous_command",
        "missing_target",
        "low_transcript_confidence",
    }
    if "unsafe_action_request" in reasons:
        return False
    return outcome in {"store_only", "requires_confirmation", "blocked"} and bool(reasons.intersection(clarification_reasons))


def _build_one_clarifier_prompt(transcript: str) -> str:
    request = _normalize_prompt_key(transcript)[:72]
    if request:
        return (
            f"For '{request}', I'm missing one detail: do you want me to answer a question, suggest a plan, or take an action?"
        )
    return "I'm missing one detail: do you want me to answer a question, suggest a plan, or take an action?"


def _build_clarification_limit_prompt(escalation_reasons: list[str], transcript: str) -> str:
    reasons = {str(item).strip().lower() for item in (escalation_reasons or []) if str(item).strip()}
    if "missing_target" in reasons:
        missing = "the exact object or location"
    elif "low_transcript_confidence" in reasons:
        missing = "a clearer request"
    else:
        missing = "the intended outcome"
    request = _normalize_prompt_key(transcript)[:72]
    if request:
        return (
            f"For '{request}', I am still missing {missing}. Options: 1) ask a question, 2) suggest a short plan, 3) request an action."
        )
    return f"I am still missing {missing}. Options: 1) ask a question, 2) suggest a short plan, 3) request an action."


async def _recent_voice_clarification_count(db: AsyncSession, *, within_seconds: int = 180) -> int:
    threshold = datetime.now(timezone.utc) - timedelta(seconds=max(30, int(within_seconds)))
    rows = (
        await db.execute(
            select(InputEventResolution)
            .where(InputEventResolution.created_at >= threshold)
            .where(InputEventResolution.clarification_prompt != "")
            .order_by(InputEventResolution.id.desc())
            .limit(12)
        )
    ).scalars().all()

    count = 0
    for row in rows:
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if str(meta.get("source", "")).strip().lower() != "voice":
            continue
        if not _is_clarification_driven(row.escalation_reasons or [], str(row.outcome or "")):
            continue
        count += 1
    return count


def _lang_to_iso639_1(language: str) -> str:
    normalized = str(language or "en-US").strip().replace("_", "-").lower()
    if not normalized:
        return "en"
    if "-" in normalized:
        return normalized.split("-", 1)[0]
    return normalized[:2]


def _select_tts_voice(language: str, requested_voice: str) -> str:
    requested = str(requested_voice or "").strip()
    if requested:
        return requested

    lang = str(language or "en-US").strip().lower()
    if lang.startswith("en"):
        return "en-US-EmmaMultilingualNeural"
    if lang.startswith("es"):
        return "es-ES-ElviraNeural"
    if lang.startswith("fr"):
        return "fr-FR-DeniseNeural"
    if lang.startswith("de"):
        return "de-DE-SeraphinaMultilingualNeural"
    if lang.startswith("it"):
        return "it-IT-ElsaNeural"
    if lang.startswith("pt"):
        return "pt-BR-FranciscaNeural"
    return "en-US-EmmaMultilingualNeural"


def _is_safe_web_url(raw_url: str) -> bool:
    try:
        parsed = urlparse(str(raw_url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = str(parsed.hostname or "").strip().lower()
    if not hostname:
        return False
    blocked_hosts = {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "::1",
    }
    if hostname in blocked_hosts:
        return False
    if hostname.endswith(".local"):
        return False
    return True


def _extract_visible_text_from_html(raw_html: str, *, max_chars: int = 12000) -> tuple[str, str]:
    html = str(raw_html or "")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""

    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(r"<style\b[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL)
    with_breaks = re.sub(r"</?(p|div|h1|h2|h3|h4|h5|h6|li|br|tr|section|article)[^>]*>", "\n", without_styles, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", with_breaks)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return title, text


def _build_web_summary(*, title: str, text: str, max_sentences: int = 4) -> str:
    cleaned = str(text or "").strip()
    if not cleaned:
        return "I could access the page, but I could not extract readable text to summarize."

    # Keep summarization deterministic and dependency-free.
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    sentences = [item.strip() for item in sentences if item and item.strip()]
    selected: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        normalized = sentence.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        selected.append(sentence)
        if len(selected) >= max(1, min(8, int(max_sentences))):
            break

    if not selected:
        selected = [cleaned[:420].rstrip()]

    prefix = f"Page title: {title}. " if title else ""
    return prefix + " ".join(selected)

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
        if _is_clarification_driven(escalation_reasons, outcome):
            prior_clarifications = await _recent_voice_clarification_count(db, within_seconds=180)
            if prior_clarifications <= 0:
                clarification_prompt = _build_one_clarifier_prompt(event.raw_input)
            else:
                clarification_prompt = _build_clarification_limit_prompt(escalation_reasons, event.raw_input)
                if "clarification_limit_reached" not in escalation_reasons:
                    escalation_reasons.append("clarification_limit_reached")
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
            "clarification_prompt_key": _normalize_prompt_key(clarification_prompt),
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


def _hash_payload(parts: list[str]) -> str:
    joined = "|".join(str(item) for item in parts)
    return sha256(joined.encode("utf-8")).hexdigest()


async def _get_or_create_perception_source(
    *,
    source_type: str,
    device_id: str,
    session_id: str,
    is_remote: bool,
    db: AsyncSession,
) -> WorkspacePerceptionSource:
    row = (
        await db.execute(
            select(WorkspacePerceptionSource)
            .where(WorkspacePerceptionSource.source_type == source_type)
            .where(WorkspacePerceptionSource.device_id == device_id)
            .order_by(WorkspacePerceptionSource.id.desc())
            .limit(1)
        )
    ).scalars().first()
    if row:
        if session_id.strip():
            row.session_id = session_id.strip()
        row.is_remote = bool(is_remote)
        return row

    row = WorkspacePerceptionSource(
        source_type=source_type,
        device_id=device_id,
        session_id=session_id,
        is_remote=bool(is_remote),
        status="active",
        health_status="healthy",
        min_interval_seconds=2,
        duplicate_window_seconds=20,
        confidence_floor=0.5,
        metadata_json={},
    )
    db.add(row)
    await db.flush()
    return row


def _is_duplicate_event(*, row: WorkspacePerceptionSource, fingerprint: str, now: datetime) -> bool:
    if not fingerprint or not row.last_event_fingerprint:
        return False
    if row.last_event_fingerprint != fingerprint:
        return False
    if not row.last_seen_at:
        return False
    age_seconds = max(0.0, (now - row.last_seen_at).total_seconds())
    return age_seconds <= float(max(1, int(row.duplicate_window_seconds or 20)))


def _is_interval_blocked(*, row: WorkspacePerceptionSource, now: datetime) -> bool:
    if not row.last_accepted_at:
        return False
    age_seconds = max(0.0, (now - row.last_accepted_at).total_seconds())
    return age_seconds < float(max(0, int(row.min_interval_seconds or 0)))


def _to_perception_source_out(row: WorkspacePerceptionSource) -> dict:
    return {
        "source_id": int(row.id),
        "source_type": row.source_type,
        "device_id": row.device_id,
        "session_id": row.session_id,
        "is_remote": bool(row.is_remote),
        "status": row.status,
        "health_status": row.health_status,
        "last_seen_at": row.last_seen_at,
        "last_accepted_at": row.last_accepted_at,
        "accepted_count": int(row.accepted_count or 0),
        "dropped_count": int(row.dropped_count or 0),
        "duplicate_count": int(row.duplicate_count or 0),
        "low_confidence_count": int(row.low_confidence_count or 0),
        "min_interval_seconds": int(row.min_interval_seconds or 0),
        "duplicate_window_seconds": int(row.duplicate_window_seconds or 0),
        "confidence_floor": float(row.confidence_floor or 0.0),
        "metadata_json": row.metadata_json if isinstance(row.metadata_json, dict) else {},
        "created_at": row.created_at,
    }


def _normalize_text_for_learning(raw: str) -> str:
    return " ".join(str(raw or "").strip().split())


def _interaction_pref_signal(transcript: str) -> tuple[str, str]:
    text = _normalize_text_for_learning(transcript)
    lowered = text.lower()
    if not lowered:
        return "", ""

    patterns = [
        ("call_me", ["call me ", "my name is ", "i am ", "i'm "]),
        ("preference", ["i prefer ", "please use ", "i would like "]),
        ("like", ["i like ", "i love "]),
        ("dislike", ["i do not like ", "i don't like ", "i hate "]),
    ]
    for signal, prefixes in patterns:
        for prefix in prefixes:
            idx = lowered.find(prefix)
            if idx < 0:
                continue
            value = text[idx + len(prefix):].strip(" .,!?")
            if value:
                return signal, value[:140]
    return "", ""


async def _store_interaction_learning(
    *,
    transcript: str,
    confidence: float,
    source: WorkspacePerceptionSource,
    payload_metadata: dict,
    db: AsyncSession,
) -> int | None:
    clean = _normalize_text_for_learning(transcript)
    if not clean:
        return None

    compact = "".join(ch for ch in clean.lower() if ch.isalnum() or ch.isspace()).strip()
    if compact in {"hi", "hello", "hey", "ok", "okay", "thanks", "thank you"}:
        return None

    pref_type, pref_value = _interaction_pref_signal(clean)
    word_count = len([part for part in compact.split(" ") if part])
    if not pref_type and (word_count < 4 or float(confidence) < 0.6):
        return None

    transcript_hash = sha256(clean.lower().encode("utf-8")).hexdigest()[:16]
    existing = (
        await db.execute(
            select(MemoryEntry)
            .where(MemoryEntry.memory_class == "interaction_learning")
            .order_by(MemoryEntry.id.desc())
            .limit(8)
        )
    ).scalars().all()
    for row in existing:
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if str(meta.get("transcript_hash", "")) == transcript_hash:
            return None

    camera_row = (
        (
            await db.execute(
                select(WorkspacePerceptionSource)
                .where(WorkspacePerceptionSource.source_type == "camera")
                .order_by(WorkspacePerceptionSource.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    camera_payload = camera_row.last_event_payload_json if camera_row and isinstance(camera_row.last_event_payload_json, dict) else {}
    camera_label = str(camera_payload.get("object_label", "")).strip()

    summary = f"User said: {clean[:110]}"
    if pref_type and pref_value:
        summary = f"Preference learned ({pref_type}): {pref_value[:110]}"
    if camera_label:
        summary = f"{summary} | Surrounding: {camera_label}"

    memory = MemoryEntry(
        memory_class="interaction_learning",
        content=clean,
        summary=summary,
        metadata_json={
            "source": "live_mic_adapter",
            "device_id": source.device_id,
            "session_id": source.session_id,
            "confidence": float(confidence),
            "preference_signal": pref_type,
            "preference_value": pref_value,
            "camera_label": camera_label,
            "transcript_hash": transcript_hash,
            "adapter_metadata": payload_metadata if isinstance(payload_metadata, dict) else {},
        },
    )
    db.add(memory)
    await db.flush()
    return int(memory.id)


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


@router.post("/voice/tts")
async def voice_tts(payload: dict = Body(...)) -> Response:
    message = str(payload.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    if edge_tts is None:
        raise HTTPException(status_code=503, detail="edge-tts is not installed")

    language = str(payload.get("language") or "en-US").strip()
    requested_voice = str(payload.get("voice") or "").strip()
    voice = _select_tts_voice(language, requested_voice)

    # Keep payload bounded to prevent unreasonably large synthesis requests.
    safe_message = message[:800]

    try:
        communicator = edge_tts.Communicate(text=safe_message, voice=voice)
        audio_chunks: list[bytes] = []
        async for chunk in communicator.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                audio_chunks.append(chunk["data"])
        audio_bytes = b"".join(audio_chunks)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"tts_synthesis_failed: {exc}") from exc

    if not audio_bytes:
        raise HTTPException(status_code=502, detail="tts_synthesis_failed: empty_audio")

    return Response(
        content=audio_bytes,
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "no-store",
            "X-MIM-TTS-Voice": voice,
        },
    )


@router.post("/web/summarize")
async def summarize_web_page(payload: dict = Body(...), db: AsyncSession = Depends(get_db)) -> dict:
    if not settings.allow_web_access:
        raise HTTPException(status_code=403, detail="web_access_disabled")

    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="url is required")
    if not _is_safe_web_url(raw_url):
        raise HTTPException(status_code=422, detail="unsupported_or_unsafe_url")

    timeout_seconds = max(3, min(20, int(payload.get("timeout_seconds") or 12)))
    max_extract_chars = max(1200, min(30000, int(payload.get("max_extract_chars") or 12000)))
    max_summary_sentences = max(1, min(8, int(payload.get("max_summary_sentences") or 4)))

    req = urllib_request.Request(
        url=raw_url,
        headers={
            "User-Agent": "MIM-WebSummarizer/1.0 (+https://mim.local)",
            "Accept": "text/html,text/plain;q=0.9,*/*;q=0.3",
        },
    )

    try:
        with urllib_request.urlopen(req, timeout=timeout_seconds) as response:
            status_code = int(getattr(response, "status", 200) or 200)
            content_type = str(response.headers.get("Content-Type", "")).lower()
            raw_bytes = response.read(1_000_000)
    except urllib_error.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"web_fetch_http_error:{exc.code}") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"web_fetch_failed:{type(exc).__name__}") from exc

    decoded = raw_bytes.decode("utf-8", errors="replace")
    if "text/plain" in content_type:
        title = ""
        extracted = re.sub(r"\s+", " ", decoded).strip()
        if len(extracted) > max_extract_chars:
            extracted = extracted[:max_extract_chars].rstrip() + "..."
    else:
        title, extracted = _extract_visible_text_from_html(decoded, max_chars=max_extract_chars)

    summary = _build_web_summary(title=title, text=extracted, max_sentences=max_summary_sentences)

    memory = MemoryEntry(
        memory_class="external_web_summary",
        content=extracted[:2000],
        summary=summary[:400],
        metadata_json={
            "url": raw_url,
            "title": title,
            "content_type": content_type,
            "status_code": status_code,
            "extract_chars": len(extracted),
            "summary_sentences": max_summary_sentences,
            "source": "gateway_web_summarize",
        },
    )
    db.add(memory)
    await db.flush()

    await write_journal(
        db,
        actor="gateway",
        action="web_page_summarized",
        target_type="external_web",
        target_id=str(memory.id),
        summary=f"Summarized {raw_url}",
        metadata_json={
            "url": raw_url,
            "title": title,
            "status_code": status_code,
            "extract_chars": len(extracted),
        },
    )
    await db.commit()

    return {
        "ok": True,
        "url": raw_url,
        "title": title,
        "summary": summary,
        "excerpt": extracted[:800],
        "content_type": content_type,
        "status_code": status_code,
        "memory_id": int(memory.id),
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


@router.post("/perception/camera/events")
async def live_camera_adapter(payload: LiveCameraAdapterRequest, db: AsyncSession = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)
    source = await _get_or_create_perception_source(
        source_type=str(payload.source_type or "camera").strip() or "camera",
        device_id=str(payload.device_id).strip(),
        session_id=str(payload.session_id or "").strip(),
        is_remote=bool(payload.is_remote),
        db=db,
    )
    source.min_interval_seconds = int(payload.min_interval_seconds)
    source.duplicate_window_seconds = int(payload.duplicate_window_seconds)
    source.confidence_floor = float(payload.observation_confidence_floor)

    observations = payload.observations if isinstance(payload.observations, list) else []
    accepted_items = [item for item in observations if float(item.confidence) >= float(source.confidence_floor)]
    if not accepted_items:
        source.last_seen_at = now
        source.health_status = "degraded"
        source.dropped_count = int(source.dropped_count or 0) + 1
        source.low_confidence_count = int(source.low_confidence_count or 0) + 1
        await db.commit()
        return {
            "status": "discarded_low_confidence",
            "reason": "observation_confidence_below_floor",
            "source": _to_perception_source_out(source),
            "accepted_count": 0,
        }

    fingerprint = _hash_payload(
        [
            source.source_type,
            source.device_id,
            source.session_id,
            *[
                f"{item.object_label}:{item.zone}:{round(float(item.confidence), 3)}"
                for item in sorted(accepted_items, key=lambda entry: (entry.zone, entry.object_label, entry.confidence))
            ],
        ]
    )
    if _is_duplicate_event(row=source, fingerprint=fingerprint, now=now):
        source.last_seen_at = now
        source.health_status = "healthy"
        source.dropped_count = int(source.dropped_count or 0) + 1
        source.duplicate_count = int(source.duplicate_count or 0) + 1
        await db.commit()
        return {
            "status": "suppressed_duplicate",
            "reason": "duplicate_observation_batch",
            "source": _to_perception_source_out(source),
            "accepted_count": 0,
        }

    if _is_interval_blocked(row=source, now=now):
        source.last_seen_at = now
        source.health_status = "healthy"
        source.dropped_count = int(source.dropped_count or 0) + 1
        await db.commit()
        return {
            "status": "throttled_interval",
            "reason": "min_interval_not_elapsed",
            "source": _to_perception_source_out(source),
            "accepted_count": 0,
        }

    top = max(accepted_items, key=lambda item: float(item.confidence))
    normalized = NormalizedInputCreate(
        source="vision",
        raw_input=f"live_camera:{top.object_label}:{top.zone}",
        parsed_intent="vision_observation",
        confidence=float(top.confidence),
        target_system="mim",
        requested_goal="update workspace observation memory",
        safety_flags=["requires_confirmation"],
        metadata_json={
            **(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
            "adapter": "vision_live_camera",
            "device_id": source.device_id,
            "source_type": source.source_type,
            "session_id": source.session_id,
            "is_remote": source.is_remote,
            "detected_labels": [item.object_label for item in accepted_items],
        },
    )

    event_out = await _store_normalized(normalized, db)
    for item in accepted_items:
        observed_at = item.timestamp or now
        db.add(
            WorkspaceObservation(
                observed_at=observed_at,
                zone=str(item.zone or "workspace"),
                label=str(item.object_label),
                confidence=float(item.confidence),
                source="live_camera",
                execution_id=None,
                lifecycle_status="active",
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                observation_count=1,
                metadata_json={
                    "device_id": source.device_id,
                    "source_type": source.source_type,
                    "session_id": source.session_id,
                    "is_remote": source.is_remote,
                    "objective61_live_adapter": True,
                },
            )
        )

    source.last_seen_at = now
    source.last_accepted_at = now
    source.last_event_fingerprint = fingerprint
    source.last_event_payload_json = {
        "type": "camera",
        "object_label": top.object_label,
        "zone": top.zone,
        "confidence": float(top.confidence),
        "timestamp": now.isoformat(),
    }
    source.accepted_count = int(source.accepted_count or 0) + 1
    source.health_status = "healthy"
    source.metadata_json = {
        **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
        "objective61_live_adapter": True,
    }
    await db.commit()

    return {
        "status": "accepted",
        "source": _to_perception_source_out(source),
        "accepted_count": len(accepted_items),
        "event": event_out,
    }


@router.post("/perception/mic/events")
async def live_mic_adapter(payload: LiveMicAdapterRequest, db: AsyncSession = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)
    source = await _get_or_create_perception_source(
        source_type=str(payload.source_type or "microphone").strip() or "microphone",
        device_id=str(payload.device_id).strip(),
        session_id=str(payload.session_id or "").strip(),
        is_remote=bool(payload.is_remote),
        db=db,
    )
    source.min_interval_seconds = int(payload.min_interval_seconds)
    source.duplicate_window_seconds = int(payload.duplicate_window_seconds)
    source.confidence_floor = float(payload.transcript_confidence_floor)

    transcript = str(payload.transcript or "").strip()
    confidence = float(payload.confidence)

    if not transcript:
        # Heartbeat-only update: preserve mic activity visibility without storing a voice input.
        source.last_seen_at = now
        source.health_status = "healthy"
        source.dropped_count = int(source.dropped_count or 0) + 1
        source.last_event_payload_json = {
            "type": "microphone",
            "transcript": "",
            "confidence": confidence,
            "timestamp": now.isoformat(),
            "status": "heartbeat_no_transcript",
        }
        await db.commit()
        return {
            "status": "heartbeat_no_transcript",
            "reason": "no_transcript",
            "source": _to_perception_source_out(source),
            "accepted": False,
        }

    fingerprint = _hash_payload([source.source_type, source.device_id, source.session_id, transcript.lower(), round(confidence, 3)])

    if confidence < float(source.confidence_floor) and bool(payload.discard_low_confidence):
        source.last_seen_at = now
        source.health_status = "degraded"
        source.dropped_count = int(source.dropped_count or 0) + 1
        source.low_confidence_count = int(source.low_confidence_count or 0) + 1
        await db.commit()
        return {
            "status": "discarded_low_confidence",
            "reason": "clarification_required",
            "source": _to_perception_source_out(source),
            "accepted": False,
        }

    if _is_duplicate_event(row=source, fingerprint=fingerprint, now=now):
        source.last_seen_at = now
        source.health_status = "healthy"
        source.dropped_count = int(source.dropped_count or 0) + 1
        source.duplicate_count = int(source.duplicate_count or 0) + 1
        await db.commit()
        return {
            "status": "suppressed_duplicate",
            "reason": "duplicate_transcript",
            "source": _to_perception_source_out(source),
            "accepted": False,
        }

    if _is_interval_blocked(row=source, now=now):
        source.last_seen_at = now
        source.health_status = "healthy"
        source.dropped_count = int(source.dropped_count or 0) + 1
        await db.commit()
        return {
            "status": "throttled_interval",
            "reason": "min_interval_not_elapsed",
            "source": _to_perception_source_out(source),
            "accepted": False,
        }

    normalized = NormalizedInputCreate(
        source="voice",
        raw_input=transcript,
        parsed_intent="unknown",
        confidence=confidence,
        target_system="mim",
        requested_goal="voice_live_input",
        safety_flags=[],
        metadata_json={
            **(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
            "adapter": "voice_live_mic",
            "device_id": source.device_id,
            "source_type": source.source_type,
            "session_id": source.session_id,
            "is_remote": source.is_remote,
            "timestamp": (payload.timestamp or now).isoformat(),
        },
    )
    event_out = await _store_normalized(normalized, db)
    learning_memory_id = await _store_interaction_learning(
        transcript=transcript,
        confidence=confidence,
        source=source,
        payload_metadata=(payload.metadata_json if isinstance(payload.metadata_json, dict) else {}),
        db=db,
    )

    source.last_seen_at = now
    source.last_accepted_at = now
    source.last_event_fingerprint = fingerprint
    source.last_event_payload_json = {
        "type": "microphone",
        "transcript": transcript,
        "confidence": confidence,
        "timestamp": now.isoformat(),
    }
    source.accepted_count = int(source.accepted_count or 0) + 1
    source.health_status = "healthy"
    source.metadata_json = {
        **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
        "objective61_live_adapter": True,
        **({"interaction_learning_memory_id": int(learning_memory_id)} if learning_memory_id else {}),
    }
    await db.commit()

    return {
        "status": "accepted",
        "source": _to_perception_source_out(source),
        "accepted": True,
        "event": event_out,
        "interaction_learning_memory_id": int(learning_memory_id) if learning_memory_id else None,
    }


@router.post("/perception/mic/transcribe")
async def transcribe_mic_audio(payload: dict = Body(...)) -> dict:
    started_at = datetime.now(timezone.utc)
    trace_id = sha256(f"{started_at.isoformat()}|{id(payload)}".encode("utf-8")).hexdigest()[:12]
    raw_audio = str(payload.get("audio_wav_base64") or "").strip()
    language = str(payload.get("language") or "en-US").strip() or "en-US"
    debug_enabled = _mic_debug_enabled(payload)
    provider_mode = _resolve_mic_provider_mode(payload)
    payload_metadata = payload.get("metadata_json") if isinstance(payload.get("metadata_json"), dict) else {}
    purpose = str(payload_metadata.get("purpose") or payload.get("purpose") or "").strip().lower()
    helper_mode_enabled = str(os.getenv("MIM_OPENAI_HELPER_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}
    always_openai_stt = str(os.getenv("MIM_OPENAI_STT_ALWAYS", "")).strip().lower() in {"1", "true", "yes", "on"}
    openai_helper_purposes = {
        "training",
        "learning",
        "evaluation",
        "research",
        "information",
        "object_identification",
        "object-id",
        "subject_context",
        "subject-and-context",
        "context",
    }
    openai_helper_request = bool(
        helper_mode_enabled
        or always_openai_stt
        or
        payload.get("training_mode")
        or payload.get("learning_mode")
        or payload.get("openai_helper")
        or payload_metadata.get("training_mode")
        or payload_metadata.get("learning_mode")
        or payload_metadata.get("openai_helper")
        or purpose in openai_helper_purposes
    )

    def _debug_event(stage: str, **fields: dict) -> dict:
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        event = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace_id": trace_id,
            "stage": stage,
            "elapsed_ms": elapsed_ms,
            "language": language,
            "audio_base64_chars": len(raw_audio),
            "openai_helper_request": openai_helper_request,
            "helper_mode_enabled": helper_mode_enabled,
            "always_openai_stt": always_openai_stt,
            "purpose": purpose,
        }
        event.update(fields)
        return event

    _append_mic_debug_event(_debug_event("received", debug_enabled=debug_enabled, provider_mode=provider_mode))

    if not raw_audio:
        _append_mic_debug_event(_debug_event("reject", reason="missing_audio_base64"))
        raise HTTPException(
            status_code=400,
            detail=_mic_debug_detail("audio_wav_base64 is required", trace_id) if debug_enabled else "audio_wav_base64 is required",
        )

    try:
        import speech_recognition as sr
    except Exception as exc:
        _append_mic_debug_event(
            _debug_event(
                "import_backend_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        )
        raise HTTPException(
            status_code=503,
            detail=_mic_debug_detail("speech_recognition backend unavailable", trace_id) if debug_enabled else "speech_recognition backend unavailable",
        )

    try:
        audio_bytes = base64.b64decode(raw_audio, validate=True)
    except Exception as exc:
        _append_mic_debug_event(
            _debug_event(
                "decode_base64_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        )
        raise HTTPException(
            status_code=400,
            detail=_mic_debug_detail("invalid base64 audio payload", trace_id) if debug_enabled else "invalid base64 audio payload",
        )

    audio_sha_prefix = sha256(audio_bytes).hexdigest()[:16]
    _append_mic_debug_event(
        _debug_event(
            "base64_decoded",
            audio_bytes_len=len(audio_bytes),
            audio_sha256_prefix=audio_sha_prefix,
        )
    )

    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 120
    recognizer.dynamic_energy_threshold = True
    recognizer.operation_timeout = 10

    def _openai_ready(*, allow_general: bool = False) -> tuple[bool, str]:
        openai_general_allowed = allow_general and _openai_auto_stt_enabled()
        if not openai_helper_request and not always_openai_stt and not openai_general_allowed:
            return False, "openai_helper_only"
        api_key = str(settings.openai_api_key or os.getenv("MIM_OPENAI_API_KEY") or "").strip()
        forced_disable = str(os.getenv("MIM_DISABLE_OPENAI", "")).strip().lower() in {"1", "true", "yes", "on"}
        openai_allowed = bool((settings.allow_openai or bool(api_key) or str(os.getenv("MIM_ALLOW_OPENAI", "")).strip().lower() in {"1", "true", "yes", "on"}) and not forced_disable)
        if not openai_allowed:
            return False, "openai_not_allowed"
        if not api_key:
            return False, "openai_api_key_missing"
        return True, "ready"

    async def _recognize_with_openai(trigger: str) -> dict | None:
        ready, reason = _openai_ready(allow_general=(provider_mode == "auto"))
        if not ready:
            _append_mic_debug_event(
                _debug_event(
                    "recognize_openai_skip",
                    provider="openai",
                    trigger=trigger,
                    reason=reason,
                    audio_sha256_prefix=audio_sha_prefix,
                )
            )
            return None

        api_key = str(settings.openai_api_key or os.getenv("MIM_OPENAI_API_KEY") or "").strip()
        model = str(os.getenv("MIM_OPENAI_STT_MODEL") or "gpt-4o-mini-transcribe").strip() or "gpt-4o-mini-transcribe"
        language_short = _lang_to_iso639_1(language)

        def _build_multipart(model_name: str) -> tuple[bytes, str]:
            boundary = f"----mimBoundary{uuid.uuid4().hex}"
            chunks: list[bytes] = []

            def _field(name: str, value: str) -> None:
                chunks.append(f"--{boundary}\r\n".encode("utf-8"))
                chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
                chunks.append(str(value).encode("utf-8"))
                chunks.append(b"\r\n")

            _field("model", model_name)
            _field("language", language_short)
            _field("temperature", "0")

            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            chunks.append(b'Content-Disposition: form-data; name="file"; filename="input.wav"\r\n')
            chunks.append(b"Content-Type: audio/wav\r\n\r\n")
            chunks.append(audio_bytes)
            chunks.append(b"\r\n")
            chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
            return b"".join(chunks), boundary

        def _call_openai(model_name: str) -> dict:
            body, boundary = _build_multipart(model_name)
            req = urllib_request.Request(
                url="https://api.openai.com/v1/audio/transcriptions",
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            with urllib_request.urlopen(req, timeout=22) as response:
                payload_raw = response.read().decode("utf-8", errors="replace")
            return json.loads(payload_raw)

        try_models = [model]
        if model != "whisper-1":
            try_models.append("whisper-1")

        for model_name in try_models:
            try:
                response_json = await asyncio.wait_for(asyncio.to_thread(_call_openai, model_name), timeout=24)
                transcript_text = str(response_json.get("text") or "").strip()
                if transcript_text:
                    _append_mic_debug_event(
                        _debug_event(
                            "recognize_openai_success",
                            provider="openai",
                            trigger=trigger,
                            model=model_name,
                            transcript_chars=len(transcript_text),
                            audio_sha256_prefix=audio_sha_prefix,
                        )
                    )
                    return {
                        "ok": True,
                        "transcript": transcript_text,
                        "confidence": 0.9,
                        "provider": "openai_transcribe",
                        "model": model_name,
                        "trace_id": trace_id,
                    }

                _append_mic_debug_event(
                    _debug_event(
                        "recognize_openai_no_match",
                        provider="openai",
                        trigger=trigger,
                        model=model_name,
                        audio_sha256_prefix=audio_sha_prefix,
                    )
                )
                return {
                    "ok": True,
                    "transcript": "",
                    "confidence": 0.0,
                    "provider": "openai_transcribe",
                    "reason": "no_match",
                    "trace_id": trace_id,
                }
            except urllib_error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                detail = f"http_{exc.code}: {body[:220]}".strip()
                _append_mic_debug_event(
                    _debug_event(
                        "recognize_openai_http_error",
                        provider="openai",
                        trigger=trigger,
                        model=model_name,
                        status_code=exc.code,
                        detail=detail,
                        audio_sha256_prefix=audio_sha_prefix,
                    )
                )
                # Retry with fallback model on 4xx/5xx once.
                continue
            except asyncio.TimeoutError:
                _append_mic_debug_event(
                    _debug_event(
                        "recognize_openai_timeout",
                        provider="openai",
                        trigger=trigger,
                        model=model_name,
                        timeout_seconds=24,
                        audio_sha256_prefix=audio_sha_prefix,
                    )
                )
                continue
            except Exception as exc:
                _append_mic_debug_event(
                    _debug_event(
                        "recognize_openai_error",
                        provider="openai",
                        trigger=trigger,
                        model=model_name,
                        error_type=type(exc).__name__,
                        detail=str(exc),
                        audio_sha256_prefix=audio_sha_prefix,
                    )
                )
                continue

        return None

    async def _recognize_with_local_fallback(trigger: str, upstream_detail: str = "") -> dict | None:
        if not str(language or "").lower().startswith("en"):
            _append_mic_debug_event(
                _debug_event(
                    "recognize_local_skip",
                    provider="pocketsphinx",
                    trigger=trigger,
                    reason="unsupported_language",
                    language=language,
                    audio_sha256_prefix=audio_sha_prefix,
                )
            )
            return None

        try:
            local_transcript = await asyncio.wait_for(
                asyncio.to_thread(recognizer.recognize_sphinx, audio_data),
                timeout=14,
            )
        except asyncio.TimeoutError:
            _append_mic_debug_event(
                _debug_event(
                    "recognize_local_timeout",
                    provider="pocketsphinx",
                    trigger=trigger,
                    timeout_seconds=14,
                    audio_sha256_prefix=audio_sha_prefix,
                )
            )
            return None
        except Exception as local_exc:
            _append_mic_debug_event(
                _debug_event(
                    "recognize_local_error",
                    provider="pocketsphinx",
                    trigger=trigger,
                    error_type=type(local_exc).__name__,
                    detail=str(local_exc),
                    audio_sha256_prefix=audio_sha_prefix,
                )
            )
            return None

        transcript_text = str(local_transcript or "").strip()
        if not transcript_text:
            _append_mic_debug_event(
                _debug_event(
                    "recognize_local_no_match",
                    provider="pocketsphinx",
                    trigger=trigger,
                    audio_sha256_prefix=audio_sha_prefix,
                )
            )
            return {
                "ok": True,
                "transcript": "",
                "confidence": 0.0,
                "provider": "pocketsphinx",
                "reason": "no_match",
                "trace_id": trace_id,
                **({"fallback_from": "google_web_speech"} if trigger.startswith("google") else {}),
            }

        if _is_low_quality_local_transcript(transcript_text):
            _append_mic_debug_event(
                _debug_event(
                    "recognize_local_low_quality",
                    provider="pocketsphinx",
                    trigger=trigger,
                    transcript_chars=len(transcript_text),
                    audio_sha256_prefix=audio_sha_prefix,
                )
            )
            return {
                "ok": True,
                "transcript": "",
                "confidence": 0.0,
                "provider": "pocketsphinx",
                "reason": "low_quality_transcript",
                "trace_id": trace_id,
                **({"fallback_from": "google_web_speech"} if trigger.startswith("google") else {}),
            }

        local_confidence = 0.58
        local_confidence_min = _local_stt_min_confidence()
        if local_confidence < local_confidence_min:
            _append_mic_debug_event(
                _debug_event(
                    "recognize_local_low_confidence",
                    provider="pocketsphinx",
                    trigger=trigger,
                    confidence=local_confidence,
                    min_confidence=local_confidence_min,
                    transcript_chars=len(transcript_text),
                    audio_sha256_prefix=audio_sha_prefix,
                )
            )
            return {
                "ok": True,
                "transcript": "",
                "confidence": local_confidence,
                "provider": "pocketsphinx",
                "reason": "low_confidence",
                "trace_id": trace_id,
                **({"fallback_from": "google_web_speech"} if trigger.startswith("google") else {}),
            }

        _append_mic_debug_event(
            _debug_event(
                "recognize_local_success",
                provider="pocketsphinx",
                trigger=trigger,
                transcript_chars=len(transcript_text),
                audio_sha256_prefix=audio_sha_prefix,
            )
        )
        return {
            "ok": True,
            "transcript": transcript_text,
            "confidence": local_confidence,
            "provider": "pocketsphinx",
            "trace_id": trace_id,
            **({"fallback_from": "google_web_speech"} if trigger.startswith("google") else {}),
            **({"upstream_detail": upstream_detail} if debug_enabled and upstream_detail else {}),
        }

    try:
        def _read_audio_file() -> sr.AudioData:
            with sr.AudioFile(io.BytesIO(audio_bytes)) as source:
                return recognizer.record(source)

        audio_data = await asyncio.wait_for(asyncio.to_thread(_read_audio_file), timeout=6)
    except Exception as exc:
        _append_mic_debug_event(
            _debug_event(
                "wav_parse_failed",
                error_type=type(exc).__name__,
                error=str(exc),
                audio_bytes_len=len(audio_bytes),
                audio_sha256_prefix=audio_sha_prefix,
            )
        )
        raise HTTPException(
            status_code=400,
            detail=_mic_debug_detail("invalid wav audio payload", trace_id) if debug_enabled else "invalid wav audio payload",
        )

    if provider_mode == "local":
        _append_mic_debug_event(
            _debug_event(
                "recognize_provider_selected",
                provider="pocketsphinx",
                mode=provider_mode,
                audio_sha256_prefix=audio_sha_prefix,
            )
        )
        local_result = await _recognize_with_local_fallback(trigger="local_primary")
        if local_result is not None:
            return local_result
        return {
            "ok": False,
            "transcript": "",
            "confidence": 0.0,
            "provider": "pocketsphinx",
            "reason": "provider_unavailable",
            "detail": "local speech provider unavailable",
            "trace_id": trace_id,
        }

    if provider_mode == "openai":
        if not openai_helper_request and not always_openai_stt:
            _append_mic_debug_event(
                _debug_event(
                    "recognize_openai_blocked",
                    provider="openai",
                    mode=provider_mode,
                    reason="openai_helper_only",
                    audio_sha256_prefix=audio_sha_prefix,
                )
            )
            provider_mode = "auto"

    if provider_mode == "openai":
        _append_mic_debug_event(
            _debug_event(
                "recognize_provider_selected",
                provider="openai",
                mode=provider_mode,
                audio_sha256_prefix=audio_sha_prefix,
            )
        )
        openai_result = await _recognize_with_openai(trigger="openai_primary")
        if openai_result is not None:
            return openai_result
        return {
            "ok": False,
            "transcript": "",
            "confidence": 0.0,
            "provider": "openai_transcribe",
            "reason": "provider_unavailable",
            "detail": "openai speech provider unavailable",
            "trace_id": trace_id,
        }

    if provider_mode == "auto":
        openai_result = await _recognize_with_openai(trigger="auto_preferred")
        if openai_result is not None:
            return openai_result

    try:
        transcript = await asyncio.wait_for(
            asyncio.to_thread(recognizer.recognize_google, audio_data, language),
            timeout=12,
        )
        _append_mic_debug_event(
            _debug_event(
                "recognize_success",
                transcript_chars=len(str(transcript or "").strip()),
                provider="google_web_speech",
                mode=provider_mode,
                audio_sha256_prefix=audio_sha_prefix,
            )
        )
        return {
            "ok": True,
            "transcript": str(transcript or "").strip(),
            "confidence": 0.74,
            "provider": "google_web_speech",
            "trace_id": trace_id,
        }
    except asyncio.TimeoutError:
        _append_mic_debug_event(
            _debug_event(
                "recognize_timeout",
                timeout_seconds=12,
                provider="google_web_speech",
                mode=provider_mode,
                audio_sha256_prefix=audio_sha_prefix,
            )
        )
        if provider_mode == "auto":
            local_result = await _recognize_with_local_fallback(trigger="google_timeout", upstream_detail="speech request timeout")
            if local_result is not None:
                return local_result
        raise HTTPException(
            status_code=504,
            detail=_mic_debug_detail("speech request timeout", trace_id) if debug_enabled else "speech request timeout",
        )
    except sr.UnknownValueError:
        _append_mic_debug_event(
            _debug_event(
                "recognize_no_match",
                provider="google_web_speech",
                mode=provider_mode,
                audio_sha256_prefix=audio_sha_prefix,
            )
        )
        return {
            "ok": True,
            "transcript": "",
            "confidence": 0.0,
            "provider": "google_web_speech",
            "reason": "no_match",
            "trace_id": trace_id,
        }
    except sr.RequestError as exc:
        detail = str(exc)
        provider_error = _classify_provider_error(detail)
        debug_event = _debug_event(
            "recognize_provider_error",
            provider="google_web_speech",
            mode=provider_mode,
            error_type=type(exc).__name__,
            detail=detail,
            audio_sha256_prefix=audio_sha_prefix,
            **provider_error,
        )
        _append_mic_debug_event(debug_event)
        logger.warning("mic_transcribe_provider_error trace_id=%s detail=%s", trace_id, detail)

        if provider_mode == "auto":
            local_result = await _recognize_with_local_fallback(trigger="google_provider_error", upstream_detail=detail)
            if local_result is not None:
                return local_result

        debug_payload = {
            "trace_id": trace_id,
            "provider_error": provider_error,
            "audio_bytes_len": len(audio_bytes),
            "audio_sha256_prefix": audio_sha_prefix,
            "language": language,
            "mode": provider_mode,
        }
        return {
            "ok": False,
            "transcript": "",
            "confidence": 0.0,
            "provider": "google_web_speech",
            "reason": "provider_unavailable",
            "detail": detail,
            "trace_id": trace_id,
            **({"debug": debug_payload} if debug_enabled else {}),
        }


@router.get("/perception/sources")
async def list_perception_sources(
    source_type: str = "",
    active_only: bool = False,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(WorkspacePerceptionSource).order_by(WorkspacePerceptionSource.id.desc())
    if source_type.strip():
        stmt = stmt.where(WorkspacePerceptionSource.source_type == source_type.strip())
    rows = (await db.execute(stmt.limit(max(1, min(500, int(limit)))))).scalars().all()

    now = datetime.now(timezone.utc)
    source_items = []
    for row in rows:
        if active_only:
            if not row.last_seen_at:
                continue
            if (now - row.last_seen_at).total_seconds() > PERCEPTION_STALE_SECONDS:
                continue
        source_items.append(_to_perception_source_out(row))

    return {
        "sources": source_items,
    }


@router.get("/perception/status")
async def get_perception_status(db: AsyncSession = Depends(get_db)) -> dict:
    rows = (
        await db.execute(
            select(WorkspacePerceptionSource)
            .order_by(WorkspacePerceptionSource.id.desc())
            .limit(200)
        )
    ).scalars().all()

    now = datetime.now(timezone.utc)
    active = []
    last_camera_event = None
    last_mic_transcript = None

    for row in rows:
        age_seconds = None
        if row.last_seen_at:
            age_seconds = max(0.0, (now - row.last_seen_at).total_seconds())
        if age_seconds is not None and age_seconds <= PERCEPTION_STALE_SECONDS:
            active.append({
                "source_id": int(row.id),
                "source_type": row.source_type,
                "device_id": row.device_id,
                "session_id": row.session_id,
                "is_remote": bool(row.is_remote),
                "last_seen_at": row.last_seen_at,
                "health_status": row.health_status,
            })

        payload = row.last_event_payload_json if isinstance(row.last_event_payload_json, dict) else {}
        if row.source_type == "camera" and payload and last_camera_event is None:
            last_camera_event = payload
            last_camera_event["device_id"] = row.device_id
        if row.source_type == "microphone" and payload and last_mic_transcript is None:
            last_mic_transcript = payload
            last_mic_transcript["device_id"] = row.device_id

    return {
        "active_perception_adapters": active,
        "camera_source_status": {
            "active": any(item.get("source_type") == "camera" for item in active),
            "last_event": last_camera_event,
        },
        "mic_source_status": {
            "active": any(item.get("source_type") == "microphone" for item in active),
            "last_transcript": last_mic_transcript,
        },
        "adapter_health": {
            "healthy_count": sum(1 for row in rows if str(row.health_status or "") == "healthy"),
            "degraded_count": sum(1 for row in rows if str(row.health_status or "") != "healthy"),
        },
        "last_event_timestamp": max(
            [row.last_seen_at for row in rows if row.last_seen_at is not None],
            default=None,
        ),
    }


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
