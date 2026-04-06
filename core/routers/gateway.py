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

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.camera_scene import (
    collect_fresh_camera_observations,
    summarize_camera_observations,
)
from core.db import get_db
from core.config import settings
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
from core.journal import write_journal
from core.mim_arm_dispatch_telemetry import update_dispatch_telemetry_from_feedback
from core.preferences import (
    DEFAULT_USER_ID,
    get_user_preference_payload,
    upsert_user_preference,
)
from core.user_action_inquiry_service import InquiryStatus, UserActionInquiryService
from core.user_action_safety_monitor import (
    ActionCategory,
    UserAction,
    UserActionSafetyMonitor,
)
from core.routers.self_awareness_router import health_monitor as _mim_health_monitor
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
    if request:
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
    if request:
        return f"For '{request}', I am still missing {missing}. Options: 1) ask a question, 2) suggest a short plan, 3) request an action."
    return f"I am still missing {missing}. Options: 1) ask a question, 2) suggest a short plan, 3) request an action."


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


def _to_execution_out(row: CapabilityExecution) -> dict:
    feedback = row.feedback_json if isinstance(row.feedback_json, dict) else {}
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

    return False


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


def _mentions_tod(text: str) -> bool:
    raw = str(text or "").lower()
    return _contains_word(raw, "tod") or _contains_word(raw, "tods")


def _is_low_signal_turn(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return True

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


def _text_route_preference(*, text: str, parsed_intent: str) -> str:
    # Conversation-first lane for low-stakes dialogue turns.
    normalized_intent = str(parsed_intent or "").strip().lower()
    if normalized_intent in {"question", "discussion", "observation"}:
        return "conversation_layer"

    if _looks_like_action_request(text):
        return "goal_system"

    raw = str(text or "").strip()
    if _looks_like_question_text(raw):
        return "conversation_layer"

    if _is_low_signal_turn(raw):
        return "conversation_layer"

    return "goal_system"


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
            for token in {"what is the system", "our system", "define the system"}
        )
        or "the system is mim plus tod" in prompt
    ):
        return "system"
    if (
        any(
            token in query
            for token in {
                "what is our objective",
                "current objective",
                "active objective",
            }
        )
        or "current objective focus" in prompt
    ):
        return "objective"
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
    if "news" in query or "top ai and tech themes today" in prompt:
        return "news"
    return "general"


async def _get_recent_text_conversation_context(
    db: AsyncSession,
    *,
    session_id: str,
    exclude_event_id: int | None = None,
    limit: int = 8,
) -> dict:
    normalized_session = str(session_id or "").strip()
    if not normalized_session:
        return {
            "turn_count": 0,
            "last_user_input": "",
            "last_prompt": "",
            "last_topic": "",
            "last_object_inquiry": {},
            "last_technical_research": {},
        }

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
        return {
            "turn_count": 0,
            "last_user_input": "",
            "last_prompt": "",
            "last_topic": "",
            "last_object_inquiry": {},
            "last_technical_research": {},
        }

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

    return {
        "turn_count": len(matched),
        "last_user_input": str(last_event.raw_input or "").strip(),
        "last_prompt": last_prompt,
        "last_topic": last_topic,
        "last_object_inquiry": last_object_inquiry,
        "last_technical_research": last_technical_research,
    }


def _conversation_followup_response(
    normalized_query: str,
    context: dict[str, object] | None = None,
) -> str:
    query = str(normalized_query or "").strip().lower()
    session_context = context or {}
    last_topic = str(session_context.get("last_topic") or "").strip().lower()
    last_prompt = str(session_context.get("last_prompt") or "").strip()

    if not query:
        return ""

    if query in {"thanks", "thank you", "ok thanks", "okay thanks"}:
        return "You're welcome."

    technical_followup = _technical_research_followup_response(query, session_context)
    if technical_followup:
        return technical_followup

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
        if last_topic == "technical_research":
            return "After that, choose the next path worth deeper research, research that path, and stop when the evidence stops improving or the budget runs out."
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
            "short recap",
            "short final recap",
            "one line",
            "summarize in one line",
            "shorter",
        }
    ):
        compact_map = {
            "tod_status": "One line: TOD looks usable when health, freshness, and alignment stay in sync.",
            "system": "One line: MIM manages interaction and context, while TOD manages tasks and execution.",
            "objective": "One line: the objective is reliable conversation flow and stable MIM to TOD handoff.",
            "priorities": "One line: stabilize routing, keep tests green, and verify the next handoff.",
            "mission": "One line: assist safely, stay coherent, and help execute goals.",
            "risk": "One line: the main risk is drift between conversation behavior and execution state.",
            "risk_reduction": "One line: reduce risk with regression checks and explicit handoff verification.",
            "project_planning": "One line: define scope, name the MVP, and create the first tasks.",
            "news": "One line: the big themes are agent guardrails, cost pressure, private AI, and bot-authenticity scrutiny.",
        }
        if last_topic in compact_map:
            return compact_map[last_topic]
        if last_prompt:
            return _compact_text(last_prompt, 120)
        return "One line: I can keep the answer short, specific, and actionable."

    if "checklist" in query:
        checklist_map = {
            "technical_research": "Checklist: 1. Confirm the time budget. 2. Lock the next technical step. 3. Research that step. 4. Decide whether another round is justified.",
            "priorities": "Checklist: 1. Stabilize routing. 2. Run regression tests. 3. Verify live MIM to TOD handoff.",
            "objective": "Checklist: 1. Improve reliability. 2. Keep task state clear. 3. Confirm stable handoff.",
            "risk_reduction": "Checklist: 1. Add regression checks. 2. Tighten routing rules. 3. Verify handoff explicitly.",
            "project_planning": "Checklist: 1. Define scope. 2. Choose the MVP. 3. Create first tasks and milestones.",
        }
        if last_topic in checklist_map:
            return checklist_map[last_topic]
        return "Checklist: 1. Confirm the goal. 2. Choose the next action. 3. Verify the result."

    if any(
        token in query for token in {"why that", "why that one", "why that priority"}
    ):
        if last_topic == "technical_research":
            return "Because open-ended technical research can loop forever; the budget and stop condition force the next round to earn its cost."
        if last_topic in {"priorities", "objective"}:
            return "Because reliability and handoff stability protect every later task; if those drift, the rest of the workflow gets noisy fast."
        return "Because it reduces uncertainty before taking the next step."

    if "dependency" in query or "dependencies" in query:
        if last_topic == "technical_research":
            return "Main dependencies are verified baseline facts, credible sources, and a clear stop condition for when the next research round is no longer paying off."
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
    }
    return any(marker in query for marker in followup_markers) or bool(
        re.search(r"\bstep\s+\d+\b", query)
    )


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

    followup_response = _conversation_followup_response(normalized_query, context)
    if followup_response:
        return followup_response

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
    if lowered in greetings or normalized_query in greetings:
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
            "what is the system",
            "what is our system",
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
        return "TOD status: I can check health, freshness, and alignment, then give you a one-line summary."

    if any(
        token in normalized_query
        for token in {
            "what exactly do you need",
            "what do you need from me",
            "what do you need",
        }
    ):
        return "I need one concrete request from you: a question, a short plan, or an action."

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
        return "One-line status: online, stable, and focused on reliable MIM to TOD handoff."

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
        return "I am online and operating normally."

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
    internal_intent = _infer_intent(event)
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    route_preference = str(metadata.get("route_preference", "")).strip().lower()
    conversation_override = route_preference == "conversation_layer"
    conversation_session_id = str(metadata.get("conversation_session_id", "")).strip()
    optional_escalation = ""

    if conversation_override:
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
        conversation_context = {}
        if conversation_session_id:
            conversation_context = await _get_recent_text_conversation_context(
                db,
                session_id=conversation_session_id,
                exclude_event_id=event.id,
            )
        confidence_tier = "conversation"
        if _looks_like_action_request(event.raw_input):
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
            if not repeated_prompt and conversation_session_id:
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
            if not repeated_prompt and conversation_session_id:
                repeated_prompt = await _has_recent_similar_text_precision_prompt(
                    db,
                    transcript=event.raw_input,
                    exclude_event_id=event.id,
                    session_id=conversation_session_id,
                    within_seconds=180,
                )
            outcome = "store_only"
            safety_decision = "store_only"
            if repeated_prompt:
                reason = "conversation_precision_limit"
                escalation_reasons = [
                    "needs_specific_request",
                    "clarification_limit_reached",
                ]
                clarification_prompt = (
                    "I still need one specific request. Options: 1) ask one question, "
                    "2) ask for one-line status, 3) create goal: <action>."
                )
            else:
                reason = "conversation_precision_prompt"
                escalation_reasons = ["needs_specific_request"]
                clarification_prompt = (
                    "I can help right away with one specific request. Say one "
                    "question or one action."
                )
        else:
            outcome = "store_only"
            safety_decision = "store_only"
            reason = "conversation_override"
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
            response_context = {
                "source": event.source,
                "target_system": event.target_system,
                **camera_context,
                **object_memory_context,
                **conversation_context,
            }
            if camera_object_inquiry:
                response_context["camera_object_inquiry_prompt"] = str(
                    camera_object_inquiry.get("inquiry_prompt") or ""
                ).strip()
            if learned_object_prompt:
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
                    clarification_prompt = _conversation_response(
                        event.raw_input,
                        context=response_context,
                    )
                    if (
                        camera_object_inquiry
                        and clarification_prompt
                        == str(
                            camera_object_inquiry.get("inquiry_prompt") or ""
                        ).strip()
                    ):
                        object_inquiry = camera_object_inquiry
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
        elif capability_requires_confirmation:
            safety_decision = "requires_confirmation"
            reason = "capability_policy_requires_confirmation"
        else:
            safety_decision = "auto_execute"
            reason = "policy_allows_auto_execute"
        outcome = safety_decision

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
    goal_description = _goal_description(event, internal_intent)
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
        proposed_actions=_proposed_actions(
            internal_intent, capability_name, goal_description
        ),
        metadata_json={
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
            "object_inquiry": object_inquiry,
            "web_research": web_research,
            "user_action_safety": user_action_safety,
            "governance": governance,
            "last_technical_research": _compact_technical_research_context(
                web_research
            ),
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
    resolution_meta = (
        resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}
    )
    is_conversation_override = bool(resolution_meta.get("conversation_override"))
    if is_conversation_override and event.source == "text":
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
    execution = None
    if not is_conversation_override:
        execution = await _create_or_update_execution_binding(
            event=event,
            resolution=resolution,
            capability_name=resolution.capability_name,
            db=db,
            arguments_json=_default_execution_arguments(
                event, resolution.capability_name
            ),
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
        "hello",
        "hi",
        "hey",
        "mim",
        "tod",
    }
    if lowered in blocked:
        return ""
    parts = cleaned.split(" ")[:4]
    normalized = " ".join(part[:40] for part in parts).strip()
    return normalized[:80]


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
    payload: TextInputAdapterRequest, db: AsyncSession = Depends(get_db)
) -> dict:
    route_preference = _text_route_preference(
        text=payload.text, parsed_intent=payload.parsed_intent
    )
    normalized = NormalizedInputCreate(
        source="text",
        raw_input=payload.text,
        parsed_intent=payload.parsed_intent,
        confidence=payload.confidence,
        target_system=payload.target_system,
        requested_goal=payload.requested_goal,
        safety_flags=payload.safety_flags,
        metadata_json={
            **payload.metadata_json,
            "adapter": "text",
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
