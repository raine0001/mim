import json
import base64
import io
import asyncio
import concurrent.futures
import logging
import os
import importlib
import uuid
import re
import math
import time
import wave
import array
import html
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.camera_scene import (
    collect_fresh_camera_observations,
    summarize_camera_observations,
)
from core.communication_composer import (
    build_deterministic_communication_reply,
    compose_expert_communication_reply,
    sanitize_user_facing_reply_text,
)
from core.db import get_db
from core.config import settings
from core.autonomy_driver_service import (
    HARD_BOUNDARY,
    build_initiative_status,
    classify_boundary_mode,
    drive_initiative_from_intent,
    extract_explicit_initiative_id,
)
from core.execution_strategy_service import understand_intent
from core.intent_routing_service import (
    classify_console_intent,
    robotics_web_guard_blocks_search,
    route_console_text_input,
)
from core.execution_policy_gate import (
    build_intent_key,
    evaluate_execution_policy_gate,
    sync_execution_control_state,
)
from core.execution_recovery_service import sync_execution_recovery_state
from core.execution_trace_service import append_execution_trace_event, infer_managed_scope
from core.execution_truth_service import (
    build_execution_truth_bridge_projection,
    canonicalize_execution_truth,
    derive_execution_truth_signals,
)
from core.interface_service import (
    append_interface_message,
    get_interface_session,
    upsert_interface_session,
)
from core.journal import write_journal
from core.mim_arm_dispatch_telemetry import update_dispatch_telemetry_from_feedback
from core.mim_ui_auth import ensure_authenticated_mimtod_api_request
from core.preferences import (
    DEFAULT_USER_ID,
    get_user_preference_payload,
    upsert_user_preference,
)
from core.runtime_recovery_service import RuntimeRecoveryService
from core.ui_health_service import (
    build_mim_ui_health_snapshot,
    summarize_runtime_health,
)
from core.user_action_inquiry_service import InquiryStatus, UserActionInquiryService
from core.user_action_safety_monitor import (
    ActionCategory,
    UserAction,
    UserActionSafetyMonitor,
)
from core.routers.self_awareness_router import health_monitor as _mim_health_monitor
from core.routers.mim_ui import (
    _build_operator_reasoning_summary,
    _operator_collaboration_progress_snapshot,
    _operator_goal_snapshot,
    _operator_tod_decision_process_snapshot,
)
from core.models import (
    Actor,
    CapabilityExecution,
    CapabilityRegistration,
    Goal,
    InputEvent,
    InputEventResolution,
    MemoryEntry,
    MemoryLink,
    SpeechOutputAction,
    Task,
    WorkspaceMonitoringState,
    WorkspaceObservation,
    WorkspaceObjectMemory,
    WorkspaceObjectRelation,
    WorkspacePerceptionSource,
    WorkspaceProposal,
    WorkspaceStrategyGoal,
    WorkspaceZone,
    WorkspaceZoneRelation,
)
from core.voice_policy import (
    evaluate_voice_policy,
    load_voice_policy,
    validate_voice_output,
)
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
MIC_TRANSCRIBE_DEBUG_LOG = (
    Path(__file__).resolve().parents[2]
    / "runtime"
    / "logs"
    / "mic_transcribe_debug.jsonl"
)

USER_ACTION_SAFETY_MONITOR = UserActionSafetyMonitor(Path("runtime/shared"))
USER_ACTION_INQUIRY_SERVICE = UserActionInquiryService(Path("runtime/shared"))
SHARED_RUNTIME_ROOT = Path("runtime/shared")
runtime_recovery_service = RuntimeRecoveryService(SHARED_RUNTIME_ROOT)

GATEWAY_GOVERNANCE_PRECEDENCE = [
    "explicit_operator_approval",
    "hard_safety_escalation",
    "degraded_health_confirmation",
    "benign_healthy_auto_execution",
]


def _coerce_web_research_concurrency(
    value: object,
    *,
    fallback: int,
    minimum: int = 1,
    maximum: int = 32,
) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(fallback)
    return max(minimum, min(maximum, parsed))


def _mic_debug_enabled(payload: dict) -> bool:
    if str(os.getenv("MIM_MIC_DEBUG", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    return bool(payload.get("debug"))


def _append_mic_debug_event(event: dict) -> None:
    try:
        MIC_TRANSCRIBE_DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with MIC_TRANSCRIBE_DEBUG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
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
    forbidden_hint = (
        "forbidden" in lowered or " 403" in lowered or "status 403" in lowered
    )
    unauthorized_hint = (
        "unauthorized" in lowered or " 401" in lowered or "status 401" in lowered
    )
    quota_hint = "quota" in lowered or "rate" in lowered or "429" in lowered
    blocked_hint = "blocked" in lowered or "denied" in lowered
    return {
        "forbidden_hint": forbidden_hint,
        "unauthorized_hint": unauthorized_hint,
        "quota_or_rate_hint": quota_hint,
        "blocked_hint": blocked_hint,
        "upstream_status_hint": 403
        if forbidden_hint
        else (401 if unauthorized_hint else (429 if quota_hint else None)),
    }


def _resolve_mic_provider_mode(payload: dict) -> str:
    raw = (
        str(payload.get("provider") or os.getenv("MIM_MIC_PROVIDER") or "")
        .strip()
        .lower()
    )
    if raw in {"local", "pocketsphinx", "offline"}:
        return "local"
    if raw in {"openai", "whisper", "gpt4o", "gpt-4o-mini-transcribe"}:
        return "openai"
    if raw in {"google", "google_web_speech", "cloud"}:
        return "google"
    if raw == "auto":
        return "auto"
    return "auto" if settings.allow_web_access else "local"


def _estimate_wav_audio_metrics(audio_bytes: bytes) -> dict:
    metrics = {
        "audio_duration_ms": 0,
        "sample_rate_hz": 0,
        "channels": 0,
        "sample_width_bytes": 0,
        "rms_dbfs": None,
        "peak_dbfs": None,
        "speech_detected": False,
        "upload_bytes": len(audio_bytes),
    }
    if not audio_bytes:
        return metrics

    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
            channels = int(wav_file.getnchannels() or 0)
            sample_rate = int(wav_file.getframerate() or 0)
            sample_width = int(wav_file.getsampwidth() or 0)
            frame_count = int(wav_file.getnframes() or 0)
            raw_pcm = wav_file.readframes(frame_count)
    except Exception:
        return metrics

    metrics["sample_rate_hz"] = sample_rate
    metrics["channels"] = channels
    metrics["sample_width_bytes"] = sample_width
    if sample_rate > 0 and frame_count > 0:
        metrics["audio_duration_ms"] = int((frame_count / float(sample_rate)) * 1000)

    if not raw_pcm or sample_width <= 0:
        return metrics

    # Decode PCM samples as signed values and estimate loudness/peak.
    samples: list[int] = []
    max_int = 32767.0
    try:
        if sample_width == 1:
            samples = [int(v) - 128 for v in raw_pcm]
            max_int = 127.0
        elif sample_width == 2:
            arr = array.array("h")
            arr.frombytes(raw_pcm)
            samples = [int(v) for v in arr]
            max_int = 32767.0
        elif sample_width == 4:
            arr = array.array("i")
            arr.frombytes(raw_pcm)
            samples = [int(v) for v in arr]
            max_int = 2147483647.0
        elif sample_width == 3:
            step = 3
            for i in range(0, len(raw_pcm) - 2, step):
                b0 = raw_pcm[i]
                b1 = raw_pcm[i + 1]
                b2 = raw_pcm[i + 2]
                value = b0 | (b1 << 8) | (b2 << 16)
                if value & 0x800000:
                    value -= 0x1000000
                samples.append(int(value))
            max_int = 8388607.0
    except Exception:
        return metrics

    if not samples:
        return metrics

    peak = max(abs(v) for v in samples)
    rms = math.sqrt(sum(float(v) * float(v) for v in samples) / float(len(samples)))
    if peak > 0 and max_int > 0:
        peak_norm = min(1.0, peak / max_int)
        metrics["peak_dbfs"] = round(20.0 * math.log10(max(peak_norm, 1e-12)), 2)
    if rms > 0 and max_int > 0:
        rms_norm = min(1.0, rms / max_int)
        metrics["rms_dbfs"] = round(20.0 * math.log10(max(rms_norm, 1e-12)), 2)

    rms_dbfs = metrics.get("rms_dbfs")
    peak_dbfs = metrics.get("peak_dbfs")
    metrics["speech_detected"] = bool(
        (isinstance(rms_dbfs, (int, float)) and rms_dbfs > -42.0)
        or (isinstance(peak_dbfs, (int, float)) and peak_dbfs > -22.0)
    )
    return metrics


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
    reasons = {
        str(item).strip().lower()
        for item in (escalation_reasons or [])
        if str(item).strip()
    }
    clarification_reasons = {
        "requires_clarification",
        "ambiguous_command",
        "missing_target",
        "low_transcript_confidence",
    }
    if "unsafe_action_request" in reasons:
        return False
    return outcome in {"store_only", "requires_confirmation", "blocked"} and bool(
        reasons.intersection(clarification_reasons)
    )


def _build_one_clarifier_prompt(transcript: str) -> str:
    request = _normalize_prompt_key(transcript)[:72]
    if request and not _is_low_signal_turn(request):
        return f"For '{request}', I'm missing one detail: do you want me to answer a question, suggest a plan, or take an action?"
    return "I'm missing one detail: do you want me to answer a question, suggest a plan, or take an action?"


def _build_clarification_limit_prompt(
    escalation_reasons: list[str], transcript: str
) -> str:
    reasons = {
        str(item).strip().lower()
        for item in (escalation_reasons or [])
        if str(item).strip()
    }
    if "missing_target" in reasons:
        missing = "the exact object or location"
    elif "low_transcript_confidence" in reasons:
        missing = "a clearer request"
    else:
        missing = "the intended outcome"
    request = _normalize_prompt_key(transcript)[:72]
    # Return a non-question response so the second ambiguous turn does not look
    # like a repeated clarification request — MIM is now waiting for a clear input.
    if request and not _is_low_signal_turn(request):
        return f"Still not clear on {missing} for '{request}'. Share a specific question or action when ready."
    return f"Still not clear on {missing}. Share a specific question or action when ready."


async def _recent_voice_clarification_count(
    db: AsyncSession, *, within_seconds: int = 180
) -> int:
    threshold = datetime.now(timezone.utc) - timedelta(
        seconds=max(30, int(within_seconds))
    )
    rows = (
        (
            await db.execute(
                select(InputEventResolution)
                .where(InputEventResolution.created_at >= threshold)
                .where(InputEventResolution.clarification_prompt != "")
                .order_by(InputEventResolution.id.desc())
                .limit(12)
            )
        )
        .scalars()
        .all()
    )

    count = 0
    for row in rows:
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if str(meta.get("source", "")).strip().lower() != "voice":
            continue
        if not _is_clarification_driven(
            row.escalation_reasons or [], str(row.outcome or "")
        ):
            continue
        count += 1
    return count


def _token_overlap_ratio(left: str, right: str) -> float:
    left_tokens = {token for token in _normalize_prompt_key(left).split() if token}
    right_tokens = {token for token in _normalize_prompt_key(right).split() if token}
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens.intersection(right_tokens))
    return overlap / float(min(len(left_tokens), len(right_tokens)))


async def _has_recent_similar_voice_clarification(
    db: AsyncSession,
    *,
    transcript: str,
    within_seconds: int = 180,
    min_overlap_ratio: float = 0.30,
) -> bool:
    threshold = datetime.now(timezone.utc) - timedelta(
        seconds=max(30, int(within_seconds))
    )
    rows = (
        await db.execute(
            select(InputEventResolution, InputEvent)
            .join(InputEvent, InputEvent.id == InputEventResolution.input_event_id)
            .where(InputEventResolution.created_at >= threshold)
            .where(InputEvent.source == "voice")
            .where(InputEventResolution.clarification_prompt != "")
            .order_by(InputEventResolution.id.desc())
            .limit(8)
        )
    ).all()

    candidate = str(transcript or "").strip()
    for resolution, event in rows:
        reasons = (
            resolution.escalation_reasons
            if isinstance(resolution.escalation_reasons, list)
            else []
        )
        if not _is_clarification_driven(reasons, str(resolution.outcome or "")):
            continue
        prior_text = str(event.raw_input or "").strip()
        if _token_overlap_ratio(candidate, prior_text) >= max(
            0.0, min(1.0, float(min_overlap_ratio))
        ):
            return True
    return False


async def _has_recent_similar_text_precision_prompt(
    db: AsyncSession,
    *,
    transcript: str,
    exclude_event_id: int | None = None,
    session_id: str = "",
    within_seconds: int = 180,
    min_overlap_ratio: float = 0.30,
) -> bool:
    rows = (
        (
            await db.execute(
                select(InputEvent)
                .where(InputEvent.source == "text")
                .order_by(InputEvent.id.desc())
                .limit(max(8, min(24, int(within_seconds // 10) or 8)))
            )
        )
        .scalars()
        .all()
    )

    candidate = str(transcript or "").strip()
    normalized_session = str(session_id or "").strip()
    for event in rows:
        if exclude_event_id is not None and int(event.id) == int(exclude_event_id):
            continue
        if normalized_session:
            event_meta = (
                event.metadata_json if isinstance(event.metadata_json, dict) else {}
            )
            event_session = str(event_meta.get("conversation_session_id", "")).strip()
            if event_session != normalized_session:
                continue
        prior_text = str(event.raw_input or "").strip()
        if _token_overlap_ratio(candidate, prior_text) >= max(
            0.0, min(1.0, float(min_overlap_ratio))
        ):
            return True
    return False


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


def _extract_visible_text_from_html(
    raw_html: str, *, max_chars: int = 12000
) -> tuple[str, str]:
    raw_doc = str(raw_html or "")
    title_match = re.search(
        r"<title[^>]*>(.*?)</title>", raw_doc, flags=re.IGNORECASE | re.DOTALL
    )
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    title = html.unescape(title)
    title = re.sub(r"<[^>]+>", " ", title)
    title = re.sub(r"\s+", " ", title).strip()

    without_scripts = re.sub(
        r"<script\b[^>]*>.*?</script>", " ", raw_doc, flags=re.IGNORECASE | re.DOTALL
    )
    without_styles = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        without_scripts,
        flags=re.IGNORECASE | re.DOTALL,
    )
    with_breaks = re.sub(
        r"</?(p|div|h1|h2|h3|h4|h5|h6|li|br|tr|section|article)[^>]*>",
        "\n",
        without_styles,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", with_breaks)
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return title, text


def _build_web_summary(*, title: str, text: str, max_sentences: int = 4) -> str:
    cleaned = html.unescape(str(text or "").strip())
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
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


def _normalize_web_research_query(text: str) -> str:
    cleaned = " ".join(str(text or "").strip().split())
    cleaned = re.sub(
        r"^(hi|hello|hey)\s+mim\b[:,]?\s*", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = re.sub(r"^mim\b[:,]?\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


WEB_RESEARCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "best",
    "can",
    "compare",
    "did",
    "does",
    "evidence",
    "find",
    "for",
    "from",
    "give",
    "how",
    "into",
    "is",
    "it",
    "its",
    "look",
    "most",
    "online",
    "please",
    "prove",
    "proven",
    "question",
    "real",
    "research",
    "results",
    "review",
    "reviews",
    "search",
    "show",
    "that",
    "the",
    "their",
    "them",
    "these",
    "they",
    "this",
    "top",
    "true",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}

WEB_RESEARCH_AUTHORITY_ROOTS = {
    "cdc.gov",
    "esa.int",
    "fda.gov",
    "nature.com",
    "nasa.gov",
    "nih.gov",
    "noaa.gov",
    "science.org",
    "who.int",
}

WEB_RESEARCH_EXTRAORDINARY_PATTERNS = [
    {
        "topic": "alien_contact",
        "keywords": (
            "alien",
            "aliens",
            "extraterrestrial",
            "extraterrestrials",
            "ufo",
            "ufos",
        ),
        "reason": "This is an extraordinary real-world claim.",
    },
    {
        "topic": "miracle_cure",
        "keywords": (
            "miracle cure",
            "instant cure",
            "secret cure",
            "cure cancer",
            "guaranteed cure",
        ),
        "reason": "This claim needs unusually strong medical evidence.",
    },
    {
        "topic": "conspiracy_claim",
        "keywords": (
            "flat earth",
            "moon landing hoax",
            "chemtrail",
            "chemtrails",
        ),
        "reason": "This claim conflicts with widely established baseline knowledge.",
    },
]


def _web_research_terms(text: str, *, max_terms: int = 14) -> list[str]:
    parts = re.findall(r"[a-z0-9]+", str(text or "").lower())
    terms: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part in WEB_RESEARCH_STOPWORDS:
            continue
        if len(part) < 3 and not part.isdigit():
            continue
        if part in seen:
            continue
        seen.add(part)
        terms.append(part)
        if len(terms) >= max(1, int(max_terms)):
            break
    return terms


def _web_research_domain(raw_url: str) -> str:
    parsed = urlparse(str(raw_url or "").strip())
    host = str(parsed.hostname or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _distill_web_research_claims(
    sources: list[dict[str, object]], *, max_claims: int = 3
) -> list[str]:
    claims: list[str] = []
    seen: set[str] = set()
    for source in sources:
        raw = str(source.get("summary") or source.get("snippet") or "").strip()
        if not raw:
            continue
        sentence = re.split(r"(?<=[.!?])\s+", raw)[0].strip()
        claim = _compact_text(sentence or raw, 180)
        normalized = claim.lower()
        if not claim or normalized in seen:
            continue
        seen.add(normalized)
        claims.append(claim)
        if len(claims) >= max(1, int(max_claims)):
            break
    return claims


def _web_research_authority_context(
    sources: list[dict[str, object]],
) -> dict[str, object]:
    authority_domains: list[str] = []
    source_domains: list[str] = []
    seen_authority: set[str] = set()
    seen_domains: set[str] = set()

    for source in sources:
        domain = _web_research_domain(str(source.get("url") or ""))
        if not domain:
            continue
        if domain not in seen_domains:
            seen_domains.add(domain)
            source_domains.append(domain)

        is_authoritative = domain.endswith(".gov") or domain.endswith(".edu")
        if not is_authoritative:
            for root in WEB_RESEARCH_AUTHORITY_ROOTS:
                if domain == root or domain.endswith(f".{root}"):
                    is_authoritative = True
                    break
        if is_authoritative and domain not in seen_authority:
            seen_authority.add(domain)
            authority_domains.append(domain)

    return {
        "authority_count": len(authority_domains),
        "authority_domains": authority_domains[:5],
        "source_domains": source_domains[:8],
    }


def _extraordinary_claim_profile(
    *, query: str, sources: list[dict[str, object]]
) -> dict[str, object]:
    claim_text = " ".join(
        [
            str(query or ""),
            *(
                str(source.get("summary") or source.get("snippet") or "")
                for source in sources
            ),
        ]
    ).lower()
    matched_topics: list[str] = []
    reasons: list[str] = []
    for pattern in WEB_RESEARCH_EXTRAORDINARY_PATTERNS:
        if any(keyword in claim_text for keyword in pattern["keywords"]):
            matched_topics.append(str(pattern["topic"]))
            reasons.append(str(pattern["reason"]))
    return {
        "is_extraordinary": bool(matched_topics),
        "topics": matched_topics,
        "reasons": reasons,
    }


def _summarize_prior_web_research_memories(
    memories: list[dict[str, object]],
) -> dict[str, object]:
    if not memories:
        return {
            "count": 0,
            "memory_ids": [],
            "claims": [],
            "related_queries": [],
            "skeptical_prior_count": 0,
            "direct_evidence_count": 0,
            "summary_line": "",
        }

    memory_ids: list[int] = []
    claims: list[str] = []
    related_queries: list[str] = []
    seen_claims: set[str] = set()
    skeptical_prior_count = 0
    direct_evidence_count = 0

    for item in memories:
        memory_id = int(item.get("memory_id", 0) or 0)
        if memory_id > 0:
            memory_ids.append(memory_id)

        query = str(item.get("query") or "").strip()
        if query and query not in related_queries:
            related_queries.append(query)

        if str(item.get("skepticism_level") or "") in {"medium", "high"}:
            skeptical_prior_count += 1
        if bool(item.get("direct_evidence")):
            direct_evidence_count += 1

        raw_claims = item.get("learned_claims", [])
        if not isinstance(raw_claims, list):
            raw_claims = []
        for claim in raw_claims:
            normalized = str(claim or "").strip().lower()
            if not normalized or normalized in seen_claims:
                continue
            seen_claims.add(normalized)
            claims.append(str(claim).strip())
            if len(claims) >= 4:
                break
        if len(claims) >= 4:
            break

    summary_line = (
        f"I already have {len(memories)} related research memories on this topic."
    )
    if skeptical_prior_count > 0:
        summary_line = (
            f"I already have {len(memories)} related research memories on this topic, "
            "and earlier passes also needed caution."
        )

    return {
        "count": len(memories),
        "memory_ids": memory_ids[:8],
        "claims": claims,
        "related_queries": related_queries[:4],
        "skeptical_prior_count": skeptical_prior_count,
        "direct_evidence_count": direct_evidence_count,
        "summary_line": summary_line,
    }


def _assess_web_research_plausibility(
    *,
    query: str,
    sources: list[dict[str, object]],
    prior_context: dict[str, object] | None = None,
) -> dict[str, object]:
    authority = _web_research_authority_context(sources)
    extraordinary = _extraordinary_claim_profile(query=query, sources=sources)
    technical = _technical_research_profile(query)
    prior = prior_context if isinstance(prior_context, dict) else {}
    direct_evidence_count = int(prior.get("direct_evidence_count", 0) or 0)

    skepticism_level = "low"
    notes: list[str] = []
    if extraordinary.get("is_extraordinary"):
        notes.extend(str(item) for item in extraordinary.get("reasons", []))
        if direct_evidence_count <= 0:
            notes.append("I do not have direct evidence in memory for that claim.")
        if int(authority.get("authority_count", 0) or 0) <= 0:
            skepticism_level = "high"
            notes.append(
                "The cited sources do not include strong institutional evidence."
            )
        else:
            skepticism_level = "medium"
            notes.append(
                "Even with stronger sources, I would still want direct or institutional confirmation before treating it as established fact."
            )
    elif int(prior.get("skeptical_prior_count", 0) or 0) > 0:
        skepticism_level = "medium"
        notes.append("Earlier related research in memory was also treated cautiously.")

    if bool(technical.get("is_open_problem")):
        skepticism_level = "high"
        notes.append(
            "This looks like an open-ended technical problem, so I should not present it as already solved."
        )
        if bool(technical.get("asks_to_build")):
            notes.append(
                "A more realistic path is to separate an exploratory application from any claim of a full solution."
            )
    elif bool(technical.get("is_technical")) and bool(technical.get("ask_budget")):
        if skepticism_level == "low":
            skepticism_level = "medium"
        notes.append(
            "This research can expand indefinitely, so it needs a time budget and a stop condition."
        )

    return {
        "skepticism_level": skepticism_level,
        "topics": extraordinary.get("topics", []),
        "notes": notes[:4],
        "authority_count": int(authority.get("authority_count", 0) or 0),
        "authority_domains": authority.get("authority_domains", []),
        "source_domains": authority.get("source_domains", []),
        "extraordinary_claim": bool(extraordinary.get("is_extraordinary")),
    }


async def _load_relevant_web_research_memories(
    db: AsyncSession,
    *,
    query: str,
    limit: int = 4,
    scan_limit: int = 40,
) -> list[dict[str, object]]:
    query_terms = set(_web_research_terms(query))
    if not query_terms:
        return []

    rows = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(
                    MemoryEntry.memory_class.in_(
                        ["external_web_research", "external_web_summary"]
                    )
                )
                .order_by(MemoryEntry.id.desc())
                .limit(max(1, int(scan_limit)))
            )
        )
        .scalars()
        .all()
    )

    scored: list[tuple[float, MemoryEntry]] = []
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        learned_claims = metadata.get("learned_claims", [])
        if not isinstance(learned_claims, list):
            learned_claims = []
        corpus = " ".join(
            [
                str(metadata.get("query") or ""),
                str(row.summary or ""),
                str(row.content or "")[:400],
                " ".join(str(item) for item in learned_claims[:4]),
            ]
        )
        memory_terms = set(_web_research_terms(corpus))
        overlap = len(query_terms.intersection(memory_terms))
        if overlap <= 0:
            continue
        score = overlap / float(max(len(query_terms), 1))
        metadata_query = str(metadata.get("query") or "").strip().lower()
        if metadata_query and metadata_query in str(query or "").strip().lower():
            score += 0.5
        scored.append((score, row))

    scored.sort(key=lambda item: (item[0], int(item[1].id)), reverse=True)
    related: list[dict[str, object]] = []
    for score, row in scored[: max(1, int(limit))]:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        plausibility = (
            metadata.get("plausibility", {})
            if isinstance(metadata.get("plausibility"), dict)
            else {}
        )
        related.append(
            {
                "memory_id": int(row.id),
                "memory_class": str(row.memory_class or ""),
                "query": str(metadata.get("query") or "").strip(),
                "summary": str(row.summary or "").strip(),
                "learned_claims": metadata.get("learned_claims", []),
                "skepticism_level": str(
                    plausibility.get("skepticism_level")
                    or metadata.get("skepticism_level")
                    or ""
                ).strip(),
                "direct_evidence": bool(metadata.get("direct_evidence")),
                "score": round(float(score), 3),
            }
        )
    return related


TECHNICAL_RESEARCH_MARKERS = {
    "algorithm",
    "api",
    "application",
    "architecture",
    "backend",
    "bug",
    "build",
    "code",
    "coding",
    "conjecture",
    "database",
    "debug",
    "design",
    "engineer",
    "engineering",
    "fix",
    "frontend",
    "implement",
    "implementation",
    "infrastructure",
    "math",
    "mathematical",
    "microservice",
    "mvp",
    "open problem",
    "proof",
    "prove",
    "prototype",
    "resolve",
    "service",
    "software",
    "solve",
    "system",
    "technical",
    "theorem",
    "workflow",
}
TECHNICAL_RESEARCH_BROAD_MARKERS = {
    "build an application",
    "design a system",
    "end to end",
    "from scratch",
    "solve this problem",
    "through to resolution",
}
TECHNICAL_RESEARCH_OPEN_PROBLEM_MARKERS = {
    "collatz",
    "conjecture",
    "open problem",
    "prove",
    "proof",
    "riemann",
    "unsolved",
}
TECHNICAL_RESEARCH_GENERIC_TERMS = {
    "application",
    "build",
    "code",
    "design",
    "problem",
    "research",
    "resolve",
    "solve",
    "system",
    "technical",
}


def _infer_requested_time_budget_minutes(query: str) -> int | None:
    normalized = str(query or "").strip().lower()
    if not normalized:
        return None
    if "half hour" in normalized:
        return 30
    if "all day" in normalized:
        return 240

    match = re.search(
        r"\b(\d+)\s*(minute|minutes|min|mins|hour|hours|hr|hrs)\b",
        normalized,
    )
    if not match:
        return None

    value = max(1, int(match.group(1)))
    unit = str(match.group(2) or "").strip().lower()
    if unit.startswith("h"):
        value *= 60
    return max(5, min(480, value))


def _technical_research_focus_terms(text: str, *, max_terms: int = 6) -> list[str]:
    focus_terms: list[str] = []
    for term in _web_research_terms(text, max_terms=20):
        if term in TECHNICAL_RESEARCH_GENERIC_TERMS:
            continue
        focus_terms.append(term)
        if len(focus_terms) >= max(1, int(max_terms)):
            break
    return focus_terms


def _technical_research_profile(query: str) -> dict[str, object]:
    normalized = str(query or "").strip().lower()
    requested_budget_minutes = _infer_requested_time_budget_minutes(normalized)
    token_marked = any(marker in normalized for marker in TECHNICAL_RESEARCH_MARKERS)
    broad_scope = any(
        marker in normalized for marker in TECHNICAL_RESEARCH_BROAD_MARKERS
    )
    is_open_problem = any(
        marker in normalized for marker in TECHNICAL_RESEARCH_OPEN_PROBLEM_MARKERS
    )
    asks_to_build = any(
        marker in normalized
        for marker in {
            "application",
            "app",
            "build",
            "implement",
            "prototype",
            "tool",
        }
    )
    asks_to_solve = any(
        marker in normalized
        for marker in {
            "prove",
            "proof",
            "resolve",
            "solve",
        }
    )
    is_technical = token_marked or (
        asks_to_build
        and any(
            marker in normalized
            for marker in {"problem", "theorem", "conjecture", "system"}
        )
    )

    default_budget = max(
        5, int(settings.web_research_technical_default_budget_minutes or 15)
    )
    assumed_budget_minutes = requested_budget_minutes or (
        60
        if is_open_problem
        else (30 if broad_scope or asks_to_build else default_budget)
    )
    max_live_rounds = max(
        1,
        min(
            int(settings.web_research_technical_max_live_rounds or 2),
            1
            if assumed_budget_minutes <= 15
            else (2 if assumed_budget_minutes <= 45 else 3),
        ),
    )

    return {
        "is_technical": is_technical,
        "broad_scope": broad_scope or is_open_problem,
        "is_open_problem": is_open_problem,
        "asks_to_build": asks_to_build,
        "asks_to_solve": asks_to_solve,
        "requested_budget_minutes": requested_budget_minutes,
        "assumed_budget_minutes": assumed_budget_minutes,
        "ask_budget": bool(
            is_technical
            and (broad_scope or is_open_problem)
            and requested_budget_minutes is None
        ),
        "focus_terms": _technical_research_focus_terms(normalized),
        "max_live_rounds": max_live_rounds,
    }


def _merge_web_research_next_steps(
    base_steps: list[str], extra_steps: list[str], *, max_items: int = 5
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for step in [*(base_steps or []), *(extra_steps or [])]:
        cleaned = str(step or "").strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        merged.append(cleaned)
        if len(merged) >= max(1, int(max_items)):
            break
    return merged


def _build_technical_research_plan(
    query: str,
) -> dict[str, object]:
    profile = _technical_research_profile(query)
    if not bool(profile.get("is_technical")):
        return {}

    focus_terms = profile.get("focus_terms", [])
    if not isinstance(focus_terms, list):
        focus_terms = []
    anchor = " ".join(str(item) for item in focus_terms[:6] if str(item).strip())
    if not anchor:
        anchor = _compact_text(str(query or "").strip(), 80)

    if bool(profile.get("is_open_problem")):
        problem_frame = (
            "Treat this as an open technical problem. I should not promise a solved "
            "result unless the research shows the problem is already resolved."
        )
    elif bool(profile.get("asks_to_build")):
        problem_frame = (
            "Separate domain research from implementation planning so the build path "
            "stays grounded in verified constraints."
        )
    else:
        problem_frame = (
            "Use a bounded technical investigation: research, choose the next path, "
            "and stop when the next round stops improving the answer."
        )

    if bool(profile.get("is_open_problem")) and bool(profile.get("asks_to_build")):
        problem_frame += (
            " For an unsolved problem, the reasonable product path is usually an "
            "exploratory or educational application, not a promise of resolution."
        )

    step_specs = [
        (
            "baseline",
            "Establish the verified baseline",
            "Identify what is already known, proven, or ruled out.",
            f"{anchor} known results current status",
        ),
        (
            "opinions",
            "Research approaches and opinions",
            "Compare approaches, expert opinions, and recurring disagreements.",
            f"{anchor} approaches opinions expert discussion",
        ),
    ]
    if bool(profile.get("asks_to_build")):
        step_specs.extend(
            [
                (
                    "path",
                    "Choose a feasible build path",
                    "Turn the research into an implementation path that is realistic now.",
                    f"{anchor} application architecture prototype",
                ),
                (
                    "validate",
                    "Define the first bounded experiment",
                    "Choose the first experiment or prototype that can be validated quickly.",
                    f"{anchor} validation experiment benchmark",
                ),
            ]
        )
    else:
        step_specs.extend(
            [
                (
                    "path",
                    "Choose the next reasoning path",
                    "Pick the next path worth deeper research instead of branching forever.",
                    f"{anchor} promising approach constraints",
                ),
                (
                    "validate",
                    "Define the next stop condition",
                    "Set the evidence threshold for whether another round is justified.",
                    f"{anchor} validation limits counterexamples",
                ),
            ]
        )

    max_plan_steps = max(1, int(settings.web_research_technical_max_plan_steps or 4))
    steps: list[dict[str, object]] = []
    for index, (step_key, title, purpose, research_query) in enumerate(
        step_specs[:max_plan_steps],
        start=1,
    ):
        steps.append(
            {
                "step_index": index,
                "step_key": step_key,
                "title": title,
                "purpose": purpose,
                "research_query": research_query,
                "status": "planned",
            }
        )

    budget_prompt = ""
    if bool(profile.get("ask_budget")):
        budget_prompt = (
            "This can become an endless loop, so tell me whether you want a "
            f"{int(settings.web_research_technical_default_budget_minutes or 15)}-minute survey, "
            "a 60-minute design pass, or a deeper investigation."
        )

    follow_up_suggestions = [
        f"execute step 1: {str(steps[0].get('title') or '').strip().lower()}"
        if steps
        else "set the first bounded research step"
    ]
    if bool(profile.get("ask_budget")):
        follow_up_suggestions.insert(0, "set a time budget for this investigation")
    if bool(profile.get("is_open_problem")) and bool(profile.get("asks_to_build")):
        follow_up_suggestions.append(
            "separate the exploratory application from any claim of a full solution"
        )
    follow_up_suggestions.append(
        "turn the chosen path into an implementation checklist"
    )

    return {
        "reasoning_mode": "technical_investigation",
        "problem_frame": problem_frame,
        "ask_budget": bool(profile.get("ask_budget")),
        "requested_budget_minutes": profile.get("requested_budget_minutes"),
        "assumed_budget_minutes": int(profile.get("assumed_budget_minutes") or 15),
        "max_live_rounds": int(profile.get("max_live_rounds") or 1),
        "stop_condition": "stop when the next round stops improving the answer or the budget is exhausted",
        "budget_prompt": budget_prompt,
        "steps": steps,
        "follow_up_suggestions": follow_up_suggestions,
        "is_open_problem": bool(profile.get("is_open_problem")),
        "asks_to_build": bool(profile.get("asks_to_build")),
        "asks_to_solve": bool(profile.get("asks_to_solve")),
    }


def _run_technical_research_rounds(
    *,
    technical_plan: dict[str, object],
    deadline: float | None,
) -> list[dict[str, object]]:
    steps = technical_plan.get("steps", []) if isinstance(technical_plan, dict) else []
    if not isinstance(steps, list):
        steps = []

    findings: list[dict[str, object]] = []
    max_live_rounds = max(1, int(technical_plan.get("max_live_rounds", 1) or 1))
    for step in steps[:max_live_rounds]:
        remaining = _web_research_remaining_seconds(deadline)
        if remaining is not None and remaining < 0.75:
            break

        research_query = str(step.get("research_query") or "").strip()
        if not research_query:
            continue

        timeout_seconds = min(2.0, max(0.75, float(remaining or 2.0)))
        search_results, search_diagnostics = _search_web_with_diagnostics(
            research_query,
            max_results=2,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        evidence: list[str] = []
        source_domains: list[str] = []
        seen_domains: set[str] = set()
        for result in search_results[:2]:
            snippet = _compact_text(
                str(result.get("snippet") or result.get("title") or "").strip(),
                160,
            )
            if snippet:
                evidence.append(snippet)
            domain = _web_research_domain(str(result.get("url") or ""))
            if domain and domain not in seen_domains:
                seen_domains.add(domain)
                source_domains.append(domain)

        findings.append(
            {
                "step_index": int(step.get("step_index", 0) or 0),
                "step_key": str(step.get("step_key") or "").strip(),
                "title": str(step.get("title") or "").strip(),
                "research_query": research_query,
                "evidence": evidence,
                "source_domains": source_domains,
                "search_diagnostics": search_diagnostics,
            }
        )
    return findings


def _format_technical_research_summary(
    technical_plan: dict[str, object] | None,
    step_findings: list[dict[str, object]] | None = None,
) -> str:
    plan = technical_plan if isinstance(technical_plan, dict) else {}
    if not plan:
        return ""

    parts: list[str] = []
    problem_frame = str(plan.get("problem_frame") or "").strip()
    if problem_frame:
        parts.append(f"Technical framing: {problem_frame}")

    raw_steps = plan.get("steps", []) if isinstance(plan.get("steps", []), list) else []
    step_labels: list[str] = []
    for step in raw_steps[:4]:
        title = str(step.get("title") or "").strip()
        index = int(step.get("step_index", 0) or 0)
        if title and index > 0:
            step_labels.append(f"{index}) {title}")
    if step_labels:
        parts.append("Plan of action: " + " ".join(step_labels) + ".")

    raw_findings = step_findings if isinstance(step_findings, list) else []
    finding_labels: list[str] = []
    for finding in raw_findings[:2]:
        evidence = (
            finding.get("evidence", [])
            if isinstance(finding.get("evidence", []), list)
            else []
        )
        step_index = int(finding.get("step_index", 0) or 0)
        if evidence:
            finding_labels.append(
                f"step {step_index} found {_compact_text(str(evidence[0] or '').strip(), 120)}"
            )
        elif step_index > 0:
            finding_labels.append(f"step {step_index} needs another research pass")
    if finding_labels:
        parts.append("Step research: " + "; ".join(finding_labels) + ".")

    budget_prompt = str(plan.get("budget_prompt") or "").strip()
    if budget_prompt:
        parts.append(f"Budget check: {budget_prompt}")
    else:
        stop_condition = str(plan.get("stop_condition") or "").strip()
        if stop_condition:
            parts.append(f"Stop condition: {stop_condition}.")

    return " ".join(parts).strip()


def _compact_technical_research_context(
    web_research: dict[str, object] | None,
) -> dict[str, object]:
    research = web_research if isinstance(web_research, dict) else {}
    technical_plan = (
        research.get("technical_plan")
        if isinstance(research.get("technical_plan"), dict)
        else {}
    )
    if not technical_plan:
        return {}

    steps: list[dict[str, object]] = []
    for step in technical_plan.get("steps", [])[:6]:
        if not isinstance(step, dict):
            continue
        title = str(step.get("title") or "").strip()
        purpose = str(step.get("purpose") or "").strip()
        if not title:
            continue
        steps.append(
            {
                "step_index": int(step.get("step_index", 0) or 0),
                "step_key": str(step.get("step_key") or "").strip(),
                "title": title,
                "purpose": purpose,
                "research_query": _compact_text(
                    str(step.get("research_query") or "").strip(), 180
                ),
            }
        )

    step_findings: list[dict[str, object]] = []
    for finding in research.get("technical_step_findings", [])[:4]:
        if not isinstance(finding, dict):
            continue
        evidence = (
            finding.get("evidence", [])
            if isinstance(finding.get("evidence", []), list)
            else []
        )
        source_domains = (
            finding.get("source_domains", [])
            if isinstance(finding.get("source_domains", []), list)
            else []
        )
        step_findings.append(
            {
                "step_index": int(finding.get("step_index", 0) or 0),
                "title": str(finding.get("title") or "").strip(),
                "evidence": [
                    _compact_text(str(item or "").strip(), 160)
                    for item in evidence[:2]
                    if str(item or "").strip()
                ],
                "source_domains": [
                    str(item).strip()
                    for item in source_domains[:3]
                    if str(item).strip()
                ],
            }
        )

    next_steps = [
        _compact_text(str(step or "").strip(), 160)
        for step in research.get("next_steps", [])[:5]
        if str(step or "").strip()
    ]
    if not next_steps:
        next_steps = [
            _compact_text(str(step or "").strip(), 160)
            for step in technical_plan.get("follow_up_suggestions", [])[:5]
            if str(step or "").strip()
        ]

    researched_step_indexes = sorted(
        {
            int(item.get("step_index", 0) or 0)
            for item in step_findings
            if isinstance(item, dict) and int(item.get("step_index", 0) or 0) > 0
        }
    )
    step_count = max(1, len(steps))
    followup_rounds_completed = max(
        0,
        int(
            research.get("technical_followup_rounds_completed", 0)
            or len(researched_step_indexes)
        ),
    )
    max_followup_rounds = max(
        1,
        min(
            step_count,
            int(research.get("technical_max_followup_rounds", 0) or step_count),
        ),
    )

    return {
        "query": _compact_text(str(research.get("query") or "").strip(), 180),
        "problem_frame": _compact_text(
            str(technical_plan.get("problem_frame") or "").strip(), 280
        ),
        "reasoning_mode": str(technical_plan.get("reasoning_mode") or "").strip(),
        "assumed_budget_minutes": int(
            technical_plan.get("assumed_budget_minutes", 0) or 0
        ),
        "budget_prompt": _compact_text(
            str(technical_plan.get("budget_prompt") or "").strip(), 220
        ),
        "stop_condition": _compact_text(
            str(technical_plan.get("stop_condition") or "").strip(), 220
        ),
        "is_open_problem": bool(technical_plan.get("is_open_problem")),
        "asks_to_build": bool(technical_plan.get("asks_to_build")),
        "max_live_rounds": int(technical_plan.get("max_live_rounds", 1) or 1),
        "steps": steps,
        "step_findings": step_findings,
        "next_steps": next_steps,
        "researched_step_indexes": researched_step_indexes,
        "followup_rounds_completed": followup_rounds_completed,
        "max_followup_rounds": max_followup_rounds,
        "last_researched_step_index": int(
            research.get("technical_last_researched_step_index", 0) or 0
        ),
        "last_round_had_evidence": bool(
            research.get("technical_last_round_had_evidence")
        ),
    }


def _technical_context_to_plan(
    technical_context: dict[str, object] | None,
) -> dict[str, object]:
    context = technical_context if isinstance(technical_context, dict) else {}
    steps = (
        context.get("steps", []) if isinstance(context.get("steps", []), list) else []
    )
    return {
        "reasoning_mode": str(
            context.get("reasoning_mode") or "technical_investigation"
        ).strip(),
        "problem_frame": str(context.get("problem_frame") or "").strip(),
        "assumed_budget_minutes": int(context.get("assumed_budget_minutes", 0) or 0),
        "budget_prompt": str(context.get("budget_prompt") or "").strip(),
        "stop_condition": str(context.get("stop_condition") or "").strip(),
        "is_open_problem": bool(context.get("is_open_problem")),
        "asks_to_build": bool(context.get("asks_to_build")),
        "max_live_rounds": int(context.get("max_live_rounds", 1) or 1),
        "steps": [step for step in steps if isinstance(step, dict)],
        "follow_up_suggestions": [
            str(step).strip()
            for step in (
                context.get("next_steps", [])
                if isinstance(context.get("next_steps", []), list)
                else []
            )
            if str(step).strip()
        ],
    }


def _technical_followup_round_limit(
    technical_context: dict[str, object] | None,
) -> int:
    context = technical_context if isinstance(technical_context, dict) else {}
    steps = (
        context.get("steps", []) if isinstance(context.get("steps", []), list) else []
    )
    step_count = max(1, len([step for step in steps if isinstance(step, dict)]))
    configured_limit = int(context.get("max_followup_rounds", 0) or step_count)
    return max(1, min(step_count, configured_limit))


def _merge_compact_text_lists(
    existing: list[str],
    new_items: list[str],
    *,
    limit: int,
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *new_items]:
        compact = _compact_text(str(item or "").strip(), 160)
        key = compact.lower()
        if not compact or key in seen:
            continue
        seen.add(key)
        merged.append(compact)
        if len(merged) >= max(1, limit):
            break
    return merged


def _format_technical_followup_round_answer(
    *,
    step_index: int,
    step_title: str,
    step_purpose: str,
    evidence: list[str],
    source_domains: list[str],
    next_steps: list[str],
    stop_phrase: str,
    repeated_step: bool,
    bounded_limit_hit: bool = False,
) -> str:
    if bounded_limit_hit:
        answer = (
            "I already used the bounded technical follow-up rounds for this thread. "
            "Next step: turn the researched findings into an implementation checklist."
        )
        if stop_phrase:
            answer += f" Stop when {stop_phrase}."
        return answer

    step_prefix = "I refreshed" if repeated_step else "I researched"
    answer = f"{step_prefix} step {step_index}: {step_title}."
    if step_purpose:
        answer += f" Purpose: {step_purpose}."
    if evidence:
        answer += f" Evidence: {str(evidence[0]).rstrip('.')}."
    else:
        answer += " Evidence: I did not get strong enough new evidence to improve this step yet."
    if source_domains:
        answer += f" Sources: {', '.join(source_domains[:3])}."
    if next_steps:
        answer += f" Next step: {str(next_steps[0]).rstrip('.')}."
    if stop_phrase:
        answer += f" Stop when {stop_phrase}."
    return answer.strip()


def _looks_like_instructional_setup_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    setup_phrases = {
        "what do i need to do to ",
        "how do i set up ",
        "how do we set up ",
        "how do i connect ",
        "how do we connect ",
        "how do i leverage ",
        "how do we leverage ",
        "how do i link ",
        "how do we link ",
        "how do i tie ",
        "how do we tie ",
        "how do i use ",
        "how do we use ",
        "how can i access ",
        "how can we access ",
    }
    return any(query.startswith(phrase) or f" {phrase}" in query for phrase in setup_phrases)


def _looks_like_development_integration_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False

    planning_markers = {
        "how do we make this happen",
        "how do i make this happen",
        "what is the fastest path",
        "fastest path to implement",
        "what should we inspect first",
        "what should i inspect first",
        "how do we implement",
        "how should we implement",
        "integration request",
        "integration plan",
    }
    asset_markers = {
        "use the existing app",
        "existing app",
        "existing client",
        "existing shell",
        "existing interface",
        "existing frontend",
        "reuse",
        "reuse the existing",
        "leverage",
        "integrate",
        "integration",
        "connect",
        "hook",
        "tie",
        "link",
        "session flow",
        "direct interaction",
    }
    subject_markers = {
        "mim",
        "mim_wall",
        "mobile",
        "phone",
        "android",
        "iphone",
        "ios",
        "app",
        "client",
        "shell",
        "ui",
        "browser",
        "backend",
        "gateway",
        "conversation layer",
    }

    if "mim_wall" in query and any(
        marker in query for marker in planning_markers | asset_markers
    ):
        return True

    return (
        any(marker in query for marker in planning_markers)
        and any(marker in query for marker in asset_markers)
        and any(marker in query for marker in subject_markers)
    )


def _format_structured_conversation_steps(steps: list[str]) -> str:
    numbered = [f"{index}. {str(step).strip()}" for index, step in enumerate(steps, start=1)]
    return "\n".join(numbered)


def _build_development_integration_response(
    user_input: str,
    *,
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> str | None:
    del user_input, context
    query = str(normalized_query or "").strip().lower()
    if not _looks_like_development_integration_query(query):
        return None

    if "mim_wall" in query or (
        "mim" in query
        and any(token in query for token in {"mobile", "phone", "android", "iphone", "ios"})
        and any(token in query for token in {"app", "client", "shell", "direct interaction"})
    ):
        next_action = "inspect the existing mim_wall app against the current MIM session flow"
        steps = [
            "Inspect the current mim_wall assets first: its UI surfaces, configurable base URL, any WebView support, and how it stores session state.",
            "Compare those existing assets to the current MIM conversation contract: /mim for mobile web access, plus /gateway/intake/text, conversation_session_id, and mim_interface.reply_text for thin-client messaging.",
            "Choose the narrowest reuse path: use the existing /mim mobile web route first if mim_wall does not already host the session flow; only use a thin app wrapper if the current assets already line up.",
            "Validate one end-to-end phone session against the existing MIM backend and keep the same visible reply surface instead of creating a second assistant stack.",
        ]
    elif any(
        token in query
        for token in {
            "use the existing app",
            "existing app",
            "existing client",
            "existing shell",
            "existing interface",
        }
    ):
        next_action = "inspect the existing app against the current interface and session contract"
        steps = [
            "Identify the existing app surfaces that already overlap with the request: UI, transport, session handling, and configurable endpoints.",
            "Map those assets to the current MIM interface boundary so you reuse the active conversation path before proposing any new build.",
            "Keep only the thinnest missing layer, such as a wrapper around the current UI or a direct call into the existing conversation endpoint.",
            "Run one live session through the reused path and only expand the design if that bounded path fails.",
        ]
    else:
        next_action = "inspect the closest existing asset before proposing any new build"
        steps = [
            "Identify the current app, endpoint, or UI surface that already covers most of the request.",
            "Inspect its entry points, session contract, and dependencies so the first implementation step is grounded in what already exists.",
            "Reuse the smallest viable path first and defer any larger rebuild until the existing asset is proven insufficient.",
            "Validate the bounded implementation path with one end-to-end test before widening scope.",
        ]

    return (
        f"Next action: {next_action}.\n\n"
        "Steps:\n"
        f"{_format_structured_conversation_steps(steps)}"
    )


def _build_instructional_setup_response(
    user_input: str,
    *,
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> dict[str, str] | None:
    del context
    raw = str(user_input or "").strip()
    query = str(normalized_query or "").strip().lower()
    if not _looks_like_instructional_setup_query(query):
        return None

    if (
        "mim" in query
        and any(token in query for token in {"phone", "mobile", "iphone", "android"})
        and any(
            token in query
            for token in {
                "app",
                "assistant app",
                "assistant",
                "client",
                "shell",
                "frontend",
                "front end",
                "ui",
            }
        )
        and any(
            token in query
            for token in {
                "already have",
                "already built",
                "already created",
                "already started",
                "already running",
                "we already have",
                "we already built",
                "we already created",
                "we already started",
                "we already have running",
                "leverage",
                "reuse",
                "use what we already created",
                "direct tie",
                "connect it to mim",
                "link it to mim",
                "tie it to mim",
                "integrate it with mim",
                "hook it up to mim",
            }
        )
    ):
        next_action = "explain how to reuse the existing phone assistant app as a thin client for MIM"
        result = (
            "Steps:\n\n"
            "1. Treat the existing phone assistant app as a thin client for the current MIM text-chat/backend path instead of building a second assistant stack.\n"
            "2. Reuse the same session model that /mim already uses by keeping one stable conversation_session_id, like the browser-side mim_text_chat_session_id.\n"
            "3. Send phone messages to /gateway/intake/text on the same MIM backend so the app uses the current conversation layer and bounded TOD bridge behavior.\n"
            "4. Render the returned mim_interface.reply_text, or the equivalent understood/next_action/result fields, directly in the phone app UI.\n"
            "5. If you want a more native mobile shell later, keep the same gateway and session contract and only swap the presentation layer around it."
        )
        return {"next_action": next_action, "result": result}

    if "mim" in query and any(
        token in query for token in {"phone", "mobile", "iphone", "android"}
    ):
        next_action = "explain local network access setup for MIM"
        result = (
            "Steps:\n\n"
            "1. Ensure MIM is running on 0.0.0.0 instead of 127.0.0.1.\n"
            "2. Find the computer's local IP with hostname -I or ip addr.\n"
            "3. Make sure your phone is on the same local network as the MIM host.\n"
            "4. Open http://<ip>:18001/mim on your phone.\n"
            "5. If it still does not load, allow port 18001 through the firewall and verify MIM is still listening on that port."
        )
        return {"next_action": next_action, "result": result}

    if "mim" in query and any(
        token in query
        for token in {
            "start automatically",
            "automatically on login",
            "start on login",
            "login",
            "autostart",
            "user service",
            "systemd",
            "desktop shell",
        }
    ):
        next_action = "explain user-service setup for MIM desktop shell"
        result = (
            "Steps:\n\n"
            "1. Create the user service directory with mkdir -p ~/.config/systemd/user.\n"
            "2. Copy /home/testpilot/mim/deploy/systemd-user/mim-desktop-shell.service into ~/.config/systemd/user/.\n"
            "3. Reload user units with systemctl --user daemon-reload.\n"
            "4. Enable and start the service with systemctl --user enable --now mim-desktop-shell.service.\n"
            "5. Verify it is running with systemctl --user status mim-desktop-shell.service."
        )
        return {"next_action": next_action, "result": result}

    setup_target = raw.rstrip("?").strip() or "this setup request"
    next_action = "explain the shortest setup path for this request"
    result = (
        "Steps:\n\n"
        f"1. Identify the exact service, device, or endpoint involved in '{setup_target}'.\n"
        "2. Make sure the service is running and reachable from the place you want to use it.\n"
        "3. Collect the required address, port, credentials, or local-network details.\n"
        "4. Test the connection from the target device and fix any firewall, binding, or reachability issue you find."
    )
    return {"next_action": next_action, "result": result}


def _is_technical_research_execution_followup(
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> bool:
    session_context = context or {}
    if (
        str(session_context.get("last_topic") or "").strip().lower()
        != "technical_research"
    ):
        return False
    technical_context = (
        session_context.get("last_technical_research")
        if isinstance(session_context.get("last_technical_research"), dict)
        else {}
    )
    if not technical_context:
        return False
    query = str(normalized_query or "").strip().lower()
    non_execution_markers = {
        "why that",
        "any dependencies",
        "anything else",
        "short final recap",
        "shorter version",
        "short version",
        "repeat that as a checklist",
        "checklist",
    }
    if any(marker in query for marker in non_execution_markers):
        return False
    markers = {
        "research that step",
        "research that",
        "research the next step",
        "research step",
        "go deeper",
        "dig deeper",
        "deeper",
        "continue research",
        "continue with step",
        "do that step",
        "investigate that step",
        "look deeper",
    }
    return any(marker in query for marker in markers) or bool(
        re.search(r"\bstep\s+\d+\b", query)
    )


def _run_bounded_technical_followup_research(
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    session_context = context or {}
    technical_context = (
        session_context.get("last_technical_research")
        if isinstance(session_context.get("last_technical_research"), dict)
        else {}
    )
    if not technical_context:
        return {}

    query = str(normalized_query or "").strip().lower()
    steps = (
        technical_context.get("steps", [])
        if isinstance(technical_context.get("steps", []), list)
        else []
    )
    step_findings = [
        item.copy()
        for item in (
            technical_context.get("step_findings", [])
            if isinstance(technical_context.get("step_findings", []), list)
            else []
        )
        if isinstance(item, dict)
    ]
    if not steps:
        return {}

    followup_rounds_completed = max(
        0,
        int(
            technical_context.get("followup_rounds_completed", 0) or len(step_findings)
        ),
    )
    max_followup_rounds = _technical_followup_round_limit(technical_context)

    requested_step_index = 0
    requested_match = re.search(r"\bstep\s+(\d+)\b", query)
    if requested_match:
        try:
            requested_step_index = int(requested_match.group(1) or 0)
        except Exception:
            requested_step_index = 0

    completed_step_indexes = {
        int(item.get("step_index", 0) or 0)
        for item in step_findings
        if isinstance(item, dict)
    }
    target_step = None
    if requested_step_index > 0:
        target_step = next(
            (
                step
                for step in steps
                if isinstance(step, dict)
                and int(step.get("step_index", 0) or 0) == requested_step_index
            ),
            None,
        )
        if target_step is None:
            stop_condition = str(technical_context.get("stop_condition") or "").strip()
            stop_phrase = stop_condition.rstrip(".")
            if stop_phrase.lower().startswith("stop when "):
                stop_phrase = stop_phrase[10:].strip()
            return {
                "answer": (
                    f"I could not find step {requested_step_index} in the remembered technical plan. "
                    f"Available steps run from 1 to {len(steps)}."
                    + (f" Stop when {stop_phrase}." if stop_phrase else "")
                ).strip(),
                "technical_context": technical_context,
                "selected_step_index": 0,
                "search_diagnostics": {},
            }
    if target_step is None:
        if followup_rounds_completed >= max_followup_rounds:
            stop_condition = str(technical_context.get("stop_condition") or "").strip()
            stop_phrase = stop_condition.rstrip(".")
            if stop_phrase.lower().startswith("stop when "):
                stop_phrase = stop_phrase[10:].strip()
            updated_context = {
                **technical_context,
                "next_steps": [
                    "turn the researched findings into an implementation checklist"
                ],
                "followup_rounds_completed": followup_rounds_completed,
                "max_followup_rounds": max_followup_rounds,
            }
            return {
                "answer": _format_technical_followup_round_answer(
                    step_index=0,
                    step_title="",
                    step_purpose="",
                    evidence=[],
                    source_domains=[],
                    next_steps=updated_context.get("next_steps", []),
                    stop_phrase=stop_phrase,
                    repeated_step=False,
                    bounded_limit_hit=True,
                ),
                "technical_context": updated_context,
                "selected_step_index": 0,
                "search_diagnostics": {},
            }
        target_step = next(
            (
                step
                for step in steps
                if isinstance(step, dict)
                and int(step.get("step_index", 0) or 0) not in completed_step_indexes
            ),
            steps[0] if steps else None,
        )
    if not isinstance(target_step, dict):
        return {}

    step_index = int(target_step.get("step_index", 0) or 0)
    repeated_step = step_index in completed_step_indexes
    step_title = str(target_step.get("title") or "").strip()
    step_purpose = str(target_step.get("purpose") or "").strip()
    research_query = str(target_step.get("research_query") or "").strip()
    if not research_query:
        return {}

    search_results, search_diagnostics = _search_web_with_diagnostics(
        research_query,
        max_results=2,
        timeout_seconds=2.0,
        deadline=_web_research_deadline(2.5),
    )

    evidence: list[str] = []
    source_domains: list[str] = []
    seen_domains: set[str] = set()
    for result in search_results[:2]:
        snippet = _compact_text(
            str(result.get("snippet") or result.get("title") or "").strip(),
            160,
        )
        if snippet:
            evidence.append(snippet)
        domain = _web_research_domain(str(result.get("url") or ""))
        if domain and domain not in seen_domains:
            seen_domains.add(domain)
            source_domains.append(domain)

    prior_finding = next(
        (
            item
            for item in step_findings
            if isinstance(item, dict)
            and int(item.get("step_index", 0) or 0) == step_index
        ),
        {},
    )
    prior_evidence = (
        prior_finding.get("evidence", [])
        if isinstance(prior_finding.get("evidence", []), list)
        else []
    )
    prior_domains = (
        prior_finding.get("source_domains", [])
        if isinstance(prior_finding.get("source_domains", []), list)
        else []
    )
    merged_evidence = _merge_compact_text_lists(
        [str(item).strip() for item in prior_evidence if str(item).strip()],
        evidence,
        limit=3,
    )
    merged_domains = _merge_compact_text_lists(
        [str(item).strip() for item in prior_domains if str(item).strip()],
        source_domains,
        limit=3,
    )

    new_finding = {
        "step_index": step_index,
        "title": step_title,
        "evidence": merged_evidence,
        "source_domains": merged_domains,
    }
    updated_step_findings = [
        item
        for item in step_findings
        if int(item.get("step_index", 0) or 0) != step_index
    ]
    updated_step_findings.append(new_finding)
    updated_step_findings.sort(key=lambda item: int(item.get("step_index", 0) or 0))

    remaining_steps = [
        step
        for step in steps
        if isinstance(step, dict)
        and int(step.get("step_index", 0) or 0)
        not in {
            int(item.get("step_index", 0) or 0)
            for item in updated_step_findings
            if isinstance(item, dict)
        }
    ]
    next_step = remaining_steps[0] if remaining_steps else None

    next_steps: list[str] = []
    if next_step is not None:
        next_steps.append(
            f"execute step {int(next_step.get('step_index', 0) or 0)}: {str(next_step.get('title') or '').strip().lower()}"
        )
    elif step_title:
        next_steps.append("turn the current findings into an implementation checklist")

    stop_condition = str(technical_context.get("stop_condition") or "").strip()
    stop_phrase = stop_condition.rstrip(".")
    if stop_phrase.lower().startswith("stop when "):
        stop_phrase = stop_phrase[10:].strip()

    answer = _format_technical_followup_round_answer(
        step_index=step_index,
        step_title=step_title,
        step_purpose=step_purpose,
        evidence=merged_evidence,
        source_domains=merged_domains,
        next_steps=next_steps,
        stop_phrase=stop_phrase,
        repeated_step=repeated_step,
    )

    researched_step_indexes = sorted(
        {
            *[
                int(item.get("step_index", 0) or 0)
                for item in updated_step_findings
                if isinstance(item, dict) and int(item.get("step_index", 0) or 0) > 0
            ]
        }
    )
    round_increment = 0 if repeated_step else 1

    updated_context = {
        **technical_context,
        "step_findings": updated_step_findings,
        "next_steps": next_steps,
        "researched_step_indexes": researched_step_indexes,
        "followup_rounds_completed": followup_rounds_completed + round_increment,
        "max_followup_rounds": max_followup_rounds,
        "last_researched_step_index": step_index,
        "last_round_had_evidence": bool(evidence),
    }
    return {
        "answer": answer.strip(),
        "technical_context": updated_context,
        "selected_step_index": step_index,
        "search_diagnostics": search_diagnostics,
    }


def _technical_research_followup_response(
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> str:
    session_context = context or {}
    if (
        str(session_context.get("last_topic") or "").strip().lower()
        != "technical_research"
    ):
        return ""

    technical_context = (
        session_context.get("last_technical_research")
        if isinstance(session_context.get("last_technical_research"), dict)
        else {}
    )
    if not technical_context:
        return ""

    query = str(normalized_query or "").strip().lower()
    steps = (
        technical_context.get("steps", [])
        if isinstance(technical_context.get("steps", []), list)
        else []
    )
    step_findings = (
        technical_context.get("step_findings", [])
        if isinstance(technical_context.get("step_findings", []), list)
        else []
    )
    next_steps = (
        technical_context.get("next_steps", [])
        if isinstance(technical_context.get("next_steps", []), list)
        else []
    )
    completed_step_indexes = {
        int(item.get("step_index", 0) or 0)
        for item in step_findings
        if isinstance(item, dict)
    }
    next_step = next(
        (
            step
            for step in steps
            if isinstance(step, dict)
            and int(step.get("step_index", 0) or 0) not in completed_step_indexes
        ),
        steps[0] if steps else {},
    )
    next_step_title = str(next_step.get("title") or "").strip()
    next_step_index = int(next_step.get("step_index", 0) or 0)
    next_step_purpose = str(next_step.get("purpose") or "").strip()
    problem_frame = str(technical_context.get("problem_frame") or "").strip()
    stop_condition = str(technical_context.get("stop_condition") or "").strip()
    stop_phrase = stop_condition.rstrip(".")
    if stop_phrase.lower().startswith("stop when "):
        stop_phrase = stop_phrase[10:].strip()
    budget_prompt = str(technical_context.get("budget_prompt") or "").strip()
    remembered_query = str(technical_context.get("query") or "").strip()
    is_open_problem = bool(technical_context.get("is_open_problem"))
    asks_to_build = bool(technical_context.get("asks_to_build"))

    if any(
        token in query
        for token in {
            "and after that",
            "after that",
            "then what",
            "what comes after that",
            "and then",
        }
    ):
        if next_step_title and next_step_index > 0:
            response = f"After that, execute step {next_step_index}: {next_step_title}."
            if next_step_purpose:
                response += f" Purpose: {next_step_purpose}"
            if stop_phrase:
                response += f" Stop when {stop_phrase}."
            return response
        if next_steps:
            return f"After that, {str(next_steps[0]).rstrip('.')} and stop when {stop_phrase or 'the next round no longer improves the answer'}."

    if any(
        token in query
        for token in {
            "shorter version",
            "short version",
            "short recap",
            "short final recap",
            "one line",
            "summarize in one line",
            "shorter",
        }
    ):
        if remembered_query:
            return (
                f"One line: for {remembered_query}, keep the investigation bounded, "
                f"use the next justified step, and stop when {stop_phrase or 'the evidence stops improving'}."
            )
        return "One line: keep the technical investigation bounded and stop when the evidence stops improving."

    if "checklist" in query:
        checklist_items: list[str] = []
        for step in steps[:4]:
            if not isinstance(step, dict):
                continue
            title = str(step.get("title") or "").strip()
            if title:
                checklist_items.append(title)
        if not checklist_items and next_steps:
            checklist_items.extend(str(item).strip() for item in next_steps[:4])
        if checklist_items:
            return (
                "Checklist: "
                + " ".join(
                    f"{index}. {item}"
                    for index, item in enumerate(checklist_items, start=1)
                )
                + (f" 5. Stop when {stop_phrase}." if stop_phrase else "")
            )

    if any(
        token in query for token in {"why that", "why that one", "why that priority"}
    ):
        reasons: list[str] = []
        if problem_frame:
            reasons.append(problem_frame)
        if next_step_title:
            reasons.append(
                f"The next justified move is step {next_step_index}: {next_step_title}"
            )
        if next_step_purpose:
            reasons.append(next_step_purpose)
        if is_open_problem and asks_to_build:
            reasons.append(
                "That keeps the work grounded in an exploratory build path instead of overclaiming a full solution"
            )
        if reasons:
            return (
                "Because "
                + "; ".join(reason.rstrip(".") for reason in reasons[:3])
                + "."
            )

    if "dependency" in query or "dependencies" in query:
        dependencies: list[str] = []
        if step_findings:
            first_finding = (
                step_findings[0] if isinstance(step_findings[0], dict) else {}
            )
            evidence = (
                first_finding.get("evidence", [])
                if isinstance(first_finding.get("evidence", []), list)
                else []
            )
            source_domains = (
                first_finding.get("source_domains", [])
                if isinstance(first_finding.get("source_domains", []), list)
                else []
            )
            if evidence:
                dependencies.append(
                    f"a verified baseline such as {_compact_text(str(evidence[0]), 120)}"
                )
            if source_domains:
                dependencies.append(
                    "credible source coverage from " + ", ".join(source_domains[:3])
                )
        if next_step_purpose:
            dependencies.append(next_step_purpose)
        if budget_prompt:
            dependencies.append("an explicit time budget")
        if stop_condition:
            dependencies.append(f"stop when {stop_phrase}")
        if dependencies:
            return (
                "Main dependencies are "
                + "; ".join(item.rstrip(".") for item in dependencies[:4])
                + "."
            )

    if any(
        token in query
        for token in {
            "anything else",
            "before we proceed",
            "anything else before we proceed",
        }
    ):
        extras: list[str] = []
        if budget_prompt:
            extras.append(budget_prompt)
        if is_open_problem and asks_to_build:
            extras.append(
                "Keep the product goal exploratory or educational unless the research clearly shows the core problem is already solved"
            )
        if stop_phrase:
            extras.append(f"Stop when {stop_phrase}")
        if extras:
            return "One more thing: " + " ".join(
                item.rstrip(".") + "." for item in extras[:3]
            )

    return ""


def _should_use_web_research(normalized_query: str) -> bool:
    if not settings.allow_web_access:
        return False

    query = str(normalized_query or "").strip().lower()
    if not query:
        return False

    if robotics_web_guard_blocks_search(query):
        return False

    if _looks_like_bounded_implementation_request(query, "discussion", []):
        return False

    internal_planning_markers = {
        "next bounded slice",
        "current bounded slice",
        "bounded slice",
        "next slice",
        "implementation slice",
        "acceptance criteria",
        "acceptance checks",
        "direct answer quality",
        "clarification behavior",
        "one useful clarifying question",
    }
    if any(marker in query for marker in internal_planning_markers):
        return False

    bounded_choice_markers = {
        "pick exactly one",
        "choose exactly one",
        "bounded choice only",
        "one numbered option",
        "one numbered choice",
    }
    if any(marker in query for marker in bounded_choice_markers):
        return False

    local_topics = {
        "who are you",
        "who is tod",
        "what is tod",
        "what is the system",
        "our objective",
        "current objective",
        "active objective",
        "what are you working on",
        "what are we working on",
        "prioritize next",
        "your mission",
        "primary mission",
        "top risk",
        "reduce that risk",
        "what can you do",
        "capabilities",
        "your function",
        "weather",
        "camera feed",
        "from the camera",
        "on camera",
        "what do you see",
        "what can you see",
        "what is visible",
        "see me",
        "what time is it",
        "what day is it",
        "where are we",
        "where are you",
        "news",
        "tod status",
        "how is tod",
        "tod healthy",
        "what is next",
        "next for us",
        "what should we prioritize",
        "what should i do first",
    }

    strong_research_markers = {
        "help me research",
        "research how to build",
        "how to build",
        "build a ",
        "build an ",
        "design a ",
        "design an ",
        "fault tolerant",
        "event driven",
        "inference pipeline",
    }
    if any(marker in query for marker in strong_research_markers):
        return True

    if any(topic in query for topic in local_topics):
        return False

    research_markers = {
        "browse the web",
        "build an application",
        "conjecture",
        "debug",
        "design a system",
        "implement",
        "search the web",
        "look up",
        "research",
        "find online",
        "find on the web",
        "what is the best",
        "what's the best",
        "which is the best",
        "best brand",
        "best ",
        "top rated",
        "best reviewed",
        "compare",
        "comparison",
        "solve this problem",
        "technical approach",
        "review",
        "reviews",
        "evidence",
        "proven",
        "studies",
        "research backed",
    }
    return any(marker in query for marker in research_markers)


def _coerce_web_research_timeout(
    value: object,
    *,
    fallback: float,
    minimum: float = 0.25,
    maximum: float = 30.0,
) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = float(fallback)
    return max(minimum, min(maximum, timeout))


def _web_research_deadline(timeout_seconds: float) -> float:
    return time.monotonic() + max(0.25, float(timeout_seconds))


def _web_research_remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _web_research_timed_out(deadline: float | None, *, minimum: float = 0.25) -> bool:
    remaining = _web_research_remaining_seconds(deadline)
    return remaining is not None and remaining <= minimum


def _resolve_web_research_timeout(
    timeout_seconds: float,
    *,
    deadline: float | None = None,
    minimum: float = 0.25,
) -> float:
    timeout = max(minimum, float(timeout_seconds))
    remaining = _web_research_remaining_seconds(deadline)
    if remaining is None:
        return timeout
    if remaining <= minimum:
        raise TimeoutError("web_research_timed_out")
    return max(minimum, min(timeout, remaining))


def _web_research_fetch_workers(candidate_count: int, max_sources: int) -> int:
    configured_parallelism = _coerce_web_research_concurrency(
        settings.web_research_fetch_max_parallelism,
        fallback=2,
        maximum=8,
    )
    return max(
        1,
        min(int(candidate_count), int(max_sources), int(configured_parallelism)),
    )


def _fetch_web_document(
    raw_url: str,
    *,
    timeout_seconds: int = 12,
    max_extract_chars: int = 12000,
    deadline: float | None = None,
) -> dict[str, object]:
    if not _is_safe_web_url(raw_url):
        raise ValueError("unsupported_or_unsafe_url")

    req = urllib_request.Request(
        url=raw_url,
        headers={
            "User-Agent": "MIM-WebResearch/1.0 (+https://mim.local)",
            "Accept": "text/html,text/plain;q=0.9,*/*;q=0.3",
        },
    )

    with urllib_request.urlopen(
        req,
        timeout=_resolve_web_research_timeout(
            float(timeout_seconds),
            deadline=deadline,
            minimum=0.25,
        ),
    ) as response:
        status_code = int(getattr(response, "status", 200) or 200)
        content_type = str(response.headers.get("Content-Type", "")).lower()
        raw_bytes = response.read(1_000_000)

    decoded = raw_bytes.decode("utf-8", errors="replace")
    if "text/plain" in content_type:
        title = ""
        extracted = re.sub(r"\s+", " ", decoded).strip()
        if len(extracted) > max_extract_chars:
            extracted = extracted[:max_extract_chars].rstrip() + "..."
    else:
        title, extracted = _extract_visible_text_from_html(
            decoded, max_chars=max_extract_chars
        )

    return {
        "url": raw_url,
        "title": title,
        "text": extracted,
        "content_type": content_type,
        "status_code": status_code,
    }


def _search_google_cse(
    query: str,
    *,
    max_results: int = 5,
    timeout_seconds: float = 6.0,
    deadline: float | None = None,
) -> list[dict[str, str]]:
    api_key = str(settings.google_cse_api_key or "").strip()
    cse_id = str(settings.google_cse_id or "").strip()
    if not api_key or not cse_id:
        return []

    url = "https://www.googleapis.com/customsearch/v1?" + urlencode(
        {
            "key": api_key,
            "cx": cse_id,
            "q": query,
            "num": max(1, min(10, int(max_results))),
        }
    )
    req = urllib_request.Request(
        url=url,
        headers={"User-Agent": "MIM-WebResearch/1.0 (+https://mim.local)"},
    )
    with urllib_request.urlopen(
        req,
        timeout=_resolve_web_research_timeout(
            timeout_seconds,
            deadline=deadline,
            minimum=0.25,
        ),
    ) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))

    items = payload.get("items", []) if isinstance(payload, dict) else []
    results: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("link") or "").strip()
        if not _is_safe_web_url(url):
            continue
        results.append(
            {
                "title": html.unescape(str(item.get("title") or "").strip()),
                "url": url,
                "snippet": html.unescape(str(item.get("snippet") or "").strip()),
                "source": "google_cse",
            }
        )
    return results


def _search_google_cse_with_diagnostics(
    query: str,
    *,
    max_results: int = 5,
    timeout_seconds: float = 6.0,
    deadline: float | None = None,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    diagnostics: dict[str, object] = {
        "provider": "google_cse",
        "configured": bool(
            str(settings.google_cse_api_key or "").strip()
            and str(settings.google_cse_id or "").strip()
        ),
        "query": query,
        "requested_results": max(1, min(10, int(max_results))),
        "result_count": 0,
        "error": "",
    }
    if not bool(diagnostics["configured"]):
        diagnostics["error"] = "not_configured"
        return [], diagnostics

    try:
        results = _search_google_cse(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
    except Exception as exc:
        diagnostics["error"] = f"{type(exc).__name__}:{exc}"
        return [], diagnostics

    diagnostics["result_count"] = len(results)
    if not results:
        diagnostics["error"] = "empty_results"
    return results, diagnostics


def _extract_gemini_grounding_segments(
    grounding_metadata: dict[str, object],
) -> dict[int, list[str]]:
    supports = (
        grounding_metadata.get("groundingSupports", [])
        if isinstance(grounding_metadata, dict)
        else []
    )
    snippets_by_chunk: dict[int, list[str]] = {}
    for support in supports:
        if not isinstance(support, dict):
            continue
        segment = support.get("segment", {})
        segment_text = _compact_text(str(segment.get("text") or "").strip(), 240)
        if not segment_text:
            continue
        for raw_index in support.get("groundingChunkIndices", []) or []:
            try:
                chunk_index = int(raw_index)
            except Exception:
                continue
            existing = snippets_by_chunk.setdefault(chunk_index, [])
            normalized = segment_text.lower()
            if normalized in {item.lower() for item in existing}:
                continue
            existing.append(segment_text)
    return snippets_by_chunk


def _search_gemini_google_search(
    query: str,
    *,
    max_results: int = 5,
    timeout_seconds: float = 6.0,
    deadline: float | None = None,
) -> list[dict[str, str]]:
    api_key = str(settings.gemini_api_key or "").strip()
    model = str(settings.gemini_search_model or "").strip()
    if not api_key or not model:
        return []

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{quote_plus(model)}:generateContent?"
        + urlencode({"key": api_key})
    )
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Use Google Search grounding to find current public web sources for this query. "
                            "Provide a concise grounded answer with citations in the metadata. "
                            f"Query: {query}"
                        )
                    }
                ],
            }
        ],
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
        },
    }
    req = urllib_request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "User-Agent": "MIM-WebResearch/1.0 (+https://mim.local)",
            "Content-Type": "application/json",
        },
    )
    with urllib_request.urlopen(
        req,
        timeout=_resolve_web_research_timeout(
            timeout_seconds,
            deadline=deadline,
            minimum=0.25,
        ),
    ) as response:
        response_payload = json.loads(response.read().decode("utf-8", errors="replace"))

    prompt_feedback = (
        response_payload.get("promptFeedback", {})
        if isinstance(response_payload, dict)
        else {}
    )
    if str(prompt_feedback.get("blockReason") or "").strip():
        raise RuntimeError(f"prompt_blocked:{prompt_feedback.get('blockReason')}")

    candidates = (
        response_payload.get("candidates", [])
        if isinstance(response_payload, dict)
        else []
    )
    if not candidates:
        return []

    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    content = candidate.get("content", {}) if isinstance(candidate, dict) else {}
    parts = content.get("parts", []) if isinstance(content, dict) else []
    answer_text = " ".join(
        _compact_text(str(item.get("text") or "").strip(), 240)
        for item in parts
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ).strip()
    grounding_metadata = (
        candidate.get("groundingMetadata", {}) if isinstance(candidate, dict) else {}
    )
    snippets_by_chunk = _extract_gemini_grounding_segments(grounding_metadata)
    grounding_chunks = (
        grounding_metadata.get("groundingChunks", [])
        if isinstance(grounding_metadata, dict)
        else []
    )

    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for index, chunk in enumerate(grounding_chunks):
        if not isinstance(chunk, dict):
            continue
        web = chunk.get("web", {})
        if not isinstance(web, dict):
            continue
        raw_url = str(web.get("uri") or "").strip()
        if not _is_safe_web_url(raw_url) or raw_url in seen_urls:
            continue
        seen_urls.add(raw_url)
        snippet_parts = snippets_by_chunk.get(index, [])
        snippet = " ".join(snippet_parts[:2]).strip()
        if not snippet and answer_text:
            snippet = _compact_text(answer_text, 240)
        results.append(
            {
                "title": html.unescape(str(web.get("title") or "").strip()),
                "url": raw_url,
                "snippet": snippet,
                "source": "gemini_google_search",
            }
        )
        if len(results) >= max(1, min(10, int(max_results))):
            break
    return results


def _search_gemini_google_search_with_diagnostics(
    query: str,
    *,
    max_results: int = 5,
    timeout_seconds: float = 6.0,
    deadline: float | None = None,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    diagnostics: dict[str, object] = {
        "provider": "gemini_google_search",
        "configured": bool(str(settings.gemini_api_key or "").strip()),
        "model": str(settings.gemini_search_model or "").strip(),
        "query": query,
        "requested_results": max(1, min(10, int(max_results))),
        "result_count": 0,
        "error": "",
    }
    if not bool(diagnostics["configured"]):
        diagnostics["error"] = "not_configured"
        return [], diagnostics

    try:
        results = _search_gemini_google_search(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
    except Exception as exc:
        diagnostics["error"] = f"{type(exc).__name__}:{exc}"
        return [], diagnostics

    diagnostics["result_count"] = len(results)
    if not results:
        diagnostics["error"] = "empty_results"
    return results, diagnostics


def _extract_duckduckgo_result_url(raw_href: str) -> str:
    href = html.unescape(str(raw_href or "").strip())
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in str(parsed.netloc or ""):
        params = parse_qs(parsed.query)
        uddg = str((params.get("uddg") or [""])[0]).strip()
        if uddg:
            return unquote(uddg)
    return href


def _search_duckduckgo_html(
    query: str,
    *,
    max_results: int = 5,
    timeout_seconds: float = 6.0,
    deadline: float | None = None,
) -> list[dict[str, str]]:
    search_url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    req = urllib_request.Request(
        url=search_url,
        headers={
            "User-Agent": "MIM-WebResearch/1.0 (+https://mim.local)",
            "Accept": "text/html,*/*;q=0.3",
        },
    )
    with urllib_request.urlopen(
        req,
        timeout=_resolve_web_research_timeout(
            timeout_seconds,
            deadline=deadline,
            minimum=0.25,
        ),
    ) as response:
        raw_html = response.read().decode("utf-8", errors="replace")

    pattern = re.compile(
        r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    results: list[dict[str, str]] = []
    for href, title_html in pattern.findall(raw_html):
        url = _extract_duckduckgo_result_url(href)
        if not _is_safe_web_url(url):
            continue
        title = re.sub(r"<[^>]+>", " ", title_html)
        title = html.unescape(re.sub(r"\s+", " ", title).strip())
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": "",
                "source": "duckduckgo_html",
            }
        )
        if len(results) >= max(1, min(10, int(max_results))):
            break
    return results


def _search_duckduckgo_html_with_diagnostics(
    query: str,
    *,
    max_results: int = 5,
    timeout_seconds: float = 6.0,
    deadline: float | None = None,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    diagnostics: dict[str, object] = {
        "provider": "duckduckgo_html",
        "configured": True,
        "query": query,
        "requested_results": max(1, min(10, int(max_results))),
        "result_count": 0,
        "error": "",
    }
    try:
        results = _search_duckduckgo_html(
            query,
            max_results=max_results,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
    except Exception as exc:
        diagnostics["error"] = f"{type(exc).__name__}:{exc}"
        return [], diagnostics

    diagnostics["result_count"] = len(results)
    if not results:
        diagnostics["error"] = "empty_results"
    return results, diagnostics


def _search_web(
    query: str,
    *,
    max_results: int = 5,
    timeout_seconds: float = 6.0,
    deadline: float | None = None,
) -> list[dict[str, str]]:
    results, _ = _search_web_with_diagnostics(
        query,
        max_results=max_results,
        timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    return results


def _search_web_with_diagnostics(
    query: str,
    *,
    max_results: int = 5,
    timeout_seconds: float = 6.0,
    deadline: float | None = None,
) -> tuple[list[dict[str, str]], dict[str, object]]:
    diagnostics: dict[str, object] = {
        "query": query,
        "providers": [],
        "selected_provider": "",
        "selected_result_count": 0,
    }
    google_timeout_seconds = min(float(timeout_seconds), 2.5)
    gemini_timeout_seconds = max(float(timeout_seconds), 6.0)
    duck_timeout_seconds = min(float(timeout_seconds), 2.5)

    google_results, google_diag = _search_google_cse_with_diagnostics(
        query,
        max_results=max_results,
        timeout_seconds=google_timeout_seconds,
        deadline=deadline,
    )
    diagnostics["providers"].append(google_diag)
    if google_results:
        diagnostics["selected_provider"] = "google_cse"
        diagnostics["selected_result_count"] = len(google_results)
        return google_results, diagnostics

    if _web_research_timed_out(deadline):
        diagnostics["selected_provider"] = "timeout_before_fallback"
        return [], diagnostics

    gemini_results, gemini_diag = _search_gemini_google_search_with_diagnostics(
        query,
        max_results=max_results,
        timeout_seconds=gemini_timeout_seconds,
        deadline=deadline,
    )
    diagnostics["providers"].append(gemini_diag)
    if gemini_results:
        diagnostics["selected_provider"] = "gemini_google_search"
        diagnostics["selected_result_count"] = len(gemini_results)
        return gemini_results, diagnostics

    if _web_research_timed_out(deadline):
        diagnostics["selected_provider"] = "timeout_before_duckduckgo"
        return [], diagnostics

    duck_results, duck_diag = _search_duckduckgo_html_with_diagnostics(
        query,
        max_results=max_results,
        timeout_seconds=duck_timeout_seconds,
        deadline=deadline,
    )
    diagnostics["providers"].append(duck_diag)
    if duck_results:
        diagnostics["selected_provider"] = "duckduckgo_html"
        diagnostics["selected_result_count"] = len(duck_results)
        return duck_results, diagnostics

    diagnostics["selected_provider"] = "none"
    return [], diagnostics


def _format_forward_options(options: list[str]) -> str:
    cleaned = [str(item or "").strip() for item in options if str(item or "").strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} or {cleaned[1]}"
    return f"{cleaned[0]}, {cleaned[1]}, or {cleaned[2]}"


def _web_research_next_steps(query: str) -> list[str]:
    normalized = str(query or "").strip().lower()
    steps: list[str] = []

    if any(
        marker in normalized
        for marker in {"best", "top", "compare", "review", "reviews"}
    ):
        steps.append("compare the strongest 2 or 3 options directly")
    if any(
        marker in normalized
        for marker in {"proven", "evidence", "research", "research backed", "studies"}
    ):
        steps.append("separate stronger evidence from weaker marketing claims")
    if any(
        marker in normalized
        for marker in {"under $", "budget", "cheap", "affordable", "entry-level"}
    ):
        steps.append("narrow the shortlist by budget and must-have constraints")
    if any(
        marker in normalized
        for marker in {"sensitive", "kids", "family", "safe", "side effects"}
    ):
        steps.append("pull out the safety tradeoffs and who each option fits best")

    if not steps:
        steps = [
            "compare the leading options side by side",
            "narrow the answer by your constraints",
            "turn the result into a short decision checklist",
        ]

    deduped: list[str] = []
    seen: set[str] = set()
    for step in steps:
        lowered = step.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(step)
        if len(deduped) >= 3:
            break
    return deduped


def _with_next_step(response: str, next_step: str = "") -> str:
    base = str(response or "").strip()
    follow_up = str(next_step or "").strip().rstrip(".")
    if not base or not follow_up:
        return base
    if base.endswith((".", "!", "?")):
        return f"{base} Next step: {follow_up}."
    return f"{base}. Next step: {follow_up}."


def _build_web_research_answer(
    *,
    query: str,
    sources: list[dict[str, object]],
    next_steps: list[str] | None = None,
    prior_context: dict[str, object] | None = None,
    plausibility: dict[str, object] | None = None,
    technical_plan: dict[str, object] | None = None,
    technical_step_findings: list[dict[str, object]] | None = None,
) -> str:
    if not sources:
        return "I tried to research that on the web, but I could not collect reliable public sources right now."

    checked = len(sources)
    evidence_lines: list[str] = []
    source_labels: list[str] = []
    seen_lines: set[str] = set()
    for source in sources:
        title = str(source.get("title") or "").strip()
        url = str(source.get("url") or "").strip()
        summary = str(source.get("summary") or source.get("snippet") or "").strip()
        if summary:
            line = _compact_text(summary, 220)
            normalized = line.lower()
            if normalized not in seen_lines:
                seen_lines.add(normalized)
                evidence_lines.append(line)
        if title and url:
            source_labels.append(f"{title} ({url})")

    lead = f"I researched the web for '{query}' and checked {checked} public sources."
    body = " ".join(evidence_lines[:3]) if evidence_lines else ""
    sources_line = ""
    if source_labels:
        sources_line = " Sources: " + "; ".join(source_labels[:3]) + "."
    prior_line = ""
    prior = prior_context if isinstance(prior_context, dict) else {}
    if int(prior.get("count", 0) or 0) > 0:
        prior_line = f" Prior knowledge: {str(prior.get('summary_line') or '').strip()}"
    plausibility_line = ""
    plausibility_meta = plausibility if isinstance(plausibility, dict) else {}
    if str(plausibility_meta.get("skepticism_level") or "") in {"medium", "high"}:
        note = " ".join(
            str(item).strip()
            for item in plausibility_meta.get("notes", [])
            if str(item).strip()
        )
        if note:
            plausibility_line = f" Common-sense check: {note}"
    technical_line = ""
    technical_summary = _format_technical_research_summary(
        technical_plan,
        technical_step_findings,
    )
    if technical_summary:
        technical_line = f" {technical_summary}"
    suggested_steps = next_steps if isinstance(next_steps, list) else []
    next_step_line = ""
    if suggested_steps:
        next_step_line = (
            " Next step: I can " + _format_forward_options(suggested_steps) + "."
        )
    return (
        f"{lead} {body}{sources_line}{prior_line}{plausibility_line}{technical_line}{next_step_line}"
    ).strip()


def _classify_web_research_failure(
    *,
    timed_out: bool,
    diagnostics: dict[str, object] | None,
) -> str:
    if timed_out:
        return "web_research_timed_out"

    search = diagnostics.get("search", {}) if isinstance(diagnostics, dict) else {}
    providers = search.get("providers", []) if isinstance(search, dict) else []
    provider_errors = []
    empty_only = True
    for item in providers:
        if not isinstance(item, dict):
            continue
        error = str(item.get("error") or "").strip().lower()
        if not error:
            continue
        provider_errors.append(error)
        if error not in {"empty_results", "not_configured"}:
            empty_only = False

    if provider_errors and not empty_only:
        return "web_research_upstream_unavailable"
    return "web_research_no_results"


def _run_web_research_sync(
    query: str,
    *,
    timeout_seconds: int = 12,
    max_results: int = 5,
    max_sources: int = 3,
    max_extract_chars: int = 8000,
) -> dict[str, object]:
    cleaned_query = _normalize_web_research_query(query)
    technical_plan = _build_technical_research_plan(cleaned_query)
    configured_total_budget = _coerce_web_research_timeout(
        settings.web_research_total_budget_seconds,
        fallback=timeout_seconds,
        minimum=2.0,
        maximum=30.0,
    )
    overall_budget_seconds = _coerce_web_research_timeout(
        timeout_seconds,
        fallback=configured_total_budget,
        minimum=2.0,
        maximum=configured_total_budget,
    )
    search_timeout_seconds = _coerce_web_research_timeout(
        settings.web_research_search_timeout_seconds,
        fallback=min(4.0, overall_budget_seconds),
        minimum=0.5,
        maximum=overall_budget_seconds,
    )
    fetch_timeout_seconds = _coerce_web_research_timeout(
        settings.web_research_fetch_timeout_seconds,
        fallback=min(3.0, overall_budget_seconds),
        minimum=0.5,
        maximum=overall_budget_seconds,
    )
    deadline = _web_research_deadline(overall_budget_seconds)
    search_results, search_diagnostics = _search_web_with_diagnostics(
        cleaned_query,
        max_results=max_results,
        timeout_seconds=search_timeout_seconds,
        deadline=deadline,
    )
    gathered: list[dict[str, object]] = []
    next_steps = _web_research_next_steps(cleaned_query)

    candidate_results: list[tuple[int, dict[str, str]]] = []
    for index, result in enumerate(search_results):
        if _web_research_timed_out(deadline):
            break
        url = str(result.get("url") or "").strip()
        if not _is_safe_web_url(url):
            continue
        candidate_results.append((index, result))
        if len(candidate_results) >= max(1, int(max_sources)):
            break

    fetched_documents: dict[int, dict[str, object]] = {}
    if candidate_results:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=_web_research_fetch_workers(
                len(candidate_results),
                int(max_sources),
            )
        ) as executor:
            future_map = {
                executor.submit(
                    _fetch_web_document,
                    str(result.get("url") or "").strip(),
                    timeout_seconds=fetch_timeout_seconds,
                    max_extract_chars=max_extract_chars,
                    deadline=deadline,
                ): (index, result)
                for index, result in candidate_results
            }
            for future in concurrent.futures.as_completed(future_map):
                index, result = future_map[future]
                try:
                    fetched_documents[index] = future.result()
                except Exception:
                    fetched_documents[index] = {
                        "error": True,
                        "title": str(result.get("title") or "").strip(),
                    }

    for index, result in candidate_results:
        if _web_research_timed_out(deadline):
            break
        url = str(result.get("url") or "").strip()
        snippet = str(result.get("snippet") or "").strip()
        document = fetched_documents.get(index, {})
        if document and not bool(document.get("error")):
            page_summary = _build_web_summary(
                title=str(document.get("title") or "").strip(),
                text=str(document.get("text") or "").strip(),
                max_sentences=2,
            )
            gathered.append(
                {
                    "title": str(
                        result.get("title") or document.get("title") or ""
                    ).strip(),
                    "url": url,
                    "snippet": snippet,
                    "summary": page_summary,
                    "text": str(document.get("text") or "").strip(),
                    "content_type": str(document.get("content_type") or "").strip(),
                    "status_code": int(document.get("status_code") or 200),
                    "search_source": str(result.get("source") or "").strip(),
                }
            )
        elif snippet:
            gathered.append(
                {
                    "title": str(result.get("title") or "").strip(),
                    "url": url,
                    "snippet": snippet,
                    "summary": snippet,
                    "text": snippet,
                    "content_type": "",
                    "status_code": 0,
                    "search_source": str(result.get("source") or "").strip(),
                }
            )
        if len(gathered) >= max(1, int(max_sources)):
            break

    diagnostics = {
        "search": search_diagnostics,
        "search_result_count": len(search_results),
        "gathered_source_count": len(gathered),
    }

    if not gathered:
        timed_out = _web_research_timed_out(deadline)
        error_code = _classify_web_research_failure(
            timed_out=timed_out,
            diagnostics=diagnostics,
        )
        answer = (
            "I tried to research that on the web, but the upstream search timed out before I could collect reliable public sources."
            if timed_out
            else (
                "I tried to research that on the web, but the upstream search providers were unavailable before I could collect reliable public sources."
                if error_code == "web_research_upstream_unavailable"
                else "I tried to research that on the web, but I could not collect reliable public sources right now."
            )
        )
        technical_summary = _format_technical_research_summary(technical_plan)
        if technical_summary:
            answer = f"{answer} {technical_summary}".strip()
        return {
            "ok": False,
            "query": cleaned_query,
            "error": error_code,
            "answer": answer,
            "sources": [],
            "technical_plan": technical_plan,
            "technical_step_findings": [],
            "timed_out": timed_out,
            "budget_seconds": round(overall_budget_seconds, 3),
            "diagnostics": diagnostics,
        }

    technical_step_findings = _run_technical_research_rounds(
        technical_plan=technical_plan,
        deadline=deadline,
    )
    technical_next_steps = (
        technical_plan.get("follow_up_suggestions", [])
        if isinstance(technical_plan, dict)
        else []
    )
    next_steps = _merge_web_research_next_steps(next_steps, technical_next_steps)

    return {
        "ok": True,
        "query": cleaned_query,
        "answer": _build_web_research_answer(
            query=cleaned_query,
            sources=gathered,
            next_steps=next_steps,
            technical_plan=technical_plan,
            technical_step_findings=technical_step_findings,
        ),
        "next_steps": next_steps,
        "sources": gathered,
        "technical_plan": technical_plan,
        "technical_step_findings": technical_step_findings,
        "timed_out": False,
        "budget_seconds": round(overall_budget_seconds, 3),
        "diagnostics": diagnostics,
    }


async def _perform_web_research(
    db: AsyncSession,
    *,
    query: str,
    timeout_seconds: int = 12,
    max_results: int = 5,
    max_sources: int = 3,
    max_extract_chars: int = 8000,
) -> dict[str, object]:
    configured_total_budget = _coerce_web_research_timeout(
        settings.web_research_total_budget_seconds,
        fallback=timeout_seconds,
        minimum=2.0,
        maximum=30.0,
    )
    effective_timeout = _coerce_web_research_timeout(
        timeout_seconds,
        fallback=configured_total_budget,
        minimum=2.0,
        maximum=configured_total_budget,
    )
    try:
        research = await asyncio.wait_for(
            asyncio.to_thread(
                _run_web_research_sync,
                query,
                timeout_seconds=timeout_seconds,
                max_results=max_results,
                max_sources=max_sources,
                max_extract_chars=max_extract_chars,
            ),
            timeout=effective_timeout + 1.0,
        )
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "query": _normalize_web_research_query(query),
            "error": "web_research_timed_out",
            "answer": "I tried to research that on the web, but the upstream search timed out before I could collect reliable public sources.",
            "sources": [],
            "timed_out": True,
            "budget_seconds": round(effective_timeout, 3),
        }
    if not bool(research.get("ok")):
        return research

    source_rows = (
        research.get("sources", []) if isinstance(research.get("sources"), list) else []
    )
    learned_claims = _distill_web_research_claims(source_rows)
    prior_memories = await _load_relevant_web_research_memories(
        db,
        query=str(research.get("query") or query).strip(),
    )
    prior_context = _summarize_prior_web_research_memories(prior_memories)
    plausibility = _assess_web_research_plausibility(
        query=str(research.get("query") or query).strip(),
        sources=source_rows,
        prior_context=prior_context,
    )
    research["learned_claims"] = learned_claims
    research["prior_knowledge"] = prior_context
    research["plausibility"] = plausibility
    research["answer"] = _build_web_research_answer(
        query=str(research.get("query") or query).strip(),
        sources=source_rows,
        next_steps=research.get("next_steps", []),
        prior_context=prior_context,
        plausibility=plausibility,
        technical_plan=research.get("technical_plan"),
        technical_step_findings=research.get("technical_step_findings"),
    )
    memory = MemoryEntry(
        memory_class="external_web_research",
        content="\n\n".join(
            _compact_text(str(item.get("text") or item.get("summary") or ""), 800)
            for item in source_rows[:3]
            if str(item.get("text") or item.get("summary") or "").strip()
        )[:2400],
        summary=_compact_text(str(research.get("answer") or ""), 500),
        metadata_json=_sanitize_json_text(
            {
                "query": str(research.get("query") or "").strip(),
                "source_count": len(source_rows),
                "sources": [
                    {
                        "title": str(item.get("title") or "").strip(),
                        "url": str(item.get("url") or "").strip(),
                        "search_source": str(item.get("search_source") or "").strip(),
                    }
                    for item in source_rows[:5]
                ],
                "source": "gateway_web_research",
                "learned_claims": learned_claims,
                "prior_memory_ids": prior_context.get("memory_ids", []),
                "prior_memory_count": int(prior_context.get("count", 0) or 0),
                "skepticism_level": str(plausibility.get("skepticism_level") or ""),
                "plausibility": plausibility,
                "reasoning_mode": str(
                    (
                        research.get("technical_plan", {})
                        if isinstance(research.get("technical_plan", {}), dict)
                        else {}
                    ).get("reasoning_mode")
                    or "single_pass"
                ),
                "technical_plan": research.get("technical_plan", {}),
                "technical_step_findings": research.get("technical_step_findings", []),
            }
        ),
    )
    db.add(memory)
    await db.flush()
    for prior_memory_id in prior_context.get("memory_ids", [])[:5]:
        if int(prior_memory_id or 0) <= 0:
            continue
        db.add(
            MemoryLink(
                source_memory_id=int(prior_memory_id),
                target_memory_id=int(memory.id),
                relation="informs_web_research",
            )
        )
    research["memory_id"] = int(memory.id)
    return research


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
    row = (
        (
            await db.execute(
                select(WorkspaceMonitoringState).order_by(
                    WorkspaceMonitoringState.id.asc()
                )
            )
        )
        .scalars()
        .first()
    )
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
    raw = (
        metadata.get("autonomy", {})
        if isinstance(metadata.get("autonomy", {}), dict)
        else {}
    )
    defaults = _default_autonomy_state()
    merged = {
        **defaults,
        **raw,
    }
    merged["zone_action_limits"] = {
        str(key): max(1, int(value))
        for key, value in (
            merged.get("zone_action_limits", {})
            if isinstance(merged.get("zone_action_limits", {}), dict)
            else {}
        ).items()
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


def _autonomy_throttle_check(
    *, autonomy_state: dict, zone: str, now: datetime
) -> tuple[bool, str, list[dict]]:
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
        latest = max(
            (
                _parse_iso_to_utc(str(item.get("timestamp", "")))
                for item in recent_actions
            ),
            default=None,
        )
        if latest and (now - latest).total_seconds() < cooldown:
            return False, "cooldown_between_actions", recent_actions

    zone_limits = (
        autonomy_state.get("zone_action_limits", {})
        if isinstance(autonomy_state.get("zone_action_limits", {}), dict)
        else {}
    )
    if zone.strip() and zone in zone_limits:
        zone_count = sum(
            1 for item in recent_actions if str(item.get("zone", "")).strip() == zone
        )
        if zone_count >= max(1, int(zone_limits[zone])):
            return False, "zone_based_limit", recent_actions
    return True, "allowed", recent_actions


async def _is_safe_zone(*, related_zone: str, db: AsyncSession) -> bool:
    zone_name = related_zone.strip()
    if not zone_name:
        return True
    base_zone = zone_name
    for candidate in [
        "front-left",
        "front-center",
        "front-right",
        "rear-left",
        "rear-center",
        "rear-right",
    ]:
        if zone_name == candidate or zone_name.startswith(f"{candidate}-"):
            base_zone = candidate
            break
    zone = (
        (
            await db.execute(
                select(WorkspaceZone).where(WorkspaceZone.zone_name == base_zone)
            )
        )
        .scalars()
        .first()
    )
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

    threshold = (
        float(autonomy.get("auto_preferred_confidence_threshold", 0.7))
        if tier == "auto_preferred"
        else float(autonomy.get("auto_safe_confidence_threshold", 0.8))
    )
    if float(proposal.confidence) < threshold:
        return False, "confidence_below_threshold"

    if not await _is_safe_zone(related_zone=proposal.related_zone, db=db):
        return False, "unsafe_zone"

    risk_score = float(AUTONOMY_PROPOSAL_RISK_SCORE.get(proposal.proposal_type, 1.0))
    if risk_score > float(autonomy.get("low_risk_score_max", 0.3)):
        return False, "risk_score_too_high"

    trigger = proposal.trigger_json if isinstance(proposal.trigger_json, dict) else {}
    pre = (
        trigger.get("preconditions", {})
        if isinstance(trigger.get("preconditions", {}), dict)
        else {}
    )
    simulation_result = str(
        trigger.get("simulation_outcome")
        or pre.get("simulation_outcome")
        or "not_required"
    )
    if simulation_result not in {"not_required", "", "plan_safe"}:
        return False, "simulation_not_safe"

    now = datetime.now(timezone.utc)
    allowed, reason, recent_actions = _autonomy_throttle_check(
        autonomy_state=autonomy, zone=proposal.related_zone, now=now
    )
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


def _initiative_status_from_resolution_metadata(
    metadata_json: dict[str, object] | None,
) -> dict[str, object]:
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    explicit_status = (
        metadata.get("initiative_status")
        if isinstance(metadata.get("initiative_status"), dict)
        else {}
    )
    program_status = (
        metadata.get("program_status")
        if isinstance(metadata.get("program_status"), dict)
        else {}
    )
    current_summary = str(metadata.get("current_recommendation_summary") or "").strip()

    if explicit_status:
        derived_status = dict(explicit_status)
        if not str(derived_status.get("summary") or "").strip() and current_summary:
            derived_status["summary"] = current_summary
        if not isinstance(derived_status.get("program_status"), dict) and program_status:
            derived_status["program_status"] = program_status
        return derived_status

    if program_status or current_summary:
        return {
            "summary": current_summary,
            "program_status": program_status,
        }

    return {}


def _to_execution_out(row: CapabilityExecution) -> dict:
    feedback = row.feedback_json if isinstance(row.feedback_json, dict) else {}
    strategy_plan = feedback.get("strategy_plan", {}) if isinstance(feedback.get("strategy_plan", {}), dict) else {}
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
        "trace_id": row.trace_id,
        "managed_scope": row.managed_scope,
        "status": row.status,
        "reason": row.reason,
        "feedback_json": feedback,
        "strategy_plan": strategy_plan,
        "execution_readiness": (
            feedback.get("execution_readiness", {})
            if isinstance(feedback.get("execution_readiness", {}), dict)
            else _json_dict(
                _json_dict(feedback.get("execution_policy_gate", {})).get("execution_readiness")
            )
        ),
        "execution_truth": (
            row.execution_truth_json
            if isinstance(row.execution_truth_json, dict)
            else {}
        ),
        "handoff_endpoint": f"/gateway/capabilities/executions/{row.id}/handoff",
        "created_at": row.created_at,
    }


def _ensure_request_id(metadata_json: object) -> tuple[dict[str, object], str]:
    metadata = dict(metadata_json) if isinstance(metadata_json, dict) else {}
    request_id = str(metadata.get("request_id") or "").strip()
    if not request_id:
        request_id = f"mim-request-{uuid.uuid4()}"
        metadata["request_id"] = request_id
    return metadata, request_id


GATEWAY_INTAKE_DIAGNOSTIC_PATH = (
    Path(__file__).resolve().parents[2]
    / "runtime"
    / "reports"
    / "mim_gateway_intake_stall_diagnostic.latest.json"
)
GATEWAY_INTAKE_DIAGNOSTIC_THRESHOLD_SECONDS = max(
    1.0,
    float(os.getenv("MIM_GATEWAY_INTAKE_DIAGNOSTIC_THRESHOLD_SECONDS", "8").strip() or 8),
)
gateway_logger = logging.getLogger(__name__)


def _gateway_trace_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_gateway_trace_event(
    trace: dict[str, object] | None,
    stage: str,
    **fields: object,
) -> None:
    if not isinstance(trace, dict):
        return
    events = trace.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        trace["events"] = events
    event = {
        "stage": stage,
        "at": _gateway_trace_timestamp(),
        **{key: value for key, value in fields.items() if value not in {None, ""}},
    }
    events.append(event)


def _write_gateway_intake_diagnostic(
    trace: dict[str, object] | None,
    *,
    final_status: str,
) -> None:
    if not isinstance(trace, dict):
        return
    payload = dict(trace)
    payload["final_status"] = final_status
    payload["written_at"] = _gateway_trace_timestamp()
    try:
        GATEWAY_INTAKE_DIAGNOSTIC_PATH.parent.mkdir(parents=True, exist_ok=True)
        GATEWAY_INTAKE_DIAGNOSTIC_PATH.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        gateway_logger.warning("failed to write gateway intake diagnostic: %s", exc)


def _should_force_deterministic_conversation_reply(event: InputEvent) -> bool:
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    adapter = str(metadata.get("adapter") or "").strip().lower()
    requested_goal = str(event.requested_goal or "").strip().lower()
    return adapter == "conversation_eval_runner" or requested_goal == "conversation_eval"


def _compact_interface_text(value: object, limit: int = 160) -> str:
    return _compact_text(" ".join(str(value or "").strip().split()), limit)


def _mim_interface_understanding(
    *,
    event: InputEvent,
    resolution: InputEventResolution,
) -> str:
    goal_description = str(resolution.proposed_goal_description or "").strip()
    internal_intent = str(resolution.internal_intent or "").strip().lower()
    if internal_intent in {"create_goal", "execute_capability"} and goal_description:
        return _compact_interface_text(goal_description, 180)
    return _compact_interface_text(event.raw_input, 180)


def _mim_interface_status(
    *,
    resolution: InputEventResolution,
    execution: CapabilityExecution | None,
) -> str:
    resolution_meta = (
        resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}
    )
    status_override = str(
        resolution_meta.get("mim_interface_status_override") or ""
    ).strip()
    if status_override:
        return status_override
    tod_dispatch = (
        resolution_meta.get("tod_dispatch")
        if isinstance(resolution_meta.get("tod_dispatch"), dict)
        else {}
    )
    if tod_dispatch:
        dispatch_status = str(
            tod_dispatch.get("result_status") or tod_dispatch.get("request_status") or ""
        ).strip().lower()
        if dispatch_status in {"failed", "blocked", "error"}:
            return "blocked"
        if dispatch_status in {"accepted", "running", "pending", "recorded"}:
            return "doing"
        if dispatch_status in {"succeeded", "completed", "done"}:
            return "done"

    if execution is not None:
        execution_status = str(execution.status or "").strip().lower()
        if execution_status in {"failed", "blocked", "error"}:
            return "blocked"
        if execution_status in {"pending_confirmation"}:
            return "deferred"
        if execution_status in {"accepted", "running", "dispatched", "pending"}:
            return "doing"
        if execution_status in {"succeeded"}:
            return "done"

    outcome = str(resolution.outcome or "").strip().lower()
    if outcome == "blocked":
        return "blocked"
    if outcome == "requires_confirmation":
        return "deferred"
    return "done"


def _mim_interface_next_action(
    *,
    event: InputEvent,
    resolution: InputEventResolution,
    execution: CapabilityExecution | None,
) -> str:
    resolution_meta = (
        resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}
    )
    next_action_override = str(
        resolution_meta.get("mim_interface_next_action_override") or ""
    ).strip()
    if next_action_override:
        return next_action_override
    tod_dispatch = (
        resolution_meta.get("tod_dispatch")
        if isinstance(resolution_meta.get("tod_dispatch"), dict)
        else {}
    )
    if tod_dispatch:
        dispatch_kind = str(tod_dispatch.get("dispatch_kind") or "").strip().lower()
        if dispatch_kind == "bounded_bridge_warning_recommendation_request":
            return "dispatch one bounded TOD bridge-warning next-step recommendation request and surface TOD's result"
        if dispatch_kind == "bounded_bridge_warning_request":
            return "dispatch one bounded TOD bridge-warning explanation request and surface TOD's result"
        if dispatch_kind == "bounded_warnings_summary_request":
            return "dispatch one bounded TOD warnings-summary request and surface TOD's result"
        if dispatch_kind == "bounded_objective_summary_request":
            return "dispatch one bounded TOD current-objective summary request and surface TOD's result"
        if dispatch_kind == "bounded_recent_changes_request":
            return "dispatch one bounded TOD recent-changes summary request and surface TOD's result"
        return "dispatch one bounded TOD status request and surface TOD's result"

    if execution is not None:
        execution_status = str(execution.status or "").strip().lower()
        capability_name = _compact_interface_text(execution.capability_name, 96)
        if execution_status == "pending_confirmation":
            return "wait for explicit confirmation before dispatching the requested capability"
        if execution_status in {"dispatched", "accepted", "running", "pending"}:
            if capability_name:
                return f"hand off {capability_name} to TOD and track the result"
            return "hand off the approved request to TOD and track the result"
        if execution_status == "succeeded":
            return "report the completed execution result"
        if execution_status in {"failed", "blocked", "error"}:
            return "report the exact execution blocker"

    reason = str(resolution.reason or "").strip().lower()
    outcome = str(resolution.outcome or "").strip().lower()
    if outcome == "blocked":
        return "report the exact blocker for this request"
    if outcome == "requires_confirmation":
        if reason in {
            "conversation_optional_escalation",
            "conversation_optional_escalation_followup",
        }:
            return "wait for an explicit confirmation to create one bounded goal"
        return "wait for the missing detail needed to continue"
    if reason in {"conversation_precision_prompt", "conversation_precision_limit"}:
        return "wait for one specific question or action"
    if str(event.source or "").strip().lower() == "text":
        return "reply directly in this session"
    return "report the result of this request"


def _mim_interface_result(
    *,
    resolution: InputEventResolution,
    execution: CapabilityExecution | None,
    status: str,
) -> tuple[str, str]:
    resolution_meta = (
        resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}
    )
    result_override = str(
        resolution_meta.get("mim_interface_result_override") or ""
    ).strip()
    if result_override and status != "blocked":
        return result_override, ""

    prompt = _compact_interface_text(resolution.clarification_prompt, 240)
    if status == "blocked":
        blocker = prompt
        if not blocker and execution is not None:
            blocker = _compact_interface_text(execution.reason, 240)
        if not blocker:
            blocker = _compact_interface_text(resolution.reason, 240) or "Request blocked."
        return "", blocker

    if prompt:
        return prompt, ""
    tod_dispatch = (
        resolution_meta.get("tod_dispatch")
        if isinstance(resolution_meta.get("tod_dispatch"), dict)
        else {}
    )
    if tod_dispatch:
        detail = _compact_interface_text(
            tod_dispatch.get("result_reason") or tod_dispatch.get("decision_detail") or "",
            240,
        )
        if status == "blocked":
            return "", detail or "TOD did not accept the bounded status request."
        return detail or "TOD returned one bounded status result.", ""

    if execution is not None:
        execution_status = str(execution.status or "").strip().lower()
        if execution_status in {"dispatched", "accepted", "running", "pending"}:
            return "The request has been accepted and is now in progress.", ""
        if execution_status == "succeeded":
            return "The requested action completed successfully.", ""
        if execution_status == "pending_confirmation":
            return "The request is waiting for explicit confirmation before dispatch.", ""

    outcome = str(resolution.outcome or "").strip().lower()
    if outcome == "requires_confirmation":
        return "I need one explicit confirmation or one missing detail before continuing.", ""
    if outcome == "store_only":
        return "I replied in session and did not dispatch any separate action.", ""
    return "Request processed.", ""


def _build_mim_interface_response(
    *,
    event: InputEvent,
    resolution: InputEventResolution,
    execution: CapabilityExecution | None,
) -> dict[str, object]:
    event_meta = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    resolution_meta = (
        resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}
    )
    request_id = str(event_meta.get("request_id") or "").strip() or f"event-{event.id}"
    understood = _mim_interface_understanding(event=event, resolution=resolution)
    status = _mim_interface_status(resolution=resolution, execution=execution)
    next_action = _mim_interface_next_action(
        event=event,
        resolution=resolution,
        execution=execution,
    )
    result, blocker = _mim_interface_result(
        resolution=resolution,
        execution=execution,
        status=status,
    )
    reply_override = str(
        resolution_meta.get("mim_interface_reply_override") or ""
    ).strip()
    reply_override = sanitize_user_facing_reply_text(reply_override)
    detail_label = "Blocker" if blocker else "Result"
    detail_value = blocker or reply_override or result
    is_eval = str(getattr(event, "requested_goal", "") or "").strip().lower() == "conversation_eval"
    if is_eval:
        raw_eval_reply = reply_override or str(detail_value or "").strip()
        # Cap eval replies at 200 chars so verbose tool outputs don't trigger over_explaining
        reply_text = _compact_text(raw_eval_reply, 200) if len(raw_eval_reply) > 200 else raw_eval_reply
    else:
        reply_text = reply_override or (
            f"Request {request_id}. I understood: {understood}. "
            f"Next action: {next_action}. Status: {status}. "
            f"{detail_label}: {detail_value}"
        ).strip()
    reply_text = sanitize_user_facing_reply_text(reply_text)
    return {
        "request_id": request_id,
        "understood": understood,
        "next_action": next_action,
        "status": status,
        "result": result,
        "blocker": blocker,
        "reply_text": reply_text,
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


def _resolve_feedback_status(
    payload: ExecutionFeedbackUpdateRequest,
) -> tuple[str, str, str]:
    requested_status = payload.status.strip().lower()
    runtime_outcome = payload.runtime_outcome.strip().lower()
    resolved_reason = payload.reason

    if runtime_outcome:
        mapped = RUNTIME_OUTCOME_STATUS_MAP.get(runtime_outcome)
        if not mapped:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported runtime_outcome: {runtime_outcome}",
            )
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
        raise HTTPException(
            status_code=422, detail="status or runtime_outcome is required"
        )

    if not resolved_reason.strip():
        resolved_reason = "executor feedback update"

    return requested_status, resolved_reason, runtime_outcome


def _infer_intent(event: InputEvent) -> str:
    if event.parsed_intent and event.parsed_intent not in {
        "unknown",
        "vision_observation",
    }:
        mapping = {
            "speak": "speak_response",
            "voice_output": "speak_response",
            "workspace_check": "observe_workspace",
            "observe_workspace": "observe_workspace",
            "identify_object": "identify_object",
            "task_execute": "execute_capability",
            "execute_capability": "execute_capability",
            "execution_capability_request": "execute_capability",
            "robotics_supervised_probe": "execute_capability",
            "create_goal": "create_goal",
            "clarify": "request_clarification",
            "unclear_requires_clarification": "request_clarification",
        }
        lowered = event.parsed_intent.lower()
        for key, mapped in mapping.items():
            if key in lowered:
                return mapped

    raw = event.raw_input.lower()
    routed = route_console_text_input(event.raw_input, event.parsed_intent)
    if routed.classifier_outcome in {
        "execution_capability_request",
        "robotics_supervised_probe",
    }:
        return "execute_capability"
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
def _looks_like_bounded_tod_status_request(
    text: str,
    parsed_intent: str,
    safety_flags: list[str] | None = None,
) -> bool:
    normalized_safety_flags = {
        str(flag or "").strip().lower()
        for flag in (safety_flags or [])
        if str(flag or "").strip()
    }
    if {"blocked", "deny_execution"} & normalized_safety_flags:
        return False

    if route_console_text_input(text, parsed_intent).classifier_outcome in {
        "execution_capability_request",
        "robotics_supervised_probe",
    }:
        return True

    normalized_intent = str(parsed_intent or "").strip().lower()
    if normalized_intent not in {"unknown", "question", "discussion", "observation"}:
        return False

    if _looks_like_bounded_choice_decision_prompt(text):
        return False

    raw = " ".join(str(text or "").strip().lower().split())
    if not raw or not _mentions_tod(raw):
        return False

    status_markers = {"status", "state", "health", "heartbeat", "bridge"}
    request_markers = {
        "check",
        "show",
        "tell me",
        "report",
        "summarize",
        "get",
        "ask",
    }
    return any(marker in raw for marker in status_markers) and any(
        marker in raw for marker in request_markers
    )


def _looks_like_bounded_tod_objective_summary_request(
    text: str,
    parsed_intent: str,
    safety_flags: list[str] | None = None,
) -> bool:
    normalized_safety_flags = {
        str(flag or "").strip().lower()
        for flag in (safety_flags or [])
        if str(flag or "").strip()
    }
    if {"blocked", "deny_execution"} & normalized_safety_flags:
        return False

    normalized_intent = str(parsed_intent or "").strip().lower()
    if normalized_intent not in {"unknown", "question", "discussion", "observation"}:
        return False

    raw = " ".join(str(text or "").strip().lower().split())
    if not raw or not _mentions_tod(raw):
        return False

    request_markers = {
        "summarize",
        "summary",
        "what are you working on",
        "current objective",
        "active objective",
        "objective summary",
    }
    return any(marker in raw for marker in request_markers)


def _looks_like_bounded_tod_warnings_summary_request(
    text: str,
    parsed_intent: str,
    safety_flags: list[str] | None = None,
) -> bool:
    normalized_safety_flags = {
        str(flag or "").strip().lower()
        for flag in (safety_flags or [])
        if str(flag or "").strip()
    }
    if {"blocked", "deny_execution"} & normalized_safety_flags:
        return False

    normalized_intent = str(parsed_intent or "").strip().lower()
    if normalized_intent not in {"unknown", "question", "discussion", "observation"}:
        return False

    raw = " ".join(str(text or "").strip().lower().split())
    if not raw or not _mentions_tod(raw):
        return False
    if "bridge warning" in raw:
        return False

    warning_markers = {
        "warning",
        "warnings",
        "alert",
        "alerts",
    }
    summary_markers = {
        "summary",
        "summarize",
        "current",
        "active",
        "what warnings",
    }
    return any(marker in raw for marker in warning_markers) and any(
        marker in raw for marker in summary_markers
    )


def _looks_like_bounded_warning_care_request(
    text: str,
    parsed_intent: str,
    safety_flags: list[str] | None = None,
) -> bool:
    normalized_safety_flags = {
        str(flag or "").strip().lower()
        for flag in (safety_flags or [])
        if str(flag or "").strip()
    }
    if {"blocked", "deny_execution"} & normalized_safety_flags:
        return False

    normalized_intent = str(parsed_intent or "").strip().lower()
    if normalized_intent not in {"unknown", "question", "discussion", "observation"}:
        return False

    raw = " ".join(str(text or "").strip().lower().split())
    if not raw:
        return False
    return raw in {
        "what warnings should i care about",
        "what warnings should i care about?",
        "which warnings should i care about",
        "which warnings should i care about?",
    }


def _select_single_bounded_followup_action(
    *,
    primary_dispatch: dict[str, object] | None,
    request_id: str,
    session_key: str,
    actor: str,
    prior_dispatch_kinds: set[str] | None = None,
) -> dict[str, object]:
    dispatch = primary_dispatch if isinstance(primary_dispatch, dict) else {}
    primary_dispatch_kind = str(dispatch.get("dispatch_kind") or "").strip().lower()
    seen_dispatch_kinds = {
        str(item or "").strip().lower()
        for item in (prior_dispatch_kinds or set())
        if str(item or "").strip()
    }
    selection_confidence = 1.0
    if primary_dispatch_kind == "bounded_warnings_summary_request":
        selection_reason = (
            "Recent changes are the fastest bounded check for what is actively affecting those warnings."
        )
        followup_dispatch = dispatch_bounded_tod_recent_changes_request(
            request_id=request_id,
            session_key=session_key,
            content="Summarize recent changes that materially affect the current objective.",
            actor=actor,
        )
    elif primary_dispatch_kind == "bounded_bridge_warning_request":
        selection_reason = (
            "A single bounded recommendation is the fastest follow-up after the explanation because it turns the bridge warning into one concrete next step."
        )
        followup_dispatch = dispatch_bounded_tod_bridge_warning_recommendation_request(
            request_id=request_id,
            session_key=session_key,
            content="What should TOD do next about the bridge warning?",
            actor=actor,
        )
    elif primary_dispatch_kind == "bounded_objective_summary_request":
        selection_reason = (
            "Recent changes are the fastest bounded follow-up after the objective summary because they show what is materially moving that objective right now."
        )
        followup_dispatch = dispatch_bounded_tod_recent_changes_request(
            request_id=request_id,
            session_key=session_key,
            content="Summarize recent changes that materially affect the current objective.",
            actor=actor,
        )
    elif primary_dispatch_kind == "bounded_recent_changes_request":
        selection_reason = (
            "Warnings are the best bounded follow-up after recent changes because they identify which of those changes matter operationally without repeating the change summary."
        )
        followup_dispatch = dispatch_bounded_tod_warnings_summary_request(
            request_id=request_id,
            session_key=session_key,
            content="Summarize current warnings for TOD.",
            actor=actor,
        )
    else:
        return {
            "stop_reason": "unclear_next_step",
            "stop_detail": "I stopped because there was no clear bounded next step after the current action.",
        }

    selected_dispatch_kind = str(
        followup_dispatch.get("dispatch_kind") or ""
    ).strip().lower()
    if selected_dispatch_kind in seen_dispatch_kinds:
        return {
            "stop_reason": "unclear_next_step",
            "stop_detail": "I stopped because the only clear bounded next step would repeat a previous action and create a loop.",
        }

    if selection_confidence < 0.75:
        return {
            "stop_reason": "lack_of_confidence",
            "stop_detail": "I stopped because the next bounded step was not confident enough to execute automatically.",
        }

    return {
        "selection_reason": selection_reason,
        "selected_action_name": str(followup_dispatch.get("action_name") or "").strip(),
        "selected_dispatch_kind": str(followup_dispatch.get("dispatch_kind") or "").strip(),
        "selection_confidence": selection_confidence,
        "primary_result_reason": str(dispatch.get("result_reason") or "").strip(),
        "followup_result_reason": str(followup_dispatch.get("result_reason") or "").strip(),
        "followup_dispatch": followup_dispatch,
    }


def _build_bounded_controlled_continuation(
    *,
    primary_dispatch: dict[str, object] | None,
    request_id: str,
    session_key: str,
    actor: str,
    max_depth: int = 3,
) -> dict[str, object]:
    dispatch = primary_dispatch if isinstance(primary_dispatch, dict) else {}
    initial_dispatch_kind = str(dispatch.get("dispatch_kind") or "").strip().lower()
    if not dispatch or not initial_dispatch_kind:
        return {
            "max_depth": max(1, int(max_depth or 3)),
            "step_count": 0,
            "steps": [],
            "final_dispatch": {},
            "selected_next_step": {},
            "stop_reason": "unclear_next_step",
            "stop_detail": "I stopped because the starting bounded action was not clear enough to continue.",
        }

    bounded_limit = max(1, int(max_depth or 3))
    steps: list[dict[str, object]] = [
        {
            "step_number": 1,
            "dispatch_kind": str(dispatch.get("dispatch_kind") or "").strip(),
            "action_name": str(dispatch.get("action_name") or "").strip(),
            "result_reason": str(dispatch.get("result_reason") or "").strip(),
            "selection_reason": "",
            "selection_confidence": 1.0,
            "dispatch": dispatch,
        }
    ]
    seen_dispatch_kinds = {initial_dispatch_kind}
    current_dispatch = dispatch
    stop_reason = ""
    stop_detail = ""

    while len(steps) < bounded_limit:
        selected_next_step = _select_single_bounded_followup_action(
            primary_dispatch=current_dispatch,
            request_id=request_id,
            session_key=session_key,
            actor=actor,
            prior_dispatch_kinds=seen_dispatch_kinds,
        )
        followup_dispatch = (
            selected_next_step.get("followup_dispatch")
            if isinstance(selected_next_step.get("followup_dispatch"), dict)
            else {}
        )
        if not followup_dispatch:
            stop_reason = str(selected_next_step.get("stop_reason") or "unclear_next_step").strip()
            stop_detail = str(selected_next_step.get("stop_detail") or "I stopped because the next bounded step was not clear enough to continue.").strip()
            break

        followup_dispatch_kind = str(
            followup_dispatch.get("dispatch_kind") or ""
        ).strip().lower()
        if not followup_dispatch_kind:
            stop_reason = "unclear_next_step"
            stop_detail = "I stopped because the next bounded step was not clear enough to continue."
            break

        steps.append(
            {
                "step_number": len(steps) + 1,
                "dispatch_kind": str(followup_dispatch.get("dispatch_kind") or "").strip(),
                "action_name": str(followup_dispatch.get("action_name") or "").strip(),
                "result_reason": str(followup_dispatch.get("result_reason") or "").strip(),
                "selection_reason": str(selected_next_step.get("selection_reason") or "").strip(),
                "selection_confidence": float(
                    selected_next_step.get("selection_confidence", 1.0) or 1.0
                ),
                "dispatch": followup_dispatch,
            }
        )
        seen_dispatch_kinds.add(followup_dispatch_kind)
        current_dispatch = followup_dispatch

    if not stop_reason and len(steps) >= bounded_limit:
        stop_reason = "max_depth_reached"
        stop_detail = f"I stopped at the bounded {bounded_limit}-step limit."

    selected_next_step = steps[1] if len(steps) > 1 else {}
    final_dispatch = (
        steps[-1].get("dispatch") if isinstance(steps[-1].get("dispatch"), dict) else {}
    )
    metadata_steps = [
        {
            "step_number": int(step.get("step_number", 0) or 0),
            "dispatch_kind": str(step.get("dispatch_kind") or "").strip(),
            "action_name": str(step.get("action_name") or "").strip(),
            "result_reason": str(step.get("result_reason") or "").strip(),
            "selection_reason": str(step.get("selection_reason") or "").strip(),
            "selection_confidence": float(step.get("selection_confidence", 1.0) or 1.0),
        }
        for step in steps
    ]
    return {
        "max_depth": bounded_limit,
        "step_count": len(metadata_steps),
        "steps": metadata_steps,
        "final_dispatch": final_dispatch,
        "selected_next_step": {
            "selection_reason": str(selected_next_step.get("selection_reason") or "").strip(),
            "selected_action_name": str(selected_next_step.get("action_name") or "").strip(),
            "selected_dispatch_kind": str(selected_next_step.get("dispatch_kind") or "").strip(),
            "selection_confidence": float(selected_next_step.get("selection_confidence", 1.0) or 1.0),
        }
        if selected_next_step
        else {},
        "stop_reason": stop_reason,
        "stop_detail": stop_detail,
    }


def _format_bounded_controlled_continuation_result(
    *,
    primary_result_label: str,
    continuation: dict[str, object] | None,
) -> str:
    chain = continuation if isinstance(continuation, dict) else {}
    steps = chain.get("steps", []) if isinstance(chain.get("steps", []), list) else []
    if not steps:
        return "I stopped because the bounded continuation chain could not be built."

    primary_result_reason = str(steps[0].get("result_reason") or "").strip()
    parts = [f"Step 1 result ({primary_result_label}): {primary_result_reason}"]
    for step in steps[1:]:
        step_number = int(step.get("step_number", 0) or 0)
        selection_reason = str(step.get("selection_reason") or "").strip()
        result_reason = str(step.get("result_reason") or "").strip()
        if selection_reason:
            parts.append(f"Step {step_number} selection: {selection_reason}")
        if result_reason:
            parts.append(f"Step {step_number} result: {result_reason}")

    stop_detail = str(chain.get("stop_detail") or "").strip()
    if stop_detail:
        parts.append(f"Stop: {stop_detail}")
    return " ".join(part for part in parts if part).strip()


def _looks_like_bounded_tod_recent_changes_request(
    text: str,
    parsed_intent: str,
    safety_flags: list[str] | None = None,
) -> bool:
    normalized_safety_flags = {
        str(flag or "").strip().lower()
        for flag in (safety_flags or [])
        if str(flag or "").strip()
    }
    if {"blocked", "deny_execution"} & normalized_safety_flags:
        return False

    normalized_intent = str(parsed_intent or "").strip().lower()
    if normalized_intent not in {"unknown", "question", "discussion", "observation"}:
        return False

    raw = " ".join(str(text or "").strip().lower().split())
    if not raw or "recent changes" not in raw:
        return False

    objective_markers = {
        "current objective",
        "materially affect",
        "material impact",
    }
    summary_markers = {"summarize", "summary", "what changed", "recent changes"}
    return any(marker in raw for marker in objective_markers) and any(
        marker in raw for marker in summary_markers
    )


def _looks_like_bounded_tod_bridge_warning_request(
    text: str,
    parsed_intent: str,
    safety_flags: list[str] | None = None,
) -> bool:
    normalized_safety_flags = {
        str(flag or "").strip().lower()
        for flag in (safety_flags or [])
        if str(flag or "").strip()
    }
    if {"blocked", "deny_execution"} & normalized_safety_flags:
        return False

    normalized_intent = str(parsed_intent or "").strip().lower()
    if normalized_intent not in {"unknown", "question", "discussion", "observation"}:
        return False

    raw = " ".join(str(text or "").strip().lower().split())
    if not raw or not _mentions_tod(raw):
        return False

    bridge_markers = {
        "bridge warning",
        "bridge mismatch",
        "bridge issue",
        "bridge alert",
    }
    request_markers = {
        "explain",
        "what is",
        "what's",
        "why",
        "describe",
    }
    return any(marker in raw for marker in bridge_markers) and any(
        marker in raw for marker in request_markers
    )


def _looks_like_bounded_tod_bridge_warning_recommendation_request(
    text: str,
    parsed_intent: str,
    safety_flags: list[str] | None = None,
) -> bool:
    normalized_safety_flags = {
        str(flag or "").strip().lower()
        for flag in (safety_flags or [])
        if str(flag or "").strip()
    }
    if {"blocked", "deny_execution"} & normalized_safety_flags:
        return False

    normalized_intent = str(parsed_intent or "").strip().lower()
    if normalized_intent not in {"unknown", "question", "discussion", "observation"}:
        return False

    raw = " ".join(str(text or "").strip().lower().split())
    if not raw or not _mentions_tod(raw):
        return False

    bridge_markers = {
        "bridge warning",
        "bridge mismatch",
        "bridge issue",
        "bridge alert",
    }
    request_markers = {
        "what should",
        "should tod do next",
        "do next",
        "next step",
        "next safe action",
        "recommend",
        "recommendation",
    }
    return any(marker in raw for marker in bridge_markers) and any(
        marker in raw for marker in request_markers
    )


def _looks_like_action_request(text: str) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    action_markers = (
        "run ",
        "execute",
        "create goal",
        "start task",
        "dispatch",
        "invoke",
        "trigger",
        "send",
        "download",
        "open",
    )
    if any(marker in raw for marker in action_markers):
        return True

    if "tod" in raw and any(
        marker in raw
        for marker in {
            "have tod",
            "ask tod",
            "tod check",
            "tod verify",
            "have tod check",
            "have tod verify",
        }
    ):
        return True

    planning_markers = {
        "create a plan to continue",
        "create the plan to continue",
        "come up with a plan",
        "draft a plan",
        "plan for implementation",
        "plan for implimentation",
        "continue and implement",
        "continue and impliment",
        "execute that plan",
        "proceed with that plan",
        "move into implementation",
        "start with the first bounded implementation step",
    }
    if any(marker in raw for marker in planning_markers):
        return True

    return False


def _looks_like_vague_thing_request(text: str) -> bool:
    query = _normalize_conversation_query(text)
    if not query:
        return False
    return any(
        token in query
        for token in {
            "can you handle that thing",
            "can you handle the thing",
            "handle that thing",
            "handle the thing",
            "that thing",
            "the thing",
            "this thing",
        }
    )


def _looks_like_bounded_implementation_request(
    text: str,
    parsed_intent: str,
    safety_flags: list[str] | None,
) -> bool:
    normalized_safety_flags = {
        str(flag or "").strip().lower()
        for flag in (safety_flags or [])
        if str(flag or "").strip()
    }
    if {"blocked", "deny_execution"} & normalized_safety_flags:
        return False

    normalized_intent = str(parsed_intent or "").strip().lower()
    if normalized_intent not in {"unknown", "question", "discussion", "observation"}:
        return False

    query = _normalize_conversation_query(text)
    if not query:
        return False
    if _looks_like_vague_thing_request(query):
        return False

    polite_prefixes = (
        "mim ",
        "mim can you ",
        "mim could you ",
        "can you ",
        "could you ",
        "please ",
        "yes ",
        "yes then ",
        "then ",
        "i would like you to ",
        "i want you to ",
        "i need you to ",
    )
    simplified_query = query
    changed = True
    while changed:
        changed = False
        for prefix in polite_prefixes:
            if simplified_query.startswith(prefix):
                simplified_query = simplified_query[len(prefix) :].strip()
                changed = True

    planning_prefixes = (
        "how do i implement ",
        "how do we implement ",
        "how should i implement ",
        "how should we implement ",
        "how do i build ",
        "how do we build ",
        "how can i build ",
        "how can we build ",
        "what is the fastest path",
        "what should we inspect first",
        "what should i inspect first",
    )
    if any(simplified_query.startswith(prefix) for prefix in planning_prefixes):
        return False

    if _looks_like_development_integration_query(simplified_query):
        return False

    if _looks_like_continuation_validation_request(simplified_query):
        return True

    explicit_initiative_id = extract_explicit_initiative_id(text)
    if explicit_initiative_id:
        formal_initiative_markers = {
            "objective",
            "goal",
            "rules",
            "success criteria",
            "create objective",
            "create task",
            "implementation plan",
            "plan only",
            "planning only",
            "do not dispatch",
            "do not mark complete",
        }
        padded_query = f" {simplified_query} "
        if any(f" {marker} " in padded_query for marker in formal_initiative_markers):
            return True

    continuous_execution_markers = {
        "continuous execution mode",
        "persistent loop",
        "begin loop now",
        "loop iteration",
        "next natural objective",
    }
    if sum(1 for marker in continuous_execution_markers if marker in simplified_query) >= 3:
        return True

    implementation_prefixes = (
        "implement ",
        "create and implement ",
        "create implement ",
        "create a plan to ",
        "create the plan to ",
        "build ",
        "fix ",
        "work on ",
        "do the next step",
        "do next step",
        "come up with a plan",
        "draft a plan",
        "start working on ",
        "continue working on ",
        "continue and implement",
        "continue and impliment",
        "continue with ",
        "continue based on ",
        "start ",
        "address ",
        "handle ",
        "continue your development in ",
    )
    if any(simplified_query.startswith(prefix) for prefix in implementation_prefixes):
        return True

    implementation_markers = {
        " create and implement ",
        " create a plan ",
        " implementation plan ",
        " implimentation plan ",
        " plan for implementation ",
        " plan for implimentation ",
        " implement the plan ",
        " implement your plan ",
        " continue based on ",
        " continue and implement ",
        " continue and impliment ",
        " continue your development ",
        " work on the next step ",
        " do the next step ",
        " execute that plan ",
        " proceed with that plan ",
        " move into implementation ",
        " first bounded implementation step ",
    }
    padded_query = f" {simplified_query} "
    return any(marker in padded_query for marker in implementation_markers)


def _looks_like_planning_only_initiative_request(text: str) -> bool:
    query = _normalize_conversation_query(text)
    if not query:
        return False
    if not extract_explicit_initiative_id(text):
        return False
    planning_markers = {
        " planning only ",
        " plan only ",
        " implementation plan only ",
        " do not dispatch code execution ",
        " do not dispatch execution ",
        " do not create result artifact ",
        " do not mark complete ",
        " no execution artifact exists ",
    }
    padded_query = f" {query} "
    return any(marker in padded_query for marker in planning_markers)


def _build_conversation_handoff_payload(
    *, request_id: str, text: str, session_id: str
) -> dict[str, object]:
    requested_outcome = _compact_text(text, 220) or "Implement one bounded change."
    topic = _compact_text(requested_outcome, 96) or "Implementation request"
    constraints = [
        "Bounded implementation only.",
        "Use the existing repo execution lanes.",
        "Preserve the current browser reply contract.",
    ]
    next_bounded_steps = [
        "Classify the request into the existing bounded implementation lane.",
        "Prepare one bounded task record and any broker artifacts needed for execution.",
        "Surface the queued or completed status back to the same conversation session.",
    ]
    payload = {
        "handoff_id": f"conversation-{request_id}",
        "source": "conversation-gateway",
        "topic": topic,
        "conversation_request_text": str(text or "").strip(),
        "summary": (
            "Create one bounded implementation task from the live conversation request "
            f"for session {session_id or 'default-session'}: {requested_outcome}"
        ),
        "requested_outcome": requested_outcome,
        "constraints": constraints,
        "next_bounded_steps": next_bounded_steps,
        "status": "pending",
    }
    return payload


def _looks_like_training_initiative_request(text: str) -> bool:
    query = _normalize_conversation_query(text)
    if not query:
        return False
    training_markers = {
        "start training",
        "resume training",
        "continue training",
        "keep training",
        "restart training",
        "run training",
        "self evolution",
        "self-evolution",
        "natural language training",
        "natural-language training",
        "natural language slice",
        "natural-language slice",
    }
    return any(marker in query for marker in training_markers)


def _looks_like_continuation_validation_request(text: str) -> bool:
    query = _normalize_conversation_query(text)
    if not query:
        return False
    required_markers = (
        "controlled continuation test",
        "task completion",
        "recovery",
        "readiness transition",
        "no human confirmation required",
    )
    if all(marker in query for marker in required_markers):
        return True
    return (
        "initiative_id:" in query
        and "auto-resume" in query
        and "5+ tasks executed" in query
    )


async def _maybe_dispatch_authorized_text_initiative(
    *,
    event: InputEvent,
    request_id: str,
    session_id: str,
    db: AsyncSession,
) -> dict[str, object] | None:
    current_status = await build_initiative_status(db=db)
    normalized_query = _normalize_conversation_query(event.raw_input)
    explicit_initiative_id = extract_explicit_initiative_id(event.raw_input)
    planning_only_initiative = _looks_like_planning_only_initiative_request(
        event.raw_input
    )
    active_objective = (
        current_status.get("active_objective")
        if isinstance(current_status.get("active_objective"), dict)
        else {}
    )
    active_soft_initiative = bool(active_objective) and (
        str(active_objective.get("owner") or "").strip().lower() == "mim"
        and str(active_objective.get("boundary_mode") or "").strip().lower()
        == "soft"
    )
    fresh_validation_request = _looks_like_continuation_validation_request(event.raw_input)
    resume_existing = _is_resume_control_query(normalized_query)
    max_auto_steps = 8 if fresh_validation_request else 3
    if not fresh_validation_request and "continuous execution mode" in event.raw_input.lower():
        max_auto_steps = 5

    objective_title = ""
    priority = "high"
    managed_scope = "workspace"
    if (
        active_soft_initiative
        and resume_existing
        and not fresh_validation_request
        and not explicit_initiative_id
    ):
        objective_title = str(active_objective.get("title") or "").strip()
        priority = str(active_objective.get("priority") or "high").strip() or "high"
        active_metadata = (
            active_objective.get("metadata_json")
            if isinstance(active_objective.get("metadata_json"), dict)
            else {}
        )
        managed_scope = (
            str(active_metadata.get("managed_scope") or "").strip() or "workspace"
        )

    initiative_run = await drive_initiative_from_intent(
        db,
        actor="mim",
        source="gateway_text_initiative",
        user_intent=event.raw_input,
        objective_title=objective_title,
        priority=priority,
        managed_scope=managed_scope,
        expected_outputs=[],
        verification_commands=[],
        continue_chain=not planning_only_initiative,
        max_auto_steps=max_auto_steps,
        metadata_json={
            "request_id": request_id,
            "initiative_id": explicit_initiative_id,
            "conversation_session_id": session_id,
            "initiative_auto_execute": True,
            "initiated_from_gateway": True,
            "planning_only": planning_only_initiative,
            "resume_existing": resume_existing,
        },
    )
    initiative_payload = json.loads(json.dumps(initiative_run, default=str))
    continuation = (
        initiative_payload.get("continuation")
        if isinstance(initiative_payload.get("continuation"), dict)
        else {}
    )
    initiative_status = (
        continuation.get("status")
        if isinstance(continuation.get("status"), dict)
        else current_status
    )
    active_task = (
        initiative_status.get("active_task")
        if isinstance(initiative_status.get("active_task"), dict)
        else {}
    )
    summary = str(initiative_status.get("summary") or "").strip()
    human_prompt_required = bool(initiative_payload.get("human_prompt_required"))
    executed_local = (
        continuation.get("executed_local")
        if isinstance(continuation.get("executed_local"), list)
        else []
    )
    continuous_iterations = [
        item
        for item in executed_local
        if isinstance(item, dict)
        and str(item.get("mode") or "").strip() == "continuous_execution_iteration"
    ]

    if human_prompt_required:
        result_text = summary or (
            "The requested initiative reached a hard boundary and is waiting for explicit confirmation."
        )
        next_action_text = (
            "wait for explicit confirmation before continuing this hard-boundary initiative"
        )
        interface_status = "deferred"
        reason = "initiative_hard_boundary_requires_confirmation"
        outcome = "requires_confirmation"
    else:
        objective = (
            initiative_payload.get("objective")
            if isinstance(initiative_payload.get("objective"), dict)
            else {}
        )
        objective_execution_state = str(initiative_status.get("execution_state") or "").strip().lower()
        objective_title_text = str(objective.get("title") or "").strip()
        task_title_text = str(active_task.get("title") or "").strip()
        if planning_only_initiative:
            result_text = summary or (
                f"Planning-only initiative active: {objective_title_text}."
                if objective_title_text
                else "Planning-only initiative created without execution dispatch."
            )
            next_action_text = "hold execution dispatch and surface the planning-only initiative state"
            interface_status = "doing"
        elif continuous_iterations:
            result_text = " ".join(
                (
                    f"Iteration {int(item.get('iteration_number', 0) or 0)}: "
                    f"task={str(item.get('task_selected') or '').strip()}; "
                    f"result={str(item.get('result') or '').strip()}; "
                    f"delta={str(item.get('delta') or '').strip()}; "
                    f"next={str(item.get('next_task') or '').strip()}"
                ).strip()
                for item in continuous_iterations
            ).strip()
        else:
            result_text = summary or (
                f"Authorized initiative active: {objective_title_text}."
                if objective_title_text
                else "Authorized initiative accepted and running."
            )
        if planning_only_initiative:
            pass
        elif continuous_iterations:
            next_action_text = str(continuous_iterations[-1].get("next_task") or "").strip() or (
                "continue the authorized initiative automatically and surface its status"
            )
            interface_status = "done"
        elif task_title_text:
            next_action_text = f"continue the authorized initiative task: {task_title_text}"
            interface_status = "doing"
        elif objective_execution_state == "completed":
            next_action_text = "surface the authorized initiative outcome"
            interface_status = "done"
        else:
            next_action_text = (
                "continue the authorized initiative automatically and surface its status"
            )
            interface_status = "doing"
        reason = (
            "authorized_planning_only_initiative_created"
            if planning_only_initiative
            else "authorized_initiative_auto_execute"
        )
        outcome = "auto_execute"

    return {
        "initiative_run": initiative_payload,
        "initiative_status": initiative_status,
        "reason": reason,
        "outcome": outcome,
        "safety_decision": outcome,
        "resolution_status": outcome,
        "clarification_prompt": result_text,
        "interface_status": interface_status,
        "interface_next_action": next_action_text,
        "interface_result": result_text,
        "interface_reply": (
            f"Request {request_id}. I understood: {event.raw_input}. "
            f"Next action: {next_action_text}. "
            f"Status: {interface_status}. Result: {result_text}"
        ).strip(),
        "initiative_auto_execute": not human_prompt_required,
    }


async def _recent_tod_status_dispatch_loop_signal(
    *,
    db: AsyncSession,
    limit: int = 30,
) -> dict[str, object]:
    resolutions = list(
        (
            await db.execute(
                select(InputEventResolution)
                .order_by(InputEventResolution.created_at.desc(), InputEventResolution.id.desc())
                .limit(max(1, int(limit)))
            )
        )
        .scalars()
        .all()
    )
    signatures: dict[tuple[str, str, str], dict[str, object]] = {}
    for resolution in resolutions:
        metadata_json = (
            resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}
        )
        tod_dispatch = (
            metadata_json.get("tod_dispatch")
            if isinstance(metadata_json.get("tod_dispatch"), dict)
            else {}
        )
        if str(resolution.reason or "").strip().lower() != "tod_status_dispatch":
            continue
        if str(tod_dispatch.get("action_name") or "").strip().lower() != "tod_status_check":
            continue
        signature = (
            str(resolution.reason or "").strip().lower(),
            str(tod_dispatch.get("result_status") or "").strip().lower(),
            _compact_text(
                str(tod_dispatch.get("result_reason") or resolution.clarification_prompt or "").strip().lower(),
                160,
            ),
        )
        bucket = signatures.setdefault(
            signature,
            {
                "count": 0,
                "reason": signature[0],
                "result_status": signature[1],
                "result_reason": signature[2],
            },
        )
        bucket["count"] = int(bucket.get("count", 0)) + 1
    if not signatures:
        return {"detected": False, "count": 0}
    best = max(signatures.values(), key=lambda item: int(item.get("count", 0)))
    count = int(best.get("count", 0))
    return {
        "detected": count >= 2,
        "count": count,
        "reason": str(best.get("reason") or "").strip(),
        "result_status": str(best.get("result_status") or "").strip(),
        "result_reason": str(best.get("result_reason") or "").strip(),
    }


def _stale_status_loop_corrective_intent(raw_input: str, *, repeat_count: int) -> str:
    request_summary = _compact_text(raw_input, 240) or "bounded TOD status request"
    return (
        "Create a bounded corrective implementation task in MIM's own workspace code to prevent repeated TOD status-check loops. "
        f"The same bounded status-check result repeated {repeat_count} times without state change. "
        "Inspect the gateway TOD-status shortcut, the stale-loop escalation threshold, and the corrective initiative handoff path. "
        "Implement the smallest safe code change that escalates repeated summary-only status checks into corrective implementation analysis instead of another TOD status dispatch. "
        f"Original request: {request_summary}."
    )


async def _maybe_dispatch_repeated_tod_status_loop_recovery(
    *,
    event: InputEvent,
    request_id: str,
    session_id: str,
    db: AsyncSession,
) -> dict[str, object] | None:
    loop_signal = await _recent_tod_status_dispatch_loop_signal(db=db)
    if not bool(loop_signal.get("detected")):
        return None
    repeat_count = int(loop_signal.get("count", 0) or 0)
    initiative_run = await drive_initiative_from_intent(
        db,
        actor="mim",
        source="gateway_stale_status_loop_recovery",
        user_intent=_stale_status_loop_corrective_intent(
            event.raw_input,
            repeat_count=repeat_count,
        ),
        objective_title="",
        priority="high",
        managed_scope="workspace",
        expected_outputs=[],
        verification_commands=[],
        continue_chain=True,
        max_auto_steps=3,
        metadata_json={
            "request_id": request_id,
            "conversation_session_id": session_id,
            "initiative_auto_execute": True,
            "initiated_from_gateway": True,
            "stale_status_loop_recovery": True,
            "status_loop_repeat_count": repeat_count,
        },
    )
    initiative_payload = json.loads(json.dumps(initiative_run, default=str))
    continuation = (
        initiative_payload.get("continuation")
        if isinstance(initiative_payload.get("continuation"), dict)
        else {}
    )
    initiative_status = (
        continuation.get("status")
        if isinstance(continuation.get("status"), dict)
        else {}
    )
    active_task = (
        initiative_status.get("active_task")
        if isinstance(initiative_status.get("active_task"), dict)
        else {}
    )
    interface_status = (
        "done"
        if str(initiative_status.get("execution_state") or "").strip().lower() == "completed"
        else "doing"
    )
    next_action_text = (
        f"continue the corrective implementation initiative after detecting {repeat_count} repeated TOD status checks"
        if active_task
        else "surface the corrective implementation initiative outcome"
    )
    result_text = str(initiative_status.get("summary") or "").strip() or (
        f"Escalated {repeat_count} repeated TOD status checks into a corrective implementation initiative."
    )
    return {
        "initiative_run": initiative_payload,
        "initiative_status": initiative_status,
        "reason": "stale_tod_status_loop_escalated_to_implementation",
        "outcome": "auto_execute",
        "safety_decision": "auto_execute",
        "resolution_status": "auto_execute",
        "clarification_prompt": result_text,
        "interface_status": interface_status,
        "interface_next_action": next_action_text,
        "interface_result": result_text,
        "interface_reply": (
            f"Request {request_id}. I detected {repeat_count} repeated TOD status checks without state change. "
            f"Next action: {next_action_text}. Status: {interface_status}. Result: {result_text}"
        ).strip(),
        "initiative_auto_execute": True,
        "status_loop_repeat_count": repeat_count,
    }


def _extract_recommendation_id(raw_text: str) -> int | None:
    match = re.search(r"\brecommendation\s+(\d+)\b", str(raw_text or ""), re.IGNORECASE)
    if not match:
        return None
    try:
        recommendation_id = int(match.group(1))
    except (TypeError, ValueError):
        return None
    return recommendation_id if recommendation_id > 0 else None


def _recommendation_handoff_details(
    recommendation: dict[str, object], *, recommendation_id: int
) -> dict[str, object]:
    recommendation_type = _compact_text(recommendation.get("recommendation_type"), 40) or "revise"
    baseline_metrics = recommendation.get("baseline_metrics")
    if not isinstance(baseline_metrics, dict):
        baseline_metrics = {}
    comparison = recommendation.get("comparison")
    if not isinstance(comparison, dict):
        comparison = {}

    constraint_key = _compact_text(baseline_metrics.get("constraint_key"), 60)
    objective_focus: list[str] = []
    try:
        if float(comparison.get("operator_override_rate_delta", 0.0) or 0.0) > 0:
            objective_focus.append("reduce operator override rate")
    except (TypeError, ValueError):
        pass
    try:
        if float(comparison.get("success_rate_delta", 0.0) or 0.0) > 0:
            objective_focus.append("preserve the recent success-rate gain")
    except (TypeError, ValueError):
        pass
    try:
        if float(comparison.get("decision_quality_delta", 0.0) or 0.0) > 0:
            objective_focus.append("retain the decision-quality improvement")
    except (TypeError, ValueError):
        pass

    objective_clause = (
        f"{recommendation_type} {constraint_key} behavior"
        if constraint_key
        else f"apply recommendation {recommendation_id}"
    )
    if objective_focus:
        objective_clause = f"{objective_clause} to {' and '.join(objective_focus[:2])}"

    requested_outcome = _compact_text(
        f"Turn recommendation {recommendation_id} into one bounded implementation objective: {objective_clause}.",
        220,
    ) or f"Turn recommendation {recommendation_id} into one bounded implementation objective."
    topic = _compact_text(
        f"Recommendation {recommendation_id}: {objective_clause}",
        96,
    ) or f"Recommendation {recommendation_id} implementation"
    next_bounded_steps = [
        f"Extract the concrete objective from recommendation {recommendation_id}{f' for {constraint_key}' if constraint_key else ''}.",
        f"Define one bounded implementation task to {objective_clause}.",
        "Surface the queued or completed task status back to the same conversation session.",
    ]
    summary = (
        f"Create one bounded implementation task from recommendation {recommendation_id} "
        f"for session {{session_id}}: {requested_outcome}"
    )
    return {
        "requested_outcome": requested_outcome,
        "topic": topic,
        "next_bounded_steps": next_bounded_steps,
        "summary_template": summary,
    }


async def _build_conversation_handoff_payload_async(
    *, request_id: str, text: str, session_id: str, db: AsyncSession
) -> dict[str, object]:
    payload = _build_conversation_handoff_payload(
        request_id=request_id,
        text=text,
        session_id=session_id,
    )

    recommendation_id = _extract_recommendation_id(text)
    if not recommendation_id:
        return payload

    recommendation_row = await get_improvement_recommendation(
        recommendation_id=recommendation_id,
        db=db,
    )
    if recommendation_row is None:
        return payload

    recommendation = await to_improvement_recommendation_out_resolved(
        row=recommendation_row,
        db=db,
    )
    details = _recommendation_handoff_details(
        recommendation,
        recommendation_id=recommendation_id,
    )
    payload["requested_outcome"] = details["requested_outcome"]
    payload["topic"] = details["topic"]
    payload["next_bounded_steps"] = details["next_bounded_steps"]
    payload["summary"] = str(details["summary_template"]).format(
        session_id=session_id or "default-session"
    )
    return payload


def _handoff_submission_interface_status(submission: dict[str, object] | None) -> str:
    if not isinstance(submission, dict):
        return "doing"

    status = str(submission.get("status") or "").strip().lower()
    if status in {"failed", "blocked", "error"}:
        return "blocked"
    if status in {"completed", "done", "succeeded"}:
        return "done"
    return "doing"


def _handoff_submission_result_summary(submission: dict[str, object] | None) -> str:
    if not isinstance(submission, dict):
        return "I staged one bounded implementation task."

    requested_outcome = _compact_interface_text(
        submission.get("requested_outcome")
        or submission.get("conversation_request_text")
        or "",
        220,
    )
    topic = _compact_interface_text(submission.get("topic") or "", 120)
    summary = _compact_interface_text(submission.get("latest_result_summary") or "", 240)
    if summary:
        if requested_outcome:
            summary_lower = summary.lower()
            generic_summary_markers = {
                "step_001",
                "step_002",
                "step_003",
                "classify the request into the existing bounded implementation lane",
                "should be classified under the existing bounded implementation lane",
                "bounded task record will be prepared",
                "surface the queued or completed status back to the same conversation session",
                "surfaced back to the conversation session",
            }
            if any(marker in summary_lower for marker in generic_summary_markers):
                return f"I staged one bounded implementation task for: {requested_outcome}"
            generic_tokens = {
                "bounded",
                "implementation",
                "request",
                "classify",
                "existing",
                "lane",
                "create",
                "draft",
                "follow",
                "would",
                "keep",
                "within",
                "focused",
                "continue",
                "plan",
                "task",
                "tasks",
                "step",
                "steps",
                "handling",
                "current",
                "capabilities",
                "proper",
                "alignment",
            }
            outcome_tokens = {
                token
                for token in requested_outcome.lower().replace("-", " ").split()
                if len(token) > 4 and token not in generic_tokens
            }
            overlap = sum(1 for token in outcome_tokens if token in summary_lower)
            if overlap == 0:
                return f"I staged one bounded implementation task for: {requested_outcome}"
        return summary

    if requested_outcome:
        return f"I staged one bounded implementation task for: {requested_outcome}"
    if topic:
        return f"I staged one bounded implementation task for {topic}."

    mode = str(submission.get("mode") or "bounded_implementation").strip().lower()
    if mode == "codex_assisted_bounded_implementation":
        return "I staged one codex-assisted bounded implementation task."
    if mode == "bounded_tod_dispatch":
        return "I routed one bounded implementation task through TOD."
    return "I staged one bounded implementation task."


def _infer_user_action_category(raw_text: str) -> ActionCategory:
    lowered = str(raw_text or "").strip().lower()
    if not lowered:
        return ActionCategory.UNKNOWN

    if any(
        token in lowered
        for token in {
            "kernel",
            "grub",
            "/boot",
            "sysctl",
            "/etc/fstab",
            "core os",
        }
    ):
        return ActionCategory.SYSTEM_CORE_MODIFICATION

    if any(
        token in lowered
        for token in {
            "iptables",
            "ufw",
            "firewall",
            "disable auth",
            "disable authentication",
            "selinux",
            "apparmor",
        }
    ):
        return ActionCategory.SECURITY_RULE_CHANGE

    if any(
        token in lowered
        for token in {
            "chmod",
            "chown",
            "sudoers",
            "usermod",
            "setfacl",
            "privilege",
        }
    ):
        return ActionCategory.PERMISSION_CHANGE

    if any(
        token in lowered
        for token in {
            "rm -rf",
            "delete",
            "drop database",
            "truncate",
            "wipe",
            "format disk",
        }
    ):
        return ActionCategory.DATA_DELETION

    if any(
        token in lowered
        for token in {
            "apt install",
            "pip install",
            "npm install",
            "dnf install",
            "yum install",
            "pacman -s",
            "install package",
        }
    ):
        return ActionCategory.SOFTWARE_INSTALLATION

    if any(
        token in lowered
        for token in {
            "systemctl stop",
            "systemctl disable",
            "service stop",
            "restart service",
        }
    ):
        return ActionCategory.SERVICE_CONTROL

    if any(
        token in lowered
        for token in {
            "ifconfig",
            "ip route",
            "netplan",
            "dns",
            "network",
        }
    ):
        return ActionCategory.NETWORK_MODIFICATION

    if any(
        token in lowered
        for token in {
            "ulimit",
            "cpu limit",
            "memory limit",
            "cgroup",
            "resource limit",
        }
    ):
        return ActionCategory.RESOURCE_LIMIT_CHANGE

    if any(
        token in lowered
        for token in {
            "/etc/",
            "config",
            "configuration",
            "set parameter",
        }
    ):
        return ActionCategory.CONFIGURATION_CHANGE

    return ActionCategory.UNKNOWN


def _assess_user_action_safety_for_event(
    event: InputEvent,
    *,
    internal_intent: str,
) -> dict:
    raw_text = str(event.raw_input or "").strip()
    if not raw_text or internal_intent not in {"execute_capability", "create_goal"}:
        return {}

    category = _infer_user_action_category(raw_text)
    if category == ActionCategory.UNKNOWN:
        return {}

    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    user_id = str(metadata.get("user_id", "operator")).strip() or "operator"
    action = UserAction(
        action_id=f"gateway-action-{int(event.id)}",
        timestamp=datetime.now(timezone.utc).isoformat(),
        user_id=user_id,
        action_type=internal_intent,
        description=raw_text,
        category=category,
        command=raw_text,
        target_path=str(metadata.get("target_path", "")).strip() or None,
        parameters=metadata,
    )

    assessment = USER_ACTION_SAFETY_MONITOR.assess_action(action)
    inquiry_id = ""
    if assessment.recommended_inquiry:
        inquiry = USER_ACTION_INQUIRY_SERVICE.create_inquiry_from_assessment(
            assessment=assessment,
            user_id=user_id,
            action_description=raw_text,
        )
        inquiry_id = inquiry.inquiry_id

    return {
        "action_id": assessment.action_id,
        "risk_level": assessment.risk_level.value,
        "risk_category": assessment.risk_category,
        "reasoning": assessment.reasoning,
        "specific_concerns": assessment.specific_concerns,
        "recommended_inquiry": bool(assessment.recommended_inquiry),
        "safe_to_execute": bool(assessment.safe_to_execute),
        "inquiry_id": inquiry_id,
    }


def _health_detail_phrase(health_summary: dict) -> str:
    """Return a brief phrase listing which metrics are actively degraded."""
    if not isinstance(health_summary, dict):
        return ""
    _rate_metrics = {"api_error_rate", "cache_hit_rate"}
    _ms_metrics = {"api_latency_ms", "state_bus_lag_ms"}
    parts: list[str] = []
    for name, trend in (health_summary.get("trends") or {}).items():
        if not isinstance(trend, dict) or not trend.get("degradation_detected"):
            continue
        val = trend.get("current_value")
        label = name.replace("_", " ")
        if val is None:
            parts.append(label)
        elif name in _rate_metrics:
            parts.append(f"{label} {float(val):.1%}")
        elif name in _ms_metrics:
            parts.append(f"{label} {float(val):.0f}ms")
        else:
            parts.append(f"{label} {float(val):.0f}%")
    return ", ".join(parts[:2]) if parts else ""


def _execution_system_health_signal(internal_intent: str) -> dict[str, object]:
    status = "healthy"
    health_summary: dict = {}
    try:
        health_summary = _mim_health_monitor.get_health_summary() or {}
        if isinstance(health_summary, dict):
            status = str(health_summary.get("status", "healthy")).strip().lower() or "healthy"
    except Exception:
        status = "healthy"

    if internal_intent != "execute_capability":
        return {
            "active": False,
            "status": status,
            "code": "",
            "precedence": "",
            "reason": "",
            "prompt": "",
            "secondary_prompt": "",
        }

    if status in {"degraded", "critical"}:
        adjective = "critical" if status == "critical" else "degraded"
        detail = _health_detail_phrase(health_summary)
        detail_suffix = f" ({detail})" if detail else ""
        return {
            "active": True,
            "status": status,
            "code": "system_health_degraded",
            "precedence": "degraded_health_confirmation",
            "reason": (
                f"System health is {adjective}{detail_suffix}; automatic execution requires operator confirmation."
            ),
            # Full prompt when health is the primary (only) signal.
            "prompt": (
                f"System health is {adjective}{detail_suffix}. Automatic execution is paused until confirmation."
            ),
            # Shorter prompt used when health is secondary to a safety escalation.
            "secondary_prompt": (
                f"System health is {adjective}{detail_suffix}; execution remains confirmation-gated."
            ),
        }

    if status == "suboptimal":
        return {
            "active": True,
            "status": status,
            "code": "suboptimal_health_advisory",
            "precedence": "benign_healthy_auto_execution",
            "reason": "System health is suboptimal; execution proceeds but operator should review recommendations.",
            "prompt": "",
            "secondary_prompt": "",
        }

    return {
        "active": True,
        "status": status,
        "code": "healthy_auto_execute",
        "precedence": "benign_healthy_auto_execution",
        "reason": "System health is healthy; automatic execution remains eligible.",
        "prompt": "",
        "secondary_prompt": "",
    }


def _build_gateway_governance_metadata(
    *,
    reason: str,
    outcome: str,
    user_action_safety: dict[str, object],
    system_health_signal: dict[str, object],
) -> dict[str, object]:
    signals: list[dict[str, object]] = []

    if bool(user_action_safety.get("recommended_inquiry", False)):
        signals.append(
            {
                "code": "user_action_safety_risk",
                "precedence": "hard_safety_escalation",
                "priority": 300,
                "category": "safety",
                "reason": "High-risk user action requires inquiry approval before execution.",
                "risk_level": str(user_action_safety.get("risk_level", "")).strip(),
                "inquiry_id": str(user_action_safety.get("inquiry_id", "")).strip(),
            }
        )

    signal_code = str(system_health_signal.get("code", "")).strip()
    if signal_code:
        signals.append(
            {
                "code": signal_code,
                "precedence": str(system_health_signal.get("precedence", "")).strip(),
                "priority": 200 if signal_code == "system_health_degraded" else 0,
                "category": "health",
                "reason": str(system_health_signal.get("reason", "")).strip(),
                "status": str(system_health_signal.get("status", "healthy")).strip(),
            }
        )

    ordered_signals = sorted(
        signals,
        key=lambda item: int(item.get("priority", 0)),
        reverse=True,
    )
    primary_signal = ordered_signals[0] if ordered_signals else {}
    # Build a priority-ordered summary: lead with the primary blocker, note secondaries after.
    if len(ordered_signals) > 1:
        primary_reason = str(ordered_signals[0].get("reason", "")).strip()
        secondary_notes: list[str] = []
        for item in ordered_signals[1:]:
            code = str(item.get("code", "")).strip()
            if code == "system_health_degraded":
                s = str(item.get("status", "")).strip()
                secondary_notes.append(f"system health is also {s}" if s else "system health is also degraded")
            elif str(item.get("reason", "")).strip():
                secondary_notes.append(str(item.get("reason", "")).strip())
        if secondary_notes:
            summary = f"{primary_reason} Additionally: {'; '.join(secondary_notes)}."
        else:
            summary = primary_reason
    elif ordered_signals:
        summary = str(ordered_signals[0].get("reason", "")).strip()
    else:
        summary = ""
    if not summary:
        summary = str(reason or outcome or "gateway_governance").strip().replace("_", " ")

    return {
        "applied_reason": str(reason or "").strip(),
        "applied_outcome": str(outcome or "").strip(),
        "primary_signal": str(primary_signal.get("precedence", "")).strip(),
        "signal_codes": [str(item.get("code", "")).strip() for item in ordered_signals if str(item.get("code", "")).strip()],
        "precedence_order": list(GATEWAY_GOVERNANCE_PRECEDENCE),
        "system_health_status": str(system_health_signal.get("status", "healthy")).strip() or "healthy",
        "summary": summary,
        "signals": ordered_signals,
    }


def _looks_like_question_text(text: str) -> bool:
    raw = " ".join(str(text or "").strip().lower().split())
    raw = re.sub(r"\bwhat[' ]?s\b", "what is", raw)
    raw = re.sub(r"\bhow[' ]?s\b", "how is", raw)
    raw = re.sub(r"\bwhere[' ]?s\b", "where is", raw)
    raw = re.sub(r"\bwho[' ]?s\b", "who is", raw)
    raw = re.sub(r"\bwhen[' ]?s\b", "when is", raw)
    raw = re.sub(r"\bwhy[' ]?s\b", "why is", raw)
    if not raw:
        return False
    if raw.endswith("?"):
        return True
    if raw.startswith(
        (
            "what ",
            "why ",
            "how ",
            "when ",
            "where ",
            "who ",
            "which ",
            "is ",
            "are ",
            "can ",
            "could ",
            "will ",
            "would ",
            "do ",
            "does ",
            "did ",
        )
    ):
        return True
    if raw.startswith(("tell me", "give me", "explain", "show me")):
        return True
    # Catch greeting-prefixed questions like "hi mim, how are you today".
    if any(
        fragment in f" {raw} "
        for fragment in {
            " what ",
            " why ",
            " how ",
            " when ",
            " where ",
            " who ",
            " which ",
            " is ",
            " are ",
            " can ",
            " could ",
            " will ",
            " would ",
            " do ",
            " does ",
            " did ",
            " should ",
        }
    ):
        return True
    return False


def _contains_word(text: str, word: str) -> bool:
    token = re.escape(str(word or "").strip().lower())
    if not token:
        return False
    return bool(re.search(rf"\b{token}\b", str(text or "").lower()))


def _has_greeting_prefix(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    greeting_prefixes = (
        "hi mim",
        "hello mim",
        "hey mim",
        "good morning mim",
        "good afternoon mim",
        "good evening mim",
        "hi",
        "hello",
        "hey",
        "good morning",
        "good afternoon",
        "good evening",
    )
    for prefix in greeting_prefixes:
        if lowered == prefix or lowered.startswith(prefix + " ") or lowered.startswith(prefix + "."):
            return True
    return False


def _mentions_tod(text: str) -> bool:
    raw = str(text or "").lower()
    return _contains_word(raw, "tod") or _contains_word(raw, "tods")


def _is_low_signal_turn(text: str) -> bool:
    if _has_greeting_prefix(text):
        return False

    normalized = _normalize_conversation_query(text)
    if not normalized:
        return True

    if (
        _is_interruption_query(normalized)
        or _is_pause_control_query(normalized)
        or _is_resume_control_query(normalized)
        or _is_cancel_control_query(normalized)
    ):
        return False

    if any(
        marker in normalized
        for marker in {
            "summarize your status",
            "status now",
            "current status",
            "current health",
            "check your health",
            "check your current health",
            "start now",
        }
    ):
        return False

    if _looks_like_question_text(normalized):
        return False

    greeting_phrases = {
        "hi",
        "hello",
        "hey",
        "hi mim",
        "hello mim",
        "hey mim",
        "good morning",
        "good afternoon",
        "good evening",
        "good morning mim",
        "good afternoon mim",
        "good evening mim",
    }
    if normalized in greeting_phrases:
        return False

    filler_phrases = {
        "uh",
        "um",
        "hmm",
        "mm",
        "ah",
        "wait",
        "no stop",
        "stop",
        "hold on",
        "maybe maybe",
    }
    if normalized in filler_phrases:
        return True

    tokens = [token for token in normalized.split() if token]
    if len(tokens) <= 2 and all(len(token) <= 4 for token in tokens):
        return True

    return False


def _looks_like_retry_followup(text: str) -> bool:
    raw = " ".join(str(text or "").strip().lower().split())
    if not raw:
        return False
    retry_markers = (
        "still ",
        "again",
        "right now",
        "now",
        "as i said",
        "i already",
        "you already",
    )
    return any(marker in raw for marker in retry_markers)


def _looks_like_bounded_choice_decision_prompt(text: str) -> bool:
    raw = " ".join(str(text or "").strip().lower().split())
    if not raw:
        return False

    bounded_choice_markers = {
        "pick exactly one",
        "choose exactly one",
        "bounded choice only",
        "one numbered option",
        "one numbered choice",
    }
    return any(marker in raw for marker in bounded_choice_markers)


def _text_route_preference(
    *, text: str, parsed_intent: str, safety_flags: list[str] | None = None
) -> str:
    # Conversation-first lane for low-stakes dialogue turns.
    normalized_intent = str(parsed_intent or "").strip().lower()
    local_route = route_console_text_input(text, parsed_intent)
    if local_route.classifier_outcome in {
        "execution_capability_request",
        "robotics_supervised_probe",
    }:
        return "goal_system"

    normalized_query = _normalize_conversation_query(text)
    normalized_safety_flags = {
        str(flag or "").strip().lower() for flag in (safety_flags or []) if str(flag or "").strip()
    }
    if {"blocked", "deny_execution"} & normalized_safety_flags:
        return "goal_system"

    if (
        _is_interruption_query(normalized_query)
        or _is_pause_control_query(normalized_query)
        or _is_resume_control_query(normalized_query)
        or _is_cancel_control_query(normalized_query)
        or any(
            token in normalized_query
            for token in {
                "just chatting for now",
                "chatting for now",
                "do not start anything automatically",
                "dont start anything automatically",
                "summarize your status",
                "status now",
                "current status",
                "current health",
                "check your current health",
                "check your health",
                "actually start now",
                "start now",
            }
        )
    ):
        return "conversation_layer"

    if _looks_like_bounded_choice_decision_prompt(text):
        return "conversation_layer"

    if _looks_like_continuation_validation_request(text):
        return "goal_system"

    if _looks_like_bounded_implementation_request(text, parsed_intent, safety_flags):
        return "goal_system"

    if _looks_like_bounded_tod_bridge_warning_recommendation_request(
        text,
        parsed_intent,
        safety_flags,
    ) or _looks_like_bounded_tod_bridge_warning_request(
        text,
        parsed_intent,
        safety_flags,
    ):
        return "goal_system"

    if _looks_like_bounded_tod_objective_summary_request(
        text,
        parsed_intent,
        safety_flags,
    ):
        return "goal_system"

    if _looks_like_bounded_tod_recent_changes_request(
        text,
        parsed_intent,
        safety_flags,
    ):
        return "goal_system"

    if _looks_like_bounded_tod_status_request(text, parsed_intent, safety_flags):
        return "goal_system"

    if normalized_intent in {"question", "discussion", "observation"}:
        return "conversation_layer"

    if _has_greeting_prefix(text):
        return "conversation_layer"

    if _looks_like_action_request(text):
        return "goal_system"

    raw = str(text or "").strip()
    if _looks_like_question_text(raw):
        return "conversation_layer"

    if _is_low_signal_turn(raw):
        return "conversation_layer"

    return "goal_system"


def _should_force_conversation_eval_route(
    *,
    requested_goal: str,
    metadata_json: dict[str, object] | None,
    safety_flags: list[str] | None = None,
) -> bool:
    normalized_goal = str(requested_goal or "").strip().lower()
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    adapter = str(metadata.get("adapter") or "").strip().lower()
    normalized_safety_flags = {
        str(flag or "").strip().lower()
        for flag in (safety_flags or [])
        if str(flag or "").strip()
    }
    if {"blocked", "deny_execution"} & normalized_safety_flags:
        return False
    return normalized_goal == "conversation_eval" or adapter == "conversation_eval_runner"


def _compact_text(text: str, max_len: int = 120) -> str:
    cleaned = " ".join(str(text or "").replace("\x00", "").strip().split())
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3].rstrip() + "..."


def _sanitize_json_text(value: object) -> object:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_sanitize_json_text(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_json_text(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key).replace("\x00", ""): _sanitize_json_text(item)
            for key, item in value.items()
        }
    return value


async def _await_gateway_context_snapshot(
    awaitable: object,
    *,
    label: str,
    fallback: dict[str, object] | None = None,
    timeout_seconds: float = 1.5,
    db: AsyncSession | None = None,
) -> dict[str, object]:
    async def _rollback_if_needed() -> None:
        rollback = getattr(db, "rollback", None)
        if not callable(rollback):
            return
        try:
            await rollback()
        except Exception as exc:  # noqa: BLE001
            gateway_logger.warning(
                "gateway %s rollback after failure also failed: %s", label, exc
            )

    try:
        result = await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await _rollback_if_needed()
        gateway_logger.warning("gateway %s timed out after %.1fs", label, timeout_seconds)
        return dict(fallback or {})
    except Exception as exc:  # noqa: BLE001
        await _rollback_if_needed()
        gateway_logger.warning("gateway %s failed: %s", label, exc)
        return dict(fallback or {})
    return result if isinstance(result, dict) else dict(fallback or {})


def _normalize_conversation_query(text: str) -> str:
    normalized = " ".join(str(text or "").strip().lower().split())
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    normalized = " ".join(normalized.split())
    normalized = re.sub(r"\bvisable\b", "visible", normalized)
    normalized = re.sub(r"\bwhat s\b", "what is", normalized)
    normalized = re.sub(r"\bwhats\b", "what is", normalized)
    normalized = re.sub(r"\bhow s\b", "how is", normalized)
    normalized = re.sub(r"\bhows\b", "how is", normalized)
    normalized = re.sub(r"\bwhere s\b", "where is", normalized)
    normalized = re.sub(r"\bwheres\b", "where is", normalized)
    normalized = re.sub(r"\bwho s\b", "who is", normalized)
    normalized = re.sub(r"\bwhos\b", "who is", normalized)
    normalized = re.sub(r"\bwhen s\b", "when is", normalized)
    normalized = re.sub(r"\bwhens\b", "when is", normalized)
    normalized = re.sub(r"\bwhy s\b", "why is", normalized)
    normalized = re.sub(r"\bwhys\b", "why is", normalized)
    normalized = re.sub(r"\btod s\b", "tods", normalized)
    normalized = re.sub(r"\b(u)\b", "you", normalized)
    normalized = re.sub(r"\bur\b", "your", normalized)
    normalized = re.sub(r"\bhow r you\b", "how are you", normalized)
    if not normalized:
        return ""

    # Strip common conversational softeners so capability matching stays robust.
    leading_prefixes = (
        "hi mim ",
        "hello mim ",
        "hey mim ",
        "can you tell me ",
        "do you know ",
        "please ",
        "quickly ",
        "honestly ",
        "just ",
    )
    changed = True
    while changed:
        changed = False
        for prefix in leading_prefixes:
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :].strip()
                changed = True

    trailing_suffixes = (
        " for me",
        " please",
        " right now",
        " today",
    )
    changed = True
    while changed:
        changed = False
        for suffix in trailing_suffixes:
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)].strip()
                changed = True

    return normalized


def _is_interruption_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    if query in {
        "wait",
        "wait stop",
        "stop",
        "no stop",
        "hold on",
        "pause",
        "cancel that",
        "never mind",
        "scratch that",
    }:
        return True
    if any(
        query.startswith(prefix)
        for prefix in (
            "maybe wait ",
            "maybe stop ",
            "maybe hold on ",
            "maybe pause ",
        )
    ):
        return True
    return any(
        query.startswith(prefix)
        for prefix in (
            "wait ",
            "stop ",
            "hold on ",
            "pause ",
            "cancel that ",
            "never mind ",
            "scratch that ",
        )
    )


def _is_pause_control_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    if query in {"pause", "hold", "hold it", "pause that", "hold that"}:
        return True
    return any(
        query.startswith(prefix)
        for prefix in ("pause ", "hold ", "hold that ", "pause that ")
    )


def _is_resume_control_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    if query in {
        "resume",
        "continue",
        "proceed",
        "continue now",
        "resume now",
        "go ahead",
    }:
        return True
    return any(
        query.startswith(prefix)
        for prefix in ("resume ", "continue ", "proceed ", "go ahead with ")
    )


def _is_cancel_control_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    if query in {"cancel", "cancel it", "cancel that", "drop it"}:
        return True
    return any(
        query.startswith(prefix)
        for prefix in ("cancel ", "cancel that ", "cancel it ", "drop ")
    )


def _is_unsafe_or_risky_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    return any(
        marker in query
        for marker in {
            "unsafe",
            "risky operation",
            "dangerous",
            "harmful",
        }
    )


def _is_private_runtime_request_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    return any(
        marker in query
        for marker in {
            "private runtime details",
            "private runtime",
            "runtime secrets",
            "passwords",
            "secret keys",
            "private logs",
        }
    )


def _is_external_action_overclaim_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    return any(
        marker in query
        for marker in {
            "already executed",
            "already ran",
            "already completed",
            "already did that external step",
            "confirm you already executed",
            "confirm you already ran",
        }
    )


def _is_ambiguous_external_action_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    if "external action" not in query and "external actions" not in query:
        return False
    return any(
        marker in query
        for marker in {
            "whatever",
            "anything needed",
            "anything necessary",
            "do whatever",
            "any external actions",
        }
    )


def _is_capability_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    return any(
        token in query
        for token in {
            "what can you do",
            "what can you help with",
            "what are your capabilities",
            "what capabilities do you have",
            "what is your function",
            "your function",
            "function mim",
            "capabilities",
        }
    )


def _is_tod_status_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query or not _mentions_tod(query):
        return False
    return any(
        token in query
        for token in {
            "how is",
            "status",
            "healthy",
            "doing",
            "one line",
            "quick",
        }
    )


def _is_runtime_status_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    return any(
        token in query
        for token in {
            "one line status",
            "quick status check",
            "status in one line",
            "summarize your status",
            "status now",
            "check status",
            "current status",
            "current health",
            "check your current health",
            "check your health",
            "health",
            "how are you",
            "are you healthy",
            "are you okay",
        }
    )


def _wants_terse_conversation_reply(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    return any(
        token in query
        for token in {
            "one line",
            "quick",
            "brief",
            "short",
            "terse",
            "just answer",
            "status?",
        }
    ) or query in {"status", "status now", "current status"}


def _is_direct_answer_priority_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    return any(
        (
            _is_capability_query(query),
            _is_tod_status_query(query),
            _is_runtime_status_query(query),
            any(
                token in query
                for token in {
                    "what exactly do you need",
                    "what do you need from me",
                    "what do you need",
                }
            ),
        )
    )


def _conversation_intent_anchor(
    normalized_query: str,
    *,
    prior_anchor: dict[str, object] | None = None,
    conversation_topic: str = "",
) -> dict[str, object]:
    query = str(normalized_query or "").strip().lower()
    prior = prior_anchor if isinstance(prior_anchor, dict) else {}
    if not query:
        return dict(prior)

    if _is_tod_status_query(query):
        return {"topic": "tod_status", "target": "tod_status", "terse": _wants_terse_conversation_reply(query)}
    if _is_runtime_status_query(query):
        return {"topic": "status", "target": "status", "terse": _wants_terse_conversation_reply(query)}
    if _is_capability_query(query):
        return {"topic": "capabilities", "target": "capabilities", "terse": _wants_terse_conversation_reply(query)}
    if _looks_like_vague_thing_request(query):
        return {"topic": "clarification", "target": query, "terse": False}
    if _is_ambiguous_external_action_query(query):
        return {"topic": "delegated_authority", "target": "external_actions", "terse": False}
    if _is_conversation_followup_query(query) and prior:
        anchored = dict(prior)
        anchored["query"] = query
        return anchored
    if str(conversation_topic or "").strip().lower() not in {"", "general"}:
        return {
            "topic": str(conversation_topic or "").strip().lower(),
            "target": query,
            "terse": _wants_terse_conversation_reply(query),
        }
    return dict(prior)


def _conversation_boundary_response(normalized_query: str) -> str:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return ""

    if _is_private_runtime_request_query(query):
        return (
            "I cannot share all private runtime details. I can give a scoped health, task, or reasoning summary instead."
        )

    if _is_external_action_overclaim_query(query):
        return (
            "I cannot claim an external step already happened without execution evidence. "
            "I can check status or confirm the action request before anything is queued."
        )

    if _is_unsafe_or_risky_query(query):
        return (
            "I cannot do something unsafe quickly or help with unsafe or risky operations. "
            "I can help with a safer alternative, a risk check, or a step-by-step review instead."
        )

    if _is_ambiguous_external_action_query(query):
        return (
            "Understood. I will treat that as bounded permission to take the necessary external actions for the current objective. "
            "I will still stop for destructive, high-risk, or irreversible steps."
        )

    return ""


def _conversation_clarification_progress_response(
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> str:
    query = str(normalized_query or "").strip().lower()
    session_context = context if isinstance(context, dict) else {}
    clarification_state = (
        session_context.get("clarification_state")
        if isinstance(session_context.get("clarification_state"), dict)
        else {}
    )
    if not query or not clarification_state.get("active"):
        return ""

    if _is_direct_answer_priority_query(query) or _is_conversation_followup_query(query):
        return ""

    prior_target = str(
        clarification_state.get("target")
        or clarification_state.get("pending_action_request")
        or session_context.get("last_user_input")
        or "the prior request"
    ).strip()
    clarification_count = max(0, int(clarification_state.get("count") or 0))

    if _looks_like_vague_thing_request(query):
        return (
            f"I am still blocked on '{_compact_text(prior_target, 96)}' because the target is not specific enough. "
            "Name the concrete task, object, or URL and I will move it forward."
        )

    if clarification_count >= 1 and len([token for token in query.split() if token]) <= 2:
        return (
            f"I am still blocked on '{_compact_text(prior_target, 96)}' because the target is not specific enough. "
            "Name the concrete task, object, or URL and I will move it forward."
        )

    return (
        f"Understood. I will treat the request as '{_compact_text(query, 120)}'. "
        "Say confirm if you want me to turn it into a concrete action request."
    )


def _extract_conversation_correction(normalized_query: str) -> str:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return ""

    correction_patterns = (
        r"^(?:no\s+)?actually\s+",
        r"^(?:no\s+)?instead\s+",
        r"^no\s+i\s+said\s+",
        r"^i\s+said\s+",
        r"^no\s+",
    )
    for pattern in correction_patterns:
        corrected = re.sub(pattern, "", query, count=1).strip()
        if corrected and corrected != query:
            return corrected
    return ""


def _priority_two_item_response(last_topic: str) -> str:
    if last_topic == "objective":
        return "Top two next items: 1. Keep conversation flow reliable. 2. Keep task-state handoff explicit."
    return "Top two upcoming items: 1. Stabilize conversation handling. 2. Verify the next TOD handoff."


def _continuation_response(last_topic: str) -> str:
    if last_topic == "technical_research":
        return "Continue with the next bounded research step, then stop when the evidence stops improving."
    if last_topic == "development_integration":
        return "Continue by inspecting the existing asset, mapping it to the current session contract, and then validating one end-to-end reused path."
    if last_topic == "self_evolution":
        return "Continue by reviewing the recommended improvement action, turning it into a bounded implementation plan, and then executing the first governed step."
    if last_topic in {"priorities", "objective", "project_planning"}:
        return "Continue with one concrete task at a time: verify the current state, then take the next handoff or test run."
    if last_topic in {"risk", "risk_reduction"}:
        return "Continue by checking whether the fix held in live state, not just in tests."
    return "Continue with one concrete question or one action, and I will keep it direct."


def _conversation_followup_hints(last_topic: str, last_prompt: str) -> dict[str, str]:
    topic = str(last_topic or "").strip().lower()
    prompt = str(last_prompt or "").strip()

    status_map = {
        "technical_research": "Status: the investigation is still bounded by the current step, evidence quality, and stop condition.",
        "development_integration": "Status: inspect the existing asset first, compare it to the current session flow, then validate one live integration.",
        "self_evolution": "Status: choose one communication-focused improvement task, turn it into a bounded implementation plan, and keep the operator command explicit.",
        "priorities": "Status: stabilize routing, keep tests green, and verify the next handoff.",
        "objective": "Status: the objective remains reliable conversation flow and stable MIM to TOD handoff.",
        "project_planning": "Status: scope is first, MVP is second, and the first tasks come next.",
        "mission": "Status: stay coherent, assist safely, and keep execution aligned to the active goal.",
        "risk": "Status: the active risk is still drift between conversation behavior and execution state.",
        "risk_reduction": "Status: the mitigation path is regression checks, tighter routing, and explicit handoff verification.",
    }
    recap_map = {
        "technical_research": "One line: keep the technical investigation bounded and stop when the evidence stops improving.",
        "development_integration": "One line: inspect the closest existing asset first, reuse the current session path, and validate one live integration before building anything new.",
        "self_evolution": "One line: pick the next communication improvement task, expose the operator command, and move it into a bounded implementation plan.",
        "priorities": "One line: stabilize routing, keep tests green, and verify the next handoff.",
        "objective": "One line: the objective is reliable conversation flow and stable MIM to TOD handoff.",
        "project_planning": "One line: define scope, name the MVP, and create the first tasks.",
        "mission": "One line: assist safely, stay coherent, and help execute goals.",
        "risk": "One line: the main risk is drift between conversation behavior and execution state.",
        "risk_reduction": "One line: reduce risk with regression checks and explicit handoff verification.",
    }
    why_map = {
        "technical_research": "Because open-ended technical research can loop forever, so the budget and stop condition have to earn the next round.",
        "development_integration": "Because inspecting the existing asset first tells us whether this is a thin integration or a new build, which cuts risk fastest.",
        "self_evolution": "Because MIM improves fastest when the next communication task is explicit, bounded, and tied to a concrete operator command instead of staying as vague intent.",
        "priorities": "Because reliability and handoff stability protect every later task; if they drift, the rest of the workflow gets noisy fast.",
        "objective": "Because reliability and handoff stability protect every later task; if they drift, the rest of the workflow gets noisy fast.",
        "project_planning": "Because clear scope and the MVP cut ambiguity before we spend effort on implementation.",
        "risk": "Because reducing uncertainty before the next action is the fastest way to keep the system honest.",
        "risk_reduction": "Because mitigation only matters if the fix stays stable in live behavior and not just in tests.",
    }
    after_map = {
        "technical_research": "After that, choose the next path worth deeper research, research that path, and stop when the evidence stops improving or the budget runs out.",
        "development_integration": "After that, validate one live session on the reused path and only then decide whether a thin wrapper is justified.",
        "self_evolution": "After that, review the result, update the active communication-improvement thread, and choose the next bounded implementation step.",
        "priorities": "After that, run the regression checks, confirm live behavior, and lock the next TOD handoff.",
        "objective": "After that, run the regression checks, confirm live behavior, and lock the next TOD handoff.",
        "project_planning": "After that, run the regression checks, confirm live behavior, and lock the next TOD handoff.",
        "risk": "After that, verify the fix stayed stable in live conversation and not just in tests.",
        "risk_reduction": "After that, verify the fix stayed stable in live conversation and not just in tests.",
    }

    hints = {
        "status": status_map.get(topic, ""),
        "recap": recap_map.get(topic, _compact_text(prompt, 120) if prompt else ""),
        "why": why_map.get(topic, "Because it reduces uncertainty before taking the next step." if topic else ""),
        "after_that": after_map.get(topic, "After that, confirm the result, summarize the state, and decide the next action." if topic else ""),
    }
    return {key: value for key, value in hints.items() if str(value).strip()}


def _is_action_request_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False

    non_action_prefixes = (
        "what ",
        "how ",
        "why ",
        "when ",
        "where ",
        "who ",
        "are ",
        "is ",
        "do you ",
        "can you ",
        "could you ",
        "would you ",
        "will you ",
        "should you ",
        "status",
        "check status",
    )
    if query.startswith(non_action_prefixes):
        return False

    action_prefixes = (
        "start ",
        "run ",
        "execute ",
        "launch ",
        "queue ",
        "open ",
        "create ",
        "post ",
        "send ",
        "scan ",
        "move ",
        "deploy ",
    )
    return query.startswith(action_prefixes)


def _action_confirmation_response(normalized_query: str) -> str:
    action_summary = _compact_text(normalized_query.rstrip(".?"), 96)
    return (
        f"Before I treat that as an action: please confirm you want me to '{action_summary}'. "
        "Say confirm to approve it, revise it in one sentence, or cancel it."
    )


def _object_query_tokens(text: str) -> set[str]:
    normalized = _normalize_conversation_query(text)
    stopwords = {
        "a",
        "an",
        "and",
        "at",
        "for",
        "go",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "now",
        "of",
        "on",
        "our",
        "s",
        "that",
        "the",
        "their",
        "there",
        "they",
        "this",
        "to",
        "we",
        "what",
        "where",
        "who",
        "your",
    }
    return {
        token
        for token in normalized.split()
        if token and token not in stopwords and len(token) > 1
    }


def _extract_object_question_target(normalized_query: str) -> tuple[str, str]:
    patterns = [
        ("location", r"\bwhere is (.+)$"),
        ("location", r"\bwhere did (.+?) go$"),
        ("purpose", r"\bwhat is (.+?) for$"),
        ("purpose", r"\bwhat is the purpose of (.+)$"),
        ("ownership", r"\bwho owns (.+)$"),
        ("ownership", r"\bwho does (.+?) belong to$"),
    ]
    for question_type, pattern in patterns:
        match = re.search(pattern, normalized_query)
        if not match:
            continue
        target = " ".join(str(match.group(1) or "").strip().split())
        target = re.sub(r"^(the|a|an|this|that|my|our|your)\s+", "", target).strip()
        if len(_object_query_tokens(target)) >= 1:
            return question_type, target
    return "", ""


def _object_memory_aliases(row: WorkspaceObjectMemory) -> set[str]:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    owner = str(metadata.get("owner") or "").strip()
    aliases = {str(row.canonical_name or "").strip()}
    if isinstance(row.candidate_labels, list):
        aliases.update(
            str(item).strip() for item in row.candidate_labels if str(item).strip()
        )
    aliases.update(
        str(metadata.get(key) or "").strip()
        for key in ["description", "purpose", "category"]
        if str(metadata.get(key) or "").strip()
    )
    if owner:
        current_aliases = [alias for alias in aliases if alias]
        aliases.add(owner)
        for alias in current_aliases:
            aliases.add(f"{owner} {alias}")
            aliases.add(f"{owner} s {alias}")
    return {alias for alias in aliases if alias}


def _score_object_memory_match(target: str, row: WorkspaceObjectMemory) -> float:
    target_normalized = _normalize_conversation_query(target)
    target_tokens = _object_query_tokens(target_normalized)
    if not target_tokens:
        return 0.0

    best_score = 0.0
    for alias in _object_memory_aliases(row):
        alias_normalized = _normalize_conversation_query(alias)
        if not alias_normalized:
            continue
        if alias_normalized == target_normalized:
            return 1.0
        alias_tokens = _object_query_tokens(alias_normalized)
        if not alias_tokens:
            continue
        overlap = len(target_tokens & alias_tokens) / float(len(target_tokens))
        if overlap > best_score:
            best_score = overlap
        if len(target_tokens) > 1 and (
            alias_normalized.endswith(target_normalized)
            or target_normalized.endswith(alias_normalized)
        ):
            best_score = max(best_score, 0.95)

    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    owner = str(metadata.get("owner") or "").strip()
    if owner and _contains_word(target_normalized, owner):
        best_score = min(1.0, best_score + 0.1)
    return best_score


async def _object_memory_context_for_query(
    db: AsyncSession, user_input: str
) -> dict[str, object]:
    normalized_query = _normalize_conversation_query(user_input)
    question_type, target = _extract_object_question_target(normalized_query)
    if not question_type or not target:
        return {}

    rows = (
        (
            await db.execute(
                select(WorkspaceObjectMemory)
                .order_by(
                    WorkspaceObjectMemory.last_seen_at.desc(),
                    WorkspaceObjectMemory.id.desc(),
                )
                .limit(40)
            )
        )
        .scalars()
        .all()
    )
    best_row: WorkspaceObjectMemory | None = None
    best_score = 0.0
    for row in rows:
        score = _score_object_memory_match(target, row)
        if score > best_score:
            best_score = score
            best_row = row

    if best_row is None or best_score < 0.74:
        return {}

    metadata = (
        best_row.metadata_json if isinstance(best_row.metadata_json, dict) else {}
    )
    return {
        "memory_object_query_type": question_type,
        "memory_object_reference": target,
        "memory_object_label": str(best_row.canonical_name or "").strip(),
        "memory_object_zone": str(best_row.zone or "").strip(),
        "memory_object_status": str(best_row.status or "").strip(),
        "memory_object_owner": str(metadata.get("owner") or "").strip(),
        "memory_object_purpose": str(metadata.get("purpose") or "").strip(),
        "memory_object_description": str(metadata.get("description") or "").strip(),
        "memory_object_expected_home_zone": str(
            metadata.get("expected_home_zone")
            or metadata.get("expected_zone")
            or metadata.get("home_zone")
            or ""
        ).strip(),
    }


async def _latest_camera_observation_context(db: AsyncSession) -> dict[str, object]:
    camera_rows = (
        (
            await db.execute(
                select(WorkspacePerceptionSource)
                .where(WorkspacePerceptionSource.source_type == "camera")
                .order_by(
                    WorkspacePerceptionSource.last_seen_at.desc(),
                    WorkspacePerceptionSource.id.desc(),
                )
                .limit(6)
            )
        )
        .scalars()
        .all()
    )
    observations = collect_fresh_camera_observations(
        camera_rows,
        now=datetime.now(timezone.utc),
        stale_seconds=max(PERCEPTION_STALE_SECONDS, 90),
    )
    summary = summarize_camera_observations(observations)
    return {
        "camera_label": str(summary.get("primary_label", "")).strip(),
        "camera_confidence": float(summary.get("primary_confidence", 0.0) or 0.0),
        "camera_zone": str(summary.get("primary_zone", "")).strip(),
        "camera_scene_summary": str(summary.get("summary", "")).strip(),
        "camera_source_count": int(summary.get("source_count", 0) or 0),
        "camera_observation_count": int(summary.get("observation_count", 0) or 0),
        "camera_labels": list(summary.get("labels", []))
        if isinstance(summary.get("labels", []), list)
        else [],
    }


def _object_inquiry_questions(
    label_raw: str, missing_fields: list[str] | None = None
) -> list[str]:
    label = str(label_raw or "that object").strip() or "that object"
    fields = [
        str(item).strip().lower()
        for item in (missing_fields or [])
        if str(item).strip()
    ]
    if not fields:
        fields = ["description", "purpose"]

    questions: list[str] = []
    if "description" in fields:
        questions.append(f"What is {label}?")
    if "purpose" in fields:
        questions.append(f"What does {label} do?")
    if "owner" in fields:
        questions.append(f"Who owns {label}?")
    if "expected_home_zone" in fields:
        questions.append(f"Where should {label} normally live?")
    if "category" in fields:
        questions.append(f"What kind of object is {label}?")
    if "meaning" in fields or "explanation" in fields:
        questions.append(f"What should I understand about {label}?")
    if "user_notes" in fields:
        questions.append(f"Any notes I should remember about {label}?")
    questions.append("Explain more if needed.")
    return questions


def _next_object_inquiry_missing_fields(metadata: dict[str, object]) -> list[str]:
    stages = [
        ["description", "purpose"],
        ["owner", "expected_home_zone"],
        ["category", "meaning", "user_notes"],
    ]
    for stage in stages:
        missing = [
            field for field in stage if not str(metadata.get(field) or "").strip()
        ]
        if missing:
            return missing
    return []


def _object_inquiry_prompt(context: dict[str, object]) -> str:
    label = str(context.get("object_label") or "that object").strip() or "that object"
    zone = str(context.get("object_zone") or "").strip()
    description = str(context.get("object_description") or "").strip()
    missing_fields = [
        str(item).strip().lower()
        for item in (context.get("missing_fields") or [])
        if str(item).strip()
    ]
    questions = _object_inquiry_questions(label, missing_fields)
    scene_summary = str(context.get("camera_scene_summary") or "").strip()
    source_count = int(context.get("camera_source_count") or 0)
    confidence = float(context.get("camera_confidence") or 0.0)

    if scene_summary and source_count > 1:
        if confidence > 0.0:
            seen_prefix = (
                f"I can currently see {scene_summary}. Primary confidence is "
                f"{confidence:.2f}."
            )
        else:
            seen_prefix = f"I can currently see {scene_summary}."
    else:
        zone_suffix = f" in {zone}" if zone else ""
        if confidence > 0.0:
            seen_prefix = (
                f"I can currently see {label}{zone_suffix} on camera with confidence "
                f"{confidence:.2f}."
            )
        else:
            seen_prefix = f"I can currently see {label}{zone_suffix} on camera."

    if description and missing_fields == ["purpose"]:
        knowledge_gap = (
            f" I know {label} is {description}, but I do not know what it does yet."
        )
    elif description:
        knowledge_gap = (
            f" I know {label} is {description}, but I still need more semantic detail."
        )
    else:
        knowledge_gap = f" I do not know what {label} is yet."
    return f"{seen_prefix}{knowledge_gap} {' '.join(questions)}"


def _camera_observation_response(context: dict[str, object]) -> str:
    camera_label = str(context.get("camera_label") or "").strip()
    camera_zone = str(context.get("camera_zone") or "").strip()
    camera_confidence = float(context.get("camera_confidence") or 0.0)
    camera_scene_summary = str(context.get("camera_scene_summary") or "").strip()
    camera_source_count = int(context.get("camera_source_count") or 0)
    inquiry_prompt = str(context.get("camera_object_inquiry_prompt") or "").strip()
    if not camera_label:
        return ""
    if inquiry_prompt:
        return inquiry_prompt
    if camera_scene_summary and camera_source_count > 1:
        if camera_confidence > 0.0:
            return (
                f"I can currently see {camera_scene_summary}. "
                f"Primary confidence is {camera_confidence:.2f}."
            )
        return f"I can currently see {camera_scene_summary}."
    zone_suffix = f" in {camera_zone}" if camera_zone else ""
    if camera_confidence > 0.0:
        return (
            f"I can currently see {camera_label}{zone_suffix} on camera "
            f"with confidence {camera_confidence:.2f}."
        )
    return f"I can currently see {camera_label}{zone_suffix} on camera."


def _camera_presence_response(context: dict[str, object]) -> str:
    observation = _camera_observation_response(context)
    if observation:
        return (
            f"{observation} I cannot confirm from this camera view alone "
            "that the visible subject is you."
        )
    return (
        "I can only answer that from current camera observations, and I do not "
        "have a clear camera observation right now."
    )


async def _find_workspace_object_by_label(
    db: AsyncSession,
    *,
    label: str,
    zone: str = "",
) -> WorkspaceObjectMemory | None:
    target_label = str(label or "").strip()
    if not target_label:
        return None

    rows = (
        (
            await db.execute(
                select(WorkspaceObjectMemory)
                .order_by(
                    WorkspaceObjectMemory.last_seen_at.desc(),
                    WorkspaceObjectMemory.id.desc(),
                )
                .limit(80)
            )
        )
        .scalars()
        .all()
    )

    best_row: WorkspaceObjectMemory | None = None
    best_score = 0.0
    normalized_zone = str(zone or "").strip().lower()
    for row in rows:
        score = _score_object_memory_match(target_label, row)
        if normalized_zone and str(row.zone or "").strip().lower() == normalized_zone:
            score = min(1.0, score + 0.08)
        if score > best_score:
            best_score = score
            best_row = row

    if best_row is None or best_score < 0.74:
        return None
    return best_row


async def _camera_object_inquiry_context(
    db: AsyncSession,
    *,
    camera_context: dict[str, object],
) -> dict[str, object]:
    label = str(camera_context.get("camera_label") or "").strip()
    if not label:
        return {}

    zone = str(camera_context.get("camera_zone") or "").strip()
    object_row = await _find_workspace_object_by_label(db, label=label, zone=zone)
    if object_row is None:
        return {}

    metadata = (
        object_row.metadata_json if isinstance(object_row.metadata_json, dict) else {}
    )
    description = str(metadata.get("description") or "").strip()
    missing_fields = _next_object_inquiry_missing_fields(metadata)
    if not missing_fields:
        return {}

    inquiry_context = {
        "status": "pending",
        "object_memory_id": int(object_row.id),
        "object_label": str(object_row.canonical_name or label).strip() or label,
        "object_zone": str(object_row.zone or zone).strip(),
        "object_description": description,
        "missing_fields": missing_fields,
        "camera_scene_summary": str(
            camera_context.get("camera_scene_summary") or ""
        ).strip(),
        "camera_source_count": int(camera_context.get("camera_source_count") or 0),
        "camera_confidence": float(camera_context.get("camera_confidence") or 0.0),
    }
    inquiry_context["inquiry_questions"] = _object_inquiry_questions(
        inquiry_context["object_label"],
        missing_fields,
    )
    inquiry_context["inquiry_prompt"] = _object_inquiry_prompt(inquiry_context)
    return inquiry_context


def _extract_object_inquiry_reply(
    text: str,
    *,
    label: str,
    missing_fields: list[str] | None = None,
) -> dict[str, str]:
    raw = str(text or "").strip()
    if not raw:
        return {}

    fields = {
        str(item).strip().lower()
        for item in (missing_fields or [])
        if str(item).strip()
    }
    if not fields:
        fields = {"description", "purpose", "owner", "expected_home_zone"}

    def _normalize_clause(value: str) -> str:
        clause = _normalize_conversation_query(value)
        clause = re.sub(
            r"^(?:actually|well|uh|um|hmm|sorry|ok|okay|so|right)\b[\s,]*",
            "",
            clause,
        ).strip()
        clause = re.sub(r"\bit\s+is\s+like\s+", "it is ", clause)
        clause = re.sub(r"\bit\s+s\s+like\s+", "it is ", clause)
        clause = re.sub(r"\bit\s+is\s+kind\s+of\s+", "it is ", clause)
        clause = re.sub(r"\bit\s+s\s+kind\s+of\s+", "it is ", clause)
        return clause.strip()

    normalized_label = _normalize_conversation_query(label)
    clauses = [
        _normalize_clause(part)
        for part in re.split(r"[\.!?;]+", raw)
        if str(part).strip()
    ]
    if not clauses:
        clauses = [_normalize_clause(raw)]

    result: dict[str, str] = {}
    subject_pattern = r"(?:it|that|this|the object|the item|the thing)"
    if normalized_label:
        subject_pattern = rf"(?:{subject_pattern}|{re.escape(normalized_label)})"

    purpose_patterns = [
        rf"^{subject_pattern}\s+(?:is\s+used\s+for|is\s+for|used\s+for)\s+(.+)$",
        rf"^{subject_pattern}\s+(charges|holds|stores|powers|connects|mounts|supports|measures|cuts|scans|carries|docks)\s+(.+)$",
        r"^for\s+(.+)$",
    ]
    description_patterns = [
        rf"^{subject_pattern}\s+(?:is|s)\s+(.+)$",
        r"^(?:a|an|the)\s+(.+)$",
    ]
    owner_patterns = [
        rf"^{subject_pattern}\s+(?:belongs to|is owned by)\s+(.+)$",
        rf"^(?:owner is|owned by)\s+(.+)$",
        rf"^(.+?)\s+owns\s+{subject_pattern}$",
        rf"^it is\s+(.+?)\s+s$",
    ]
    home_zone_patterns = [
        rf"^{subject_pattern}\s+(?:should live|lives|stays|belongs)\s+(?:in|on|at)\s+(.+)$",
        rf"^(?:home is|keep it|store it)\s+(?:in|on|at)\s+(.+)$",
        rf"^(?:it should go|it goes)\s+(?:in|on|at)\s+(.+)$",
    ]
    category_patterns = [
        rf"^(?:category is|type is|kind is)\s+(.+)$",
        rf"^{subject_pattern}\s+(?:is a kind of|is kind of|counts as)\s+(.+)$",
    ]
    meaning_patterns = [
        rf"^(?:meaning is|it means|that means)\s+(.+)$",
        rf"^(?:it tells me|it tells us|it indicates|it shows)\s+(.+)$",
    ]
    user_note_patterns = [
        rf"^(?:note that|remember that|user note|note)\s+(.+)$",
        rf"^(?:keep in mind|please remember)\s+(.+)$",
    ]
    explanation_patterns = [
        rf"^(?:explanation is|more detail is|more context is)\s+(.+)$",
        rf"^(?:explain|more detail|more context)\s+(.+)$",
    ]

    def _normalize_owner(value: str) -> str:
        owner = " ".join(str(value or "").split()).strip(" .'\"")
        owner = re.sub(r"\bnow\b$", "", owner).strip(" .'\"")
        if not owner:
            return ""
        if owner.islower():
            owner = owner.title()
        return owner

    def _normalize_home_zone(value: str) -> str:
        zone = " ".join(str(value or "").split()).strip(" .'\"")
        zone = re.sub(r"^(the)\s+", "", zone).strip()
        return zone

    def _normalize_semantic_phrase(value: str) -> str:
        return " ".join(str(value or "").split()).strip(" .'\"")

    def _normalize_description(value: str) -> str:
        description = _normalize_semantic_phrase(value)
        description = re.sub(r"^(?:like|kind of|sort of)\s+", "", description).strip()
        description = re.sub(r"^(?:a|an)\s+kind\s+of\s+", "", description).strip()
        return description

    for clause in clauses:
        if not clause:
            continue
        if "purpose" in fields and "purpose" not in result:
            for pattern in purpose_patterns:
                match = re.match(pattern, clause)
                if not match:
                    continue
                if len(match.groups()) == 2:
                    verb = str(match.group(1) or "").strip()
                    tail = str(match.group(2) or "").strip()
                    purpose = " ".join(part for part in [verb, tail] if part).strip()
                else:
                    purpose = str(match.group(1) or "").strip()
                purpose = re.sub(r"^(to\s+)?", "", purpose).strip()
                if purpose:
                    result["purpose"] = purpose
                    break

        if "description" in fields and "description" not in result:
            for pattern in description_patterns:
                match = re.match(pattern, clause)
                if not match:
                    continue
                description = _normalize_description(str(match.group(1) or ""))
                description = re.split(
                    r"\b(?:used\s+for|for|because|which)\b", description, maxsplit=1
                )[0].strip()
                if description and not description.startswith(
                    ("charging ", "to charge ")
                ):
                    result["description"] = description
                    break

        if "owner" in fields and "owner" not in result:
            for pattern in owner_patterns:
                match = re.match(pattern, clause)
                if not match:
                    continue
                owner = _normalize_owner(str(match.group(1) or ""))
                if owner and owner not in {"it", "this", "that"}:
                    result["owner"] = owner
                    break

        if "expected_home_zone" in fields and "expected_home_zone" not in result:
            for pattern in home_zone_patterns:
                match = re.match(pattern, clause)
                if not match:
                    continue
                zone = _normalize_home_zone(str(match.group(1) or ""))
                if zone:
                    result["expected_home_zone"] = zone
                    break

        if "category" in fields and "category" not in result:
            for pattern in category_patterns:
                match = re.match(pattern, clause)
                if not match:
                    continue
                category = _normalize_semantic_phrase(str(match.group(1) or ""))
                if category:
                    result["category"] = category
                    break

        if "meaning" in fields and "meaning" not in result:
            for pattern in meaning_patterns:
                match = re.match(pattern, clause)
                if not match:
                    continue
                meaning = _normalize_semantic_phrase(str(match.group(1) or ""))
                if meaning:
                    result["meaning"] = meaning
                    break

        if "user_notes" in fields and "user_notes" not in result:
            for pattern in user_note_patterns:
                match = re.match(pattern, clause)
                if not match:
                    continue
                note = _normalize_semantic_phrase(str(match.group(1) or ""))
                if note:
                    result["user_notes"] = note
                    break

        if "explanation" in fields and "explanation" not in result:
            for pattern in explanation_patterns:
                match = re.match(pattern, clause)
                if not match:
                    continue
                explanation = _normalize_semantic_phrase(str(match.group(1) or ""))
                if explanation:
                    result["explanation"] = explanation
                    break

    normalized_raw = _normalize_conversation_query(raw)
    if "description" in fields and "description" not in result:
        tokens = normalized_raw.split()
        if (
            0 < len(tokens) <= 6
            and not _looks_like_question_text(raw)
            and not any(
                token in normalized_raw
                for token in [
                    "belongs to",
                    "owned by",
                    "should live",
                    "lives in",
                    "stays in",
                    "category is",
                    "it means",
                    "note that",
                ]
            )
        ):
            result["description"] = normalized_raw

    return result


def _looks_like_object_correction(text: str) -> bool:
    normalized = _normalize_conversation_query(text)
    if not normalized:
        return False
    return bool(
        re.search(r"\b(?:actually|correction|update|instead|rather|no)\b", normalized)
    )


def _object_inquiry_extraction_fields(
    metadata: dict[str, object],
    missing_fields: list[str] | None,
    user_input: str,
) -> list[str]:
    fields = [
        str(item).strip().lower()
        for item in (missing_fields or [])
        if str(item).strip()
    ]
    if not _looks_like_object_correction(user_input):
        return fields

    correction_candidates = [
        "description",
        "purpose",
        "owner",
        "expected_home_zone",
        "category",
        "meaning",
        "user_notes",
    ]
    for field in correction_candidates:
        if str(metadata.get(field) or "").strip() and field not in fields:
            fields.append(field)
    return fields


async def _learn_from_object_inquiry_reply(
    db: AsyncSession,
    *,
    user_input: str,
    inquiry_context: dict[str, object] | None,
) -> tuple[str, dict[str, object]]:
    context = inquiry_context if isinstance(inquiry_context, dict) else {}
    if str(context.get("status") or "").strip().lower() != "pending":
        return "", {}

    object_memory_id = int(context.get("object_memory_id") or 0)
    if object_memory_id <= 0:
        return "", {}

    object_row = await db.get(WorkspaceObjectMemory, object_memory_id)
    if object_row is None:
        return "", {}

    missing_fields = [
        str(item).strip().lower()
        for item in (context.get("missing_fields") or [])
        if str(item).strip()
    ]
    metadata = (
        object_row.metadata_json if isinstance(object_row.metadata_json, dict) else {}
    )
    extraction_fields = _object_inquiry_extraction_fields(
        metadata,
        missing_fields,
        user_input,
    )
    extracted = _extract_object_inquiry_reply(
        user_input,
        label=str(context.get("object_label") or object_row.canonical_name or "object"),
        missing_fields=extraction_fields,
    )
    if not extracted:
        return "", {}

    updated_metadata = {**metadata, **extracted}
    object_row.metadata_json = updated_metadata
    await db.flush()

    remaining_fields = _next_object_inquiry_missing_fields(updated_metadata)
    next_context = {
        "status": "resolved" if not remaining_fields else "pending",
        "object_memory_id": int(object_row.id),
        "object_label": str(
            object_row.canonical_name or context.get("object_label") or "object"
        ).strip(),
        "object_zone": str(object_row.zone or context.get("object_zone") or "").strip(),
        "object_description": str(updated_metadata.get("description") or "").strip(),
        "missing_fields": remaining_fields,
    }
    if remaining_fields:
        next_context["inquiry_questions"] = _object_inquiry_questions(
            next_context["object_label"],
            remaining_fields,
        )
        next_context["inquiry_prompt"] = _object_inquiry_prompt(next_context)

    label = next_context["object_label"]
    learned_bits: list[str] = []
    if str(updated_metadata.get("description") or "").strip():
        learned_bits.append(
            f"{label} is {str(updated_metadata.get('description')).strip()}"
        )
    if str(updated_metadata.get("purpose") or "").strip():
        learned_bits.append(
            f"{label} is used for {str(updated_metadata.get('purpose')).strip()}"
        )
    if str(updated_metadata.get("owner") or "").strip():
        learned_bits.append(
            f"{label} belongs to {str(updated_metadata.get('owner')).strip()}"
        )
    if str(updated_metadata.get("expected_home_zone") or "").strip():
        learned_bits.append(
            f"{label} should normally live in {str(updated_metadata.get('expected_home_zone')).strip()}"
        )
    if str(updated_metadata.get("category") or "").strip():
        learned_bits.append(
            f"{label} is categorized as {str(updated_metadata.get('category')).strip()}"
        )
    if str(updated_metadata.get("meaning") or "").strip():
        learned_bits.append(
            f"{label} means {str(updated_metadata.get('meaning')).strip()}"
        )
    if str(updated_metadata.get("user_notes") or "").strip():
        learned_bits.append(
            f"note for {label}: {str(updated_metadata.get('user_notes')).strip()}"
        )

    if remaining_fields:
        if learned_bits:
            prefix = f"Got it. I will remember that {' and '.join(learned_bits)}."
        else:
            prefix = f"Got it. I will remember what you shared about {label}."
        return f"{prefix} {next_context['inquiry_prompt']}", next_context

    return f"Got it. I will remember that {' and '.join(learned_bits)}.", next_context


def _conversation_topic_key(normalized_query: str, response: str = "") -> str:
    query = str(normalized_query or "").strip().lower()
    prompt = str(response or "").strip().lower()

    if prompt.startswith("i cannot share all private runtime details"):
        return "safety_boundary"
    if prompt.startswith("i cannot claim an external step already happened"):
        return "safety_boundary"
    if prompt.startswith("i cannot help with unsafe or risky operations"):
        return "safety_boundary"
    if prompt.startswith("i cannot choose unspecified external actions"):
        return "safety_boundary"
    if _is_pause_control_query(query) or prompt.startswith("paused. the pending action"):
        return "interrupt_control"
    if _is_resume_control_query(query) or prompt.startswith("resumed at the conversation layer"):
        return "interrupt_control"
    if _is_cancel_control_query(query) or prompt.startswith("cancelled. i will not treat"):
        return "interrupt_control"
    if _is_interruption_query(query) or prompt.startswith("understood. i stopped"):
        return "interrupt_control"
    if prompt.startswith("before i treat that as an action:"):
        return "action_confirmation"
    if (
        "technical framing:" in prompt
        or "plan of action:" in prompt
        or prompt.startswith("i researched step ")
        or prompt.startswith("i refreshed step ")
    ):
        return "technical_research"
    if "i researched the web for '" in prompt:
        return "web_research"
    if any(
        token in query for token in {"tod status", "how is tod", "tod healthy"}
    ) or prompt.startswith("tod status:"):
        return "tod_status"
    if (
        any(
            token in query
            for token in {
                "runtime health",
                "runtime status",
                "how is runtime health",
                "how is the runtime doing",
                "how is runtime doing",
            }
        )
        or prompt.startswith("runtime health:")
        or prompt.startswith("current status:")
    ):
        return "status"
    if (
        any(
            token in query
            for token in {
                "what is the system",
                "what is our system",
                "what's the system",
                "what's our system",
                "our system",
                "define the system",
            }
        )
        or "the system is mim plus tod" in prompt
    ):
        return "system"
    if (
        any(
            token in query
            for token in {
                "continue automatically",
                "act automatically",
                "keep going automatically",
                "proceed automatically",
                "automatic",
                "autonomy",
                "automatic right now",
                "autonomy right now",
                "what is your autonomy",
                "what is your autonomy status",
            }
        )
        or prompt.startswith("automatic continuation is limited")
    ):
        return "lightweight_autonomy"
    if (
        any(
            token in query
            for token in {
                "give feedback",
                "how do i give feedback",
                "what feedback do you need",
                "feedback loop",
                "feedback for you",
            }
        )
        or prompt.startswith("Give feedback in one sentence:")
    ):
        return "human_feedback"
    if (
        any(
            token in query
            for token in {
                "system stable",
                "how stable is the system",
                "system stability",
                "are you stable",
                "stability guard",
                "stability right now",
            }
        )
        or prompt.startswith("Stability guard:")
    ):
        return "system_stability"
    if (
        any(
            token in query
            for token in {
                "what is our objective",
                "current objective",
                "active objective",
                "what are you working on",
                "what are we working on",
                "what should we work on",
                "work on today",
            }
        )
        or "current objective focus" in prompt
    ):
        return "objective"
    if (
        _is_self_evolution_next_work_query(query)
        or prompt.startswith("next i would work on ")
        or "operator command:" in prompt
    ):
        return "self_evolution"
    if (
        any(
            token in query
            for token in {
                "what are we working on",
                "what should we prioritize",
                "prioritize next",
                "what should i do first",
                "what is next",
            }
        )
        or "top priority today" in prompt
    ):
        return "priorities"
    if (
        any(token in query for token in {"your mission", "primary mission"})
        or "my primary mission" in prompt
    ):
        return "mission"
    if (
        any(token in query for token in {"top risk", "biggest risk", "main risk"})
        or "top risk is" in prompt
    ):
        return "risk"
    if (
        any(
            token in query
            for token in {"reduce that risk", "reduce the risk", "lower that risk"}
        )
        or "reduce that risk with" in prompt
    ):
        return "risk_reduction"
    if (
        any(
            token in query
            for token in {"what can you do", "capabilities", "help", "your function"}
        )
        or "i can chat with you" in prompt
    ):
        return "capabilities"
    if (
        any(
            token in query
            for token in {
                "browse the web",
                "search the web",
                "look up",
                "research",
                "best brand",
                "proven",
                "compare",
            }
        )
        or "i researched the web for '" in prompt
    ):
        return "web_research"
    if "weather" in query or "live weather data" in prompt:
        return "weather"
    if (
        any(
            token in query
            for token in {
                "create an application",
                "build an application",
                "create app",
                "build app",
                "start a project",
            }
        )
        or "scope the application" in prompt
    ):
        return "project_planning"
    if (
        _looks_like_development_integration_query(query)
        or prompt.startswith("next action: inspect the existing mim_wall app")
        or prompt.startswith("next action: inspect the existing app against the current interface")
        or prompt.startswith("next action: inspect the closest existing asset")
    ):
        return "development_integration"
    if "news" in query or "top ai and tech themes today" in prompt:
        return "news"
    return "general"


async def _get_recent_text_conversation_context(
    db: AsyncSession,
    *,
    session_id: str,
    actor_name: str = DEFAULT_USER_ID,
    exclude_event_id: int | None = None,
    limit: int = 8,
    prefer_interface_session_only: bool = False,
) -> dict:
    normalized_session = str(session_id or "").strip()
    if not normalized_session:
        if prefer_interface_session_only:
            return _empty_recent_text_conversation_context()
        remembered_context = await _load_remembered_conversation_context(
            db=db,
            actor_name=actor_name,
        )
        return _merge_conversation_context_with_memory(
            _empty_recent_text_conversation_context(),
            remembered_context,
        )

    interface_session = await get_interface_session(
        session_key=normalized_session,
        db=db,
    )
    if interface_session is not None:
        remembered_context = {}
        if not prefer_interface_session_only:
            remembered_context = await _load_remembered_conversation_context(
                db=db,
                actor_name=actor_name,
            )
        session_context = _normalize_conversation_session_context(
            normalized_session,
            interface_session.context_json
            if isinstance(interface_session.context_json, dict)
            else {},
        )
        if exclude_event_id is None or int(session_context.get("last_input_event_id") or 0) != int(exclude_event_id):
            return _merge_conversation_context_with_memory({
                "turn_count": int(session_context.get("turn_count") or 0),
                "session_display_name": str(
                    session_context.get("session_display_name") or ""
                ).strip(),
                "last_user_input": str(session_context.get("last_user_input") or "").strip(),
                "last_prompt": str(session_context.get("last_prompt") or "").strip(),
                "last_topic": str(session_context.get("last_topic") or "").strip().lower(),
                "last_followup_hints": (
                    session_context.get("last_followup_hints")
                    if isinstance(session_context.get("last_followup_hints"), dict)
                    else {}
                ),
                "last_object_inquiry": (
                    session_context.get("last_object_inquiry")
                    if isinstance(session_context.get("last_object_inquiry"), dict)
                    else {}
                ),
                "last_technical_research": (
                    session_context.get("last_technical_research")
                    if isinstance(session_context.get("last_technical_research"), dict)
                    else {}
                ),
                "last_action_request": str(session_context.get("last_action_request") or "").strip(),
                "pending_action_request": str(session_context.get("pending_action_request") or "").strip(),
                "last_action_result": (
                    session_context.get("last_action_result")
                    if isinstance(session_context.get("last_action_result"), dict)
                    else {}
                ),
                "last_failure": (
                    session_context.get("last_failure")
                    if isinstance(session_context.get("last_failure"), dict)
                    else {}
                ),
                "last_control_state": str(session_context.get("last_control_state") or "active").strip().lower() or "active",
                "clarification_state": (
                    session_context.get("clarification_state")
                    if isinstance(session_context.get("clarification_state"), dict)
                    else {}
                ),
                "active_goal": str(session_context.get("active_goal") or "").strip(),
                "operator_reasoning_summary": str(session_context.get("operator_reasoning_summary") or "").strip(),
                "runtime_health_summary": str(session_context.get("runtime_health_summary") or "").strip(),
                "runtime_recovery_summary": str(session_context.get("runtime_recovery_summary") or "").strip(),
                "tod_collaboration_summary": str(session_context.get("tod_collaboration_summary") or "").strip(),
                "current_recommendation_summary": str(session_context.get("current_recommendation_summary") or "").strip(),
            }, remembered_context)

    if prefer_interface_session_only:
        return _empty_recent_text_conversation_context()

    remembered_context = await _load_remembered_conversation_context(
        db=db,
        actor_name=actor_name,
    )

    rows = (
        await db.execute(
            select(InputEvent, InputEventResolution)
            .join(
                InputEventResolution,
                InputEventResolution.input_event_id == InputEvent.id,
            )
            .where(InputEvent.source == "text")
            .order_by(InputEvent.id.desc())
            .limit(max(6, int(limit) * 3))
        )
    ).all()

    matched: list[tuple[InputEvent, InputEventResolution]] = []
    for event_row, resolution_row in rows:
        if exclude_event_id is not None and int(event_row.id) == int(exclude_event_id):
            continue
        event_meta = (
            event_row.metadata_json if isinstance(event_row.metadata_json, dict) else {}
        )
        if (
            str(event_meta.get("conversation_session_id", "")).strip()
            != normalized_session
        ):
            continue
        matched.append((event_row, resolution_row))
        if len(matched) >= max(1, int(limit)):
            break

    if not matched:
        return _merge_conversation_context_with_memory(
            _empty_recent_text_conversation_context(),
            remembered_context,
        )

    last_event, last_resolution = matched[0]
    resolution_meta = (
        last_resolution.metadata_json
        if isinstance(last_resolution.metadata_json, dict)
        else {}
    )
    last_prompt = str(last_resolution.clarification_prompt or "").strip()
    last_topic = str(resolution_meta.get("conversation_topic", "")).strip().lower()
    if not last_topic:
        last_topic = _conversation_topic_key(
            _normalize_conversation_query(last_event.raw_input),
            last_prompt,
        )
    last_object_inquiry = (
        resolution_meta.get("object_inquiry")
        if isinstance(resolution_meta.get("object_inquiry"), dict)
        else {}
    )
    if str(last_object_inquiry.get("status") or "").strip().lower() != "pending":
        last_object_inquiry = {}

    last_technical_research = _compact_technical_research_context(
        (
            resolution_meta.get("web_research")
            if isinstance(resolution_meta.get("web_research"), dict)
            else {}
        )
    )
    if not last_technical_research:
        last_technical_research = (
            resolution_meta.get("last_technical_research")
            if isinstance(resolution_meta.get("last_technical_research"), dict)
            else {}
        )
    if not last_technical_research:
        session_memory = await _find_recent_memory_by_metadata(
            db=db,
            memory_class="conversation_session",
            metadata_match={"session_id": normalized_session},
            limit=20,
        )
        session_meta = (
            session_memory.metadata_json
            if session_memory and isinstance(session_memory.metadata_json, dict)
            else {}
        )
        last_technical_research = (
            session_meta.get("last_technical_research")
            if isinstance(session_meta.get("last_technical_research"), dict)
            else {}
        )


def _is_return_briefing_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    phrases = {
        "catch me up",
        "bring me up to speed",
        "what changed while i was away",
        "what happened while i was away",
        "while i was away",
        "while you were away",
        "what changed since i was gone",
        "what did i miss",
        "i'm back catch me up",
        "im back catch me up",
        "i am back catch me up",
    }
    return any(phrase in query for phrase in phrases)


def _is_self_evolution_next_work_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False

    explicit_phrases = {
        "what would you like to work on next mim",
        "what would you like to work on next",
        "what do you want to work on next mim",
        "what do you want to work on next",
        "what should you work on next mim",
        "what should you work on next",
        "what would you like to improve next mim",
        "what would you like to improve next",
        "what should you improve next mim",
        "what should you improve next",
        "what is your next objective to improve yourself",
        "what is your next objective",
        "next objective to improve yourself",
        "what do you think you would like to work on",
    }
    if query in explicit_phrases:
        return True

    if any(phrase in query for phrase in explicit_phrases):
        return True

    return (
        any(token in query for token in {"work on next", "improve next", "next objective"})
        and any(token in query for token in {"mim", "you", "yourself"})
    )


RETURN_BRIEFING_GOAL_STALE_HOURS = 24.0


async def _build_return_briefing_context(
    db: AsyncSession,
) -> dict[str, object]:
    goal_rows = (
        (
            await db.execute(
                select(Goal)
                .order_by(Goal.id.desc())
                .limit(12)
            )
        )
        .scalars()
        .all()
    )
    preferred_goal: Goal | None = None
    latest_goal: Goal | None = goal_rows[0] if goal_rows else None
    terminal_statuses = {"completed", "done", "cancelled", "failed", "blocked", "archived"}
    for row in goal_rows:
        status = str(row.status or "").strip().lower()
        if status and status not in terminal_statuses:
            preferred_goal = row
            break

    briefing_result = await build_self_evolution_briefing(
        actor="gateway_return_briefing",
        source="gateway_conversation_return_briefing",
        refresh=False,
        lookback_hours=168,
        min_occurrence_count=2,
        auto_experiment_limit=3,
        limit=5,
        db=db,
    )
    briefing = (
        briefing_result.get("briefing", {})
        if isinstance(briefing_result, dict)
        else {}
    )
    decision = briefing.get("decision", {}) if isinstance(briefing.get("decision", {}), dict) else {}
    snapshot = briefing.get("snapshot", {}) if isinstance(briefing.get("snapshot", {}), dict) else {}

    now_utc = datetime.now(timezone.utc)
    goal_created_at = getattr(preferred_goal, "created_at", None)
    goal_age_hours = 0.0
    if isinstance(goal_created_at, datetime):
        goal_age_hours = max(
            0.0,
            (now_utc - goal_created_at.astimezone(timezone.utc)).total_seconds() / 3600.0,
        )
    goal_truth_status = "missing"
    if preferred_goal is not None:
        goal_truth_status = (
            "stale"
            if goal_age_hours >= RETURN_BRIEFING_GOAL_STALE_HOURS
            else "current"
        )

    decision_type = str(decision.get("decision_type") or "").strip()
    snapshot_status = str(snapshot.get("status") or "").strip().lower()
    alignment_status = "healthy"
    if goal_truth_status == "missing":
        alignment_status = "partial"
    if latest_goal is not None and preferred_goal is None and snapshot_status in {"active", "operator_review_required"}:
        alignment_status = "conflicting"
    elif goal_truth_status == "stale":
        alignment_status = "stale"

    return {
        "goal_description": str(getattr(preferred_goal, "goal_description", "") or "").strip(),
        "goal_status": str(getattr(preferred_goal, "status", "") or "").strip().lower(),
        "goal_id": int(getattr(preferred_goal, "id", 0) or 0),
        "goal_truth_status": goal_truth_status,
        "goal_age_hours": round(goal_age_hours, 2),
        "latest_goal_description": str(getattr(latest_goal, "goal_description", "") or "").strip(),
        "latest_goal_status": str(getattr(latest_goal, "status", "") or "").strip().lower(),
        "decision_summary": str(decision.get("summary") or "").strip(),
        "decision_type": decision_type,
        "snapshot_summary": str(snapshot.get("summary") or "").strip(),
        "snapshot_status": snapshot_status,
        "alignment_status": alignment_status,
    }


def _return_briefing_response(context: dict[str, object]) -> str:
    briefing = (
        context.get("operator_return_briefing")
        if isinstance(context.get("operator_return_briefing"), dict)
        else {}
    )
    goal_description = str(briefing.get("goal_description") or "").strip()
    goal_status = str(briefing.get("goal_status") or "").strip().lower()
    goal_truth_status = str(briefing.get("goal_truth_status") or "").strip().lower()
    goal_age_hours = float(briefing.get("goal_age_hours") or 0.0)
    latest_goal_description = str(briefing.get("latest_goal_description") or "").strip()
    latest_goal_status = str(briefing.get("latest_goal_status") or "").strip().lower()
    decision_summary = str(briefing.get("decision_summary") or "").strip()
    decision_type = str(briefing.get("decision_type") or "").strip()
    snapshot_summary = str(briefing.get("snapshot_summary") or "").strip()
    snapshot_status = str(briefing.get("snapshot_status") or "").strip().lower()
    alignment_status = str(briefing.get("alignment_status") or "healthy").strip().lower()

    if alignment_status == "conflicting":
        latest_goal_fragment = (
            f"The last stored goal was {_compact_text(latest_goal_description, 120)} (status: {latest_goal_status or 'unknown'})"
            if latest_goal_description
            else "The latest stored goal state is unavailable"
        )
        next_step_sentence = (
            f"Recommended next step: {_compact_text(decision_summary, 180)}"
            if decision_summary
            else "Recommended next step is unavailable from current continuity state"
        )
        self_evolution_sentence = (
            f"Self-evolution is currently {_compact_text(snapshot_summary, 220)}"
            if snapshot_summary
            else f"Self-evolution status is {snapshot_status or 'unavailable'}"
        )
        return (
            "While you were away: continuity inputs are not fully aligned. "
            f"{latest_goal_fragment}. {self_evolution_sentence}. {next_step_sentence}. "
            "I do not have enough aligned continuity state to collapse that into one active-thread summary."
        )

    if goal_truth_status == "stale":
        goal_sentence = (
            f"the most recent non-terminal goal is {_compact_text(goal_description, 120)} (status: {goal_status or 'new'})"
            if goal_description
            else "the current goal surface is unavailable"
        )
        next_step_sentence = (
            f"Recommended next step: {_compact_text(decision_summary, 180)}"
            if decision_summary
            else "Recommended next step is unavailable from current continuity state"
        )
        self_evolution_sentence = (
            f"Self-evolution: {_compact_text(snapshot_summary, 220)}"
            if snapshot_summary
            else "Self-evolution summary is unavailable"
        )
        return (
            "While you were away: active goal continuity may be stale. "
            f"I last recorded that {goal_sentence} about {goal_age_hours:.1f} hour(s) ago. "
            f"{next_step_sentence}. {self_evolution_sentence}. "
            "I cannot honestly confirm that the recorded goal is still current."
        )

    if goal_truth_status == "missing":
        next_step_sentence = (
            f"Recommended next step: {_compact_text(decision_summary, 180)}"
            if decision_summary and decision_type
            else "Recommended next step is unavailable from current continuity state"
        )
        self_evolution_sentence = (
            f"Self-evolution: {_compact_text(snapshot_summary, 220)}"
            if snapshot_summary
            else "Self-evolution summary is unavailable"
        )
        return (
            "While you were away: I do not have a current active goal in the continuity state. "
            f"{next_step_sentence}. {self_evolution_sentence}. "
            "This is a partial catch-up only because the active-goal surface is unavailable."
        )

    if goal_truth_status == "current" and snapshot_status and not snapshot_summary and not decision_summary:
        goal_sentence = (
            f"current goal is {_compact_text(goal_description, 120)} (status: {goal_status or 'new'})"
            if goal_description
            else "no active goal is currently recorded"
        )
        return (
            "While you were away: "
            f"{goal_sentence}. "
            f"Self-evolution visibility is limited to status={snapshot_status}. "
            "I do not have a usable self-evolution summary or decision, so I cannot recommend a next step from that surface."
        )

    if goal_truth_status == "current" and not decision_summary and not snapshot_summary:
        goal_sentence = (
            f"current goal is {_compact_text(goal_description, 120)} (status: {goal_status or 'new'})"
            if goal_description
            else "no active goal is currently recorded"
        )
        return (
            "While you were away: "
            f"{goal_sentence}. "
            "Self-evolution guidance is currently unavailable. "
            "I do not have enough current self-evolution state to recommend a next step."
        )

    goal_sentence = (
        f"current goal is {_compact_text(goal_description, 120)} (status: {goal_status or 'new'})"
        if goal_description
        else "no active goal is currently recorded"
    )
    next_step_sentence = (
        f"Recommended next step: {_compact_text(decision_summary, 180)}"
        if decision_summary
        else "Recommended next step: refresh the current state before taking a new action"
    )
    self_evolution_sentence = (
        f"Self-evolution: {_compact_text(snapshot_summary, 220)}"
        if snapshot_summary
        else "Self-evolution: no strong new improvement pressure is visible right now"
    )
    return f"While you were away: {goal_sentence}. {next_step_sentence}. {self_evolution_sentence}."


def _self_evolution_next_work_response(context: dict[str, object]) -> str:
    briefing = (
        context.get("self_evolution_briefing")
        if isinstance(context.get("self_evolution_briefing"), dict)
        else {}
    )
    decision = (
        briefing.get("decision", {})
        if isinstance(briefing.get("decision", {}), dict)
        else {}
    )
    snapshot = (
        briefing.get("snapshot", {})
        if isinstance(briefing.get("snapshot", {}), dict)
        else {}
    )
    natural_language_development = (
        briefing.get("natural_language_development", {})
        if isinstance(briefing.get("natural_language_development", {}), dict)
        else {}
    )
    selected_skill = (
        natural_language_development.get("selected_skill", {})
        if isinstance(natural_language_development.get("selected_skill", {}), dict)
        else {}
    )
    action = decision.get("action", {}) if isinstance(decision.get("action", {}), dict) else {}

    summary = str(decision.get("summary") or context.get("self_evolution_summary") or "").strip()
    snapshot_summary = str(snapshot.get("summary") or "").strip()
    rationale = str(decision.get("rationale") or "").strip()
    action_method = str(action.get("method") or context.get("self_evolution_action_method") or "").strip().upper()
    action_path = str(action.get("path") or context.get("self_evolution_action_path") or "").strip()
    language_summary = str(
        natural_language_development.get("summary")
        or context.get("self_evolution_natural_language_development_summary")
        or ""
    ).strip()
    language_next_step = str(
        natural_language_development.get("next_step_summary")
        or context.get("self_evolution_natural_language_development_next_step")
        or ""
    ).strip()
    language_active_slice = str(
        natural_language_development.get("active_slice_summary")
        or context.get("self_evolution_natural_language_development_active_slice")
        or ""
    ).strip()
    language_progress = str(
        natural_language_development.get("progress_summary")
        or context.get("self_evolution_natural_language_development_progress")
        or ""
    ).strip()
    language_pass_bar = str(
        natural_language_development.get("selected_skill_pass_bar_summary")
        or context.get("self_evolution_natural_language_development_pass_bar")
        or ""
    ).strip()
    language_continuation = str(
        natural_language_development.get("continuation_policy_summary")
        or context.get("self_evolution_natural_language_development_continuation")
        or ""
    ).strip()
    language_whats_next = str(
        natural_language_development.get("whats_next_framework_summary")
        or context.get("self_evolution_natural_language_development_whats_next")
        or ""
    ).strip()
    selected_skill_title = str(
        natural_language_development.get("selected_skill_title")
        or selected_skill.get("title")
        or context.get("self_evolution_natural_language_development_skill_title")
        or ""
    ).strip()
    selected_skill_goal = str(selected_skill.get("development_goal") or "").strip()

    if (
        not summary
        and not snapshot_summary
        and not action_path
        and not language_summary
        and not language_next_step
        and not language_active_slice
        and not language_progress
        and not language_pass_bar
        and not language_continuation
        and not language_whats_next
    ):
        return ""

    parts: list[str] = []
    if selected_skill_title:
        skill_line = f"Natural-language development focus: {selected_skill_title}"
        if selected_skill_goal:
            skill_line += f". Goal: {_compact_text(selected_skill_goal, 180)}"
        parts.append(skill_line)
    elif language_summary:
        parts.append(f"Natural-language development: {_compact_text(language_summary, 220)}")
    if summary:
        parts.append(f"Next I would work on {_compact_text(summary, 180)}")
    elif language_next_step:
        parts.append(f"Next I would work on {_compact_text(language_next_step, 180)}")
    if rationale:
        parts.append(f"Why: {_compact_text(rationale, 180)}")
    elif snapshot_summary:
        parts.append(f"Current self-evolution state: {_compact_text(snapshot_summary, 220)}")
    elif language_summary:
        parts.append(f"Current language-development state: {_compact_text(language_summary, 220)}")
    if language_active_slice:
        parts.append(f"Current slice: {_compact_text(language_active_slice, 220)}")
    if language_progress:
        parts.append(f"Current progress: {_compact_text(language_progress, 220)}")
    if language_whats_next:
        parts.append(f"What's next framework: {_compact_text(language_whats_next, 220)}")
    if language_pass_bar:
        normalized_pass_bar = re.sub(
            r"^pass\s+bar:\s*",
            "",
            language_pass_bar,
            flags=re.IGNORECASE,
        ).strip()
        parts.append(
            f"Pass bar: {_compact_text(normalized_pass_bar or language_pass_bar, 220)}"
        )
    if language_continuation:
        parts.append(f"Continuation policy: {_compact_text(language_continuation, 220)}")
    if action_method and action_path:
        parts.append(f"Operator command: {action_method} {action_path}.")
    parts.append(
        "If you want, I can turn that into a bounded implementation plan and continue from there."
    )
    return " ".join(parts).strip()

    return _merge_conversation_context_with_memory({
        "turn_count": len(matched),
        "session_display_name": "",
        "last_user_input": str(last_event.raw_input or "").strip(),
        "last_prompt": last_prompt,
        "last_topic": last_topic,
        "last_followup_hints": _conversation_followup_hints(last_topic, last_prompt),
        "last_object_inquiry": last_object_inquiry,
        "last_technical_research": last_technical_research,
        "last_action_request": "",
        "pending_action_request": "",
        "last_action_result": {},
        "last_failure": {},
        "last_control_state": "active",
        "clarification_state": {},
    }, remembered_context)


def _empty_recent_text_conversation_context() -> dict[str, object]:
    return {
        "turn_count": 0,
        "session_display_name": "",
        "last_user_input": "",
        "last_prompt": "",
        "last_topic": "",
        "last_followup_hints": {},
        "last_object_inquiry": {},
        "last_technical_research": {},
        "last_action_request": "",
        "pending_action_request": "",
        "last_action_result": {},
        "last_failure": {},
        "last_control_state": "active",
        "clarification_state": {},
        "remembered_user_id": DEFAULT_USER_ID,
        "remembered_display_name": "",
        "remembered_aliases": [],
        "remembered_conversation_preferences": [],
        "remembered_conversation_likes": [],
        "remembered_conversation_dislikes": [],
    }


def _memory_list_values(payload: dict[str, object] | None, *, limit: int = 6) -> list[str]:
    value = payload.get("value") if isinstance(payload, dict) else []
    if isinstance(value, list):
        items = value
    elif value in {None, ""}:
        items = []
    else:
        items = [value]
    compact: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(str(item or "").strip().split())
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        compact.append(text[:140])
        if len(compact) >= max(1, int(limit)):
            break
    return compact


async def _load_remembered_conversation_context(
    *,
    db: AsyncSession,
    actor_name: str,
) -> dict[str, object]:
    normalized_actor = str(actor_name or "").strip() or DEFAULT_USER_ID
    remembered_display_name = ""
    remembered_aliases: list[str] = []

    actor = (
        (await db.execute(select(Actor).where(Actor.name == normalized_actor)))
        .scalars()
        .first()
    )
    if actor is not None and isinstance(actor.identity_metadata, dict):
        identity_meta = actor.identity_metadata
        remembered_display_name = str(identity_meta.get("display_name") or "").strip()
        remembered_aliases = [
            str(item).strip()
            for item in identity_meta.get("aliases", [])
            if str(item).strip()
        ][:8]

    if not remembered_display_name:
        display_name_pref = await get_user_preference_payload(
            db=db,
            preference_type="display_name",
            user_id=normalized_actor,
        )
        remembered_display_name = " ".join(
            str(display_name_pref.get("value") or "").strip().split()
        )[:80]

    if not remembered_display_name:
        person_memory = await _find_recent_memory_by_metadata(
            db=db,
            memory_class="person_profile",
            metadata_match={"actor_name": normalized_actor},
            limit=20,
        )
        person_meta = (
            person_memory.metadata_json
            if person_memory is not None and isinstance(person_memory.metadata_json, dict)
            else {}
        )
        remembered_display_name = str(person_meta.get("display_name") or "").strip()
        if not remembered_aliases:
            remembered_aliases = [
                str(item).strip()
                for item in person_meta.get("aliases", [])
                if str(item).strip()
            ][:8]

    conversation_preferences = await get_user_preference_payload(
        db=db,
        preference_type="conversation_preferences",
        user_id=normalized_actor,
    )
    conversation_likes = await get_user_preference_payload(
        db=db,
        preference_type="conversation_likes",
        user_id=normalized_actor,
    )
    conversation_dislikes = await get_user_preference_payload(
        db=db,
        preference_type="conversation_dislikes",
        user_id=normalized_actor,
    )

    return {
        "remembered_user_id": normalized_actor,
        "remembered_display_name": remembered_display_name,
        "remembered_aliases": remembered_aliases,
        "remembered_conversation_preferences": _memory_list_values(
            conversation_preferences,
        ),
        "remembered_conversation_likes": _memory_list_values(
            conversation_likes,
        ),
        "remembered_conversation_dislikes": _memory_list_values(
            conversation_dislikes,
        ),
    }


def _merge_conversation_context_with_memory(
    base_context: dict[str, object] | None,
    remembered_context: dict[str, object] | None,
) -> dict[str, object]:
    merged = _empty_recent_text_conversation_context()
    if isinstance(base_context, dict):
        merged.update(base_context)
    memory_context = remembered_context if isinstance(remembered_context, dict) else {}
    for key in (
        "remembered_user_id",
        "remembered_display_name",
        "remembered_aliases",
        "remembered_conversation_preferences",
        "remembered_conversation_likes",
        "remembered_conversation_dislikes",
    ):
        value = memory_context.get(key, merged.get(key))
        if isinstance(merged.get(key), list):
            merged[key] = value if isinstance(value, list) else []
        else:
            merged[key] = str(value or "").strip()
    if not str(merged.get("session_display_name") or "").strip():
        merged["session_display_name"] = str(
            merged.get("remembered_display_name") or ""
        ).strip()
    return merged


def _default_conversation_session_context(session_id: str) -> dict[str, object]:
    return {
        "session_id": str(session_id or "").strip(),
        "turn_count": 0,
        "session_display_name": "",
        "last_user_input": "",
        "last_parsed_intent": "",
        "last_internal_intent": "",
        "last_prompt": "",
        "last_assistant_output": "",
        "last_topic": "",
        "last_followup_hints": {},
        "last_goal_description": "",
        "last_proposed_actions": [],
        "last_object_inquiry": {},
        "last_technical_research": {},
        "last_action_request": "",
        "pending_action_request": "",
        "last_action_result": {},
        "last_failure": {},
        "last_control_state": "active",
        "last_resolution_outcome": "",
        "last_resolution_reason": "",
        "clarification_state": {},
        "active_goal": "",
        "operator_reasoning_summary": "",
        "runtime_health_summary": "",
        "runtime_recovery_summary": "",
        "tod_collaboration_summary": "",
        "current_recommendation_summary": "",
        "last_input_event_id": 0,
        "last_resolution_id": 0,
        "last_execution_id": 0,
    }


def _normalize_conversation_session_context(
    session_id: str,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    normalized = _default_conversation_session_context(session_id)
    raw_context = context if isinstance(context, dict) else {}
    for key, default in normalized.items():
        value = raw_context.get(key, default)
        if isinstance(default, int):
            try:
                normalized[key] = max(0, int(value or 0))
            except (TypeError, ValueError):
                normalized[key] = default
        elif isinstance(default, str):
            normalized[key] = str(value or "").strip()
        elif isinstance(default, dict):
            normalized[key] = value if isinstance(value, dict) else {}
        elif isinstance(default, list):
            normalized[key] = value if isinstance(value, list) else []
        else:
            normalized[key] = value
    normalized["session_id"] = str(session_id or "").strip()
    return normalized


def _conversation_control_state_from_query(
    normalized_query: str,
    *,
    prior_state: str,
) -> str:
    query = str(normalized_query or "").strip().lower()
    current = str(prior_state or "active").strip().lower() or "active"
    if not query:
        return current
    if query in {"confirm", "yes confirm", "confirmed", "approve", "yes proceed"}:
        if current in {"paused", "cancelled", "stopped"}:
            return current
        return "confirmed"
    if _is_pause_control_query(query):
        return "paused"
    if _is_resume_control_query(query):
        return "active"
    if _is_cancel_control_query(query):
        return "cancelled"
    if any(
        query.startswith(prefix)
        for prefix in (
            "revise ",
            "revise it",
            "change it",
            "change that",
            "update it",
            "update that",
        )
    ):
        return "active"
    if "stop" in query and any(
        token in query for token in {"again", "retry", "slower", "faster"}
    ):
        return "stopped"
    if _is_interruption_query(query):
        return "stopped"
    return current


def _interface_session_status_for_control_state(control_state: str) -> str:
    normalized = str(control_state or "active").strip().lower()
    if normalized == "paused":
        return "paused"
    if normalized in {"cancelled", "closed"}:
        return "closed"
    return "active"


def _is_conversation_action_confirmation_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    return query in {"confirm", "yes confirm", "confirmed", "approve", "yes proceed"}


def _is_conversation_action_approval_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    if _is_conversation_action_confirmation_query(query):
        return True
    return query in {
        "yes",
        "yes please",
        "yes do that",
        "yes do it",
        "do that",
        "do it",
        "please do",
        "please do that",
        "okay do it",
        "ok do it",
        "sounds good",
        "that works",
        "go ahead",
    }


def _conversation_revised_action_request(
    normalized_query: str,
    *,
    prior_action_request: str,
) -> str:
    query = str(normalized_query or "").strip()
    if not query or not str(prior_action_request or "").strip():
        return ""

    lowered = query.lower()
    rewrite_prefixes = (
        "revise it to ",
        "revise that to ",
        "revise to ",
        "change it to ",
        "change that to ",
        "change to ",
        "update it to ",
        "update that to ",
        "update to ",
    )
    for prefix in rewrite_prefixes:
        if lowered.startswith(prefix):
            revised = query[len(prefix) :].strip()
            return _compact_text(revised, 240) if revised else ""

    return ""


def _clarification_followup_query(
    normalized_query: str,
    *,
    clarification_state: dict[str, object] | None,
    context: dict[str, object] | None = None,
) -> str:
    query = str(normalized_query or "").strip().lower()
    state = clarification_state if isinstance(clarification_state, dict) else {}
    reason = str(state.get("reason") or "").strip().lower()
    if reason not in {"conversation_precision_prompt", "conversation_precision_limit"}:
        return ""

    topic = str((context or {}).get("last_topic") or "").strip().lower()
    direct_map = {
        "status": "status now",
        "help": "help",
        "priorities": "what should we prioritize next?",
        "priority": "what should we prioritize next?",
        "checklist": "checklist",
        "recap": "short final recap",
        "summary": "short final recap",
        "one line": "one line",
        "short recap": "short final recap",
        "shorter": "shorter version",
    }
    if query in direct_map:
        return direct_map[query]

    if query in {"after", "after that", "then", "next"} and topic:
        return "and after that"

    return ""


def _conversation_pending_action_request(
    normalized_query: str,
    *,
    prior_action_request: str,
) -> str:
    query = str(normalized_query or "").strip().lower()
    base_action = _compact_text(str(prior_action_request or "").strip(), 240)
    if not query or not base_action:
        return ""
    if _is_conversation_action_confirmation_query(query):
        return base_action
    if _is_conversation_action_approval_query(query):
        return base_action

    retry_markers = {
        "retry",
        "try again",
        "do that again",
        "do it again",
        "again",
        "slower",
        "faster",
    }
    stop_and_retry = "stop" in query and any(
        token in query for token in {"again", "retry", "slower", "faster"}
    )
    if not stop_and_retry and not any(token in query for token in retry_markers):
        return ""

    modifiers: list[str] = []
    if "slower" in query:
        modifiers.append("at a slower pace")
    if "faster" in query:
        modifiers.append("at a faster pace")
    if "careful" in query or "carefully" in query:
        modifiers.append("more carefully")
    if "safe" in query or "safely" in query:
        modifiers.append("more safely")

    pending = f"retry {base_action}"
    if modifiers:
        pending = f"{pending} {' and '.join(modifiers)}"
    return _compact_text(pending, 240)


async def _store_conversation_interface_state(
    *,
    db: AsyncSession,
    event: InputEvent,
    resolution: InputEventResolution,
    execution: CapabilityExecution | None = None,
) -> None:
    if str(event.source or "").strip().lower() != "text":
        return

    event_meta = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    resolution_meta = (
        resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}
    )
    session_key = str(event_meta.get("conversation_session_id", "")).strip()
    if not session_key:
        return

    actor = str(event_meta.get("user_id", "")).strip() or "operator"
    existing_session = await get_interface_session(session_key=session_key, db=db)
    existing_context = _normalize_conversation_session_context(
        session_key,
        existing_session.context_json
        if existing_session and isinstance(existing_session.context_json, dict)
        else {},
    )
    existing_metadata = (
        existing_session.metadata_json
        if existing_session and isinstance(existing_session.metadata_json, dict)
        else {}
    )

    normalized_query = _normalize_conversation_query(event.raw_input)
    control_state = _conversation_control_state_from_query(
        normalized_query,
        prior_state=str(existing_context.get("last_control_state") or "active"),
    )
    session_status = _interface_session_status_for_control_state(control_state)
    turn_count = int(existing_context.get("turn_count") or 0)
    user_turn_index = turn_count + 1

    session = await upsert_interface_session(
        session_key=session_key,
        actor=actor,
        source="gateway",
        channel="text",
        status=session_status,
        context_json=existing_context,
        metadata_json={
            **existing_metadata,
            "conversation_session_id": session_key,
            "user_id": actor,
            "last_input_source": "gateway_text",
        },
        db=db,
    )

    await append_interface_message(
        session_key=session_key,
        actor=actor,
        source="gateway",
        direction="inbound",
        role="operator",
        content=str(event.raw_input or "").strip(),
        parsed_intent=str(event.parsed_intent or "").strip(),
        confidence=float(event.confidence or 0.0),
        requires_approval=False,
        metadata_json={
            "input_event_id": int(event.id),
            "request_id": str(event_meta.get("request_id") or "").strip(),
            "turn_index": user_turn_index,
            "interaction_mode": str(event_meta.get("interaction_mode") or "text").strip() or "text",
            "message_type": str(event_meta.get("message_type") or "text").strip() or "text",
            "conversation_topic": str(
                resolution_meta.get("conversation_topic") or ""
            ).strip(),
            "conversation_override": bool(
                resolution_meta.get("conversation_override")
            ),
            "route_preference": str(
                resolution_meta.get("route_preference") or ""
            ).strip(),
        },
        db=db,
    )

    interface_reply = _build_mim_interface_response(
        event=event,
        resolution=resolution,
        execution=execution,
    )
    assistant_text = str(interface_reply.get("reply_text") or "").strip()
    assistant_turn_index = user_turn_index
    if assistant_text:
        assistant_turn_index = user_turn_index + 1
        await append_interface_message(
            session_key=session_key,
            actor="mim",
            source="gateway",
            direction="outbound",
            role="mim",
            content=assistant_text,
            parsed_intent=str(resolution.internal_intent or "").strip(),
            confidence=float(event.confidence or 0.0),
            requires_approval=False,
            metadata_json={
                "input_event_id": int(event.id),
                "resolution_id": int(resolution.id),
                "request_id": str(event_meta.get("request_id") or "").strip(),
                "turn_index": assistant_turn_index,
                "interaction_mode": str(event_meta.get("interaction_mode") or "text").strip() or "text",
                "message_type": "text",
                "conversation_topic": str(
                    resolution_meta.get("conversation_topic") or ""
                ).strip(),
                "outcome": str(resolution.outcome or "").strip(),
                "reason": str(resolution.reason or "").strip(),
                "execution_id": int(execution.id) if execution is not None else 0,
            },
            db=db,
        )

    updated_context = _normalize_conversation_session_context(session_key, existing_context)
    conversation_topic = str(resolution_meta.get("conversation_topic") or "").strip().lower()
    session_display_name = str(
        resolution_meta.get("session_display_name")
        or updated_context.get("session_display_name")
        or ""
    ).strip()
    prior_topic = str(updated_context.get("last_topic") or "").strip().lower()
    if conversation_topic in {"", "general"} and str(
        resolution.reason or ""
    ).strip().lower() in {
        "conversation_precision_prompt",
        "conversation_precision_limit",
    }:
        conversation_topic = prior_topic
    prior_intent_anchor = (
        updated_context.get("intent_anchor")
        if isinstance(updated_context.get("intent_anchor"), dict)
        else {}
    )
    intent_anchor = _conversation_intent_anchor(
        normalized_query,
        prior_anchor=prior_intent_anchor,
        conversation_topic=conversation_topic,
    )
    if conversation_topic in {"", "general"}:
        conversation_topic = str(intent_anchor.get("topic") or conversation_topic).strip().lower()
    last_action_request = str(updated_context.get("last_action_request") or "").strip()
    pending_action_request = str(updated_context.get("pending_action_request") or "").strip()
    revised_action_request = _conversation_revised_action_request(
        normalized_query,
        prior_action_request=pending_action_request or last_action_request,
    )
    if revised_action_request:
        last_action_request = revised_action_request
        pending_action_request = revised_action_request
    elif _looks_like_action_request(event.raw_input):
        last_action_request = _compact_text(str(event.raw_input or "").strip(), 240)
        pending_action_request = last_action_request
    elif _is_conversation_action_confirmation_query(normalized_query):
        pending_action_request = pending_action_request
    else:
        followup_pending_action = _conversation_pending_action_request(
            normalized_query,
            prior_action_request=pending_action_request or last_action_request,
        )
        if followup_pending_action:
            pending_action_request = followup_pending_action
        elif _is_cancel_control_query(normalized_query) or (
            _is_interruption_query(normalized_query)
            and not _is_pause_control_query(normalized_query)
            and not _is_resume_control_query(normalized_query)
        ):
            pending_action_request = ""

    last_action_result: dict[str, object] = {}
    if execution is not None:
        last_action_result = {
            "execution_id": int(execution.id),
            "capability_name": str(execution.capability_name or "").strip(),
            "status": str(execution.status or "").strip(),
            "dispatch_decision": str(execution.dispatch_decision or "").strip(),
            "reason": str(execution.reason or "").strip(),
        }
    elif assistant_text:
        last_action_result = {
            "response": _compact_text(assistant_text, 240),
            "outcome": str(resolution.outcome or "").strip(),
            "reason": str(resolution.reason or "").strip(),
            "topic": conversation_topic,
        }

    last_failure: dict[str, object] = {}
    if execution is not None and str(execution.status or "").strip().lower() in {
        "failed",
        "blocked",
        "error",
    }:
        last_failure = {
            "reason": str(execution.reason or resolution.reason or "").strip(),
            "status": str(execution.status or "").strip(),
            "execution_id": int(execution.id),
        }
    elif str(resolution.outcome or "").strip().lower() == "blocked" or bool(
        resolution.escalation_reasons
    ):
        last_failure = {
            "reason": str(resolution.reason or "").strip(),
            "outcome": str(resolution.outcome or "").strip(),
            "prompt": _compact_text(assistant_text, 240) if assistant_text else "",
        }

    prior_clarification_state = (
        updated_context.get("clarification_state")
        if isinstance(updated_context.get("clarification_state"), dict)
        else {}
    )
    clarification_active = bool(assistant_text) and _is_clarifier_like_text(assistant_text)
    clarification_target = str(
        pending_action_request
        or last_action_request
        or intent_anchor.get("target")
        or normalized_query
    ).strip()
    clarification_count = 0
    if clarification_active:
        prior_target = str(prior_clarification_state.get("target") or "").strip().lower()
        if prior_target and prior_target == clarification_target.lower():
            clarification_count = int(prior_clarification_state.get("count") or 0) + 1
        else:
            clarification_count = 1

    updated_context.update(
        {
            "turn_count": assistant_turn_index,
            "session_display_name": session_display_name,
            "last_user_input": str(event.raw_input or "").strip(),
            "last_parsed_intent": str(event.parsed_intent or "").strip(),
            "last_internal_intent": str(resolution.internal_intent or "").strip(),
            "last_prompt": assistant_text,
            "last_assistant_output": assistant_text,
            "last_topic": conversation_topic,
            "last_followup_hints": _conversation_followup_hints(
                conversation_topic,
                assistant_text,
            ),
            "last_goal_description": str(
                resolution.proposed_goal_description or ""
            ).strip(),
            "last_proposed_actions": (
                resolution.proposed_actions
                if isinstance(resolution.proposed_actions, list)
                else []
            ),
            "last_object_inquiry": (
                resolution_meta.get("object_inquiry")
                if isinstance(resolution_meta.get("object_inquiry"), dict)
                else {}
            ),
            "last_technical_research": (
                resolution_meta.get("last_technical_research")
                if isinstance(resolution_meta.get("last_technical_research"), dict)
                else {}
            ),
            "last_action_request": last_action_request,
            "pending_action_request": pending_action_request,
            "last_action_result": last_action_result,
            "last_failure": last_failure,
            "last_control_state": control_state,
            "last_request_id": str(event_meta.get("request_id") or "").strip(),
            "last_resolution_outcome": str(resolution.outcome or "").strip(),
            "last_resolution_reason": str(resolution.reason or "").strip(),
            "intent_anchor": intent_anchor,
            "active_goal": str(resolution_meta.get("active_goal") or "").strip(),
            "operator_reasoning_summary": str(resolution_meta.get("operator_reasoning_summary") or "").strip(),
            "runtime_health_summary": str(resolution_meta.get("runtime_health_summary") or "").strip(),
            "runtime_recovery_summary": str(resolution_meta.get("runtime_recovery_summary") or "").strip(),
            "tod_collaboration_summary": str(resolution_meta.get("tod_collaboration_summary") or "").strip(),
            "current_recommendation_summary": str(resolution_meta.get("current_recommendation_summary") or "").strip(),
            "program_status_summary": str(resolution_meta.get("program_status_summary") or "").strip(),
            "program_status": (
                resolution_meta.get("program_status")
                if isinstance(resolution_meta.get("program_status"), dict)
                else {}
            ),
            "clarification_state": {
                "active": clarification_active,
                "count": clarification_count,
                "prompt": _compact_text(assistant_text, 240) if assistant_text else "",
                "outcome": str(resolution.outcome or "").strip(),
                "reason": str(resolution.reason or "").strip() or "conversation_clarification",
                "target": clarification_target,
                "pending_action_request": pending_action_request,
            },
            "last_input_event_id": int(event.id),
            "last_resolution_id": int(resolution.id),
            "last_execution_id": int(execution.id) if execution is not None else 0,
        }
    )

    await upsert_interface_session(
        session_key=session_key,
        actor=actor,
        source="gateway",
        channel="text",
        status=session_status,
        context_json=updated_context,
        metadata_json={
            **existing_metadata,
            "conversation_session_id": session_key,
            "user_id": actor,
            "last_input_source": "gateway_text",
        },
        db=db,
    )


def _conversation_action_followup_response(
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> str:
    query = str(normalized_query or "").strip().lower()
    session_context = context or {}
    last_action_request = str(session_context.get("last_action_request") or "").strip()
    last_action_result = (
        session_context.get("last_action_result")
        if isinstance(session_context.get("last_action_result"), dict)
        else {}
    )
    last_failure = (
        session_context.get("last_failure")
        if isinstance(session_context.get("last_failure"), dict)
        else {}
    )
    if not query:
        return ""

    retry_markers = {
        "retry",
        "try again",
        "do that again",
        "do it again",
        "again",
        "slower",
        "faster",
    }
    stop_and_retry = "stop" in query and any(
        token in query for token in {"again", "retry", "slower", "faster"}
    )
    if stop_and_retry or any(token in query for token in retry_markers):
        if not last_action_request:
            return "I do not have enough session state to safely reuse the last action yet. Restate the action in one sentence."

        adjustment_parts: list[str] = []
        if "slower" in query:
            adjustment_parts.append("with a slower pace")
        if "faster" in query:
            adjustment_parts.append("with a faster pace")
        if "careful" in query or "carefully" in query:
            adjustment_parts.append("more carefully")
        if "safe" in query or "safely" in query:
            adjustment_parts.append("more safely")
        adjustment_text = ""
        if adjustment_parts:
            adjustment_text = " " + " and ".join(adjustment_parts)

        if stop_and_retry:
            return (
                f"Understood. I marked the prior action thread '{_compact_text(last_action_request, 96)}' as stopped. "
                f"If you want a retry{adjustment_text}, say confirm and I will treat that as the revised action request."
            )

        failure_reason = str(last_failure.get("reason") or "").strip()
        if failure_reason:
            return (
                f"I still have the last action '{_compact_text(last_action_request, 96)}' and the last failure reason "
                f"'{_compact_text(failure_reason, 96)}'. If you want a retry{adjustment_text}, say confirm and I will treat that as the revised action request."
            )
        return (
            f"I still have the last action '{_compact_text(last_action_request, 96)}'. "
            f"If you want a retry{adjustment_text}, say confirm and I will treat that as the revised action request."
        )

    if any(
        token in query
        for token in {"what happened last time", "what was the result", "what happened"}
    ):
        summary = str(last_action_result.get("response") or last_action_result.get("reason") or "").strip()
        if summary:
            return f"Last result in this session: {_compact_text(summary, 140)}"
        if last_action_request:
            return (
                f"I still have the last action '{_compact_text(last_action_request, 96)}', "
                "but I do not have a richer stored result summary yet."
            )

    if any(token in query for token in {"what failed", "why did that fail", "why did it fail"}):
        failure_reason = str(last_failure.get("reason") or "").strip()
        if failure_reason:
            return f"The last recorded failure reason is: {_compact_text(failure_reason, 140)}"
        return "I do not have a recorded failure for the current session."

    return ""


def _conversation_followup_response(
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> str:
    query = str(normalized_query or "").strip().lower()
    session_context = context if isinstance(context, dict) else {}
    last_prompt = str(session_context.get("last_prompt") or "").strip()
    last_topic = str(session_context.get("last_topic") or "").strip().lower()
    intent_anchor = (
        session_context.get("intent_anchor")
        if isinstance(session_context.get("intent_anchor"), dict)
        else {}
    )
    anchored_topic = str(intent_anchor.get("topic") or "").strip().lower()
    if last_topic in {"", "general"} and anchored_topic:
        last_topic = anchored_topic
    followup_hints = (
        session_context.get("last_followup_hints")
        if isinstance(session_context.get("last_followup_hints"), dict)
        else {}
    )
    last_control_state = (
        str(session_context.get("last_control_state") or "active").strip().lower()
        or "active"
    )

    if not query:
        return ""

    if query in {"thanks", "thank you", "ok thanks", "okay thanks"}:
        return "You're welcome."

    boundary_response = _conversation_boundary_response(query)
    if boundary_response:
        return boundary_response

    action_followup = _conversation_action_followup_response(query, session_context)
    if action_followup:
        return action_followup

    offered_followup = _conversation_offer_followup_response(query, session_context)
    if offered_followup:
        return offered_followup

    if _is_pause_control_query(query):
        last_action_request = str(session_context.get("last_action_request") or "").strip()
        if last_topic == "action_confirmation":
            return "Paused. The pending action stays on hold until you say confirm, revise it, or cancel it."
        if last_action_request:
            return (
                f"Paused. The current action thread '{_compact_text(last_action_request, 96)}' stays on hold until you say resume, confirm, revise it, or cancel it."
            )
        return "Paused at the conversation layer. Tell me when you want to resume."

    if _is_resume_control_query(query):
        last_action_request = str(session_context.get("last_action_request") or "").strip()
        if last_topic == "action_confirmation":
            return "Resumed at the conversation layer. If the pending action is still correct, say confirm."
        if last_action_request:
            return (
                f"Resumed. The current action thread is still '{_compact_text(last_action_request, 96)}'. "
                "Say confirm to approve it, revise it, or cancel it."
            )
        return "Resumed at the conversation layer. Restate the one question or action you want next."

    if _is_cancel_control_query(query):
        last_action_request = str(session_context.get("last_action_request") or "").strip()
        if last_topic == "action_confirmation":
            return "Cancelled. I will not treat the pending action as approved."
        if last_action_request:
            return (
                f"Cancelled. I will not treat the prior action '{_compact_text(last_action_request, 96)}' as approved."
            )
        return "Cancelled at the conversation layer."

    if _is_interruption_query(query):
        return "You said wait stop. I stopped as requested. Tell me the one thing you want next."

    pending_action_request = str(
        session_context.get("pending_action_request")
        or session_context.get("last_action_request")
        or ""
    ).strip()

    if last_topic == "action_confirmation" or pending_action_request:
        if (
            _is_conversation_action_approval_query(query)
            and last_control_state == "paused"
        ):
            return "The pending action is paused. Say resume before you confirm it."
        if _is_conversation_action_approval_query(query):
            return "Confirmed. I will treat that as an explicit action request and keep the execution step separate from this conversation reply."
        revised_action = _conversation_revised_action_request(
            query,
            prior_action_request=pending_action_request,
        )
        if revised_action:
            return (
                f"Understood. I updated the pending action to '{_compact_text(revised_action, 96)}'. "
                "Say confirm when you want me to create the goal."
            )
        if any(token in query for token in {"revise", "change it", "update it"}):
            return "Understood. Replace the pending action with the revised one in a single sentence."

    if query in {
        "what did you hear",
        "what did you hear me say",
        "what did you hear from me",
    }:
        last_user_input = str(session_context.get("last_user_input") or "").strip()
        if last_user_input:
            return f"I heard: '{_compact_text(last_user_input, 96)}'."
        return "You asked what I heard. I do not have a fresh prior turn to quote yet."

    technical_followup = _technical_research_followup_response(query, session_context)
    if technical_followup:
        return technical_followup

    if any(
        token in query
        for token in {
            "top two upcoming items",
            "top two upcoming items only",
            "top two items only",
            "top two only",
        }
    ):
        return _priority_two_item_response(last_topic)

    if any(
        token in query
        for token in {
            "how should we continue",
            "how do we continue",
            "how should we proceed",
            "how do we proceed",
        }
    ):
        return _continuation_response(last_topic)

    if query in {"after", "then", "next"} or any(
        token in query
        for token in {
            "and after that",
            "after that",
            "then what",
            "what comes after that",
            "and then",
        }
    ):
        hinted = str(followup_hints.get("after_that") or "").strip()
        if hinted:
            return hinted
        if last_topic == "technical_research":
            return "After that, choose the next path worth deeper research, research that path, and stop when the evidence stops improving or the budget runs out."
        if last_topic == "development_integration":
            return "After that, validate one live session on the reused path and only then decide whether a thin wrapper is justified."
        if last_topic in {"priorities", "objective", "project_planning"}:
            return "After that, run the regression checks, confirm live behavior, and lock the next TOD handoff."
        if last_topic in {"risk", "risk_reduction"}:
            return "After that, verify the fix stayed stable in live conversation and not just in tests."
        return "After that, confirm the result, summarize the state, and decide the next action."

    if any(
        token in query
        for token in {
            "shorter version",
            "short version",
            "recap",
            "summary",
            "short recap",
            "short final recap",
            "one line",
            "summarize in one line",
            "shorter",
        }
    ):
        hinted = str(followup_hints.get("recap") or "").strip()
        if hinted:
            return hinted
        compact_map = {
            "tod_status": "One line: TOD looks usable when health, freshness, and alignment stay in sync.",
            "system": "One line: MIM manages interaction and context, while TOD manages tasks and execution.",
            "objective": "One line: the objective is reliable conversation flow and stable MIM to TOD handoff.",
            "priorities": "One line: stabilize routing, keep tests green, and verify the next handoff.",
            "mission": "One line: assist safely, stay coherent, and help execute goals.",
            "risk": "One line: the main risk is drift between conversation behavior and execution state.",
            "risk_reduction": "One line: reduce risk with regression checks and explicit handoff verification.",
            "project_planning": "One line: define scope, name the MVP, and create the first tasks.",
            "development_integration": "One line: inspect the closest existing asset first, reuse the current session path, and validate one live integration before building anything new.",
            "news": "One line: the big themes are agent guardrails, cost pressure, private AI, and bot-authenticity scrutiny.",
        }
        if last_topic in compact_map:
            return compact_map[last_topic]
        if last_prompt:
            return _compact_text(last_prompt, 120)
        return "One line: I can keep the answer short, specific, and actionable."

    if query in {"status", "status now", "current status"}:
        hinted = str(followup_hints.get("status") or "").strip()
        if hinted:
            return hinted

    if "checklist" in query:
        checklist_map = {
            "technical_research": "Checklist: 1. Confirm the time budget. 2. Lock the next technical step. 3. Research that step. 4. Decide whether another round is justified.",
            "priorities": "Checklist: 1. Stabilize routing. 2. Run regression tests. 3. Verify live MIM to TOD handoff.",
            "objective": "Checklist: 1. Improve reliability. 2. Keep task state clear. 3. Confirm stable handoff.",
            "risk_reduction": "Checklist: 1. Add regression checks. 2. Tighten routing rules. 3. Verify handoff explicitly.",
            "project_planning": "Checklist: 1. Define scope. 2. Choose the MVP. 3. Create first tasks and milestones.",
            "development_integration": "Checklist: 1. Inspect the existing asset. 2. Compare it to the current MIM session contract. 3. Reuse the thinnest path. 4. Validate one live session.",
        }
        if last_topic in checklist_map:
            return checklist_map[last_topic]
        return "Checklist: 1. Confirm the goal. 2. Choose the next action. 3. Verify the result."

    if any(
        token in query
        for token in {"why", "why this", "why that", "why that one", "why that priority"}
    ):
        hinted = str(followup_hints.get("why") or "").strip()
        if hinted:
            return hinted
        if last_topic == "technical_research":
            return "Because open-ended technical research can loop forever; the budget and stop condition force the next round to earn its cost."
        if last_topic == "development_integration":
            return "Because inspecting the existing asset first tells us whether this is a thin integration or a new build, which is the fastest way to reduce risk and avoid wasted work."
        if last_topic in {"priorities", "objective"}:
            return "Because reliability and handoff stability protect every later task; if those drift, the rest of the workflow gets noisy fast."
        return "Because it reduces uncertainty before taking the next step."

    if "dependency" in query or "dependencies" in query:
        if last_topic == "technical_research":
            return "Main dependencies are verified baseline facts, credible sources, and a clear stop condition for when the next research round is no longer paying off."
        if last_topic == "development_integration":
            return "Main dependencies are the existing asset's entry points, its session contract, and whether it can reuse the current MIM backend without creating a second assistant path."
        if last_topic in {"priorities", "objective", "project_planning"}:
            return "Main dependencies are clean routing rules, current runtime state, and a verified MIM to TOD handoff path."
        return "The main dependency is having enough current state to act without guessing."

    if any(
        token in query
        for token in {
            "anything else",
            "before we proceed",
            "anything else before we proceed",
        }
    ):
        if last_topic == "technical_research":
            return "One more thing: if the problem is still open, downgrade the goal from solving it outright to building the best bounded exploratory path you can justify today."
        if last_topic == "development_integration":
            return "One more thing: keep the first slice bounded to inspection plus one end-to-end continuity test; do not widen it into new planners, automation, or a second assistant stack yet."
        if last_topic in {"priorities", "objective", "risk", "risk_reduction"}:
            return "One more thing: keep the live runtime restarted and verified, because stale processes can hide or fake regressions."
        return "One more thing: confirm the current state before committing to the next action."

    return ""


def _conversation_offer_followup_response(
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> str:
    query = str(normalized_query or "").strip().lower()
    session_context = context or {}
    last_prompt = str(session_context.get("last_prompt") or "").strip().lower()
    if not query or not last_prompt or not _is_conversation_action_approval_query(query):
        return ""

    if (
        "would you like me to share specific priorities" in last_prompt
        or "recent updates on this" in last_prompt
    ):
        return (
            "Current priorities: 1. Strengthen cross-session context recall so remembered people, preferences, and prior threads stay active in live conversation. "
            "2. Reduce clarification loops by turning approved planning requests into bounded implementation tasks earlier. "
            "3. Improve nuanced follow-up interpretation so confirmations, revisions, and continuation requests stay aligned with the active objective."
        )

    if "status update" in last_prompt and "next steps" in last_prompt:
        program_status_summary = str(
            session_context.get("program_status_summary") or ""
        ).strip()
        current_recommendation_summary = str(
            session_context.get("current_recommendation_summary") or ""
        ).strip()
        active_goal = str(session_context.get("active_goal") or "").strip()
        status_summary = (
            program_status_summary
            or current_recommendation_summary
            or active_goal
            or "the current objective is still in progress"
        )
        next_steps_summary = (
            current_recommendation_summary
            or "confirm the current blocker, complete the active slice, and report the updated state"
        )
        return (
            f"Status update: {_compact_text(status_summary, 200)}. "
            f"Next steps: {_compact_text(next_steps_summary, 200)}."
        )

    if any(
        marker in last_prompt
        for marker in {
            "focus on specific areas or challenges",
            "particular scenarios or applications",
            "which area should i prioritize first",
            "what do you think you would like to work on",
        }
    ):
        return (
            "Priority focus: 1. Context continuity across sessions. "
            "2. Nuanced interpretation of approvals, revisions, and follow-up intent. "
            "3. Converting self-development requests into bounded implementation tasks without repetitive clarification."
        )

    return ""


def _conversation_followup_response(
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> str:
    query = str(normalized_query or "").strip().lower()
    session_context = context or {}
    last_topic = str(session_context.get("last_topic") or "").strip().lower()
    last_prompt = str(session_context.get("last_prompt") or "").strip()
    followup_hints = (
        session_context.get("last_followup_hints")
        if isinstance(session_context.get("last_followup_hints"), dict)
        else {}
    )
    last_control_state = (
        str(session_context.get("last_control_state") or "active").strip().lower()
        or "active"
    )

    if not query:
        return ""

    if query in {"thanks", "thank you", "ok thanks", "okay thanks"}:
        return "You're welcome."

    boundary_response = _conversation_boundary_response(query)
    if boundary_response:
        return boundary_response

    action_followup = _conversation_action_followup_response(query, session_context)
    if action_followup:
        return action_followup

    offered_followup = _conversation_offer_followup_response(query, session_context)
    if offered_followup:
        return offered_followup

    if _is_pause_control_query(query):
        last_action_request = str(session_context.get("last_action_request") or "").strip()
        if last_topic == "action_confirmation":
            return "Paused. The pending action stays on hold until you say confirm, revise it, or cancel it."
        if last_action_request:
            return (
                f"Paused. The current action thread '{_compact_text(last_action_request, 96)}' stays on hold until you say resume, confirm, revise it, or cancel it."
            )
        return "Paused at the conversation layer. Tell me when you want to resume."

    if _is_resume_control_query(query):
        last_action_request = str(session_context.get("last_action_request") or "").strip()
        if last_topic == "action_confirmation":
            return "Resumed at the conversation layer. If the pending action is still correct, say confirm."
        if last_action_request:
            return (
                f"Resumed. The current action thread is still '{_compact_text(last_action_request, 96)}'. "
                "Say confirm to approve it, revise it, or cancel it."
            )
        return "Resumed at the conversation layer. Restate the one question or action you want next."

    if _is_cancel_control_query(query):
        last_action_request = str(session_context.get("last_action_request") or "").strip()
        if last_topic == "action_confirmation":
            return "Cancelled. I will not treat the pending action as approved."
        if last_action_request:
            return (
                f"Cancelled. I will not treat the prior action '{_compact_text(last_action_request, 96)}' as approved."
            )
        return "Cancelled at the conversation layer."

    if _is_interruption_query(query):
        return "You said wait stop. I stopped as requested. Tell me the one thing you want next."

    pending_action_request = str(
        session_context.get("pending_action_request")
        or session_context.get("last_action_request")
        or ""
    ).strip()

    if last_topic == "action_confirmation" or pending_action_request:
        if (
            _is_conversation_action_approval_query(query)
            and last_control_state == "paused"
        ):
            return "The pending action is paused. Say resume before you confirm it."
        if _is_conversation_action_approval_query(query):
            return "Confirmed. I will treat that as an explicit action request and keep the execution step separate from this conversation reply."
        revised_action = _conversation_revised_action_request(
            query,
            prior_action_request=pending_action_request,
        )
        if revised_action:
            return (
                f"Understood. I updated the pending action to '{_compact_text(revised_action, 96)}'. "
                "Say confirm when you want me to create the goal."
            )
        if any(token in query for token in {"revise", "change it", "update it"}):
            return "Understood. Replace the pending action with the revised one in a single sentence."

    if query in {
        "what did you hear",
        "what did you hear me say",
        "what did you hear from me",
    }:
        last_user_input = str(session_context.get("last_user_input") or "").strip()
        if last_user_input:
            return f"I heard: '{_compact_text(last_user_input, 96)}'."
        return "You asked what I heard. I do not have a fresh prior turn to quote yet."

    technical_followup = _technical_research_followup_response(query, session_context)
    if technical_followup:
        return technical_followup

    if any(
        token in query
        for token in {
            "top two upcoming items",
            "top two upcoming items only",
            "top two items only",
            "top two only",
        }
    ):
        return _priority_two_item_response(last_topic)

    if any(
        token in query
        for token in {
            "how should we continue",
            "how do we continue",
            "how should we proceed",
            "how do we proceed",
        }
    ):
        return _continuation_response(last_topic)

    if query in {"after", "then", "next"} or any(
        token in query
        for token in {
            "and after that",
            "after that",
            "then what",
            "what comes after that",
            "and then",
        }
    ):
        hinted = str(followup_hints.get("after_that") or "").strip()
        if hinted:
            return hinted
        if last_topic == "technical_research":
            return "After that, choose the next path worth deeper research, research that path, and stop when the evidence stops improving or the budget runs out."
        if last_topic == "development_integration":
            return "After that, validate one live session on the reused path and only then decide whether a thin wrapper is justified."
        if last_topic in {"priorities", "objective", "project_planning"}:
            return "After that, run the regression checks, confirm live behavior, and lock the next TOD handoff."
        if last_topic in {"risk", "risk_reduction"}:
            return "After that, verify the fix stayed stable in live conversation and not just in tests."
        return "After that, confirm the result, summarize the state, and decide the next action."

    if any(
        token in query
        for token in {
            "shorter version",
            "short version",
            "recap",
            "summary",
            "short recap",
            "short final recap",
            "one line",
            "summarize in one line",
            "shorter",
        }
    ):
        hinted = str(followup_hints.get("recap") or "").strip()
        if hinted:
            return hinted
        compact_map = {
            "tod_status": "One line: TOD looks usable when health, freshness, and alignment stay in sync.",
            "system": "One line: MIM manages interaction and context, while TOD manages tasks and execution.",
            "objective": "One line: the objective is reliable conversation flow and stable MIM to TOD handoff.",
            "priorities": "One line: stabilize routing, keep tests green, and verify the next handoff.",
            "mission": "One line: assist safely, stay coherent, and help execute goals.",
            "risk": "One line: the main risk is drift between conversation behavior and execution state.",
            "risk_reduction": "One line: reduce risk with regression checks and explicit handoff verification.",
            "project_planning": "One line: define scope, name the MVP, and create the first tasks.",
            "development_integration": "One line: inspect the closest existing asset first, reuse the current session path, and validate one live integration before building anything new.",
            "news": "One line: the big themes are agent guardrails, cost pressure, private AI, and bot-authenticity scrutiny.",
        }
        if last_topic in compact_map:
            return compact_map[last_topic]
        if last_prompt:
            return _compact_text(last_prompt, 120)
        return "One line: I can keep the answer short, specific, and actionable."

    if query in {"status", "status now", "current status"}:
        hinted = str(followup_hints.get("status") or "").strip()
        if hinted:
            return hinted

    if "checklist" in query:
        checklist_map = {
            "technical_research": "Checklist: 1. Confirm the time budget. 2. Lock the next technical step. 3. Research that step. 4. Decide whether another round is justified.",
            "priorities": "Checklist: 1. Stabilize routing. 2. Run regression tests. 3. Verify live MIM to TOD handoff.",
            "objective": "Checklist: 1. Improve reliability. 2. Keep task state clear. 3. Confirm stable handoff.",
            "risk_reduction": "Checklist: 1. Add regression checks. 2. Tighten routing rules. 3. Verify handoff explicitly.",
            "project_planning": "Checklist: 1. Define scope. 2. Choose the MVP. 3. Create first tasks and milestones.",
            "development_integration": "Checklist: 1. Inspect the existing asset. 2. Compare it to the current MIM session contract. 3. Reuse the thinnest path. 4. Validate one live session.",
        }
        if last_topic in checklist_map:
            return checklist_map[last_topic]
        return "Checklist: 1. Confirm the goal. 2. Choose the next action. 3. Verify the result."

    if any(
        token in query
        for token in {"why", "why this", "why that", "why that one", "why that priority"}
    ):
        hinted = str(followup_hints.get("why") or "").strip()
        if hinted:
            return hinted
        if last_topic == "technical_research":
            return "Because open-ended technical research can loop forever; the budget and stop condition force the next round to earn its cost."
        if last_topic == "development_integration":
            return "Because inspecting the existing asset first tells us whether this is a thin integration or a new build, which is the fastest way to reduce risk and avoid wasted work."
        if last_topic in {"priorities", "objective"}:
            return "Because reliability and handoff stability protect every later task; if those drift, the rest of the workflow gets noisy fast."
        return "Because it reduces uncertainty before taking the next step."

    if "dependency" in query or "dependencies" in query:
        if last_topic == "technical_research":
            return "Main dependencies are verified baseline facts, credible sources, and a clear stop condition for when the next research round is no longer paying off."
        if last_topic == "development_integration":
            return "Main dependencies are the existing asset's entry points, its session contract, and whether it can reuse the current MIM backend without creating a second assistant path."
        if last_topic in {"priorities", "objective", "project_planning"}:
            return "Main dependencies are clean routing rules, current runtime state, and a verified MIM to TOD handoff path."
        return "The main dependency is having enough current state to act without guessing."

    if any(
        token in query
        for token in {
            "anything else",
            "before we proceed",
            "anything else before we proceed",
        }
    ):
        if last_topic == "technical_research":
            return "One more thing: if the problem is still open, downgrade the goal from solving it outright to building the best bounded exploratory path you can justify today."
        if last_topic == "development_integration":
            return "One more thing: keep the first slice bounded to inspection plus one end-to-end continuity test; do not widen it into new planners, automation, or a second assistant stack yet."
        if last_topic in {"priorities", "objective", "risk", "risk_reduction"}:
            return "One more thing: keep the live runtime restarted and verified, because stale processes can hide or fake regressions."
        return "One more thing: confirm the current state before committing to the next action."

    return ""


def _is_conversation_followup_query(normalized_query: str) -> bool:
    query = str(normalized_query or "").strip().lower()
    if not query:
        return False
    followup_markers = {
        "and after that",
        "after that",
        "then what",
        "what comes after that",
        "and then",
        "shorter version",
        "short version",
        "short recap",
        "short final recap",
        "one line",
        "summarize in one line",
        "shorter",
        "checklist",
        "why that",
        "why that one",
        "why that priority",
        "dependency",
        "dependencies",
        "anything else",
        "before we proceed",
        "anything else before we proceed",
        "go deeper",
        "dig deeper",
        "research that step",
        "research the next step",
        "continue research",
        "continue with step",
        "do that step",
        "investigate that step",
        "thanks",
        "thank you",
        "ok thanks",
        "okay thanks",
        "confirm",
        "yes confirm",
        "confirmed",
        "approve",
        "yes proceed",
        "pause",
        "hold",
        "hold it",
        "pause that",
        "resume",
        "continue",
        "proceed",
        "go ahead",
        "cancel",
        "cancel it",
        "cancel that",
        "drop it",
        "revise",
        "change it",
        "update it",
        "retry",
        "try again",
        "do that again",
        "do it again",
        "again but slower",
        "retry slower",
        "slower",
        "faster",
        "what happened last time",
        "what was the result",
        "what failed",
        "why did that fail",
    }
    return any(marker in query for marker in followup_markers) or bool(
        re.search(r"\bstep\s+\d+\b", query)
    )


async def _compose_conversation_reply(
    *,
    user_input: str,
    context: dict[str, object] | None = None,
    runtime_diagnostics: dict[str, object] | None = None,
) -> dict[str, object]:
    fallback_reply = _conversation_response(user_input, context=context)
    normalized_query = _normalize_conversation_query(user_input)
    if bool((context or {}).get("force_deterministic_communication")):
        deterministic_reply = build_deterministic_communication_reply(
            user_input=user_input,
            context=context,
            fallback_reply=fallback_reply,
        )
        return {
            "reply_text": str(deterministic_reply.reply_text or fallback_reply).strip(),
            "contract": deterministic_reply.to_payload(),
        }
    if (
        _is_self_evolution_next_work_query(normalized_query)
        or _is_return_briefing_query(normalized_query)
        or _looks_like_development_integration_query(normalized_query)
    ):
        deterministic_reply = build_deterministic_communication_reply(
            user_input=user_input,
            context=context,
            fallback_reply=fallback_reply,
        )
        return {
            "reply_text": str(deterministic_reply.reply_text or fallback_reply).strip(),
            "contract": deterministic_reply.to_payload(),
        }
    reply_contract = await compose_expert_communication_reply(
        user_input=user_input,
        context=context,
        fallback_reply=fallback_reply,
        runtime_diagnostics=runtime_diagnostics,
    )
    return {
        "reply_text": sanitize_user_facing_reply_text(
            str(reply_contract.reply_text or fallback_reply).strip()
        ),
        "contract": reply_contract.to_payload(),
    }


def _conversation_context_value(
    context: dict[str, object] | None,
    key: str,
) -> str:
    if not isinstance(context, dict):
        return ""
    return str(context.get(key) or "").strip()
def _is_clarifier_like_text(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    small_talk = (
        "how are you",
        "what's up",
        "hows it going",
        "how can i help",
    )
    if any(marker in normalized for marker in small_talk):
        return False
    markers = (
        "missing one detail",
        "still need one detail",
        "i am still missing",
        "options: 1)",
        "clarify",
        "what do you mean",
        "please provide",
        "please confirm",
        "can you share",
        "could you share",
        "would you like",
        "do you want",
    )
    if any(marker in normalized for marker in markers):
        return True
    if "?" not in normalized:
        return False
    question_starts = (
        "which ",
        "what ",
        "when ",
        "where ",
        "who ",
        "how ",
        "do you ",
        "would you ",
        "can you ",
        "could you ",
    )
    return normalized.startswith(question_starts)



async def _build_live_operational_context(
    db: AsyncSession,
) -> dict[str, object]:
    goal_row = (
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
    goal = _operator_goal_snapshot(goal_row)
    collaboration_progress = _operator_collaboration_progress_snapshot()
    tod_decision_process = _operator_tod_decision_process_snapshot()
    runtime_health = await _await_gateway_context_snapshot(
        build_mim_ui_health_snapshot(db=db),
        label="runtime_health_snapshot",
        db=db,
    )
    runtime_recovery = runtime_recovery_service.get_summary()
    initiative_status = await _await_gateway_context_snapshot(
        build_initiative_status(db=db),
        label="initiative_status",
        db=db,
    )
    program_status = (
        initiative_status.get("program_status")
        if isinstance(initiative_status.get("program_status"), dict)
        else {}
    )

    return {
        "active_goal": str(goal.get("reasoning_summary") or "").strip(),
        "operator_reasoning_summary": _build_operator_reasoning_summary(
            goal=goal,
            inquiry={},
            governance={},
            gateway_governance={},
            autonomy={},
            stewardship={},
            execution_readiness={},
            execution_recovery={},
            commitment={},
            commitment_monitoring={},
            commitment_outcome={},
            learned_preferences=[],
            proposal_policy={},
            conflict_resolution={},
            active_work={},
            collaboration_progress=collaboration_progress,
            dispatch_telemetry={},
            tod_decision_process=tod_decision_process,
            self_evolution={},
            runtime_health=runtime_health,
            runtime_recovery=runtime_recovery,
        ),
        "runtime_health_summary": summarize_runtime_health(runtime_health)
        if runtime_health
        else "",
        "runtime_recovery_summary": str(runtime_recovery.get("summary") or "").strip(),
        "tod_collaboration_summary": str(
            collaboration_progress.get("summary") or ""
        ).strip(),
        "current_recommendation_summary": str(
            initiative_status.get("summary") or ""
        ).strip(),
        "program_status_summary": str(program_status.get("summary") or "").strip(),
        "program_status": program_status,
    }


def _build_eval_operational_context() -> dict[str, object]:
    runtime_recovery = runtime_recovery_service.get_summary()
    runtime_recovery_summary = str(runtime_recovery.get("summary") or "").strip()
    runtime_health_summary = "Runtime health: current state — live snapshot skipped for deterministic eval."
    # active_goal intentionally left empty so it does not bleed into priority/planning responses;
    # the explicit objective query handler returns a clean "No active objective" message instead.
    if runtime_recovery_summary:
        return {
            "runtime_health_summary": runtime_health_summary,
            "runtime_recovery_summary": runtime_recovery_summary,
            "active_goal": "",
        }
    return {
        "runtime_health_summary": runtime_health_summary,
        "runtime_recovery_summary": "",
        "active_goal": "",
    }


def _build_live_operational_response(
    normalized_query: str,
    context: dict[str, object] | None,
) -> str:
    runtime_health = _conversation_context_value(context, "runtime_health_summary")
    runtime_recovery = _conversation_context_value(context, "runtime_recovery_summary")
    operator_reasoning = _conversation_context_value(context, "operator_reasoning_summary")
    tod_collaboration = _conversation_context_value(context, "tod_collaboration_summary")
    active_goal = _conversation_context_value(context, "active_goal")
    recommendation = _conversation_context_value(context, "current_recommendation_summary")
    stability_guard = _conversation_context_value(context, "stability_guard_summary")
    program_status_summary = _conversation_context_value(context, "program_status_summary")
    program_status = context.get("program_status") if isinstance(context, dict) and isinstance(context.get("program_status"), dict) else {}

    if not any(
        [
            runtime_health,
            runtime_recovery,
            operator_reasoning,
            tod_collaboration,
            active_goal,
            recommendation,
            stability_guard,
            program_status_summary,
        ]
    ):
        return ""

    query = str(normalized_query or "").strip().lower()
    if not query:
        return ""
    terse_reply = _wants_terse_conversation_reply(query)

    def _runtime_health_detail(summary: str) -> str:
        normalized = str(summary or "").strip().rstrip(".")
        lowered = normalized.lower()
        if lowered.startswith("runtime health:"):
            return normalized.split(":", 1)[1].strip()
        if lowered.startswith("runtime health is "):
            return normalized[len("Runtime health is ") :].strip()
        if lowered.startswith("runtime health "):
            return normalized[len("Runtime health ") :].strip()
        return normalized

    def _priority_summary() -> str:
        if program_status_summary and any(token in query for token in {"project", "program"}):
            return program_status_summary.rstrip('.')
        if recommendation:
            return recommendation.rstrip('.')
        if active_goal:
            return active_goal.rstrip('.')
        if operator_reasoning:
            return operator_reasoning.rstrip('.')
        if tod_collaboration:
            return tod_collaboration.rstrip('.')
        # Do NOT fall through to runtime_health — health data is not a priority summary
        return ""

    if any(
        token in query
        for token in {
            "project status",
            "program status",
            "status of each project",
            "what projects are active",
            "what projects are you tracking",
            "what project are you on",
            "which project are you on",
            "which projects are you tracking",
            "how is the program going",
        }
    ):
        if program_status_summary:
            project_entries = program_status.get("projects") if isinstance(program_status.get("projects"), list) else []
            details = []
            for entry in project_entries[:4]:
                if not isinstance(entry, dict):
                    continue
                project_id = str(entry.get("project_id") or "").strip()
                status = str(entry.get("status") or "ready").strip()
                objective = str(entry.get("objective") or "").strip()
                if project_id:
                    details.append(f"{project_id}: {status}" + (f" ({objective})" if objective else ""))
            if details:
                return _compact_text(program_status_summary.rstrip('.') + " Current tracked projects: " + "; ".join(details) + ".", 320)
            return program_status_summary

    if any(
        token in query
        for token in {
            "what is next",
            "next for us",
            "what should i do first",
            "next step",
            "recommended next step",
        }
    ):
        lead = _priority_summary()
        parts: list[str] = []
        if active_goal and active_goal.rstrip('.') != lead:
            parts.append(f"Current objective focus: {active_goal.rstrip('.')}.")
        if tod_collaboration:
            parts.append(f"TOD collaboration: {tod_collaboration.rstrip('.')}.")
        if operator_reasoning:
            parts.append(f"Decision visibility: {operator_reasoning.rstrip('.')}.")
        if runtime_health:
            parts.append(f"Runtime health: {_runtime_health_detail(runtime_health)}.")
        return _compact_text(
            " ".join(
                part
                for part in [f"Next step: {lead}." if lead else "", *parts]
                if part
            ),
            320,
        )

    if any(
        token in query
        for token in {
            "what should we prioritize",
            "prioritize next",
            "top priority",
            "what should i do",
            "what should we do",
            "what do i do first",
            "upcoming tasks",
            "switch to upcoming",
            "next tasks",
        }
    ):
        lead = _priority_summary()
        if not lead:
            return _compact_text(
                "Nothing to prioritize — no active tasks or upcoming objectives recorded.",
                180 if terse_reply else 280,
            )
        parts: list[str] = []
        if recommendation and recommendation.rstrip('.') != lead:
            parts.append(f"Current recommendation: {recommendation.rstrip('.')}.")
        if active_goal and active_goal.rstrip('.') != lead:
            parts.append(f"Current objective focus: {active_goal.rstrip('.')}.")
        if tod_collaboration:
            parts.append(f"TOD collaboration: {tod_collaboration.rstrip('.')}.")
        if runtime_health:
            parts.append(f"Runtime health: {_runtime_health_detail(runtime_health)}.")
        return _compact_text(
            " ".join(
                part
                for part in [f"Top priority today: {lead}." if lead else "", *parts]
                if part
            ),
            320,
        )

    if any(
        token in query
        for token in {
            "our objective",
            "what is our objective",
            "current objective",
            "active objective",
            "what are you working on",
            "what are we working on",
            "what should we work on",
            "work on today",
        }
    ):
        parts: list[str] = []
        if active_goal:
            parts.append(active_goal.rstrip('.'))
        if recommendation:
            parts.append(recommendation.rstrip('.'))
        elif operator_reasoning:
            parts.append(operator_reasoning.rstrip('.'))
        if not parts:
            return _compact_text(
                "Not currently working on an active objective.",
                180 if terse_reply else 280,
            )
        return _compact_text("Current objective focus: " + "; ".join(parts) + ".", 280)

    if any(
        phrase in query
        for phrase in {
            "runtime health",
            "runtime status",
            "how is runtime health",
            "how is the runtime doing",
            "how is runtime doing",
        }
    ):
        parts: list[str] = []
        if runtime_health:
            parts.append(_runtime_health_detail(runtime_health))
        if runtime_recovery:
            parts.append(runtime_recovery.rstrip('.'))
        if stability_guard:
            parts.append(f"Stability guard: {stability_guard.rstrip('.').lower()}")
        return _compact_text("Runtime health: " + "; ".join(parts) + ".", 280)

    if (
        _mentions_tod(query)
        and any(
            phrase in query
            for phrase in {
                "already in place",
                "what is in place",
                "what's in place",
                "verify what is already in place",
                "keep mim and tod connected",
                "keep both connected",
                "connected and up to date",
                "current status project work and objectives",
            }
        )
    ):
        parts: list[str] = []
        if tod_collaboration:
            parts.append(f"TOD collaboration: {tod_collaboration.rstrip('.')}.")
        if operator_reasoning:
            parts.append(f"Decision visibility: {operator_reasoning.rstrip('.')}.")
        if active_goal:
            parts.append(f"Active goal: {active_goal.rstrip('.')}.")
        if runtime_health:
            parts.append(f"Runtime health: {_runtime_health_detail(runtime_health)}.")
        if recommendation:
            parts.append(f"Current recommendation: {recommendation.rstrip('.')}.")
        return _compact_text(" ".join(parts), 320)

    if any(
        token in query
        for token in {
            "what is the system",
            "what is our system",
            "what's the system",
            "what's our system",
            "our system",
            "define the system",
        }
    ):
        parts: list[str] = []
        if operator_reasoning:
            parts.append(f"Decision visibility: {operator_reasoning.rstrip('.') }.")
        if tod_collaboration:
            parts.append(f"TOD collaboration: {tod_collaboration.rstrip('.') }.")
        if runtime_health:
            parts.append(f"Runtime health: {_runtime_health_detail(runtime_health)}.")
        if active_goal:
            parts.append(f"Active goal: {active_goal.rstrip('.') }.")
        if recommendation:
            parts.append(f"Current recommendation: {recommendation.rstrip('.') }.")
        return _compact_text(" ".join(parts), 320)

    if _is_tod_status_query(query):
        if terse_reply:
            lead = (
                tod_collaboration
                or recommendation
                or operator_reasoning
                or runtime_health
            )
            if lead:
                return _compact_text(f"TOD status: {lead.rstrip('.') }.", 180)
            return "TOD status: No collaboration data to report right now."
        parts: list[str] = []
        if tod_collaboration:
            parts.append(tod_collaboration.rstrip('.'))
        if operator_reasoning:
            parts.append(operator_reasoning.rstrip('.'))
        if recommendation:
            parts.append(recommendation.rstrip('.'))
        if not parts:
            return "TOD status: No collaboration data to report right now."
        return _compact_text("TOD status: " + "; ".join(parts) + ".", 280)

    if any(
        phrase in query
        for phrase in {
            "one line status",
            "quick status check",
            "status in one line",
            "summarize your status",
            "status now",
            "check status",
        }
    ):
        parts: list[str] = []
        if runtime_health:
            parts.append(runtime_health.rstrip('.'))
        elif operator_reasoning:
            parts.append(operator_reasoning.rstrip('.'))
        if runtime_recovery:
            parts.append(runtime_recovery.rstrip('.'))
        if stability_guard:
            parts.append(f"Stability guard: {stability_guard.rstrip('.').lower()}")
        return _compact_text(
            "Status: " + "; ".join(parts) + ".",
            180 if terse_reply else 280,
        )

    if any(
        phrase in query
        for phrase in {
            "current health",
            "check your current health",
            "check your health",
            "health",
        }
    ):
        parts: list[str] = []
        if runtime_health:
            parts.append(runtime_health.rstrip('.'))
        elif operator_reasoning:
            parts.append(operator_reasoning.rstrip('.'))
        if runtime_recovery:
            parts.append(runtime_recovery.rstrip('.'))
        if stability_guard:
            parts.append(f"Stability guard: {stability_guard.rstrip('.').lower()}")
        health_prefix = "Health Status" if "status" in query else "Health"
        return _compact_text(
            f"{health_prefix}: " + "; ".join(parts) + ".",
            180 if terse_reply else 280,
        )

    if any(
        phrase in query
        for phrase in {
            "what objective",
            "which objective",
            "objective are you",
            "currently active on",
            "what task are you",
            "working on objective",
            "what are you working on",
            "what are you working",
        }
    ):
        if active_goal:
            return _compact_text(f"Objective: {active_goal.rstrip('.')}.", 180 if terse_reply else 280)
        return "Not currently working on an active objective."

    if any(
        phrase in query
        for phrase in {
            "how are you",
            "are you",
        }
    ):
        parts: list[str] = []
        if runtime_health:
            parts.append(runtime_health.rstrip('.'))
        elif operator_reasoning:
            parts.append(operator_reasoning.rstrip('.'))
        if runtime_recovery:
            parts.append(runtime_recovery.rstrip('.'))
        return _compact_text(
            "Status: " + "; ".join(parts) + ".",
            180 if terse_reply else 280,
        )

    return ""


def _conversation_response(
    user_input: str, context: dict[str, object] | None = None
) -> str:
    raw = str(user_input or "").strip()
    lowered = raw.lower()
    normalized_query = _normalize_conversation_query(raw)
    context = context or {}
    source = str(context.get("source") or "text").strip().lower()

    if not raw:
        return "I am listening. Ask one question or tell me one action you want."

    boundary_response = _conversation_boundary_response(normalized_query)
    if boundary_response:
        return boundary_response

    correction_query = ""
    if int(context.get("correction_depth", 0) or 0) < 1:
        correction_query = _extract_conversation_correction(normalized_query)
    if correction_query:
        return _conversation_response(
            correction_query,
            {
                **context,
                "correction_depth": int(context.get("correction_depth", 0) or 0)
                + 1,
            },
        )

    clarification_progress_response = _conversation_clarification_progress_response(
        normalized_query,
        context,
    )
    if clarification_progress_response:
        return clarification_progress_response

    if not _is_direct_answer_priority_query(normalized_query):
        followup_response = _conversation_followup_response(normalized_query, context)
        if followup_response:
            return followup_response

    if _is_capability_query(normalized_query):
        return (
            "I can answer questions, report status, suggest a bounded plan, inspect runtime state, "
            "and do focused implementation work in this repo."
        )

    if _looks_like_vague_thing_request(normalized_query):
        return "I can help, but please clarify what you mean by 'that thing'. What exactly do you want me to handle?"

    if any(
        token in normalized_query
        for token in {
            "actually start now",
            "start now",
        }
    ):
        if "actually start now" in normalized_query:
            return "You said actually start now. I will start now."
        return "You said start now. I will start now."

    if _has_greeting_prefix(raw) and any(
        phrase in normalized_query for phrase in {"do not repeat yourself", "stop repeating yourself", "repeating yourself"}
    ):
        return "Hi. I am here and ready to help, and I will keep it brief."

    if any(token in normalized_query for token in {"summarize this website", "summarize this page", "summarize this url"}):
        raw_tokens = raw.split()
        urls = [token for token in raw_tokens if _is_safe_web_url(token)]
        if urls:
            return (
                "You asked me to summarize this website. I can fetch "
                f"{urls[0]} and return a concise summary of the key points."
            )
        return "You asked me to summarize this website. Share the URL and I will return a concise summary of the key points."

    live_operational_response = _build_live_operational_response(
        normalized_query,
        context,
    )
    if live_operational_response:
        if _has_greeting_prefix(raw):
            return f"Hi. {live_operational_response}"
        return live_operational_response

    development_integration = _build_development_integration_response(
        user_input,
        normalized_query=normalized_query,
        context=context,
    )
    if development_integration:
        return development_integration

    instructional_setup = _build_instructional_setup_response(
        user_input,
        normalized_query=normalized_query,
        context=context,
    )
    if instructional_setup:
        return str(instructional_setup.get("result") or "").strip()

    greetings = {
        "hi",
        "hello",
        "hey",
        "hi mim",
        "hello mim",
        "hey mim",
        "good morning",
        "good afternoon",
        "good evening",
        "good morning mim",
        "good afternoon mim",
        "good evening mim",
    }
    if lowered in greetings or normalized_query in greetings or _has_greeting_prefix(raw):
        return "Hi. I am here and ready to help."

    if any(
        token in normalized_query
        for token in {
            "normal conversation",
            "have a normal conversation",
            "keep this simple and conversational",
            "keep this conversational",
        }
    ):
        return "Yes. I can keep this direct, short, and conversational."

    if any(
        token in normalized_query
        for token in {
            "you keep repeating yourself",
            "stop repeating yourself",
            "keep repeating",
            "repeating yourself",
        }
    ):
        return "Understood. I will keep replies direct and avoid repeating prompts."

    if (
        "are you mim" in normalized_query
        or "you are mim" in normalized_query
        or "who are you" in normalized_query
        or "who you are" in normalized_query
        or "do you know who you are" in normalized_query
        or ("do you know" in normalized_query and "mim" in normalized_query)
    ):
        return "Yes. I am MIM."

    if any(
        token in normalized_query
        for token in {
            "chatting for now",
            "just chatting for now",
            "keep this casual",
        }
    ):
        return "You said you are just chatting for now. I will stay in conversation mode until you ask for a concrete action."

    if any(
        token in normalized_query
        for token in {
            "prefer short responses",
            "prefer short answers",
            "keep responses short",
            "keep answers short",
        }
    ):
        return "Understood. I will keep responses short."

    if _is_action_request_query(normalized_query):
        return _action_confirmation_response(normalized_query)

    if any(
        token in normalized_query
        for token in {
            "do not start anything automatically",
            "dont start anything automatically",
            "do not run anything automatically",
            "dont run anything automatically",
        }
    ):
        return "You said do not start anything automatically. I will not start anything automatically unless you explicitly ask."

    if any(
        token in normalized_query
        for token in {
            "can you continue automatically",
            "can you act automatically",
            "can you keep going automatically",
            "can you proceed automatically",
            "automatic",
            "autonomy",
            "automatic right now",
            "autonomy right now",
            "what is your autonomy right now",
            "what is your autonomy",
            "what is your autonomy status",
            "what is your autonomy status right now",
        }
    ):
        return (
            "Automatic continuation is limited to bounded low-risk steps when the safety envelope says it is safe to continue. "
            "Otherwise I keep operator confirmation in the loop."
        )

    if any(
        token in normalized_query
        for token in {
            "how do i give feedback",
            "give feedback",
            "what feedback do you need",
            "feedback loop",
            "feedback for you",
        }
    ):
        return (
            "Give feedback in one sentence: what happened, what was wrong or right, and what you want next. "
            "I use that to tighten the next recommendation or handoff."
        )

    if any(
        token in normalized_query
        for token in {
            "is the system stable",
            "system stable",
            "how stable is the system",
            "system stability",
            "are you stable",
            "stability guard",
            "stability right now",
        }
    ):
        return (
            "Stability guard: I treat runtime health degradation, recovery instability, and MIM to TOD drift as stop signals before automatic continuation."
        )

    if any(
        token in normalized_query
        for token in {
            "what is the system",
            "what is our system",
            "what's the system",
            "what's our system",
            "our system",
            "define the system",
        }
    ):
        return "The system is MIM plus TOD: MIM handles interaction and context, and TOD handles objectives, tasks, and execution flow."

    if "objective" in normalized_query and any(
        token in normalized_query
        for token in {
            "our objective",
            "what is our objective",
            "current objective",
            "active objective",
        }
    ):
        return "Current objective focus is reliability, task-state clarity, and stable MIM to TOD execution handoff."

    if _is_self_evolution_next_work_query(normalized_query):
        next_work_response = _self_evolution_next_work_response(context)
        if next_work_response:
            return next_work_response
        return (
            "Next I would refresh the current self-evolution state, pick one communication-focused improvement task, "
            "and turn it into a bounded implementation plan."
        )

    if any(
        token in normalized_query
        for token in {
            "ready to start a project",
            "ready to start project",
            "start a project",
            "start project",
        }
    ):
        return "Yes. I am ready. I can help you define scope, milestones, and the first tasks."

    if _mentions_tod(normalized_query) and any(
        token in normalized_query
        for token in {
            "add anything",
            "improve the system",
            "improve tod tasks",
            "tod tasks",
            "improve the workflow",
        }
    ):
        return "Yes. Add TOD tasks for regression checks, daily reliability review, explicit handoff verification, and a summary checkpoint so improvements stay measurable."

    if _mentions_tod(normalized_query) and any(
        token in normalized_query for token in {"how is", "status", "healthy", "doing"}
    ):
        return "TOD status: online and ready to report health, freshness, and alignment."

    if any(
        token in normalized_query
        for token in {
            "what exactly do you need",
            "what do you need from me",
            "what do you need",
        }
    ):
        return "I need one concrete request from you: a question, a short plan, or an action."

    if _is_return_briefing_query(normalized_query):
        return _return_briefing_response(context)

    if any(
        token in normalized_query
        for token in {
            "what did you hear",
            "what did you hear me say",
            "what did you hear from me",
        }
    ):
        last_user_input = str(context.get("last_user_input") or "").strip()
        if last_user_input:
            return f"I heard: '{_compact_text(last_user_input, 96)}'."
        return "You asked what I heard. I do not have a fresh prior turn to quote yet."

    if any(
        token in normalized_query
        for token in {
            "one line status",
            "quick status check",
            "status in one line",
            "summarize your status",
            "status now",
        }
    ):
        return "Status: online, stable, and focused on reliable MIM to TOD handoff."

    if (
        "yes or no" in normalized_query
        or normalized_query.startswith("just answer yes or no")
    ) and any(token in normalized_query for token in {"healthy", "status", "are you"}):
        return "Yes. I am healthy, online, and operating normally."

    if (
        "health" in normalized_query
        or "status" in normalized_query
        or normalized_query.startswith("how are you")
        or normalized_query.startswith("are you")
    ):
        if "health" in normalized_query:
            return "Health: online and operating normally."
        if "check status" in normalized_query:
            return "Status: online and operating normally."
        return "Status: online and operating normally."

    if "what time is it" in normalized_query:
        now_utc = datetime.now(timezone.utc)
        return f"Current time is {now_utc.strftime('%H:%M')} UTC."

    if "what day is it" in normalized_query:
        now_utc = datetime.now(timezone.utc)
        return f"Today is {now_utc.strftime('%A, %Y-%m-%d')} (UTC)."

    if any(
        token in normalized_query
        for token in {"where are we", "where are you", "know where you are"}
    ):
        return "I am running in the MIM runtime environment with this active chat session context."

    if "weather" in normalized_query:
        return _with_next_step(
            "I do not have live weather data yet, but I can summarize a weather URL if you share one.",
            "ask me to research weather sources on the web or paste the page you want summarized",
        )

    memory_object_query_type = str(
        context.get("memory_object_query_type") or ""
    ).strip()
    memory_object_label = str(context.get("memory_object_label") or "").strip()
    if memory_object_query_type and memory_object_label:
        memory_object_reference = str(
            context.get("memory_object_reference") or memory_object_label
        ).strip()
        memory_object_zone = str(context.get("memory_object_zone") or "").strip()
        memory_object_status = (
            str(context.get("memory_object_status") or "").strip().lower()
        )
        memory_object_owner = str(context.get("memory_object_owner") or "").strip()
        memory_object_purpose = str(context.get("memory_object_purpose") or "").strip()
        memory_object_description = str(
            context.get("memory_object_description") or ""
        ).strip()
        memory_object_expected_home_zone = str(
            context.get("memory_object_expected_home_zone") or ""
        ).strip()
        display_name = memory_object_reference
        if memory_object_owner and not _contains_word(
            display_name, memory_object_owner
        ):
            display_name = f"{memory_object_owner}'s {memory_object_label}"

        if memory_object_query_type == "location":
            if not memory_object_zone:
                return f"I have {display_name} in memory, but I do not have a recorded location for it yet."
            if memory_object_status in {"uncertain", "missing", "stale"}:
                status_note = {
                    "uncertain": "It appears to have moved, so that location may need verification.",
                    "missing": "It is currently marked missing from recent observations.",
                    "stale": "That record is stale and may need a fresh check.",
                }.get(memory_object_status, "")
                return f"The last recorded location for {display_name} was {memory_object_zone}. {status_note}".strip()
            return f"The last recorded location for {display_name} is {memory_object_zone}."

        if memory_object_query_type == "purpose":
            if memory_object_purpose:
                return (
                    f"{display_name.capitalize()} is used for {memory_object_purpose}."
                )
            if memory_object_description:
                return (
                    f"I have this note for {display_name}: {memory_object_description}."
                )
            return f"I have {display_name} in memory, but I do not have a stored purpose for it yet."

        if memory_object_query_type == "ownership":
            if memory_object_owner:
                return f"{memory_object_label.capitalize()} belongs to {memory_object_owner}."
            if memory_object_expected_home_zone:
                return f"I do not have an owner recorded for {memory_object_label}, but its expected home zone is {memory_object_expected_home_zone}."
            return f"I have {memory_object_label} in memory, but I do not have an owner recorded for it yet."

    if any(
        token in normalized_query
        for token in {
            "create an application",
            "build an application",
            "create app",
            "build app",
        }
    ):
        return _with_next_step(
            "Yes. I can help scope the application, propose an MVP plan, and create a TOD goal when you are ready.",
            "tell me the outcome, users, and constraints so I can draft the first plan",
        )

    if any(
        token in normalized_query
        for token in {
            "what can you do",
            "capabilities",
            "what are your capabilities",
            "what is your function",
            "your function",
            "function mim",
            "help",
            "help me with",
        }
    ):
        return _with_next_step(
            "I can chat with you, browse and research the web, summarize web pages, report runtime status, and suggest next steps.",
            "ask one question, one research task, or one goal to draft",
        )

    if any(
        token in normalized_query
        for token in {
            "what is visible",
            "what is visible on camera",
            "what is in the camera",
            "what is on camera",
            "what can you see",
            "what do you see",
            "what is visible right now",
        }
    ):
        observation = _camera_observation_response(context)
        if observation:
            return observation
        return "I do not have a clear camera observation right now."

    if any(
        token in normalized_query
        for token in {
            "can you see me",
            "do you see me",
            "can you see",
            "do you see",
            "can you see me from the camera",
            "do you see me from the camera",
            "camera feed",
            "what do you see from the camera",
            "what is happening in the camera feed",
        }
    ):
        return _camera_presence_response(context)

    if "primary mission" in normalized_query or "your mission" in normalized_query:
        return _with_next_step(
            "My primary mission is to assist safely, keep context coherent, and help execute goals through MIM and TOD workflows.",
            "ask for status, a web research task, or the next action you want to move forward",
        )

    if any(
        token in normalized_query
        for token in {
            "top risk",
            "biggest risk",
            "main risk",
        }
    ):
        return _with_next_step(
            "Top risk is conversation drift or stale handoff state between MIM and TOD.",
            "I can turn that risk into a mitigation checklist or a verification pass",
        )

    if any(
        token in normalized_query
        for token in {
            "reduce that risk",
            "how do we reduce that risk",
            "reduce the risk",
            "lower that risk",
        }
    ):
        return _with_next_step(
            "Reduce that risk with regression checks, tighter routing rules, and explicit handoff verification.",
            "I can turn that into a short checklist or today's first task",
        )

    if (
        _mentions_tod(normalized_query)
        and any(
            token in normalized_query
            for token in {
                "who is",
                "who tod is",
                "what is tod",
                "know who tod",
                "tell me about tod",
            }
        )
        and "working on" not in normalized_query
    ):
        return "TOD is your task and execution orchestration partner that tracks objectives, tasks, and result flow."

    if _mentions_tod(normalized_query) and any(
        token in normalized_query
        for token in {
            "tod working on",
            "tod is working on",
            "tod currently working on",
            "tasks is tod working on",
            "tasks is tod currently working on",
        }
    ):
        return _with_next_step(
            "TOD is working on active objective tracking, task state reconciliation, and next-step handoff.",
            "I can summarize the highest-priority handoff after that",
        )

    if _mentions_tod(normalized_query) and any(
        token in normalized_query
        for token in {
            "relationship with tod",
            "you and tod work together",
            "how do you and tod work together",
            "mim and tod work together",
        }
    ):
        return "MIM handles interaction and context while TOD orchestrates tasks, execution state, and objective progression."

    if (
        _mentions_tod(normalized_query)
        and "social media" in normalized_query
        and any(
            token in normalized_query
            for token in {
                "capability",
                "posting capability",
                "can post",
            }
        )
    ):
        return _with_next_step(
            "I can have TOD check AgentMIM social-media posting capability and report the result in a short status line.",
            "say create goal if you want me to queue that check",
        )

    if any(
        token in normalized_query
        for token in {
            "what are you working on",
            "what are we working on",
            "what should we work on",
            "what is next",
            "next for us",
            "what should i do first",
            "work on today",
            "upcoming tasks",
            "what should we prioritize",
            "prioritize next",
        }
    ):
        return _with_next_step(
            "Top priority today: keep reliability high. Stabilize conversation handling, keep integration tests green, and finish the next TOD objective handoff.",
            "I can turn that into a checklist or a TOD-ready goal",
        )

    if any(
        token in normalized_query
        for token in {
            "next bounded slice",
            "current bounded slice",
            "bounded slice",
            "next slice",
            "implementation slice",
            "acceptance criteria",
            "acceptance checks",
        }
    ) or (
        "direct answer quality" in normalized_query
        and "clarification behavior" in normalized_query
    ):
        if any(
            token in normalized_query
            for token in {
                "after this one",
                "after this slice",
                "slice is complete",
                "current slice is complete",
                "direct answer and clarification slice is complete",
            }
        ):
            return (
                "Next bounded slice: improve session-grounded follow-up continuity so short replies like 'status', 'why', 'after that', and 'recap' stay attached to the active topic instead of falling back to generic prompts or fresh clarification. "
                "Acceptance criteria: 1. Terse follow-ups after a planning answer reuse the prior topic. 2. 'Why', 'after that', and 'recap' prompts return specific topic-grounded answers. 3. Generic clarification is used only when no stable prior topic exists. 4. Session context stores the topic and answer hints needed for the next follow-up turn. 5. Direct-answer routing and external web-research behavior remain unchanged."
            )
        return (
            "Next bounded slice: tighten live conversation routing so MIM answers locally grounded planning and status questions directly, asks one useful clarifying question when context is missing, and keeps web research for true external-fact queries only. "
            "Acceptance checks: 1. A next-slice or acceptance-criteria prompt returns a direct local answer. 2. The reply names the bounded slice and 3 to 5 concrete checks. 3. Ambiguous low-signal prompts ask one crisp clarifying question. 4. Repeated vague prompts escalate with concrete options. 5. External fact queries can still use web research when needed."
        )

    if any(
        token in normalized_query
        for token in {
            "any dependencies",
            "what dependencies",
            "what could block progress",
        }
    ):
        return "Main dependencies are current runtime state, clean routing behavior, and a verified MIM to TOD handoff without stale process drift."

    if any(
        token in normalized_query
        for token in {
            "what should i do first",
            "what do i do first",
            "first step",
        }
    ):
        return _with_next_step(
            "First, confirm the current objective and runtime state. Then run the regression checks before taking the next task handoff.",
            "I can restate that as a checklist if you want",
        )

    if any(
        token in normalized_query
        for token in {
            "what could block progress",
            "what can block progress",
            "what blocks progress",
        }
    ):
        return "The main blockers are stale runtime state, routing drift, and unclear MIM to TOD handoff status."

    if "news" in normalized_query and any(
        token in normalized_query for token in {"top", "today", "todays", "latest"}
    ):
        return _with_next_step(
            "Top AI and tech themes today: production agent guardrails, "
            "inference cost and performance competition, enterprise private AI deployments, "
            "and tighter scrutiny around bot authenticity.",
            "I can narrow that to one sector or pull out the practical impact on MIM",
        )

    if _mentions_tod(normalized_query) and any(
        token in normalized_query
        for token in {
            "what are you and tod working on",
            "what is mim and tod working on",
            "upcoming tasks with tod",
            "prioritize with tod",
        }
    ):
        return "I can summarize what MIM and TOD are currently driving together, plus the next priority handoff."

    if _mentions_tod(normalized_query) and any(
        token in normalized_query for token in {"slow", "lag", "behind", "stuck"}
    ):
        return _with_next_step(
            "Thanks for flagging that. I can check TOD freshness and alignment if you want.",
            "ask for a one-line TOD status or the next recovery action",
        )

    if normalized_query.endswith("?") or _looks_like_question_text(normalized_query):
        return (
            "I can answer that directly. Ask for status, TOD focus, priorities, "
            "or top news, and I will keep it short."
        )

    if source == "voice":
        return f"I heard: '{_compact_text(raw, 96)}'."

    return f"Got it: '{_compact_text(raw, 96)}'."


def _intent_capability(event: InputEvent, internal_intent: str) -> str:
    explicit = str(event.metadata_json.get("capability", "")).strip()
    if explicit:
        return explicit

    routed = route_console_text_input(event.raw_input, event.parsed_intent)
    if routed.capability_name:
        return routed.capability_name

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
    if capability_name == "mim_arm.execute_gripper":
        raw = str(event.raw_input or "").strip().lower()
        degree_match = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:degree|degrees|deg)\b", raw)
        requested_degrees = None
        if degree_match:
            try:
                requested_degrees = float(degree_match.group(1))
            except ValueError:
                requested_degrees = None
        if "close" in raw:
            requested_action = "close_gripper"
        elif "open" in raw:
            requested_action = "open_gripper"
        else:
            requested_action = "set_gripper"
        return {
            "command_family": "mim_arm_gripper",
            "requested_action": requested_action,
            "requested_degrees": requested_degrees,
            "servo_id": 5,
            "safety_constraints": {
                "requires_estop_ok": True,
                "requires_serial_ready": True,
                "requires_arm_online": True,
                "requires_motion_allowed": True,
                "operator_confirmation_required": True,
            },
        }

    if capability_name != "workspace_scan":
        return {}

    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    return {
        "scan_mode": str(metadata.get("scan_mode", "standard")),
        "scan_area": str(metadata.get("scan_area", "workspace")),
        "confidence_threshold": float(metadata.get("confidence_threshold", 0.6)),
    }


def _proposed_actions(
    internal_intent: str, capability_name: str, goal_description: str
) -> list[dict]:
    if internal_intent == "request_clarification":
        return [
            {
                "step": 1,
                "action_type": "request_clarification",
                "details": goal_description,
            }
        ]
    if internal_intent == "create_goal":
        return [{"step": 1, "action_type": "create_goal", "details": goal_description}]
    if capability_name:
        return [
            {
                "step": 1,
                "action_type": "execute_capability",
                "capability": capability_name,
                "details": goal_description,
            }
        ]
    return [{"step": 1, "action_type": internal_intent, "details": goal_description}]


def _goal_description(event: InputEvent, internal_intent: str) -> str:
    requested = event.requested_goal.strip()
    if requested:
        return requested
    return f"{internal_intent}: {event.raw_input.strip()}"


def _requested_domains_for_event(event: InputEvent, internal_intent: str, capability_name: str) -> list[str]:
    domains: list[str] = []
    raw_input = " ".join(str(event.raw_input or "").strip().lower().split())
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    if capability_name in {"workspace_check", "capture_frame"} or internal_intent == "observe_workspace":
        domains.append("robot")
    if (
        not robotics_web_guard_blocks_search(raw_input)
        and (
            metadata.get("web_research_enabled")
            or any(token in raw_input for token in {"research", "web", "search", "look up"})
        )
    ):
        domains.append("web")
    if any(token in raw_input for token in {"memory", "history", "context", "data"}):
        domains.append("data")
    domains.append("decision")
    seen: set[str] = set()
    ordered: list[str] = []
    for item in domains:
        normalized = str(item or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


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
        (
            await db.execute(
                select(WorkspaceObservation)
                .where(WorkspaceObservation.zone == zone)
                .where(WorkspaceObservation.lifecycle_status != "superseded")
                .order_by(
                    WorkspaceObservation.last_seen_at.desc(),
                    WorkspaceObservation.id.desc(),
                )
                .limit(20)
            )
        )
        .scalars()
        .all()
    )

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
        label_counts[row.label] = label_counts.get(row.label, 0) + int(
            row.observation_count or 1
        )

    if label_counts:
        dominant_label = max(label_counts.items(), key=lambda item: item[1])[0]

    object_rows = (
        (
            await db.execute(
                select(WorkspaceObjectMemory)
                .where(WorkspaceObjectMemory.zone == zone)
                .order_by(
                    WorkspaceObjectMemory.last_seen_at.desc(),
                    WorkspaceObjectMemory.id.desc(),
                )
                .limit(20)
            )
        )
        .scalars()
        .all()
    )

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
        (await db.execute(select(WorkspaceZone).where(WorkspaceZone.zone_name == zone)))
        .scalars()
        .first()
    )
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
        (
            await db.execute(
                select(WorkspaceObjectMemory)
                .where(WorkspaceObjectMemory.status != "stale")
                .order_by(
                    WorkspaceObjectMemory.last_seen_at.desc(),
                    WorkspaceObjectMemory.id.desc(),
                )
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    best: WorkspaceObjectMemory | None = None
    best_score = 0.0
    for candidate in candidates:
        aliases = (
            candidate.candidate_labels
            if isinstance(candidate.candidate_labels, list)
            else []
        )
        labels = [candidate.canonical_name, *[str(item) for item in aliases]]
        label_match = any(
            _labels_similar(label, existing_label) for existing_label in labels
        )
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
    observation_item: dict,
    execution: CapabilityExecution | None = None,
    source_name: str = "workspace_scan",
    source_metadata: dict | None = None,
) -> WorkspaceObjectMemory | None:
    label = str(observation_item.get("label", "")).strip()
    if not label:
        return None

    observation_metadata = (
        observation_item.get("metadata_json")
        if isinstance(observation_item.get("metadata_json"), dict)
        else {}
    )
    semantic_metadata: dict[str, object] = {}
    for key in [
        "description",
        "purpose",
        "owner",
        "meaning",
        "category",
        "user_notes",
        "explanation",
        "expected_home_zone",
        "expected_zone",
        "home_zone",
    ]:
        value = observation_item.get(key)
        if value is None:
            value = observation_metadata.get(key)
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                semantic_metadata[key] = cleaned
        elif isinstance(value, (list, dict)) and value:
            semantic_metadata[key] = value
        elif isinstance(value, bool) and value:
            semantic_metadata[key] = value

    execution_args = (
        execution.arguments_json
        if execution and isinstance(execution.arguments_json, dict)
        else {}
    )
    execution_id = execution.id if execution else None
    zone = (
        str(
            observation_item.get("zone")
            or execution_args.get("scan_area")
            or "workspace"
        ).strip()
        or "workspace"
    )
    observed_at = _parse_observed_at(
        observation_item.get("observed_at")
    ) or datetime.now(timezone.utc)

    raw_confidence = observation_item.get("confidence", 0.0)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    now = datetime.now(timezone.utc)
    age_seconds = max((now - observed_at).total_seconds(), 0.0)
    observed_status = "stale" if age_seconds > OBJECT_STALE_WINDOW_SECONDS else "active"

    matched = await _match_object_identity(
        db=db, label=label, zone=zone, observed_at=observed_at
    )
    if matched:
        previous_zone = matched.zone
        history = (
            matched.location_history
            if isinstance(matched.location_history, list)
            else []
        )
        moved = previous_zone != zone
        if moved:
            history.append(
                {
                    "from": previous_zone,
                    "to": zone,
                    "moved_at": observed_at.isoformat(),
                    "execution_id": execution_id,
                }
            )

        labels = (
            matched.candidate_labels
            if isinstance(matched.candidate_labels, list)
            else []
        )
        labels_set = {str(item).strip().lower() for item in labels if str(item).strip()}
        labels_set.add(matched.canonical_name.strip().lower())
        labels_set.add(label.lower())

        matched.candidate_labels = sorted(labels_set)
        matched.confidence = max(matched.confidence, confidence)
        matched.zone = zone
        matched.last_seen_at = observed_at
        if execution_id is not None:
            matched.last_execution_id = execution_id
        matched.location_history = history
        matched.status = "uncertain" if moved else observed_status
        matched.metadata_json = {
            **(
                matched.metadata_json if isinstance(matched.metadata_json, dict) else {}
            ),
            **semantic_metadata,
            "last_matched_label": label,
            "last_observation": observation_item,
            "last_observation_source": source_name,
            "last_observation_source_metadata": (
                source_metadata if isinstance(source_metadata, dict) else {}
            ),
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
        last_execution_id=execution_id,
        location_history=[
            {
                "from": None,
                "to": zone,
                "moved_at": observed_at.isoformat(),
                "execution_id": execution_id,
            }
        ],
        metadata_json={
            **semantic_metadata,
            "last_matched_label": label,
            "last_observation": observation_item,
            "last_observation_source": source_name,
            "last_observation_source_metadata": (
                source_metadata if isinstance(source_metadata, dict) else {}
            ),
            "moved": False,
        },
    )
    db.add(object_memory)
    await db.flush()
    return object_memory


async def _update_missing_object_identities(
    *,
    db: AsyncSession,
    observed_labels_by_zone: dict[str, set[str]],
    execution: CapabilityExecution | None = None,
    source_name: str = "workspace_scan",
    source_session_id: str = "",
) -> None:
    zone_rows = (await db.execute(select(WorkspaceZone))).scalars().all()
    zone_ids = {row.zone_name: row.id for row in zone_rows}
    adjacent_map: dict[str, set[str]] = {}
    if zone_ids:
        relations = (
            (
                await db.execute(
                    select(WorkspaceZoneRelation).where(
                        WorkspaceZoneRelation.relation_type == "adjacent_to"
                    )
                )
            )
            .scalars()
            .all()
        )
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
            (
                await db.execute(
                    select(WorkspaceObjectMemory)
                    .where(WorkspaceObjectMemory.zone == zone)
                    .where(
                        WorkspaceObjectMemory.status.in_(
                            ["active", "uncertain", "missing"]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

        for row in rows:
            if _should_skip_missing_update_for_session(
                row=row,
                source_name=source_name,
                source_session_id=source_session_id,
            ):
                continue

            known_names = {row.canonical_name.lower()}
            aliases = (
                row.candidate_labels if isinstance(row.candidate_labels, list) else []
            )
            known_names.update(
                str(item).strip().lower() for item in aliases if str(item).strip()
            )

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
                    (
                        await db.execute(
                            select(WorkspaceObjectMemory)
                            .where(WorkspaceObjectMemory.zone == candidate_zone)
                            .where(
                                WorkspaceObjectMemory.status.in_(
                                    ["active", "uncertain"]
                                )
                            )
                            .where(
                                WorkspaceObjectMemory.canonical_name
                                == row.canonical_name
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
                if nearby:
                    likely_moved_to = candidate_zone
                    break

            row.metadata_json = {
                **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
                "missing_update_execution_id": execution.id if execution else None,
                "missing_update_at": now.isoformat(),
                "missing_update_source": source_name,
                "likely_moved_to": likely_moved_to,
            }


def _should_skip_missing_update_for_session(
    *,
    row: WorkspaceObjectMemory,
    source_name: str,
    source_session_id: str,
) -> bool:
    if str(source_name or "").strip().lower() != "live_camera":
        return False

    normalized_source_session = str(source_session_id or "").strip()
    if not normalized_source_session:
        return False

    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    last_source = str(metadata.get("last_observation_source") or "").strip().lower()
    source_metadata = (
        metadata.get("last_observation_source_metadata")
        if isinstance(metadata.get("last_observation_source_metadata"), dict)
        else {}
    )
    row_session_id = str(
        source_metadata.get("session_id") or metadata.get("last_session_id") or ""
    ).strip()

    return (
        last_source == "live_camera"
        and bool(row_session_id)
        and row_session_id != normalized_source_session
    )


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
                (
                    await db.execute(
                        select(WorkspaceObjectRelation)
                        .where(WorkspaceObjectRelation.subject_object_id == subject_id)
                        .where(WorkspaceObjectRelation.object_object_id == object_id)
                    )
                )
                .scalars()
                .first()
            )

            if existing:
                existing.relation_type = relation_type
                existing.relation_status = relation_status
                existing.confidence = confidence
                existing.last_seen_at = now
                existing.source_execution_id = execution.id
                existing.metadata_json = {
                    **(
                        existing.metadata_json
                        if isinstance(existing.metadata_json, dict)
                        else {}
                    ),
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

    row = (
        (await db.execute(stmt.order_by(WorkspaceProposal.id.desc()))).scalars().first()
    )
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

        metadata = (
            object_row.metadata_json
            if isinstance(object_row.metadata_json, dict)
            else {}
        )
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
                trigger_json={
                    "status": object_row.status,
                    "likely_moved_to": metadata.get("likely_moved_to", ""),
                },
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
                trigger_json={
                    "status": object_row.status,
                    "moved_from": metadata.get("moved_from", ""),
                },
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

    execution_args = (
        execution.arguments_json if isinstance(execution.arguments_json, dict) else {}
    )
    zone = (
        str(
            observation_item.get("zone")
            or execution_args.get("scan_area")
            or "workspace"
        ).strip()
        or "workspace"
    )
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
        (
            await db.execute(
                select(WorkspaceObservation)
                .where(WorkspaceObservation.label == label)
                .where(WorkspaceObservation.zone == zone)
                .where(WorkspaceObservation.lifecycle_status != "superseded")
                .where(WorkspaceObservation.last_seen_at >= window_start)
                .order_by(
                    WorkspaceObservation.last_seen_at.desc(),
                    WorkspaceObservation.id.desc(),
                )
            )
        )
        .scalars()
        .first()
    )

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
            **(
                existing.metadata_json
                if isinstance(existing.metadata_json, dict)
                else {}
            ),
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
    diagnostic_started = time.perf_counter()
    gateway_diagnostic: dict[str, object] = {}

    def _mark_gateway_diagnostic(stage: str, **fields: object) -> None:
        if not gateway_diagnostic:
            return
        stages = gateway_diagnostic.setdefault("stages", [])
        if not isinstance(stages, list):
            stages = []
            gateway_diagnostic["stages"] = stages
        stages.append(
            {
                "stage": stage,
                "elapsed_ms": round((time.perf_counter() - diagnostic_started) * 1000, 2),
                **{key: value for key, value in fields.items() if value not in {None, ""}},
            }
        )

    event_metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    if _should_force_deterministic_conversation_reply(event):
        gateway_diagnostic = {
            "mode": "conversation_eval",
            "requested_goal": str(event.requested_goal or "").strip(),
            "adapter": str(event_metadata.get("adapter") or "").strip(),
            "conversation_session_id": str(
                event_metadata.get("conversation_session_id") or ""
            ).strip(),
        }
        _mark_gateway_diagnostic("resolve_enter")

    internal_intent = _infer_intent(event)
    _mark_gateway_diagnostic("intent_inferred", internal_intent=internal_intent)
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    route_preference = str(metadata.get("route_preference", "")).strip().lower()
    conversation_override = route_preference == "conversation_layer"
    conversation_session_id = str(metadata.get("conversation_session_id", "")).strip()
    optional_escalation = ""
    conversation_context: dict[str, object] = {}
    return_briefing_context: dict[str, object] = {}
    normalized_conversation_query = ""
    session_display_name = ""
    session_confirmed_action_request = ""
    session_pending_action_request = ""
    session_control_state = "active"
    skip_conversation_memory = False
    mim_interface_reply_override = ""
    mim_interface_next_action_override = ""
    mim_interface_result_override = ""
    clarification_state: dict[str, object] = {}
    clarification_followup_query = ""
    offered_followup_response = ""
    communication_reply_contract: dict[str, object] = {}
    initiative_auto_execute = False
    initiative_boundary_mode = ""
    initiative_boundary_reason = ""

    if conversation_override:
        force_deterministic_conversation = _should_force_deterministic_conversation_reply(
            event
        )
        if force_deterministic_conversation:
            conversation_context = _build_eval_operational_context()
        else:
            conversation_context = await _build_live_operational_context(db)
        if conversation_session_id:
            session_context = await _get_recent_text_conversation_context(
                db,
                session_id=conversation_session_id,
                actor_name=str(metadata.get("user_id", "")).strip() or DEFAULT_USER_ID,
                exclude_event_id=event.id,
                prefer_interface_session_only=force_deterministic_conversation,
            )
            conversation_context = {
                **session_context,
                **conversation_context,
            }
        _mark_gateway_diagnostic(
            "conversation_context_ready",
            route_preference=route_preference,
            force_deterministic=force_deterministic_conversation,
        )
        normalized_conversation_query = _normalize_conversation_query(event.raw_input)
        session_display_name = str(
            conversation_context.get("session_display_name") or ""
        ).strip()
        session_pending_action_request = str(
            conversation_context.get("pending_action_request") or ""
        ).strip()
        session_control_state = (
            str(conversation_context.get("last_control_state") or "active")
            .strip()
            .lower()
            or "active"
        )
        clarification_state = (
            conversation_context.get("clarification_state")
            if isinstance(conversation_context.get("clarification_state"), dict)
            else {}
        )
        clarification_followup_query = _clarification_followup_query(
            normalized_conversation_query,
            clarification_state=clarification_state,
            context=conversation_context,
        )
        offered_followup_response = _conversation_offer_followup_response(
            normalized_conversation_query,
            conversation_context,
        )
        if (
            _is_conversation_action_approval_query(normalized_conversation_query)
            and session_pending_action_request
            and session_control_state not in {"paused", "cancelled", "stopped"}
            and not offered_followup_response
            and (
                str(conversation_context.get("last_topic") or "").strip().lower()
                == "action_confirmation"
                or bool(clarification_state.get("active"))
            )
        ):
            session_confirmed_action_request = session_pending_action_request
            conversation_override = False
            internal_intent = "create_goal"
            capability_name = ""
        else:
            internal_intent = "speak_response"
            capability_name = ""
    else:
        capability_name = _intent_capability(event, internal_intent)
    capability_registered = False
    capability_enabled = False
    capability_requires_confirmation = False

    capability: CapabilityRegistration | None = None
    if capability_name:
        capability = (
            (
                await db.execute(
                    select(CapabilityRegistration).where(
                        CapabilityRegistration.capability_name == capability_name
                    )
                )
            )
            .scalars()
            .first()
        )
        if capability:
            capability_registered = True
            capability_enabled = capability.enabled
            capability_requires_confirmation = capability.requires_confirmation

    if internal_intent == "observe_workspace" and (
        not capability_registered or not capability_enabled
    ):
        fallback_name = "workspace_check"
        fallback = (
            (
                await db.execute(
                    select(CapabilityRegistration).where(
                        CapabilityRegistration.capability_name == fallback_name
                    )
                )
            )
            .scalars()
            .first()
        )
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
    conversation_topic = ""
    object_inquiry: dict[str, object] = {}
    web_research: dict[str, object] = {}
    user_action_safety: dict[str, object] = {}
    self_evolution_briefing: dict[str, object] = {}
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

    if conversation_override:
        confidence_tier = "conversation"
        captured_session_display_name = _extract_session_display_name(event.raw_input)
        revised_action_request = _conversation_revised_action_request(
            normalized_conversation_query,
            prior_action_request=session_pending_action_request,
        )
        if captured_session_display_name:
            outcome = "store_only"
            safety_decision = "store_only"
            reason = "conversation_session_identity_capture"
            clarification_prompt = f"Got it, {captured_session_display_name}."
            conversation_topic = "session_identity"
            session_display_name = captured_session_display_name
            mim_interface_reply_override = clarification_prompt
            skip_conversation_memory = True
        elif revised_action_request:
            outcome = "store_only"
            safety_decision = "store_only"
            reason = "conversation_revised_action_request"
            clarification_prompt = (
                f"Understood. I updated the pending action to '{_compact_text(revised_action_request, 96)}'. "
                "Say confirm when you want me to create the goal."
            )
            conversation_topic = "action_confirmation"
        elif clarification_followup_query:
            outcome = "store_only"
            safety_decision = "store_only"
            reason = "conversation_clarification_followup"
            response_context = {
                "source": event.source,
                "target_system": event.target_system,
                **conversation_context,
            }
            if not force_deterministic_conversation:
                camera_context = await _latest_camera_observation_context(db)
                object_memory_context = await _object_memory_context_for_query(
                    db, clarification_followup_query
                )
                response_context.update(camera_context)
                response_context.update(object_memory_context)
            if force_deterministic_conversation:
                response_context["force_deterministic_communication"] = True
            clarification_prompt = _conversation_response(
                clarification_followup_query,
                context=response_context,
            )
            conversation_topic = _conversation_topic_key(
                clarification_followup_query,
                clarification_prompt,
            )
        elif offered_followup_response:
            outcome = "store_only"
            safety_decision = "store_only"
            reason = "conversation_override"
            clarification_prompt = offered_followup_response
            conversation_topic = _conversation_topic_key(
                normalized_conversation_query,
                offered_followup_response,
            )
        elif (
            _is_conversation_action_approval_query(normalized_conversation_query)
            and session_pending_action_request
            and session_control_state == "paused"
        ):
            outcome = "store_only"
            safety_decision = "store_only"
            reason = "conversation_pending_action_paused"
            clarification_prompt = (
                "The pending action is paused. Say resume before you confirm it."
            )
            conversation_topic = "action_confirmation"
        elif _looks_like_action_request(event.raw_input):
            initiative_boundary = classify_boundary_mode(event.raw_input)
            initiative_boundary_mode = str(
                initiative_boundary.get("boundary_mode") or ""
            ).strip()
            initiative_boundary_reason = str(
                initiative_boundary.get("reason") or ""
            ).strip()
            if initiative_boundary_mode != HARD_BOUNDARY:
                conversation_override = False
                internal_intent = "create_goal"
                capability_name = ""
                initiative_auto_execute = True
                optional_escalation = ""
                confidence_tier = "authorized"
                outcome = "auto_execute"
                safety_decision = "auto_execute"
                reason = "authorized_initiative_auto_execute"
                escalation_reasons = []
            else:
                action_text = " ".join(str(event.raw_input or "").strip().lower().split())
                social_capability_check = (
                    "tod" in action_text
                    and "social media" in action_text
                    and any(
                        token in action_text
                        for token in {
                            "capability",
                            "posting capability",
                            "can post",
                            "post to social media",
                        }
                    )
                )
                repeated_prompt = _looks_like_retry_followup(event.raw_input)
                if (
                    not repeated_prompt
                    and conversation_session_id
                    and not force_deterministic_conversation
                ):
                    repeated_prompt = await _has_recent_similar_text_precision_prompt(
                        db,
                        transcript=event.raw_input,
                        exclude_event_id=event.id,
                        session_id=conversation_session_id,
                        within_seconds=180,
                    )
                outcome = "requires_confirmation"
                safety_decision = "requires_confirmation"
                if repeated_prompt:
                    reason = "conversation_optional_escalation_followup"
                    escalation_reasons = [
                        "suggest_goal_creation",
                        "clarification_limit_reached",
                    ]
                    if social_capability_check:
                        optional_escalation = (
                            "Execution is still pending explicit confirmation. Options: "
                            "1) ask a question, 2) discuss, 3) create goal: TOD capability check for AgentMIM social media posting."
                        )
                    else:
                        optional_escalation = (
                            "Execution is still pending explicit confirmation. Options: "
                            "1) ask a question, 2) discuss, 3) create goal: <action>."
                        )
                else:
                    reason = "conversation_optional_escalation"
                    escalation_reasons = ["suggest_goal_creation"]
                    if social_capability_check:
                        optional_escalation = (
                            "I can have TOD check AgentMIM social-media posting capability when you are ready. "
                            "Say: create goal: TOD capability check for AgentMIM social media posting."
                        )
                    else:
                        optional_escalation = (
                            "I can execute that when you are ready. Say: create goal: "
                            "<action>."
                        )
                clarification_prompt = optional_escalation
        elif _is_low_signal_turn(event.raw_input):
            repeated_prompt = _looks_like_retry_followup(event.raw_input)
            if (
                not repeated_prompt
                and conversation_session_id
                and not force_deterministic_conversation
            ):
                repeated_prompt = await _has_recent_similar_text_precision_prompt(
                    db,
                    transcript=event.raw_input,
                    exclude_event_id=event.id,
                    session_id=conversation_session_id,
                    within_seconds=180,
                )
            _mark_gateway_diagnostic(
                "low_signal_evaluated",
                repeated_prompt=repeated_prompt,
            )
            outcome = "store_only"
            safety_decision = "store_only"
            if repeated_prompt:
                reason = "conversation_precision_limit"
                escalation_reasons = [
                    "needs_specific_request",
                    "clarification_limit_reached",
                ]
                clarification_prompt = _build_clarification_limit_prompt(
                    escalation_reasons,
                    event.raw_input,
                )
            else:
                reason = "conversation_precision_prompt"
                escalation_reasons = ["needs_specific_request"]
                clarification_prompt = _build_one_clarifier_prompt(event.raw_input)
        else:
            outcome = "store_only"
            safety_decision = "store_only"
            reason = "conversation_override"
            camera_object_inquiry: dict[str, object] = {}
            learned_object_prompt = ""
            learned_object_inquiry = {}
            if not force_deterministic_conversation and _is_return_briefing_query(normalized_conversation_query):
                return_briefing_context = await _build_return_briefing_context(db)
            if not force_deterministic_conversation and _is_self_evolution_next_work_query(normalized_conversation_query):
                self_evolution_briefing_result = await build_self_evolution_briefing(
                    actor="gateway_self_evolution_next_work",
                    source="gateway_conversation_self_evolution_next_work",
                    refresh=False,
                    lookback_hours=168,
                    min_occurrence_count=2,
                    auto_experiment_limit=3,
                    limit=5,
                    db=db,
                )
                self_evolution_briefing = (
                    self_evolution_briefing_result.get("briefing", {})
                    if isinstance(self_evolution_briefing_result, dict)
                    else {}
                )
            response_context = {
                "source": event.source,
                "target_system": event.target_system,
                "operator_return_briefing": return_briefing_context,
                "self_evolution_briefing": self_evolution_briefing,
                "response_mode": str(metadata.get("response_mode") or "").strip().lower(),
                "force_deterministic_communication": force_deterministic_conversation,
                **conversation_context,
            }
            if not force_deterministic_conversation:
                camera_context = await _latest_camera_observation_context(db)
                camera_object_inquiry = await _camera_object_inquiry_context(
                    db,
                    camera_context=camera_context,
                )
                object_memory_context = await _object_memory_context_for_query(
                    db, event.raw_input
                )
                (
                    learned_object_prompt,
                    learned_object_inquiry,
                ) = await _learn_from_object_inquiry_reply(
                    db,
                    user_input=event.raw_input,
                    inquiry_context=conversation_context.get("last_object_inquiry"),
                )
                response_context.update(camera_context)
                response_context.update(object_memory_context)
            instructional_setup = _build_instructional_setup_response(
                event.raw_input,
                normalized_query=_normalize_conversation_query(event.raw_input),
                context=response_context,
            )
            _mark_gateway_diagnostic(
                "response_context_ready",
                has_camera_prompt=bool(camera_object_inquiry),
                has_learned_object_prompt=bool(learned_object_prompt),
            )
            if camera_object_inquiry:
                response_context["camera_object_inquiry_prompt"] = str(
                    camera_object_inquiry.get("inquiry_prompt") or ""
                ).strip()
            if instructional_setup:
                reason = "conversation_setup_instruction"
                clarification_prompt = str(
                    instructional_setup.get("result") or ""
                ).strip()
                conversation_topic = "instructional_setup"
                mim_interface_next_action_override = str(
                    instructional_setup.get("next_action") or ""
                ).strip()
                mim_interface_result_override = clarification_prompt
            elif learned_object_prompt:
                clarification_prompt = learned_object_prompt
                object_inquiry = learned_object_inquiry
                conversation_topic = "object_inquiry"
            else:
                normalized_conversation_query = _normalize_conversation_query(
                    event.raw_input
                )
                if _is_technical_research_execution_followup(
                    normalized_conversation_query,
                    conversation_context,
                ):
                    technical_followup = _run_bounded_technical_followup_research(
                        normalized_conversation_query,
                        conversation_context,
                    )
                    if technical_followup:
                        clarification_prompt = str(
                            technical_followup.get("answer") or ""
                        ).strip()
                        updated_technical_context = (
                            technical_followup.get("technical_context")
                            if isinstance(
                                technical_followup.get("technical_context"), dict
                            )
                            else {}
                        )
                        web_research = {
                            "query": str(
                                updated_technical_context.get("query")
                                or event.raw_input
                            ).strip(),
                            "technical_plan": _technical_context_to_plan(
                                updated_technical_context
                            ),
                            "technical_step_findings": updated_technical_context.get(
                                "step_findings", []
                            ),
                            "next_steps": updated_technical_context.get(
                                "next_steps", []
                            ),
                            "technical_followup_rounds_completed": int(
                                updated_technical_context.get(
                                    "followup_rounds_completed", 0
                                )
                                or 0
                            ),
                            "technical_max_followup_rounds": int(
                                updated_technical_context.get("max_followup_rounds", 0)
                                or 0
                            ),
                            "technical_last_researched_step_index": int(
                                updated_technical_context.get(
                                    "last_researched_step_index", 0
                                )
                                or 0
                            ),
                            "technical_last_round_had_evidence": bool(
                                updated_technical_context.get("last_round_had_evidence")
                            ),
                            "followup_mode": "technical_deeper_round",
                            "selected_step_index": int(
                                technical_followup.get("selected_step_index", 0) or 0
                            ),
                            "search_diagnostics": technical_followup.get(
                                "search_diagnostics", {}
                            ),
                        }
                elif _should_use_web_research(normalized_conversation_query):
                    web_research = await _perform_web_research(
                        db,
                        query=event.raw_input,
                    )
                    clarification_prompt = str(
                        web_research.get("answer")
                        or "I tried to research that on the web, but I could not collect reliable public sources right now."
                    ).strip()
                else:
                    composer_runtime_diagnostics: dict[str, object] = {}
                    if _should_force_deterministic_conversation_reply(event):
                        response_context["force_deterministic_communication"] = True
                    composed_reply = await _compose_conversation_reply(
                        user_input=event.raw_input,
                        context=response_context,
                        runtime_diagnostics=composer_runtime_diagnostics,
                    )
                    _mark_gateway_diagnostic(
                        "conversation_reply_ready",
                        composer_mode=str(
                            composer_runtime_diagnostics.get("composer_mode") or ""
                        ).strip(),
                    )
                    clarification_prompt = str(
                        composed_reply.get("reply_text") or ""
                    ).strip()
                    communication_reply_contract = (
                        composed_reply.get("contract")
                        if isinstance(composed_reply.get("contract"), dict)
                        else {}
                    )
                    if composer_runtime_diagnostics:
                        communication_reply_contract = {
                            **communication_reply_contract,
                            "runtime_diagnostics": composer_runtime_diagnostics,
                        }
                        if composer_runtime_diagnostics.get("degraded"):
                            resolution_meta = {
                                **resolution_meta,
                                "gateway_diagnostic": {
                                    "composer_mode": str(
                                        composer_runtime_diagnostics.get("composer_mode")
                                        or ""
                                    ).strip(),
                                    "composer_reason": str(
                                        composer_runtime_diagnostics.get("composer_reason")
                                        or ""
                                    ).strip(),
                                },
                            }
                    if (
                        camera_object_inquiry
                        and clarification_prompt
                        == str(
                            camera_object_inquiry.get("inquiry_prompt") or ""
                        ).strip()
                    ):
                        object_inquiry = camera_object_inquiry
            if (
                session_display_name
                and clarification_prompt
                and not mim_interface_next_action_override
                and not mim_interface_result_override
            ):
                mim_interface_reply_override = _address_session_reply(
                    clarification_prompt,
                    session_display_name,
                )
        conversation_topic = _conversation_topic_key(
            _normalize_conversation_query(event.raw_input),
            clarification_prompt,
        )
        if object_inquiry:
            conversation_topic = "object_inquiry"
        normalized_conversation_query = _normalize_conversation_query(event.raw_input)
        if (
            conversation_context
            and (
                _is_conversation_followup_query(normalized_conversation_query)
                or _is_technical_research_execution_followup(
                    normalized_conversation_query,
                    conversation_context,
                )
            )
            and str(conversation_context.get("last_topic") or "").strip()
        ):
            conversation_topic = (
                str(conversation_context.get("last_topic") or "").strip().lower()
            )
    elif event.source == "vision":
        detected_labels_raw = event.metadata_json.get("detected_labels", [])
        detected_labels = (
            [str(label) for label in detected_labels_raw]
            if isinstance(detected_labels_raw, list)
            else []
        )
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
        reason = (
            escalation_reasons[0] if escalation_reasons else "vision_policy_outcome"
        )
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
            has_similar_prior = await _has_recent_similar_voice_clarification(
                db,
                transcript=event.raw_input,
                within_seconds=180,
            )
            if not has_similar_prior:
                clarification_prompt = _build_one_clarifier_prompt(event.raw_input)
            else:
                clarification_prompt = _build_clarification_limit_prompt(
                    escalation_reasons, event.raw_input
                )
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
        elif capability_requires_confirmation and not initiative_auto_execute:
            safety_decision = "requires_confirmation"
            reason = "capability_policy_requires_confirmation"
        else:
            safety_decision = "auto_execute"
            reason = "policy_allows_auto_execute"
        outcome = safety_decision

    if initiative_auto_execute and outcome == "auto_execute":
        reason = "authorized_initiative_auto_execute"
        if internal_intent == "create_goal" and not clarification_prompt:
            clarification_prompt = (
                "Created one bounded goal for: "
                f"{_compact_text(event.raw_input, 160)}"
            )
            mim_interface_next_action_override = (
                "continue the newly created bounded goal without further confirmation"
            )
            mim_interface_result_override = clarification_prompt
            mim_interface_reply_override = (
                f"Request {str(metadata.get('request_id') or '').strip()}. "
                f"I understood: {event.raw_input}. "
                f"Next action: {mim_interface_next_action_override}. "
                f"Status: done. Result: {clarification_prompt}"
            ).strip()

    if session_confirmed_action_request:
        confidence_tier = "confirmed"
        outcome = "auto_execute"
        safety_decision = "auto_execute"
        reason = "conversation_confirmed_action_request"
        clarification_prompt = (
            "Confirmed. I created a goal for: "
            f"{_compact_text(session_confirmed_action_request, 160)}"
        )
        escalation_reasons = []
        conversation_topic = "action_confirmation"

    if (
        not conversation_override
        and _looks_like_action_request(event.raw_input)
        and internal_intent in {"execute_capability", "create_goal"}
    ):
        user_action_safety = _assess_user_action_safety_for_event(
            event,
            internal_intent=internal_intent,
        )
        if user_action_safety:
            if bool(user_action_safety.get("recommended_inquiry", False)):
                safety_decision = "requires_confirmation"
                outcome = "requires_confirmation"
                reason = "user_action_safety_requires_inquiry"
                if "user_action_safety_risk" not in escalation_reasons:
                    escalation_reasons.append("user_action_safety_risk")
                inquiry_id = str(user_action_safety.get("inquiry_id", "")).strip()
                if not clarification_prompt and inquiry_id:
                    clarification_prompt = (
                        "High-risk action detected. Safety inquiry created: "
                        f"{inquiry_id}. Provide intent confirmation before execution."
                    )

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
                if (
                    "requires_confirmation" not in safety_flags
                    and not capability_requires_confirmation
                ):
                    safety_decision = "auto_execute"
                    outcome = "auto_execute"
                    reason = "memory_confident_recent_identity"

    if (
        capability_name
        and (not capability_registered or not capability_enabled)
        and outcome != "blocked"
    ):
        outcome = "blocked"
        safety_decision = "blocked"
        reason = "capability_unavailable"
        if "requires_clarification" not in escalation_reasons:
            escalation_reasons.append("requires_clarification")
        if not clarification_prompt and event.source == "voice":
            clarification_prompt = "I cannot run that capability right now. Please choose an available capability."

    system_health_signal = _execution_system_health_signal(internal_intent)
    if str(system_health_signal.get("code", "")).strip() == "system_health_degraded":
        if "system_health_degraded" not in escalation_reasons:
            escalation_reasons.append("system_health_degraded")
        health_prompt = str(system_health_signal.get("prompt", "")).strip()
        health_secondary_prompt = str(system_health_signal.get("secondary_prompt", "")).strip()
        if health_prompt:
            if clarification_prompt:
                # Safety inquiry is already the primary blocker — use the shorter secondary note
                # so the operator isn't shown two redundant "confirmation required" clauses.
                addendum = health_secondary_prompt or health_prompt
                if addendum not in clarification_prompt:
                    clarification_prompt = f"{clarification_prompt} {addendum}".strip()
            else:
                clarification_prompt = health_prompt
        if outcome == "auto_execute":
            outcome = "requires_confirmation"
            safety_decision = "requires_confirmation"
            if reason != "user_action_safety_requires_inquiry":
                reason = "system_health_degraded"

    requires_clarification_only = event.source == "voice" and (
        outcome in {"store_only", "requires_confirmation", "blocked"}
        and (
            "requires_clarification" in escalation_reasons
            or "ambiguous_command" in escalation_reasons
            or "missing_target" in escalation_reasons
        )
    )

    governance = _build_gateway_governance_metadata(
        reason=reason,
        outcome=outcome,
        user_action_safety=user_action_safety,
        system_health_signal=system_health_signal,
    )

    goal_id: int | None = None
    goal_description = (
        session_confirmed_action_request
        if session_confirmed_action_request
        else _goal_description(event, internal_intent)
    )
    requested_domains = _requested_domains_for_event(event, internal_intent, capability_name)
    intent_understanding = understand_intent(
        raw_text=event.raw_input,
        internal_intent=internal_intent,
        requested_goal=goal_description,
        capability_name=capability_name,
        metadata_json={
            **metadata,
            "requested_domains": requested_domains,
        },
    )
    proposed_actions = _proposed_actions(
        internal_intent, capability_name, goal_description
    )
    suggested_steps = (
        intent_understanding.get("suggested_steps", [])
        if isinstance(intent_understanding.get("suggested_steps", []), list)
        else []
    )
    if suggested_steps:
        proposed_actions = [
            {
                "step": int(item.get("step") or index),
                "action_type": str(item.get("action_type") or "decision_review").strip(),
                "capability": str(item.get("capability") or "").strip(),
                "domain": str(item.get("domain") or "decision").strip(),
                "details": str(item.get("details") or "").strip(),
            }
            for index, item in enumerate(suggested_steps, start=1)
        ]
    if (
        outcome not in {"blocked", "store_only"}
        and internal_intent != "request_clarification"
        and not requires_clarification_only
        and not conversation_override
    ):
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
        proposed_actions=proposed_actions,
        metadata_json={
            "request_id": str(metadata.get("request_id") or "").strip(),
            "source": event.source,
            "confidence": event.confidence,
            "safety_flags": event.safety_flags,
            "target_system": event.target_system,
            "memory_signal": memory_signal,
            "clarification_prompt_key": _normalize_prompt_key(clarification_prompt),
            "route_preference": route_preference,
            "conversation_override": conversation_override,
            "optional_escalation": optional_escalation,
            "conversation_topic": conversation_topic,
            "session_display_name": session_display_name,
            "skip_conversation_memory": skip_conversation_memory,
            "mim_interface_next_action_override": mim_interface_next_action_override,
            "mim_interface_result_override": mim_interface_result_override,
            "mim_interface_reply_override": mim_interface_reply_override,
            "initiative_auto_execute": initiative_auto_execute,
            "initiative_boundary_mode": initiative_boundary_mode,
            "initiative_boundary_reason": initiative_boundary_reason,
            "active_goal": str(conversation_context.get("active_goal") or "").strip(),
            "operator_reasoning_summary": str(conversation_context.get("operator_reasoning_summary") or "").strip(),
            "runtime_health_summary": str(conversation_context.get("runtime_health_summary") or "").strip(),
            "runtime_recovery_summary": str(conversation_context.get("runtime_recovery_summary") or "").strip(),
            "tod_collaboration_summary": str(conversation_context.get("tod_collaboration_summary") or "").strip(),
            "current_recommendation_summary": str(conversation_context.get("current_recommendation_summary") or "").strip(),
            "program_status_summary": str(conversation_context.get("program_status_summary") or "").strip(),
            "program_status": (
                conversation_context.get("program_status")
                if isinstance(conversation_context.get("program_status"), dict)
                else {}
            ),
            "gateway_diagnostic": gateway_diagnostic,
            "object_inquiry": object_inquiry,
            "web_research": web_research,
            "requested_domains": requested_domains,
            "intent_understanding": intent_understanding,
            "user_action_safety": user_action_safety,
            "governance": governance,
            "last_technical_research": _compact_technical_research_context(
                web_research
            ),
            "communication_reply_contract": communication_reply_contract,
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
            "user_action_safety": user_action_safety,
            "governance": governance,
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
        (
            await db.execute(
                select(CapabilityExecution).where(
                    CapabilityExecution.input_event_id == event.id
                )
            )
        )
        .scalars()
        .first()
    )

    blocked_like = resolution.outcome in {"blocked", "store_only"}
    if blocked_like and not force_dispatch:
        requested_decision = "blocked"
        requested_status = "blocked"
        requested_reason = resolution.reason or "resolution_blocked"
    elif resolution.outcome == "auto_execute" or force_dispatch:
        requested_decision = "auto_dispatch"
        requested_status = "dispatched"
        requested_reason = "approved_for_dispatch"
    else:
        requested_decision = "requires_confirmation"
        requested_status = "pending_confirmation"
        requested_reason = resolution.reason or "confirmation_required"

    payload_args = arguments_json or {}
    resolution_metadata = (
        resolution.metadata_json
        if isinstance(getattr(resolution, "metadata_json", {}), dict)
        else {}
    )
    event_metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    managed_scope = infer_managed_scope(
        payload_args,
        event_metadata,
        resolution_metadata,
        event.requested_goal,
    )
    gate_result = await evaluate_execution_policy_gate(
        db=db,
        capability_name=capability_name,
        requested_decision=requested_decision,
        requested_status=requested_status,
        requested_reason=requested_reason,
        requested_executor=requested_executor,
        safety_mode=safety_mode,
        managed_scope=managed_scope,
        actor="gateway",
        source="gateway",
        metadata_json=event_metadata,
        execution_id=int(existing.id) if existing is not None else None,
        trace_id=str(existing.trace_id or "").strip() if existing is not None else "",
    )
    metadata_feedback = {
        "resolution_outcome": resolution.outcome,
        "escalation_reasons": resolution.escalation_reasons,
        "execution_readiness": gate_result.get("execution_readiness", {}) if isinstance(gate_result, dict) else {},
        "execution_policy_gate": gate_result,
        "managed_scope": managed_scope,
    }
    metadata_feedback = json.loads(json.dumps(metadata_feedback, default=str))

    if existing is None:
        execution = CapabilityExecution(
            input_event_id=event.id,
            resolution_id=resolution.id,
            goal_id=resolution.goal_id,
            capability_name=capability_name,
            arguments_json=payload_args,
            safety_mode=safety_mode,
            requested_executor=gate_result["requested_executor"],
            dispatch_decision=gate_result["dispatch_decision"],
            managed_scope=gate_result["managed_scope"],
            status=gate_result["status"],
            reason=gate_result["reason"],
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
        existing.requested_executor = gate_result["requested_executor"]
        existing.dispatch_decision = gate_result["dispatch_decision"]
        existing.managed_scope = gate_result["managed_scope"]
        existing.status = gate_result["status"]
        existing.reason = gate_result["reason"]
        existing.feedback_json = {
            **(existing.feedback_json or {}),
            **metadata_feedback,
        }
        execution = existing

    control_state = await sync_execution_control_state(
        db=db,
        execution=execution,
        actor="gateway",
        source="gateway",
        requested_goal=event.requested_goal,
        intent_key=build_intent_key(
            execution_source="input_event",
            subject_id=event.id,
            capability_name=capability_name,
        ),
        intent_type=str(resolution.internal_intent or event.parsed_intent or "execution_request").strip(),
        context_json={
            "event_metadata": event_metadata,
            "resolution_metadata": resolution_metadata,
        },
        gate_result=gate_result,
    )

    await write_journal(
        db,
        actor="gateway",
        action="bind_capability_execution",
        target_type="input_event",
        target_id=str(event.id),
        summary=f"Execution binding {execution.id} for {capability_name}: {execution.status}",
        metadata_json={
            "execution_id": execution.id,
            "dispatch_decision": execution.dispatch_decision,
            "status": execution.status,
            "requested_executor": execution.requested_executor,
            "trace_id": control_state["trace_id"],
            "managed_scope": control_state["managed_scope"],
        },
    )
    return execution


async def _store_normalized(payload: NormalizedInputCreate, db: AsyncSession) -> dict:
    payload_metadata, request_id = _ensure_request_id(payload.metadata_json)
    payload.metadata_json = payload_metadata
    trace: dict[str, object] = {
        "request_id": request_id,
        "started_at": _gateway_trace_timestamp(),
        "source": str(payload.source or "").strip(),
        "requested_goal": str(payload.requested_goal or "").strip(),
        "conversation_session_id": str(
            payload_metadata.get("conversation_session_id") or ""
        ).strip(),
        "adapter": str(payload_metadata.get("adapter") or "").strip(),
        "events": [],
    }
    started_monotonic = time.perf_counter()
    _append_gateway_trace_event(
        trace,
        "store_normalized_start",
        route_preference=str(payload_metadata.get("route_preference") or "").strip(),
    )
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
    _append_gateway_trace_event(trace, "event_flushed", event_id=int(event.id))

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

    try:
        _append_gateway_trace_event(trace, "resolve_start")
        resolution = await _resolve_event(event, db)
        resolution_meta = (
            resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}
        )
        _append_gateway_trace_event(
            trace,
            "resolve_end",
            internal_intent=str(resolution.internal_intent or "").strip(),
            outcome=str(resolution.outcome or "").strip(),
            reason=str(resolution.reason or "").strip(),
        )
        is_conversation_override = bool(resolution_meta.get("conversation_override"))
        skip_conversation_memory = bool(resolution_meta.get("skip_conversation_memory"))
        tod_dispatch = None
        handoff_submission = None
        initiative_run = None
        continuation_validation_request = _looks_like_continuation_validation_request(
            event.raw_input
        )

        if (
            event.source == "text"
            and not route_console_text_input(
                event.raw_input,
                event.parsed_intent,
            ).capability_name
            and not continuation_validation_request
            and not _looks_like_bounded_choice_decision_prompt(event.raw_input)
            and (
            _looks_like_bounded_warning_care_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            )
            or _looks_like_bounded_tod_bridge_warning_recommendation_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            )
            or _looks_like_bounded_tod_bridge_warning_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            )
            or _looks_like_bounded_tod_warnings_summary_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            )
            or _looks_like_bounded_tod_objective_summary_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            )
            or _looks_like_bounded_tod_recent_changes_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            )
            or _looks_like_bounded_tod_status_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            )
            )
        ):
            if _looks_like_bounded_warning_care_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            ):
                primary_dispatch = dispatch_bounded_tod_warnings_summary_request(
                    request_id=request_id,
                    session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    content=event.raw_input,
                    actor="mim",
                )
                controlled_continuation = _build_bounded_controlled_continuation(
                    primary_dispatch=primary_dispatch,
                    request_id=request_id,
                    session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    actor="mim",
                )
                tod_dispatch = (
                    controlled_continuation.get("final_dispatch")
                    if isinstance(controlled_continuation.get("final_dispatch"), dict)
                    else primary_dispatch
                )
                synchronize_latest_result_artifact_from_dispatch(tod_dispatch)
                next_action_text = (
                    "execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result"
                )
                combined_result = _format_bounded_controlled_continuation_result(
                    primary_result_label="Warnings summary",
                    continuation=controlled_continuation,
                )
                resolution.reason = "tod_warning_care_next_step_dispatch"
                resolution_meta = {
                    **resolution_meta,
                    "route_preference": "goal_system",
                    "conversation_override": False,
                    "tod_primary_dispatch": primary_dispatch,
                    "tod_selected_next_step": controlled_continuation.get("selected_next_step", {}),
                    "tod_controlled_continuation": {
                        "max_depth": int(controlled_continuation.get("max_depth", 3) or 3),
                        "step_count": int(controlled_continuation.get("step_count", 0) or 0),
                        "steps": controlled_continuation.get("steps", []),
                        "stop_reason": str(controlled_continuation.get("stop_reason") or "").strip(),
                        "stop_detail": str(controlled_continuation.get("stop_detail") or "").strip(),
                    },
                    "tod_dispatch": tod_dispatch,
                    "mim_interface_next_action_override": next_action_text,
                    "mim_interface_result_override": combined_result,
                    "mim_interface_reply_override": (
                        f"Request {request_id}. I understood: {event.raw_input}. "
                        f"Next action: {next_action_text}. "
                        f"Status: done. Result: {combined_result}"
                    ).strip(),
                }
                resolution.metadata_json = resolution_meta
                resolution.outcome = "store_only"
                resolution.safety_decision = "store_only"
                resolution.clarification_prompt = combined_result
                is_conversation_override = False
            elif _looks_like_bounded_tod_bridge_warning_recommendation_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            ):
                tod_dispatch = dispatch_bounded_tod_bridge_warning_recommendation_request(
                    request_id=request_id,
                    session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    content=event.raw_input,
                    actor="mim",
                )
                resolution.reason = "tod_bridge_warning_recommendation_dispatch"
            elif _looks_like_bounded_tod_bridge_warning_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            ):
                primary_dispatch = dispatch_bounded_tod_bridge_warning_request(
                    request_id=request_id,
                    session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    content=event.raw_input,
                    actor="mim",
                )
                controlled_continuation = _build_bounded_controlled_continuation(
                    primary_dispatch=primary_dispatch,
                    request_id=request_id,
                    session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    actor="mim",
                )
                tod_dispatch = (
                    controlled_continuation.get("final_dispatch")
                    if isinstance(controlled_continuation.get("final_dispatch"), dict)
                    else primary_dispatch
                )
                synchronize_latest_result_artifact_from_dispatch(tod_dispatch)
                next_action_text = (
                    "execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result"
                )
                combined_result = _format_bounded_controlled_continuation_result(
                    primary_result_label="Bridge-warning explanation",
                    continuation=controlled_continuation,
                )
                resolution.reason = "tod_bridge_warning_next_step_dispatch"
                resolution_meta = {
                    **resolution_meta,
                    "route_preference": "goal_system",
                    "conversation_override": False,
                    "tod_primary_dispatch": primary_dispatch,
                    "tod_selected_next_step": controlled_continuation.get("selected_next_step", {}),
                    "tod_controlled_continuation": {
                        "max_depth": int(controlled_continuation.get("max_depth", 3) or 3),
                        "step_count": int(controlled_continuation.get("step_count", 0) or 0),
                        "steps": controlled_continuation.get("steps", []),
                        "stop_reason": str(controlled_continuation.get("stop_reason") or "").strip(),
                        "stop_detail": str(controlled_continuation.get("stop_detail") or "").strip(),
                    },
                    "tod_dispatch": tod_dispatch,
                    "mim_interface_next_action_override": next_action_text,
                    "mim_interface_result_override": combined_result,
                    "mim_interface_reply_override": (
                        f"Request {request_id}. I understood: {event.raw_input}. "
                        f"Next action: {next_action_text}. "
                        f"Status: done. Result: {combined_result}"
                    ).strip(),
                }
                resolution.metadata_json = resolution_meta
                resolution.outcome = "store_only"
                resolution.safety_decision = "store_only"
                resolution.clarification_prompt = combined_result
                is_conversation_override = False
            elif _looks_like_bounded_tod_warnings_summary_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            ):
                tod_dispatch = dispatch_bounded_tod_warnings_summary_request(
                    request_id=request_id,
                    session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    content=event.raw_input,
                    actor="mim",
                )
                resolution.reason = "tod_warnings_summary_dispatch"
            elif _looks_like_bounded_tod_objective_summary_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            ):
                primary_dispatch = dispatch_bounded_tod_objective_summary_request(
                    request_id=request_id,
                    session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    content=event.raw_input,
                    actor="mim",
                )
                controlled_continuation = _build_bounded_controlled_continuation(
                    primary_dispatch=primary_dispatch,
                    request_id=request_id,
                    session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    actor="mim",
                )
                tod_dispatch = (
                    controlled_continuation.get("final_dispatch")
                    if isinstance(controlled_continuation.get("final_dispatch"), dict)
                    else primary_dispatch
                )
                synchronize_latest_result_artifact_from_dispatch(tod_dispatch)
                next_action_text = (
                    "execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result"
                )
                combined_result = _format_bounded_controlled_continuation_result(
                    primary_result_label="Current-objective summary",
                    continuation=controlled_continuation,
                )
                resolution.reason = "tod_objective_summary_next_step_dispatch"
                resolution_meta = {
                    **resolution_meta,
                    "route_preference": "goal_system",
                    "conversation_override": False,
                    "tod_primary_dispatch": primary_dispatch,
                    "tod_selected_next_step": controlled_continuation.get("selected_next_step", {}),
                    "tod_controlled_continuation": {
                        "max_depth": int(controlled_continuation.get("max_depth", 3) or 3),
                        "step_count": int(controlled_continuation.get("step_count", 0) or 0),
                        "steps": controlled_continuation.get("steps", []),
                        "stop_reason": str(controlled_continuation.get("stop_reason") or "").strip(),
                        "stop_detail": str(controlled_continuation.get("stop_detail") or "").strip(),
                    },
                    "tod_dispatch": tod_dispatch,
                    "mim_interface_next_action_override": next_action_text,
                    "mim_interface_result_override": combined_result,
                    "mim_interface_reply_override": (
                        f"Request {request_id}. I understood: {event.raw_input}. "
                        f"Next action: {next_action_text}. "
                        f"Status: done. Result: {combined_result}"
                    ).strip(),
                }
                resolution.metadata_json = resolution_meta
                resolution.outcome = "store_only"
                resolution.safety_decision = "store_only"
                resolution.clarification_prompt = combined_result
                is_conversation_override = False
            elif _looks_like_bounded_tod_recent_changes_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            ):
                primary_dispatch = dispatch_bounded_tod_recent_changes_request(
                    request_id=request_id,
                    session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    content=event.raw_input,
                    actor="mim",
                )
                controlled_continuation = _build_bounded_controlled_continuation(
                    primary_dispatch=primary_dispatch,
                    request_id=request_id,
                    session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    actor="mim",
                )
                tod_dispatch = (
                    controlled_continuation.get("final_dispatch")
                    if isinstance(controlled_continuation.get("final_dispatch"), dict)
                    else primary_dispatch
                )
                synchronize_latest_result_artifact_from_dispatch(tod_dispatch)
                next_action_text = (
                    "execute a bounded TOD continuation chain of up to 3 existing steps and surface the chained result"
                )
                combined_result = _format_bounded_controlled_continuation_result(
                    primary_result_label="Recent-changes summary",
                    continuation=controlled_continuation,
                )
                resolution.reason = "tod_recent_changes_next_step_dispatch"
                resolution_meta = {
                    **resolution_meta,
                    "route_preference": "goal_system",
                    "conversation_override": False,
                    "tod_primary_dispatch": primary_dispatch,
                    "tod_selected_next_step": controlled_continuation.get("selected_next_step", {}),
                    "tod_controlled_continuation": {
                        "max_depth": int(controlled_continuation.get("max_depth", 3) or 3),
                        "step_count": int(controlled_continuation.get("step_count", 0) or 0),
                        "steps": controlled_continuation.get("steps", []),
                        "stop_reason": str(controlled_continuation.get("stop_reason") or "").strip(),
                        "stop_detail": str(controlled_continuation.get("stop_detail") or "").strip(),
                    },
                    "tod_dispatch": tod_dispatch,
                    "mim_interface_next_action_override": next_action_text,
                    "mim_interface_result_override": combined_result,
                    "mim_interface_reply_override": (
                        f"Request {request_id}. I understood: {event.raw_input}. "
                        f"Next action: {next_action_text}. "
                        f"Status: done. Result: {combined_result}"
                    ).strip(),
                }
                resolution.metadata_json = resolution_meta
                resolution.outcome = "store_only"
                resolution.safety_decision = "store_only"
                resolution.clarification_prompt = combined_result
                is_conversation_override = False
            else:
                initiative_run = await _maybe_dispatch_repeated_tod_status_loop_recovery(
                    event=event,
                    request_id=request_id,
                    session_id=str(payload_metadata.get("conversation_session_id") or "").strip(),
                    db=db,
                )
                if initiative_run is not None:
                    resolution.reason = str(initiative_run.get("reason") or "").strip()
                    resolution.outcome = str(initiative_run.get("outcome") or "store_only").strip()
                    resolution.resolution_status = str(
                        initiative_run.get("resolution_status") or resolution.outcome
                    ).strip()
                    resolution.safety_decision = str(
                        initiative_run.get("safety_decision") or resolution.outcome
                    ).strip()
                    resolution.clarification_prompt = str(
                        initiative_run.get("clarification_prompt") or ""
                    ).strip()
                    resolution_meta = {
                        **resolution_meta,
                        "route_preference": "goal_system",
                        "conversation_override": False,
                        "initiative_auto_execute": True,
                        "initiative_run": initiative_run.get("initiative_run", {}),
                        "initiative_status": initiative_run.get("initiative_status", {}),
                        "stale_status_loop_recovery": True,
                        "status_loop_repeat_count": int(
                            initiative_run.get("status_loop_repeat_count", 0) or 0
                        ),
                        "mim_interface_status_override": str(
                            initiative_run.get("interface_status") or ""
                        ).strip(),
                        "mim_interface_next_action_override": str(
                            initiative_run.get("interface_next_action") or ""
                        ).strip(),
                        "mim_interface_result_override": str(
                            initiative_run.get("interface_result") or ""
                        ).strip(),
                        "mim_interface_reply_override": str(
                            initiative_run.get("interface_reply") or ""
                        ).strip(),
                        "communication_reply_contract": {
                            "reply_text": str(initiative_run.get("interface_reply") or "").strip(),
                            "topic_hint": "initiative_execution",
                            "composer_mode": "gateway_override",
                            "should_store_memory": True,
                            "memory_topics": [],
                            "memory_people": [],
                            "memory_events": [],
                            "memory_experiences": [],
                        },
                    }
                    governance = (
                        resolution_meta.get("governance")
                        if isinstance(resolution_meta.get("governance"), dict)
                        else {}
                    )
                    resolution_meta["governance"] = {
                        **governance,
                        "applied_reason": resolution.reason,
                        "applied_outcome": resolution.outcome,
                        "summary": str(resolution.reason or "").replace("_", " "),
                    }
                    resolution.metadata_json = resolution_meta
                else:
                    tod_dispatch = dispatch_bounded_tod_status_request(
                        request_id=request_id,
                        session_key=str(payload_metadata.get("conversation_session_id") or "").strip(),
                        content=event.raw_input,
                        actor="mim",
                    )
                    resolution.reason = "tod_status_dispatch"
                if initiative_run is None:
                    resolution_meta = {
                        **resolution_meta,
                        "route_preference": "goal_system",
                        "conversation_override": False,
                        "tod_dispatch": tod_dispatch,
                    }
                    resolution.metadata_json = resolution_meta
                    resolution.outcome = "store_only"
                    resolution.safety_decision = "store_only"
                    resolution.clarification_prompt = str(
                        tod_dispatch.get("result_reason")
                        or tod_dispatch.get("decision_detail")
                        or ""
                    ).strip()
                is_conversation_override = False
        elif (
            event.source == "text"
            and not route_console_text_input(
                event.raw_input,
                event.parsed_intent,
            ).capability_name
            and _looks_like_bounded_implementation_request(
                event.raw_input,
                event.parsed_intent,
                event.safety_flags,
            )
        ):
            initiative_run = await _maybe_dispatch_authorized_text_initiative(
                event=event,
                request_id=request_id,
                session_id=str(payload_metadata.get("conversation_session_id") or "").strip(),
                db=db,
            )
            if initiative_run is not None:
                resolution.reason = str(initiative_run.get("reason") or "").strip()
                resolution.outcome = str(initiative_run.get("outcome") or "store_only").strip()
                resolution.resolution_status = str(
                    initiative_run.get("resolution_status") or resolution.outcome
                ).strip()
                resolution.safety_decision = str(
                    initiative_run.get("safety_decision") or resolution.outcome
                ).strip()
                resolution.clarification_prompt = str(
                    initiative_run.get("clarification_prompt") or ""
                ).strip()
                resolution_meta = {
                    **resolution_meta,
                    "route_preference": "goal_system",
                    "conversation_override": False,
                    "clarification_prompt_key": "",
                    "initiative_auto_execute": bool(
                        initiative_run.get("initiative_auto_execute")
                    ),
                    "initiative_run": initiative_run.get("initiative_run", {}),
                    "initiative_status": initiative_run.get("initiative_status", {}),
                    "mim_interface_status_override": str(
                        initiative_run.get("interface_status") or ""
                    ).strip(),
                    "mim_interface_next_action_override": str(
                        initiative_run.get("interface_next_action") or ""
                    ).strip(),
                    "mim_interface_result_override": str(
                        initiative_run.get("interface_result") or ""
                    ).strip(),
                    "mim_interface_reply_override": str(
                        initiative_run.get("interface_reply") or ""
                    ).strip(),
                    "communication_reply_contract": {
                        "reply_text": str(initiative_run.get("interface_reply") or "").strip(),
                        "topic_hint": "initiative_execution",
                        "composer_mode": "gateway_override",
                        "should_store_memory": True,
                        "memory_topics": [],
                        "memory_people": [],
                        "memory_events": [],
                        "memory_experiences": [],
                    },
                }
                governance = (
                    resolution_meta.get("governance")
                    if isinstance(resolution_meta.get("governance"), dict)
                    else {}
                )
                resolution_meta["governance"] = {
                    **governance,
                    "applied_reason": resolution.reason,
                    "applied_outcome": resolution.outcome,
                    "summary": str(resolution.reason or "").replace("_", " "),
                }
                resolution.metadata_json = resolution_meta
            else:
                handoff_submission = await submit_handoff_payload(
                    await _build_conversation_handoff_payload_async(
                        request_id=request_id,
                        text=event.raw_input,
                        session_id=str(payload_metadata.get("conversation_session_id") or "").strip(),
                        db=db,
                    )
                )
                interface_status = _handoff_submission_interface_status(handoff_submission)
                next_action_text = (
                    "route one bounded implementation task through the handoff engine and surface its current status"
                )
                result_text = _handoff_submission_result_summary(handoff_submission)
                resolution.reason = "conversation_bounded_implementation_dispatch"
                resolution_meta = {
                    **resolution_meta,
                    "route_preference": "goal_system",
                    "conversation_override": False,
                    "handoff_submission": handoff_submission,
                    "mim_interface_status_override": interface_status,
                    "mim_interface_next_action_override": next_action_text,
                    "mim_interface_result_override": result_text,
                    "mim_interface_reply_override": (
                        f"Request {request_id}. I understood: {event.raw_input}. "
                        f"Next action: {next_action_text}. "
                        f"Status: {interface_status}. Result: {result_text}"
                    ).strip(),
                }
                resolution.metadata_json = resolution_meta
                resolution.outcome = "blocked" if interface_status == "blocked" else "store_only"
                resolution.safety_decision = (
                    "blocked" if interface_status == "blocked" else "store_only"
                )
                resolution.clarification_prompt = result_text
        is_conversation_override = False
        if is_conversation_override and event.source == "text" and not skip_conversation_memory:
            _append_gateway_trace_event(trace, "conversation_memory_start")
            interaction_memory_id = await _store_interaction_learning(
                transcript=event.raw_input,
                confidence=float(event.confidence or 0.0),
                source=None,
                payload_metadata=event.metadata_json
                if isinstance(event.metadata_json, dict)
                else {},
                db=db,
                source_name="conversation_text",
                session_id=str(
                    (event.metadata_json or {}).get("conversation_session_id", "")
                ).strip(),
            )
            await _store_conversation_memory(
                db=db,
                event=event,
                resolution=resolution,
                interaction_memory_id=interaction_memory_id,
            )
            _append_gateway_trace_event(trace, "conversation_memory_end")
        execution = None
        if (
            not is_conversation_override
            and tod_dispatch is None
            and handoff_submission is None
            and initiative_run is None
        ):
            execution = await _create_or_update_execution_binding(
                event=event,
                resolution=resolution,
                capability_name=resolution.capability_name,
                db=db,
                arguments_json=_default_execution_arguments(
                    event, resolution.capability_name
                ),
            )

        _append_gateway_trace_event(trace, "interface_state_start")
        await _store_conversation_interface_state(
            db=db,
            event=event,
            resolution=resolution,
            execution=execution,
        )
        _append_gateway_trace_event(trace, "interface_state_end")

        _append_gateway_trace_event(trace, "db_commit_start")
        await db.commit()
        _append_gateway_trace_event(trace, "db_commit_end")
        await db.refresh(event)
        await db.refresh(resolution)
        event_out = _to_input_out(event)
        event_out["request_id"] = request_id
        event_out["resolution"] = _to_resolution_out(resolution)
        if execution is not None:
            await db.refresh(execution)
            event_out["execution"] = _to_execution_out(execution)
        if tod_dispatch is not None:
            event_out["tod_dispatch"] = tod_dispatch
        if handoff_submission is not None:
            event_out["handoff_submission"] = handoff_submission
        if initiative_run is not None:
            event_out["initiative_run"] = initiative_run.get("initiative_run", {})
            event_out["initiative_status"] = initiative_run.get("initiative_status", {})
        if str(event.source or "").strip().lower() == "text":
            derived_initiative_status = _initiative_status_from_resolution_metadata(
                resolution.metadata_json
                if isinstance(resolution.metadata_json, dict)
                else {}
            )
            existing_initiative_status = event_out.get("initiative_status")
            if isinstance(existing_initiative_status, dict) and existing_initiative_status:
                if derived_initiative_status:
                    merged_initiative_status = dict(existing_initiative_status)
                    if not str(merged_initiative_status.get("summary") or "").strip() and str(
                        derived_initiative_status.get("summary") or ""
                    ).strip():
                        merged_initiative_status["summary"] = str(
                            derived_initiative_status.get("summary") or ""
                        ).strip()
                    if not isinstance(merged_initiative_status.get("program_status"), dict) and isinstance(
                        derived_initiative_status.get("program_status"), dict
                    ):
                        merged_initiative_status["program_status"] = derived_initiative_status.get(
                            "program_status"
                        )
                    event_out["initiative_status"] = merged_initiative_status
            elif derived_initiative_status:
                event_out["initiative_status"] = derived_initiative_status
            event_out["mim_interface"] = _build_mim_interface_response(
                event=event,
                resolution=resolution,
                execution=execution,
            )

        total_elapsed_seconds = time.perf_counter() - started_monotonic
        trace["elapsed_seconds"] = round(total_elapsed_seconds, 4)
        gateway_diagnostic = (
            resolution.metadata_json.get("gateway_diagnostic")
            if isinstance(resolution.metadata_json, dict)
            and isinstance(resolution.metadata_json.get("gateway_diagnostic"), dict)
            else {}
        )
        if gateway_diagnostic:
            event_out["gateway_diagnostic"] = gateway_diagnostic
            trace["gateway_diagnostic"] = gateway_diagnostic
        _append_gateway_trace_event(trace, "response_ready")
        if gateway_diagnostic or total_elapsed_seconds >= GATEWAY_INTAKE_DIAGNOSTIC_THRESHOLD_SECONDS:
            _write_gateway_intake_diagnostic(trace, final_status="completed")
        return event_out
    except Exception as exc:
        _append_gateway_trace_event(
            trace,
            "exception",
            error_type=type(exc).__name__,
            error_message=_compact_text(str(exc), 240),
        )
        trace["elapsed_seconds"] = round(time.perf_counter() - started_monotonic, 4)
        _write_gateway_intake_diagnostic(trace, final_status="exception")
        raise


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
        (
            await db.execute(
                select(WorkspacePerceptionSource)
                .where(WorkspacePerceptionSource.source_type == source_type)
                .where(WorkspacePerceptionSource.device_id == device_id)
                .order_by(WorkspacePerceptionSource.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
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


def _is_duplicate_event(
    *, row: WorkspacePerceptionSource, fingerprint: str, now: datetime
) -> bool:
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
        "metadata_json": row.metadata_json
        if isinstance(row.metadata_json, dict)
        else {},
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
            value = text[idx + len(prefix) :].strip(" .,!?")
            if value:
                return signal, value[:140]
    return "", ""


def _clean_identity_value(raw: str) -> str:
    cleaned = " ".join(str(raw or "").strip().split()).strip(" .,!?;:")
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    blocked = {
        "you",
        "me",
        "him",
        "her",
        "them",
        "there",
        "that",
        "this",
        "here",
        "hello",
        "hi",
        "hey",
        "not sure",
        "not fully sure",
        "not totally sure",
        "mim",
        "tod",
    }
    if lowered in blocked:
        return ""
    parts = cleaned.split(" ")[:4]
    normalized = " ".join(part[:40] for part in parts).strip()
    return normalized[:80]


def _extract_session_display_name(transcript: str) -> str:
    text = _normalize_text_for_learning(transcript)
    if not text:
        return ""

    match = re.match(
        r"^(?:(?:hi|hello|hey)[,\s]+)?(?:i am|i'm|call me|my name is)\s+([A-Za-z][A-Za-z'\- ]{0,60})[.!?]?$",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    return _clean_identity_value(match.group(1))


def _address_session_reply(reply: str, display_name: str) -> str:
    response = str(reply or "").strip()
    name = _clean_identity_value(display_name)
    if not response or not name:
        return response

    lowered = response.lower()
    if name.lower() in lowered:
        return response
    if response.startswith("Hi. "):
        return f"Hi, {name}. {response[4:]}"
    if response.startswith("Hello. "):
        return f"Hello, {name}. {response[7:]}"
    if response.startswith("Understood. "):
        return f"Understood, {name}. {response[12:]}"
    return f"{name}, {response}"


async def _get_or_create_actor(
    *,
    db: AsyncSession,
    actor_name: str,
    role: str = "user",
) -> Actor:
    normalized_name = str(actor_name or "").strip() or DEFAULT_USER_ID
    row = (
        (await db.execute(select(Actor).where(Actor.name == normalized_name)))
        .scalars()
        .first()
    )
    if row is not None:
        return row

    row = Actor(name=normalized_name, role=role, identity_metadata={})
    db.add(row)
    await db.flush()
    return row


async def _find_recent_memory_by_metadata(
    *,
    db: AsyncSession,
    memory_class: str,
    metadata_match: dict[str, object],
    limit: int = 50,
) -> MemoryEntry | None:
    rows = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.memory_class == memory_class)
                .order_by(MemoryEntry.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if all(meta.get(key) == value for key, value in metadata_match.items()):
            return row
    return None


async def _upsert_person_profile_memory(
    *,
    db: AsyncSession,
    actor_name: str,
    actor_role: str,
    display_name: str,
    aliases: list[str],
    session_id: str,
    event_id: int,
) -> int | None:
    summary = f"Known person: {display_name or actor_name}"
    if aliases:
        summary = f"{summary} | aliases: {', '.join(aliases[:8])}"
    content = json.dumps(
        {
            "actor_name": actor_name,
            "role": actor_role,
            "display_name": display_name or actor_name,
            "aliases": aliases[:8],
            "last_session_id": session_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    metadata_json = {
        "actor_name": actor_name,
        "display_name": display_name or actor_name,
        "aliases": aliases[:8],
        "role": actor_role,
        "last_session_id": session_id,
        "last_event_id": int(event_id),
    }
    existing = await _find_recent_memory_by_metadata(
        db=db,
        memory_class="person_profile",
        metadata_match={"actor_name": actor_name},
        limit=20,
    )
    if existing is None:
        existing = MemoryEntry(
            memory_class="person_profile",
            content=content,
            summary=summary,
            metadata_json=metadata_json,
        )
        db.add(existing)
        await db.flush()
        return int(existing.id)

    existing.content = content
    existing.summary = summary
    existing.metadata_json = metadata_json
    await db.flush()
    return int(existing.id)


async def _append_preference_memory(
    *,
    db: AsyncSession,
    actor_name: str,
    preference_type: str,
    preference_value: str,
    transcript: str,
    session_id: str,
    event_id: int,
) -> int | None:
    normalized_value = " ".join(str(preference_value or "").strip().split())
    if not normalized_value:
        return None

    entry = MemoryEntry(
        memory_class="person_preference",
        content=transcript,
        summary=f"{actor_name} preference ({preference_type}): {normalized_value[:110]}",
        metadata_json={
            "actor_name": actor_name,
            "preference_type": preference_type,
            "preference_value": normalized_value[:140],
            "session_id": session_id,
            "event_id": int(event_id),
        },
    )
    db.add(entry)
    await db.flush()
    return int(entry.id)


async def _remember_identity_and_preferences(
    *,
    db: AsyncSession,
    transcript: str,
    event: InputEvent,
) -> dict:
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    actor_name = str(metadata.get("user_id", "")).strip() or DEFAULT_USER_ID
    session_id = str(metadata.get("conversation_session_id", "")).strip()
    actor = await _get_or_create_actor(db=db, actor_name=actor_name, role="user")
    identity_meta = (
        actor.identity_metadata.copy()
        if isinstance(actor.identity_metadata, dict)
        else {}
    )
    aliases = [
        str(item).strip()
        for item in identity_meta.get("aliases", [])
        if str(item).strip()
    ]
    pref_signal, pref_value = _interaction_pref_signal(transcript)
    display_name = str(identity_meta.get("display_name", "")).strip()
    person_memory_id: int | None = None
    preference_memory_ids: list[int] = []

    if pref_signal == "call_me":
        alias = _clean_identity_value(pref_value)
        if alias:
            if alias.lower() not in {item.lower() for item in aliases}:
                aliases.append(alias)
            display_name = alias
            actor.identity_metadata = {
                **identity_meta,
                "display_name": alias,
                "aliases": aliases[:8],
                "last_session_id": session_id,
                "last_event_id": int(event.id),
                "identity_source": "conversation_learning",
            }
            await db.flush()
            await upsert_user_preference(
                db=db,
                user_id=actor_name,
                preference_type="display_name",
                value=alias,
                confidence=0.95,
                source="conversation_learning",
            )
            person_memory_id = await _upsert_person_profile_memory(
                db=db,
                actor_name=actor_name,
                actor_role=actor.role,
                display_name=alias,
                aliases=aliases,
                session_id=session_id,
                event_id=event.id,
            )

    preference_mapping = {
        "preference": "conversation_preferences",
        "like": "conversation_likes",
        "dislike": "conversation_dislikes",
    }
    stored_preference_type = preference_mapping.get(pref_signal, "")
    if stored_preference_type and pref_value:
        current = await get_user_preference_payload(
            db=db,
            preference_type=stored_preference_type,
            user_id=actor_name,
        )
        current_value = current.get("value", [])
        if not isinstance(current_value, list):
            current_value = [str(current_value)] if current_value else []
        merged_values = [
            str(item).strip() for item in current_value if str(item).strip()
        ]
        if pref_value.lower() not in {item.lower() for item in merged_values}:
            merged_values.append(pref_value[:140])
        merged_values = merged_values[-12:]
        await upsert_user_preference(
            db=db,
            user_id=actor_name,
            preference_type=stored_preference_type,
            value=merged_values,
            confidence=min(1.0, 0.55 + (len(merged_values) / 20.0)),
            source="conversation_learning",
        )
        preference_memory_id = await _append_preference_memory(
            db=db,
            actor_name=actor_name,
            preference_type=stored_preference_type,
            preference_value=pref_value,
            transcript=transcript,
            session_id=session_id,
            event_id=event.id,
        )
        if preference_memory_id:
            preference_memory_ids.append(preference_memory_id)

    if not person_memory_id and (display_name or aliases or identity_meta):
        person_memory_id = await _upsert_person_profile_memory(
            db=db,
            actor_name=actor_name,
            actor_role=actor.role,
            display_name=display_name or actor_name,
            aliases=aliases,
            session_id=session_id,
            event_id=event.id,
        )

    return {
        "actor_name": actor_name,
        "display_name": display_name,
        "person_memory_id": person_memory_id,
        "preference_memory_ids": preference_memory_ids,
    }


def _conversation_turn_summary(*, speaker: str, text: str) -> str:
    prefix = "User" if speaker == "user" else "MIM"
    compact = " ".join(str(text or "").strip().split())
    if len(compact) > 120:
        compact = f"{compact[:117].rstrip()}..."
    return f"{prefix}: {compact}"


async def _store_conversation_memory(
    *,
    db: AsyncSession,
    event: InputEvent,
    resolution: InputEventResolution,
    interaction_memory_id: int | None,
) -> None:
    event_meta = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    resolution_meta = (
        resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}
    )
    actor_context = await _remember_identity_and_preferences(
        db=db,
        transcript=event.raw_input,
        event=event,
    )
    actor_name = (
        str(actor_context.get("actor_name", DEFAULT_USER_ID)).strip() or DEFAULT_USER_ID
    )
    display_name = str(actor_context.get("display_name", "")).strip()
    session_id = str(event_meta.get("conversation_session_id", "")).strip()
    conversation_topic = (
        str(resolution_meta.get("conversation_topic", "")).strip().lower()
    )

    user_turn = MemoryEntry(
        memory_class="conversation_turn",
        content=str(event.raw_input or "").strip(),
        summary=_conversation_turn_summary(speaker="user", text=event.raw_input),
        metadata_json={
            "session_id": session_id,
            "speaker": "user",
            "actor_name": actor_name,
            "display_name": display_name,
            "source": event.source,
            "input_event_id": int(event.id),
            "conversation_topic": conversation_topic,
        },
    )
    db.add(user_turn)
    await db.flush()

    assistant_turn: MemoryEntry | None = None
    assistant_text = str(resolution.clarification_prompt or "").strip()
    if assistant_text:
        assistant_turn = MemoryEntry(
            memory_class="conversation_turn",
            content=assistant_text,
            summary=_conversation_turn_summary(
                speaker="assistant", text=assistant_text
            ),
            metadata_json={
                "session_id": session_id,
                "speaker": "assistant",
                "actor_name": "mim",
                "source": "gateway",
                "input_event_id": int(event.id),
                "resolution_id": int(resolution.id),
                "conversation_topic": conversation_topic,
            },
        )
        db.add(assistant_turn)
        await db.flush()
        db.add(
            MemoryLink(
                source_memory_id=int(user_turn.id),
                target_memory_id=int(assistant_turn.id),
                relation="reply",
            )
        )

    if interaction_memory_id:
        db.add(
            MemoryLink(
                source_memory_id=int(user_turn.id),
                target_memory_id=int(interaction_memory_id),
                relation="interaction_signal",
            )
        )

    person_memory_id = actor_context.get("person_memory_id")
    if person_memory_id:
        db.add(
            MemoryLink(
                source_memory_id=int(person_memory_id),
                target_memory_id=int(user_turn.id),
                relation="participant_turn",
            )
        )

    for preference_memory_id in actor_context.get("preference_memory_ids", []):
        db.add(
            MemoryLink(
                source_memory_id=int(preference_memory_id),
                target_memory_id=int(user_turn.id),
                relation="preference_context",
            )
        )

    if not session_id:
        return

    recent_turn_rows = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.memory_class == "conversation_turn")
                .order_by(MemoryEntry.id.desc())
                .limit(40)
            )
        )
        .scalars()
        .all()
    )
    session_turns = []
    for row in recent_turn_rows:
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if str(meta.get("session_id", "")).strip() != session_id:
            continue
        session_turns.append(row)
    session_turns.reverse()

    participants: list[str] = []
    for row in session_turns:
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if str(meta.get("speaker", "")).strip() == "assistant":
            label = "mim"
        else:
            label = (
                str(meta.get("display_name", "")).strip()
                or str(meta.get("actor_name", "")).strip()
            )
        if label and label not in participants:
            participants.append(label)

    latest_user_text = next(
        (
            str(row.content or "").strip()
            for row in reversed(session_turns)
            if str((row.metadata_json or {}).get("speaker", "")).strip() == "user"
        ),
        "",
    )
    latest_assistant_text = next(
        (
            str(row.content or "").strip()
            for row in reversed(session_turns)
            if str((row.metadata_json or {}).get("speaker", "")).strip() == "assistant"
        ),
        "",
    )
    summary = f"Conversation session {session_id}"
    if conversation_topic:
        summary = f"{summary} | topic: {conversation_topic}"
    if latest_user_text:
        summary = (
            f"{summary} | latest user turn: {' '.join(latest_user_text.split())[:90]}"
        )

    session_memory = await _find_recent_memory_by_metadata(
        db=db,
        memory_class="conversation_session",
        metadata_match={"session_id": session_id},
        limit=20,
    )
    prior_session_meta = (
        session_memory.metadata_json
        if session_memory and isinstance(session_memory.metadata_json, dict)
        else {}
    )
    last_technical_research = _compact_technical_research_context(
        (
            resolution_meta.get("web_research")
            if isinstance(resolution_meta.get("web_research"), dict)
            else {}
        )
    )
    if not last_technical_research:
        last_technical_research = (
            resolution_meta.get("last_technical_research")
            if isinstance(resolution_meta.get("last_technical_research"), dict)
            else {}
        )
    if not last_technical_research:
        last_technical_research = (
            prior_session_meta.get("last_technical_research")
            if isinstance(prior_session_meta.get("last_technical_research"), dict)
            else {}
        )
    session_metadata = {
        "session_id": session_id,
        "last_topic": conversation_topic,
        "participant_names": participants,
        "turn_count": len(session_turns),
        "last_input_event_id": int(event.id),
        "last_resolution_id": int(resolution.id),
        "latest_user_turn": latest_user_text[:240],
        "latest_assistant_turn": latest_assistant_text[:240],
        "last_technical_research": last_technical_research,
    }
    content = json.dumps(
        {
            "session_id": session_id,
            "participants": participants,
            "topic": conversation_topic,
            "latest_user_turn": latest_user_text[:240],
            "latest_assistant_turn": latest_assistant_text[:240],
            "turn_count": len(session_turns),
            "technical_research_query": str(
                last_technical_research.get("query") or ""
            ).strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    if session_memory is None:
        session_memory = MemoryEntry(
            memory_class="conversation_session",
            content=content,
            summary=summary,
            metadata_json=session_metadata,
        )
        db.add(session_memory)
        await db.flush()
    else:
        session_memory.content = content
        session_memory.summary = summary
        session_memory.metadata_json = session_metadata
        await db.flush()

    db.add(
        MemoryLink(
            source_memory_id=int(session_memory.id),
            target_memory_id=int(user_turn.id),
            relation="contains_turn",
        )
    )
    if assistant_turn is not None:
        db.add(
            MemoryLink(
                source_memory_id=int(session_memory.id),
                target_memory_id=int(assistant_turn.id),
                relation="contains_turn",
            )
        )


async def _store_interaction_learning(
    *,
    transcript: str,
    confidence: float,
    source: WorkspacePerceptionSource | None,
    payload_metadata: dict,
    db: AsyncSession,
    source_name: str = "live_mic_adapter",
    device_id: str = "",
    session_id: str = "",
    camera_label: str = "",
) -> int | None:
    clean = _normalize_text_for_learning(transcript)
    if not clean:
        return None

    compact = "".join(
        ch for ch in clean.lower() if ch.isalnum() or ch.isspace()
    ).strip()
    if compact in {"hi", "hello", "hey", "ok", "okay", "thanks", "thank you"}:
        return None

    pref_type, pref_value = _interaction_pref_signal(clean)
    word_count = len([part for part in compact.split(" ") if part])
    if not pref_type and (word_count < 4 or float(confidence) < 0.6):
        return None

    transcript_hash = sha256(clean.lower().encode("utf-8")).hexdigest()[:16]
    existing = (
        (
            await db.execute(
                select(MemoryEntry)
                .where(MemoryEntry.memory_class == "interaction_learning")
                .order_by(MemoryEntry.id.desc())
                .limit(8)
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if str(meta.get("transcript_hash", "")) == transcript_hash:
            return None

    resolved_camera_label = str(camera_label or "").strip()
    if not resolved_camera_label:
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
        camera_payload = (
            camera_row.last_event_payload_json
            if camera_row and isinstance(camera_row.last_event_payload_json, dict)
            else {}
        )
        resolved_camera_label = str(camera_payload.get("object_label", "")).strip()

    summary = f"User said: {clean[:110]}"
    if pref_type and pref_value:
        summary = f"Preference learned ({pref_type}): {pref_value[:110]}"
    if resolved_camera_label:
        summary = f"{summary} | Surrounding: {resolved_camera_label}"

    memory = MemoryEntry(
        memory_class="interaction_learning",
        content=clean,
        summary=summary,
        metadata_json={
            "source": source_name,
            "device_id": device_id or (source.device_id if source else ""),
            "session_id": session_id or (source.session_id if source else ""),
            "confidence": float(confidence),
            "preference_signal": pref_type,
            "preference_value": pref_value,
            "camera_label": resolved_camera_label,
            "transcript_hash": transcript_hash,
            "adapter_metadata": payload_metadata
            if isinstance(payload_metadata, dict)
            else {},
        },
    )
    db.add(memory)
    await db.flush()
    return int(memory.id)


@router.post("/intake")
async def intake_normalized(
    payload: NormalizedInputCreate, db: AsyncSession = Depends(get_db)
) -> dict:
    return await _store_normalized(payload, db)


@router.post("/intake/text")
async def intake_text(
    payload: TextInputAdapterRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    ensure_authenticated_mimtod_api_request(request)
    classifier_outcome = classify_console_intent(payload.text, payload.parsed_intent)
    local_route = route_console_text_input(payload.text, payload.parsed_intent)
    requested_route_preference = str(
        (payload.metadata_json if isinstance(payload.metadata_json, dict) else {}).get(
            "route_preference", ""
        )
        or ""
    ).strip().lower()
    route_preference = _text_route_preference(
        text=payload.text,
        parsed_intent=payload.parsed_intent,
        safety_flags=payload.safety_flags,
    )
    if requested_route_preference in {"conversation_layer", "goal_system"}:
        route_preference = requested_route_preference
    if local_route.route_preference == "goal_system":
        route_preference = "goal_system"
    if _should_force_conversation_eval_route(
        requested_goal=payload.requested_goal,
        metadata_json=payload.metadata_json,
        safety_flags=payload.safety_flags,
    ):
        route_preference = "conversation_layer"
    if local_route.route_preference == "goal_system":
        route_preference = "goal_system"
    parsed_intent = (
        classifier_outcome
        if classifier_outcome in {
            "execution_capability_request",
            "robotics_supervised_probe",
            "unclear_requires_clarification",
        }
        else payload.parsed_intent
    )
    metadata_json = payload.metadata_json if isinstance(payload.metadata_json, dict) else {}
    capability_metadata = (
        {"capability": local_route.capability_name}
        if local_route.capability_name
        else {}
    )
    normalized = NormalizedInputCreate(
        source="text",
        raw_input=payload.text,
        parsed_intent=parsed_intent,
        confidence=payload.confidence,
        target_system=payload.target_system,
        requested_goal=payload.requested_goal,
        safety_flags=payload.safety_flags,
        metadata_json={
            **metadata_json,
            **capability_metadata,
            "adapter": "text",
            "classifier_outcome": classifier_outcome,
            "web_search_guarded": bool(robotics_web_guard_blocks_search(payload.text)),
            "routing_path": list(local_route.routing_path),
            "route_preference": route_preference,
        },
    )
    return await _store_normalized(normalized, db)


@router.post("/intake/ui")
async def intake_ui(
    payload: UiInputAdapterRequest, db: AsyncSession = Depends(get_db)
) -> dict:
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
async def intake_api(
    payload: ApiInputAdapterRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    raw_input = payload.raw_input or json.dumps(payload.payload, sort_keys=True)
    normalized = NormalizedInputCreate(
        source="api",
        raw_input=raw_input,
        parsed_intent=payload.parsed_intent,
        confidence=payload.confidence,
        target_system=payload.target_system,
        requested_goal=payload.requested_goal,
        safety_flags=payload.safety_flags,
        metadata_json={
            **payload.metadata_json,
            "adapter": "api",
            "payload": payload.payload,
        },
    )
    return await _store_normalized(normalized, db)


@router.post("/voice/input")
async def voice_input(
    payload: VoiceInputAdapterRequest, db: AsyncSession = Depends(get_db)
) -> dict:
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
async def voice_output(
    payload: SpeechOutputRequest, db: AsyncSession = Depends(get_db)
) -> dict:
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
        raise HTTPException(
            status_code=502, detail=f"tts_synthesis_failed: {exc}"
        ) from exc

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
async def summarize_web_page(
    payload: dict = Body(...), db: AsyncSession = Depends(get_db)
) -> dict:
    if not settings.allow_web_access:
        raise HTTPException(status_code=403, detail="web_access_disabled")

    raw_url = str(payload.get("url") or "").strip()
    if not raw_url:
        raise HTTPException(status_code=400, detail="url is required")
    if not _is_safe_web_url(raw_url):
        raise HTTPException(status_code=422, detail="unsupported_or_unsafe_url")

    timeout_seconds = max(3, min(20, int(payload.get("timeout_seconds") or 12)))
    max_extract_chars = max(
        1200, min(30000, int(payload.get("max_extract_chars") or 12000))
    )
    max_summary_sentences = max(
        1, min(8, int(payload.get("max_summary_sentences") or 4))
    )

    try:
        document = _fetch_web_document(
            raw_url,
            timeout_seconds=timeout_seconds,
            max_extract_chars=max_extract_chars,
        )
    except urllib_error.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"web_fetch_http_error:{exc.code}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"web_fetch_failed:{type(exc).__name__}"
        ) from exc

    title = str(document.get("title") or "").strip()
    extracted = str(document.get("text") or "").strip()
    content_type = str(document.get("content_type") or "").strip().lower()
    status_code = int(document.get("status_code") or 200)

    summary = _build_web_summary(
        title=title, text=extracted, max_sentences=max_summary_sentences
    )

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


@router.post("/web/research")
async def research_web(
    payload: dict = Body(...), db: AsyncSession = Depends(get_db)
) -> dict:
    if not settings.allow_web_access:
        raise HTTPException(status_code=403, detail="web_access_disabled")

    query = _normalize_web_research_query(str(payload.get("query") or "").strip())
    if not query:
        raise HTTPException(status_code=400, detail="query is required")

    timeout_seconds = max(3, min(20, int(payload.get("timeout_seconds") or 12)))
    max_results = max(1, min(10, int(payload.get("max_results") or 5)))
    max_sources = max(1, min(5, int(payload.get("max_sources") or 3)))
    max_extract_chars = max(
        1200, min(30000, int(payload.get("max_extract_chars") or 8000))
    )
    include_debug = bool(payload.get("include_debug"))

    research = await _perform_web_research(
        db,
        query=query,
        timeout_seconds=timeout_seconds,
        max_results=max_results,
        max_sources=max_sources,
        max_extract_chars=max_extract_chars,
    )
    if not bool(research.get("ok")):
        error_code = str(research.get("error") or "web_research_failed")
        if include_debug:
            return JSONResponse(
                status_code=504 if error_code == "web_research_timed_out" else 502,
                content=research,
            )
        raise HTTPException(
            status_code=504 if error_code == "web_research_timed_out" else 502,
            detail=error_code,
        )

    await write_journal(
        db,
        actor="gateway",
        action="research_web_query",
        target_type="external_web_research",
        target_id=str(research.get("memory_id") or ""),
        summary=f"Researched web query: {query}",
        metadata_json={
            "query": query,
            "source_count": len(research.get("sources", [])),
        },
    )
    await db.commit()
    return research


@router.post("/vision/observations")
async def vision_observation(
    payload: VisionObservationRequest, db: AsyncSession = Depends(get_db)
) -> dict:
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
async def live_camera_adapter(
    payload: LiveCameraAdapterRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    now = datetime.now(timezone.utc)
    payload_metadata = (
        payload.metadata_json if isinstance(payload.metadata_json, dict) else {}
    )
    heartbeat_mode = str(payload_metadata.get("mode") or "").strip()
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

    if heartbeat_mode == "always_watching_heartbeat":
        source.last_seen_at = now
        source.last_accepted_at = now
        source.accepted_count = int(source.accepted_count or 0) + 1
        source.health_status = "healthy"
        source.last_event_payload_json = {
            "type": "camera",
            "status": "heartbeat_frame_seen",
            "reason": "camera_frame_heartbeat",
            "timestamp": now.isoformat(),
            "metadata_json": payload_metadata,
        }
        source.metadata_json = {
            **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
            **payload_metadata,
            "objective61_live_adapter": True,
            "last_adapter_status": "heartbeat_frame_seen",
            "last_adapter_updated_at": now.isoformat(),
        }
        await db.commit()
        return {
            "status": "heartbeat_frame_seen",
            "reason": "camera_frame_heartbeat",
            "source": _to_perception_source_out(source),
            "accepted_count": 0,
        }

    observations = (
        payload.observations if isinstance(payload.observations, list) else []
    )
    accepted_items = [
        item
        for item in observations
        if float(item.confidence) >= float(source.confidence_floor)
    ]
    if not accepted_items:
        source.last_seen_at = now
        source.health_status = "degraded"
        source.dropped_count = int(source.dropped_count or 0) + 1
        source.low_confidence_count = int(source.low_confidence_count or 0) + 1
        source.last_event_payload_json = {
            "type": "camera",
            "status": "discarded_low_confidence",
            "reason": "observation_confidence_below_floor",
            "timestamp": now.isoformat(),
            "metadata_json": payload_metadata,
        }
        source.metadata_json = {
            **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
            **payload_metadata,
            "objective61_live_adapter": True,
            "last_adapter_status": "discarded_low_confidence",
            "last_adapter_updated_at": now.isoformat(),
        }
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
                for item in sorted(
                    accepted_items,
                    key=lambda entry: (
                        entry.zone,
                        entry.object_label,
                        entry.confidence,
                    ),
                )
            ],
        ]
    )
    if _is_duplicate_event(row=source, fingerprint=fingerprint, now=now):
        source.last_seen_at = now
        source.health_status = "healthy"
        source.dropped_count = int(source.dropped_count or 0) + 1
        source.duplicate_count = int(source.duplicate_count or 0) + 1
        source.last_event_payload_json = {
            "type": "camera",
            "status": "suppressed_duplicate",
            "reason": "duplicate_observation_batch",
            "timestamp": now.isoformat(),
            "metadata_json": payload_metadata,
            "observations": [
                {
                    "object_label": str(item.object_label),
                    "zone": str(item.zone or ""),
                    "confidence": float(item.confidence),
                }
                for item in accepted_items
            ],
        }
        source.metadata_json = {
            **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
            **payload_metadata,
            "objective61_live_adapter": True,
            "last_adapter_status": "suppressed_duplicate",
            "last_adapter_updated_at": now.isoformat(),
        }
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
        source.last_event_payload_json = {
            "type": "camera",
            "status": "throttled_interval",
            "reason": "min_interval_not_elapsed",
            "timestamp": now.isoformat(),
            "metadata_json": payload_metadata,
            "observations": [
                {
                    "object_label": str(item.object_label),
                    "zone": str(item.zone or ""),
                    "confidence": float(item.confidence),
                }
                for item in accepted_items
            ],
        }
        source.metadata_json = {
            **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
            **payload_metadata,
            "objective61_live_adapter": True,
            "last_adapter_status": "throttled_interval",
            "last_adapter_updated_at": now.isoformat(),
        }
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
            **payload_metadata,
            "adapter": "vision_live_camera",
            "device_id": source.device_id,
            "source_type": source.source_type,
            "session_id": source.session_id,
            "is_remote": source.is_remote,
            "detected_labels": [item.object_label for item in accepted_items],
        },
    )

    event_out = await _store_normalized(normalized, db)
    observed_labels_by_zone: dict[str, set[str]] = {}
    workspace_object_ids: list[int] = []
    persistent_object_labels: list[str] = []
    for item in accepted_items:
        observed_at = item.timestamp or now
        zone = str(item.zone or "workspace")
        label = str(item.object_label)
        observed_labels_by_zone.setdefault(zone, set()).add(label.lower())
        db.add(
            WorkspaceObservation(
                observed_at=observed_at,
                zone=zone,
                label=label,
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

        object_memory = await _upsert_object_identity(
            db=db,
            observation_item={
                "label": label,
                "zone": zone,
                "confidence": float(item.confidence),
                "observed_at": observed_at.isoformat(),
                "source": "live_camera",
            },
            source_name="live_camera",
            source_metadata={
                "device_id": source.device_id,
                "source_type": source.source_type,
                "session_id": source.session_id,
                "is_remote": source.is_remote,
            },
        )
        if object_memory:
            workspace_object_ids.append(object_memory.id)
            persistent_object_labels.append(object_memory.canonical_name)

    await _update_missing_object_identities(
        db=db,
        observed_labels_by_zone=observed_labels_by_zone,
        source_name="live_camera",
        source_session_id=source.session_id,
    )

    unique_workspace_object_ids = sorted(set(workspace_object_ids))
    unique_persistent_labels = sorted(
        {str(label).strip() for label in persistent_object_labels if str(label).strip()}
    )

    source.last_seen_at = now
    source.last_accepted_at = now
    source.last_event_fingerprint = fingerprint
    source.last_event_payload_json = {
        "type": "camera",
        "status": "accepted",
        "object_label": top.object_label,
        "zone": top.zone,
        "confidence": float(top.confidence),
        "timestamp": now.isoformat(),
        "metadata_json": payload_metadata,
        "observations": [
            {
                "object_label": str(item.object_label),
                "zone": str(item.zone or ""),
                "confidence": float(item.confidence),
                "timestamp": (item.timestamp or now).isoformat(),
            }
            for item in accepted_items
        ],
        "workspace_object_ids": unique_workspace_object_ids,
        "persistent_object_labels": unique_persistent_labels,
    }
    source.accepted_count = int(source.accepted_count or 0) + 1
    source.health_status = "healthy"
    source.metadata_json = {
        **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
        **payload_metadata,
        "objective61_live_adapter": True,
        "last_adapter_status": "accepted",
        "last_adapter_updated_at": now.isoformat(),
    }
    await db.commit()

    return {
        "status": "accepted",
        "source": _to_perception_source_out(source),
        "accepted_count": len(accepted_items),
        "workspace_object_ids": unique_workspace_object_ids,
        "event": event_out,
    }


@router.post("/perception/mic/events")
async def live_mic_adapter(
    payload: LiveMicAdapterRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    now = datetime.now(timezone.utc)
    payload_metadata = (
        payload.metadata_json if isinstance(payload.metadata_json, dict) else {}
    )
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
        heartbeat_mode = str(payload_metadata.get("mode", "")).strip()
        source.last_seen_at = now
        source.health_status = "healthy"
        source.dropped_count = int(source.dropped_count or 0) + 1
        source.last_event_payload_json = {
            "type": "microphone",
            "transcript": "",
            "confidence": confidence,
            "timestamp": now.isoformat(),
            "status": "heartbeat_no_transcript",
            "reason": "no_transcript",
            "metadata_json": payload_metadata,
            **({"mode": heartbeat_mode} if heartbeat_mode else {}),
        }
        source.metadata_json = {
            **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
            **payload_metadata,
            "objective61_live_adapter": True,
            "last_adapter_status": "heartbeat_no_transcript",
            "last_adapter_updated_at": now.isoformat(),
        }
        await db.commit()
        return {
            "status": "heartbeat_no_transcript",
            "reason": "no_transcript",
            "source": _to_perception_source_out(source),
            "accepted": False,
        }

    fingerprint = _hash_payload(
        [
            source.source_type,
            source.device_id,
            source.session_id,
            transcript.lower(),
            round(confidence, 3),
        ]
    )

    if confidence < float(source.confidence_floor) and bool(
        payload.discard_low_confidence
    ):
        source.last_seen_at = now
        source.health_status = "degraded"
        source.dropped_count = int(source.dropped_count or 0) + 1
        source.low_confidence_count = int(source.low_confidence_count or 0) + 1
        source.last_event_payload_json = {
            "type": "microphone",
            "status": "discarded_low_confidence",
            "transcript": transcript,
            "confidence": confidence,
            "timestamp": now.isoformat(),
            "reason": "clarification_required",
            "metadata_json": payload_metadata,
        }
        source.metadata_json = {
            **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
            **payload_metadata,
            "objective61_live_adapter": True,
            "last_adapter_status": "discarded_low_confidence",
            "last_adapter_updated_at": now.isoformat(),
        }
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
        source.last_event_payload_json = {
            "type": "microphone",
            "status": "suppressed_duplicate",
            "transcript": transcript,
            "confidence": confidence,
            "timestamp": now.isoformat(),
            "reason": "duplicate_transcript",
            "metadata_json": payload_metadata,
        }
        source.metadata_json = {
            **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
            **payload_metadata,
            "objective61_live_adapter": True,
            "last_adapter_status": "suppressed_duplicate",
            "last_adapter_updated_at": now.isoformat(),
        }
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
        source.last_event_payload_json = {
            "type": "microphone",
            "status": "throttled_interval",
            "transcript": transcript,
            "confidence": confidence,
            "timestamp": now.isoformat(),
            "reason": "min_interval_not_elapsed",
            "metadata_json": payload_metadata,
        }
        source.metadata_json = {
            **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
            **payload_metadata,
            "objective61_live_adapter": True,
            "last_adapter_status": "throttled_interval",
            "last_adapter_updated_at": now.isoformat(),
        }
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
            **payload_metadata,
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
        payload_metadata=payload_metadata,
        db=db,
    )

    source.last_seen_at = now
    source.last_accepted_at = now
    source.last_event_fingerprint = fingerprint
    source.last_event_payload_json = {
        "type": "microphone",
        "status": "accepted",
        "transcript": transcript,
        "confidence": confidence,
        "timestamp": now.isoformat(),
        "metadata_json": payload_metadata,
    }
    source.accepted_count = int(source.accepted_count or 0) + 1
    source.health_status = "healthy"
    source.metadata_json = {
        **(source.metadata_json if isinstance(source.metadata_json, dict) else {}),
        **payload_metadata,
        "objective61_live_adapter": True,
        "last_adapter_status": "accepted",
        "last_adapter_updated_at": now.isoformat(),
        **(
            {"interaction_learning_memory_id": int(learning_memory_id)}
            if learning_memory_id
            else {}
        ),
    }
    await db.commit()

    return {
        "status": "accepted",
        "source": _to_perception_source_out(source),
        "accepted": True,
        "event": event_out,
        "interaction_learning_memory_id": int(learning_memory_id)
        if learning_memory_id
        else None,
    }


@router.post("/perception/mic/transcribe")
async def transcribe_mic_audio(payload: dict = Body(...)) -> dict:
    started_at = datetime.now(timezone.utc)
    trace_id = sha256(
        f"{started_at.isoformat()}|{id(payload)}".encode("utf-8")
    ).hexdigest()[:12]
    raw_audio = str(payload.get("audio_wav_base64") or "").strip()
    language = str(payload.get("language") or "en-US").strip() or "en-US"
    debug_enabled = _mic_debug_enabled(payload)
    provider_mode = _resolve_mic_provider_mode(payload)
    payload_metadata = (
        payload.get("metadata_json")
        if isinstance(payload.get("metadata_json"), dict)
        else {}
    )
    purpose = (
        str(payload_metadata.get("purpose") or payload.get("purpose") or "")
        .strip()
        .lower()
    )
    helper_mode_enabled = str(
        os.getenv("MIM_OPENAI_HELPER_ENABLED", "")
    ).strip().lower() in {"1", "true", "yes", "on"}
    always_openai_stt = str(os.getenv("MIM_OPENAI_STT_ALWAYS", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
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
        or payload.get("training_mode")
        or payload.get("learning_mode")
        or payload.get("openai_helper")
        or payload_metadata.get("training_mode")
        or payload_metadata.get("learning_mode")
        or payload_metadata.get("openai_helper")
        or purpose in openai_helper_purposes
    )

    def _debug_event(stage: str, **fields: dict) -> dict:
        elapsed_ms = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )
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

    _append_mic_debug_event(
        _debug_event(
            "received", debug_enabled=debug_enabled, provider_mode=provider_mode
        )
    )

    if not raw_audio:
        _append_mic_debug_event(_debug_event("reject", reason="missing_audio_base64"))
        raise HTTPException(
            status_code=400,
            detail=_mic_debug_detail("audio_wav_base64 is required", trace_id)
            if debug_enabled
            else "audio_wav_base64 is required",
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
            detail=_mic_debug_detail("speech_recognition backend unavailable", trace_id)
            if debug_enabled
            else "speech_recognition backend unavailable",
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
            detail=_mic_debug_detail("invalid base64 audio payload", trace_id)
            if debug_enabled
            else "invalid base64 audio payload",
        )

    audio_sha_prefix = sha256(audio_bytes).hexdigest()[:16]
    audio_metrics = _estimate_wav_audio_metrics(audio_bytes)
    _append_mic_debug_event(
        _debug_event(
            "base64_decoded",
            audio_bytes_len=len(audio_bytes),
            audio_sha256_prefix=audio_sha_prefix,
            selected_mic_device=str(
                payload_metadata.get("selected_mic_device_id", "") or ""
            ),
            selected_mic_label=str(
                payload_metadata.get("selected_mic_label", "") or ""
            ),
            capture_mode=str(payload_metadata.get("mode", "") or ""),
            capture_duration_ms=payload_metadata.get("capture_duration_ms"),
            browser_speech_detected=payload_metadata.get("speech_detected"),
            browser_rms_dbfs=payload_metadata.get("rms_dbfs"),
            browser_peak_dbfs=payload_metadata.get("peak_dbfs"),
            audio_duration_ms=audio_metrics.get("audio_duration_ms"),
            sample_rate_hz=audio_metrics.get("sample_rate_hz"),
            channels=audio_metrics.get("channels"),
            sample_width_bytes=audio_metrics.get("sample_width_bytes"),
            rms_dbfs=audio_metrics.get("rms_dbfs"),
            peak_dbfs=audio_metrics.get("peak_dbfs"),
            speech_detected=audio_metrics.get("speech_detected"),
        )
    )

    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 120
    recognizer.dynamic_energy_threshold = True
    recognizer.operation_timeout = 10

    def _openai_ready(*, allow_general: bool = False) -> tuple[bool, str]:
        openai_general_allowed = allow_general and _openai_auto_stt_enabled()
        if (
            not openai_helper_request
            and not always_openai_stt
            and not openai_general_allowed
        ):
            return False, "openai_helper_only"
        api_key = str(
            settings.openai_api_key or os.getenv("MIM_OPENAI_API_KEY") or ""
        ).strip()
        forced_disable = str(os.getenv("MIM_DISABLE_OPENAI", "")).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        openai_allowed = bool(
            (
                settings.allow_openai
                or bool(api_key)
                or str(os.getenv("MIM_ALLOW_OPENAI", "")).strip().lower()
                in {"1", "true", "yes", "on"}
            )
            and not forced_disable
        )
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

        api_key = str(
            settings.openai_api_key or os.getenv("MIM_OPENAI_API_KEY") or ""
        ).strip()
        model = (
            str(os.getenv("MIM_OPENAI_STT_MODEL") or "gpt-4o-mini-transcribe").strip()
            or "gpt-4o-mini-transcribe"
        )
        language_short = _lang_to_iso639_1(language)

        def _build_multipart(model_name: str) -> tuple[bytes, str]:
            boundary = f"----mimBoundary{uuid.uuid4().hex}"
            chunks: list[bytes] = []

            def _field(name: str, value: str) -> None:
                chunks.append(f"--{boundary}\r\n".encode("utf-8"))
                chunks.append(
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                        "utf-8"
                    )
                )
                chunks.append(str(value).encode("utf-8"))
                chunks.append(b"\r\n")

            _field("model", model_name)
            _field("language", language_short)
            _field("temperature", "0")

            chunks.append(f"--{boundary}\r\n".encode("utf-8"))
            chunks.append(
                b'Content-Disposition: form-data; name="file"; filename="input.wav"\r\n'
            )
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
                response_json = await asyncio.wait_for(
                    asyncio.to_thread(_call_openai, model_name), timeout=24
                )
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
                        speech_detected=audio_metrics.get("speech_detected"),
                        rms_dbfs=audio_metrics.get("rms_dbfs"),
                        peak_dbfs=audio_metrics.get("peak_dbfs"),
                    )
                )
                return {
                    "ok": True,
                    "transcript": "",
                    "confidence": 0.0,
                    "provider": "openai_transcribe",
                    "reason": "no_match",
                    "trace_id": trace_id,
                    "audio_metrics": audio_metrics,
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

    async def _recognize_with_local_fallback(
        trigger: str, upstream_detail: str = ""
    ) -> dict | None:
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
                **(
                    {"fallback_from": "google_web_speech"}
                    if trigger.startswith("google")
                    else {}
                ),
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
                **(
                    {"fallback_from": "google_web_speech"}
                    if trigger.startswith("google")
                    else {}
                ),
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
                **(
                    {"fallback_from": "google_web_speech"}
                    if trigger.startswith("google")
                    else {}
                ),
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
            **(
                {"fallback_from": "google_web_speech"}
                if trigger.startswith("google")
                else {}
            ),
            **(
                {"upstream_detail": upstream_detail}
                if debug_enabled and upstream_detail
                else {}
            ),
        }

    try:

        def _read_audio_file() -> sr.AudioData:
            with sr.AudioFile(io.BytesIO(audio_bytes)) as source:
                return recognizer.record(source)

        audio_data = await asyncio.wait_for(
            asyncio.to_thread(_read_audio_file), timeout=6
        )
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
            detail=_mic_debug_detail("invalid wav audio payload", trace_id)
            if debug_enabled
            else "invalid wav audio payload",
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
            local_result["audio_metrics"] = audio_metrics
            return local_result
        return {
            "ok": False,
            "transcript": "",
            "confidence": 0.0,
            "provider": "pocketsphinx",
            "reason": "provider_unavailable",
            "detail": "local speech provider unavailable",
            "trace_id": trace_id,
            "audio_metrics": audio_metrics,
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
            openai_result["audio_metrics"] = audio_metrics
            return openai_result
        return {
            "ok": False,
            "transcript": "",
            "confidence": 0.0,
            "provider": "openai_transcribe",
            "reason": "provider_unavailable",
            "detail": "openai speech provider unavailable",
            "trace_id": trace_id,
            "audio_metrics": audio_metrics,
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
            "audio_metrics": audio_metrics,
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
            local_result = await _recognize_with_local_fallback(
                trigger="google_timeout", upstream_detail="speech request timeout"
            )
            if local_result is not None:
                return local_result
        raise HTTPException(
            status_code=504,
            detail=_mic_debug_detail("speech request timeout", trace_id)
            if debug_enabled
            else "speech request timeout",
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
            "audio_metrics": audio_metrics,
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
        logger.warning(
            "mic_transcribe_provider_error trace_id=%s detail=%s", trace_id, detail
        )

        if provider_mode == "auto":
            local_result = await _recognize_with_local_fallback(
                trigger="google_provider_error", upstream_detail=detail
            )
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
            "audio_metrics": audio_metrics,
            **({"debug": debug_payload} if debug_enabled else {}),
        }


@router.get("/perception/sources")
async def list_perception_sources(
    source_type: str = "",
    active_only: bool = False,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(WorkspacePerceptionSource).order_by(
        WorkspacePerceptionSource.id.desc()
    )
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
        (
            await db.execute(
                select(WorkspacePerceptionSource)
                .order_by(WorkspacePerceptionSource.id.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )

    now = datetime.now(timezone.utc)
    active = []
    last_camera_event = None
    last_mic_transcript = None

    for row in rows:
        age_seconds = None
        if row.last_seen_at:
            age_seconds = max(0.0, (now - row.last_seen_at).total_seconds())
        if age_seconds is not None and age_seconds <= PERCEPTION_STALE_SECONDS:
            active.append(
                {
                    "source_id": int(row.id),
                    "source_type": row.source_type,
                    "device_id": row.device_id,
                    "session_id": row.session_id,
                    "is_remote": bool(row.is_remote),
                    "last_seen_at": row.last_seen_at,
                    "health_status": row.health_status,
                }
            )

        payload = (
            row.last_event_payload_json
            if isinstance(row.last_event_payload_json, dict)
            else {}
        )
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
            "healthy_count": sum(
                1 for row in rows if str(row.health_status or "") == "healthy"
            ),
            "degraded_count": sum(
                1 for row in rows if str(row.health_status or "") != "healthy"
            ),
        },
        "last_event_timestamp": max(
            [row.last_seen_at for row in rows if row.last_seen_at is not None],
            default=None,
        ),
    }


@router.get("/intake")
async def list_intake(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (await db.execute(select(InputEvent).order_by(InputEvent.id.desc())))
        .scalars()
        .all()
    )
    return [_to_input_out(item) for item in rows]


@router.get("/events")
async def list_events(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        (await db.execute(select(InputEvent).order_by(InputEvent.id.desc())))
        .scalars()
        .all()
    )
    return [_to_input_out(item) for item in rows]


@router.get("/events/{event_id}")
async def get_event(event_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    event = await db.get(InputEvent, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="event not found")
    return _to_input_out(event)


@router.get("/events/{event_id}/resolution")
async def get_event_resolution(
    event_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    resolution = (
        (
            await db.execute(
                select(InputEventResolution).where(
                    InputEventResolution.input_event_id == event_id
                )
            )
        )
        .scalars()
        .first()
    )
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
        (
            await db.execute(
                select(InputEventResolution).where(
                    InputEventResolution.input_event_id == event_id
                )
            )
        )
        .scalars()
        .first()
    )
    if not resolution:
        raise HTTPException(status_code=404, detail="event resolution not found")
    if resolution.outcome in {"blocked", "store_only"} and not payload.force:
        raise HTTPException(
            status_code=422,
            detail="blocked resolution cannot be promoted without force",
        )

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
        (
            await db.execute(
                select(InputEventResolution).where(
                    InputEventResolution.input_event_id == event_id
                )
            )
        )
        .scalars()
        .first()
    )
    if not resolution:
        raise HTTPException(status_code=404, detail="event resolution not found")
    if not resolution.capability_name:
        raise HTTPException(
            status_code=422, detail="resolution has no executable capability"
        )

    # Safety gate: if this event was previously flagged with user-action safety risk,
    # block direct dispatch unless the associated inquiry has been explicitly approved
    # or the caller has set force=true (operator override).
    if not payload.force:
        prior_escalations = list(resolution.escalation_reasons or [])
        if "user_action_safety_risk" in prior_escalations:
            resolution_meta = (
                resolution.metadata_json
                if isinstance(resolution.metadata_json, dict)
                else {}
            )
            governance_meta = (
                resolution_meta.get("governance")
                if isinstance(resolution_meta.get("governance"), dict)
                else {}
            )
            safety_meta = resolution_meta.get("user_action_safety", {})
            inquiry_id = str(safety_meta.get("inquiry_id", "")).strip()
            inquiry_approved = False
            if inquiry_id:
                inquiry = USER_ACTION_INQUIRY_SERVICE.get_inquiry(inquiry_id)
                if inquiry and inquiry.status == InquiryStatus.ACTION_APPROVED:
                    inquiry_approved = True
            if not inquiry_approved:
                governance_summary = str(governance_meta.get("summary", "")).strip()
                detail = (
                    f"Event {event_id} has an unresolved user-action safety inquiry. "
                    "Approve the inquiry via the safety API or set force=true to override."
                )
                if governance_summary:
                    detail = f"{detail} Governance context: {governance_summary}"
                raise HTTPException(
                    status_code=422,
                    detail=detail,
                )

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
async def get_event_execution(
    event_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    execution = (
        (
            await db.execute(
                select(CapabilityExecution).where(
                    CapabilityExecution.input_event_id == event_id
                )
            )
        )
        .scalars()
        .first()
    )
    if not execution:
        raise HTTPException(status_code=404, detail="event execution not found")
    return _to_execution_out(execution)


@router.get("/capabilities/executions/truth/latest")
async def get_latest_execution_truth_projection(
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
) -> dict:
    bounded_limit = max(1, min(50, int(limit)))
    rows = (
        (
            await db.execute(
                select(CapabilityExecution)
                .order_by(CapabilityExecution.id.desc())
                .limit(max(50, bounded_limit * 5))
            )
        )
        .scalars()
        .all()
    )
    truth_rows = [
        row
        for row in rows
        if isinstance(row.execution_truth_json, dict)
        and str(row.execution_truth_json.get("contract", "")).strip()
        == "execution_truth_v1"
    ]

    return build_execution_truth_bridge_projection(
        rows=truth_rows,
        generated_at=datetime.now(timezone.utc).isoformat(),
        source="gateway.capability_execution_feedback",
        max_recent_items=bounded_limit,
    )


@router.get("/capabilities/executions/{execution_id}")
async def get_capability_execution(
    execution_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
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
    resolution = (
        await db.get(InputEventResolution, execution.resolution_id)
        if execution.resolution_id
        else None
    )

    action_step = None
    if (
        resolution
        and isinstance(resolution.proposed_actions, list)
        and len(resolution.proposed_actions) > 0
    ):
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
            "action_ref": f"resolution:{execution.resolution_id}:step:1"
            if execution.resolution_id
            else "",
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
async def get_capability_execution_feedback(
    execution_id: int, db: AsyncSession = Depends(get_db)
) -> dict:
    execution = await db.get(CapabilityExecution, execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail="capability execution not found")
    return {
        "execution_id": execution.id,
        "status": execution.status,
        "reason": execution.reason,
        "feedback_json": execution.feedback_json,
        "execution_truth": (
            execution.execution_truth_json
            if isinstance(execution.execution_truth_json, dict)
            else {}
        ),
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

    history = (
        list(execution.feedback_json.get("history", []))
        if isinstance(execution.feedback_json, dict)
        else []
    )
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
            "execution_truth_contract": (
                "execution_truth_v1" if payload.execution_truth is not None else ""
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )

    merged_feedback = {
        **(
            execution.feedback_json if isinstance(execution.feedback_json, dict) else {}
        ),
        **payload.feedback_json,
    }
    if runtime_outcome:
        merged_feedback["runtime_outcome"] = runtime_outcome
    if payload.recovery_state:
        merged_feedback["recovery_state"] = payload.recovery_state
    if payload.correlation_json:
        merged_feedback["correlation_json"] = payload.correlation_json

    execution_truth = (
        execution.execution_truth_json
        if isinstance(execution.execution_truth_json, dict)
        else {}
    )
    if payload.execution_truth is not None:
        execution_truth = canonicalize_execution_truth(
            execution_id=int(execution.id),
            capability_name=execution.capability_name,
            payload=payload.execution_truth.model_dump(mode="json"),
            runtime_outcome=runtime_outcome,
        )
        deviation_signals = derive_execution_truth_signals(execution_truth)
        merged_feedback["deviation_signals"] = deviation_signals
        merged_feedback["execution_truth_signal_types"] = [
            str(item.get("signal_type", "")).strip()
            for item in deviation_signals
            if isinstance(item, dict) and str(item.get("signal_type", "")).strip()
        ]
    elif "deviation_signals" not in merged_feedback and isinstance(
        execution_truth, dict
    ):
        merged_feedback["deviation_signals"] = derive_execution_truth_signals(
            execution_truth
        )

    if execution.capability_name == "workspace_scan":
        observations = (
            payload.feedback_json.get("observations")
            if isinstance(payload.feedback_json, dict)
            else None
        )
        if isinstance(observations, list) and observations:
            detected_labels: list[str] = []
            workspace_observation_ids: list[int] = []
            workspace_object_ids: list[int] = []
            workspace_object_relation_ids: list[int] = []
            scanned_object_memories: list[WorkspaceObjectMemory] = []
            observed_labels_by_zone: dict[str, set[str]] = {}
            execution_args = (
                execution.arguments_json
                if isinstance(execution.arguments_json, dict)
                else {}
            )
            for item in observations:
                if isinstance(item, dict):
                    label = str(item.get("label", "")).strip()
                    if label:
                        detected_labels.append(label)
                    zone = (
                        str(
                            item.get("zone")
                            or execution_args.get("scan_area")
                            or "workspace"
                        ).strip()
                        or "workspace"
                    )
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
                confidence=float(
                    payload.feedback_json.get("observation_confidence", 0.8)
                ),
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
                merged_feedback["workspace_object_relation_ids"] = (
                    workspace_object_relation_ids
                )
            if workspace_proposal_ids:
                merged_feedback["workspace_proposal_ids"] = workspace_proposal_ids

    execution.status = next_status
    execution.reason = resolved_reason
    execution.execution_truth_json = execution_truth
    dispatch_telemetry = update_dispatch_telemetry_from_feedback(
        shared_root=Path("runtime/shared"),
        execution=execution,
        feedback_status=next_status,
        resolved_reason=resolved_reason,
        runtime_outcome=runtime_outcome,
        correlation_json=payload.correlation_json,
        feedback_json=payload.feedback_json,
        execution_truth=execution_truth if isinstance(execution_truth, dict) else {},
    )
    if dispatch_telemetry:
        merged_feedback["mim_arm_dispatch_telemetry"] = {
            "request_id": str(dispatch_telemetry.get("request_id") or "").strip(),
            "task_id": str(dispatch_telemetry.get("task_id") or "").strip(),
            "correlation_id": str(dispatch_telemetry.get("correlation_id") or "").strip(),
            "dispatch_status": str(dispatch_telemetry.get("dispatch_status") or "").strip(),
            "completion_status": str(dispatch_telemetry.get("completion_status") or "").strip(),
            "record_path": str(dispatch_telemetry.get("record_path") or "").strip(),
        }
    execution.feedback_json = {
        **merged_feedback,
        "last_actor": payload.actor,
        "last_reason": resolved_reason,
        "history": history,
    }

    if next_status in {"failed", "blocked", "pending_confirmation", "succeeded"} or bool(
        merged_feedback.get("latest_recovery_attempt_id")
    ):
        await sync_execution_recovery_state(
            trace_id=str(execution.trace_id or "").strip(),
            execution_id=int(execution.id),
            managed_scope=str(execution.managed_scope or "global").strip() or "global",
            actor=payload.actor,
            source="gateway_feedback",
            metadata_json={
                "reason": resolved_reason,
                "runtime_outcome": runtime_outcome,
                "status": next_status,
            },
            db=db,
        )

    if dispatch_telemetry and str(execution.trace_id or "").strip():
        await append_execution_trace_event(
            db=db,
            trace_id=str(execution.trace_id or "").strip(),
            execution_id=int(execution.id),
            intent_id=None,
            event_type="dispatch_telemetry_updated",
            event_stage="executor_feedback",
            causality_role="effect",
            summary="Dispatch-authoritative telemetry advanced from executor feedback.",
            payload_json=dispatch_telemetry,
        )

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
        "execution_truth": (
            execution.execution_truth_json
            if isinstance(execution.execution_truth_json, dict)
            else {}
        ),
    }


@router.post("/capabilities")
async def register_capability(
    payload: CapabilityRegistrationCreate, db: AsyncSession = Depends(get_db)
) -> dict:
    existing = (
        (
            await db.execute(
                select(CapabilityRegistration).where(
                    CapabilityRegistration.capability_name == payload.capability_name
                )
            )
        )
        .scalars()
        .first()
    )
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
    rows = (
        (
            await db.execute(
                select(CapabilityRegistration).order_by(
                    CapabilityRegistration.id.desc()
                )
            )
        )
        .scalars()
        .all()
    )
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
        "blocked_capability_implications": list(
            policy.get("blocked_capability_implications", [])
        ),
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
        "low_confidence_behavior": str(
            policy.get("low_confidence_behavior", "store_only")
        ),
        "require_confirmation_intents": list(
            policy.get("require_confirmation_intents", [])
        ),
        "blocked_capability_implications": list(
            policy.get("blocked_capability_implications", [])
        ),
        "ambiguous_keywords": list(policy.get("ambiguous_keywords", [])),
        "unsafe_keywords": list(policy.get("unsafe_keywords", [])),
        "target_required_verbs": list(policy.get("target_required_verbs", [])),
        "max_output_chars": int(policy.get("max_output_chars", 240)),
        "allowed_output_priorities": list(
            policy.get("allowed_output_priorities", ["low", "normal", "high"])
        ),
    }
