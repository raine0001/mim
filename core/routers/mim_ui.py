import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.autonomy_boundary_service import build_boundary_action_controls, build_boundary_decision_basis, build_boundary_profile_snapshot
from core.camera_scene import (
    collect_fresh_camera_observations,
    summarize_camera_observations,
)
from core.db import get_db
from core.execution_recovery_service import evaluate_execution_recovery
from core.execution_readiness_service import (
  execution_readiness_summary,
  load_latest_execution_readiness,
)
from core.execution_strategy_service import latest_execution_strategy_plan, to_execution_strategy_plan_out
from core.models import (
    Actor,
    CapabilityExecution,
    ExecutionStrategyPlan,
    InputEvent,
    InputEventResolution,
    MemoryEntry,
    SpeechOutputAction,
    WorkspaceAutonomyBoundaryProfile,
    WorkspaceExecutionTruthGovernanceProfile,
    WorkspaceInquiryQuestion,
    WorkspaceObjectMemory,
    WorkspaceOperatorResolutionCommitment,
    WorkspaceOperatorResolutionCommitmentMonitoringProfile,
    WorkspaceOperatorResolutionCommitmentOutcomeProfile,
    WorkspacePerceptionSource,
    WorkspaceStewardshipCycle,
    WorkspaceStewardshipState,
    WorkspaceStrategyGoal,
)
from core.mim_arm_dispatch_telemetry import refresh_dispatch_telemetry_record
from core.operator_commitment_monitoring_service import (
    latest_commitment_monitoring_profile,
    to_operator_resolution_commitment_monitoring_out,
)
from core.operator_commitment_outcome_service import (
    latest_commitment_outcome_profile,
    to_operator_resolution_commitment_outcome_out,
)
from core.operator_preference_convergence_service import (
  latest_scope_learned_preference,
  list_learned_preferences,
  preference_conflicts,
)
from core.operator_resolution_service import (
  choose_operator_resolution_commitment,
  commitment_effect_labels,
  commitment_is_recovery_policy_tuning_derived,
  commitment_scope_application,
  commitment_snapshot,
)
from core.policy_conflict_resolution_service import list_workspace_policy_conflict_profiles
from core.proposal_policy_convergence_service import list_workspace_proposal_policy_preferences
from core.self_evolution_service import build_self_evolution_briefing
from core.runtime_recovery_service import RuntimeRecoveryService
from core.ui_health_service import (
  build_mim_ui_health_snapshot,
  build_mim_ui_health_snapshot_from_rows,
  summarize_runtime_health,
)

router = APIRouter(tags=["mim-ui"])
SHARED_RUNTIME_ROOT = Path("runtime/shared")
runtime_recovery_service = RuntimeRecoveryService(SHARED_RUNTIME_ROOT)

MIC_PROMPT_MIN_CONFIDENCE = 0.66
MIC_PROMPT_MAX_AGE_SECONDS = 25.0


class RuntimeRecoveryEventRequest(BaseModel):
    lane: str
    event_type: str
    detail: str | None = None
    next_retry_at: str | None = None
    metadata: dict | None = None


def _known_people() -> set[str]:
    return {
        "testpilot",
        "operator",
        "alice",
        "bob",
        "charlie",
    }


def _age_seconds(now: datetime, ts: datetime | None) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds())


def _parse_payload_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compact_sentence(raw: str, *, max_len: int = 180) -> str:
    text = " ".join(str(raw or "").split())
    if len(text) <= max_len:
        return text
    return f"{text[: max_len - 3].rstrip()}..."


def _tokenize(text: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())
    return {token for token in cleaned.split() if token}


def _strip_conversation_noise(text: str) -> str:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return ""

    for suffix in (
        "please do not repeat yourself",
        "and i am giving some extra context because i am thinking out loud",
        "i am not totally sure",
    ):
        if suffix in normalized:
            normalized = normalized.split(suffix, 1)[0].strip(" .,!?;")

    changed = True
    while changed and normalized:
        changed = False
        for prefix in (
            "okay so ",
            "okay ",
            "just ",
            "honestly ",
            "quickly ",
            "can you tell me ",
            "do you know ",
            "maybe ",
            "uh ",
            "um ",
            "you know ",
            "hmm ",
        ):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :].strip(" .,!?;")
                changed = True
                break

    return normalized.strip(" .,!?;")


def _looks_like_low_signal_turn(text: str) -> bool:
    normalized = _strip_conversation_noise(text)
    if not normalized:
        return True
    low_signal = {
        "uh",
        "um",
        "hmm",
        "you know",
        "maybe",
        "maybe maybe",
        "wait",
        "no stop",
    }
    return normalized in low_signal


def _looks_like_status_request(text: str) -> bool:
    normalized = _strip_conversation_noise(text)
    if not normalized:
        return False
    return normalized in {
        "status",
        "status now",
        "one line status",
        "health",
        "health now",
        "current health",
    }


def _looks_like_direct_question(text: str) -> bool:
    prompt = _strip_conversation_noise(text)
    if not prompt:
        return False
    if "?" in prompt:
        return True
    question_starts = (
        "what ",
        "why ",
        "how ",
        "when ",
        "where ",
        "who ",
        "which ",
        "does ",
        "do ",
        "can ",
        "could ",
        "is ",
        "are ",
        "will ",
        "would ",
    )
    return prompt.startswith(question_starts)


def _looks_like_greeting(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False
    greeting_tokens = {
        "hello",
        "hi",
        "hey",
        "yo",
        "good morning",
        "good afternoon",
        "good evening",
    }
    return normalized in greeting_tokens


def _is_clarifier_prompt_text(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False
    return (
        "missing one detail" in normalized
        or "options: 1)" in normalized
        or "i am still missing" in normalized
    )


def _plain_answer_from_context(
    *,
    latest_mic_transcript: str,
    environment_now: str,
    goal_summary: str,
    memory_summary: str,
) -> str:
    question = str(latest_mic_transcript or "").strip()
    ql = _strip_conversation_noise(question)
    question_stub = _compact_sentence(question, max_len=72)
    if _looks_like_greeting(question):
        return "Hi. I can hear you and I am ready to help."

    if "annoying" in ql and "repeating" in ql:
        return "Understood. I will keep this concise and avoid repeating myself."

    if (
        "what exactly do you need" in ql
        or "what do you need" in ql
        or "what do u need" in ql
    ):
        return (
            "I need one concrete request from you: ask one question or name one action."
        )

    if "what can you do" in ql or "what can you help with" in ql:
        return "I can answer a question, suggest a plan, or take an action."

    if "just chatting for now" in ql or "keep this simple and conversational" in ql:
        return "Understood. I will keep this simple and conversational."

    if "upcoming tasks" in ql:
        if goal_summary:
            return _compact_sentence(
                f"Upcoming task focus: {goal_summary.rstrip('.')}", max_len=180
            )
        return "Upcoming task focus: I am ready for the next concrete task you want to take on."

    if "recap" in ql:
        if goal_summary:
            return _compact_sentence(
                f"Short recap: {goal_summary.rstrip('.')}", max_len=180
            )
        if memory_summary:
            return _compact_sentence(
                f"Short recap: {memory_summary.rstrip('.')}", max_len=180
            )
        return "Short recap: I am online, listening, and ready for the next concrete request."

    if "why that" in ql or "why that one" in ql:
        if goal_summary:
            because_clause = goal_summary.rstrip(".")
            if because_clause:
                because_clause = because_clause[0].lower() + because_clause[1:]
            return _compact_sentence(f"Because {because_clause}.", max_len=180)
        return "Because that is the clearest next priority in the current state."

    if any(
        phrase in ql
        for phrase in {
            "what should i do first",
            "what should we do first",
            "what should i do next",
            "what should we do next",
            "what should we prioritize next",
        }
    ):
        if goal_summary:
            return _compact_sentence(
                f"First priority: {goal_summary.rstrip('.')}", max_len=180
            )
        return "First priority: give me one concrete request so I can help directly."

    if "objective" in ql and any(
        token in ql for token in {"current", "active", "working", "on"}
    ):
        if goal_summary:
            return _compact_sentence(
                f"Current active objective: {goal_summary.rstrip('.')}", max_len=180
            )
        return "I do not have an active objective in this state yet."

    if _looks_like_status_request(question):
        return "Current health status: healthy, online, and listening right now."

    if ("health" in ql or "status" in ql) and any(
        token in ql for token in {"what", "how", "are you", "your"}
    ):
        return "Current health status: healthy, online, and listening right now."

    if "tod" in ql and "how" in ql:
        if goal_summary:
            return _compact_sentence(
                f"TOD status now: {goal_summary.rstrip('.')}", max_len=180
            )
        return (
            "TOD status now: healthy, online, and waiting on the next concrete request."
        )

    if "task 75" in ql and ("what" in ql or "does" in ql):
        return "Task 75 checks whether MIM and TOD stay synchronized without drift."

    if goal_summary:
        return _compact_sentence(f"{goal_summary.rstrip('.')}.", max_len=180)
    if memory_summary:
        return _compact_sentence(f"{memory_summary.rstrip('.')}.", max_len=180)
    if environment_now:
        if environment_now.startswith("camera has no clear"):
            return f"For '{question_stub}', I do not have enough current state to answer directly yet."
        return _compact_sentence(
            f"For '{question_stub}', current state is {environment_now.rstrip('.')}.",
            max_len=180,
        )
    return f"For '{question_stub}', I do not have enough current state to answer directly yet."


def _apply_anti_drift_rewrite(
    *,
    text: str,
    latest_mic_transcript: str,
    environment_now: str,
    goal_summary: str,
    memory_summary: str,
) -> str:
    candidate = str(text or "").strip()
    if not candidate:
        return ""

    lowered = candidate.lower()
    drift_openers = (
        "what you're really asking",
        "what you are really asking",
        "at a high level",
        "in broad terms",
        "more generally",
    )
    if lowered.startswith(drift_openers):
        first_sentence = candidate.split(".", 1)[0].strip()
        candidate = first_sentence if first_sentence else candidate

    if _looks_like_direct_question(latest_mic_transcript):
        user_tokens = _tokenize(latest_mic_transcript)
        reply_tokens = _tokenize(candidate)
        overlap = len(user_tokens.intersection(reply_tokens))
        if overlap < 2:
            direct = _plain_answer_from_context(
                latest_mic_transcript=latest_mic_transcript,
                environment_now=environment_now,
                goal_summary=goal_summary,
                memory_summary=memory_summary,
            )
            return direct
    return _compact_sentence(candidate, max_len=220)


def _is_low_quality_learning_entry(entry: MemoryEntry) -> bool:
    meta = entry.metadata_json if isinstance(entry.metadata_json, dict) else {}
    signal = str(meta.get("preference_signal", "")).strip().lower()
    value = str(meta.get("preference_value", "")).strip().lower()
    try:
        confidence = float(meta.get("confidence", 0.0) or 0.0)
    except Exception:
        confidence = 0.0

    if confidence and confidence < 0.68:
        return True

    if signal == "call_me":
        low_value_tokens = {
            "what",
            "that",
            "there",
            "here",
            "hello",
            "hi",
            "hey",
            "him",
            "you",
            "see",
        }
        if not value or len(value) < 3 or value in low_value_tokens:
            return True

    return False


def _looks_like_identity_prompt(raw: str) -> bool:
    text = str(raw or "").strip().lower()
    if not text:
        return False
    return (
        "what should i call you" in text
        or "what's your name" in text
        or "tell me your name" in text
    )


def _rewrite_state_output_text(
    raw_text: str,
    *,
    needs_identity_prompt: bool,
    open_question_summary: str,
    goal_summary: str,
    latest_mic_transcript: str,
    environment_now: str,
    memory_summary: str,
) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""

    normalized = " ".join(text.lower().split())
    if normalized in {"hello, i am mim.", "hello i am mim.", "hello i am mim"}:
        return ""

    if needs_identity_prompt:
        return text

    if _looks_like_identity_prompt(text):
        if open_question_summary:
            return f"Before I proceed, I need one decision: {open_question_summary}"
        if goal_summary:
            return f"I am tracking this goal: {goal_summary}. Tell me what you want me to do next."
        return ""

    return _apply_anti_drift_rewrite(
        text=text,
        latest_mic_transcript=latest_mic_transcript,
        environment_now=environment_now,
        goal_summary=goal_summary,
        memory_summary=memory_summary,
    )


def _choose_phrase(options: list[str], key: str) -> str:
    phrases = [item.strip() for item in options if str(item or "").strip()]
    if not phrases:
        return ""
    digest = sha256(str(key or "seed").encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(phrases)
    return phrases[idx]


def _build_curiosity_prompt(
    *,
    environment_now: str,
    goal_summary: str,
    memory_summary: str,
    latest_mic_transcript: str,
    learning_summary: str,
    clarification_budget_exhausted: bool = False,
) -> str:
    env = _compact_sentence(environment_now, max_len=96)
    goal = _compact_sentence(goal_summary, max_len=110)
    memory = _compact_sentence(memory_summary, max_len=110)
    mic = _compact_sentence(latest_mic_transcript, max_len=90)
    learning = _compact_sentence(learning_summary, max_len=110)

    if mic:
        if _looks_like_greeting(mic):
            return "Hi. I can hear you and I am ready to help."
        if _looks_like_direct_question(mic) or _looks_like_status_request(mic):
            return _plain_answer_from_context(
                latest_mic_transcript=mic,
                environment_now=env,
                goal_summary=goal,
                memory_summary=memory,
            )
        if clarification_budget_exhausted:
            if _looks_like_low_signal_turn(mic):
                return "I am waiting for one concrete request: answer, plan, or action."
            return f"For '{mic}', I still need one detail. Options: 1) answer, 2) plan, 3) action."
        return f"For '{mic}', I'm missing one detail: do you want me to answer a question, suggest a plan, or take an action?"

    if learning:
        return _choose_phrase(
            [
                f"Current preference signal: {learning}.",
                f"Stored interaction pattern: {learning}.",
                f"Recent preference memory: {learning}.",
            ],
            key=f"learn:{learning}|env:{env}",
        )

    if goal and env:
        return _choose_phrase(
            [
                f"Current scene: {env}. Active goal: {goal}.",
                f"I can see {env}. I am tracking {goal}.",
                f"Context: {env}. Goal in play: {goal}.",
            ],
            key=f"goal-env:{goal}|{env}",
        )

    if goal:
        return _choose_phrase(
            [
                f"I am tracking this goal: {goal}.",
                f"Goal status: {goal}.",
            ],
            key=f"goal:{goal}",
        )

    if memory:
        return _choose_phrase(
            [
                f"From memory: {memory}.",
                f"I remember: {memory}.",
            ],
            key=f"memory:{memory}",
        )

    return _choose_phrase(
        [
            "I am ready. Choose one: answer a question, suggest a plan, or take an action.",
            "I am ready. Options: answer, plan, or action.",
            "I am available. Pick one path: answer, plan, or action.",
        ],
        key="fallback-curiosity",
    )


def _semantic_metadata_fields(metadata: dict[str, object]) -> list[str]:
    fields = [
        "description",
        "purpose",
        "owner",
        "category",
        "meaning",
        "user_notes",
    ]
    return [field for field in fields if str(metadata.get(field) or "").strip()]


def _extract_conversation_session_id(metadata: object) -> str:
    if not isinstance(metadata, dict):
        return ""
    return str(
        metadata.get("conversation_session_id") or metadata.get("session_id") or ""
    ).strip()


def _resolve_active_perception_session(
    *,
    camera_rows: list[WorkspacePerceptionSource],
    mic_row: WorkspacePerceptionSource | None,
    now: datetime,
    fallback_session_id: str = "",
) -> str:
    freshest_session = ""
    freshest_seen_at: datetime | None = None

    for row in camera_rows:
        session_id = str(row.session_id or "").strip()
        seen_at = (
            row.last_seen_at.astimezone(timezone.utc) if row.last_seen_at else None
        )
        if not session_id or seen_at is None:
            continue
        age_seconds = max((now - seen_at).total_seconds(), 0.0)
        if age_seconds > 90.0:
            continue
        if freshest_seen_at is None or seen_at > freshest_seen_at:
            freshest_session = session_id
            freshest_seen_at = seen_at

    if freshest_session:
        return freshest_session

    if mic_row:
        mic_session = str(mic_row.session_id or "").strip()
        mic_seen_at = (
            mic_row.last_seen_at.astimezone(timezone.utc)
            if mic_row.last_seen_at
            else None
        )
        if mic_session and mic_seen_at is not None:
            age_seconds = max((now - mic_seen_at).total_seconds(), 0.0)
            if age_seconds <= 90.0:
                return mic_session

    return str(fallback_session_id or "").strip()


def _object_memory_matches_active_session(
  row: WorkspaceObjectMemory,
  active_session_id: str,
) -> bool:
  if not active_session_id:
    return True

  metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
  last_source = str(metadata.get("last_observation_source") or "").strip().lower()
  source_metadata = (
    metadata.get("last_observation_source_metadata")
    if isinstance(metadata.get("last_observation_source_metadata"), dict)
    else {}
  )
  source_session_id = str(
    source_metadata.get("session_id") or metadata.get("last_session_id") or ""
  ).strip()

  if last_source == "live_camera":
    return source_session_id == active_session_id
  if source_session_id:
    return source_session_id == active_session_id
  return True


def _object_memory_matches_label(row: WorkspaceObjectMemory, label: str) -> bool:
    target = str(label or "").strip().lower()
    if not target:
        return False
    aliases = {str(row.canonical_name or "").strip().lower()}
    if isinstance(row.candidate_labels, list):
        aliases.update(
            str(item).strip().lower()
            for item in row.candidate_labels
            if str(item).strip()
        )
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    aliases.add(str(metadata.get("last_matched_label") or "").strip().lower())
    return target in aliases


def _build_semantic_note(metadata: dict[str, object]) -> str:
    parts: list[str] = []
    owner = str(metadata.get("owner") or "").strip()
    purpose = str(metadata.get("purpose") or "").strip()
    meaning = str(metadata.get("meaning") or "").strip()
    expected_home_zone = str(
        metadata.get("expected_home_zone")
        or metadata.get("expected_zone")
        or metadata.get("home_zone")
        or ""
    ).strip()
    if owner:
        parts.append(f"Owner: {owner}")
    if purpose:
        parts.append(f"Purpose: {purpose}")
    if meaning:
        parts.append(f"Meaning: {meaning}")
    if expected_home_zone:
        parts.append(f"Expected home zone: {expected_home_zone}")
    return ". ".join(parts)


def _camera_detail_for_label(
    *,
    label: str,
    state: str,
    metadata: dict[str, object] | None = None,
    row: WorkspaceObjectMemory | None = None,
    inquiry_questions: list[str] | None = None,
) -> dict[str, object]:
    meta = metadata if isinstance(metadata, dict) else {}
    semantic_fields = _semantic_metadata_fields(meta)
    expected_home_zone = str(
        meta.get("expected_home_zone")
        or meta.get("expected_zone")
        or meta.get("home_zone")
        or getattr(row, "zone", "")
        or ""
    ).strip()
    detail = {
        "state": state,
        "semantic_fields": semantic_fields,
        "semantic_memory": {
            field: str(meta.get(field) or "").strip() for field in semantic_fields
        },
        "semantic_note": _build_semantic_note(meta),
        "expected_home_zone": expected_home_zone,
    }
    if inquiry_questions:
        detail["inquiry_questions"] = inquiry_questions
    return detail


def _operator_goal_snapshot(row: WorkspaceStrategyGoal | None) -> dict:
    if row is None:
        return {}
    ranking = (
        row.ranking_factors_json if isinstance(row.ranking_factors_json, dict) else {}
    )
    reasoning = _compact_sentence(
        row.reasoning_summary or row.evidence_summary or row.success_criteria,
        max_len=180,
    )
    return {
        "goal_id": int(row.id),
        "strategy_type": str(row.strategy_type or "").strip(),
        "priority": str(row.priority or "").strip(),
        "priority_score": round(float(row.priority_score or 0.0), 6),
        "status": str(row.status or "").strip(),
        "reasoning_summary": reasoning,
        "execution_truth_governance_decision": str(
            ranking.get("execution_truth_governance_decision") or ""
        ).strip(),
        "contributing_domains": (
            row.contributing_domains_json
            if isinstance(row.contributing_domains_json, list)
            else []
        )[:5],
    }


def _operator_inquiry_snapshot(row: WorkspaceInquiryQuestion | None) -> dict:
    if row is None:
        return {}
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    policy = metadata.get("inquiry_policy", {})
    if not isinstance(policy, dict):
        policy = {}
    applied_effect = (
        row.applied_effect_json if isinstance(row.applied_effect_json, dict) else {}
    )
    trigger_evidence = (
        row.trigger_evidence_json if isinstance(row.trigger_evidence_json, dict) else {}
    )
    return {
        "question_id": int(row.id),
        "status": str(row.status or "").strip(),
        "trigger_type": str(row.trigger_type or "").strip(),
        "managed_scope": str(trigger_evidence.get("managed_scope") or "").strip(),
        "decision_state": str(
            policy.get("decision_state") or applied_effect.get("decision_state") or ""
        ).strip(),
        "decision_reason": str(
            policy.get("decision_reason") or policy.get("reason") or ""
        ).strip(),
        "suppression_reason": str(policy.get("suppression_reason") or "").strip(),
        "waiting_decision": _compact_sentence(
            row.waiting_decision or row.why_answer_matters or row.safe_default_if_unanswered,
            max_len=180,
        ),
        "recent_answer_reused": bool(policy.get("recent_answer_reused", False)),
        "duplicate_suppressed": bool(policy.get("duplicate_suppressed", False)),
        "cooldown_remaining_seconds": int(
            policy.get("cooldown_remaining_seconds", 0) or 0
        ),
        "state_delta_summary": (
            applied_effect.get("state_delta_summary", [])
            if isinstance(applied_effect.get("state_delta_summary", []), list)
            else []
        ),
    }


def _operator_governance_snapshot(
    row: WorkspaceExecutionTruthGovernanceProfile | None,
) -> dict:
    if row is None:
        return {}
    return {
        "governance_id": int(row.id),
        "managed_scope": str(row.managed_scope or "").strip(),
        "governance_state": str(row.governance_state or "").strip(),
        "governance_decision": str(row.governance_decision or "").strip(),
        "governance_reason": _compact_sentence(row.governance_reason, max_len=180),
        "signal_count": int(row.signal_count or 0),
        "execution_count": int(row.execution_count or 0),
        "confidence": round(float(row.confidence or 0.0), 6),
    }


def _operator_autonomy_snapshot(
    row: WorkspaceAutonomyBoundaryProfile | None,
) -> dict:
  if row is None:
    return {}
  boundary_profile = build_boundary_profile_snapshot(row)
  action_controls = build_boundary_action_controls(boundary_profile)
  reasoning = (
    row.adaptation_reasoning_json
    if isinstance(row.adaptation_reasoning_json, dict)
    else {}
  )
  governance = (
    reasoning.get("execution_truth_governance", {})
    if isinstance(reasoning.get("execution_truth_governance", {}), dict)
    else {}
  )
  proposal_arbitration_review = (
    reasoning.get("proposal_arbitration_autonomy_review", {})
    if isinstance(reasoning.get("proposal_arbitration_autonomy_review", {}), dict)
    else {}
  )
  policy_conflict = (
    reasoning.get("policy_conflict_resolution", {})
    if isinstance(reasoning.get("policy_conflict_resolution", {}), dict)
    else {}
  )
  decision_basis = build_boundary_decision_basis(
    boundary_profile=boundary_profile,
    requested_action="automatic_execution",
    policy_source=str(policy_conflict.get("winning_policy_source") or "").strip() or "autonomy_boundary",
    auto_execution_allowed=boundary_profile.get("current_level") not in {"manual_only", "operator_required"},
    reason=str(row.adjustment_reason or row.adaptation_summary or "").strip(),
    policy_conflict=policy_conflict,
  )
  return {
    "boundary_id": int(row.id),
    "scope": str(row.scope or "").strip(),
    "current_level": str(boundary_profile.get("current_level") or "").strip(),
    "profile_status": str(row.profile_status or "").strip(),
    "adaptation_summary": _compact_sentence(row.adaptation_summary, max_len=180),
    "decision": str(reasoning.get("decision") or "").strip(),
    "governance_decision": str(
      governance.get("governance_decision") or ""
    ).strip(),
    "confidence": round(float(row.confidence or 0.0), 6),
    "decision_basis": decision_basis,
    "why_not_automatic": str(decision_basis.get("why_not_automatic") or "").strip(),
    "allowed_actions": action_controls.get("allowed_actions", []),
    "approval_required": bool(action_controls.get("approval_required", True)),
    "retry_policy": action_controls.get("retry_policy", {}),
    "risk_level": str(action_controls.get("risk_level") or "").strip(),
    "proposal_arbitration_review": {
      "applied": bool(proposal_arbitration_review.get("applied", False)),
      "review_weight": round(
        float(proposal_arbitration_review.get("review_weight", 0.0) or 0.0),
        6,
      ),
      "target_level_cap": str(
        proposal_arbitration_review.get("target_level_cap") or ""
      ).strip(),
      "related_zone": str(
        proposal_arbitration_review.get("related_zone") or ""
      ).strip(),
      "proposal_types": (
        proposal_arbitration_review.get("proposal_types", [])
        if isinstance(proposal_arbitration_review.get("proposal_types", []), list)
        else []
      ),
    },
  }


def _operator_stewardship_snapshot(
    state_row: WorkspaceStewardshipState | None,
    cycle_row: WorkspaceStewardshipCycle | None,
) -> dict:
    if state_row is None:
        return {}
    cycle_metadata = (
        cycle_row.metadata_json
        if cycle_row and isinstance(cycle_row.metadata_json, dict)
        else {}
    )
    verification = (
        cycle_metadata.get("verification", {})
        if isinstance(cycle_metadata.get("verification", {}), dict)
        else {}
    )
    inquiry_candidates = (
        verification.get("inquiry_candidates", [])
        if isinstance(verification.get("inquiry_candidates", []), list)
        else []
    )
    governance = (
        verification.get("execution_truth_governance", {})
        if isinstance(verification.get("execution_truth_governance", {}), dict)
        else {}
    )
    persistent_degradation = bool(verification.get("persistent_degradation", False))
    if inquiry_candidates:
        followup_status = "generated"
    elif persistent_degradation:
        followup_status = "suppressed"
    else:
        followup_status = "not_needed"
    return {
        "stewardship_id": int(state_row.id),
        "managed_scope": str(state_row.managed_scope or "").strip(),
        "status": str(state_row.status or "").strip(),
        "current_health": round(float(state_row.current_health or 0.0), 6),
        "cycle_count": int(state_row.cycle_count or 0),
        "last_decision_summary": _compact_sentence(
            state_row.last_decision_summary,
            max_len=180,
        ),
        "persistent_degradation": persistent_degradation,
        "followup_status": followup_status,
        "inquiry_candidate_count": len(inquiry_candidates),
        "governance_decision": str(
            governance.get("governance_decision") or ""
        ).strip(),
    }


def _build_operator_reasoning_summary(
    *,
    goal: dict,
    inquiry: dict,
    governance: dict,
    gateway_governance: dict,
    autonomy: dict,
    stewardship: dict,
    execution_readiness: dict,
    execution_recovery: dict,
    commitment: dict,
    commitment_monitoring: dict,
    commitment_outcome: dict,
    learned_preferences: list[dict],
    proposal_policy: dict,
    conflict_resolution: dict,
    collaboration_progress: dict | None = None,
    dispatch_telemetry: dict | None = None,
    tod_decision_process: dict | None = None,
    self_evolution: dict | None = None,
    runtime_health: dict | None = None,
    runtime_recovery: dict | None = None,
) -> str:
    collaboration_progress = (
      collaboration_progress if isinstance(collaboration_progress, dict) else {}
    )
    dispatch_telemetry = (
      dispatch_telemetry if isinstance(dispatch_telemetry, dict) else {}
    )
    tod_decision_process = tod_decision_process if isinstance(tod_decision_process, dict) else {}
    self_evolution = self_evolution if isinstance(self_evolution, dict) else {}
    runtime_health = runtime_health if isinstance(runtime_health, dict) else {}
    runtime_recovery = runtime_recovery if isinstance(runtime_recovery, dict) else {}
    parts: list[str] = []
    decision_summary = str(tod_decision_process.get("summary") or "").strip()
    if decision_summary:
      parts.append(f"TOD decision: {decision_summary}")
    if str(goal.get("reasoning_summary") or "").strip():
        parts.append(f"Goal: {str(goal.get('reasoning_summary') or '').strip()}")
    if str(inquiry.get("decision_state") or "").strip():
        trigger = str(inquiry.get("trigger_type") or "").strip().replace("_", " ")
        decision = str(inquiry.get("decision_state") or "").strip().replace("_", " ")
        if trigger:
            parts.append(f"Inquiry: {decision} for {trigger}")
        else:
            parts.append(f"Inquiry: {decision}")
    if str(governance.get("governance_decision") or "").strip():
        parts.append(
            "Governance: "
            f"{str(governance.get('governance_decision') or '').strip().replace('_', ' ')}"
        )
    if str(gateway_governance.get("summary") or "").strip():
        parts.append(
            "Gateway governance: "
            f"{str(gateway_governance.get('summary') or '').strip()}"
        )
    collaboration_summary = str(collaboration_progress.get("summary") or "").strip()
    if collaboration_summary:
      parts.append(f"TOD collaboration: {collaboration_summary}")
    dispatch_summary = str(dispatch_telemetry.get("summary") or "").strip()
    if dispatch_summary:
      parts.append(f"Dispatch telemetry: {dispatch_summary}")
    self_evolution_summary = str(self_evolution.get("summary") or "").strip()
    if self_evolution_summary:
      parts.append(f"Self-evolution: {self_evolution_summary}")
    runtime_summary = summarize_runtime_health(runtime_health)
    runtime_status = str(runtime_health.get("status") or "").strip()
    if runtime_summary and runtime_status and runtime_status != "healthy":
        parts.append(f"Runtime health: {runtime_summary}")
    recovery_summary = str(runtime_recovery.get("summary") or "").strip()
    recovery_status = str(runtime_recovery.get("status") or "").strip()
    if recovery_summary and recovery_status and recovery_status != "healthy":
      parts.append(f"Runtime recovery: {recovery_summary}")
    if str(autonomy.get("current_level") or "").strip():
      autonomy_summary = (
        "Autonomy: "
        f"{str(autonomy.get('current_level') or '').strip().replace('_', ' ')}"
      )
      why_not_automatic = str(autonomy.get("why_not_automatic") or "").strip()
      if why_not_automatic:
        autonomy_summary = f"{autonomy_summary}. {why_not_automatic}"
      parts.append(autonomy_summary)
    if str(stewardship.get("followup_status") or "").strip():
        status = str(stewardship.get("followup_status") or "").strip().replace("_", " ")
        scope = str(stewardship.get("managed_scope") or "").strip()
        if scope:
            parts.append(f"Stewardship: {status} on {scope}")
        else:
            parts.append(f"Stewardship: {status}")
    if str(execution_readiness.get("summary") or "").strip():
      parts.append(
        "Readiness: "
        f"{str(execution_readiness.get('summary') or '').strip()}"
      )
    if str(execution_recovery.get("summary") or "").strip():
      parts.append(
        "Recovery: "
        f"{str(execution_recovery.get('summary') or '').strip()}"
      )
    if bool(commitment.get("active", False)):
        decision = str(commitment.get("decision_type") or "").strip().replace("_", " ")
        scope = str(commitment.get("managed_scope") or "").strip()
        if decision and scope:
            parts.append(f"Operator: {decision} on {scope}")
        elif decision:
            parts.append(f"Operator: {decision}")
    if str(commitment_monitoring.get("governance_state") or "").strip():
        state = str(commitment_monitoring.get("governance_state") or "").strip().replace("_", " ")
        parts.append(f"Commitment monitoring: {state}")
    if str(commitment_outcome.get("outcome_status") or "").strip():
      status = str(commitment_outcome.get("outcome_status") or "").strip().replace("_", " ")
      parts.append(f"Commitment outcome: {status}")
    if learned_preferences:
      first = learned_preferences[0] if isinstance(learned_preferences[0], dict) else {}
      direction = str(first.get("preference_direction") or "").strip().replace("_", " ")
      scope = str(first.get("managed_scope") or "").strip()
      if direction and scope:
        parts.append(f"Preference: {direction} on {scope}")
      elif direction:
        parts.append(f"Preference: {direction}")
    if int(proposal_policy.get("active_policy_count", 0) or 0) > 0:
      summary = str(proposal_policy.get("summary") or "").strip()
      if summary:
        parts.append(f"Proposal policy: {summary}")
    if int(conflict_resolution.get("active_conflict_count", 0) or 0) > 0:
      summary = str(conflict_resolution.get("summary") or "").strip()
      if summary:
        parts.append(f"Conflict resolution: {summary}")
    return _compact_sentence(". ".join(part for part in parts if part), max_len=260)


def _operator_proposal_policy_snapshot(preferences: list[dict]) -> dict:
    items = [item for item in preferences if isinstance(item, dict)]
    active = [
        item
        for item in items
        if str(item.get("policy_state") or "").strip() in {"preferred", "suppressed", "downgraded"}
    ]
    first = active[0] if active else (items[0] if items else {})
    return {
        "active_policy_count": len(active),
        "managed_scope": str(first.get("managed_scope") or "").strip(),
        "policy_state": str(first.get("policy_state") or "").strip(),
        "proposal_type": str(first.get("proposal_type") or "").strip(),
        "convergence_confidence": round(float(first.get("convergence_confidence", 0.0) or 0.0), 6),
        "summary": _compact_sentence(str(first.get("rationale") or ""), max_len=180),
        "items": items[:5],
    }


def _operator_policy_conflict_snapshot(conflicts: list[dict]) -> dict:
    items = [item for item in conflicts if isinstance(item, dict)]
    active = [
        item
        for item in items
        if str(item.get("conflict_state") or "").strip() in {"active_conflict", "cooldown_held"}
    ]
    first = active[0] if active else (items[0] if items else {})
    winner = str(first.get("winning_policy_source") or "").strip().replace("_", " ")
    loser_sources = first.get("losing_policy_sources", [])
    loser = ""
    if isinstance(loser_sources, list) and loser_sources:
        loser = str(loser_sources[0] or "").strip().replace("_", " ")
    summary = ""
    if winner and loser:
        summary = _compact_sentence(f"{winner} prevailed over {loser} in this scope.", max_len=180)
    elif winner:
        summary = _compact_sentence(f"{winner} is currently shaping the scoped policy posture.", max_len=180)
    return {
        "active_conflict_count": len(active),
        "managed_scope": str(first.get("managed_scope") or "").strip(),
        "winning_policy_source": str(first.get("winning_policy_source") or "").strip(),
        "precedence_rule": str(first.get("precedence_rule") or "").strip(),
        "conflict_state": str(first.get("conflict_state") or "").strip(),
        "summary": summary,
        "items": items[:5],
    }


def _operator_execution_readiness_snapshot(readiness: dict) -> dict:
    if not isinstance(readiness, dict):
        return {}
    summary = execution_readiness_summary(readiness)
    return {
        **summary,
        "signal_name": str(readiness.get("signal_name") or "").strip(),
        "freshness_state": str(readiness.get("freshness_state") or "").strip(),
        "valid": bool(readiness.get("valid", False)),
        "authoritative": bool(readiness.get("authoritative", False)),
    }


def _summarize_operator_http_action(action: dict) -> dict:
    packet = action if isinstance(action, dict) else {}
    method = str(packet.get("method") or "").strip().upper()
    path = str(packet.get("path") or "").strip()
    payload = packet.get("payload", {}) if isinstance(packet.get("payload", {}), dict) else {}
    payload_keys = [
        str(key).strip()
        for key in payload.keys()
        if str(key).strip()
    ]
    payload_keys.sort()
    summary = ""
    if method and path:
        summary = f"{method} {path}"
    elif path:
        summary = path
    elif method:
        summary = method
    if payload_keys:
        preview = ", ".join(payload_keys[:3])
        if len(payload_keys) > 3:
            preview = f"{preview}, +{len(payload_keys) - 3} more"
        summary = f"{summary} with {preview}" if summary else f"payload: {preview}"
    return {
        "method": method,
        "path": path,
        "payload": payload,
        "payload_keys": payload_keys,
        "summary": _compact_sentence(summary, max_len=180),
    }


def _build_self_evolution_operator_commands(*, decision: dict, target: dict, action: dict) -> list[dict]:
    normalized_action = action if isinstance(action, dict) else {}
    method = str(normalized_action.get("method") or "").strip().upper()
    path = str(normalized_action.get("path") or "").strip()
    if not method or not path:
        return []
    decision_type = str(decision.get("decision_type") or "").strip().replace("_", " ")
    target_kind = str(target.get("target_kind") or "").strip().replace("_", " ")
    purpose = "Inspect the current self-evolution follow-up route."
    if decision_type and target_kind:
        purpose = f"Review the self-evolution {decision_type} step for the current {target_kind}."
    elif decision_type:
        purpose = f"Review the self-evolution {decision_type} step."
    elif target_kind:
        purpose = f"Inspect the current self-evolution route for the {target_kind}."
    return [
        {
            "method": method,
            "path": path,
            "purpose": _compact_sentence(purpose, max_len=180),
        }
    ]


def _operator_self_evolution_snapshot(briefing: dict) -> dict:
    packet = briefing if isinstance(briefing, dict) else {}
    snapshot = packet.get("snapshot", {}) if isinstance(packet.get("snapshot", {}), dict) else {}
    decision = packet.get("decision", {}) if isinstance(packet.get("decision", {}), dict) else {}
    target = packet.get("target", {}) if isinstance(packet.get("target", {}), dict) else {}
    recommendation = (
        target.get("recommendation", {}) if isinstance(target.get("recommendation", {}), dict) else {}
    )
    backlog_item = (
        target.get("backlog_item", {}) if isinstance(target.get("backlog_item", {}), dict) else {}
    )
    proposal = target.get("proposal", {}) if isinstance(target.get("proposal", {}), dict) else {}
    action = decision.get("action", {}) if isinstance(decision.get("action", {}), dict) else {}

    target_summary = ""
    if recommendation:
        target_summary = str(
            recommendation.get("summary")
            or recommendation.get("recommendation_type")
            or ""
        ).strip()
    elif backlog_item:
        target_summary = str(
            backlog_item.get("summary")
            or backlog_item.get("proposal_type")
            or ""
        ).strip()
    elif proposal:
        target_summary = str(
            proposal.get("summary")
            or proposal.get("proposal_type")
            or ""
        ).strip()

    summary = str(decision.get("summary") or snapshot.get("summary") or "").strip()
    normalized_action = _summarize_operator_http_action(action)
    operator_commands = _build_self_evolution_operator_commands(
      decision=decision,
      target=target,
      action=normalized_action,
    )
    primary_operator_command = operator_commands[0] if operator_commands else {}
    operator_command_summary = ""
    if primary_operator_command:
      operator_command_summary = _compact_sentence(
        f"{str(primary_operator_command.get('method') or '').strip()} "
        f"{str(primary_operator_command.get('path') or '').strip()}: "
        f"{str(primary_operator_command.get('purpose') or '').strip()}",
        max_len=220,
      )
    return {
        "summary": _compact_sentence(summary, max_len=200),
        "status": str(snapshot.get("status") or "").strip(),
        "decision_type": str(decision.get("decision_type") or "").strip(),
        "priority": str(decision.get("priority") or "").strip(),
        "target_kind": str(target.get("target_kind") or decision.get("target_kind") or "").strip(),
        "target_id": target.get("target_id", decision.get("target_id")),
        "target_summary": _compact_sentence(target_summary, max_len=180),
      "action": normalized_action,
      "action_summary": str(normalized_action.get("summary") or "").strip(),
      "action_method": str(normalized_action.get("method") or "").strip(),
      "action_path": str(normalized_action.get("path") or "").strip(),
      "operator_commands": operator_commands,
      "primary_operator_command": primary_operator_command,
      "operator_command_summary": operator_command_summary,
        "snapshot": snapshot,
        "decision": decision,
        "target": target,
        "metadata_json": packet.get("metadata_json", {}) if isinstance(packet.get("metadata_json", {}), dict) else {},
        "created_at": packet.get("created_at"),
    }


def _operator_execution_recovery_snapshot(recovery: dict) -> dict:
    if not isinstance(recovery, dict):
        return {}
    reason = _compact_sentence(str(recovery.get("recovery_reason") or ""), max_len=180)
    decision = str(recovery.get("recovery_decision") or "").strip()
    summary = _compact_sentence(
        str(recovery.get("summary") or "") or (
            f"{decision.replace('_', ' ')}: {reason}" if decision and reason else decision.replace("_", " ")
        ),
        max_len=180,
    )
    return {
        "trace_id": str(recovery.get("trace_id") or "").strip(),
        "execution_id": recovery.get("execution_id"),
        "managed_scope": str(recovery.get("managed_scope") or "").strip(),
        "execution_status": str(recovery.get("execution_status") or "").strip(),
        "recovery_decision": decision,
        "recovery_classification": str(recovery.get("recovery_classification") or "").strip(),
        "recovery_taxonomy": recovery.get("recovery_taxonomy", {}) if isinstance(recovery.get("recovery_taxonomy", {}), dict) else {},
        "recovery_policy_tuning": recovery.get("recovery_policy_tuning", {}) if isinstance(recovery.get("recovery_policy_tuning", {}), dict) else {},
        "recommended_attempt_decision": str(recovery.get("recommended_attempt_decision") or "").strip(),
        "recovery_reason": reason,
        "operator_action_required": bool(recovery.get("operator_action_required", False)),
        "recovery_allowed": bool(recovery.get("recovery_allowed", False)),
        "resume_step_key": str(recovery.get("resume_step_key") or "").strip(),
        "summary": summary,
        "latest_attempt": recovery.get("latest_attempt", {}) if isinstance(recovery.get("latest_attempt", {}), dict) else {},
        "latest_outcome": recovery.get("latest_outcome", {}) if isinstance(recovery.get("latest_outcome", {}), dict) else {},
        "recovery_learning": recovery.get("recovery_learning", {}) if isinstance(recovery.get("recovery_learning", {}), dict) else {},
        "why_recovery_escalated_before_retry": str(recovery.get("why_recovery_escalated_before_retry") or "").strip(),
        "conflict_resolution": recovery.get("conflict_resolution", {}) if isinstance(recovery.get("conflict_resolution", {}), dict) else {},
    }


def _operator_execution_recovery_policy_tuning_snapshot(recovery: dict) -> dict:
    if not isinstance(recovery, dict):
        return {}
    tuning = (
        recovery.get("recovery_policy_tuning", {})
        if isinstance(recovery.get("recovery_policy_tuning", {}), dict)
        else {}
    )
    if not tuning:
        return {}
    return {
        "managed_scope": str(recovery.get("managed_scope") or "").strip(),
        "policy_action": str(tuning.get("policy_action") or "").strip(),
        "current_boundary_level": str(tuning.get("current_boundary_level") or "").strip(),
        "recommended_boundary_level": str(tuning.get("recommended_boundary_level") or "").strip(),
        "operator_review_required": bool(tuning.get("operator_review_required", False)),
        "boundary_floor_applied": bool(tuning.get("boundary_floor_applied", False)),
        "summary": _compact_sentence(str(tuning.get("summary") or "").strip(), max_len=180),
        "rationale": _compact_sentence(str(tuning.get("rationale") or "").strip(), max_len=180),
        "recovery_decision": str(tuning.get("recovery_decision") or "").strip(),
        "recommended_attempt_decision": str(tuning.get("recommended_attempt_decision") or "").strip(),
        "recovery_classification": str(tuning.get("recovery_classification") or "").strip(),
        "applies_to": str(tuning.get("applies_to") or "").strip(),
        "source": str(tuning.get("source") or "").strip(),
        "evidence": tuning.get("evidence", {}) if isinstance(tuning.get("evidence", {}), dict) else {},
    }


def _operator_execution_recovery_learning_snapshot(recovery: dict) -> dict:
    if not isinstance(recovery, dict):
        return {}
    learning = recovery.get("recovery_learning", {}) if isinstance(recovery.get("recovery_learning", {}), dict) else {}
    if not learning:
        return {}
    summary = _compact_sentence(
        str(
            learning.get("why_recovery_escalated_before_retry")
            or learning.get("rationale")
            or ""
        ).strip(),
        max_len=180,
    )
    return {
        "managed_scope": str(learning.get("managed_scope") or recovery.get("managed_scope") or "").strip(),
        "capability_family": str(learning.get("capability_family") or "").strip(),
        "recovery_decision": str(learning.get("recovery_decision") or "").strip(),
        "learning_state": str(learning.get("learning_state") or "").strip(),
        "escalation_decision": str(learning.get("escalation_decision") or "").strip(),
        "confidence": float(learning.get("confidence") or 0.0),
        "sample_count": int(learning.get("sample_count") or 0),
        "summary": summary,
        "why_recovery_escalated_before_retry": str(
            learning.get("why_recovery_escalated_before_retry") or ""
        ).strip(),
    }


def _operator_gateway_governance_snapshot(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        return {}
    signal_codes = [
        str(item).strip()
        for item in (snapshot.get("signal_codes") or [])
        if str(item).strip()
    ]
    return {
        "applied_reason": str(snapshot.get("applied_reason") or "").strip(),
        "applied_outcome": str(snapshot.get("applied_outcome") or "").strip(),
        "primary_signal": str(snapshot.get("primary_signal") or "").strip(),
        "system_health_status": str(snapshot.get("system_health_status") or "").strip(),
        "summary": _compact_sentence(str(snapshot.get("summary") or "").strip(), max_len=220),
        "signal_codes": signal_codes,
        "signal_count": len(signal_codes),
        "precedence_order": [
            str(item).strip()
            for item in (snapshot.get("precedence_order") or [])
            if str(item).strip()
        ],
    }


async def _latest_gateway_governance_snapshot(
    *,
    db: AsyncSession,
    managed_scope: str,
) -> dict:
    normalized_scope = str(managed_scope or "").strip()
    rows = (
        (
            await db.execute(
                select(InputEventResolution)
                .order_by(InputEventResolution.id.desc())
                .limit(120)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        governance = (
            metadata.get("governance")
            if isinstance(metadata.get("governance"), dict)
            else {}
        )
        if not governance:
            continue
        governance_scope = str(governance.get("managed_scope") or "").strip()
        if normalized_scope and governance_scope and governance_scope != normalized_scope:
            continue
        return _operator_gateway_governance_snapshot(governance)
    return {}


def _operator_current_recommendation(
    *,
    inquiry: dict,
    governance: dict,
    autonomy: dict,
    stewardship: dict,
    commitment: dict,
  recovery_commitment: dict,
    execution_recovery: dict,
    commitment_monitoring: dict,
    commitment_outcome: dict,
    strategy_plan: dict,
) -> dict:
    continuation = (
      strategy_plan.get("continuation_state", {})
      if isinstance(strategy_plan.get("continuation_state", {}), dict)
      else {}
    )
    explainability = (
      strategy_plan.get("explainability", {})
      if isinstance(strategy_plan.get("explainability", {}), dict)
      else {}
    )
    safety_envelope = (
      strategy_plan.get("safety_envelope", {})
      if isinstance(strategy_plan.get("safety_envelope", {}), dict)
      else {}
    )
    if strategy_plan and bool(safety_envelope.get("operator_review_required", False)):
      return {
        "source": "strategy_safety_envelope",
        "decision": str(strategy_plan.get("status") or "pending_review").strip() or "pending_review",
        "managed_scope": str(strategy_plan.get("managed_scope") or "").strip(),
        "summary": _compact_sentence(
          str(
            safety_envelope.get("stop_reason")
            or safety_envelope.get("governance_decision")
            or explainability.get("why_it_did_it")
            or "strategy safety envelope requested operator review"
          ).strip(),
          max_len=180,
        ),
      }
    if strategy_plan and bool(continuation.get("should_stop", False)):
      return {
        "source": "strategy_plan",
        "decision": str(strategy_plan.get("status") or "blocked").strip() or "blocked",
        "managed_scope": str(strategy_plan.get("managed_scope") or "").strip(),
        "summary": _compact_sentence(
          str(
            continuation.get("stop_reason")
            or explainability.get("what_it_will_do_next")
            or explainability.get("summary")
            or "strategy plan requested stop"
          ).strip(),
          max_len=180,
        ),
      }
    if strategy_plan and bool(continuation.get("can_continue", False)):
      return {
        "source": "strategy_plan",
        "decision": "continue_plan",
        "managed_scope": str(strategy_plan.get("managed_scope") or "").strip(),
        "summary": _compact_sentence(
          str(
            explainability.get("what_it_will_do_next")
            or explainability.get("summary")
            or strategy_plan.get("goal_summary")
            or "continue bounded strategy plan"
          ).strip(),
          max_len=180,
        ),
      }
    if str(commitment_monitoring.get("governance_decision") or "").strip() and str(
        commitment_monitoring.get("governance_decision") or ""
    ).strip() != "maintain_commitment":
        return {
            "source": "commitment_monitoring",
            "decision": str(commitment_monitoring.get("governance_decision") or "").strip(),
            "managed_scope": str(commitment_monitoring.get("managed_scope") or "").strip(),
            "summary": _compact_sentence(
                str(commitment_monitoring.get("governance_reason") or "").strip(),
                max_len=180,
            ),
        }
    if str(commitment_outcome.get("outcome_status") or "").strip() in {
        "ineffective",
        "harmful",
        "abandoned",
    }:
        return {
            "source": "commitment_outcome",
            "decision": str(commitment_outcome.get("outcome_status") or "").strip(),
            "managed_scope": str(commitment_outcome.get("managed_scope") or "").strip(),
            "summary": _compact_sentence(
                str(commitment_outcome.get("outcome_reason") or "").strip(),
                max_len=180,
            ),
        }

    active_commitment = commitment
    if not (
        bool(active_commitment.get("active", False))
        and str(active_commitment.get("decision_type") or "").strip()
    ):
        active_commitment = recovery_commitment if isinstance(recovery_commitment, dict) else {}
    if bool(active_commitment.get("active", False)) and str(active_commitment.get("decision_type") or "").strip():
        return {
            "source": "governance",
            "decision": str(active_commitment.get("decision_type") or "").strip(),
            "managed_scope": str(active_commitment.get("managed_scope") or "").strip(),
            "summary": _compact_sentence(
                str(active_commitment.get("reason") or active_commitment.get("summary") or "").strip(),
                max_len=180,
            ),
        }

    if str(governance.get("governance_decision") or "").strip():
        decision = str(governance.get("governance_decision") or "").strip()
        scope = str(governance.get("managed_scope") or "").strip()
        reason = str(governance.get("governance_reason") or "").strip()
        summary = decision.replace("_", " ")
        if scope:
            summary = f"{summary} on {scope}"
        if reason:
            summary = _compact_sentence(f"{summary}: {reason}", max_len=180)
        return {
            "source": "governance",
            "decision": decision,
            "managed_scope": scope,
            "summary": summary,
        }

    recovery_learning = (
        execution_recovery.get("recovery_learning", {})
        if isinstance(execution_recovery.get("recovery_learning", {}), dict)
        else {}
    )
    recovery_conflict_resolution = (
        execution_recovery.get("conflict_resolution", {})
        if isinstance(execution_recovery.get("conflict_resolution", {}), dict)
        else {}
    )
    recovery_policy_tuning = (
      execution_recovery.get("recovery_policy_tuning", {})
      if isinstance(execution_recovery.get("recovery_policy_tuning", {}), dict)
      else {}
    )
    if str(recovery_conflict_resolution.get("winning_policy_source") or "").strip() in {"operator_commitment", "execution_recovery_commitment"} and str(
      recovery_policy_tuning.get("policy_action") or ""
    ).strip():
      return {
        "source": "governance",
        "decision": "lower_autonomy_for_scope",
        "managed_scope": str(execution_recovery.get("managed_scope") or recovery_learning.get("managed_scope") or "").strip(),
        "summary": _compact_sentence(
          str(
            recovery_policy_tuning.get("summary")
            or recovery_policy_tuning.get("rationale")
            or ""
          ).strip(),
          max_len=180,
        ),
      }
    if str(recovery_policy_tuning.get("policy_action") or "").strip() and str(
      recovery_policy_tuning.get("policy_action") or ""
    ).strip() != "maintain_current_recovery_autonomy":
      return {
        "source": "execution_recovery_policy_tuning",
        "decision": str(recovery_policy_tuning.get("policy_action") or "").strip(),
        "managed_scope": str(execution_recovery.get("managed_scope") or recovery_learning.get("managed_scope") or "").strip(),
        "summary": _compact_sentence(
          str(
            recovery_policy_tuning.get("summary")
            or recovery_policy_tuning.get("rationale")
            or ""
          ).strip(),
          max_len=180,
        ),
      }
    if str(recovery_learning.get("escalation_decision") or "").strip() and str(
        recovery_learning.get("escalation_decision") or ""
    ).strip() != "continue_bounded_recovery":
        return {
            "source": "execution_recovery_learning",
            "decision": str(recovery_learning.get("escalation_decision") or "").strip(),
            "managed_scope": str(recovery_learning.get("managed_scope") or "").strip(),
            "summary": _compact_sentence(
                str(
                    recovery_learning.get("why_recovery_escalated_before_retry")
                    or recovery_learning.get("rationale")
                    or ""
                ).strip(),
                max_len=180,
            ),
        }
    if str(execution_recovery.get("recovery_decision") or "").strip() and (
        bool(execution_recovery.get("operator_action_required", False))
        or bool(execution_recovery.get("recovery_allowed", False))
    ):
        return {
            "source": "execution_recovery",
            "decision": str(execution_recovery.get("recovery_decision") or "").strip(),
            "managed_scope": str(execution_recovery.get("managed_scope") or "").strip(),
            "summary": _compact_sentence(
                str(execution_recovery.get("summary") or execution_recovery.get("recovery_reason") or "").strip(),
                max_len=180,
            ),
        }
    if str(stewardship.get("last_decision_summary") or "").strip():
        decision = str(stewardship.get("followup_status") or "").strip()
        scope = str(stewardship.get("managed_scope") or "").strip()
        return {
            "source": "stewardship",
            "decision": decision,
            "managed_scope": scope,
            "summary": str(stewardship.get("last_decision_summary") or "").strip(),
        }
    if str(inquiry.get("decision_state") or "").strip():
        return {
            "source": "inquiry",
            "decision": str(inquiry.get("decision_state") or "").strip(),
            "managed_scope": str(inquiry.get("managed_scope") or "").strip(),
            "summary": str(inquiry.get("waiting_decision") or "").strip(),
        }
    if str(autonomy.get("current_level") or "").strip():
        return {
            "source": "autonomy",
            "decision": str(autonomy.get("current_level") or "").strip(),
            "managed_scope": str(autonomy.get("scope") or "").strip(),
            "summary": str(autonomy.get("adaptation_summary") or "").strip(),
        }
    return {}


def _operator_resolution_commitment_snapshot(
    row: WorkspaceOperatorResolutionCommitment | None,
) -> dict:
    if row is None:
        return {}
    snapshot = commitment_snapshot(row)
    snapshot["reason"] = _compact_sentence(
        str(snapshot.get("reason", "")),
        max_len=180,
    )
    snapshot["effect_labels"] = commitment_effect_labels(row)
    return snapshot


def _operator_resolution_commitment_monitoring_snapshot(
    row: WorkspaceOperatorResolutionCommitmentMonitoringProfile | None,
) -> dict:
    if row is None:
        return {}
    snapshot = to_operator_resolution_commitment_monitoring_out(row)
    snapshot["governance_reason"] = _compact_sentence(
        str(snapshot.get("governance_reason") or ""),
        max_len=180,
    )
    return snapshot


def _operator_resolution_commitment_outcome_snapshot(
    row: WorkspaceOperatorResolutionCommitmentOutcomeProfile | None,
) -> dict:
    if row is None:
        return {}
    snapshot = to_operator_resolution_commitment_outcome_out(row)
    snapshot["outcome_reason"] = _compact_sentence(
        str(snapshot.get("outcome_reason") or ""),
        max_len=180,
    )
    return snapshot


def _load_json_artifact(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_execution_identity(payload: dict) -> dict:
  task_id = str(payload.get("task_id") or payload.get("registry_task_id") or "").strip()
  request_id = str(payload.get("request_id") or payload.get("bridge_request_id") or "").strip()
  execution_id = str(payload.get("execution_id") or "").strip()
  id_kind = str(payload.get("id_kind") or payload.get("execution_id_kind") or "").strip()
  if not execution_id:
    if id_kind == "bridge_request_id":
      execution_id = request_id or task_id
    elif id_kind == "mim_task_registry_id":
      execution_id = task_id or request_id
    else:
      execution_id = request_id or task_id
  if not id_kind:
    if request_id and execution_id == request_id:
      id_kind = "bridge_request_id"
    elif task_id and execution_id == task_id:
      id_kind = "mim_task_registry_id"
  execution_lane = str(payload.get("execution_lane") or "").strip()
  if not execution_lane:
    execution_lane = (
      "tod_bridge_request"
      if id_kind == "bridge_request_id"
      else ("mim_task_registry" if id_kind == "mim_task_registry_id" else "")
    )
  if id_kind == "bridge_request_id":
    execution_id_label = f"bridge request {execution_id}" if execution_id else ""
  elif id_kind == "mim_task_registry_id":
    execution_id_label = f"task {execution_id}" if execution_id else ""
  else:
    execution_id_label = execution_id
  return {
    "execution_id": execution_id,
    "id_kind": id_kind,
    "execution_lane": execution_lane,
    "execution_id_label": execution_id_label,
    "task_id": task_id,
    "request_id": request_id,
  }


def _operator_collaboration_progress_snapshot(
    shared_root: Path = SHARED_RUNTIME_ROOT,
) -> dict:
  payload = _load_json_artifact(shared_root / "MIM_TOD_COLLAB_PROGRESS.latest.json")
  if not payload:
    return {}

  identity = _resolve_execution_identity(payload)

  workstreams_raw = payload.get("workstreams")
  workstreams = [
    item for item in workstreams_raw if isinstance(item, dict)
  ] if isinstance(workstreams_raw, list) else []
  first = workstreams[0] if workstreams else {}
  active = next(
    (
      item
      for item in workstreams
      if str(item.get("tod_status") or "").strip().endswith("recovery_in_progress")
      or "recovery" in str(item.get("name") or "").strip().lower()
    ),
    first,
  )
  name = str(active.get("name") or "").strip().replace("_", " ")
  mim_status = str(active.get("mim_status") or "").strip().replace("_", " ")
  tod_status = str(active.get("tod_status") or "").strip().replace("_", " ")
  observation = _compact_sentence(
    str(active.get("latest_observation") or ""),
    max_len=180,
  )
  summary_parts = []
  if identity["execution_id_label"]:
    summary_parts.append(identity["execution_id_label"])
  if name:
    summary_parts.append(name)
  if tod_status:
    summary_parts.append(tod_status)
  elif mim_status:
    summary_parts.append(mim_status)
  summary = _compact_sentence(" | ".join(summary_parts), max_len=180)
  return {
    **identity,
    "generated_at": str(payload.get("generated_at") or "").strip(),
    "type": str(payload.get("type") or "").strip(),
    "summary": summary,
    "active_workstream": {
      "name": str(active.get("name") or "").strip(),
      "mim_status": str(active.get("mim_status") or "").strip(),
      "tod_status": str(active.get("tod_status") or "").strip(),
      "latest_observation": observation,
    },
    "workstreams": [
      {
        "id": int(item.get("id") or 0),
        "name": str(item.get("name") or "").strip(),
        "mim_status": str(item.get("mim_status") or "").strip(),
        "tod_status": str(item.get("tod_status") or "").strip(),
        "latest_observation": _compact_sentence(
          str(item.get("latest_observation") or ""),
          max_len=180,
        ),
      }
      for item in workstreams[:5]
    ],
  }


def _operator_dispatch_telemetry_snapshot(
    shared_root: Path = SHARED_RUNTIME_ROOT,
) -> dict:
  payload = refresh_dispatch_telemetry_record(shared_root)
  if not payload:
    return {}

  evidence_sources = payload.get("evidence_sources")
  evidence_items = [item for item in evidence_sources if isinstance(item, dict)] if isinstance(evidence_sources, list) else []
  evidence_kinds = [
    str(item.get("kind") or "").strip()
    for item in evidence_items
    if str(item.get("kind") or "").strip()
  ]
  command_name = str(payload.get("command_name") or "").strip()
  request_id = str(payload.get("request_id") or "").strip()
  dispatch_status = str(payload.get("dispatch_status") or "").strip().replace("_", " ")
  completion_status = str(payload.get("completion_status") or "").strip().replace("_", " ")
  summary_parts = []
  if command_name:
    summary_parts.append(command_name)
  if request_id:
    summary_parts.append(request_id)
  if dispatch_status:
    summary_parts.append(f"dispatch {dispatch_status}")
  if completion_status and completion_status != "pending":
    summary_parts.append(f"completion {completion_status}")
  if evidence_kinds:
    summary_parts.append(f"evidence via {', '.join(evidence_kinds[:4])}")

  return {
    "request_id": request_id,
    "task_id": str(payload.get("task_id") or "").strip(),
    "correlation_id": str(payload.get("correlation_id") or "").strip(),
    "execution_id": payload.get("execution_id"),
    "execution_lane": str(payload.get("execution_lane") or "").strip(),
    "command_name": command_name,
    "dispatch_timestamp": str(payload.get("dispatch_timestamp") or "").strip(),
    "host_received_timestamp": str(payload.get("host_received_timestamp") or "").strip(),
    "host_completed_timestamp": str(payload.get("host_completed_timestamp") or "").strip(),
    "dispatch_status": str(payload.get("dispatch_status") or "").strip(),
    "completion_status": str(payload.get("completion_status") or "").strip(),
    "result_reason": str(payload.get("result_reason") or "").strip(),
    "record_path": str(payload.get("record_path") or "").strip(),
    "evidence_source_kinds": evidence_kinds,
    "summary": "; ".join(summary_parts),
  }


def _operator_tod_decision_process_snapshot(
    shared_root: Path = SHARED_RUNTIME_ROOT,
) -> dict:
  payload = _load_json_artifact(shared_root / "MIM_DECISION_TASK.latest.json")
  if not payload:
    return {}

  decision_process = payload.get("decision_process") if isinstance(payload.get("decision_process"), dict) else {}
  questions = decision_process.get("questions") if isinstance(decision_process.get("questions"), dict) else {}
  tod_knows = questions.get("tod_knows_what_mim_did") if isinstance(questions.get("tod_knows_what_mim_did"), dict) else {}
  mim_knows = questions.get("mim_knows_what_tod_did") if isinstance(questions.get("mim_knows_what_tod_did"), dict) else {}
  tod_work = questions.get("tod_current_work") if isinstance(questions.get("tod_current_work"), dict) else {}
  tod_liveness = questions.get("tod_liveness") if isinstance(questions.get("tod_liveness"), dict) else {}
  escalation = decision_process.get("communication_escalation") if isinstance(decision_process.get("communication_escalation"), dict) else {}
  if not escalation:
    escalation = payload.get("communication_escalation") if isinstance(payload.get("communication_escalation"), dict) else {}
  selected_action = decision_process.get("selected_action") if isinstance(decision_process.get("selected_action"), dict) else {}

  summary_parts = [
    f"TOD {'knows' if bool(tod_knows.get('known')) else 'does not know'} what MIM did",
    f"MIM {'knows' if bool(mim_knows.get('known')) else 'does not know'} what TOD did",
  ]
  work_phase = str(tod_work.get("phase") or "").strip().replace("_", " ")
  if work_phase:
    summary_parts.append(f"TOD work {work_phase}")
  liveness_status = str(tod_liveness.get("status") or "").strip().replace("_", " ")
  if liveness_status:
    summary_parts.append(f"liveness {liveness_status}")
  if escalation.get("required") is True:
    summary_parts.append("escalation required")

  return {
    "generated_at": str(decision_process.get("generated_at") or payload.get("generated_at") or "").strip(),
    "state": str(decision_process.get("state") or payload.get("state") or "").strip(),
    "state_reason": str(decision_process.get("state_reason") or payload.get("state_reason") or "").strip(),
    "active_task_id": str(decision_process.get("active_task_id") or payload.get("active_task_id") or "").strip(),
    "objective_id": str(decision_process.get("objective_id") or payload.get("objective_id") or "").strip(),
    "tod_knows_what_mim_did": {
      "known": bool(tod_knows.get("known")),
      "detail": str(tod_knows.get("detail") or "").strip(),
      "evidence": tod_knows.get("evidence") if isinstance(tod_knows.get("evidence"), list) else [],
    },
    "mim_knows_what_tod_did": {
      "known": bool(mim_knows.get("known")),
      "detail": str(mim_knows.get("detail") or "").strip(),
      "evidence": mim_knows.get("evidence") if isinstance(mim_knows.get("evidence"), list) else [],
    },
    "tod_current_work": {
      "known": bool(tod_work.get("known")),
      "task_id": str(tod_work.get("task_id") or "").strip(),
      "objective_id": str(tod_work.get("objective_id") or "").strip(),
      "phase": str(tod_work.get("phase") or "").strip(),
      "detail": str(tod_work.get("detail") or "").strip(),
    },
    "tod_liveness": {
      "status": str(tod_liveness.get("status") or "").strip(),
      "ask_required": bool(tod_liveness.get("ask_required") is True),
      "latest_progress_age_seconds": tod_liveness.get("latest_progress_age_seconds"),
      "ping_response_age_seconds": tod_liveness.get("ping_response_age_seconds"),
      "console_probe_age_seconds": tod_liveness.get("console_probe_age_seconds"),
      "console_probe_status": str(tod_liveness.get("console_probe_status") or "").strip(),
      "primary_alert_code": str(tod_liveness.get("primary_alert_code") or "").strip(),
    },
    "communication_escalation": {
      "required": bool(escalation.get("required") is True),
      "code": str(escalation.get("code") or "monitor_only").strip(),
      "detail": str(escalation.get("detail") or "").strip(),
      "required_cycle_count": int(escalation.get("required_cycle_count", 0) or 0),
      "block_dispatch_threshold_cycles": int(escalation.get("block_dispatch_threshold_cycles", 0) or 0),
      "console_url": str(escalation.get("console_url") or "").strip(),
      "kick_hint": str(escalation.get("kick_hint") or "").strip(),
    },
    "selected_action": {
      "code": str(selected_action.get("code") or "monitor_only").strip(),
      "detail": str(selected_action.get("detail") or "").strip(),
    },
    "blocking_reason_codes": payload.get("blocking_reason_codes") if isinstance(payload.get("blocking_reason_codes"), list) else [],
    "summary": "; ".join(part for part in summary_parts if part),
  }


def _choose_operator_resolution_commitment(
    rows: list[WorkspaceOperatorResolutionCommitment],
    *,
    scope: str,
) -> WorkspaceOperatorResolutionCommitment | None:
    return choose_operator_resolution_commitment(rows, scope=scope)


def _choose_recovery_policy_commitment(
  rows: list[WorkspaceOperatorResolutionCommitment],
  *,
  scope: str,
) -> WorkspaceOperatorResolutionCommitment | None:
  filtered = [row for row in rows if commitment_is_recovery_policy_tuning_derived(row)]
  return choose_operator_resolution_commitment(filtered, scope=scope)


def _operator_recovery_governance_rollup_snapshot(
  *,
  execution_recovery: dict,
  recovery_commitment: dict,
  recovery_commitment_monitoring: dict,
  recovery_commitment_outcome: dict,
  conflict_resolution: dict,
) -> dict:
  commitment = recovery_commitment if isinstance(recovery_commitment, dict) else {}
  monitoring = recovery_commitment_monitoring if isinstance(recovery_commitment_monitoring, dict) else {}
  outcome = recovery_commitment_outcome if isinstance(recovery_commitment_outcome, dict) else {}
  recovery = execution_recovery if isinstance(execution_recovery, dict) else {}
  conflict = conflict_resolution if isinstance(conflict_resolution, dict) else {}
  tuning = recovery.get("recovery_policy_tuning", {}) if isinstance(recovery.get("recovery_policy_tuning", {}), dict) else {}
  expiry_signal = monitoring.get("expiry_signal", {}) if isinstance(monitoring.get("expiry_signal", {}), dict) else {}
  reapply_signal = monitoring.get("reapply_signal", {}) if isinstance(monitoring.get("reapply_signal", {}), dict) else {}
  downstream = commitment.get("downstream_effects_json", {}) if isinstance(commitment.get("downstream_effects_json", {}), dict) else {}
  admission_posture = "open"
  if commitment and bool(commitment.get("active", False)):
    requested_level = str(
      downstream.get("autonomy_level") or downstream.get("autonomy_level_cap") or ""
    ).strip()
    if requested_level in {"operator_required", "manual_only"}:
      admission_posture = "operator_required"
    elif requested_level:
      admission_posture = "advisory"
  recommended_next_action = "monitor_only"
  if str(expiry_signal.get("state") or "").strip() == "ready_to_expire":
    recommended_next_action = "expire_commitment"
  elif str(reapply_signal.get("state") or "").strip() == "recommended":
    recommended_next_action = "reapply_commitment"
  elif str(conflict.get("conflict_state") or "").strip() in {"active_conflict", "cooldown_held"}:
    recommended_next_action = "review_conflict"
  elif admission_posture == "operator_required":
    recommended_next_action = "operator_review_required"
  elif commitment:
    recommended_next_action = "maintain_commitment"
  return {
    "managed_scope": str(
      commitment.get("managed_scope")
      or recovery.get("managed_scope")
      or tuning.get("recovery_boundary_scope")
      or ""
    ).strip(),
    "tuning": tuning,
    "commitment": commitment,
    "monitoring": monitoring,
    "outcome": outcome,
    "conflict": conflict,
    "expiry_signal": expiry_signal,
    "reapply_signal": reapply_signal,
    "admission_posture": admission_posture,
    "recommended_next_action": recommended_next_action,
    "scope_application": (
      commitment.get("scope_application")
      if isinstance(commitment.get("scope_application"), dict)
      else {}
    ),
    "summary": (
      f"recovery commitment={str(commitment.get('lifecycle_state') or 'inactive')}; "
      f"admission={admission_posture}; next={recommended_next_action}"
    ),
  }


def _operator_strategy_plan_snapshot(row: ExecutionStrategyPlan | None) -> dict:
  if row is None:
    return {}
  payload = to_execution_strategy_plan_out(row)
  continuation = payload.get("continuation_state", {}) if isinstance(payload.get("continuation_state", {}), dict) else {}
  explainability = payload.get("explainability", {}) if isinstance(payload.get("explainability", {}), dict) else {}
  payload["summary"] = _compact_sentence(
    str(explainability.get("summary") or payload.get("goal_summary") or "strategy plan available").strip(),
    max_len=180,
  )
  payload["next_step_key"] = str(continuation.get("current_step_key") or "").strip()
  return payload


def _operator_trust_explainability_snapshot(strategy_plan: dict) -> dict:
  if not strategy_plan:
    return {}
  explainability = strategy_plan.get("explainability", {}) if isinstance(strategy_plan.get("explainability", {}), dict) else {}
  continuation = strategy_plan.get("continuation_state", {}) if isinstance(strategy_plan.get("continuation_state", {}), dict) else {}
  confidence_assessment = strategy_plan.get("confidence_assessment", {}) if isinstance(strategy_plan.get("confidence_assessment", {}), dict) else {}
  environment_awareness = strategy_plan.get("environment_awareness", {}) if isinstance(strategy_plan.get("environment_awareness", {}), dict) else {}
  coordination_state = strategy_plan.get("coordination_state", {}) if isinstance(strategy_plan.get("coordination_state", {}), dict) else {}
  safety_envelope = strategy_plan.get("safety_envelope", {}) if isinstance(strategy_plan.get("safety_envelope", {}), dict) else {}
  return {
    "managed_scope": str(strategy_plan.get("managed_scope") or "").strip(),
    "confidence": float(strategy_plan.get("confidence") or 0.0),
    "confidence_tier": str(confidence_assessment.get("tier") or "").strip(),
    "what_it_did": _compact_sentence(str(explainability.get("what_it_did") or "").strip(), max_len=180),
    "why_it_did_it": _compact_sentence(str(explainability.get("why_it_did_it") or "").strip(), max_len=180),
    "what_it_will_do_next": _compact_sentence(str(explainability.get("what_it_will_do_next") or "").strip(), max_len=180),
    "confidence_reasoning": _compact_sentence(str(explainability.get("confidence_reasoning") or "").strip(), max_len=180),
    "environment_status": str(environment_awareness.get("status") or "").strip(),
    "coordination_mode": str(coordination_state.get("mode") or "").strip(),
    "safe_to_continue": bool(safety_envelope.get("safe_to_continue", False)),
    "operator_review_required": bool(safety_envelope.get("operator_review_required", False)),
    "can_continue": bool(continuation.get("can_continue", False)),
    "should_stop": bool(continuation.get("should_stop", False)),
    "stop_reason": str(continuation.get("stop_reason") or "").strip(),
  }


def _operator_trust_signal_summary(
    trust_explainability: dict,
    recommendation: dict,
) -> str:
  trust = trust_explainability if isinstance(trust_explainability, dict) else {}
  recommended = recommendation if isinstance(recommendation, dict) else {}
  parts: list[str] = []
  if str(trust.get("what_it_did") or "").strip():
    parts.append(f"did: {str(trust.get('what_it_did') or '').strip()}")
  if str(trust.get("what_it_will_do_next") or "").strip():
    parts.append(f"next: {str(trust.get('what_it_will_do_next') or '').strip()}")
  if str(trust.get("confidence_reasoning") or "").strip():
    parts.append(
      f"confidence: {str(trust.get('confidence_reasoning') or '').strip()}"
    )
  if str(recommended.get("summary") or "").strip():
    parts.append(f"recommendation: {str(recommended.get('summary') or '').strip()}")
  if bool(trust.get("operator_review_required", False)):
    parts.append("operator review required")
  elif trust.get("safe_to_continue") is True:
    parts.append("safe to continue")
  return _compact_sentence(". ".join(part for part in parts if part), max_len=220)


def _operator_lightweight_autonomy_snapshot(
    autonomy: dict,
    trust_explainability: dict,
    recommendation: dict,
) -> dict:
  autonomy_state = autonomy if isinstance(autonomy, dict) else {}
  trust = trust_explainability if isinstance(trust_explainability, dict) else {}
  recommended = recommendation if isinstance(recommendation, dict) else {}
  current_level = str(autonomy_state.get("current_level") or "").strip()
  operator_review_required = bool(trust.get("operator_review_required", False))
  automatic_ready = bool(
    trust.get("safe_to_continue")
    and trust.get("can_continue")
    and not operator_review_required
    and current_level not in {"operator_required", "manual_only"}
  )
  if automatic_ready:
    summary = (
      "Automatic continuation is currently limited to bounded low-risk steps under the active safety envelope."
    )
  else:
    summary = (
      "Automatic continuation is currently held behind operator review or runtime safeguards."
    )
  if str(recommended.get("summary") or "").strip():
    summary = f"{summary} {str(recommended.get('summary') or '').strip()}"
  return {
    "current_level": current_level,
    "automatic_ready": automatic_ready,
    "operator_review_required": operator_review_required,
    "managed_scope": str(autonomy_state.get("scope") or "").strip(),
    "recommended_source": str(recommended.get("source") or "").strip(),
    "recommended_decision": str(recommended.get("decision") or "").strip(),
    "summary": _compact_sentence(summary, max_len=220),
  }


def _operator_feedback_loop_snapshot(execution_row: CapabilityExecution | None) -> dict:
  if execution_row is None:
    return {
      "execution_id": 0,
      "managed_scope": "",
      "latest_actor": "",
      "latest_status": "",
      "latest_reason": "",
      "history_count": 0,
      "summary": "Awaiting explicit human feedback.",
    }
  feedback = execution_row.feedback_json if isinstance(execution_row.feedback_json, dict) else {}
  history = [item for item in (feedback.get("history") or []) if isinstance(item, dict)]
  latest_feedback = history[-1] if history else {}
  actor = str(
    latest_feedback.get("actor")
    or feedback.get("last_feedback_actor")
    or "operator_loop"
  ).strip()
  status = str(
    latest_feedback.get("status")
    or feedback.get("last_feedback_status")
    or execution_row.status
    or ""
  ).strip()
  reason = str(
    latest_feedback.get("reason")
    or feedback.get("last_feedback_reason")
    or execution_row.reason
    or ""
  ).strip()
  summary = "Awaiting explicit human feedback."
  if actor or status or reason:
    summary = _compact_sentence(
      f"Latest feedback loop state: {actor or 'operator'} marked {status or 'updated'}"
      f"{f' because {reason}' if reason else ''}.",
      max_len=200,
    )
  return {
    "execution_id": int(execution_row.id),
    "managed_scope": str(execution_row.managed_scope or "").strip(),
    "latest_actor": actor,
    "latest_status": status,
    "latest_reason": reason,
    "history_count": len(history),
    "summary": summary,
  }


def _operator_stability_guard_snapshot(
    runtime_health: dict,
    runtime_recovery: dict,
    gateway_governance: dict,
    tod_decision_process: dict,
) -> dict:
  health = runtime_health if isinstance(runtime_health, dict) else {}
  recovery = runtime_recovery if isinstance(runtime_recovery, dict) else {}
  governance = gateway_governance if isinstance(gateway_governance, dict) else {}
  tod_decision = tod_decision_process if isinstance(tod_decision_process, dict) else {}
  blockers: list[str] = []
  if str(health.get("status") or "").strip() not in {"", "healthy"}:
    blockers.append(str(health.get("status") or "").strip().replace("_", " "))
  if str(recovery.get("status") or "").strip() not in {"", "healthy"}:
    blockers.append(str(recovery.get("status") or "").strip().replace("_", " "))
  if str(governance.get("primary_signal") or "").strip():
    blockers.append(str(governance.get("primary_signal") or "").strip().replace("_", " "))
  escalation = (
    tod_decision.get("communication_escalation", {})
    if isinstance(tod_decision.get("communication_escalation", {}), dict)
    else {}
  )
  if bool(escalation.get("required", False)):
    blockers.append(str(escalation.get("code") or "communication escalation").strip().replace("_", " "))
  active = bool(blockers)
  summary = (
    "Stability guard sees no active runtime or handoff blockers."
    if not active
    else _compact_sentence(
      f"Stability guard is holding on {', '.join(blockers[:4])}.",
      max_len=200,
    )
  )
  return {
    "active": active,
    "blocking_conditions": blockers[:6],
    "summary": summary,
  }


def _build_operator_reasoning_payload(
    *,
    goal_row: WorkspaceStrategyGoal | None,
    inquiry_row: WorkspaceInquiryQuestion | None,
    governance_row: WorkspaceExecutionTruthGovernanceProfile | None,
    autonomy_row: WorkspaceAutonomyBoundaryProfile | None,
    stewardship_row: WorkspaceStewardshipState | None,
    stewardship_cycle_row: WorkspaceStewardshipCycle | None,
    commitment_row: WorkspaceOperatorResolutionCommitment | None,
    commitment_monitoring_row: WorkspaceOperatorResolutionCommitmentMonitoringProfile | None,
    commitment_outcome_row: WorkspaceOperatorResolutionCommitmentOutcomeProfile | None,
    recovery_commitment_row: WorkspaceOperatorResolutionCommitment | None,
    recovery_commitment_monitoring_row: WorkspaceOperatorResolutionCommitmentMonitoringProfile | None,
    recovery_commitment_outcome_row: WorkspaceOperatorResolutionCommitmentOutcomeProfile | None,
    gateway_governance_snapshot: dict,
    execution_recovery: dict,
    learned_preferences: list[dict],
    preference_conflicts_items: list[dict],
    proposal_policy_preferences: list[dict],
    policy_conflict_profiles: list[dict],
    execution_readiness: dict,
    collaboration_progress: dict,
    dispatch_telemetry: dict,
    tod_decision_process: dict,
    self_evolution_briefing: dict,
    runtime_health: dict,
    runtime_recovery: dict,
    latest_execution_row: CapabilityExecution | None,
    strategy_plan_row: ExecutionStrategyPlan | None,
) -> dict:
    goal = _operator_goal_snapshot(goal_row)
    inquiry = _operator_inquiry_snapshot(inquiry_row)
    governance = _operator_governance_snapshot(governance_row)
    autonomy = _operator_autonomy_snapshot(autonomy_row)
    stewardship = _operator_stewardship_snapshot(stewardship_row, stewardship_cycle_row)
    commitment = _operator_resolution_commitment_snapshot(commitment_row)
    commitment_monitoring = _operator_resolution_commitment_monitoring_snapshot(
      commitment_monitoring_row
    )
    commitment_outcome = _operator_resolution_commitment_outcome_snapshot(
      commitment_outcome_row
    )
    recovery_commitment = _operator_resolution_commitment_snapshot(recovery_commitment_row)
    recovery_commitment_monitoring = _operator_resolution_commitment_monitoring_snapshot(
      recovery_commitment_monitoring_row
    )
    recovery_commitment_outcome = _operator_resolution_commitment_outcome_snapshot(
      recovery_commitment_outcome_row
    )
    gateway_governance = _operator_gateway_governance_snapshot(
        gateway_governance_snapshot
    )
    proposal_policy = _operator_proposal_policy_snapshot(proposal_policy_preferences)
    conflict_resolution = _operator_policy_conflict_snapshot(policy_conflict_profiles)
    readiness = _operator_execution_readiness_snapshot(execution_readiness)
    recovery = _operator_execution_recovery_snapshot(execution_recovery)
    recovery_learning = _operator_execution_recovery_learning_snapshot(execution_recovery)
    recovery_policy_tuning = _operator_execution_recovery_policy_tuning_snapshot(execution_recovery)
    strategy_plan = _operator_strategy_plan_snapshot(strategy_plan_row)
    self_evolution = _operator_self_evolution_snapshot(self_evolution_briefing)
    trust_explainability = _operator_trust_explainability_snapshot(strategy_plan)
    recovery_governance_rollup = _operator_recovery_governance_rollup_snapshot(
      execution_recovery=execution_recovery,
      recovery_commitment=recovery_commitment,
      recovery_commitment_monitoring=recovery_commitment_monitoring,
      recovery_commitment_outcome=recovery_commitment_outcome,
      conflict_resolution=conflict_resolution,
    )
    recommendation = _operator_current_recommendation(
        inquiry=inquiry,
        governance=governance,
        autonomy=autonomy,
        stewardship=stewardship,
        commitment=commitment,
        recovery_commitment=recovery_commitment,
        execution_recovery=recovery,
        commitment_monitoring=commitment_monitoring,
        commitment_outcome=commitment_outcome,
        strategy_plan=strategy_plan,
    )
    trust_signal_summary = _operator_trust_signal_summary(
        trust_explainability,
        recommendation,
    )
    lightweight_autonomy = _operator_lightweight_autonomy_snapshot(
      autonomy,
      trust_explainability,
      recommendation,
    )
    feedback_loop = _operator_feedback_loop_snapshot(latest_execution_row)
    stability_guard = _operator_stability_guard_snapshot(
      runtime_health,
      runtime_recovery,
      gateway_governance,
      tod_decision_process,
    )
    return {
        "summary": _build_operator_reasoning_summary(
            goal=goal,
            inquiry=inquiry,
            governance=governance,
            gateway_governance=gateway_governance,
            autonomy=autonomy,
            stewardship=stewardship,
            execution_readiness=readiness,
            execution_recovery=recovery,
            commitment=commitment,
            commitment_monitoring=commitment_monitoring,
            commitment_outcome=commitment_outcome,
            learned_preferences=learned_preferences,
            proposal_policy=proposal_policy,
            conflict_resolution=conflict_resolution,
            collaboration_progress=collaboration_progress,
            dispatch_telemetry=dispatch_telemetry,
            tod_decision_process=tod_decision_process,
            self_evolution=self_evolution,
            runtime_health=runtime_health,
            runtime_recovery=runtime_recovery,
        ),
        "active_goal": goal,
        "inquiry": inquiry,
        "governance": governance,
        "gateway_governance": gateway_governance,
        "autonomy": autonomy,
        "stewardship": stewardship,
        "execution_readiness": readiness,
        "execution_recovery": recovery,
        "execution_recovery_learning": recovery_learning,
        "execution_recovery_policy_tuning": recovery_policy_tuning,
        "execution_recovery_policy_commitment": recovery_commitment,
        "execution_recovery_policy_commitment_monitoring": recovery_commitment_monitoring,
        "execution_recovery_policy_commitment_outcome": recovery_commitment_outcome,
        "execution_recovery_governance_rollup": recovery_governance_rollup,
        "strategy_plan": strategy_plan,
        "trust_explainability": trust_explainability,
        "trust_signal_summary": trust_signal_summary,
        "lightweight_autonomy": lightweight_autonomy,
        "feedback_loop": feedback_loop,
        "stability_guard": stability_guard,
        "current_recommendation": recommendation,
        "resolution_commitment": commitment,
        "commitment_monitoring": commitment_monitoring,
        "commitment_outcome": commitment_outcome,
        "learned_preferences": learned_preferences,
        "preference_conflicts": preference_conflicts_items,
        "proposal_policy": proposal_policy,
        "conflict_resolution": conflict_resolution,
        "collaboration_progress": collaboration_progress,
        "dispatch_telemetry": dispatch_telemetry,
        "tod_decision_process": tod_decision_process,
        "self_evolution": self_evolution,
        "runtime_health": runtime_health,
        "runtime_recovery": runtime_recovery,
    }


def _scope_value(raw: object) -> str:
    return str(raw or "").strip()


def _choose_operator_reasoning_scope(
    *,
    inquiry_row: WorkspaceInquiryQuestion | None,
    governance_row: WorkspaceExecutionTruthGovernanceProfile | None,
    autonomy_row: WorkspaceAutonomyBoundaryProfile | None,
    stewardship_row: WorkspaceStewardshipState | None,
) -> str:
    inquiry_scope = ""
    stewardship_scope = _scope_value(getattr(stewardship_row, "managed_scope", ""))
    governance_scope = _scope_value(getattr(governance_row, "managed_scope", ""))
    autonomy_scope = _scope_value(getattr(autonomy_row, "scope", ""))
    if inquiry_row is not None:
        trigger_evidence = (
            inquiry_row.trigger_evidence_json
            if isinstance(inquiry_row.trigger_evidence_json, dict)
            else {}
        )
        inquiry_scope = _scope_value(trigger_evidence.get("managed_scope"))
    if inquiry_scope and inquiry_scope in {
        stewardship_scope,
        governance_scope,
        autonomy_scope,
    }:
        return inquiry_scope
    return (
        stewardship_scope
        or governance_scope
        or inquiry_scope
        or autonomy_scope
    )


def _row_matches_operator_reasoning_scope(
    row: object,
    scope: str,
    *,
    inquiry: bool = False,
    autonomy: bool = False,
) -> bool:
    normalized_scope = _scope_value(scope)
    if not normalized_scope or row is None:
        return True
    if inquiry:
        trigger_evidence = (
            row.trigger_evidence_json if isinstance(row.trigger_evidence_json, dict) else {}
        )
        return _scope_value(trigger_evidence.get("managed_scope")) == normalized_scope
    if autonomy:
        return _scope_value(getattr(row, "scope", "")) == normalized_scope
    return _scope_value(getattr(row, "managed_scope", "")) == normalized_scope


def _build_camera_state_prompt(
    *,
    camera_scene_summary: str,
    camera_source_count: int,
    recognized_person: str,
    unknown_camera_label: str,
    uncertain_camera_label: str,
    missing_camera_label: str,
    camera_object_details: dict[str, dict[str, object]],
    camera_last_confidence: float,
) -> str:
    scene_prefix = ""
    if camera_scene_summary:
        if camera_source_count > 1:
            scene_prefix = f"I can currently see {camera_scene_summary}. "
        elif camera_last_confidence > 0.0:
            scene_prefix = f"I can currently see {camera_scene_summary}. Primary confidence is {camera_last_confidence:.2f}. "
        else:
            scene_prefix = f"I can currently see {camera_scene_summary}. "

    if missing_camera_label:
        details = camera_object_details.get(missing_camera_label, {})
        semantic_note = str(details.get("semantic_note") or "").strip()
        note = f" {semantic_note}." if semantic_note else ""
        return (
            f"{scene_prefix}I cannot find {missing_camera_label} on camera right now.{note} "
            f"Where did it go, or did it get moved?"
        ).strip()

    if uncertain_camera_label:
        details = camera_object_details.get(uncertain_camera_label, {})
        semantic_note = str(details.get("semantic_note") or "").strip()
        note = f" {semantic_note}." if semantic_note else ""
        return (
            f"{scene_prefix}{uncertain_camera_label} seems to have moved.{note} "
            f"Can you confirm whether that move was intentional?"
        ).strip()

    if unknown_camera_label:
        details = camera_object_details.get(unknown_camera_label, {})
        questions = [
            str(item).strip()
            for item in (details.get("inquiry_questions") or [])
            if str(item).strip()
        ]
        lead = scene_prefix or f"I can currently see {unknown_camera_label} on camera. "
        return f"{lead}I do not know what {unknown_camera_label} is yet. {' '.join(questions)}".strip()

    if recognized_person:
        if scene_prefix:
            return f"{scene_prefix}Good to see you, {recognized_person}. I recognize you on camera.".strip()
        return f"Good to see you, {recognized_person}. I recognize you on camera."

    return ""


@router.get("/mim", response_class=HTMLResponse)
async def mim_ui_page() -> str:
    return """
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>MIM</title>
  <style>
    :root {
      --bg: #071c2b;
      --panel: #0c2436;
      --line: #1fd5ff;
      --text: #d7efff;
      --muted: #9dc6d8;
      --ok: #2dcf6b;
      --err: #c56a2d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: radial-gradient(circle at 40% 20%, #0e3550, var(--bg));
      color: var(--text);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: flex-start;
      padding: 20px;
      gap: 16px;
    }
    h1 {
      margin: 0;
      letter-spacing: 0.18em;
      font-weight: 600;
      color: #e5f7ff;
    }
    .mim-icon {
      position: relative;
      display: inline-flex;
      align-items: center;
      gap: 10px;
    }
    .mim-icon::before {
      content: '';
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: #37596a;
      box-shadow: 0 0 0 rgba(0, 0, 0, 0);
      transition: background 160ms ease, box-shadow 160ms ease;
    }
    .mim-icon.ok::before {
      background: var(--ok);
      box-shadow: 0 0 18px rgba(45, 207, 107, 0.7);
    }
    .mim-icon.err::before {
      background: var(--err);
      box-shadow: 0 0 18px rgba(197, 106, 45, 0.72);
    }
    .panel {
      width: min(920px, 96vw);
      background: color-mix(in oklab, var(--panel) 88%, black 12%);
      border: 1px solid #16415a;
      border-radius: 12px;
      padding: 14px;
    }
    .top-right {
      position: fixed;
      top: 12px;
      right: 12px;
      z-index: 20;
      display: flex;
      gap: 8px;
    }
    .icon-btn {
      width: 34px;
      height: 34px;
      border-radius: 8px;
      border: 1px solid #1b6a8d;
      background: #0f3b52;
      color: #d7efff;
      cursor: pointer;
      font-size: 16px;
      line-height: 1;
    }
    .settings-panel {
      position: fixed;
      top: 52px;
      right: 12px;
      z-index: 20;
      width: min(320px, 92vw);
      background: color-mix(in oklab, var(--panel) 90%, black 10%);
      border: 1px solid #16415a;
      border-radius: 10px;
      padding: 10px;
      display: none;
    }
    .settings-panel.open { display: block; }
    .settings-title {
      font-size: 13px;
      font-weight: 600;
      color: #d7efff;
      margin-bottom: 8px;
    }
    .settings-tabs {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-bottom: 10px;
    }
    .settings-tab {
      background: #0a2c3f;
      color: var(--muted);
      border: 1px solid #1b6a8d;
      border-radius: 8px;
      padding: 7px 8px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
    }
    .settings-tab.active {
      color: #e8f7ff;
      background: #12506f;
      border-color: #2aa6d4;
    }
    .settings-view { display: none; }
    .settings-view.active { display: block; }
    .settings-row {
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
      margin-bottom: 10px;
    }
    .settings-row label {
      font-size: 12px;
      color: var(--muted);
    }
    .settings-row select,
    .settings-row input[type="text"] {
      width: 100%;
      background: #0a1f2d;
      color: var(--text);
      border: 1px solid #1a4f68;
      border-radius: 8px;
      padding: 8px;
      font-size: 13px;
    }
    .settings-note {
      font-size: 11px;
      color: var(--muted);
      margin-top: -4px;
    }
    .camera-preview {
      width: 100%;
      height: 150px;
      border-radius: 8px;
      border: 1px solid #1a4f68;
      background: #081a25;
      object-fit: cover;
    }
    .camera-preview.inactive {
      opacity: 0.55;
      filter: grayscale(0.15);
    }
    .toggle-row {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      color: var(--muted);
    }
    .wave-wrap {
      position: relative;
      overflow: hidden;
      height: 240px;
      border-radius: 10px;
      border: 1px solid #1a4f68;
      background: linear-gradient(180deg, #072538, #081c2a);
    }
    .wave {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      opacity: 0.42;
      transform: scaleY(0.35);
      transition: transform 180ms ease, opacity 180ms ease;
    }
    .wave.speaking {
      opacity: 1;
      transform: scaleY(1);
      animation: pulseGlow 1.2s ease-in-out infinite;
    }
    .bar {
      width: 4px;
      height: 110px;
      border-radius: 4px;
      background: linear-gradient(180deg, transparent 0%, var(--line) 40%, transparent 100%);
      animation: bounce 1.4s ease-in-out infinite;
      animation-play-state: paused;
    }
    .wave.speaking .bar { animation-play-state: running; }
    .bar:nth-child(3n) { animation-duration: 1.1s; }
    .bar:nth-child(4n) { animation-duration: 1.8s; }
    .bar:nth-child(5n) { animation-duration: 1.3s; }
    .status {
      margin-top: 10px;
      font-size: 14px;
      color: var(--muted);
      min-height: 20px;
    }
    .mic-event {
      margin-top: 6px;
      font-size: 13px;
      color: #9de8ff;
      min-height: 18px;
    }
    .controls {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 10px;
      margin-top: 12px;
    }
    input {
      width: 100%;
      background: #0a1f2d;
      color: var(--text);
      border: 1px solid #1a4f68;
      border-radius: 8px;
      padding: 10px;
      font-size: 14px;
    }
    button {
      background: #0f3b52;
      color: var(--text);
      border: 1px solid #1b6a8d;
      border-radius: 8px;
      padding: 10px 12px;
      cursor: pointer;
      font-size: 14px;
    }
    button:hover { filter: brightness(1.12); }
    .small {
      font-size: 12px;
      color: var(--muted);
      margin-top: 8px;
    }
    .debug-log {
      margin-top: 8px;
      font-size: 11px;
      color: #a7deef;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap;
      word-break: break-word;
      border: 1px solid #15475e;
      border-radius: 8px;
      background: #091b27;
      padding: 8px;
      min-height: 84px;
    }
    .object-memory-panel {
      margin-top: 14px;
      border: 1px solid #15475e;
      border-radius: 10px;
      background: linear-gradient(180deg, rgba(9, 27, 39, 0.96), rgba(6, 20, 30, 0.98));
      padding: 12px;
    }
    .object-memory-panel[hidden] {
      display: none;
    }
    .object-memory-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      margin-bottom: 8px;
    }
    .object-memory-title {
      font-size: 13px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #9de8ff;
    }
    .object-memory-caption {
      font-size: 11px;
      color: var(--muted);
    }
    .object-memory-list {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 8px;
    }
    .object-memory-item {
      border: 1px solid #15475e;
      border-radius: 8px;
      background: rgba(10, 31, 45, 0.92);
      padding: 8px 10px;
    }
    .object-memory-item strong {
      display: block;
      font-size: 13px;
      color: var(--text);
      margin-bottom: 3px;
    }
    .object-memory-meta {
      font-size: 11px;
      color: #9de8ff;
    }
    .object-memory-note {
      margin-top: 4px;
      font-size: 11px;
      color: var(--muted);
    }
    .text-chat-panel {
      margin-top: 14px;
      border: 1px solid #15475e;
      border-radius: 10px;
      background: linear-gradient(180deg, rgba(9, 27, 39, 0.96), rgba(6, 20, 30, 0.98));
      padding: 12px;
    }
    .chat-log {
      margin-top: 8px;
      border: 1px solid #15475e;
      border-radius: 8px;
      background: #091b27;
      min-height: 130px;
      max-height: 240px;
      overflow-y: auto;
      padding: 10px;
      display: grid;
      gap: 8px;
    }
    .chat-bubble {
      border: 1px solid #1a4f68;
      border-radius: 8px;
      padding: 8px 10px;
      font-size: 13px;
      line-height: 1.35;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .chat-bubble.user {
      background: #103047;
      justify-self: end;
      max-width: 88%;
    }
    .chat-bubble.mim {
      background: #0a2536;
      justify-self: start;
      max-width: 92%;
    }
    .chat-controls {
      display: grid;
      grid-template-columns: 1fr auto auto;
      gap: 10px;
      margin-top: 10px;
    }
    @media (max-width: 720px) {
      .chat-controls {
        grid-template-columns: 1fr;
      }
    }
    @keyframes bounce {
      0%, 100% { transform: scaleY(0.22); }
      50% { transform: scaleY(1); }
    }
    @keyframes pulseGlow {
      0%, 100% { box-shadow: inset 0 0 0 rgba(31,213,255,0.0); }
      50% { box-shadow: inset 0 0 120px rgba(31,213,255,0.16); }
    }
  </style>
</head>
<body>
  <div class="top-right">
    <button id="settingsBtn" class="icon-btn" title="MIM settings" aria-label="MIM settings">⚙</button>
  </div>

  <div id="settingsPanel" class="settings-panel" role="dialog" aria-label="MIM settings">
    <div class="settings-title">MIM Settings</div>
    <div class="settings-tabs">
      <button id="settingsTabVoice" class="settings-tab active" type="button">Voice</button>
      <button id="settingsTabCamera" class="settings-tab" type="button">Camera</button>
    </div>

    <div id="settingsViewVoice" class="settings-view active">
      <div class="settings-row">
        <label for="voiceSelect">Fixed Voice</label>
        <select id="voiceSelect"></select>
        <div class="settings-note">This stays fixed until you change it.</div>
      </div>

      <div class="settings-row toggle-row">
        <input id="serverTtsToggle" type="checkbox" checked />
        <label for="serverTtsToggle">Use Neural Server TTS (recommended)</label>
      </div>

      <div class="settings-row">
        <label for="serverTtsVoiceSelect">Neural Server Voice</label>
        <select id="serverTtsVoiceSelect"></select>
        <div class="settings-note">Higher quality voice rendered by backend TTS.</div>
      </div>

      <div class="settings-row">
        <label for="defaultLang">Default Listen Language</label>
        <input id="defaultLang" type="text" value="en-US" placeholder="en-US" />
      </div>

      <div class="settings-row">
        <label for="micSelect">Microphone Input</label>
        <select id="micSelect"></select>
        <div class="settings-note">If you have multiple mics, choose the one MIM should use.</div>
      </div>

      <div class="settings-row toggle-row">
        <input id="autoLangToggle" type="checkbox" checked />
        <label for="autoLangToggle">Speak in detected input language</label>
      </div>

      <div class="settings-row toggle-row">
        <input id="naturalVoiceToggle" type="checkbox" checked />
        <label for="naturalVoiceToggle">Natural Voice preset (smoother)</label>
      </div>

      <div class="settings-row">
        <label for="voiceRate">Voice Speed (<span id="voiceRateValue">1.00</span>)</label>
        <input id="voiceRate" type="range" min="0.70" max="1.35" step="0.05" value="1.00" />
      </div>

      <div class="settings-row">
        <label for="voicePitch">Voice Tone (<span id="voicePitchValue">1.00</span>)</label>
        <input id="voicePitch" type="range" min="0.70" max="1.35" step="0.05" value="1.00" />
      </div>

      <div class="settings-row">
        <label for="voiceDepth">Voice Depth (<span id="voiceDepthValue">0</span>)</label>
        <input id="voiceDepth" type="range" min="0" max="100" step="5" value="0" />
        <div class="settings-note">Higher depth lowers perceived pitch.</div>
      </div>

      <div class="settings-row">
        <label for="voiceVolume">Voice Volume (<span id="voiceVolumeValue">1.00</span>)</label>
        <input id="voiceVolume" type="range" min="0.40" max="1.00" step="0.05" value="1.00" />
      </div>
    </div>

    <div id="settingsViewCamera" class="settings-view">
      <div class="settings-row">
        <label for="cameraSelect">Camera Device</label>
        <select id="cameraSelect"></select>
      </div>
      <div class="settings-row">
        <video id="cameraPreview" class="camera-preview inactive" autoplay muted playsinline></video>
        <div id="cameraSettingsStatus" class="settings-note">Camera preview is idle.</div>
      </div>
      <div class="settings-row">
        <button id="cameraRefreshBtn" type="button">Refresh Camera List</button>
      </div>
      <div class="settings-row">
        <button id="cameraToggleBtn" type="button">Start Camera Preview</button>
      </div>
      <div class="settings-note">Use this panel to verify framing and permissions for MIM camera sensing.</div>
    </div>
  </div>

  <h1 id="mimIcon" class="mim-icon">MIM</h1>
  <div id="buildTag" class="small" style="text-align:center; margin-top:-8px; margin-bottom:8px;">Build: loading...</div>

  <div class=\"panel\">
    <div class=\"wave-wrap\">
      <div id=\"wave\" class=\"wave\"></div>
    </div>
    <div id=\"status\" class=\"status\">Listening...</div>
    <div id="micEvent" class="mic-event">Mic event: waiting...</div>
    <div id=\"micDiag\" class=\"small\">Mic: detecting devices...</div>
    <div id=\"micDebug\" class=\"debug-log\">Mic debug: starting...</div>
    <div id=\"camera\" class=\"small\">Camera: waiting for observations</div>
    <div id=\"inquiry\" class=\"small\"></div>

    <div class=\"controls\">
      <input id=\"sayInput\" placeholder=\"Type what MIM should say\" value=\"Hello, I am MIM.\" />
      <button id=\"speakBtn\">Speak</button>
      <button id=\"listenBtn\">Listen</button>
    </div>

    <div class=\"controls\" style=\"grid-template-columns: 1fr auto; margin-top: 10px;\">
      <input id=\"cameraInput\" placeholder=\"Who is in view? (e.g. unknown, person, alice)\" value=\"unknown\" />
      <button id=\"cameraBtn\">Send Camera Event</button>
    </div>

    <div id=\"textChatPanel\" class=\"text-chat-panel\">
      <div class=\"object-memory-header\">
        <div class=\"object-memory-title\">Text Chat</div>
        <div class=\"object-memory-caption\">Direct typed conversation</div>
      </div>
      <div id=\"chatLog\" class=\"chat-log\" aria-live=\"polite\" aria-label=\"Text chat history\">
        <div class=\"chat-bubble mim\">Text chat is ready. Type a message and press Send Text.</div>
      </div>
      <div class=\"chat-controls\">
        <input id=\"chatInput\" placeholder=\"Type a message to MIM\" value=\"\" />
        <button id=\"chatSendBtn\">Send Text</button>
        <button id=\"chatClearBtn\" type=\"button\">Clear</button>
      </div>
    </div>

    <div id=\"objectMemoryPanel\" class=\"object-memory-panel\" hidden>
      <div class=\"object-memory-header\">
        <div class=\"object-memory-title\">Object Memory</div>
        <div class=\"object-memory-caption\">Live camera continuity</div>
      </div>
      <ul id=\"objectMemoryList\" class=\"object-memory-list\"></ul>
    </div>

    <div id=\"systemReasoningPanel\" class=\"object-memory-panel\" hidden>
      <div class=\"object-memory-header\">
        <div class=\"object-memory-title\">System Reasoning</div>
        <div class=\"object-memory-caption\">Operator-visible decision context</div>
      </div>
      <div id=\"systemReasoningSummary\" class=\"object-memory-note\"></div>
      <ul id=\"systemReasoningList\" class=\"object-memory-list\"></ul>
    </div>
  </div>

  <script>
    const wave = document.getElementById('wave');
    const statusEl = document.getElementById('status');
    const micEventEl = document.getElementById('micEvent');
    const micDiagEl = document.getElementById('micDiag');
    const micDebugEl = document.getElementById('micDebug');
    const cameraEl = document.getElementById('camera');
    const inquiryEl = document.getElementById('inquiry');
    const sayInput = document.getElementById('sayInput');
    const cameraInput = document.getElementById('cameraInput');
    const listenBtn = document.getElementById('listenBtn');
    const buildTagEl = document.getElementById('buildTag');
    const chatLog = document.getElementById('chatLog');
    const chatInput = document.getElementById('chatInput');
    const chatSendBtn = document.getElementById('chatSendBtn');
    const chatClearBtn = document.getElementById('chatClearBtn');
    const mimIcon = document.getElementById('mimIcon');
    const settingsBtn = document.getElementById('settingsBtn');
    const settingsPanel = document.getElementById('settingsPanel');
    const voiceSelect = document.getElementById('voiceSelect');
    const serverTtsToggle = document.getElementById('serverTtsToggle');
    const serverTtsVoiceSelect = document.getElementById('serverTtsVoiceSelect');
    const micSelect = document.getElementById('micSelect');
    const defaultLangInput = document.getElementById('defaultLang');
    const autoLangToggle = document.getElementById('autoLangToggle');
    const naturalVoiceToggle = document.getElementById('naturalVoiceToggle');
    const voiceRateInput = document.getElementById('voiceRate');
    const voicePitchInput = document.getElementById('voicePitch');
    const voiceDepthInput = document.getElementById('voiceDepth');
    const voiceVolumeInput = document.getElementById('voiceVolume');
    const voiceRateValueEl = document.getElementById('voiceRateValue');
    const voicePitchValueEl = document.getElementById('voicePitchValue');
    const voiceDepthValueEl = document.getElementById('voiceDepthValue');
    const voiceVolumeValueEl = document.getElementById('voiceVolumeValue');
    const settingsTabVoice = document.getElementById('settingsTabVoice');
    const settingsTabCamera = document.getElementById('settingsTabCamera');
    const settingsViewVoice = document.getElementById('settingsViewVoice');
    const settingsViewCamera = document.getElementById('settingsViewCamera');
    const cameraSelect = document.getElementById('cameraSelect');
    const cameraPreview = document.getElementById('cameraPreview');
    const cameraSettingsStatus = document.getElementById('cameraSettingsStatus');
    const cameraRefreshBtn = document.getElementById('cameraRefreshBtn');
    const cameraToggleBtn = document.getElementById('cameraToggleBtn');
    const objectMemoryPanel = document.getElementById('objectMemoryPanel');
    const objectMemoryList = document.getElementById('objectMemoryList');
    const systemReasoningPanel = document.getElementById('systemReasoningPanel');
    const systemReasoningSummary = document.getElementById('systemReasoningSummary');
    const systemReasoningList = document.getElementById('systemReasoningList');

    window.addEventListener('error', (event) => {
      const msg = String(event?.message || 'unknown_js_error');
      if (micEventEl) {
        micEventEl.textContent = `Mic event: js-error:${msg}`;
      }
      statusEl.textContent = `UI error: ${msg}`;
    });

    let micAutoMode = false;
    let micListening = false;
    let recognition = null;

    function appendChatMessage(role, text) {
      if (!chatLog) return;
      const clean = String(text || '').trim();
      if (!clean) return;
      const item = document.createElement('div');
      item.className = `chat-bubble ${role === 'user' ? 'user' : 'mim'}`;
      item.textContent = clean;
      chatLog.appendChild(item);
      chatLog.scrollTop = chatLog.scrollHeight;
    }

    function summarizeTextResolution(result) {
      const resolution = result && typeof result.resolution === 'object' ? result.resolution : {};
      const prompt = String(resolution.clarification_prompt || '').trim();
      if (prompt) {
        return prompt;
      }

      const outcome = String(resolution.outcome || '').trim().toLowerCase();
      if (outcome === 'auto_execute') {
        return 'Request accepted and routed for execution.';
      }
      if (outcome === 'requires_confirmation') {
        return 'I need one more specific detail before executing that request.';
      }
      if (outcome === 'store_only') {
        return 'Saved. Ask one specific question or request one action when ready.';
      }
      if (outcome === 'blocked') {
        return 'This request is currently blocked by safety or policy checks.';
      }
      return 'Message received.';
    }

    async function sendTextChat() {
      const text = String(chatInput ? chatInput.value : '').trim();
      if (!text) return;

      appendChatMessage('user', text);
      if (chatInput) chatInput.value = '';

      try {
        const response = await fetch('/gateway/intake/text', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            text,
            parsed_intent: 'discussion',
            metadata_json: {
              source: 'mim_ui_text_chat',
              route_preference: 'conversation_layer',
            },
          }),
        });
        if (!response.ok) {
          appendChatMessage('mim', `Text chat request failed (${response.status}).`);
          return;
        }

        const result = await response.json();
        appendChatMessage('mim', summarizeTextResolution(result));
      } catch (error) {
        const detail = error && error.message ? String(error.message) : 'request_failed';
        appendChatMessage('mim', `Text chat is temporarily unavailable (${detail}).`);
      }
      refreshState();
    }

    let motionInterval = null;
    let availableCameras = [];
    let selectedCameraDeviceId = localStorage.getItem('mim_camera_device_id') || '';
    let cameraStream = null;
    let cameraWatcherVideo = null;
    let cameraWatcherCanvas = null;
    let cameraWatcherCtx = null;
    let cameraLastFrame = null;
    let cameraLastSentAt = 0;
    let cameraLastHeartbeatAt = 0;
    let cameraWatcherStartedAt = 0;
    let cameraLastFrameSeenAt = 0;
    let cameraLastHealthyFrameAt = 0;
    let lastSpokenOutputId = Number(localStorage.getItem('mim_last_spoken_output_id') || 0);
    let availableVoices = [];
    let availableMics = [];
    let selectedVoiceURI = localStorage.getItem('mim_voice_uri') || '';
    let selectedVoiceName = localStorage.getItem('mim_voice_name') || '';
    let selectedMicDeviceId = localStorage.getItem('mim_mic_device_id') || '';
    let selectedMicLabel = localStorage.getItem('mim_mic_device_label') || '';
    let voiceRate = Number(localStorage.getItem('mim_voice_rate') || 0.90);
    let voicePitch = Number(localStorage.getItem('mim_voice_pitch') || 0.90);
    let voiceDepth = Number(localStorage.getItem('mim_voice_depth') || 22);
    let voiceVolume = Number(localStorage.getItem('mim_voice_volume') || 0.95);
    const VOICE_PROFILE_MIGRATION_VERSION = 'voice-natural-v2';
    const healthState = {
      backendOk: true,
      micOk: true,
      micAvailable: true,
      cameraOk: true,
      voicesOk: true,
      voicesLoaded: false,
    };
    let backendFailureStreak = 0;
    let backendSuccessStreak = 0;
    let micErrorStreak = 0;
    let micRetryTimer = null;
    let micHardErrorStreak = 0;
    let micLastErrorCode = '';
    let micRecoveryMode = false;
    let micRecoveryReason = '';
    let micCooldownUntil = 0;
    let micEndTimestamps = [];
    let micRecentErrorAt = 0;
    let micConsecutiveOnend = 0;
    let micLastActiveAt = 0;
    let micStartInFlight = false;
    let micRestartPending = false;
    let micStartTimeoutTimer = null;
    let micLastLifecycleEventAt = 0;
    let micStartAttemptStreak = 0;
    let micStartTimeoutStreak = 0;
    let micStartFailureStreak = 0;
    let micSessionStartedAt = 0;
    let micShortRunStreak = 0;
    let micUnstableCycleCount = 0;
    let micLastEvent = '';
    let micLastEventAt = 0;
    let micDebugLines = [];
    let micFallbackNoSpeechTimer = null;
    let micFallbackCaptureInFlight = false;
    let micFallbackInterval = null;
    let micLastSpeechEventAt = 0;
    let micLastResultAt = 0;
    let cameraRecoveryInFlight = false;
    let voiceRecoveryInterval = null;
    let voiceRecoveryAttempts = 0;
    const MIC_FLAP_WINDOW_MS = 12000;
    const MIC_FLAP_THRESHOLD = 5;
    const MIC_FLAP_COOLDOWN_MS = 5000;
    const MIC_SHORT_RUN_MS = 1200;
    const MIC_SHORT_RUN_LIMIT = 4;
    const MIC_UNSTABLE_MAX_CYCLES = 3;
    const MIC_EVENT_MIN_INTERVAL_SECONDS = 0;
    const MIC_EVENT_DUPLICATE_WINDOW_SECONDS = 2;
    const MIC_EVENT_CONFIDENCE_FLOOR = 0.2;
    const MIC_POCKETSPHINX_CONFIDENCE_MIN = 0.55;
    const MIC_FALLBACK_CAPTURE_MS = 3600;
    const MIC_FALLBACK_INTERVAL_MS = 5200;
    const MIC_LOCAL_PROVIDER_BACKOFF_MS = 300000;
    const MIC_POST_TTS_SUPPRESS_MS = 1100;
    const MIC_ECHO_MATCH_WINDOW_MS = 20000;
    const MIC_ECHO_MIN_SIGNATURE_LEN = 6;
    const RUNTIME_HEALTH_RECOVERY_COOLDOWN_MS = 15000;
    const CAMERA_FRAME_STARVED_MS = 3500;
    const CAMERA_HEARTBEAT_MS = 8000;
    const STATE_POLL_SPEAK_ENABLED = false;
    const WAKE_WORD_REQUIRED_FOR_LIVE_REPLY = true;
    const LOW_VALUE_CLARIFY_COOLDOWN_MS = 15000;
    const LOW_VALUE_SPEAK_COOLDOWN_MS = 180000;
    const SPOKEN_PHRASE_DEDUPE_MS = 2500;
    const GREETING_CLARIFY_COOLDOWN_MS = 12000;
    const SPOKEN_DUPLICATE_COOLDOWN_MS = 45000;
    const BACKEND_INQUIRY_SPEAK_COOLDOWN_MS = 25000;
    const FORCE_FALLBACK_STT = false;
    const PIN_TO_SYSTEM_DEFAULT_MIC = true;
    const WEAK_IDENTITY_WORDS = new Set(['there', 'here', 'their', 'theyre', 'unknown', 'person', 'human', 'visitor']);
    let startupInquiryIssued = false;
    let latestUiState = null;
    let lastInquiryPromptSpoken = '';
    let weakIdentityClarifyCooldownUntil = 0;
    let weakIdentityLastPromptKey = '';
    let lowValueClarifyCooldownUntil = 0;
    let lowValueSpeakCooldownUntil = 0;
    let lowValueClarifyLastCompact = '';
    let greetingClarifyCooldownUntil = 0;
    let startupFeedbackCooldownUntil = 0;
    let startupFeedbackLastCompact = '';
    let suppressBackendInquiryUntil = 0;
    let backendInquirySpeakCooldownUntil = 0;
    let lastBackendInquirySignature = '';
    let locallyAcceptedIdentity = '';
    let lastLocalTtsError = '';
    let micPermissionState = 'unknown';
    let micPermissionStream = null;
    let micKeepAliveAudioContext = null;
    let micKeepAliveSourceNode = null;
    let micKeepAliveGainNode = null;
    let micKeepAliveProcessorNode = null;
    let micKeepAliveRecorder = null;
    let micProviderLocalBackoffUntil = 0;
    const SYSTEM_DEFAULT_LANG = 'en-US';
    let defaultListenLang = localStorage.getItem('mim_default_listen_lang') || SYSTEM_DEFAULT_LANG;
    let autoLanguageMode = localStorage.getItem('mim_auto_lang_mode') !== '0';
    let naturalVoicePreset = localStorage.getItem('mim_voice_natural_preset') !== '0';
    let currentConversationLang = localStorage.getItem('mim_current_lang') || defaultListenLang;
    let activeVisualIdentity = '';
    let lastVisualIdentity = '';
    let interactionMemory = {};
    let greetingCooldownByIdentity = {};
    let lastSpokenSignature = localStorage.getItem('mim_last_spoken_signature') || '';
    let lastSpokenSignatureAt = Number(localStorage.getItem('mim_last_spoken_signature_at') || 0);
    let serverTtsEnabled = localStorage.getItem('mim_server_tts_enabled') !== '0';
    let selectedServerTtsVoice = localStorage.getItem('mim_server_tts_voice') || 'en-US-EmmaMultilingualNeural';
    let activeServerTtsAudio = null;
    let activeServerTtsUrl = '';
    let speechRequestSeq = 0;
    let speechInFlight = false;
    let speechPlaybackActive = false;
    let activeSpeechOwner = '';
    let micSuppressedUntil = 0;
    let recentSpokenUtterances = [];
    let localTtsPlaybackToken = 0;
    let lastSpokenPhraseCompact = '';
    let lastSpokenPhraseAt = 0;
    let refreshInFlight = false;
    let refreshPending = false;
    const runtimeHealthRecoveryCooldownUntil = {
      camera: 0,
      microphone: 0,
    };
    const runtimeRecoveryPending = {
      camera: null,
      microphone: null,
    };

    const SERVER_TTS_VOICES = [
      { value: 'en-US-EmmaMultilingualNeural', label: 'Emma (en-US, multilingual)' },
      { value: 'en-US-AvaMultilingualNeural', label: 'Ava (en-US, multilingual)' },
      { value: 'en-US-AriaNeural', label: 'Aria (en-US)' },
      { value: 'en-US-JennyNeural', label: 'Jenny (en-US)' },
      { value: 'en-GB-SoniaNeural', label: 'Sonia (en-GB)' },
      { value: 'es-ES-ElviraNeural', label: 'Elvira (es-ES)' },
      { value: 'fr-FR-DeniseNeural', label: 'Denise (fr-FR)' },
      { value: 'de-DE-SeraphinaMultilingualNeural', label: 'Seraphina (de-DE, multilingual)' },
      { value: 'it-IT-ElsaNeural', label: 'Elsa (it-IT)' },
      { value: 'pt-BR-FranciscaNeural', label: 'Francisca (pt-BR)' },
    ];

    try {
      interactionMemory = JSON.parse(localStorage.getItem('mim_identity_language_memory') || '{}') || {};
    } catch (_) {
      interactionMemory = {};
    }
    try {
      greetingCooldownByIdentity = JSON.parse(localStorage.getItem('mim_identity_greeting_cooldown') || '{}') || {};
    } catch (_) {
      greetingCooldownByIdentity = {};
    }

    const appliedVoiceMigration = localStorage.getItem('mim_voice_profile_migration') || '';
    if (appliedVoiceMigration !== VOICE_PROFILE_MIGRATION_VERSION) {
      voiceRate = 0.90;
      voicePitch = 0.90;
      voiceDepth = 22;
      voiceVolume = 0.95;
      naturalVoicePreset = true;
      localStorage.setItem('mim_voice_rate', String(voiceRate));
      localStorage.setItem('mim_voice_pitch', String(voicePitch));
      localStorage.setItem('mim_voice_depth', String(voiceDepth));
      localStorage.setItem('mim_voice_volume', String(voiceVolume));
      localStorage.setItem('mim_voice_natural_preset', '1');
      localStorage.setItem('mim_voice_profile_migration', VOICE_PROFILE_MIGRATION_VERSION);
    }

    for (let i = 0; i < 90; i += 1) {
      const bar = document.createElement('div');
      bar.className = 'bar';
      bar.style.height = `${40 + Math.abs(45 - i) * 1.6}px`;
      bar.style.animationDelay = `${(i % 12) * 0.07}s`;
      wave.appendChild(bar);
    }

    function setSpeaking(on) {
      wave.classList.toggle('speaking', !!on);
      statusEl.textContent = on ? 'MIM is speaking...' : 'MIM is listening...';
    }

    function clamp(value, min, max) {
      return Math.min(max, Math.max(min, value));
    }

    function chooseDialogVariant(options, key = '') {
      const variants = Array.isArray(options) ? options.filter(Boolean) : [];
      if (!variants.length) return '';
      const seedText = `${String(key || '')}|${Date.now()}`;
      let hash = 0;
      for (let i = 0; i < seedText.length; i += 1) {
        hash = ((hash << 5) - hash + seedText.charCodeAt(i)) | 0;
      }
      const index = Math.abs(hash) % variants.length;
      return String(variants[index]);
    }

    function normalizeDialogSnippet(raw, maxLen = 120) {
      const text = String(raw || '').replace(/\s+/g, ' ').trim();
      if (!text) return '';
      if (text.length <= maxLen) return text;
      return `${text.slice(0, maxLen - 3).trim()}...`;
    }

    function getConversationContext() {
      const context = latestUiState?.conversation_context;
      return context && typeof context === 'object' ? context : {};
    }

    function shouldAskForNameNow() {
      return Boolean(getConversationContext().needs_identity_prompt);
    }

    function buildContextLead() {
      const context = getConversationContext();
      const snippets = [];
      const environmentNow = normalizeDialogSnippet(context.environment_now, 90);
      const activeGoal = normalizeDialogSnippet(context.active_goal, 95);
      const openQuestion = normalizeDialogSnippet(context.open_question, 95);
      const memoryHint = normalizeDialogSnippet(context.memory_hint, 95);

      if (environmentNow) snippets.push(`Right now ${environmentNow}.`);
      if (activeGoal) snippets.push(`Current goal: ${activeGoal}.`);
      if (openQuestion) {
        snippets.push(`Open decision: ${openQuestion}.`);
      } else if (memoryHint) {
        snippets.push(`From memory: ${memoryHint}.`);
      }

      return snippets.join(' ').trim();
    }

    function escapeObjectMemoryHtml(value) {
      return String(value || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function objectMemorySortRank(details = {}) {
      if (details.recognized_person) return 0;
      if (details.observed) return details.uncertain ? 2 : 1;
      if (details.missing) return 3;
      return 4;
    }

    function collectObjectMemoryEntries(conversationContext = {}) {
      const detailsMap = (conversationContext && typeof conversationContext.camera_object_details === 'object')
        ? conversationContext.camera_object_details
        : {};
      const entriesByLabel = new Map();

      function upsertEntry(labelRaw, nextDetails = {}) {
        const label = String(labelRaw || '').trim();
        if (!label) return;
        const current = entriesByLabel.get(label) || {
          label,
          observed: false,
          uncertain: false,
          missing: false,
          recognized_person: false,
          note: '',
        };
        const merged = { ...current, ...nextDetails, label };
        if (!merged.note && typeof nextDetails.note === 'string') {
          merged.note = nextDetails.note;
        }
        entriesByLabel.set(label, merged);
      }

      Object.entries(detailsMap).forEach(([label, rawDetails]) => {
        const baseDetails = rawDetails && typeof rawDetails === 'object' ? rawDetails : {};
        upsertEntry(label, {
          observed: Boolean(baseDetails.seen_now),
          uncertain: Boolean(baseDetails.uncertain),
          missing: Boolean(baseDetails.missing),
          recognized_person: Boolean(baseDetails.recognized_person),
          note: String(baseDetails.detail || baseDetails.camera_detail || '').trim(),
        });
      });

      const recognizedPeople = Array.isArray(conversationContext.recognized_people)
        ? conversationContext.recognized_people
        : [];
      recognizedPeople.forEach((label) => {
        upsertEntry(label, {
          observed: true,
          recognized_person: true,
        });
      });

      const knownObjects = Array.isArray(conversationContext.known_camera_objects)
        ? conversationContext.known_camera_objects
        : [];
      knownObjects.forEach((label) => {
        upsertEntry(label, { observed: true });
      });

      const uncertainObjects = Array.isArray(conversationContext.uncertain_camera_objects)
        ? conversationContext.uncertain_camera_objects
        : [];
      uncertainObjects.forEach((label) => {
        upsertEntry(label, { observed: true, uncertain: true });
      });

      const missingObjects = Array.isArray(conversationContext.missing_camera_objects)
        ? conversationContext.missing_camera_objects
        : [];
      missingObjects.forEach((label) => {
        upsertEntry(label, { missing: true });
      });

      return Array.from(entriesByLabel.values())
        .sort((left, right) => {
          const rankDiff = objectMemorySortRank(left) - objectMemorySortRank(right);
          if (rankDiff !== 0) return rankDiff;
          return String(left.label || '').localeCompare(String(right.label || ''));
        });
    }

    function renderObjectMemoryPanel(conversationContext = {}) {
      if (!objectMemoryPanel || !objectMemoryList) return;

      const entries = collectObjectMemoryEntries(conversationContext);
      if (!entries.length) {
        objectMemoryPanel.hidden = true;
        objectMemoryList.innerHTML = '';
        return;
      }

      objectMemoryPanel.hidden = false;
      objectMemoryList.innerHTML = entries.map((entry) => {
        const label = escapeObjectMemoryHtml(entry.label);
        const stateBits = [];
        if (entry.recognized_person) stateBits.push('recognized person');
        if (entry.observed && entry.uncertain) {
          stateBits.push('visible now, uncertain');
        } else if (entry.observed) {
          stateBits.push('visible now');
        }
        if (entry.missing) stateBits.push('missing from current view');
        const meta = escapeObjectMemoryHtml(stateBits.join(' | ') || 'tracked in memory');
        const note = String(entry.note || '').trim();
        const noteHtml = note
          ? `<div class="object-memory-note">${escapeObjectMemoryHtml(note)}</div>`
          : '';
        return `
          <li class="object-memory-item">
            <strong>${label}</strong>
            <div class="object-memory-meta">${meta}</div>
            ${noteHtml}
          </li>
        `;
      }).join('');
    }

    function collectSystemReasoningEntries(reasoning = {}) {
      const entries = [];
      const goal = (reasoning && typeof reasoning.active_goal === 'object') ? reasoning.active_goal : {};
      const inquiry = (reasoning && typeof reasoning.inquiry === 'object') ? reasoning.inquiry : {};
      const governance = (reasoning && typeof reasoning.governance === 'object') ? reasoning.governance : {};
      const gatewayGovernance = (reasoning && typeof reasoning.gateway_governance === 'object') ? reasoning.gateway_governance : {};
      const autonomy = (reasoning && typeof reasoning.autonomy === 'object') ? reasoning.autonomy : {};
      const stewardship = (reasoning && typeof reasoning.stewardship === 'object') ? reasoning.stewardship : {};
      const recommendation = (reasoning && typeof reasoning.current_recommendation === 'object') ? reasoning.current_recommendation : {};
      const selfEvolution = (reasoning && typeof reasoning.self_evolution === 'object') ? reasoning.self_evolution : {};
      const trust = (reasoning && typeof reasoning.trust_explainability === 'object') ? reasoning.trust_explainability : {};
      const lightweightAutonomy = (reasoning && typeof reasoning.lightweight_autonomy === 'object') ? reasoning.lightweight_autonomy : {};
      const feedbackLoop = (reasoning && typeof reasoning.feedback_loop === 'object') ? reasoning.feedback_loop : {};
      const stabilityGuard = (reasoning && typeof reasoning.stability_guard === 'object') ? reasoning.stability_guard : {};

      if (String(goal.reasoning_summary || '').trim()) {
        entries.push({
          title: 'Active goal',
          meta: [goal.strategy_type, goal.priority].filter(Boolean).join(' | '),
          note: String(goal.reasoning_summary || '').trim(),
        });
      }

      if (String(inquiry.decision_state || inquiry.status || '').trim()) {
        const meta = [inquiry.decision_state || inquiry.status, inquiry.trigger_type].filter(Boolean).join(' | ');
        let note = String(inquiry.waiting_decision || inquiry.decision_reason || '').trim();
        if (!note && String(inquiry.managed_scope || '').trim()) {
          note = `Scope: ${String(inquiry.managed_scope || '').trim()}`;
        }
        entries.push({ title: 'Inquiry', meta, note });
      }

      if (String(governance.governance_decision || '').trim()) {
        const meta = [governance.governance_decision, governance.managed_scope && `scope ${governance.managed_scope}`]
          .filter(Boolean)
          .join(' | ');
        let note = String(governance.governance_reason || '').trim();
        if (!note && Number(governance.signal_count || 0) > 0) {
          note = `${Number(governance.signal_count || 0)} governance signals observed.`;
        }
        entries.push({ title: 'Governance', meta, note });
      }

      if (String(gatewayGovernance.primary_signal || gatewayGovernance.summary || '').trim()) {
        const signalCount = Number(gatewayGovernance.signal_count || 0);
        const meta = [
          gatewayGovernance.primary_signal,
          gatewayGovernance.system_health_status,
          signalCount > 1 ? `${signalCount} active signals` : '',
        ].filter(Boolean).join(' | ');
        const note = String(gatewayGovernance.summary || '').trim();
        entries.push({ title: 'Gateway governance', meta, note });
      }

      if (String(autonomy.current_level || '').trim()) {
        const meta = [autonomy.current_level, autonomy.scope && `scope ${autonomy.scope}`]
          .filter(Boolean)
          .join(' | ');
        entries.push({
          title: 'Autonomy',
          meta,
          note: String(autonomy.adaptation_summary || '').trim(),
        });
      }

      if (String(stewardship.managed_scope || stewardship.followup_status || '').trim()) {
        const bits = [];
        if (String(stewardship.managed_scope || '').trim()) bits.push(String(stewardship.managed_scope || '').trim());
        if (Number(stewardship.current_health || 0) > 0) bits.push(`health ${Number(stewardship.current_health || 0).toFixed(2)}`);
        let note = String(stewardship.last_decision_summary || '').trim();
        if (!note && String(stewardship.followup_status || '').trim()) {
          note = `Follow-up status: ${String(stewardship.followup_status || '').trim()}`;
        }
        entries.push({
          title: 'Stewardship',
          meta: bits.join(' | '),
          note,
        });
      }

      if (String(recommendation.summary || '').trim()) {
        const meta = [
          recommendation.source,
          recommendation.decision && String(recommendation.decision || '').trim().replaceAll('_', ' '),
          recommendation.managed_scope && `scope ${String(recommendation.managed_scope || '').trim()}`,
        ].filter(Boolean).join(' | ');
        entries.push({
          title: 'Current recommendation',
          meta,
          note: String(recommendation.summary || '').trim(),
        });
      }

      if (String(selfEvolution.summary || '').trim()) {
        const meta = [
          selfEvolution.decision_type && String(selfEvolution.decision_type || '').trim().replaceAll('_', ' '),
          selfEvolution.priority,
          selfEvolution.target_kind && `target ${String(selfEvolution.target_kind || '').trim().replaceAll('_', ' ')}`,
        ].filter(Boolean).join(' | ');
        const notes = [
          String(selfEvolution.summary || '').trim(),
          String(selfEvolution.target_summary || '').trim() && `Target: ${String(selfEvolution.target_summary || '').trim()}`,
          String(selfEvolution.action_summary || '').trim() && `Next: ${String(selfEvolution.action_summary || '').trim()}`,
          String(selfEvolution.operator_command_summary || '').trim() && `Command: ${String(selfEvolution.operator_command_summary || '').trim()}`,
        ].filter(Boolean).join('. ');
        entries.push({
          title: 'Self-evolution',
          meta,
          note: notes,
        });
      }

      if (
        String(trust.what_it_did || '').trim()
        || String(trust.what_it_will_do_next || '').trim()
        || String(trust.confidence_reasoning || '').trim()
      ) {
        const trustMeta = [
          trust.confidence_tier,
          Number.isFinite(Number(trust.confidence)) ? `confidence ${Number(trust.confidence).toFixed(2)}` : '',
          trust.operator_review_required ? 'operator review required' : (trust.safe_to_continue ? 'safe to continue' : ''),
        ].filter(Boolean).join(' | ');
        const trustNotes = [
          String(trust.what_it_did || '').trim() && `Did: ${String(trust.what_it_did || '').trim()}`,
          String(trust.what_it_will_do_next || '').trim() && `Next: ${String(trust.what_it_will_do_next || '').trim()}`,
          String(trust.confidence_reasoning || '').trim() && `Why: ${String(trust.confidence_reasoning || '').trim()}`,
          String(trust.stop_reason || '').trim() && `Stop reason: ${String(trust.stop_reason || '').trim()}`,
        ].filter(Boolean).join('. ');
        entries.push({
          title: 'Trust signals',
          meta: trustMeta,
          note: trustNotes,
        });
      }

      if (String(lightweightAutonomy.summary || '').trim()) {
        const meta = [
          lightweightAutonomy.current_level,
          lightweightAutonomy.automatic_ready ? 'bounded auto-ready' : 'operator-held',
          lightweightAutonomy.managed_scope && `scope ${String(lightweightAutonomy.managed_scope || '').trim()}`,
        ].filter(Boolean).join(' | ');
        entries.push({
          title: 'Lightweight autonomy',
          meta,
          note: String(lightweightAutonomy.summary || '').trim(),
        });
      }

      if (String(feedbackLoop.summary || '').trim()) {
        const meta = [
          feedbackLoop.latest_actor,
          feedbackLoop.latest_status,
          Number(feedbackLoop.history_count || 0) > 0 ? `${Number(feedbackLoop.history_count || 0)} feedback updates` : '',
        ].filter(Boolean).join(' | ');
        entries.push({
          title: 'Human feedback loop',
          meta,
          note: String(feedbackLoop.summary || '').trim(),
        });
      }

      if (String(stabilityGuard.summary || '').trim()) {
        const meta = [
          stabilityGuard.active ? 'active guard' : 'guard clear',
          Array.isArray(stabilityGuard.blocking_conditions) && stabilityGuard.blocking_conditions.length > 0
            ? `${stabilityGuard.blocking_conditions.length} blockers`
            : '',
        ].filter(Boolean).join(' | ');
        entries.push({
          title: 'Stability guard',
          meta,
          note: String(stabilityGuard.summary || '').trim(),
        });
      }

      const collaboration = (reasoning && typeof reasoning.collaboration_progress === 'object') ? reasoning.collaboration_progress : {};
      if (String(collaboration.summary || '').trim()) {
        const activeWorkstream = (collaboration.active_workstream && typeof collaboration.active_workstream === 'object')
          ? collaboration.active_workstream
          : {};
        const meta = [
          collaboration.execution_id_label || collaboration.execution_id,
          collaboration.execution_lane && String(collaboration.execution_lane || '').trim().replaceAll('_', ' '),
          activeWorkstream.name && String(activeWorkstream.name || '').trim().replaceAll('_', ' '),
          activeWorkstream.tod_status && String(activeWorkstream.tod_status || '').trim().replaceAll('_', ' '),
        ].filter(Boolean).join(' | ');
        let note = String(activeWorkstream.latest_observation || '').trim();
        if (!note) {
          note = String(collaboration.summary || '').trim();
        }
        entries.push({
          title: 'TOD collaboration',
          meta,
          note,
        });
      }

      const todDecision = (reasoning && typeof reasoning.tod_decision_process === 'object') ? reasoning.tod_decision_process : {};
      if (String(todDecision.summary || '').trim()) {
        const todKnows = (todDecision.tod_knows_what_mim_did && typeof todDecision.tod_knows_what_mim_did === 'object')
          ? todDecision.tod_knows_what_mim_did
          : {};
        const mimKnows = (todDecision.mim_knows_what_tod_did && typeof todDecision.mim_knows_what_tod_did === 'object')
          ? todDecision.mim_knows_what_tod_did
          : {};
        const todWork = (todDecision.tod_current_work && typeof todDecision.tod_current_work === 'object')
          ? todDecision.tod_current_work
          : {};
        const todLiveness = (todDecision.tod_liveness && typeof todDecision.tod_liveness === 'object')
          ? todDecision.tod_liveness
          : {};
        const escalation = (todDecision.communication_escalation && typeof todDecision.communication_escalation === 'object')
          ? todDecision.communication_escalation
          : {};
        const meta = [
          todDecision.state && String(todDecision.state || '').trim().replaceAll('_', ' '),
          todLiveness.status && `liveness ${String(todLiveness.status || '').trim().replaceAll('_', ' ')}`,
          escalation.required_cycle_count ? `cycles ${Number(escalation.required_cycle_count)}` : '',
          escalation.required ? 'escalation required' : 'no escalation',
        ].filter(Boolean).join(' | ');
        const noteParts = [
          `TOD ${todKnows.known ? 'knows' : 'does not know'} what MIM did`,
          `MIM ${mimKnows.known ? 'knows' : 'does not know'} what TOD did`,
          todWork.phase ? `TOD work: ${String(todWork.phase || '').trim().replaceAll('_', ' ')}` : '',
          escalation.code ? `Escalation: ${String(escalation.code || '').trim().replaceAll('_', ' ')}` : '',
          escalation.block_dispatch_threshold_cycles ? `Dispatch block threshold: ${Number(escalation.block_dispatch_threshold_cycles)} cycles` : '',
        ].filter(Boolean).join('. ');
        entries.push({
          title: 'TOD decision process',
          meta,
          note: noteParts || String(todDecision.summary || '').trim(),
        });
      }

      const runtimeRecovery = (reasoning && typeof reasoning.runtime_recovery === 'object') ? reasoning.runtime_recovery : {};
      if (String(runtimeRecovery.summary || '').trim()) {
        const recoveryLanes = (runtimeRecovery.lanes && typeof runtimeRecovery.lanes === 'object')
          ? Object.values(runtimeRecovery.lanes)
          : [];
        const unstableCount = recoveryLanes.filter((item) => item && item.unstable).length;
        const cooldownCount = recoveryLanes.filter((item) => item && item.cooldown_active).length;
        const meta = [
          runtimeRecovery.status,
          unstableCount > 0 ? `${unstableCount} unstable lane${unstableCount === 1 ? '' : 's'}` : '',
          cooldownCount > 0 ? `${cooldownCount} cooldown active` : '',
        ].filter(Boolean).join(' | ');
        entries.push({
          title: 'Runtime recovery',
          meta,
          note: String(runtimeRecovery.summary || '').trim(),
        });
      }

      return entries;
    }

    function renderSystemReasoningPanel(reasoning = {}) {
      if (!systemReasoningPanel || !systemReasoningSummary || !systemReasoningList) return;

      const summary = String((reasoning && reasoning.summary) || '').trim();
      const entries = collectSystemReasoningEntries(reasoning);
      if (!summary && !entries.length) {
        systemReasoningPanel.hidden = true;
        systemReasoningSummary.textContent = '';
        systemReasoningList.innerHTML = '';
        return;
      }

      systemReasoningPanel.hidden = false;
      systemReasoningSummary.textContent = summary;
      systemReasoningList.innerHTML = entries.map((entry) => {
        const title = escapeObjectMemoryHtml(entry.title);
        const meta = escapeObjectMemoryHtml(String(entry.meta || '').trim() || 'latest reasoning');
        const note = String(entry.note || '').trim();
        const noteHtml = note
          ? `<div class="object-memory-note">${escapeObjectMemoryHtml(note)}</div>`
          : '';
        return `
          <li class="object-memory-item">
            <strong>${title}</strong>
            <div class="object-memory-meta">${meta}</div>
            ${noteHtml}
          </li>
        `;
      }).join('');
    }

    async function postRuntimeRecoveryEvent(lane, eventType, detail = '', nextRetryAt = null, metadata = {}) {
      try {
        await fetch('/mim/ui/runtime-recovery-events', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            lane,
            event_type: eventType,
            detail,
            next_retry_at: nextRetryAt,
            metadata: metadata && typeof metadata === 'object' ? metadata : {},
          }),
        });
      } catch (_) {
      }
    }

    function isIdentityInquiryText(textRaw) {
      const text = String(textRaw || '').toLowerCase();
      if (!text.trim()) return false;
      return text.includes('what should i call you')
        || text.includes("what's your name")
        || text.includes('tell me your name');
    }

    function normalizeSpeechSignature(textRaw) {
      return String(textRaw || '')
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
    }

    function shortSpeechSignature(textRaw) {
      const signature = normalizeSpeechSignature(textRaw);
      if (!signature) return '-';
      let hash = 2166136261;
      for (let i = 0; i < signature.length; i += 1) {
        hash ^= signature.charCodeAt(i);
        hash = Math.imul(hash, 16777619);
      }
      return `h${(hash >>> 0).toString(16).padStart(8, '0')}:${signature.slice(0, 24)}`;
    }

    function suppressionWindowMs() {
      return Math.max(0, micSuppressedUntil - Date.now());
    }

    function addSpeechDebug(stage, detail = '') {
      const detailText = String(detail || '').trim();
      addMicDebug(`speech:${stage}`, detailText);
      try {
        console.debug(`[mim:speech] ${stage}${detailText ? ` ${detailText}` : ''}`);
      } catch (_) {
      }
    }

    function logTranscriptDrop(reason, transcript, mode = 'unknown', detail = '') {
      const preview = String(transcript || '').slice(0, 48);
      const signature = shortSpeechSignature(transcript);
      const suffix = detail ? ` ${detail}` : '';
      addMicDebug(
        'transcript-drop',
        `reason=${reason} mode=${mode} sig=${signature} token=${localTtsPlaybackToken} suppressMs=${suppressionWindowMs()} text=${preview}${suffix}`,
      );
    }

    function setMicSuppression(durationMs, reason = '') {
      const until = Date.now() + Math.max(0, Number(durationMs) || 0);
      if (until > micSuppressedUntil) {
        micSuppressedUntil = until;
      }
      if (reason) {
        addMicDebug('mic-suppress', `${reason} until=${micSuppressedUntil}`);
      }
    }

    function isMicSuppressedNow() {
      return speechInFlight || speechPlaybackActive || Date.now() < micSuppressedUntil;
    }

    function rememberSpokenUtterance(text, sourceTag = 'unknown') {
      const signature = normalizeSpeechSignature(text);
      if (!signature || signature.length < MIC_ECHO_MIN_SIGNATURE_LEN) return;
      recentSpokenUtterances.push({ signature, sourceTag, at: Date.now() });
      if (recentSpokenUtterances.length > 14) {
        recentSpokenUtterances = recentSpokenUtterances.slice(-14);
      }
    }

    function isLikelyEchoTranscript(transcript) {
      const signature = normalizeSpeechSignature(transcript);
      if (!signature || signature.length < MIC_ECHO_MIN_SIGNATURE_LEN) return false;
      const now = Date.now();
      recentSpokenUtterances = recentSpokenUtterances.filter((item) => (now - Number(item.at || 0)) <= MIC_ECHO_MATCH_WINDOW_MS);
      return recentSpokenUtterances.some((item) => {
        if (!item || !item.signature) return false;
        if (item.signature === signature) return true;
        return item.signature.includes(signature) || signature.includes(item.signature);
      });
    }

    function hasWakePhrase(transcript) {
      const text = ` ${String(transcript || '').toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim()} `;
      if (!text.trim()) return false;
      return text.includes(' mim ') || text.includes(' hey mim ') || text.includes(' okay mim ') || text.includes(' ok mim ');
    }

    function shouldSpeakBackendInquiryPrompt(inquiryPrompt, conversationContext = {}) {
      const prompt = String(inquiryPrompt || '').trim();
      if (!prompt) return false;

      const now = Date.now();
      const signature = normalizeSpeechSignature(prompt);
      const needsIdentityPrompt = Boolean(conversationContext?.needs_identity_prompt);
      const hasOpenQuestion = Boolean(String(conversationContext?.open_question || '').trim());

      if (needsIdentityPrompt || hasOpenQuestion) {
        backendInquirySpeakCooldownUntil = now + 6000;
        lastBackendInquirySignature = signature;
        return true;
      }

      if (signature && signature === lastBackendInquirySignature && now < backendInquirySpeakCooldownUntil) {
        return false;
      }

      if (now < backendInquirySpeakCooldownUntil) {
        return false;
      }

      backendInquirySpeakCooldownUntil = now + BACKEND_INQUIRY_SPEAK_COOLDOWN_MS;
      lastBackendInquirySignature = signature;
      return true;
    }

    function rewriteQueuedOutputText(textRaw, data = {}) {
      let text = String(textRaw || '').replace(/\s+/g, ' ').trim();
      if (!text) return '';

      const context = (data && typeof data.conversation_context === 'object')
        ? data.conversation_context
        : getConversationContext();
      const needsIdentityPrompt = Boolean(context?.needs_identity_prompt);
      const openQuestion = normalizeDialogSnippet(context?.open_question || '', 140);
      const activeGoal = normalizeDialogSnippet(context?.active_goal || '', 140);

      // Drop stale identity asks when context says identity is no longer required.
      if (!needsIdentityPrompt && isIdentityInquiryText(text)) {
        if (openQuestion) {
          return `Before I proceed, I need one decision: ${openQuestion}`;
        }
        if (activeGoal) {
          return `I am tracking this goal: ${activeGoal}. Tell me what you want me to do next.`;
        }
        return '';
      }

      text = text
        .replace(/^i\s+can\s+see\s+someone\.\s*/i, '')
        .replace(/^hi\s+there[,\s]*/i, '');

      const signature = normalizeSpeechSignature(text);
      const cannedAckOnly = new Set([
        'ok', 'okay', 'got it', 'understood', 'noted', 'thanks', 'thank you',
        'all right', 'alright', 'copy that', 'hello i am mim', 'hello i am mim.',
      ]);
      if (cannedAckOnly.has(signature)) {
        return '';
      }

      return text;
    }

    function buildDialogPrompt(kind, context = {}) {
      const name = String(context?.name || '').trim();
      const transcript = String(context?.transcript || '').trim();
      const contextLead = buildContextLead();
      const askForName = shouldAskForNameNow();
      if (kind === 'startup_identity') {
        if (askForName) {
          return chooseDialogVariant([
            `${contextLead ? `${contextLead} ` : ''}Hi there. What should I call you?`,
            `${contextLead ? `${contextLead} ` : ''}I can continue right away, and I only need the name you prefer.`,
            `${contextLead ? `${contextLead} ` : ''}Before we continue, what name do you want me to use?`,
          ], transcript || contextLead || 'startup-name');
        }
        return chooseDialogVariant([
          `${contextLead ? `${contextLead} ` : ''}I am listening. What do you want to work on right now?`,
          `${contextLead ? `${contextLead} ` : ''}I am here with full context. Tell me the next thing you want to do.`,
          `${contextLead ? `${contextLead} ` : ''}We can continue from where we are. What should I do now?`,
        ], transcript || contextLead || 'startup-open');
      }
      if (kind === 'low_value') {
        if (askForName) {
          return chooseDialogVariant([
            'I only caught part of that. Please say just your name once.',
            'I heard fragments. Please tell me the name you want me to use.',
            'I missed part of that. Could you repeat your name clearly?',
          ], transcript || contextLead || 'low-value-name');
        }
        return chooseDialogVariant([
          'I only caught part of that. Say your request again in one sentence.',
          'I heard fragments. Please repeat what you want me to do now.',
          'I am missing part of your intent. Tell me the next action clearly.',
        ], transcript || contextLead || 'low-value-action');
      }
      if (kind === 'greeting_only') {
        if (askForName) {
          return chooseDialogVariant([
            `${contextLead ? `${contextLead} ` : ''}Hi. What should I call you?`,
            `${contextLead ? `${contextLead} ` : ''}Hello. Share the name you want me to use and we can continue.`,
            `${contextLead ? `${contextLead} ` : ''}Hey. I am ready. What name should I address you by?`,
          ], transcript || contextLead || 'greeting-name');
        }
        return chooseDialogVariant([
          `${contextLead ? `${contextLead} ` : ''}Hi. What do you want to do next?`,
          `${contextLead ? `${contextLead} ` : ''}Hello. Tell me your next request and I will act on it.`,
          `${contextLead ? `${contextLead} ` : ''}Hey. I am ready for the next step.`,
        ], transcript || contextLead || 'greeting-action');
      }
      if (kind === 'uncertain_name') {
        if (!askForName) {
          return chooseDialogVariant([
            `${contextLead ? `${contextLead} ` : ''}I heard you, but I am not certain about the request. Say the next action in one clear sentence.`,
            `${contextLead ? `${contextLead} ` : ''}I may have misheard. Please restate exactly what you want me to do now.`,
            `${contextLead ? `${contextLead} ` : ''}I am uncertain about your intent. Give me one concise instruction.`,
          ], transcript || contextLead || 'uncertain-action');
        }
        return chooseDialogVariant([
          `${contextLead ? `${contextLead} ` : ''}I heard you, but I am not fully sure about the name. Please say only your name once.`,
          `${contextLead ? `${contextLead} ` : ''}I may have misheard the name. Please say just your name clearly.`,
          `${contextLead ? `${contextLead} ` : ''}I am uncertain about the name. Please repeat only your name, one word if possible.`,
        ], transcript || contextLead || 'uncertain-name');
      }
      if (kind === 'identity_ack') {
        return chooseDialogVariant([
          `${contextLead ? `${contextLead} ` : ''}Nice to meet you, ${name}. What should we tackle first?`,
          `${contextLead ? `${contextLead} ` : ''}Great to meet you, ${name}. What is the next step you want?`,
          `${contextLead ? `${contextLead} ` : ''}Thanks, ${name}. I am ready when you are.`,
        ], name || transcript || contextLead || 'identity-ack');
      }
      return '';
    }

    function stopMicPermissionStream() {
      stopMicKeepAliveMonitor();
      if (!micPermissionStream) return;
      try {
        for (const track of micPermissionStream.getTracks()) {
          try {
            track.stop();
          } catch (_) {
          }
        }
      } catch (_) {
      }
      micPermissionStream = null;
    }

    function stopMicKeepAliveMonitor() {
      if (micKeepAliveRecorder) {
        try {
          if (micKeepAliveRecorder.state !== 'inactive') {
            micKeepAliveRecorder.stop();
          }
        } catch (_) {
        }
        micKeepAliveRecorder.ondataavailable = null;
        micKeepAliveRecorder.onerror = null;
      }
      if (micKeepAliveProcessorNode) {
        try {
          micKeepAliveProcessorNode.disconnect();
        } catch (_) {
        }
        micKeepAliveProcessorNode.onaudioprocess = null;
      }
      if (micKeepAliveSourceNode) {
        try {
          micKeepAliveSourceNode.disconnect();
        } catch (_) {
        }
      }
      if (micKeepAliveGainNode) {
        try {
          micKeepAliveGainNode.disconnect();
        } catch (_) {
        }
      }
      if (micKeepAliveAudioContext) {
        try {
          micKeepAliveAudioContext.close();
        } catch (_) {
        }
      }
      micKeepAliveAudioContext = null;
      micKeepAliveSourceNode = null;
      micKeepAliveGainNode = null;
      micKeepAliveProcessorNode = null;
      micKeepAliveRecorder = null;
    }

    function startMicKeepAliveMonitor() {
      if (!micPermissionStream || !micPermissionStream.active) return;
      if (micKeepAliveRecorder && micKeepAliveRecorder.state !== 'inactive') return;
      if (micKeepAliveAudioContext && micKeepAliveSourceNode) return;

      // Prefer MediaRecorder keepalive because desktop audio stacks treat it as
      // an explicit ongoing capture session and keep mic indicators lit.
      if (typeof window.MediaRecorder === 'function') {
        try {
          micKeepAliveRecorder = new MediaRecorder(micPermissionStream, { mimeType: 'audio/webm;codecs=opus' });
          micKeepAliveRecorder.ondataavailable = () => {};
          micKeepAliveRecorder.onerror = (event) => {
            addMicDebug('keepalive-recorder-error', String(event?.error?.message || event?.message || 'unknown'));
          };
          micKeepAliveRecorder.start(2000);
          addMicDebug('keepalive', 'recorder-active');
          return;
        } catch (_) {
          // Fall back to AudioContext pipeline below.
          micKeepAliveRecorder = null;
        }
      }

      const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextCtor) {
        addMicDebug('keepalive', 'AudioContext unavailable');
        return;
      }

      try {
        micKeepAliveAudioContext = new AudioContextCtor();
        micKeepAliveSourceNode = micKeepAliveAudioContext.createMediaStreamSource(micPermissionStream);
        micKeepAliveProcessorNode = micKeepAliveAudioContext.createScriptProcessor(1024, 1, 1);
        micKeepAliveProcessorNode.onaudioprocess = () => {};
        micKeepAliveGainNode = micKeepAliveAudioContext.createGain();
        // Keep the stream active while producing effectively silent output.
        micKeepAliveGainNode.gain.value = 0.00001;
        micKeepAliveSourceNode.connect(micKeepAliveProcessorNode);
        micKeepAliveProcessorNode.connect(micKeepAliveGainNode);
        micKeepAliveGainNode.connect(micKeepAliveAudioContext.destination);
        if (micKeepAliveAudioContext.state === 'suspended') {
          micKeepAliveAudioContext.resume().catch(() => {});
        }
        addMicDebug('keepalive', 'active');
      } catch (error) {
        addMicDebug('keepalive-error', String(error?.message || 'failed'));
        stopMicKeepAliveMonitor();
      }
    }

    function addMicDebug(label, detail = '') {
      const time = new Date().toLocaleTimeString();
      const detailText = String(detail || '').trim();
      const line = detailText ? `[${time}] ${label} :: ${detailText}` : `[${time}] ${label}`;
      micDebugLines.push(line);
      if (micDebugLines.length > 10) {
        micDebugLines = micDebugLines.slice(-10);
      }
      if (micDebugEl) {
        const lineBreak = String.fromCharCode(10);
        micDebugEl.textContent = `Mic debug:${lineBreak}${micDebugLines.join(lineBreak)}`;
      }
    }

    function setSettingsTab(tabName) {
      const isCamera = tabName === 'camera';
      settingsTabVoice.classList.toggle('active', !isCamera);
      settingsTabCamera.classList.toggle('active', isCamera);
      settingsViewVoice.classList.toggle('active', !isCamera);
      settingsViewCamera.classList.toggle('active', isCamera);
    }

    function updateCameraSettingsUi() {
      const active = Boolean(cameraStream && cameraStream.active);
      cameraToggleBtn.textContent = active ? 'Stop Camera Preview' : 'Start Camera Preview';
      cameraPreview.classList.toggle('inactive', !active);
      if (active) {
        cameraSettingsStatus.textContent = 'Camera preview is live.';
      } else if (!cameraSettingsStatus.textContent.trim()) {
        cameraSettingsStatus.textContent = 'Camera preview is idle.';
      }
    }

    function syncVoiceControlAvailability() {
      const manualMode = !naturalVoicePreset;
      voiceRateInput.disabled = !manualMode;
      voicePitchInput.disabled = !manualMode;
      voiceDepthInput.disabled = !manualMode;
      voiceVolumeInput.disabled = !manualMode;
      serverTtsVoiceSelect.disabled = !serverTtsEnabled;
    }

    function buildServerTtsVoiceOptions() {
      serverTtsVoiceSelect.innerHTML = '';
      for (const voice of SERVER_TTS_VOICES) {
        const option = document.createElement('option');
        option.value = voice.value;
        option.textContent = voice.label;
        serverTtsVoiceSelect.appendChild(option);
      }

      const hasSelected = SERVER_TTS_VOICES.some((voice) => voice.value === selectedServerTtsVoice);
      if (!hasSelected) {
        selectedServerTtsVoice = 'en-US-EmmaMultilingualNeural';
      }
      serverTtsVoiceSelect.value = selectedServerTtsVoice;
    }

    function clearMicFallbackTimer() {
      if (micFallbackNoSpeechTimer) {
        clearTimeout(micFallbackNoSpeechTimer);
        micFallbackNoSpeechTimer = null;
      }
    }

    function stopMicFallbackLoop() {
      clearMicFallbackTimer();
      if (micFallbackInterval) {
        clearInterval(micFallbackInterval);
        micFallbackInterval = null;
      }
    }

    function startMicFallbackLoop() {
      stopMicFallbackLoop();
      micFallbackNoSpeechTimer = setTimeout(() => {
        if (!micAutoMode) return;
        noteMicEvent('fallback', 'scheduled-start');
        captureFallbackTranscription();
      }, 900);
      micFallbackInterval = setInterval(() => {
        if (!micAutoMode) return;
        captureFallbackTranscription();
      }, MIC_FALLBACK_INTERVAL_MS);
    }

    function writeAscii(view, offset, value) {
      for (let index = 0; index < value.length; index += 1) {
        view.setUint8(offset + index, value.charCodeAt(index));
      }
    }

    function downsampleChunksToRate(floatChunks, sourceRate, targetRate) {
      if (!Array.isArray(floatChunks) || !floatChunks.length) return [];

      const source = [];
      for (const chunk of floatChunks) {
        for (let index = 0; index < chunk.length; index += 1) {
          source.push(chunk[index]);
        }
      }

      const safeSourceRate = Math.max(8000, Math.round(Number(sourceRate || 16000)));
      const safeTargetRate = Math.max(8000, Math.round(Number(targetRate || 16000)));
      if (safeTargetRate >= safeSourceRate) {
        return [new Float32Array(source)];
      }

      const ratio = safeSourceRate / safeTargetRate;
      const outputLength = Math.max(1, Math.floor(source.length / ratio));
      const output = new Float32Array(outputLength);

      let outputIndex = 0;
      let inputIndex = 0;
      while (outputIndex < outputLength) {
        const nextInputIndex = Math.min(source.length, Math.floor((outputIndex + 1) * ratio));
        let sum = 0;
        let count = 0;
        for (let idx = inputIndex; idx < nextInputIndex; idx += 1) {
          sum += source[idx];
          count += 1;
        }
        output[outputIndex] = count > 0 ? sum / count : source[Math.min(inputIndex, source.length - 1)] || 0;
        outputIndex += 1;
        inputIndex = nextInputIndex;
      }

      return [output];
    }

    function getMicTranscribeProvider() {
      if (Date.now() < micProviderLocalBackoffUntil) {
        return 'local';
      }
      return 'auto';
    }

    function encodeWavBlob(floatChunks, sampleRate) {
      let totalSamples = 0;
      for (const chunk of floatChunks) {
        totalSamples += chunk.length;
      }
      const pcmBuffer = new ArrayBuffer(44 + totalSamples * 2);
      const view = new DataView(pcmBuffer);

      writeAscii(view, 0, 'RIFF');
      view.setUint32(4, 36 + totalSamples * 2, true);
      writeAscii(view, 8, 'WAVE');
      writeAscii(view, 12, 'fmt ');
      view.setUint32(16, 16, true);
      view.setUint16(20, 1, true);
      view.setUint16(22, 1, true);
      view.setUint32(24, sampleRate, true);
      view.setUint32(28, sampleRate * 2, true);
      view.setUint16(32, 2, true);
      view.setUint16(34, 16, true);
      writeAscii(view, 36, 'data');
      view.setUint32(40, totalSamples * 2, true);

      let offset = 44;
      for (const chunk of floatChunks) {
        for (let index = 0; index < chunk.length; index += 1) {
          const sample = Math.max(-1, Math.min(1, chunk[index]));
          const value = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
          view.setInt16(offset, value, true);
          offset += 2;
        }
      }
      return new Blob([view], { type: 'audio/wav' });
    }

    function blobToBase64(blob) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onloadend = () => {
          const text = String(reader.result || '');
          const base64 = text.includes(',') ? text.split(',')[1] : text;
          resolve(base64);
        };
        reader.onerror = () => reject(reader.error || new Error('read_failed'));
        reader.readAsDataURL(blob);
      });
    }

    async function fetchWithTimeout(url, options = {}, timeoutMs = 12000) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), Math.max(1000, Number(timeoutMs) || 12000));
      try {
        return await fetch(url, {
          ...options,
          signal: controller.signal,
        });
      } finally {
        clearTimeout(timer);
      }
    }

    function extractFirstUrl(rawText) {
      const text = String(rawText || '');
      const match = text.match(/https?:\/\/[^\s)]+/i);
      return match ? String(match[0]).trim() : '';
    }

    async function handleWebSummaryCommand(url, sourceMode = 'ui') {
      const targetUrl = String(url || '').trim();
      if (!targetUrl) return false;

      inquiryEl.textContent = `Summarizing website: ${targetUrl}`;
      statusEl.textContent = `Fetching website summary (${sourceMode})...`;

      try {
        const res = await fetchWithTimeout('/gateway/web/summarize', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            url: targetUrl,
            timeout_seconds: 12,
            max_summary_sentences: 4,
          }),
        }, 16000);

        let payload = {};
        try {
          payload = await res.json();
        } catch (_) {
          payload = {};
        }

        if (!res.ok) {
          const detail = String(payload?.detail || '').trim();
          let message = `I could not summarize that website (${detail || `http_${res.status}`}).`;
          if (detail.includes('web_access_disabled')) {
            message = 'Web access is currently disabled. Set ALLOW_WEB_ACCESS=true to enable website summaries.';
          }
          inquiryEl.textContent = message;
          statusEl.textContent = message;
          await speakLocally(message, true, `web_summary_error:${sourceMode}`);
          addMicDebug('web-summary-failed', `mode=${sourceMode} detail=${detail || `http_${res.status}`}`);
          return true;
        }

        const title = String(payload?.title || '').trim();
        const summary = String(payload?.summary || '').trim();
        const spoken = summary || 'I fetched the page, but there was no useful summary text.';
        const display = title ? `Web summary (${title}): ${spoken}` : `Web summary: ${spoken}`;
        inquiryEl.textContent = display;
        statusEl.textContent = `Website summarized (${sourceMode}).`;
        await speakLocally(display, true, `web_summary_result:${sourceMode}`);
        addMicDebug('web-summary-ok', `mode=${sourceMode} url=${targetUrl}`);
        return true;
      } catch (error) {
        const message = `I could not summarize that website right now (${String(error?.message || 'network_error')}).`;
        inquiryEl.textContent = message;
        statusEl.textContent = message;
        await speakLocally(message, true, `web_summary_exception:${sourceMode}`);
        addMicDebug('web-summary-error', `mode=${sourceMode} error=${String(error?.message || 'unknown')}`);
        return true;
      }
    }

    async function handleCapabilitiesCommand(sourceMode = 'ui') {
      try {
        const res = await fetchWithTimeout('/manifest', {}, 8000);
        if (!res.ok) {
          const message = `I could not read my manifest right now (http_${res.status}).`;
          inquiryEl.textContent = message;
          statusEl.textContent = message;
          await speakLocally(message, true, `capabilities_error:${sourceMode}`);
          return true;
        }
        const manifest = await res.json();
        const capabilities = Array.isArray(manifest?.capabilities) ? manifest.capabilities : [];
        const hasWebSummary = capabilities.includes('web_page_summarization');
        const message = hasWebSummary
          ? `I currently expose ${capabilities.length} capabilities. Web page summarization is available through gateway web summarize.`
          : `I currently expose ${capabilities.length} capabilities. You can inspect them through the manifest endpoint.`;
        inquiryEl.textContent = message;
        statusEl.textContent = `Capabilities summary ready (${sourceMode}).`;
        await speakLocally(message, true, `capabilities_result:${sourceMode}`);
        addMicDebug('capabilities-summary', `mode=${sourceMode} count=${capabilities.length}`);
        return true;
      } catch (error) {
        const message = `I could not retrieve capability details right now (${String(error?.message || 'network_error')}).`;
        inquiryEl.textContent = message;
        statusEl.textContent = message;
        await speakLocally(message, true, `capabilities_exception:${sourceMode}`);
        addMicDebug('capabilities-summary-error', `mode=${sourceMode} error=${String(error?.message || 'unknown')}`);
        return true;
      }
    }

    async function maybeHandleWebOrCapabilityCommand(transcript, sourceMode = 'ui') {
      const text = String(transcript || '').trim();
      if (!text) return false;

      const lowered = text.toLowerCase();
      const askedWebsiteSummary =
        lowered.includes('summarize this website')
        || lowered.includes('summary of this website')
        || lowered.includes('summarize this url')
        || lowered.includes('summarize this site')
        || (lowered.includes('summarize') && lowered.includes('http'));

      if (askedWebsiteSummary) {
        const url = extractFirstUrl(text);
        if (!url) {
          const prompt = 'Please include a full http or https URL so I can summarize the website.';
          inquiryEl.textContent = prompt;
          statusEl.textContent = prompt;
          await speakLocally(prompt, true, `web_summary_prompt:${sourceMode}`);
          return true;
        }
        return await handleWebSummaryCommand(url, sourceMode);
      }

      const askedCapabilities =
        lowered.includes('capabilities')
        || lowered.includes('what can you do')
        || lowered.includes('access the capabilities')
        || lowered.includes('application capabilities');

      if (askedCapabilities) {
        return await handleCapabilitiesCommand(sourceMode);
      }

      return false;
    }

    async function captureFallbackTranscription() {
      if (micFallbackCaptureInFlight) return;
      if (isMicSuppressedNow()) {
        noteMicEvent('fallback-suppressed', 'tts-active');
        return;
      }
      micFallbackCaptureInFlight = true;
      clearMicFallbackTimer();
      const captureStartedAt = Date.now();
      addMicDebug('fallback:start', `lang=${defaultListenLang}`);

      try {
        let stream = null;
        let ownsStream = false;

        if (micPermissionStream && micPermissionStream.active) {
          stream = micPermissionStream;
          addMicDebug('fallback:getUserMedia', 'reuse-shared-stream');
        } else {
          addMicDebug('fallback:getUserMedia', `new-stream required active=${Boolean(micPermissionStream && micPermissionStream.active)}`);
          const preferredMic = resolvePreferredMicDevice();
          const fallbackAudioConstraints = {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          };
          if (preferredMic?.deviceId && preferredMic.deviceId !== 'default' && preferredMic.deviceId !== 'communications') {
            fallbackAudioConstraints.deviceId = { exact: preferredMic.deviceId };
          }

          stream = await navigator.mediaDevices.getUserMedia({
            audio: fallbackAudioConstraints,
            video: false,
          });
          ownsStream = true;
          addMicDebug('fallback:getUserMedia', 'ok');
        }

        const activeTrackCount = (stream && typeof stream.getAudioTracks === 'function')
          ? stream.getAudioTracks().filter((track) => track.readyState === 'live').length
          : 0;
        addMicDebug('fallback:stream-state', `owns=${ownsStream} activeTracks=${activeTrackCount}`);

        const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextCtor) {
          noteMicEvent('fallback-error', 'AudioContext unavailable');
          if (stream && ownsStream) {
            for (const track of stream.getTracks()) {
              track.stop();
            }
          }
          micFallbackCaptureInFlight = false;
          return;
        }

        const audioContext = new AudioContextCtor();
        const fallbackSampleRate = Math.max(8000, Math.round(Number(audioContext.sampleRate || 16000)));
        const sourceNode = audioContext.createMediaStreamSource(stream);
        const processorNode = audioContext.createScriptProcessor(4096, 1, 1);
        const floatChunks = [];

        processorNode.onaudioprocess = (event) => {
          const input = event.inputBuffer.getChannelData(0);
          floatChunks.push(new Float32Array(input));
        };

        sourceNode.connect(processorNode);
        processorNode.connect(audioContext.destination);
        noteMicEvent('fallback', 'capturing-audio');
        addMicDebug('fallback:capture', `sampleRate=${fallbackSampleRate}`);

        await new Promise((resolve) => setTimeout(resolve, MIC_FALLBACK_CAPTURE_MS));

        try {
          processorNode.disconnect();
          sourceNode.disconnect();
        } catch (_) {
        }
        try {
          await audioContext.close();
        } catch (_) {
        }
        if (stream && ownsStream) {
          for (const track of stream.getTracks()) {
            track.stop();
          }
        }

        if (!floatChunks.length) {
          noteMicEvent('fallback-empty', 'no-audio-chunks');
          addMicDebug('fallback:empty', 'no-audio-chunks');
          micFallbackCaptureInFlight = false;
          return;
        }

        const targetSampleRate = 16000;
        const normalizedChunks = downsampleChunksToRate(floatChunks, fallbackSampleRate, targetSampleRate);
        const wavBlob = encodeWavBlob(normalizedChunks, targetSampleRate);
        const audioBase64 = await blobToBase64(wavBlob);
        addMicDebug('fallback:wav-ready', `bytes≈${Math.round((audioBase64.length * 3) / 4)}`);
        noteMicEvent('fallback', 'transcribe-request');
        const transcribeStartedAt = Date.now();
        const transcribeProvider = getMicTranscribeProvider();
        const transcribeRes = await fetchWithTimeout('/gateway/perception/mic/transcribe', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            audio_wav_base64: audioBase64,
            language: defaultListenLang,
            provider: transcribeProvider,
            debug: true,
          }),
        }, 12000);

        if (!transcribeRes.ok) {
          let detail = '';
          let traceId = '';
          let debugLogPath = '';
          let rawErrorPayload = null;
          try {
            const errPayload = await transcribeRes.json();
            rawErrorPayload = errPayload;
            if (errPayload && typeof errPayload.detail === 'object' && errPayload.detail !== null) {
              detail = String(errPayload.detail.message || '').trim();
              traceId = String(errPayload.detail.trace_id || '').trim();
              debugLogPath = String(errPayload.detail.debug_log_path || '').trim();
            } else {
              detail = String(errPayload?.detail || '').trim();
              traceId = String(errPayload?.trace_id || '').trim();
            }
          } catch (_) {
          }
          const detailText = detail || transcribeRes.statusText || '-';
          const detailLower = detailText.toLowerCase();
          const isProviderForbidden = detailLower.includes('recognition request failed: forbidden') || detailLower.includes('forbidden');
          const isProviderError = isProviderForbidden || detailLower.includes('speech request failed') || detailLower.includes('recognition request failed');
          const isUpstreamUnavailable = Number(transcribeRes.status || 0) >= 500;
          if (isProviderForbidden) {
            micProviderLocalBackoffUntil = Date.now() + MIC_LOCAL_PROVIDER_BACKOFF_MS;
            addMicDebug('fallback:provider-backoff', `local-for=${MIC_LOCAL_PROVIDER_BACKOFF_MS}ms`);
          }
          const traceSuffix = traceId ? ` trace=${traceId}` : '';
          if (isProviderError || isUpstreamUnavailable) {
            noteMicEvent('fallback-degraded', `provider-unavailable${traceSuffix}`);
            statusEl.textContent = 'Listening... (speech provider unavailable)';
          } else {
            noteMicEvent('fallback-error', `http-${transcribeRes.status}:${detailText}${traceSuffix}`);
          }
          addMicDebug(
            'fallback:transcribe-http',
            `status=${transcribeRes.status} statusText=${transcribeRes.statusText || '-'} detail=${detailText} trace=${traceId || '-'} debugLog=${debugLogPath || '-'} providerError=${isProviderError} body=${rawErrorPayload ? JSON.stringify(rawErrorPayload).slice(0, 420) : '-'}`,
          );
          micFallbackCaptureInFlight = false;
          return;
        }

        const payload = await transcribeRes.json();
        noteMicEvent('fallback', 'transcribe-response');
        addMicDebug('fallback:transcribe-ok', `${Date.now() - transcribeStartedAt}ms`);
        addMicDebug('fallback:provider', `${String(payload?.provider || 'unknown')} conf=${Number(payload?.confidence || 0).toFixed(2)}`);
        if (payload && payload.ok === false && String(payload.reason || '') === 'provider_unavailable') {
          const providerDetailLower = String(payload?.detail || '').toLowerCase();
          if (providerDetailLower.includes('forbidden')) {
            micProviderLocalBackoffUntil = Date.now() + MIC_LOCAL_PROVIDER_BACKOFF_MS;
            addMicDebug('fallback:provider-backoff', `local-for=${MIC_LOCAL_PROVIDER_BACKOFF_MS}ms`);
          }
          noteMicEvent('fallback-degraded', 'provider-unavailable');
          statusEl.textContent = 'Listening... (speech provider unavailable)';
          await submitMicTranscript('', 0.0, 'fallback_audio_heartbeat_provider_unavailable', true);
          micFallbackCaptureInFlight = false;
          return;
        }
        const transcript = String(payload?.transcript || '').trim();
        const fallbackProvider = String(payload?.provider || '').toLowerCase();
        const fallbackConfidence = Number(payload?.confidence || 0.74);
        if (fallbackProvider.includes('pocketsphinx') && fallbackConfidence < MIC_POCKETSPHINX_CONFIDENCE_MIN) {
          noteMicEvent('fallback-low-confidence', `${fallbackProvider}:${fallbackConfidence.toFixed(2)}`);
          addMicDebug('fallback:low-confidence-drop', `provider=${fallbackProvider} conf=${fallbackConfidence.toFixed(2)} transcript=${transcript.slice(0, 48)}`);
          statusEl.textContent = 'Listening... (low-confidence speech capture, please repeat)';
          await submitMicTranscript('', fallbackConfidence, 'fallback_audio_heartbeat_low_confidence', true);
          micFallbackCaptureInFlight = false;
          return;
        }
        if (!transcript) {
          const reason = String(payload?.reason || 'no-transcript').trim() || 'no-transcript';
          noteMicEvent('fallback-empty', reason);
          addMicDebug('fallback:no-transcript', `reason=${reason}`);
          await submitMicTranscript('', 0.0, 'fallback_audio_heartbeat_no_transcript', true);
          micFallbackCaptureInFlight = false;
          return;
        }

        if (isMicSuppressedNow()) {
          noteMicEvent('fallback-drop', 'tts-suppressed');
          addMicDebug('fallback:drop-suppressed', transcript.slice(0, 48));
          logTranscriptDrop('suppressed', transcript, 'fallback_audio');
          micFallbackCaptureInFlight = false;
          return;
        }
        if (isLikelyEchoTranscript(transcript)) {
          noteMicEvent('fallback-echo-drop', transcript.slice(0, 24));
          addMicDebug('fallback:echo-drop', transcript.slice(0, 48));
          logTranscriptDrop('echo', transcript, 'fallback_audio');
          micFallbackCaptureInFlight = false;
          return;
        }

        noteMicEvent('fallback-result', transcript.slice(0, 48));
        const isLowValueFallback = isLikelyLowValueTranscript(transcript);
        const micSync = await submitMicTranscript(
          transcript,
          isLowValueFallback ? Math.min(fallbackConfidence, 0.33) : fallbackConfidence,
          isLowValueFallback ? 'fallback_audio_short' : 'fallback_audio',
        );
        if (!micSync.ok) {
          noteMicEvent('fallback-sync-error', micSync.status);
          addMicDebug('fallback:event-sync', `status=${micSync.status}`);
        } else if (!micSync.accepted) {
          noteMicEvent('fallback-sync-skip', micSync.status);
          addMicDebug('fallback:event-sync', `skipped=${micSync.status}`);
        } else {
          addMicDebug('fallback:event-sync', `ok total=${Date.now() - captureStartedAt}ms`);
        }

        if (isLowValueFallback) {
          noteMicEvent('fallback-short', transcript.slice(0, 24));
          addMicDebug('fallback:short-transcript-forwarded', transcript);
          logTranscriptDrop('low_value', transcript, 'fallback_audio');
          await maybeHandleLowValueTranscript(transcript, 'fallback_audio');
          refreshState();
          return;
        }

        const handledWebOrCapabilityFallback = await maybeHandleWebOrCapabilityCommand(transcript, 'fallback_audio');
        if (handledWebOrCapabilityFallback) {
          refreshState();
          return;
        }

        statusEl.textContent = `Heard: ${transcript}`;
        const wakePresent = hasWakePhrase(transcript);
        if (WAKE_WORD_REQUIRED_FOR_LIVE_REPLY && !wakePresent) {
          addMicDebug('wake-gate-drop', `mode=fallback transcript=${transcript.slice(0, 40)}`);
          logTranscriptDrop('no_wake', transcript, 'fallback_audio');
          statusEl.textContent = 'Listening... (wake word required: "MIM")';
          refreshState();
          return;
        }
        const handledGreetingOnly = await maybeHandleGreetingWithoutIntent(transcript);
        if (!handledGreetingOnly) {
          const handledWeakIdentity = await maybeHandleWeakIdentityIntroduction(transcript);
          if (!handledWeakIdentity) {
            const handledUnparsedIdentityIntent = await maybeHandleUnparsedIdentityIntent(transcript);
            if (!handledUnparsedIdentityIntent) {
              const handledIdentity = await maybeHandleIdentityIntroduction(transcript);
              if (!handledIdentity) {
                const handledStandaloneName = await maybeHandleStandaloneNameDuringStartup(transcript);
                if (!handledStandaloneName) {
                  await maybeHandleStartupUncertainTranscript(transcript);
                }
              }
            }
          }
        }

        refreshState();
      } catch (error) {
        const errorName = String(error?.name || '').trim();
        if (errorName === 'AbortError') {
          noteMicEvent('fallback-error', 'transcribe-timeout');
          addMicDebug('fallback:error', 'AbortError/transcribe-timeout');
        } else {
          noteMicEvent('fallback-error', String(errorName || error?.message || 'unknown'));
          addMicDebug('fallback:error', String(errorName || error?.message || 'unknown'));
        }
      } finally {
        micFallbackCaptureInFlight = false;
        if (micAutoMode) {
          micLastSpeechEventAt = Date.now();
          micFallbackNoSpeechTimer = setTimeout(() => {
            if (!micAutoMode) return;
            captureFallbackTranscription();
          }, 7000);
        }
      }
    }

    function updateMicDiagnostics() {
      if (!availableMics.length) {
        if (micLastEvent) {
          micDiagEl.textContent = `Mic: no audio input devices detected yet. Last event: ${micLastEvent}`;
        } else {
          micDiagEl.textContent = 'Mic: no audio input devices detected yet.';
        }
        return;
      }

      let selected = availableMics.find((d) => d.deviceId === selectedMicDeviceId);
      if (!selected) {
        selected = availableMics.find((d) => d.deviceId === 'default') || availableMics[0];
      }

      const label = String(selected?.label || selectedMicLabel || 'Default microphone');
      const eventSuffix = micLastEvent ? ` · ${micLastEvent}` : '';
      if (availableMics.length > 1) {
        micDiagEl.textContent = `Mic: ${label} (${availableMics.length} detected)${eventSuffix}`;
      } else {
        micDiagEl.textContent = `Mic: ${label}${eventSuffix}`;
      }
    }

    function noteMicEvent(eventLabel, detail = '') {
      const time = new Date().toLocaleTimeString();
      const detailText = String(detail || '').trim();
      micLastEventAt = Date.now();
      micLastEvent = detailText ? `${eventLabel}:${detailText} @ ${time}` : `${eventLabel} @ ${time}`;
      if (micEventEl) {
        micEventEl.textContent = `Mic event: ${micLastEvent}`;
      }
      addMicDebug(`event:${eventLabel}`, detailText);
      listenBtn.textContent = micAutoMode ? 'Listening On' : 'Listening Off';
      updateMicDiagnostics();
    }

    function resolvePreferredMicDevice() {
      if (!availableMics.length) return null;

      if (PIN_TO_SYSTEM_DEFAULT_MIC) {
        const defaultDevice = availableMics.find((d) => d.deviceId === 'default');
        if (defaultDevice) {
          return defaultDevice;
        }
      }

      const explicit = availableMics.find((d) => d.deviceId === selectedMicDeviceId);
      if (explicit && selectedMicDeviceId && selectedMicDeviceId !== 'default' && selectedMicDeviceId !== 'communications') {
        return explicit;
      }

      const candidates = availableMics.filter((d) => d.deviceId && d.deviceId !== 'default' && d.deviceId !== 'communications');
      if (!candidates.length) {
        return availableMics.find((d) => d.deviceId === 'default') || availableMics[0] || null;
      }

      const scored = candidates.map((device) => {
        const label = String(device.label || '').toLowerCase();
        let score = 0;
        if (/(fduce|usb|headset|microphone|mic|pro audio|analog)/.test(label)) score += 30;
        if (/(camera|webcam|emeet|s600)/.test(label)) score -= 40;
        return { device, score };
      });

      scored.sort((a, b) => b.score - a.score);
      return scored[0]?.device || candidates[0];
    }

    async function enumerateMicDevices() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
        availableMics = [];
        micSelect.innerHTML = '';
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'Microphone listing unavailable';
        micSelect.appendChild(option);
        updateMicDiagnostics();
        return;
      }

      try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        availableMics = devices.filter((d) => d.kind === 'audioinput');
      } catch (_) {
        availableMics = [];
      }

      micSelect.innerHTML = '';
      if (!availableMics.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No microphones detected';
        micSelect.appendChild(option);
        updateMicDiagnostics();
        return;
      }

      const preferred = resolvePreferredMicDevice();
      let selected = availableMics.find((d) => d.deviceId === selectedMicDeviceId);
      if (!selected) {
        selected = preferred || availableMics.find((d) => d.deviceId === 'default') || availableMics[0];
      }

      for (let index = 0; index < availableMics.length; index += 1) {
        const mic = availableMics[index];
        const option = document.createElement('option');
        option.value = mic.deviceId;
        option.textContent = mic.label || `Microphone ${index + 1}`;
        micSelect.appendChild(option);
      }

      selectedMicDeviceId = selected?.deviceId || '';
      selectedMicLabel = selected?.label || '';
      micSelect.value = selectedMicDeviceId;
      localStorage.setItem('mim_mic_device_id', selectedMicDeviceId);
      localStorage.setItem('mim_mic_device_label', selectedMicLabel);
      updateMicDiagnostics();
    }

    async function enumerateCameraDevices() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
        availableCameras = [];
        cameraSelect.innerHTML = '';
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'Camera listing unavailable';
        cameraSelect.appendChild(option);
        cameraSettingsStatus.textContent = 'Camera listing unavailable in this runtime.';
        updateCameraSettingsUi();
        return;
      }

      try {
        const devices = await navigator.mediaDevices.enumerateDevices();
        availableCameras = devices.filter((d) => d.kind === 'videoinput');
      } catch (_) {
        availableCameras = [];
      }

      cameraSelect.innerHTML = '';
      if (!availableCameras.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'No cameras detected';
        cameraSelect.appendChild(option);
        cameraSettingsStatus.textContent = 'No camera devices detected.';
        updateCameraSettingsUi();
        return;
      }

      let selected = availableCameras.find((d) => d.deviceId === selectedCameraDeviceId);
      if (!selected) {
        selected = availableCameras[0];
      }

      for (let index = 0; index < availableCameras.length; index += 1) {
        const camera = availableCameras[index];
        const option = document.createElement('option');
        option.value = camera.deviceId;
        option.textContent = camera.label || `Camera ${index + 1}`;
        cameraSelect.appendChild(option);
      }

      selectedCameraDeviceId = selected?.deviceId || '';
      cameraSelect.value = selectedCameraDeviceId;
      localStorage.setItem('mim_camera_device_id', selectedCameraDeviceId);
      if (!(cameraStream && cameraStream.active)) {
        cameraSettingsStatus.textContent = `Selected camera: ${selected?.label || 'default camera'}`;
      }
      updateCameraSettingsUi();
    }

    function syncVoiceControlLabels() {
      voiceRateValueEl.textContent = Number(voiceRate).toFixed(2);
      voicePitchValueEl.textContent = Number(voicePitch).toFixed(2);
      voiceDepthValueEl.textContent = String(Math.round(voiceDepth));
      voiceVolumeValueEl.textContent = Number(voiceVolume).toFixed(2);
    }

    function effectivePitchValue() {
      const depthLowering = (voiceDepth / 100) * 0.45;
      return clamp(voicePitch - depthLowering, 0.5, 2.0);
    }

    function hasAnyHealthError() {
      return !healthState.backendOk || !healthState.micOk || !healthState.cameraOk || !healthState.voicesOk;
    }

    function hasCriticalHealthError() {
      return !healthState.backendOk || !healthState.micAvailable || !healthState.micOk;
    }

    function isMicEffectivelyActive() {
      if (micListening || micStartInFlight || micRestartPending) return true;
      return (Date.now() - micLastActiveAt) < 4000;
    }

    function applyStatusFromHealth() {
      if (!healthState.backendOk) {
        statusEl.textContent = 'Backend unreachable. Retrying...';
        return;
      }
      if (!healthState.micAvailable) {
        statusEl.textContent = 'Mic recognition API unavailable in this runtime.';
        return;
      }
      if (micRecoveryMode) {
        const remainingMs = Math.max(0, micCooldownUntil - Date.now());
        const remainingSec = Math.ceil(remainingMs / 1000);
        statusEl.textContent = remainingSec > 0
          ? `Mic stabilizing (${remainingSec}s)...`
          : 'Mic stabilization complete. Reconnecting...';
        return;
      }
      if (!healthState.micOk) {
        if (micLastErrorCode) {
          statusEl.textContent = `Mic recovering (${micLastErrorCode})...`;
        } else {
          statusEl.textContent = 'Mic recovering from errors...';
        }
        return;
      }
      if (!healthState.voicesOk) {
        statusEl.textContent = 'Voice list unavailable. Using system default voice.';
        return;
      }
      if (!micAutoMode && isMicEffectivelyActive()) {
        statusEl.textContent = 'Listening...';
        return;
      }
      if (micAutoMode && !isMicEffectivelyActive()) {
        if (micErrorStreak > 0) {
          statusEl.textContent = 'Mic reconnecting...';
        } else {
          statusEl.textContent = 'Starting always-listen mic...';
        }
        return;
      }
      if (micAutoMode && isMicEffectivelyActive()) {
        statusEl.textContent = 'Always listening...';
        return;
      }
      if (!micAutoMode) {
        statusEl.textContent = 'Listening paused.';
      }
    }

    function runtimeLaneSnapshot(data, laneName) {
      const operatorReasoning = (data && typeof data.operator_reasoning === 'object') ? data.operator_reasoning : {};
      const runtimeHealth = (operatorReasoning && typeof operatorReasoning.runtime_health === 'object')
        ? operatorReasoning.runtime_health
        : {};
      const checks = (runtimeHealth && typeof runtimeHealth.checks === 'object')
        ? runtimeHealth.checks
        : {};
      return (checks && typeof checks[laneName] === 'object') ? checks[laneName] : {};
    }

    function isoFromMillis(value) {
      const millis = Number(value || 0);
      if (!Number.isFinite(millis) || millis <= 0) {
        return null;
      }
      return new Date(millis).toISOString();
    }

    function isCameraWatcherRunning() {
      return Boolean(
        motionInterval
        && cameraStream
        && cameraStream.active
        && cameraWatcherVideo
        && cameraWatcherVideo.readyState >= 2
      );
    }

    function buildCameraRecoveryMetadata(extra = {}) {
      return {
        watcher_running: isCameraWatcherRunning(),
        watcher_started_at: isoFromMillis(cameraWatcherStartedAt),
        last_frame_seen_at: isoFromMillis(cameraLastFrameSeenAt),
        last_healthy_frame_at: isoFromMillis(cameraLastHealthyFrameAt),
        ...extra,
      };
    }

    function classifyCameraRetryReason(cameraLane, now) {
      const staleThresholdMs = Math.max(5000, Number(cameraLane.stale_threshold_seconds || 30) * 1000);
      const watcherRunning = isCameraWatcherRunning();
      const lastFrameAgeMs = cameraLastFrameSeenAt ? Math.max(0, now - cameraLastFrameSeenAt) : null;
      const lastHealthyFrameAgeMs = cameraLastHealthyFrameAt ? Math.max(0, now - cameraLastHealthyFrameAt) : null;

      if (!watcherRunning) {
        return {
          category: 'watcher_not_running',
          detail: 'Camera retry triggered because the watcher was not running.',
          healthReportDisagreement: false,
        };
      }

      if (lastFrameAgeMs === null || lastFrameAgeMs > CAMERA_FRAME_STARVED_MS) {
        return {
          category: 'no_frames',
          detail: 'Camera retry triggered because no recent frames were observed from the watcher.',
          healthReportDisagreement: false,
        };
      }

      if (lastHealthyFrameAgeMs !== null && lastHealthyFrameAgeMs < staleThresholdMs) {
        return {
          category: 'health_report_disagreement',
          detail: 'Backend reported camera stale even though a recent healthy frame was observed locally.',
          healthReportDisagreement: true,
        };
      }

      return {
        category: 'stale_frames',
        detail: 'Camera frames continued locally, but no recent frame kept the backend lane healthy.',
        healthReportDisagreement: false,
      };
    }

    async function maybeRecoverRuntimeHealth(data) {
      const now = Date.now();

      const microphoneLane = runtimeLaneSnapshot(data, 'microphone');
      const microphoneStatus = String(microphoneLane.status || '').trim().toLowerCase();
      if (runtimeRecoveryPending.microphone && microphoneStatus === 'healthy') {
        await postRuntimeRecoveryEvent(
          'microphone',
          'recovery_succeeded',
          'Microphone lane returned healthy after backend stale signal.',
          runtimeRecoveryPending.microphone.nextRetryAt || null,
          { status: microphoneStatus },
        );
        runtimeRecoveryPending.microphone = null;
      }
      if (
        microphoneStatus === 'stale'
        && micAutoMode
        && !micRecoveryMode
        && !micRestartPending
        && now >= Number(runtimeHealthRecoveryCooldownUntil.microphone || 0)
      ) {
        runtimeHealthRecoveryCooldownUntil.microphone = now + RUNTIME_HEALTH_RECOVERY_COOLDOWN_MS;
        const nextRetryAt = new Date(runtimeHealthRecoveryCooldownUntil.microphone).toISOString();
        runtimeRecoveryPending.microphone = {
          startedAt: new Date(now).toISOString(),
          nextRetryAt,
        };
        await postRuntimeRecoveryEvent(
          'microphone',
          'stale_detected',
          String(microphoneLane.detail || microphoneLane.summary || 'Microphone lane reported stale.').trim(),
          nextRetryAt,
          { status: microphoneStatus },
        );
        await postRuntimeRecoveryEvent(
          'microphone',
          'recovery_attempted',
          'Client entered microphone recovery after backend stale signal.',
          nextRetryAt,
          { status: microphoneStatus },
        );
        addMicDebug('runtime-health', 'backend reported stale microphone lane; entering recovery');
        enterMicRecovery('runtime-health-stale');
      }

      const cameraLane = runtimeLaneSnapshot(data, 'camera');
      const cameraStatus = String(cameraLane.status || '').trim().toLowerCase();
      if (runtimeRecoveryPending.camera && cameraStatus === 'healthy') {
        const firstHealthyAt = runtimeRecoveryPending.camera.firstHealthyAt || new Date(now).toISOString();
        runtimeRecoveryPending.camera.firstHealthyAt = firstHealthyAt;
        await postRuntimeRecoveryEvent(
          'camera',
          'healthy_observed',
          'Camera lane returned healthy after backend stale signal.',
          runtimeRecoveryPending.camera.nextRetryAt || null,
          buildCameraRecoveryMetadata({
            status: cameraStatus,
            first_healthy_at: firstHealthyAt,
            recovery_attempted_at: runtimeRecoveryPending.camera.startedAt || null,
            retry_reason: runtimeRecoveryPending.camera.retryReason || null,
            retry_reason_detail: runtimeRecoveryPending.camera.retryReasonDetail || null,
          }),
        );
        runtimeRecoveryPending.camera = null;
      }
      const cameraCanRecover = Boolean(healthState.cameraOk || (cameraStream && cameraStream.active));
      if (
        cameraStatus === 'stale'
        && cameraCanRecover
        && !cameraRecoveryInFlight
        && now >= Number(runtimeHealthRecoveryCooldownUntil.camera || 0)
      ) {
        runtimeHealthRecoveryCooldownUntil.camera = now + RUNTIME_HEALTH_RECOVERY_COOLDOWN_MS;
        const nextRetryAt = new Date(runtimeHealthRecoveryCooldownUntil.camera).toISOString();
        const retryReason = classifyCameraRetryReason(cameraLane, now);
        runtimeRecoveryPending.camera = {
          startedAt: new Date(now).toISOString(),
          nextRetryAt,
          retryReason: retryReason.category,
          retryReasonDetail: retryReason.detail,
        };
        await postRuntimeRecoveryEvent(
          'camera',
          'stale_detected',
          String(cameraLane.detail || cameraLane.summary || 'Camera lane reported stale.').trim(),
          nextRetryAt,
          buildCameraRecoveryMetadata({
            status: cameraStatus,
            backend_age_seconds: Number(cameraLane.age_seconds || 0),
            retry_reason: retryReason.category,
            retry_reason_detail: retryReason.detail,
            health_report_disagreement: retryReason.healthReportDisagreement,
          }),
        );
        await postRuntimeRecoveryEvent(
          'camera',
          'recovery_attempted',
          'Client restarted camera watcher after backend stale signal.',
          nextRetryAt,
          buildCameraRecoveryMetadata({
            status: cameraStatus,
            recovery_attempted_at: runtimeRecoveryPending.camera.startedAt,
            retry_reason: retryReason.category,
            retry_reason_detail: retryReason.detail,
            health_report_disagreement: retryReason.healthReportDisagreement,
          }),
        );
        cameraRecoveryInFlight = true;
        cameraSettingsStatus.textContent = 'Camera runtime stale. Restarting watcher...';
        try {
          await startCameraWatcher();
          await postRuntimeRecoveryEvent(
            'camera',
            'recovery_succeeded',
            'Camera watcher restarted successfully.',
            nextRetryAt,
            buildCameraRecoveryMetadata({
              status: 'healthy',
              recovery_attempted_at: runtimeRecoveryPending.camera.startedAt,
              retry_reason: runtimeRecoveryPending.camera.retryReason || null,
              retry_reason_detail: runtimeRecoveryPending.camera.retryReasonDetail || null,
            }),
          );
        } finally {
          cameraRecoveryInFlight = false;
        }
      }
    }

    function updateIconGlow() {
      mimIcon.classList.remove('ok', 'err');
      if (hasCriticalHealthError()) {
        mimIcon.classList.add('err');
        applyStatusFromHealth();
        return;
      }
      mimIcon.classList.add('ok');
      applyStatusFromHealth();
    }

    function enterMicRecovery(reason) {
      micRecoveryMode = true;
      micRecoveryReason = String(reason || 'restart-flap');
      const cooldownMs = micRecoveryReason.includes('short-run-flap') ? 12000 : MIC_FLAP_COOLDOWN_MS;
      micCooldownUntil = Date.now() + cooldownMs;
      micListening = false;
      micStartInFlight = false;
      micRestartPending = true;
      micErrorStreak = 0;
      micHardErrorStreak = 0;
      micLastErrorCode = '';
      healthState.micOk = true;

      if (recognition) {
        try {
          recognition.stop();
        } catch (_) {
        }
      }

      if (micRetryTimer) {
        clearTimeout(micRetryTimer);
      }
      micRetryTimer = setTimeout(() => {
        micRetryTimer = null;
        micRecoveryMode = false;
        micRecoveryReason = '';
        micRestartPending = false;
        micEndTimestamps = [];
        if (micUnstableCycleCount >= MIC_UNSTABLE_MAX_CYCLES) {
          micAutoMode = false;
          listenBtn.textContent = 'Listening Off';
          statusEl.textContent = 'Mic paused after repeated unstable starts. Press Listen to retry.';
          updateIconGlow();
          return;
        }
        listenOnce();
      }, cooldownMs);

      updateIconGlow();
    }

    function noteMicCycleAndMaybeRecover(reason) {
      if (!String(reason || '').startsWith('hard-error:')) {
        return false;
      }
      const now = Date.now();
      micEndTimestamps.push(now);
      micEndTimestamps = micEndTimestamps.filter((ts) => now - ts <= MIC_FLAP_WINDOW_MS);
      if (micEndTimestamps.length >= MIC_FLAP_THRESHOLD) {
        enterMicRecovery(reason || 'restart-flap');
        return true;
      }
      return false;
    }

    function scheduleMicRetry(delayMs) {
      if (!micAutoMode) return;
      micRestartPending = true;
      if (micRetryTimer) {
        clearTimeout(micRetryTimer);
      }
      micRetryTimer = setTimeout(() => {
        micRetryTimer = null;
        micRestartPending = false;
        listenOnce();
      }, Math.max(150, Number(delayMs) || 350));
    }

    function clearMicStartTimeout() {
      if (micStartTimeoutTimer) {
        clearTimeout(micStartTimeoutTimer);
        micStartTimeoutTimer = null;
      }
    }

    function pauseMicAuto(reasonText) {
      micAutoMode = false;
      listenBtn.textContent = 'Listening Off';
      micListening = false;
      micStartInFlight = false;
      micRestartPending = false;
      stopMicFallbackLoop();
      clearMicStartTimeout();
      if (micRetryTimer) {
        clearTimeout(micRetryTimer);
        micRetryTimer = null;
      }
      statusEl.textContent = reasonText || 'Mic auto-listen paused.';
      updateIconGlow();
    }

    function noteMicLifecycleEvent() {
      micLastLifecycleEventAt = Date.now();
    }

    function resetRecognitionInstance() {
      clearMicStartTimeout();
      if (recognition) {
        try {
          recognition.onstart = null;
          recognition.onresult = null;
          recognition.onerror = null;
          recognition.onend = null;
          recognition.stop();
        } catch (_) {
        }
      }
      recognition = null;
      micListening = false;
      micStartInFlight = false;
      micSessionStartedAt = 0;
    }

    function markBackendReachability(ok) {
      if (ok) {
        backendFailureStreak = 0;
        backendSuccessStreak += 1;
        if (backendSuccessStreak >= 1) {
          healthState.backendOk = true;
        }
        return;
      }

      backendSuccessStreak = 0;
      backendFailureStreak += 1;
      if (backendFailureStreak >= 3) {
        healthState.backendOk = false;
      }
    }

    async function submitMicTranscript(transcript, confidence, mode = 'always_listening', allowEmpty = false) {
      const safeTranscript = String(transcript || '').trim();
      if (!safeTranscript && !allowEmpty) {
        return { ok: false, accepted: false, status: 'empty_transcript' };
      }

      if (safeTranscript && isLikelyEchoTranscript(safeTranscript)) {
        logTranscriptDrop('echo', safeTranscript, mode);
        return { ok: true, accepted: false, status: 'echo_suppressed' };
      }

      const safeConfidence = clamp(Number(confidence || 0.72), 0.0, 1.0);
      try {
        const micEventRes = await fetchWithTimeout('/gateway/perception/mic/events', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            device_id: 'mim-ui-mic',
            source_type: 'microphone',
            session_id: 'mim-ui-session',
            is_remote: false,
            transcript: safeTranscript,
            confidence: safeConfidence,
            min_interval_seconds: MIC_EVENT_MIN_INTERVAL_SECONDS,
            duplicate_window_seconds: MIC_EVENT_DUPLICATE_WINDOW_SECONDS,
            transcript_confidence_floor: MIC_EVENT_CONFIDENCE_FLOOR,
            discard_low_confidence: false,
            metadata_json: { source: 'mim_ui_sketch', mode },
          }),
        }, 8000);

        if (!micEventRes.ok) {
          markBackendReachability(false);
          return {
            ok: false,
            accepted: false,
            status: `http_${micEventRes.status}`,
          };
        }

        markBackendReachability(true);
        let payload = {};
        try {
          payload = await micEventRes.json();
        } catch (_) {
          payload = {};
        }
        return {
          ok: true,
          accepted: payload?.accepted !== false,
          status: String(payload?.status || 'accepted'),
        };
      } catch (_) {
        markBackendReachability(false);
        return { ok: false, accepted: false, status: 'network_error' };
      }
    }

    function isLikelyLowValueTranscript(transcript) {
      const text = String(transcript || '').trim().toLowerCase();
      if (!text) return true;

      const compact = text.replace(/[^a-z]/g, '');
      if (!compact) return true;
      if (compact.length <= 2) return true;

      const normalized = text.replace(/[^a-z'\s]/g, ' ').replace(/\s+/g, ' ').trim();
      const tokens = normalized ? normalized.split(' ').filter(Boolean) : [];
      if (!tokens.length) return true;
      if (tokens.length >= 2 && tokens.every((token) => token.length <= 2)) {
        return true;
      }

      const fillerTokens = new Set(['um', 'uh', 'hmm', 'mm', 'erm', 'ah', 'eh']);
      if (tokens.every((token) => fillerTokens.has(token))) {
        return true;
      }

      return compact.length < 5;
    }

    async function maybeHandleLowValueTranscript(transcript, sourceMode = 'mic') {
      if (!isLikelyLowValueTranscript(transcript)) {
        return false;
      }

      logTranscriptDrop('low_value', transcript, sourceMode);

      const compact = String(transcript || '').toLowerCase().replace(/[^a-z]/g, '');
      const now = Date.now();
      if (now < lowValueClarifyCooldownUntil && compact && compact === lowValueClarifyLastCompact) {
        return true;
      }

      if (now < lowValueSpeakCooldownUntil) {
        addMicDebug('low-value-suppressed', `mode=${sourceMode} transcript=${String(transcript || '').slice(0, 24)}`);
        return true;
      }

      if (compact.length < 3) {
        addMicDebug('low-value-muted', `mode=${sourceMode} transcript=${String(transcript || '').slice(0, 24)}`);
        lowValueClarifyLastCompact = compact;
        lowValueClarifyCooldownUntil = now + LOW_VALUE_CLARIFY_COOLDOWN_MS;
        return true;
      }

      lowValueClarifyLastCompact = compact;
      lowValueClarifyCooldownUntil = now + LOW_VALUE_CLARIFY_COOLDOWN_MS;
      lowValueSpeakCooldownUntil = now + LOW_VALUE_SPEAK_COOLDOWN_MS;

      const clarify = buildDialogPrompt('low_value', { transcript });

      statusEl.textContent = `Low-confidence input ignored (${sourceMode}).`;
      inquiryEl.textContent = clarify;
      addMicDebug('low-value-clarify', `mode=${sourceMode} transcript=${String(transcript || '').slice(0, 24)}`);
      return true;
    }

    function isGreetingOnlyTranscript(transcript) {
      const text = String(transcript || '').toLowerCase();
      if (!text.trim()) return false;
      if (text.includes('my name is') || text.includes("i am") || text.includes("i'm")) return false;

      const normalized = text.replace(/[^a-z'\s]/g, ' ').replace(/\s+/g, ' ').trim();
      if (!normalized) return false;
      return /^(hello|hi|hey)(\s+(ma'?am|mam|maam|sir|mim))*$/.test(normalized);
    }

    async function maybeHandleGreetingWithoutIntent(transcript) {
      if (!isGreetingOnlyTranscript(transcript)) {
        return false;
      }

      const now = Date.now();
      if (now < greetingClarifyCooldownUntil) {
        return true;
      }

      greetingClarifyCooldownUntil = now + GREETING_CLARIFY_COOLDOWN_MS;
      startupInquiryIssued = true;
      const prompt = buildDialogPrompt('greeting_only', { transcript });
      inquiryEl.textContent = prompt;
      await speakLocally(prompt, true, 'greeting_only');
      lastInquiryPromptSpoken = prompt;
      addMicDebug('greeting-clarify', String(transcript || '').slice(0, 32));
      return true;
    }

    async function maybeHandleStandaloneNameDuringStartup(transcript) {
      if (!startupInquiryIssued || !shouldAskForNameNow()) return false;
      const text = String(transcript || '').toLowerCase().replace(/[^a-z'\-\s]/g, ' ').replace(/\s+/g, ' ').trim();
      if (!text) return false;
      if (text.includes('my name is') || text.includes("i'm") || text.includes('i am')) return false;

      const filler = new Set(['hello', 'hi', 'hey', 'it', 'had', 'a', 'the', 'is', 'name', 'my', 'maam', 'mam', 'sir', 'there']);
      const parts = text.split(' ').map((s) => s.trim()).filter(Boolean);
      if (!parts.length || parts.length > 2) return false;

      const candidates = parts.filter((part) => part.length >= 2 && part.length <= 24 && !filler.has(part) && !WEAK_IDENTITY_WORDS.has(part));
      if (!candidates.length) return false;

      const candidate = candidates[candidates.length - 1];
      addMicDebug('identity-standalone', `candidate=${candidate}`);
      return await acknowledgeIntroducedIdentity(candidate);
    }

    function isLikelyIdentityAttemptTranscript(transcript) {
      const text = String(transcript || '').toLowerCase().replace(/[^a-z'\s]/g, ' ').replace(/\s+/g, ' ').trim();
      if (!text) return false;
      if (text.includes('my name is') || text.includes('name is') || text.includes('i am') || text.includes("i'm")) return true;

      const parts = text.split(' ').map((s) => s.trim()).filter(Boolean);
      if (parts.length >= 1 && parts.length <= 2) {
        return parts.every((part) => part.length >= 2 && part.length <= 24);
      }

      return false;
    }

    async function maybeHandleStartupUncertainTranscript(transcript) {
      if (!startupInquiryIssued || !shouldAskForNameNow()) return false;
      if (!isLikelyIdentityAttemptTranscript(transcript)) return false;
      const compact = String(transcript || '').toLowerCase().replace(/[^a-z]/g, '');
      if (!compact) return false;

      const now = Date.now();
      if (now < startupFeedbackCooldownUntil) {
        return true;
      }

      startupFeedbackCooldownUntil = now + 45000;
      startupFeedbackLastCompact = compact;
      const prompt = buildDialogPrompt('uncertain_name', { transcript });
      inquiryEl.textContent = prompt;
      await speakLocally(prompt, true, 'startup_uncertain_name');
      lastInquiryPromptSpoken = prompt;
      addMicDebug('startup-uncertain', String(transcript || '').slice(0, 36));
      return true;
    }

    function stopServerTtsPlayback() {
      if (activeServerTtsAudio) {
        try {
          activeServerTtsAudio.pause();
          activeServerTtsAudio.src = '';
        } catch (_) {
        }
      }
      activeServerTtsAudio = null;
      if (activeServerTtsUrl) {
        try {
          URL.revokeObjectURL(activeServerTtsUrl);
        } catch (_) {
        }
      }
      activeServerTtsUrl = '';
      speechPlaybackActive = false;
      setSpeaking(false);
    }

    function speakWithBrowserTts(text, interrupt = true) {
      const phrase = String(text || '').trim();
      const smoothedPhrase = phrase.replace(/\s*[—-]\s*/g, ', ').replace(/\s{2,}/g, ' ').trim();
      if (!phrase) return false;
      if (!window.speechSynthesis) {
        lastLocalTtsError = 'speechSynthesis API unavailable';
        statusEl.textContent = 'Local TTS unavailable in this runtime.';
        return false;
      }

      try {
        const playbackToken = ++localTtsPlaybackToken;
        activeSpeechOwner = 'browser_tts';
        rememberSpokenUtterance(phrase, 'browser_tts');
        addSpeechDebug('queued', `source=browser_tts path=local sig=${shortSpeechSignature(phrase)} interrupt=${Boolean(interrupt)} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
        setMicSuppression(2500, 'browser_tts_start');
        if (window.speechSynthesis.resume) {
          window.speechSynthesis.resume();
        }
        if (interrupt) {
          addSpeechDebug('canceled', `source=browser_tts reason=interrupt token=${playbackToken}`);
          window.speechSynthesis.cancel();
        }

        let started = false;
        let retriedBare = false;
        const utteranceText = naturalVoicePreset ? (smoothedPhrase || phrase) : phrase;
        const utterance = new SpeechSynthesisUtterance(utteranceText);
        const preferredLang = getPreferredInteractionLanguage();
        utterance.lang = preferredLang;

        const chosenVoice = resolveVoiceForLanguage(preferredLang);
        if (chosenVoice) {
          utterance.voice = chosenVoice;
        }

        const appliedRate = naturalVoicePreset
          ? clamp(voiceRate, 0.88, 1.00)
          : clamp(voiceRate, 0.1, 10.0);
        const appliedPitch = naturalVoicePreset
          ? clamp(effectivePitchValue(), 0.78, 0.98)
          : effectivePitchValue();
        const appliedVolume = naturalVoicePreset
          ? clamp(voiceVolume, 0.75, 1.0)
          : clamp(voiceVolume, 0.0, 1.0);
        utterance.rate = appliedRate;
        utterance.pitch = appliedPitch;
        utterance.volume = appliedVolume;
        utterance.onstart = () => {
          if (playbackToken !== localTtsPlaybackToken) return;
          started = true;
          lastLocalTtsError = '';
          speechPlaybackActive = true;
          activeSpeechOwner = 'browser_tts';
          setMicSuppression(2500, 'browser_tts_onstart');
          addSpeechDebug('started', `source=browser_tts path=local sig=${shortSpeechSignature(phrase)} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
          setSpeaking(true);
        };
        utterance.onend = () => {
          if (playbackToken !== localTtsPlaybackToken) return;
          speechPlaybackActive = false;
          if (activeSpeechOwner === 'browser_tts') {
            activeSpeechOwner = '';
          }
          setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'browser_tts_onend');
          addSpeechDebug('ended', `source=browser_tts path=local token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
          setSpeaking(false);
        };
        utterance.onerror = (event) => {
          if (playbackToken !== localTtsPlaybackToken) return;
          lastLocalTtsError = String(event?.error || 'unknown_tts_error');
          speechPlaybackActive = false;
          if (activeSpeechOwner === 'browser_tts') {
            activeSpeechOwner = '';
          }
          setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'browser_tts_onerror');
          addSpeechDebug('canceled', `source=browser_tts reason=error:${lastLocalTtsError} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
          setSpeaking(false);
          statusEl.textContent = `Local voice playback failed (${lastLocalTtsError}).`;
        };
        window.speechSynthesis.speak(utterance);

        const tryBareRetry = () => {
          if (playbackToken !== localTtsPlaybackToken || started || retriedBare) return;
          retriedBare = true;
          try {
            window.speechSynthesis.cancel();
            const fallbackUtterance = new SpeechSynthesisUtterance(utteranceText);
            fallbackUtterance.rate = appliedRate;
            fallbackUtterance.pitch = appliedPitch;
            fallbackUtterance.volume = appliedVolume;
            fallbackUtterance.onstart = () => {
              if (playbackToken !== localTtsPlaybackToken) return;
              started = true;
              lastLocalTtsError = '';
              speechPlaybackActive = true;
              activeSpeechOwner = 'browser_tts';
              setMicSuppression(2500, 'browser_tts_fallback_onstart');
              addSpeechDebug('started', `source=browser_tts path=local-retry sig=${shortSpeechSignature(phrase)} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
              setSpeaking(true);
            };
            fallbackUtterance.onend = () => {
              if (playbackToken !== localTtsPlaybackToken) return;
              speechPlaybackActive = false;
              if (activeSpeechOwner === 'browser_tts') {
                activeSpeechOwner = '';
              }
              setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'browser_tts_fallback_onend');
              addSpeechDebug('ended', `source=browser_tts path=local-retry token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
              setSpeaking(false);
            };
            fallbackUtterance.onerror = (event) => {
              if (playbackToken !== localTtsPlaybackToken) return;
              lastLocalTtsError = String(event?.error || 'fallback_tts_error');
              speechPlaybackActive = false;
              if (activeSpeechOwner === 'browser_tts') {
                activeSpeechOwner = '';
              }
              setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'browser_tts_fallback_onerror');
              addSpeechDebug('canceled', `source=browser_tts path=local-retry reason=error:${lastLocalTtsError} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
              setSpeaking(false);
              statusEl.textContent = `Local voice playback failed (${lastLocalTtsError}).`;
            };
            window.speechSynthesis.speak(fallbackUtterance);
          } catch (_) {
          }
        };

        setTimeout(() => {
          if (playbackToken === localTtsPlaybackToken && !started) {
            tryBareRetry();
          }
        }, 1200);

        setTimeout(() => {
          if (playbackToken === localTtsPlaybackToken && !started) {
            const voices = window.speechSynthesis.getVoices ? window.speechSynthesis.getVoices() : [];
            const voiceCount = Array.isArray(voices) ? voices.length : 0;
            lastLocalTtsError = `tts_not_started voices=${voiceCount}`;
            statusEl.textContent = `Voice playback did not start (voices=${voiceCount}).`;
          }
        }, 2500);

        return true;
      } catch (_) {
        lastLocalTtsError = 'exception_during_tts';
        statusEl.textContent = 'Local voice playback failed before start.';
        return false;
      }
    }

    async function speakWithServerTts(text, interrupt = true) {
      const phrase = String(text || '').trim();
      if (!phrase || !serverTtsEnabled) return false;

      try {
        const playbackToken = localTtsPlaybackToken;
        activeSpeechOwner = 'server_tts';
        rememberSpokenUtterance(phrase, 'server_tts');
        addSpeechDebug('queued', `source=server_tts path=server sig=${shortSpeechSignature(phrase)} interrupt=${Boolean(interrupt)} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
        setMicSuppression(2500, 'server_tts_start');
        if (interrupt) {
          addSpeechDebug('canceled', `source=server_tts reason=interrupt token=${playbackToken}`);
          stopServerTtsPlayback();
        }
        if (window.speechSynthesis && window.speechSynthesis.cancel) {
          window.speechSynthesis.cancel();
        }

        const res = await fetch('/gateway/voice/tts', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: phrase,
            language: getPreferredInteractionLanguage(),
            voice: selectedServerTtsVoice,
          }),
        });
        if (!res.ok) {
          lastLocalTtsError = `server_tts_http_${res.status}`;
          addSpeechDebug('suppressed', `source=server_tts reason=http_${res.status} sig=${shortSpeechSignature(phrase)} token=${playbackToken}`);
          return false;
        }

        const audioBlob = await res.blob();
        if (!audioBlob || audioBlob.size < 256) {
          lastLocalTtsError = 'server_tts_empty_audio';
          addSpeechDebug('suppressed', `source=server_tts reason=empty-audio sig=${shortSpeechSignature(phrase)} token=${playbackToken}`);
          return false;
        }

        stopServerTtsPlayback();
        activeServerTtsUrl = URL.createObjectURL(audioBlob);
        activeServerTtsAudio = new Audio(activeServerTtsUrl);
        activeServerTtsAudio.preload = 'auto';
        speechPlaybackActive = true;
        addSpeechDebug('started', `source=server_tts path=server sig=${shortSpeechSignature(phrase)} token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
        setSpeaking(true);

        activeServerTtsAudio.onended = () => {
          speechPlaybackActive = false;
          if (activeSpeechOwner === 'server_tts') {
            activeSpeechOwner = '';
          }
          setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'server_tts_onend');
          addSpeechDebug('ended', `source=server_tts path=server token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
          setSpeaking(false);
          stopServerTtsPlayback();
        };
        activeServerTtsAudio.onerror = () => {
          speechPlaybackActive = false;
          if (activeSpeechOwner === 'server_tts') {
            activeSpeechOwner = '';
          }
          setMicSuppression(MIC_POST_TTS_SUPPRESS_MS, 'server_tts_onerror');
          addSpeechDebug('canceled', `source=server_tts path=server reason=playback-error token=${playbackToken} suppressMs=${suppressionWindowMs()}`);
          setSpeaking(false);
          stopServerTtsPlayback();
        };

        const playPromise = activeServerTtsAudio.play();
        if (playPromise && typeof playPromise.then === 'function') {
          await playPromise;
        }

        lastLocalTtsError = '';
        return true;
      } catch (error) {
        lastLocalTtsError = String(error?.message || 'server_tts_failed');
        addSpeechDebug('suppressed', `source=server_tts reason=${lastLocalTtsError} sig=${shortSpeechSignature(phrase)} token=${localTtsPlaybackToken}`);
        stopServerTtsPlayback();
        setSpeaking(false);
        return false;
      }
    }

    async function speakLocally(text, interrupt = true, sourceTag = 'unspecified') {
      const phrase = String(text || '').trim();
      if (!phrase) return false;

      const compact = phrase.toLowerCase().replace(/[^a-z0-9]/g, '');
      const now = Date.now();
      if (compact && compact === lastSpokenPhraseCompact && (now - lastSpokenPhraseAt) < SPOKEN_PHRASE_DEDUPE_MS) {
        addSpeechDebug('suppressed', `source=${sourceTag} reason=dedupe sig=${shortSpeechSignature(phrase)} token=${localTtsPlaybackToken}`);
        return false;
      }

      if ((speechInFlight || speechPlaybackActive) && !interrupt) {
        addSpeechDebug('suppressed', `source=${sourceTag} reason=busy_no_interrupt sig=${shortSpeechSignature(phrase)} token=${localTtsPlaybackToken}`);
        return false;
      }

      if ((speechInFlight || speechPlaybackActive) && interrupt) {
        addSpeechDebug('canceled', `source=${sourceTag} reason=interrupt-active-owner owner=${activeSpeechOwner || '-'} token=${localTtsPlaybackToken}`);
        stopServerTtsPlayback();
        localTtsPlaybackToken += 1;
        if (window.speechSynthesis && window.speechSynthesis.cancel) {
          window.speechSynthesis.cancel();
        }
      }

      if (compact) {
        lastSpokenPhraseCompact = compact;
        lastSpokenPhraseAt = now;
      }

      const requestId = ++speechRequestSeq;
      speechInFlight = true;
      addSpeechDebug('queued', `source=${sourceTag} route=auto sig=${shortSpeechSignature(phrase)} request=${requestId} token=${localTtsPlaybackToken} suppressMs=${suppressionWindowMs()}`);
      try {
        const serverSpoken = await speakWithServerTts(phrase, interrupt);
        if (serverSpoken) {
          return true;
        }
        return speakWithBrowserTts(phrase, interrupt);
      } finally {
        if (requestId === speechRequestSeq) {
          speechInFlight = false;
        }
      }
    }

    async function maybeSpeakFromState(data) {
      if (!STATE_POLL_SPEAK_ENABLED) return false;
      const outputId = Number(data.latest_output_action_id || 0);
      const text = String(data.latest_output_text || '').trim();
      const allowed = Boolean(data.latest_output_allowed);
      if (!allowed || !text || outputId <= 0) return false;
      if (outputId <= lastSpokenOutputId) return false;

      const rewritten = rewriteQueuedOutputText(text, data);
      if (!rewritten) {
        lastSpokenOutputId = outputId;
        localStorage.setItem('mim_last_spoken_output_id', String(outputId));
        return false;
      }

      const signature = normalizeSpeechSignature(rewritten);
      const now = Date.now();
      if (signature && signature === lastSpokenSignature && (now - lastSpokenSignatureAt) < SPOKEN_DUPLICATE_COOLDOWN_MS) {
        lastSpokenOutputId = outputId;
        localStorage.setItem('mim_last_spoken_output_id', String(outputId));
        return false;
      }

      if (await speakLocally(rewritten, true, 'state_poll_output')) {
        lastSpokenOutputId = outputId;
        localStorage.setItem('mim_last_spoken_output_id', String(outputId));
        lastSpokenSignature = signature;
        lastSpokenSignatureAt = now;
        localStorage.setItem('mim_last_spoken_signature', lastSpokenSignature);
        localStorage.setItem('mim_last_spoken_signature_at', String(lastSpokenSignatureAt));
        return true;
      }
      return false;
    }

    function persistIdentityMemory() {
      localStorage.setItem('mim_identity_language_memory', JSON.stringify(interactionMemory));
    }

    function persistGreetingCooldowns() {
      localStorage.setItem('mim_identity_greeting_cooldown', JSON.stringify(greetingCooldownByIdentity));
    }

    function normalizeIdentityLabel(raw) {
      const label = String(raw || '').trim().toLowerCase();
      if (!label) return '';
      if (['unknown', 'person', 'human', 'visitor', 'activity'].includes(label)) return '';
      return label;
    }

    function getPreferredInteractionLanguage() {
      if (!autoLanguageMode) {
        return normalizeLangCode(defaultListenLang || SYSTEM_DEFAULT_LANG);
      }

      const identity = normalizeIdentityLabel(activeVisualIdentity);
      if (identity) {
        const remembered = interactionMemory?.[identity]?.lang;
        if (remembered) {
          return normalizeLangCode(remembered);
        }
      }

      return normalizeLangCode(currentConversationLang || defaultListenLang || SYSTEM_DEFAULT_LANG);
    }

    function normalizeLangCode(raw) {
      const text = String(raw || '').trim();
      if (!text) return 'en-US';
      if (text.includes('-')) return text;
      if (text.length === 2) {
        const lower = text.toLowerCase();
        if (lower === 'en') return 'en-US';
        if (lower === 'es') return 'es-ES';
        if (lower === 'fr') return 'fr-FR';
        if (lower === 'de') return 'de-DE';
        if (lower === 'it') return 'it-IT';
        if (lower === 'pt') return 'pt-BR';
        if (lower === 'ja') return 'ja-JP';
        if (lower === 'ko') return 'ko-KR';
        if (lower === 'zh') return 'zh-CN';
      }
      return text;
    }

    function detectExplicitLanguageOverride(transcript) {
      const lower = ` ${String(transcript || '').toLowerCase()} `;
      const rules = [
        { lang: 'en-US', patterns: [' speak english ', ' use english ', ' english only '] },
        { lang: 'fr-FR', patterns: [' speak french ', ' use french ', ' en français ', ' francais '] },
        { lang: 'es-ES', patterns: [' speak spanish ', ' use spanish ', ' en español ', ' espanol '] },
        { lang: 'de-DE', patterns: [' speak german ', ' use german ', ' auf deutsch '] },
        { lang: 'it-IT', patterns: [' speak italian ', ' use italian ', ' in italiano '] },
        { lang: 'pt-BR', patterns: [' speak portuguese ', ' use portuguese ', ' em português ', ' portugues '] },
        { lang: 'ja-JP', patterns: [' speak japanese ', ' use japanese ', ' 日本語で '] },
        { lang: 'ko-KR', patterns: [' speak korean ', ' use korean ', ' 한국어로 '] },
        { lang: 'zh-CN', patterns: [' speak chinese ', ' use chinese ', ' 中文 '] },
      ];

      for (const rule of rules) {
        if (rule.patterns.some((pattern) => lower.includes(pattern))) {
          return rule.lang;
        }
      }

      return '';
    }

    function greetingForLanguage(identity, langCode) {
      const name = identity ? identity.charAt(0).toUpperCase() + identity.slice(1) : 'there';
      const lang = normalizeLangCode(langCode).toLowerCase();
      if (lang.startsWith('fr')) return `Bonjour ${name} — ravi de vous revoir.`;
      if (lang.startsWith('es')) return `Hola ${name}, me alegra verte.`;
      if (lang.startsWith('de')) return `Hallo ${name}, schön dich wiederzusehen.`;
      if (lang.startsWith('it')) return `Ciao ${name}, felice di rivederti.`;
      if (lang.startsWith('pt')) return `Olá ${name}, bom te ver novamente.`;
      if (lang.startsWith('ja')) return `${name}さん、またお会いできてうれしいです。`;
      if (lang.startsWith('ko')) return `${name}님, 다시 만나서 반가워요.`;
      if (lang.startsWith('zh')) return `${name}，很高兴再次见到你。`;
      return `Hello ${name}, great to see you again.`;
    }

    function extractNameAfterLeadIns(textRaw, leadIns) {
      const text = String(textRaw || '').toLowerCase();
      for (const leadIn of leadIns) {
        const idx = text.indexOf(leadIn);
        if (idx < 0) continue;
        const tail = text.slice(idx + leadIn.length)
          .replace(/[^a-z'\-\s]/g, ' ')
          .replace(/\s+/g, ' ')
          .trim();
        if (tail) return tail;
      }
      return '';
    }

    function extractIntroducedIdentity(transcript) {
      const text = String(transcript || '').trim();
      if (!text) return '';

      const candidate = extractNameAfterLeadIns(text, ['my name is ', "i'm ", 'i am ']);
      if (!candidate) return '';

      const filler = new Set(['hello', 'hi', 'hey', 'maam', 'mam', 'sir', 'there']);
      const pieces = candidate.split(' ').map((s) => s.trim()).filter(Boolean);
      const filtered = pieces.filter((part) => !filler.has(part) && !WEAK_IDENTITY_WORDS.has(part));
      if (!filtered.length) return '';

      const preferredSingle = filtered.find((part) => part.length >= 2 && part.length <= 24);
      if (preferredSingle && !WEAK_IDENTITY_WORDS.has(preferredSingle)) {
        return preferredSingle;
      }

      const merged = filtered.slice(0, 2).join(' ').trim();
      if (!merged || WEAK_IDENTITY_WORDS.has(merged)) return '';
      return merged;
    }

    function extractWeakIntroducedIdentity(transcript) {
      const text = String(transcript || '').trim();
      if (!text) return '';

      const candidate = extractNameAfterLeadIns(text, ['my name is ', "i'm ", 'i am ']);
      if (!candidate) return '';
      const parts = candidate.split(' ').map((s) => s.trim()).filter(Boolean);
      const weakPart = parts.find((part) => WEAK_IDENTITY_WORDS.has(part));
      if (weakPart) {
        return weakPart;
      }

      return '';
    }

    async function maybeHandleWeakIdentityIntroduction(transcript) {
      const weakIdentity = extractWeakIntroducedIdentity(transcript);
      if (!weakIdentity) return false;

      const now = Date.now();
      const promptKey = `weak:${weakIdentity}`;
      if (now < weakIdentityClarifyCooldownUntil && weakIdentityLastPromptKey === promptKey) {
        return true;
      }

      const clarification = buildDialogPrompt('low_value', { transcript });
      startupInquiryIssued = true;
      inquiryEl.textContent = clarification;
      await speakLocally(clarification, false, 'weak_identity_clarify');
      lastInquiryPromptSpoken = clarification;
      weakIdentityLastPromptKey = promptKey;
      weakIdentityClarifyCooldownUntil = now + 20000;
      return true;
    }

    async function acknowledgeIntroducedIdentity(identityRaw) {
      const normalized = normalizeIdentityLabel(identityRaw);
      if (!normalized || isUnknownOrMissingIdentity(normalized)) return false;

      const displayName = normalized.charAt(0).toUpperCase() + normalized.slice(1);
      const greeting = buildDialogPrompt('identity_ack', { name: displayName });
      startupInquiryIssued = true;
      locallyAcceptedIdentity = normalized;
      suppressBackendInquiryUntil = Date.now() + 120000;
      inquiryEl.textContent = greeting;
      await speakLocally(greeting, true, 'identity_ack');
      lastInquiryPromptSpoken = `identity:${normalized}`;

      activeVisualIdentity = normalized;
      interactionMemory[normalized] = {
        lang: currentConversationLang || defaultListenLang,
        updated_at: new Date().toISOString(),
        source: 'verbal_identity_intro',
      };
      persistIdentityMemory();

      cameraInput.value = normalized;
      try {
        await fetch('/gateway/perception/camera/events', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            device_id: 'mim-ui-camera',
            source_type: 'camera',
            session_id: 'mim-ui-session',
            is_remote: false,
            observations: [{ object_label: normalized, confidence: 0.92, zone: 'front-center' }],
            metadata_json: { source: 'mim_ui_sketch', reason: 'verbal_identity_intro' },
          }),
        });
      } catch (_) {
      }

      return true;
    }

    async function maybeHandleUnparsedIdentityIntent(transcript) {
      const text = String(transcript || '').toLowerCase();
      if (!text.includes('my name is')) {
        return false;
      }

      const parsed = extractIntroducedIdentity(transcript);
      if (parsed) {
        addMicDebug('identity-direct', `parsed=${parsed}`);
        return await acknowledgeIntroducedIdentity(parsed);
      }

      const tail = extractNameAfterLeadIns(String(transcript || ''), ['my name is ']);
      if (tail) {
        const filler = new Set(['hello', 'hi', 'hey', 'maam', 'mam', 'sir', 'there']);
        const parts = tail.split(' ').map((s) => s.trim()).filter(Boolean);
        const candidate = parts.find((part) => part.length >= 2 && part.length <= 24 && !filler.has(part) && !WEAK_IDENTITY_WORDS.has(part));
        if (candidate) {
          addMicDebug('identity-recovery', `candidate=${candidate}`);
          return await acknowledgeIntroducedIdentity(candidate);
        }
      }

      const now = Date.now();
      const promptKey = 'unparsed-name-intent';
      if (now < weakIdentityClarifyCooldownUntil && weakIdentityLastPromptKey === promptKey) {
        return true;
      }

      const clarification = buildDialogPrompt('uncertain_name', { transcript });
      startupInquiryIssued = true;
      inquiryEl.textContent = clarification;
      await speakLocally(clarification, true, 'identity_unparsed_clarify');
      lastInquiryPromptSpoken = clarification;
      weakIdentityLastPromptKey = promptKey;
      weakIdentityClarifyCooldownUntil = now + 20000;
      return true;
    }

    async function maybeHandleIdentityIntroduction(transcript) {
      const spokenIdentity = extractIntroducedIdentity(transcript);
      if (!spokenIdentity) return false;
      return await acknowledgeIntroducedIdentity(spokenIdentity);
    }

    async function maybeGreetRecognizedIdentity(identity) {
      const normalized = normalizeIdentityLabel(identity);
      if (!normalized) return;
      const rememberedLang = interactionMemory?.[normalized]?.lang;
      if (!rememberedLang) return;

      const now = Date.now();
      const lastAt = Number(greetingCooldownByIdentity?.[normalized] || 0);
      if (now - lastAt < 60000) return;

      greetingCooldownByIdentity[normalized] = now;
      persistGreetingCooldowns();

      const message = greetingForLanguage(normalized, rememberedLang);
      try {
        await fetch('/gateway/voice/output', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message,
            voice_profile: 'default',
            channel: 'ui',
            priority: 'normal',
            metadata_json: {
              source: 'mim_ui_sketch',
              reason: 'identity_language_greeting',
              identity: normalized,
              language: rememberedLang,
            },
          }),
        });
      } catch (_) {
      }
    }

    function isUnknownOrMissingIdentity(labelRaw) {
      const label = String(labelRaw || '').trim().toLowerCase();
      if (!label || label === '(none)') return true;
      return ['unknown', 'person', 'human', 'visitor', 'activity'].includes(label);
    }

    async function maybeIssueStartupIdentityInquiry(data) {
      if (startupInquiryIssued) return;

      const backendPrompt = String(data?.inquiry_prompt || '').trim();
      if (backendPrompt) {
        startupInquiryIssued = true;
        inquiryEl.textContent = backendPrompt;
        lastInquiryPromptSpoken = backendPrompt;
        return;
      }

      if (!isUnknownOrMissingIdentity(data?.camera_last_label) && !shouldAskForNameNow()) {
        startupInquiryIssued = true;
        return;
      }

      startupInquiryIssued = true;
      const startupPrompt = buildDialogPrompt('startup_identity');
      inquiryEl.textContent = startupPrompt;
      lastInquiryPromptSpoken = startupPrompt;
    }

    function detectLanguageFromTranscript(transcript) {
      const t = String(transcript || '').trim();
      if (!t) return defaultListenLang;

      if (/[\u3040-\u30ff]/.test(t)) return 'ja-JP';
      if (/[\uac00-\ud7af]/.test(t)) return 'ko-KR';
      if (/[\u4e00-\u9fff]/.test(t)) return 'zh-CN';
      if (/[а-яА-ЯЁё]/.test(t)) return 'ru-RU';

      const lower = t.toLowerCase();
      if (/[¿¡]/.test(t) || /( hola | gracias | por favor | buenos )/.test(` ${lower} `)) return 'es-ES';
      if (/( bonjour | merci | s'il vous plaît | salut )/.test(` ${lower} `)) return 'fr-FR';
      if (/( hallo | danke | bitte | guten )/.test(` ${lower} `)) return 'de-DE';
      if (/( olá | obrigado | obrigada | por favor )/.test(` ${lower} `)) return 'pt-BR';
      if (/( ciao | grazie | per favore )/.test(` ${lower} `)) return 'it-IT';

      return defaultListenLang;
    }

    function scoreVoice(voice, langPrefix) {
      const name = String(voice?.name || '').toLowerCase();
      const lang = String(voice?.lang || '').toLowerCase();
      let score = 0;

      if (lang.startsWith(langPrefix)) score += 80;
      else if (langPrefix === 'en' && lang.startsWith('en')) score += 50;

      if (voice?.default) score += 12;
      if (voice?.localService) score += 8;

      if (/(neural|natural|enhanced|premium|wavenet|studio|online|hq)/.test(name)) score += 30;
      if (/(siri|samantha|victoria|daniel|karen|moira|zira|aria|alloy|nova)/.test(name)) score += 15;
      if (/(espeak|compact|robot|test|default voice|mbrola|festival)/.test(name)) score -= 38;

      return score;
    }

    function resolveVoiceForLanguage(langCode) {
      if (!window.speechSynthesis) return null;
      const lang = normalizeLangCode(langCode).toLowerCase();
      const langPrefix = lang.split('-')[0];

      if (selectedVoiceURI) {
        const byUri = availableVoices.find((v) => v.voiceURI === selectedVoiceURI);
        if (byUri && byUri.lang && byUri.lang.toLowerCase().startsWith(langPrefix)) {
          return byUri;
        }
      }

      if (selectedVoiceName) {
        const byName = availableVoices.find((v) => v.name === selectedVoiceName && String(v.lang || '').toLowerCase().startsWith(langPrefix));
        if (byName) {
          return byName;
        }
      }

      const ranked = [...availableVoices].sort((a, b) => scoreVoice(b, langPrefix) - scoreVoice(a, langPrefix));
      if (ranked.length) {
        return ranked[0];
      }

      if (selectedVoiceURI) {
        const fallbackUri = availableVoices.find((v) => v.voiceURI === selectedVoiceURI);
        if (fallbackUri) return fallbackUri;
      }
      if (selectedVoiceName) {
        const fallbackName = availableVoices.find((v) => v.name === selectedVoiceName);
        if (fallbackName) return fallbackName;
      }

      return availableVoices[0] || null;
    }

    function applyVoiceSettings() {
      defaultListenLang = normalizeLangCode(defaultLangInput.value || defaultListenLang);
      defaultLangInput.value = defaultListenLang;
      localStorage.setItem('mim_default_listen_lang', defaultListenLang);

      autoLanguageMode = Boolean(autoLangToggle.checked);
      localStorage.setItem('mim_auto_lang_mode', autoLanguageMode ? '1' : '0');

      serverTtsEnabled = Boolean(serverTtsToggle.checked);
      localStorage.setItem('mim_server_tts_enabled', serverTtsEnabled ? '1' : '0');

      selectedServerTtsVoice = String(serverTtsVoiceSelect.value || selectedServerTtsVoice || '').trim() || 'en-US-EmmaMultilingualNeural';
      localStorage.setItem('mim_server_tts_voice', selectedServerTtsVoice);

      naturalVoicePreset = Boolean(naturalVoiceToggle.checked);
      localStorage.setItem('mim_voice_natural_preset', naturalVoicePreset ? '1' : '0');
      syncVoiceControlAvailability();

      if (!autoLanguageMode) {
        currentConversationLang = defaultListenLang;
        localStorage.setItem('mim_current_lang', currentConversationLang);
      }

      const uri = String(voiceSelect.value || '').trim();
      if (uri) {
        const matched = availableVoices.find((v) => v.voiceURI === uri);
        if (matched) {
          selectedVoiceURI = matched.voiceURI;
          selectedVoiceName = matched.name;
          localStorage.setItem('mim_voice_uri', selectedVoiceURI);
          localStorage.setItem('mim_voice_name', selectedVoiceName);
        }
      }

      const nextRate = Number(voiceRateInput.value || voiceRate);
      const nextPitch = Number(voicePitchInput.value || voicePitch);
      const nextDepth = Number(voiceDepthInput.value || voiceDepth);
      const nextVolume = Number(voiceVolumeInput.value || voiceVolume);

      voiceRate = clamp(Number.isFinite(nextRate) ? nextRate : 1.0, 0.7, 1.35);
      voicePitch = clamp(Number.isFinite(nextPitch) ? nextPitch : 1.0, 0.7, 1.35);
      voiceDepth = clamp(Number.isFinite(nextDepth) ? nextDepth : 0, 0, 100);
      voiceVolume = clamp(Number.isFinite(nextVolume) ? nextVolume : 1.0, 0.4, 1.0);

      voiceRateInput.value = voiceRate.toFixed(2);
      voicePitchInput.value = voicePitch.toFixed(2);
      voiceDepthInput.value = String(Math.round(voiceDepth));
      voiceVolumeInput.value = voiceVolume.toFixed(2);
      syncVoiceControlLabels();

      localStorage.setItem('mim_voice_rate', String(voiceRate));
      localStorage.setItem('mim_voice_pitch', String(voicePitch));
      localStorage.setItem('mim_voice_depth', String(voiceDepth));
      localStorage.setItem('mim_voice_volume', String(voiceVolume));

      if (recognition) {
        recognition.lang = defaultListenLang;
      }

      updateIconGlow();
    }

    async function ensureMicPermission(options = {}) {
      const keepStreamAlive = Boolean(options?.keepStreamAlive);
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        micPermissionState = 'unavailable';
        noteMicEvent('permission', 'mediaDevices unavailable');
        return false;
      }

      const audioConstraints = {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      };

      const preferredMic = resolvePreferredMicDevice();
      const preferredCandidates = [];
      if (preferredMic?.deviceId) {
        preferredCandidates.push(preferredMic);
      }
      if (selectedMicDeviceId && selectedMicDeviceId !== 'default' && selectedMicDeviceId !== 'communications') {
        const selectedExplicit = availableMics.find((d) => d.deviceId === selectedMicDeviceId);
        if (selectedExplicit && !preferredCandidates.some((d) => d.deviceId === selectedExplicit.deviceId)) {
          preferredCandidates.push(selectedExplicit);
        }
      }
      for (const mic of availableMics) {
        if (mic.deviceId && mic.deviceId !== 'default' && mic.deviceId !== 'communications' && !preferredCandidates.some((d) => d.deviceId === mic.deviceId)) {
          preferredCandidates.push(mic);
        }
      }

      try {
        let stream = null;
        let lastError = null;

        for (const candidate of preferredCandidates) {
          try {
            noteMicEvent('permission-route', candidate.label || candidate.deviceId);
            stream = await navigator.mediaDevices.getUserMedia({
              audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
                deviceId: { exact: candidate.deviceId },
              },
              video: false,
            });
            selectedMicDeviceId = candidate.deviceId;
            selectedMicLabel = candidate.label || selectedMicLabel || 'Microphone';
            localStorage.setItem('mim_mic_device_id', selectedMicDeviceId);
            localStorage.setItem('mim_mic_device_label', selectedMicLabel);
            break;
          } catch (candidateError) {
            lastError = candidateError;
          }
        }

        if (!stream) {
          try {
            noteMicEvent('permission-fallback', 'trying default');
            stream = await navigator.mediaDevices.getUserMedia({
              audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
              },
              video: false,
            });
            selectedMicDeviceId = 'default';
            selectedMicLabel = 'Default microphone';
            localStorage.setItem('mim_mic_device_id', selectedMicDeviceId);
            localStorage.setItem('mim_mic_device_label', selectedMicLabel);
          } catch (fallbackError) {
            throw (fallbackError || lastError || new Error('mic_open_failed'));
          }
        }

        if (!stream) {
          throw new Error('mic_stream_unavailable');
        }

        noteMicEvent('permission', 'granted');
        if (keepStreamAlive) {
          stopMicPermissionStream();
          micPermissionStream = stream;
          startMicKeepAliveMonitor();
          noteMicEvent('permission-stream', 'active');
        } else {
          try {
            for (const track of stream.getTracks()) {
              track.stop();
            }
          } catch (_) {
          }
          stopMicPermissionStream();
        }
        micPermissionState = 'granted';
        await enumerateMicDevices();
        updateMicDiagnostics();
        return true;
      } catch (error) {
        noteMicEvent('permission-error', String(error?.name || error?.message || 'unknown'));
        micPermissionState = 'denied';
        statusEl.textContent = 'Mic permission blocked. Allow microphone access for MIM Desktop.';
        healthState.micOk = false;
        updateIconGlow();
        return false;
      }
    }

    function buildVoiceOptions() {
      if (!window.speechSynthesis) return;
      availableVoices = window.speechSynthesis.getVoices() || [];
      availableVoices.sort((a, b) => {
        const aEn = String(a.lang || '').toLowerCase().startsWith('en') ? 0 : 1;
        const bEn = String(b.lang || '').toLowerCase().startsWith('en') ? 0 : 1;
        if (aEn !== bEn) return aEn - bEn;

        const langCompare = String(a.lang || '').localeCompare(String(b.lang || ''));
        if (langCompare !== 0) return langCompare;
        return String(a.name || '').localeCompare(String(b.name || ''));
      });
      voiceSelect.innerHTML = '';

      if (!availableVoices.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = 'System default voice (list unavailable)';
        voiceSelect.appendChild(option);
        healthState.voicesLoaded = false;
        healthState.voicesOk = false;
        updateIconGlow();
        return;
      }

      for (const voice of availableVoices) {
        const option = document.createElement('option');
        option.value = voice.voiceURI;
        option.textContent = `${voice.name} (${voice.lang})`;
        voiceSelect.appendChild(option);
      }
      healthState.voicesLoaded = true;
      healthState.voicesOk = true;

      let selected = availableVoices.find((v) => v.voiceURI === selectedVoiceURI);
      if (!selected && selectedVoiceName) {
        selected = availableVoices.find((v) => v.name === selectedVoiceName);
      }
      if (!selected) {
        selected = resolveVoiceForLanguage(defaultListenLang) || availableVoices.find((v) => String(v.lang || '').toLowerCase().startsWith('en')) || availableVoices[0];
      }

      selectedVoiceURI = selected.voiceURI;
      selectedVoiceName = selected.name;
      voiceSelect.value = selected.voiceURI;
      localStorage.setItem('mim_voice_uri', selectedVoiceURI);
      localStorage.setItem('mim_voice_name', selectedVoiceName);
      updateIconGlow();
    }

    function startVoiceRecoveryLoop() {
      if (voiceRecoveryInterval) return;
      voiceRecoveryInterval = setInterval(() => {
        if (!window.speechSynthesis || healthState.voicesLoaded) {
          if (healthState.voicesLoaded && voiceRecoveryInterval) {
            clearInterval(voiceRecoveryInterval);
            voiceRecoveryInterval = null;
          }
          return;
        }
        voiceRecoveryAttempts += 1;
        buildVoiceOptions();
        if (healthState.voicesLoaded && voiceRecoveryInterval) {
          clearInterval(voiceRecoveryInterval);
          voiceRecoveryInterval = null;
          return;
        }
        if (voiceRecoveryAttempts >= 45) {
          clearInterval(voiceRecoveryInterval);
          voiceRecoveryInterval = null;
        }
      }, 1000);
    }

    async function refreshState() {
      if (refreshInFlight) {
        refreshPending = true;
        return;
      }

      refreshInFlight = true;
      try {
        const res = await fetch('/mim/ui/state');
        if (!res.ok) {
          markBackendReachability(false);
          updateIconGlow();
          return;
        }
        markBackendReachability(true);
        const data = await res.json();
        latestUiState = data;
        if (buildTagEl) {
          const runtimeBuild = String(data.runtime_build || 'mim-ui');
          buildTagEl.textContent = `Build: ${runtimeBuild}`;
        }
        setSpeaking(Boolean(data.speaking));
        const spokeFromState = await maybeSpeakFromState(data).catch(() => false);
        maybeIssueStartupIdentityInquiry(data);

        const cameraLabel = data.camera_last_label || '(none)';
        const cameraConfidence = Number(data.camera_last_confidence || 0).toFixed(2);
        cameraEl.textContent = `Camera: ${cameraLabel} (confidence ${cameraConfidence})`;
        const inquiryPrompt = String(data.inquiry_prompt || '').trim();
        const conversationContext = (data && typeof data.conversation_context === 'object') ? data.conversation_context : {};
        const operatorReasoning = (data && typeof data.operator_reasoning === 'object') ? data.operator_reasoning : {};
        renderObjectMemoryPanel(conversationContext);
        renderSystemReasoningPanel(operatorReasoning);
        await maybeRecoverRuntimeHealth(data);
        const cameraIdentityKnown = !isUnknownOrMissingIdentity(cameraLabel);
        const shouldSuppressInquiryReplay = Date.now() < suppressBackendInquiryUntil && (Boolean(locallyAcceptedIdentity) || cameraIdentityKnown);
        if (inquiryPrompt && !shouldSuppressInquiryReplay && !spokeFromState) {
          inquiryEl.textContent = inquiryPrompt;
          lastInquiryPromptSpoken = inquiryPrompt;
        } else if (shouldSuppressInquiryReplay && isIdentityInquiryText(inquiryEl.textContent)) {
          inquiryEl.textContent = locallyAcceptedIdentity
            ? `Nice to meet you, ${locallyAcceptedIdentity.charAt(0).toUpperCase()}${locallyAcceptedIdentity.slice(1)}.`
            : inquiryEl.textContent;
        } else if (!startupInquiryIssued) {
          inquiryEl.textContent = '';
          lastInquiryPromptSpoken = '';
        }

        activeVisualIdentity = normalizeIdentityLabel(cameraLabel);
        if (activeVisualIdentity) {
          locallyAcceptedIdentity = activeVisualIdentity;
        }
        if (activeVisualIdentity && activeVisualIdentity !== lastVisualIdentity) {
          maybeGreetRecognizedIdentity(activeVisualIdentity);
        }
        lastVisualIdentity = activeVisualIdentity;

        updateIconGlow();
      } catch (_) {
        markBackendReachability(false);
        updateIconGlow();
      } finally {
        refreshInFlight = false;
        if (refreshPending) {
          refreshPending = false;
          setTimeout(() => {
            refreshState();
          }, 0);
        }
      }
    }

    async function speakNow() {
      const message = sayInput.value.trim();
      if (!message) return;

      const handledWebOrCapabilityText = await maybeHandleWebOrCapabilityCommand(message, 'typed_input');
      if (handledWebOrCapabilityText) {
        refreshState();
        return;
      }

      const localSpoken = await speakLocally(message, true, 'typed_input');
      if (!localSpoken) {
        const detail = lastLocalTtsError ? ` (${lastLocalTtsError})` : '';
        statusEl.textContent = `Speak requested, but local TTS is unavailable${detail}.`;
      }
      refreshState();
    }

    async function sendCameraEvent() {
      const label = (cameraInput.value || '').trim() || 'unknown';
      await fetch('/gateway/perception/camera/events', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          device_id: 'mim-ui-camera',
          source_type: 'camera',
          session_id: 'mim-ui-session',
          is_remote: false,
          observations: [{ object_label: label, confidence: 0.82, zone: 'front-center' }],
          metadata_json: { source: 'mim_ui_sketch' },
        }),
      });
      refreshState();
    }

    async function listenOnce() {
      const hardMicErrors = new Set([
        'not-allowed',
        'service-not-allowed',
        'audio-capture',
        'bad-grammar',
        'language-not-supported',
      ]);
      const softMicErrors = new Set([
        'aborted',
        'no-speech',
        'network',
      ]);

      const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
      if (!SpeechRecognition) {
        healthState.micAvailable = false;
        healthState.micOk = false;
        micAutoMode = false;
        updateIconGlow();
        return;
      }

      const micReady = await ensureMicPermission({ keepStreamAlive: FORCE_FALLBACK_STT });
      if (!micReady) {
        micAutoMode = false;
        listenBtn.textContent = 'Listening Off';
        return;
      }

      if (FORCE_FALLBACK_STT) {
        micListening = true;
        micStartInFlight = false;
        micRestartPending = false;
        micLastActiveAt = Date.now();
        healthState.micAvailable = true;
        healthState.micOk = true;
        micLastErrorCode = '';
        statusEl.textContent = 'Always listening...';
        startMicKeepAliveMonitor();
        noteMicEvent('fallback', 'forced-mode-active');
        startMicFallbackLoop();
        updateIconGlow();
        return;
      }

      if (micRecoveryMode) {
        if (Date.now() < micCooldownUntil) {
          updateIconGlow();
          return;
        }
        micRecoveryMode = false;
        micRecoveryReason = '';
      }

      healthState.micAvailable = true;

      resetRecognitionInstance();
      if (!recognition) {
        recognition = new SpeechRecognition();
        recognition.lang = defaultListenLang;
        recognition.interimResults = false;
        recognition.maxAlternatives = 1;
        recognition.continuous = true;

        recognition.onstart = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onstart');
          stopMicPermissionStream();
          stopMicFallbackLoop();
          clearMicStartTimeout();
          micStartInFlight = false;
          micRestartPending = false;
          micListening = true;
          micStartAttemptStreak = 0;
          micStartTimeoutStreak = 0;
          micStartFailureStreak = 0;
          micSessionStartedAt = Date.now();
          micLastActiveAt = Date.now();
          micLastSpeechEventAt = Date.now();
          healthState.micOk = true;
          micLastErrorCode = '';
          if (runtimeRecoveryPending.microphone && micRecoveryReason === 'runtime-health-stale') {
            void postRuntimeRecoveryEvent(
              'microphone',
              'recovery_succeeded',
              'Microphone recognition recovered after backend stale signal.',
              runtimeRecoveryPending.microphone.nextRetryAt || null,
              { status: 'healthy' },
            );
            runtimeRecoveryPending.microphone = null;
          }
          statusEl.textContent = 'Always listening...';
          updateIconGlow();
        };

        recognition.onaudiostart = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onaudiostart');
          micLastSpeechEventAt = Date.now();
        };

        recognition.onaudioend = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onaudioend');
        };

        recognition.onsoundstart = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onsoundstart');
          micLastSpeechEventAt = Date.now();
        };

        recognition.onsoundend = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onsoundend');
        };

        recognition.onspeechstart = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onspeechstart');
          micLastSpeechEventAt = Date.now();
        };

        recognition.onspeechend = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onspeechend');
        };

        recognition.onnomatch = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onnomatch');
        };

        recognition.onresult = async (event) => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onresult');
          micLastSpeechEventAt = Date.now();
          micLastResultAt = Date.now();
          const last = event.results?.[event.results.length - 1]?.[0];
          const transcript = (last?.transcript || '').trim();
          const confidence = Number(last?.confidence || 0.8);
          if (!transcript) return;
          if (isMicSuppressedNow()) {
            noteMicEvent('recognition-drop', 'tts-suppressed');
            addMicDebug('recognition:drop-suppressed', transcript.slice(0, 48));
            logTranscriptDrop('suppressed', transcript, 'always_listening');
            return;
          }
          if (isLikelyEchoTranscript(transcript)) {
            noteMicEvent('recognition-echo-drop', transcript.slice(0, 24));
            addMicDebug('recognition:echo-drop', transcript.slice(0, 48));
            logTranscriptDrop('echo', transcript, 'always_listening');
            return;
          }

          micConsecutiveOnend = 0;
          micErrorStreak = 0;
          micHardErrorStreak = 0;
          micStartInFlight = false;
          micRestartPending = false;
          micLastActiveAt = Date.now();
          healthState.micOk = true;
          micLastErrorCode = '';

          const explicitOverrideLang = detectExplicitLanguageOverride(transcript);
          if (explicitOverrideLang) {
            currentConversationLang = normalizeLangCode(explicitOverrideLang);
            localStorage.setItem('mim_current_lang', currentConversationLang);

            if (activeVisualIdentity) {
              interactionMemory[activeVisualIdentity] = {
                lang: currentConversationLang,
                updated_at: new Date().toISOString(),
                source: 'verbal_override',
              };
              persistIdentityMemory();
            }
          } else if (autoLanguageMode) {
            currentConversationLang = detectLanguageFromTranscript(transcript);
            localStorage.setItem('mim_current_lang', currentConversationLang);

            if (activeVisualIdentity) {
              interactionMemory[activeVisualIdentity] = {
                lang: currentConversationLang,
                updated_at: new Date().toISOString(),
                source: 'detected_input',
              };
              persistIdentityMemory();
            }
          }

          const isLowValueRecognition = isLikelyLowValueTranscript(transcript);
          const micSync = await submitMicTranscript(
            transcript,
            isLowValueRecognition ? Math.min(confidence, 0.33) : confidence,
            isLowValueRecognition ? 'always_listening_short' : 'always_listening',
          );
          if (!micSync.ok) {
            statusEl.textContent = `Heard: ${transcript} (backend sync delayed)`;
          } else if (!micSync.accepted) {
            statusEl.textContent = `Heard: ${transcript} (${micSync.status})`;
          }

          if (isLowValueRecognition) {
            noteMicEvent('recognition-short', transcript.slice(0, 24));
            addMicDebug('recognition:short-transcript-forwarded', transcript);
            logTranscriptDrop('low_value', transcript, 'always_listening');
            await maybeHandleLowValueTranscript(transcript, 'always_listening');
            refreshState();
            return;
          }

          const handledWebOrCapabilityRecognition = await maybeHandleWebOrCapabilityCommand(transcript, 'always_listening');
          if (handledWebOrCapabilityRecognition) {
            refreshState();
            return;
          }

          statusEl.textContent = `Heard: ${transcript}`;
          const wakePresent = hasWakePhrase(transcript);
          if (WAKE_WORD_REQUIRED_FOR_LIVE_REPLY && !wakePresent) {
            addMicDebug('wake-gate-drop', `mode=recognition transcript=${transcript.slice(0, 40)}`);
            logTranscriptDrop('no_wake', transcript, 'always_listening');
            statusEl.textContent = 'Listening... (wake word required: "MIM")';
            refreshState();
            return;
          }
          const handledGreetingOnly = await maybeHandleGreetingWithoutIntent(transcript);
          if (!handledGreetingOnly) {
            const handledWeakIdentity = await maybeHandleWeakIdentityIntroduction(transcript);
            if (!handledWeakIdentity) {
              const handledUnparsedIdentityIntent = await maybeHandleUnparsedIdentityIntent(transcript);
              if (!handledUnparsedIdentityIntent) {
                const handledIdentity = await maybeHandleIdentityIntroduction(transcript);
                if (!handledIdentity) {
                  const handledStandaloneName = await maybeHandleStandaloneNameDuringStartup(transcript);
                  if (!handledStandaloneName) {
                    await maybeHandleStartupUncertainTranscript(transcript);
                  }
                }
              }
            }
          }

          refreshState();
        };

        recognition.onerror = (event) => {
          noteMicLifecycleEvent();
          stopMicPermissionStream();
          stopMicFallbackLoop();
          clearMicStartTimeout();
          micListening = false;
          micStartInFlight = false;
          micSessionStartedAt = 0;
          micRecentErrorAt = Date.now();
          const errorCode = String(event?.error || 'unknown');
          const errorMessage = String(event?.message || '').trim();
          const detail = errorMessage && errorMessage !== errorCode ? `${errorCode}:${errorMessage}` : errorCode;
          noteMicEvent('recognition-error', detail);
          addMicDebug('recognition:onerror', `code=${errorCode} message=${errorMessage || '-'} autoMode=${micAutoMode} listening=${micListening}`);
          micLastErrorCode = errorCode;
          const isHardError = hardMicErrors.has(errorCode);
          if (runtimeRecoveryPending.microphone && errorCode !== 'aborted') {
            void postRuntimeRecoveryEvent(
              'microphone',
              'recovery_failed',
              `Microphone recovery hit ${detail}.`,
              runtimeRecoveryPending.microphone.nextRetryAt || null,
              { error_code: errorCode },
            );
          }

          if (isHardError) {
            micHardErrorStreak += 1;
            healthState.micOk = false;
            micErrorStreak += 1;
          } else if (softMicErrors.has(errorCode)) {
            micHardErrorStreak = 0;
            healthState.micOk = true;
            micLastErrorCode = '';
            micErrorStreak = 0;
          } else {
            micHardErrorStreak += 1;
            healthState.micOk = micHardErrorStreak < 3;
            micErrorStreak += 1;
          }
          updateIconGlow();
          if (isHardError) {
            if (noteMicCycleAndMaybeRecover(`hard-error:${errorCode}`)) {
              return;
            }
          }
          if (micAutoMode) {
            const backoffMs = softMicErrors.has(errorCode)
              ? 450
              : Math.min(20000, 2500 + micErrorStreak * 1200);
            scheduleMicRetry(backoffMs);
          } else {
            micRestartPending = false;
            statusEl.textContent = 'Listening failed. Try again.';
          }
        };

        recognition.onend = () => {
          noteMicLifecycleEvent();
          noteMicEvent('recognition', 'onend');
          stopMicPermissionStream();
          stopMicFallbackLoop();
          clearMicStartTimeout();
          const sessionDuration = micSessionStartedAt > 0 ? (Date.now() - micSessionStartedAt) : 0;
          micSessionStartedAt = 0;
          micListening = false;
          micStartInFlight = false;
          micConsecutiveOnend += 1;
          if (micHardErrorStreak === 0) {
            healthState.micOk = true;
          }

          if (sessionDuration > 0 && sessionDuration < MIC_SHORT_RUN_MS) {
            micShortRunStreak += 1;
          } else {
            micShortRunStreak = 0;
          }

          if (micShortRunStreak >= MIC_SHORT_RUN_LIMIT && micAutoMode) {
            micUnstableCycleCount += 1;
            micShortRunStreak = 0;
            if (runtimeRecoveryPending.microphone) {
              void postRuntimeRecoveryEvent(
                'microphone',
                'recovery_failed',
                'Microphone recovery entered short-run flap protection.',
                runtimeRecoveryPending.microphone.nextRetryAt || null,
                { unstable_cycle_count: micUnstableCycleCount },
              );
            }
            enterMicRecovery('short-run-flap');
            return;
          }

          if (micConsecutiveOnend <= 2) {
            micLastActiveAt = Date.now();
          }
          updateIconGlow();
          if (micAutoMode) {
            const shouldUseFallback = micErrorStreak > 0 || micHardErrorStreak > 0 || micConsecutiveOnend > 2;
            if (shouldUseFallback) {
              captureFallbackTranscription();
            }
            const backoffMs = micErrorStreak > 0 ? Math.min(15000, 1500 + micErrorStreak * 800) : 350;
            scheduleMicRetry(backoffMs);
          } else {
            micRestartPending = false;
            listenBtn.textContent = 'Listening Off';
            statusEl.textContent = 'Listening paused. Press Listen to start.';
          }
        };
      }

      if (micListening || micStartInFlight) return;
      try {
        micStartInFlight = true;
        micRestartPending = false;
        micStartAttemptStreak += 1;
        noteMicLifecycleEvent();
        clearMicStartTimeout();
        micStartTimeoutTimer = setTimeout(() => {
          if (!micStartInFlight && micListening) return;
          noteMicEvent('recognition-timeout', 'start');
          stopMicPermissionStream();
          micStartInFlight = false;
          micListening = false;
          micLastErrorCode = 'start-timeout';
          micErrorStreak += 1;
          micStartTimeoutStreak += 1;
          healthState.micOk = micErrorStreak < 3;
          statusEl.textContent = 'Mic startup timed out.';
          resetRecognitionInstance();
          updateIconGlow();
          if (micStartTimeoutStreak >= 2) {
            pauseMicAuto('Mic startup unstable. Auto-listen paused; press Listen to retry.');
            return;
          }
          if (micAutoMode) {
            scheduleMicRetry(Math.min(12000, 1800 + micErrorStreak * 900));
          } else {
            listenBtn.textContent = 'Listening Off';
            statusEl.textContent = 'Mic did not start. Press Listen to retry.';
          }
        }, 2600);
        recognition.start();
        micLastActiveAt = Date.now();
        healthState.micOk = true;
        micLastErrorCode = '';
        micRecoveryMode = false;
        micRecoveryReason = '';
        micErrorStreak = 0;
        micHardErrorStreak = 0;
        micShortRunStreak = 0;
        micUnstableCycleCount = 0;
        statusEl.textContent = 'Starting microphone...';
        updateIconGlow();
      } catch (error) {
        stopMicPermissionStream();
        clearMicStartTimeout();
        micStartInFlight = false;
        micRestartPending = true;
        micLastErrorCode = 'start-failed';
        micHardErrorStreak += 1;
        micStartFailureStreak += 1;
        healthState.micOk = micHardErrorStreak < 3;
        micErrorStreak += 1;
        if (micStartAttemptStreak >= 3) {
          resetRecognitionInstance();
          micStartAttemptStreak = 0;
        }
        statusEl.textContent = `Mic start failed (${String(error?.name || 'unknown')}).`;
        updateIconGlow();
        if (micStartFailureStreak >= 2) {
          pauseMicAuto('Mic failed to start repeatedly. Auto-listen paused; press Listen to retry.');
          return;
        }
        if (micAutoMode) {
          scheduleMicRetry(Math.min(12000, 1200 + micErrorStreak * 900));
        } else {
          listenBtn.textContent = 'Listening Off';
        }
      }
    }

    function stopCameraWatcher() {
      if (motionInterval) {
        clearInterval(motionInterval);
        motionInterval = null;
      }

      if (cameraWatcherVideo) {
        try {
          cameraWatcherVideo.pause();
        } catch (_) {
        }
        cameraWatcherVideo.srcObject = null;
      }

      if (cameraStream) {
        try {
          for (const track of cameraStream.getTracks()) {
            try {
              track.stop();
            } catch (_) {
            }
          }
        } catch (_) {
        }
      }

      cameraStream = null;
      cameraWatcherVideo = null;
      cameraWatcherCanvas = null;
      cameraWatcherCtx = null;
      cameraLastFrame = null;
      cameraLastSentAt = 0;
      cameraLastHeartbeatAt = 0;
      cameraWatcherStartedAt = 0;
      cameraLastFrameSeenAt = 0;
      cameraLastHealthyFrameAt = 0;

      if (cameraPreview) {
        cameraPreview.srcObject = null;
      }
      updateCameraSettingsUi();
    }

    async function startCameraWatcher() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        cameraEl.textContent = 'Camera: browser camera API not available';
        cameraSettingsStatus.textContent = 'Camera API is unavailable in this runtime.';
        healthState.cameraOk = false;
        updateCameraSettingsUi();
        updateIconGlow();
        return;
      }

      stopCameraWatcher();

      try {
        const videoConstraints = { facingMode: 'user' };
        if (selectedCameraDeviceId) {
          videoConstraints.deviceId = { exact: selectedCameraDeviceId };
        }
        cameraStream = await navigator.mediaDevices.getUserMedia({ video: videoConstraints, audio: false });
        const firstTrack = cameraStream.getVideoTracks ? cameraStream.getVideoTracks()[0] : null;
        if (firstTrack) {
          const settings = firstTrack.getSettings ? firstTrack.getSettings() : {};
          const resolvedDeviceId = String(settings?.deviceId || selectedCameraDeviceId || '').trim();
          if (resolvedDeviceId) {
            selectedCameraDeviceId = resolvedDeviceId;
            localStorage.setItem('mim_camera_device_id', selectedCameraDeviceId);
          }
        }

        cameraWatcherVideo = document.createElement('video');
        cameraWatcherVideo.srcObject = cameraStream;
        cameraWatcherVideo.muted = true;
        cameraWatcherVideo.playsInline = true;
        await cameraWatcherVideo.play();
        cameraWatcherStartedAt = Date.now();

        cameraPreview.srcObject = cameraStream;
        try {
          await cameraPreview.play();
        } catch (_) {
        }

        cameraWatcherCanvas = document.createElement('canvas');
        cameraWatcherCtx = cameraWatcherCanvas.getContext('2d', { willReadFrequently: true });
        const width = 96;
        const height = 72;
        cameraWatcherCanvas.width = width;
        cameraWatcherCanvas.height = height;

        const postCameraActivity = async (activityScore) => {
          await fetch('/gateway/perception/camera/events', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              device_id: 'mim-ui-camera',
              source_type: 'camera',
              session_id: 'mim-ui-session',
              is_remote: false,
              observations: [{ object_label: 'activity', confidence: Math.max(0.5, Math.min(0.98, activityScore)), zone: 'front-center' }],
              metadata_json: { source: 'mim_ui_sketch', mode: 'always_watching' },
            }),
          });
          cameraLastHealthyFrameAt = Date.now();
          cameraLastHeartbeatAt = cameraLastHealthyFrameAt;
          refreshState();
        };

        const postCameraHeartbeat = async () => {
          await fetch('/gateway/perception/camera/events', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              device_id: 'mim-ui-camera',
              source_type: 'camera',
              session_id: 'mim-ui-session',
              is_remote: false,
              observations: [],
              min_interval_seconds: 0,
              duplicate_window_seconds: 1,
              metadata_json: { source: 'mim_ui_sketch', mode: 'always_watching_heartbeat' },
            }),
          });
          cameraLastHealthyFrameAt = Date.now();
          cameraLastHeartbeatAt = cameraLastHealthyFrameAt;
          refreshState();
        };

        motionInterval = setInterval(async () => {
          if (!cameraWatcherCtx || !cameraWatcherVideo || cameraWatcherVideo.readyState < 2) return;
          cameraLastFrameSeenAt = Date.now();
          cameraWatcherCtx.drawImage(cameraWatcherVideo, 0, 0, width, height);
          const frame = cameraWatcherCtx.getImageData(0, 0, width, height).data;

          if (!cameraLastFrame) {
            cameraLastFrame = new Uint8ClampedArray(frame);
            return;
          }

          let delta = 0;
          const stride = 16;
          for (let i = 0; i < frame.length; i += stride) {
            delta += Math.abs(frame[i] - cameraLastFrame[i]);
          }
          const samples = Math.floor(frame.length / stride);
          const avgDelta = samples > 0 ? delta / samples : 0;
          const normalized = Math.max(0, Math.min(1, avgDelta / 40));

          cameraLastFrame.set(frame);
          const now = Date.now();
          if (normalized >= 0.18 && now - cameraLastSentAt >= 1200) {
            cameraLastSentAt = now;
            cameraEl.textContent = `Camera: activity detected (${normalized.toFixed(2)})`;
            await postCameraActivity(normalized);
            return;
          }

          if (now - cameraLastHeartbeatAt >= CAMERA_HEARTBEAT_MS) {
            cameraEl.textContent = 'Camera: watcher alive (heartbeat)';
            await postCameraHeartbeat();
          }
        }, 900);

        await enumerateCameraDevices();

        const activeLabel = firstTrack?.label ? ` (${firstTrack.label})` : '';
        cameraSettingsStatus.textContent = `Camera preview live${activeLabel}.`;
        cameraEl.textContent = 'Camera: always watching for activity';
        healthState.cameraOk = true;
        updateCameraSettingsUi();
        updateIconGlow();
      } catch (_) {
        if (runtimeRecoveryPending.camera) {
          await postRuntimeRecoveryEvent(
            'camera',
            'recovery_failed',
            'Camera watcher restart failed.',
            runtimeRecoveryPending.camera.nextRetryAt || null,
            buildCameraRecoveryMetadata({
              status: 'error',
              recovery_attempted_at: runtimeRecoveryPending.camera.startedAt || null,
              retry_reason: runtimeRecoveryPending.camera.retryReason || 'watcher_not_running',
              retry_reason_detail: runtimeRecoveryPending.camera.retryReasonDetail || 'Camera watcher restart failed before sustained healthy frames returned.',
            }),
          );
          runtimeRecoveryPending.camera = null;
        }
        stopCameraWatcher();
        cameraEl.textContent = 'Camera permission denied or unavailable';
        cameraSettingsStatus.textContent = 'Unable to start camera. Check permission and selected device.';
        healthState.cameraOk = false;
        updateCameraSettingsUi();
        updateIconGlow();
      }
    }

    document.getElementById('speakBtn').addEventListener('click', speakNow);
    document.getElementById('cameraBtn').addEventListener('click', sendCameraEvent);
    settingsBtn.addEventListener('click', () => {
      settingsPanel.classList.toggle('open');
    });
    settingsTabVoice.addEventListener('click', () => setSettingsTab('voice'));
    settingsTabCamera.addEventListener('click', () => setSettingsTab('camera'));
    cameraSelect.addEventListener('change', async () => {
      selectedCameraDeviceId = String(cameraSelect.value || '').trim();
      localStorage.setItem('mim_camera_device_id', selectedCameraDeviceId);
      cameraSettingsStatus.textContent = 'Switching camera...';
      await startCameraWatcher();
    });
    cameraRefreshBtn.addEventListener('click', async () => {
      cameraSettingsStatus.textContent = 'Refreshing camera list...';
      await enumerateCameraDevices();
    });
    cameraToggleBtn.addEventListener('click', async () => {
      if (cameraStream && cameraStream.active) {
        stopCameraWatcher();
        cameraEl.textContent = 'Camera: preview stopped by user';
        healthState.cameraOk = false;
        cameraSettingsStatus.textContent = 'Camera preview stopped.';
        updateIconGlow();
        return;
      }
      cameraSettingsStatus.textContent = 'Starting camera preview...';
      await startCameraWatcher();
    });
    voiceSelect.addEventListener('change', applyVoiceSettings);
    serverTtsToggle.addEventListener('change', applyVoiceSettings);
    serverTtsVoiceSelect.addEventListener('change', applyVoiceSettings);
    micSelect.addEventListener('change', async () => {
      selectedMicDeviceId = String(micSelect.value || '').trim();
      const selected = availableMics.find((d) => d.deviceId === selectedMicDeviceId);
      selectedMicLabel = selected?.label || selectedMicLabel;
      localStorage.setItem('mim_mic_device_id', selectedMicDeviceId);
      localStorage.setItem('mim_mic_device_label', selectedMicLabel || '');
      micPermissionState = 'unknown';
      updateMicDiagnostics();

      resetRecognitionInstance();
      await ensureMicPermission();
      if (micAutoMode) {
        listenOnce();
      }
    });
    defaultLangInput.addEventListener('change', applyVoiceSettings);
    autoLangToggle.addEventListener('change', applyVoiceSettings);
    naturalVoiceToggle.addEventListener('change', applyVoiceSettings);
    voiceRateInput.addEventListener('input', applyVoiceSettings);
    voicePitchInput.addEventListener('input', applyVoiceSettings);
    voiceDepthInput.addEventListener('input', applyVoiceSettings);
    voiceVolumeInput.addEventListener('input', applyVoiceSettings);
    listenBtn.addEventListener('click', () => {
      if (micAutoMode || micListening || micStartInFlight) {
        micAutoMode = false;
        listenBtn.textContent = 'Listening Off';
        micListening = false;
        micStartInFlight = false;
        micRestartPending = false;
        stopMicFallbackLoop();
        if (micRetryTimer) {
          clearTimeout(micRetryTimer);
          micRetryTimer = null;
        }
        clearMicStartTimeout();
        stopMicFallbackLoop();
        stopMicPermissionStream();
        if (recognition && micListening) {
          recognition.stop();
        }
        statusEl.textContent = 'Listening paused.';
        updateIconGlow();
        return;
      }

      micAutoMode = true;
      listenBtn.textContent = 'Listening On';
      micRestartPending = false;
      micStartTimeoutStreak = 0;
      micStartFailureStreak = 0;
      micShortRunStreak = 0;
      micUnstableCycleCount = 0;
      listenOnce();
    });
    chatSendBtn.addEventListener('click', sendTextChat);
    chatClearBtn.addEventListener('click', () => {
      if (!chatLog) return;
      chatLog.innerHTML = '';
      appendChatMessage('mim', 'Text chat cleared. Ready for your next message.');
    });
    chatInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendTextChat();
      }
    });

    updateIconGlow();
    addMicDebug('ui-boot', 'mim-ui-tightened-v1');
    if (micEventEl) {
      micEventEl.textContent = `Mic event: ui-boot @ ${new Date().toLocaleTimeString()}`;
    }
    defaultLangInput.value = normalizeLangCode(defaultListenLang || SYSTEM_DEFAULT_LANG);
    autoLangToggle.checked = autoLanguageMode;
    serverTtsToggle.checked = serverTtsEnabled;
    naturalVoiceToggle.checked = naturalVoicePreset;
    voiceRateInput.value = clamp(voiceRate, 0.7, 1.35).toFixed(2);
    voicePitchInput.value = clamp(voicePitch, 0.7, 1.35).toFixed(2);
    voiceDepthInput.value = String(Math.round(clamp(voiceDepth, 0, 100)));
    voiceVolumeInput.value = clamp(voiceVolume, 0.4, 1.0).toFixed(2);
    syncVoiceControlAvailability();
    syncVoiceControlLabels();
    enumerateMicDevices();
    enumerateCameraDevices();
    buildVoiceOptions();
    buildServerTtsVoiceOptions();
    startVoiceRecoveryLoop();
    if (window.speechSynthesis) {
      window.speechSynthesis.onvoiceschanged = () => {
        buildVoiceOptions();
        applyVoiceSettings();
      };
    }
    setSettingsTab('voice');
    updateCameraSettingsUi();
    applyVoiceSettings();
    refreshState();
    listenBtn.textContent = 'Listening Off';
    statusEl.textContent = 'Listening paused. Press Listen to start.';
    ensureMicPermission().then(() => enumerateMicDevices());
    startCameraWatcher();
    setInterval(refreshState, 2000);
  </script>
</body>
</html>
"""


@router.get("/mim/ui/state")
async def mim_ui_state(db: AsyncSession = Depends(get_db)) -> dict:
    now = datetime.now(timezone.utc)

    speech_row = (
        (
            await db.execute(
                select(SpeechOutputAction)
                .order_by(SpeechOutputAction.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    speaking = False
    if speech_row and speech_row.created_at:
        age_seconds = (
            now - speech_row.created_at.astimezone(timezone.utc)
        ).total_seconds()
        speaking = age_seconds <= 8

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

    mic_row = (
        (
            await db.execute(
                select(WorkspacePerceptionSource)
                .where(WorkspacePerceptionSource.source_type == "microphone")
                .order_by(
            WorkspacePerceptionSource.last_seen_at.desc().nullslast(),
                    WorkspacePerceptionSource.id.desc(),
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    camera_rows = (
        (
            await db.execute(
                select(WorkspacePerceptionSource)
                .where(WorkspacePerceptionSource.source_type == "camera")
                .order_by(
                WorkspacePerceptionSource.last_seen_at.desc().nullslast(),
                    WorkspacePerceptionSource.id.desc(),
                )
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    latest_input_event = (
        (
            await db.execute(
            select(InputEvent)
            .where(InputEvent.source.in_(["text", "voice", "ui", "api"]))
            .order_by(InputEvent.id.desc())
            .limit(1)
            )
        )
        .scalars()
        .first()
    )
    active_perception_session_id = _resolve_active_perception_session(
        camera_rows=camera_rows,
        mic_row=mic_row,
        now=now,
        fallback_session_id=_extract_conversation_session_id(
            latest_input_event.metadata_json if latest_input_event else {}
        ),
    )
    if active_perception_session_id:
        session_camera_rows = [
            row
            for row in camera_rows
            if str(row.session_id or "").strip() == active_perception_session_id
        ]
        if session_camera_rows:
            camera_rows = session_camera_rows

    fresh_camera_observations = collect_fresh_camera_observations(
        camera_rows,
        now=now,
        stale_seconds=90.0,
    )
    camera_summary = summarize_camera_observations(fresh_camera_observations)
    label_raw = str(camera_summary.get("primary_label", "")).strip()
    label = label_raw.lower()
    confidence = float(camera_summary.get("primary_confidence", 0.0) or 0.0)
    camera_scene_summary = str(camera_summary.get("summary", "")).strip()
    camera_source_count = int(camera_summary.get("source_count", 0) or 0)
    latest_camera_seen_at = max(
        [
            row.last_seen_at.astimezone(timezone.utc)
            for row in camera_rows
            if row.last_seen_at
        ],
        default=None,
    )
    observed_zones = {
        str(observation.get("zone") or "").strip().lower()
        for observation in fresh_camera_observations
        if str(observation.get("zone") or "").strip()
    }

    recent_actors = (
        (await db.execute(select(Actor).order_by(Actor.id.desc()).limit(20)))
        .scalars()
        .all()
    )
    recent_object_memories = (
        (
            await db.execute(
                select(WorkspaceObjectMemory)
                .order_by(
                    WorkspaceObjectMemory.last_seen_at.desc(),
                    WorkspaceObjectMemory.id.desc(),
                )
                .limit(120)
            )
        )
        .scalars()
        .all()
    )
    if active_perception_session_id:
        recent_object_memories = [
            row
            for row in recent_object_memories
            if _object_memory_matches_active_session(
                row,
                active_perception_session_id,
            )
        ]

    known_people = set(_known_people())
    for actor in recent_actors:
        if str(actor.role or "").strip().lower() != "user":
            continue
        known_people.add(str(actor.name or "").strip().lower())
        identity_meta = (
            actor.identity_metadata if isinstance(actor.identity_metadata, dict) else {}
        )
        display_name = str(identity_meta.get("display_name") or "").strip().lower()
        if display_name:
            known_people.add(display_name)
        aliases = (
            identity_meta.get("aliases", [])
            if isinstance(identity_meta.get("aliases", []), list)
            else []
        )
        for alias in aliases:
            alias_text = str(alias or "").strip().lower()
            if alias_text:
                known_people.add(alias_text)

    latest_goal = (
        (
            await db.execute(
                select(WorkspaceStrategyGoal)
                .order_by(WorkspaceStrategyGoal.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    open_question = (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion)
                .where(WorkspaceInquiryQuestion.status == "open")
                .order_by(WorkspaceInquiryQuestion.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    latest_inquiry = open_question or (
        (
            await db.execute(
                select(WorkspaceInquiryQuestion)
                .order_by(WorkspaceInquiryQuestion.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    latest_governance = (
        (
            await db.execute(
                select(WorkspaceExecutionTruthGovernanceProfile)
                .order_by(WorkspaceExecutionTruthGovernanceProfile.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    latest_autonomy_boundary = (
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

    latest_stewardship_state = (
        (
            await db.execute(
                select(WorkspaceStewardshipState)
                .order_by(WorkspaceStewardshipState.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    latest_stewardship_cycle = None
    commitment_rows = (
      (
        await db.execute(
          select(WorkspaceOperatorResolutionCommitment)
          .order_by(WorkspaceOperatorResolutionCommitment.id.desc())
          .limit(40)
        )
      )
      .scalars()
      .all()
    )
    latest_resolution_commitment = _choose_operator_resolution_commitment(
      commitment_rows,
      scope="",
    )
    latest_recovery_policy_commitment = _choose_recovery_policy_commitment(
      commitment_rows,
      scope="",
    )
    latest_commitment_monitoring = None
    latest_commitment_outcome = None
    latest_recovery_policy_commitment_monitoring = None
    latest_recovery_policy_commitment_outcome = None
    latest_execution_recovery: dict = {}
    latest_strategy_plan = None
    learned_preferences: list[dict] = []
    proposal_policy_preferences: list[dict] = []
    policy_conflict_profiles: list[dict] = []
    preference_conflict_items: list[dict] = []
    operator_reasoning_scope = ""
    if latest_resolution_commitment is not None:
      latest_commitment_monitoring = await latest_commitment_monitoring_profile(
        commitment_id=int(latest_resolution_commitment.id),
        db=db,
      )
      latest_commitment_outcome = await latest_commitment_outcome_profile(
        commitment_id=int(latest_resolution_commitment.id),
        db=db,
      )
    if latest_recovery_policy_commitment is not None:
      latest_recovery_policy_commitment_monitoring = await latest_commitment_monitoring_profile(
        commitment_id=int(latest_recovery_policy_commitment.id),
        db=db,
      )
      latest_recovery_policy_commitment_outcome = await latest_commitment_outcome_profile(
        commitment_id=int(latest_recovery_policy_commitment.id),
        db=db,
      )
    if latest_stewardship_state is not None:
        latest_stewardship_cycle = (
            (
                await db.execute(
                    select(WorkspaceStewardshipCycle)
                    .where(
                        WorkspaceStewardshipCycle.stewardship_id
                        == int(latest_stewardship_state.id)
                    )
                    .order_by(WorkspaceStewardshipCycle.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

        operator_reasoning_scope = _choose_operator_reasoning_scope(
            inquiry_row=latest_inquiry,
            governance_row=latest_governance,
            autonomy_row=latest_autonomy_boundary,
            stewardship_row=latest_stewardship_state,
        )
        if operator_reasoning_scope:
          if latest_inquiry is not None and not _row_matches_operator_reasoning_scope(
            latest_inquiry,
            operator_reasoning_scope,
            inquiry=True,
          ):
            latest_inquiry = (
              (
                await db.execute(
                  select(WorkspaceInquiryQuestion)
                  .order_by(WorkspaceInquiryQuestion.id.desc())
                  .limit(40)
                )
              )
              .scalars()
              .all()
            )
            latest_inquiry = next(
              (
                row
                for row in latest_inquiry
                if _row_matches_operator_reasoning_scope(
                  row,
                  operator_reasoning_scope,
                  inquiry=True,
                )
              ),
              None,
            )

          if latest_governance is not None and not _row_matches_operator_reasoning_scope(
            latest_governance,
            operator_reasoning_scope,
          ):
            latest_governance = (
              (
                await db.execute(
                  select(WorkspaceExecutionTruthGovernanceProfile)
                  .order_by(WorkspaceExecutionTruthGovernanceProfile.id.desc())
                  .limit(40)
                )
              )
              .scalars()
              .all()
            )
            latest_governance = next(
              (
                row
                for row in latest_governance
                if _row_matches_operator_reasoning_scope(
                  row,
                  operator_reasoning_scope,
                )
              ),
              None,
            )

          if latest_autonomy_boundary is not None and not _row_matches_operator_reasoning_scope(
            latest_autonomy_boundary,
            operator_reasoning_scope,
            autonomy=True,
          ):
            latest_autonomy_boundary = (
              (
                await db.execute(
                  select(WorkspaceAutonomyBoundaryProfile)
                  .order_by(WorkspaceAutonomyBoundaryProfile.id.desc())
                  .limit(40)
                )
              )
              .scalars()
              .all()
            )
            latest_autonomy_boundary = next(
              (
                row
                for row in latest_autonomy_boundary
                if _row_matches_operator_reasoning_scope(
                  row,
                  operator_reasoning_scope,
                  autonomy=True,
                )
              ),
              None,
            )

          if latest_stewardship_state is not None and not _row_matches_operator_reasoning_scope(
            latest_stewardship_state,
            operator_reasoning_scope,
          ):
            latest_stewardship_state = (
              (
                await db.execute(
                  select(WorkspaceStewardshipState)
                  .order_by(WorkspaceStewardshipState.id.desc())
                  .limit(40)
                )
              )
              .scalars()
              .all()
            )
            latest_stewardship_state = next(
              (
                row
                for row in latest_stewardship_state
                if _row_matches_operator_reasoning_scope(
                  row,
                  operator_reasoning_scope,
                )
              ),
              None,
            )
            latest_stewardship_cycle = None
            if latest_stewardship_state is not None:
              latest_stewardship_cycle = (
                (
                  await db.execute(
                    select(WorkspaceStewardshipCycle)
                    .where(
                      WorkspaceStewardshipCycle.stewardship_id
                      == int(latest_stewardship_state.id)
                    )
                    .order_by(WorkspaceStewardshipCycle.id.desc())
                    .limit(1)
                  )
                )
                .scalars()
                .first()
              )

          latest_resolution_commitment = _choose_operator_resolution_commitment(
            commitment_rows,
            scope=operator_reasoning_scope if 'operator_reasoning_scope' in locals() else "",
          )
          latest_recovery_policy_commitment = _choose_recovery_policy_commitment(
            commitment_rows,
            scope=operator_reasoning_scope if 'operator_reasoning_scope' in locals() else "",
          )
          latest_commitment_monitoring = None
          latest_commitment_outcome = None
          latest_recovery_policy_commitment_monitoring = None
          latest_recovery_policy_commitment_outcome = None
          if latest_resolution_commitment is not None:
            latest_commitment_monitoring = await latest_commitment_monitoring_profile(
              commitment_id=int(latest_resolution_commitment.id),
              db=db,
            )
            latest_commitment_outcome = await latest_commitment_outcome_profile(
              commitment_id=int(latest_resolution_commitment.id),
              db=db,
            )
          if latest_recovery_policy_commitment is not None:
            latest_recovery_policy_commitment_monitoring = await latest_commitment_monitoring_profile(
              commitment_id=int(latest_recovery_policy_commitment.id),
              db=db,
            )
            latest_recovery_policy_commitment_outcome = await latest_commitment_outcome_profile(
              commitment_id=int(latest_recovery_policy_commitment.id),
              db=db,
            )

          learned_preferences = await list_learned_preferences(
            db=db,
            managed_scope=operator_reasoning_scope,
            limit=10,
          )
          preference_conflict_items = preference_conflicts(learned_preferences)
          proposal_policy_preferences = await list_workspace_proposal_policy_preferences(
            db=db,
            related_zone=operator_reasoning_scope,
            limit=10,
          )
          policy_conflict_profiles = await list_workspace_policy_conflict_profiles(
            db=db,
            managed_scope=operator_reasoning_scope,
            limit=10,
          )

    if not learned_preferences:
      fallback_scopes: list[str] = []
      for candidate_scope in [
        operator_reasoning_scope,
        str(getattr(latest_commitment_outcome, "managed_scope", "") or "").strip(),
        str(getattr(latest_autonomy_boundary, "scope", "") or "").strip(),
        str(getattr(latest_stewardship_state, "managed_scope", "") or "").strip(),
      ]:
        normalized_scope = str(candidate_scope or "").strip()
        if normalized_scope and normalized_scope not in fallback_scopes:
          fallback_scopes.append(normalized_scope)
      for fallback_scope in fallback_scopes:
        learned_preferences = await list_learned_preferences(
          db=db,
          managed_scope=fallback_scope,
          limit=10,
        )
        preference_conflict_items = preference_conflicts(learned_preferences)
        proposal_policy_preferences = await list_workspace_proposal_policy_preferences(
          db=db,
          related_zone=fallback_scope,
          limit=10,
        )
        policy_conflict_profiles = await list_workspace_policy_conflict_profiles(
          db=db,
          managed_scope=fallback_scope,
          limit=10,
        )
        if learned_preferences:
          break

    if not learned_preferences and latest_resolution_commitment is not None:
      fallback_preference = await latest_scope_learned_preference(
        managed_scope=str(latest_resolution_commitment.managed_scope or "").strip(),
        db=db,
      )
      if fallback_preference:
        learned_preferences = [fallback_preference]
        preference_conflict_items = preference_conflicts(learned_preferences)

    recovery_execution_rows = (
      (
        await db.execute(
          select(CapabilityExecution)
          .where(CapabilityExecution.trace_id != "")
          .order_by(CapabilityExecution.id.desc())
          .limit(40)
        )
      )
      .scalars()
      .all()
    )
    latest_recovery_execution = next(
      (
        row
        for row in recovery_execution_rows
        if str(row.status or "").strip() in {"failed", "blocked", "pending_confirmation", "succeeded"}
        and (
          not operator_reasoning_scope
          or str(row.managed_scope or "").strip() == operator_reasoning_scope
        )
      ),
      None,
    )
    if latest_recovery_execution is None:
      latest_recovery_execution = next(
        (
          row
          for row in recovery_execution_rows
          if str(row.status or "").strip() in {"failed", "blocked", "pending_confirmation", "succeeded"}
        ),
        None,
      )
    if latest_recovery_execution is not None:
      latest_execution_recovery = await evaluate_execution_recovery(
        trace_id=str(latest_recovery_execution.trace_id or "").strip(),
        execution_id=int(latest_recovery_execution.id),
        managed_scope=str(latest_recovery_execution.managed_scope or "").strip(),
        db=db,
      ) or {}
      latest_strategy_plan = await latest_execution_strategy_plan(
        db=db,
        trace_id=str(latest_recovery_execution.trace_id or "").strip(),
      )
      if latest_strategy_plan is None:
        latest_strategy_plan = await latest_execution_strategy_plan(
          db=db,
          managed_scope=(operator_reasoning_scope or str(latest_recovery_execution.managed_scope or "").strip()),
        )
    elif operator_reasoning_scope:
      latest_strategy_plan = await latest_execution_strategy_plan(
        db=db,
        managed_scope=operator_reasoning_scope,
      )

    latest_memory = (
        (await db.execute(select(MemoryEntry).order_by(MemoryEntry.id.desc()).limit(1)))
        .scalars()
        .first()
    )

    latest_interaction_learning = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.memory_class == "interaction_learning")
                .order_by(MemoryEntry.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    goal_summary = ""
    if latest_goal:
        goal_summary = _compact_sentence(
            latest_goal.reasoning_summary
            or latest_goal.success_criteria
            or latest_goal.evidence_summary
            or latest_goal.strategy_type.replace("_", " "),
            max_len=160,
        )

    open_question_summary = ""
    should_surface_open_question = False
    if open_question:
        urgency = str(open_question.urgency or "").strip().lower()
        priority = str(open_question.priority or "").strip().lower()
        age_seconds = (
            (now - open_question.created_at.astimezone(timezone.utc)).total_seconds()
            if open_question.created_at
            else 0.0
        )
        urgent_flag = urgency in {"critical", "high", "urgent"} or priority in {
            "critical",
            "high",
            "urgent",
        }
        open_question_summary = _compact_sentence(
            open_question.waiting_decision
            or open_question.why_answer_matters
            or open_question.safe_default_if_unanswered,
            max_len=170,
        )

    memory_summary = ""
    if latest_memory:
        memory_summary = _compact_sentence(
            latest_memory.summary or latest_memory.content,
            max_len=140,
        )

    learning_summary = ""
    if latest_interaction_learning and not _is_low_quality_learning_entry(
        latest_interaction_learning
    ):
        learning_summary = _compact_sentence(
            latest_interaction_learning.summary or latest_interaction_learning.content,
            max_len=140,
        )
    recent_learning_rows = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.memory_class == "interaction_learning")
                .order_by(MemoryEntry.id.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    for learning_row in recent_learning_rows:
        meta = (
            learning_row.metadata_json
            if isinstance(learning_row.metadata_json, dict)
            else {}
        )
        if active_perception_session_id:
          learning_session_id = _extract_conversation_session_id(meta)
          if (
            learning_session_id
            and learning_session_id != active_perception_session_id
          ):
            continue
        if str(meta.get("preference_signal") or "").strip().lower() != "call_me":
            continue
        learned_name = str(meta.get("preference_value") or "").strip().lower()
        if learned_name:
            known_people.add(learned_name)

    recognized_people: list[str] = []
    observed_non_person_labels: list[str] = []
    known_camera_objects: list[str] = []
    uncertain_camera_objects: list[str] = []
    missing_camera_objects: list[str] = []
    unknown_camera_labels: list[str] = []
    camera_object_states: dict[str, str] = {}
    camera_object_details: dict[str, dict[str, object]] = {}

    for observation in fresh_camera_observations:
        observed_label = str(observation.get("label_raw") or "").strip()
        if not observed_label:
            continue
        observed_key = observed_label.lower()
        if observed_key in known_people:
            recognized_people.append(observed_label)
            continue
        observed_non_person_labels.append(observed_label)

        object_row = next(
            (
                row
                for row in recent_object_memories
                if _object_memory_matches_label(row, observed_label)
            ),
            None,
        )
        metadata = (
            object_row.metadata_json
            if object_row and isinstance(object_row.metadata_json, dict)
            else {}
        )
        semantic_fields = _semantic_metadata_fields(metadata)
        has_library_memory = bool(
            object_row
            and (
                object_row.last_execution_id is not None
                or semantic_fields
                or str(metadata.get("last_observation_source") or "").strip().lower()
                != "live_camera"
            )
        )

        state = "novel"
        if object_row and str(object_row.status or "").strip().lower() == "uncertain":
            state = "uncertain"
        elif object_row and has_library_memory:
            state = "known"

        camera_object_states[observed_label] = state
        if state == "uncertain":
            uncertain_camera_objects.append(observed_label)
        elif state == "known":
            known_camera_objects.append(observed_label)
        else:
            unknown_camera_labels.append(observed_label)

        if state == "novel":
            camera_object_details[observed_label] = _camera_detail_for_label(
                label=observed_label,
                state="novel",
                metadata=metadata,
                row=object_row,
                inquiry_questions=[
                    f"What is {observed_label}?",
                    f"What does {observed_label} do?",
                    "Explain more if needed.",
                ],
            )
        else:
            camera_object_details[observed_label] = _camera_detail_for_label(
                label=observed_label,
                state=state,
                metadata=metadata,
                row=object_row,
            )

    should_consider_missing_objects = bool(observed_non_person_labels) or not recognized_people
    if should_consider_missing_objects:
      for object_row in recent_object_memories:
        state = str(object_row.status or "").strip().lower()
        if state != "missing":
          continue
        if (
          observed_zones
          and str(object_row.zone or "").strip().lower() not in observed_zones
        ):
          continue
        metadata = (
          object_row.metadata_json
          if isinstance(object_row.metadata_json, dict)
          else {}
        )
        semantic_fields = _semantic_metadata_fields(metadata)
        has_library_memory = bool(
          object_row.last_execution_id is not None
          or semantic_fields
          or str(metadata.get("last_observation_source") or "").strip().lower()
          != "live_camera"
        )
        if not has_library_memory:
          continue
        missing_label = str(object_row.canonical_name or "").strip()
        if not missing_label or missing_label in missing_camera_objects:
          continue
        missing_camera_objects.append(missing_label)
        camera_object_states[missing_label] = "missing"
        camera_object_details[missing_label] = _camera_detail_for_label(
          label=missing_label,
          state="missing",
          metadata=metadata,
          row=object_row,
        )

    recognized_person = recognized_people[0] if recognized_people else ""
    unknown_camera_label = unknown_camera_labels[0] if unknown_camera_labels else ""
    uncertain_camera_label = (
        uncertain_camera_objects[0] if uncertain_camera_objects else ""
    )
    missing_camera_label = missing_camera_objects[0] if missing_camera_objects else ""

    unknown_person = False
    if label:
        if label in {"person", "human", "unknown", "visitor"}:
            unknown_person = True
        elif "person" in label and label not in known_people:
            unknown_person = True

    mic_payload = (
        mic_row.last_event_payload_json
        if mic_row and isinstance(mic_row.last_event_payload_json, dict)
        else {}
    )
    mic_confidence = float(mic_payload.get("confidence", 0.0) or 0.0)
    mic_timestamp = _parse_payload_timestamp(mic_payload.get("timestamp"))
    mic_age_seconds = _age_seconds(now, mic_timestamp)
    mic_transcript_raw = str(mic_payload.get("transcript", "")).strip()
    latest_mic_transcript = ""
    if (
        mic_transcript_raw
        and mic_confidence >= MIC_PROMPT_MIN_CONFIDENCE
        and (mic_age_seconds is None or mic_age_seconds <= MIC_PROMPT_MAX_AGE_SECONDS)
    ):
        latest_mic_transcript = _compact_sentence(mic_transcript_raw, max_len=120)

    recent_input_events = (
      (
        await db.execute(
          select(InputEvent)
          .where(InputEvent.source.in_(["text", "voice", "ui", "api"]))
          .order_by(InputEvent.id.desc())
          .limit(12)
        )
      )
      .scalars()
      .all()
    )
    if active_perception_session_id:
        session_input_events = []
        for row in recent_input_events:
            event_session_id = _extract_conversation_session_id(row.metadata_json)
            if event_session_id == active_perception_session_id:
                session_input_events.append(row)
        if session_input_events:
            recent_input_events = session_input_events
    latest_input_event = (
        recent_input_events[0] if recent_input_events else latest_input_event
    )
    latest_input_text = ""
    if latest_input_event:
        latest_input_text = _compact_sentence(
            str(latest_input_event.raw_input or "").strip(), max_len=120
        )

    repeated_ambiguous_user_input = False
    if len(recent_input_events) >= 2:
        latest_raw_input = str(recent_input_events[0].raw_input or "").strip()
        previous_raw_input = str(recent_input_events[1].raw_input or "").strip()

        def _is_ambiguous_turn(value: str) -> bool:
            if not value:
                return False
            if (
                _looks_like_greeting(value)
                or _looks_like_direct_question(value)
                or _looks_like_status_request(value)
            ):
                return False
            return True

        repeated_ambiguous_user_input = _is_ambiguous_turn(
            latest_raw_input
        ) and _is_ambiguous_turn(previous_raw_input)

    latest_user_input = latest_input_text
    if latest_mic_transcript:
        latest_input_created_at = (
            latest_input_event.created_at.astimezone(timezone.utc)
            if latest_input_event and latest_input_event.created_at
            else None
        )
        if latest_input_created_at is None or (
            mic_timestamp is not None and mic_timestamp >= latest_input_created_at
        ):
            latest_user_input = latest_mic_transcript
    if not latest_user_input:
        latest_user_input = latest_mic_transcript or latest_input_text

    latest_user_signal_at = None
    if latest_input_event and latest_input_event.created_at:
        latest_user_signal_at = latest_input_event.created_at.astimezone(timezone.utc)
    if mic_timestamp is not None and (
        latest_user_signal_at is None or mic_timestamp >= latest_user_signal_at
    ):
        latest_user_signal_at = mic_timestamp

    recent_speech_actions = (
        (
            await db.execute(
                select(SpeechOutputAction)
                .order_by(SpeechOutputAction.id.desc())
                .limit(6)
            )
        )
        .scalars()
        .all()
    )
    recent_resolutions = (
        (
            await db.execute(
                select(InputEventResolution)
                .order_by(InputEventResolution.id.desc())
                .limit(6)
            )
        )
        .scalars()
        .all()
    )
    clarification_budget_exhausted = (
        any(
            _is_clarifier_prompt_text(str(row.requested_text or ""))
            for row in recent_speech_actions
        )
        or any(
            _is_clarifier_prompt_text(str(row.clarification_prompt or ""))
            for row in recent_resolutions
        )
        or repeated_ambiguous_user_input
    )

    if open_question_summary and open_question:
        urgency = str(open_question.urgency or "").strip().lower()
        priority = str(open_question.priority or "").strip().lower()
        age_seconds = (
            (now - open_question.created_at.astimezone(timezone.utc)).total_seconds()
            if open_question.created_at
            else 0.0
        )
        critical_interrupt = urgency in {
            "critical",
            "urgent",
            "emergency",
        } or priority in {"critical", "urgent", "emergency"}
        elevated_priority = urgency in {
            "high",
            "critical",
            "urgent",
            "emergency",
        } or priority in {"high", "critical", "urgent", "emergency"}
        conversational_signal = bool(latest_mic_transcript or learning_summary)
        should_surface_open_question = bool(
            critical_interrupt
            or (
                not conversational_signal and (elevated_priority or age_seconds <= 1200)
            )
        )

    environment_now = ""
    if unknown_person:
        environment_now = "there is an unidentified person in view"
    elif camera_scene_summary:
        environment_now = camera_scene_summary
    elif label_raw:
        environment_now = (
            f"{label_raw} is visible on camera with confidence {confidence:.2f}"
        )
    else:
        environment_now = "camera has no clear person in view"

    needs_identity_prompt = bool(
        unknown_person
        and not goal_summary
        and not (open_question_summary and should_surface_open_question)
    )

    inquiry_prompt = ""
    if open_question_summary and should_surface_open_question:
        inquiry_prompt = f"If you want me to continue this workflow, I need one decision: {open_question_summary}"
    elif needs_identity_prompt:
        inquiry_prompt = "I can see someone nearby. What should I call you?"
    else:
        camera_prompt_allowed = not (
            latest_user_signal_at is not None
            and latest_camera_seen_at is not None
            and latest_user_signal_at >= latest_camera_seen_at
        )
        if camera_prompt_allowed:
            inquiry_prompt = _build_camera_state_prompt(
                camera_scene_summary=camera_scene_summary,
                camera_source_count=camera_source_count,
                recognized_person=recognized_person,
                unknown_camera_label=unknown_camera_label,
                uncertain_camera_label=uncertain_camera_label,
                missing_camera_label=missing_camera_label,
                camera_object_details=camera_object_details,
                camera_last_confidence=confidence,
            )
    if not inquiry_prompt:
        inquiry_prompt = _build_curiosity_prompt(
            environment_now=environment_now,
            goal_summary=goal_summary,
            memory_summary=memory_summary,
            latest_mic_transcript=latest_user_input,
            learning_summary=learning_summary,
            clarification_budget_exhausted=clarification_budget_exhausted,
        )

    voice_listen_hint = ""
    mic_status = str(mic_payload.get("status") or "").strip().lower()
    mic_mode = str(mic_payload.get("mode") or "").strip().lower()
    if mic_status == "heartbeat_no_transcript" or "no_wake" in mic_mode:
        if "no_wake" in mic_mode:
            voice_listen_hint = 'Wake word required: say "MIM" before your request.'
        else:
            voice_listen_hint = "Listening heartbeat active. Say a request when ready."

    latest_output_text = _rewrite_state_output_text(
        str(speech_row.requested_text or "") if speech_row else "",
        needs_identity_prompt=needs_identity_prompt,
        open_question_summary=open_question_summary,
        goal_summary=goal_summary,
        latest_mic_transcript=latest_user_input,
        environment_now=environment_now,
        memory_summary=memory_summary,
    )
    gateway_governance_snapshot = await _latest_gateway_governance_snapshot(
      db=db,
      managed_scope=operator_reasoning_scope,
    )
    runtime_health = build_mim_ui_health_snapshot_from_rows(
        now=now,
        speech_row=speech_row,
        camera_row=camera_row,
        mic_row=mic_row,
        db_ok=True,
    )
    collaboration_progress = _operator_collaboration_progress_snapshot()
    dispatch_telemetry = _operator_dispatch_telemetry_snapshot()
    tod_decision_process = _operator_tod_decision_process_snapshot()
    self_evolution_briefing = await build_self_evolution_briefing(
      actor="mim_ui_state",
      source="mim_ui_operator_reasoning",
      refresh=False,
      lookback_hours=168,
      min_occurrence_count=2,
      auto_experiment_limit=3,
      limit=5,
      db=db,
    )
    self_evolution_briefing = (
      self_evolution_briefing.get("briefing", {})
      if isinstance(self_evolution_briefing, dict)
      else {}
    )
    runtime_recovery = runtime_recovery_service.get_summary()

    operator_reasoning = _build_operator_reasoning_payload(
        goal_row=latest_goal,
        inquiry_row=latest_inquiry,
        governance_row=latest_governance,
        autonomy_row=latest_autonomy_boundary,
        stewardship_row=latest_stewardship_state,
        stewardship_cycle_row=latest_stewardship_cycle,
        commitment_row=latest_resolution_commitment,
        commitment_monitoring_row=latest_commitment_monitoring,
        commitment_outcome_row=latest_commitment_outcome,
        recovery_commitment_row=latest_recovery_policy_commitment,
        recovery_commitment_monitoring_row=latest_recovery_policy_commitment_monitoring,
        recovery_commitment_outcome_row=latest_recovery_policy_commitment_outcome,
      gateway_governance_snapshot=gateway_governance_snapshot,
        learned_preferences=learned_preferences,
        preference_conflicts_items=preference_conflict_items,
        proposal_policy_preferences=proposal_policy_preferences,
        policy_conflict_profiles=policy_conflict_profiles,
        execution_recovery=latest_execution_recovery,
        execution_readiness=load_latest_execution_readiness(
            action="mim_ui_state",
            capability_name="mim_ui_state",
            managed_scope=operator_reasoning_scope,
            requested_executor="tod",
            metadata_json={"managed_scope": operator_reasoning_scope},
        ),
        collaboration_progress=collaboration_progress,
        dispatch_telemetry=dispatch_telemetry,
        tod_decision_process=tod_decision_process,
        self_evolution_briefing=self_evolution_briefing,
        runtime_health=runtime_health,
        runtime_recovery=runtime_recovery,
        latest_execution_row=latest_recovery_execution,
        strategy_plan_row=latest_strategy_plan,
    )

    return {
        "speaking": speaking,
        "camera_last_label": label_raw,
        "camera_last_confidence": confidence,
        "camera_scene_summary": camera_scene_summary,
        "camera_source_count": camera_source_count,
        "voice_listen_hint": voice_listen_hint,
        "conversation_policy_profile": "tightened_v1",
        "runtime_build": "mim-ui-tightened-v1",
        "runtime_features": [
            "voice_listen_hint",
            "camera_scene_context",
            "object_memory_bridge",
            "conversation_policy_tightened",
          "system_awareness_visibility",
            "operator_reasoning_summary",
          "operator_trust_signals",
            "lightweight_autonomy_guidance",
            "human_feedback_loop",
            "system_stability_guard",
            "operator_resolution_commitments",
            "operator_commitment_enforcement_monitoring",
            "operator_preference_convergence",
            "cross_policy_conflict_resolution",
            "execution_readiness_integration",
            "recovery_governance_rollup",
            "tod_collaboration_progress",
            "mim_arm_dispatch_telemetry",
            "tod_decision_process_visibility",
            "self_evolution_operator_visibility",
            "self_evolution_operator_actionability",
            "self_evolution_operator_commands",
            "runtime_health_visibility",
        ],
        "inquiry_prompt": inquiry_prompt,
        "operator_reasoning": operator_reasoning,
          "mim_arm_dispatch_telemetry": dispatch_telemetry,
        "conversation_context": {
            "environment_now": environment_now,
            "active_goal": goal_summary,
            "open_question": open_question_summary,
            "memory_hint": memory_summary,
            "recent_user_input": latest_user_input,
            "interaction_learning": learning_summary,
            "active_perception_session_id": active_perception_session_id,
            "needs_identity_prompt": needs_identity_prompt,
            "camera_scene_summary": camera_scene_summary,
            "recognized_person": recognized_person,
            "recognized_people": recognized_people,
            "unknown_camera_label": unknown_camera_label,
            "unknown_camera_labels": unknown_camera_labels,
            "known_camera_objects": known_camera_objects,
            "uncertain_camera_label": uncertain_camera_label,
            "uncertain_camera_objects": uncertain_camera_objects,
            "missing_camera_label": missing_camera_label,
            "missing_camera_objects": missing_camera_objects,
            "camera_object_states": camera_object_states,
            "camera_object_details": camera_object_details,
            "operator_reasoning_summary": str(operator_reasoning.get("summary") or "").strip(),
            "current_recommendation_summary": str(
              operator_reasoning.get("current_recommendation", {}).get("summary") or ""
            ).strip()
            if isinstance(operator_reasoning.get("current_recommendation", {}), dict)
            else "",
            "current_recommendation_source": str(
              operator_reasoning.get("current_recommendation", {}).get("source") or ""
            ).strip()
            if isinstance(operator_reasoning.get("current_recommendation", {}), dict)
            else "",
            "self_evolution_summary": str(
              operator_reasoning.get("self_evolution", {}).get("summary") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_action_summary": str(
              operator_reasoning.get("self_evolution", {}).get("action_summary") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_action_method": str(
              operator_reasoning.get("self_evolution", {}).get("action_method") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_action_path": str(
              operator_reasoning.get("self_evolution", {}).get("action_path") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_operator_command_summary": str(
              operator_reasoning.get("self_evolution", {}).get("operator_command_summary") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "trust_signal_summary": str(operator_reasoning.get("trust_signal_summary") or "").strip(),
            "lightweight_autonomy_summary": str(operator_reasoning.get("lightweight_autonomy", {}).get("summary") or "").strip()
            if isinstance(operator_reasoning.get("lightweight_autonomy", {}), dict)
            else "",
            "feedback_loop_summary": str(operator_reasoning.get("feedback_loop", {}).get("summary") or "").strip()
            if isinstance(operator_reasoning.get("feedback_loop", {}), dict)
            else "",
            "stability_guard_summary": str(operator_reasoning.get("stability_guard", {}).get("summary") or "").strip()
            if isinstance(operator_reasoning.get("stability_guard", {}), dict)
            else "",
            "runtime_health_summary": str(operator_reasoning.get("runtime_health", {}).get("summary") or "").strip()
            if isinstance(operator_reasoning.get("runtime_health", {}), dict)
            else "",
            "runtime_recovery_summary": str(operator_reasoning.get("runtime_recovery", {}).get("summary") or "").strip()
            if isinstance(operator_reasoning.get("runtime_recovery", {}), dict)
            else "",
            "tod_collaboration_summary": str(
                operator_reasoning.get("collaboration_progress", {}).get("summary") or ""
            ).strip()
            if isinstance(operator_reasoning.get("collaboration_progress", {}), dict)
            else "",
            "tod_collaboration_execution_id": str(
              operator_reasoning.get("collaboration_progress", {}).get("execution_id") or ""
            ).strip()
            if isinstance(operator_reasoning.get("collaboration_progress", {}), dict)
            else "",
            "tod_collaboration_id_kind": str(
              operator_reasoning.get("collaboration_progress", {}).get("id_kind") or ""
            ).strip()
            if isinstance(operator_reasoning.get("collaboration_progress", {}), dict)
            else "",
            "tod_collaboration_task_id": str(
                operator_reasoning.get("collaboration_progress", {}).get("task_id") or ""
            ).strip()
            if isinstance(operator_reasoning.get("collaboration_progress", {}), dict)
            else "",
            "tod_collaboration_request_id": str(
              operator_reasoning.get("collaboration_progress", {}).get("request_id") or ""
            ).strip()
            if isinstance(operator_reasoning.get("collaboration_progress", {}), dict)
            else "",
            "operator_resolution_summary": str(
              (
                operator_reasoning.get("resolution_commitment", {})
                if isinstance(operator_reasoning.get("resolution_commitment", {}), dict)
                else {}
              ).get("reason")
              or ""
            ).strip(),
              "operator_preference_summary": str(
                (
                  operator_reasoning.get("learned_preferences", [])
                  if isinstance(operator_reasoning.get("learned_preferences", []), list)
                  else []
                )[0].get("preference_direction")
                if (
                  isinstance(operator_reasoning.get("learned_preferences", []), list)
                  and operator_reasoning.get("learned_preferences", [])
                  and isinstance(operator_reasoning.get("learned_preferences", [])[0], dict)
                )
                else ""
              ).strip(),
        },
        "latest_output_action_id": int(speech_row.id) if speech_row else 0,
        "latest_output_text": latest_output_text,
        "latest_output_allowed": bool(str(speech_row.delivery_status or "") == "queued")
        if speech_row
        else False,
    }


@router.get("/mim/ui/runtime-recovery")
async def get_runtime_recovery_summary() -> dict:
  return runtime_recovery_service.get_summary()


@router.post("/mim/ui/runtime-recovery-events")
async def record_runtime_recovery_event(request: RuntimeRecoveryEventRequest) -> dict:
  event = runtime_recovery_service.record_event(
    lane=request.lane,
    event_type=request.event_type,
    detail=str(request.detail or "").strip(),
    next_retry_at=str(request.next_retry_at or "").strip() or None,
    metadata=request.metadata if isinstance(request.metadata, dict) else {},
  )
  return {
    "status": "recorded",
    "event": event,
    "summary": runtime_recovery_service.get_summary(),
  }


@router.get("/mim/ui/health")
async def mim_ui_health(db: AsyncSession = Depends(get_db)) -> dict:
  return await build_mim_ui_health_snapshot(db=db)
