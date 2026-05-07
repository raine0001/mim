from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from core.communication_contract import ExpertCommunicationReply
from core.config import settings


DEFAULT_OPENAI_COMMUNICATION_MODEL = "gpt-4.1-mini"
DEFAULT_OPENAI_COMMUNICATION_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_OPENAI_COMMUNICATION_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("MIM_COMMUNICATION_OPENAI_TIMEOUT_SECONDS", "8").strip() or 8),
)
DEFAULT_OPENAI_COMMUNICATION_QUEUE_TIMEOUT_SECONDS = max(
    0.05,
    float(
        os.getenv("MIM_COMMUNICATION_OPENAI_QUEUE_TIMEOUT_SECONDS", "0.25").strip()
        or 0.25
    ),
)
DEFAULT_OPENAI_COMMUNICATION_MAX_INFLIGHT = max(
    1,
    int(os.getenv("MIM_COMMUNICATION_OPENAI_MAX_INFLIGHT", "2").strip() or 2),
)

logger = logging.getLogger(__name__)
OPENAI_COMMUNICATION_SEMAPHORE = asyncio.Semaphore(
    DEFAULT_OPENAI_COMMUNICATION_MAX_INFLIGHT
)


def _compact_text(value: Any, limit: int = 240) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _compact_list(values: Any, limit: int = 4, item_limit: int = 80) -> list[str]:
    if not isinstance(values, list):
        return []
    compact: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _compact_text(value, item_limit)
        if not text:
            continue
        lowered = text.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        compact.append(text)
        if len(compact) >= max(1, int(limit)):
            break
    return compact


def _openai_api_key() -> str:
    return str(
        settings.openai_api_key or os.getenv("MIM_OPENAI_API_KEY") or ""
    ).strip()


def _communication_openai_allowed() -> bool:
    forced_disable = str(os.getenv("MIM_DISABLE_OPENAI", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if forced_disable:
        return False
    api_key = _openai_api_key()
    return bool(
        api_key
        and (
            settings.allow_openai
            or str(os.getenv("MIM_ALLOW_OPENAI", "")).strip().lower()
            in {"1", "true", "yes", "on"}
            or bool(api_key)
        )
    )


def _record_runtime_diagnostics(
    runtime_diagnostics: dict[str, Any] | None,
    **fields: Any,
) -> None:
    if not isinstance(runtime_diagnostics, dict):
        return
    runtime_diagnostics.update(fields)


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = str(raw_text or "").strip()
    if not text:
        return {}
    fenced_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced_match:
        text = fenced_match.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _topic_hints_from_context(user_input: str, context: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    last_topic = _compact_text(context.get("last_topic"), 64)
    if last_topic:
        hints.append(last_topic)
    query = str(user_input or "").strip().lower()
    for token in [
        "business",
        "project management",
        "planning",
        "religion",
        "art",
        "music",
        "culture",
        "geography",
        "literature",
    ]:
        if token in query and token not in hints:
            hints.append(token)
    return hints[:6]


def _normalized_query_text(user_input: str) -> str:
    return " ".join(str(user_input or "").strip().lower().split())


def _compact_context_signal(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=True, sort_keys=True)[:600]
    if isinstance(value, list):
        return json.dumps(value[:12], ensure_ascii=True)[:600]
    return _compact_text(value, 600)


def _should_preserve_uncertainty_for_context(
    *,
    user_input: str,
    context: dict[str, Any],
) -> bool:
    query = _normalized_query_text(user_input)
    verification_query_markers = (
        "did that work",
        "did it work",
        "did that finish",
        "did it finish",
        "did that complete",
        "did it complete",
        "what was the result",
        "what is the result",
        "what was the outcome",
        "verify",
        "verified",
        "verification",
        "evidence",
        "proof",
        "execution result",
        "execution status",
        "run result",
        "run status",
    )
    if any(marker in query for marker in verification_query_markers):
        return True

    context_signal_keys = (
        "operator_return_briefing",
        "runtime_health_summary",
        "stability_guard_summary",
        "last_action_result",
        "last_failure",
        "execution_recovery_summary",
        "execution_truth_summary",
        "alignment_status",
    )
    context_signal_text = " ".join(
        _compact_context_signal(context.get(key))
        for key in context_signal_keys
        if context.get(key) is not None
    ).lower()
    if not context_signal_text:
        return False

    preserve_markers = (
        "conflicting",
        "publication mismatch",
        "execution-truth drift",
        "unable to verify",
        "not verified",
        "missing evidence",
        "missing data",
        "insufficient evidence",
        "inconclusive",
        "ambiguous",
        '"status": "pending"',
        '"status": "blocked"',
        '"status": "failed"',
        '"alignment_status": "conflicting"',
    )
    return any(marker in context_signal_text for marker in preserve_markers)


def _is_conversational_confident_query(
    *,
    user_input: str,
    context: dict[str, Any],
) -> bool:
    query = _normalized_query_text(user_input)
    if not query:
        return False
    confident_markers = (
        "what are you",
        "what is mim",
        "what is tod",
        "what is mim and tod",
        "what are mim and tod",
        "who are you",
        "what is your purpose",
        "what's your purpose",
        "what is the system",
        "describe the system",
        "system description",
        "how are you different",
        "how do you differ",
        "what makes you different",
        "what can you do",
        "describe yourself",
        "explain what you are",
        "explain your purpose",
    )
    if any(marker in query for marker in confident_markers):
        return True

    if any(
        marker in query
        for marker in (
            "what projects are you tracking",
            "which projects are you tracking",
            "what project are you tracking",
            "which project are you tracking",
            "what programs are you tracking",
            "which programs are you tracking",
            "what program are you tracking",
            "which program are you tracking",
        )
    ):
        program_status_summary = _compact_text(context.get("program_status_summary"), 240)
        program_status = context.get("program_status") if isinstance(context.get("program_status"), dict) else {}
        if program_status_summary or program_status:
            return True

    topic_hint = _compact_text(context.get("last_topic"), 64).lower()
    if topic_hint in {"identity", "system", "mission", "capabilities"} and query.startswith(
        ("what", "who", "how", "why", "describe", "explain")
    ):
        return True
    return False


def _response_mode_for_context(
    *,
    user_input: str,
    context: dict[str, Any],
) -> str:
    requested_mode = _compact_text(context.get("response_mode"), 48).lower()
    if requested_mode:
        return requested_mode
    if _should_preserve_uncertainty_for_context(user_input=user_input, context=context):
        return "default"
    if _is_conversational_confident_query(user_input=user_input, context=context):
        return "conversational_confident"
    return "default"


def _strip_conversational_uncertainty_prefix(reply_text: str) -> str:
    reply = " ".join(str(reply_text or "").strip().split())
    if not reply:
        return ""
    patterns = (
        r"^(?:i am|i'm) not totally sure(?:,)?(?:\s+but)?\s+",
        r"^(?:i am|i'm) not fully sure(?:,)?(?:\s+but)?\s+",
        r"^(?:i am|i'm) not sure(?:,)?(?:\s+but)?\s+",
        r"^not totally sure(?:,)?(?:\s+but)?\s+",
        r"^not fully sure(?:,)?(?:\s+but)?\s+",
        r"^not sure(?:,)?(?:\s+but)?\s+",
    )
    for pattern in patterns:
        updated = re.sub(pattern, "", reply, count=1, flags=re.IGNORECASE)
        if updated != reply:
            return updated[:1].upper() + updated[1:] if updated else ""
    return reply


def sanitize_user_facing_reply_text(reply_text: str) -> str:
    reply = " ".join(str(reply_text or "").strip().split())
    if not reply:
        return ""
    cleaned = reply
    patterns = (
        r"^(?:and\s+)?(?:i am|i'm)\s+giving\s+some\s+extra\s+context(?:\s+because\s+i\s+am\s+thinking\s+out\s+loud)?[,:-]?\s*",
        r"^giving\s+some\s+extra\s+context(?:\s+because\s+i\s+am\s+thinking\s+out\s+loud)?[,:-]?\s*",
        r"^some\s+extra\s+context(?:\s+because\s+i\s+am\s+thinking\s+out\s+loud)?[,:-]?\s*",
        r"^(?:and\s+)?(?:i am|i'm)\s+thinking\s+out\s+loud[,:-]?\s*",
        r"^thinking\s+out\s+loud[,:-]?\s*",
    )
    while cleaned:
        updated = cleaned
        for pattern in patterns:
            candidate = re.sub(pattern, "", updated, count=1, flags=re.IGNORECASE)
            candidate = candidate.lstrip(" ,:-")
            if candidate != updated:
                updated = candidate
                break
        if updated == cleaned:
            break
        cleaned = updated
    if not cleaned:
        return ""
    if cleaned != reply:
        return cleaned[:1].upper() + cleaned[1:]
    return cleaned


def _apply_response_mode_to_reply(
    *,
    reply: ExpertCommunicationReply,
    response_mode: str,
) -> ExpertCommunicationReply:
    normalized_mode = " ".join(str(response_mode or "default").strip().split())[:48] or "default"
    reply.response_mode = normalized_mode
    if normalized_mode == "conversational_confident":
        reply.reply_text = _strip_conversational_uncertainty_prefix(reply.reply_text)
    reply.reply_text = sanitize_user_facing_reply_text(reply.reply_text)
    return reply


def build_deterministic_communication_reply(
    *,
    user_input: str,
    context: dict[str, Any] | None,
    fallback_reply: str,
) -> ExpertCommunicationReply:
    reply = " ".join(str(fallback_reply or "").strip().split())
    query = " ".join(str(user_input or "").strip().split())
    lowered_query = query.lower()
    normalized_context = context if isinstance(context, dict) else {}
    topic_hint = _compact_text(normalized_context.get("last_topic"), 64).lower()
    response_mode = _response_mode_for_context(
        user_input=user_input,
        context=normalized_context,
    )

    if reply == "Hi. I am here and ready to help.":
        reply = "Hi. I'm MIM. What would you like to work on?"
        topic_hint = topic_hint or "greeting"
    elif reply == "Yes. I am MIM.":
        reply = "I'm MIM."
        topic_hint = topic_hint or "identity"
    elif reply == "Yes. I can keep this direct, short, and conversational.":
        reply = "Yes. I'll keep this direct, short, and conversational."
    elif reply == "Understood. I will keep responses short.":
        reply = "Understood. I'll keep responses short."
    elif reply == "Understood. I will stay in conversation mode until you ask for a concrete action.":
        reply = "Understood. I'll stay in conversation mode until you ask for a concrete action."
    elif reply == "You'r e welcome.":
        reply = "You're welcome."

    if reply.startswith("I can help right away with one specific request."):
        if "?" in query or any(
            lowered_query.startswith(prefix)
            for prefix in ("what", "how", "why", "when", "where", "who", "which", "can you", "do you")
        ):
            reply = "I can help, but I need one concrete detail to answer well: what exactly do you want me to focus on?"
        else:
            reply = "I can help. Give me one specific question or one concrete action."

    if reply.startswith("I still need one specific request. Options:"):
        reply = "I still need one concrete request. Ask one question, ask for a one-line status, or say create goal: <action>."

    memory_topics = _topic_hints_from_context(query, normalized_context)
    if topic_hint and topic_hint not in memory_topics:
        memory_topics.insert(0, topic_hint)
    return _apply_response_mode_to_reply(
        reply=ExpertCommunicationReply(
        reply_text=reply,
        topic_hint=topic_hint,
        composer_mode="deterministic_fallback",
        should_store_memory=True,
        memory_topics=memory_topics[:8],
        ),
        response_mode=response_mode,
    )


def _should_preserve_operational_fallback(
    *,
    user_input: str,
    context: dict[str, Any] | None,
    fallback_reply: str,
) -> bool:
    normalized_context = context if isinstance(context, dict) else {}
    topic_hint = _compact_text(normalized_context.get("last_topic"), 64).lower()
    query = " ".join(str(user_input or "").strip().lower().split())
    reply = " ".join(str(fallback_reply or "").strip().split())
    if not reply:
        return False

    operational_topics = {"system", "tod_status", "status", "objective", "priorities", "priority"}
    operational_query_markers = {
        "what is the system",
        "how is tod doing",
        "status now",
        "one line status",
        "summarize your status",
        "current objective",
        "active objective",
        "what are you working on",
        "what are we working on",
        "what should we work on",
        "work on today",
        "runtime health",
        "runtime status",
        "how is runtime health",
        "how is the runtime doing",
        "how is runtime doing",
        "current health",
        "check your current health",
        "wait stop",
        "actually start now",
        "start now",
        "what is next",
        "next for us",
        "what should i do first",
        "what should we prioritize",
        "prioritize next",
        "top priority",
    }
    evidence_markers = {
        "Decision visibility:",
        "TOD collaboration:",
        "Runtime health:",
        "One-line status:",
        "Current status:",
        "Current health:",
        "TOD status:",
        "Current objective focus:",
        "Active goal:",
        "Current recommendation:",
        "Top priority today:",
        "Priority focus:",
        "Next step:",
        "Understood. I will start now.",
        "Understood. I stopped",
        "Got it. I've stopped",
        "Got it, I've paused",
    }

    if topic_hint == "interrupt_control":
        return True

    if topic_hint in operational_topics and any(marker in reply for marker in evidence_markers):
        return True
    if any(marker in query for marker in operational_query_markers) and any(
        marker in reply for marker in evidence_markers
    ):
        return True
    return False


def _model_request_payload(
    *,
    user_input: str,
    context: dict[str, Any],
    fallback_reply: str,
    deterministic_reply: ExpertCommunicationReply,
) -> dict[str, Any]:
    prompt_payload = {
        "user_input": _compact_text(user_input, 500),
        "fallback_reply": _compact_text(fallback_reply, 500),
        "deterministic_reply": deterministic_reply.to_payload(),
        "conversation_context": {
            "session_display_name": _compact_text(context.get("session_display_name"), 80),
            "remembered_user_id": _compact_text(context.get("remembered_user_id"), 80),
            "remembered_display_name": _compact_text(context.get("remembered_display_name"), 80),
            "remembered_aliases": _compact_list(context.get("remembered_aliases"), 6, 40),
            "remembered_conversation_preferences": _compact_list(
                context.get("remembered_conversation_preferences"),
                6,
                80,
            ),
            "remembered_conversation_likes": _compact_list(
                context.get("remembered_conversation_likes"),
                6,
                80,
            ),
            "remembered_conversation_dislikes": _compact_list(
                context.get("remembered_conversation_dislikes"),
                6,
                80,
            ),
            "last_topic": _compact_text(context.get("last_topic"), 80),
            "last_user_input": _compact_text(context.get("last_user_input"), 180),
            "last_prompt": _compact_text(context.get("last_prompt"), 220),
            "last_action_request": _compact_text(context.get("last_action_request"), 180),
            "pending_action_request": _compact_text(context.get("pending_action_request"), 180),
            "assistant_name": _compact_text(context.get("assistant_name"), 40),
            "identity": _compact_text(context.get("identity"), 320),
            "assistant_identity": _compact_text(context.get("assistant_identity"), 320),
            "assistant_application": _compact_text(context.get("assistant_application"), 80),
            "assistant_channel": _compact_text(context.get("assistant_channel"), 80),
            "assistant_scope": _compact_text(context.get("assistant_scope"), 220),
            "assistant_capabilities": _compact_text(context.get("assistant_capabilities"), 220),
            "counterpart_identity": _compact_text(context.get("counterpart_identity"), 320),
            "counterpart_application": _compact_text(context.get("counterpart_application"), 80),
            "counterpart_channel": _compact_text(context.get("counterpart_channel"), 80),
            "system_identity": _compact_text(context.get("system_identity"), 320),
            "guardrails": _compact_list(context.get("guardrails"), 8, 120),
        },
        "response_contract": {
            "format": "json_object",
            "schema": {
                "reply_text": "string",
                "topic_hint": "string",
                "response_mode": "string",
                "should_store_memory": "boolean",
                "memory_topics": ["string"],
                "memory_people": ["string"],
                "memory_events": ["string"],
                "memory_experiences": ["string"],
            },
        },
    }
    return {
        "model": str(
            os.getenv("MIM_COMMUNICATION_OPENAI_MODEL")
            or DEFAULT_OPENAI_COMMUNICATION_MODEL
        ).strip(),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are MIM's communication composer. Rewrite the safe fallback reply into a direct, natural, expert conversation reply. "
                    "Preserve the original meaning, boundaries, and uncertainty. Do not claim actions, web research, or observations that are not already present. "
                    "If conversation_context includes assistant_identity, assistant_application, assistant_channel, counterpart_identity, or system_identity, treat them as authoritative facts about the system and keep identity answers consistent with them. "
                    "If conversation_context.response_mode is conversational_confident, do not prepend uncertainty hedges such as 'I am not totally sure' unless the context itself shows conflicting system state, missing verification data, or ambiguous execution results. "
                    "Use conversation_context.assistant_name as the self-identifier. Do not rename TOD to MIM or collapse distinct applications into one voice. Prefer 1-4 short sentences. If the reply needs clarification, ask one crisp clarifying question instead of generic filler. "
                    "Return JSON only."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True),
            },
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }


def _extract_completion_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message = first_choice.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").strip() in {"text", "output_text", "input_text"}:
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _compose_with_openai_sync(
    *,
    user_input: str,
    context: dict[str, Any],
    fallback_reply: str,
    deterministic_reply: ExpertCommunicationReply,
    timeout_seconds: float,
) -> ExpertCommunicationReply | None:
    if not _communication_openai_allowed():
        return None
    api_key = _openai_api_key()
    if not api_key:
        return None

    request_payload = _model_request_payload(
        user_input=user_input,
        context=context,
        fallback_reply=fallback_reply,
        deterministic_reply=deterministic_reply,
    )
    request = urllib_request.Request(
        str(
            os.getenv("MIM_COMMUNICATION_OPENAI_URL")
            or DEFAULT_OPENAI_COMMUNICATION_URL
        ).strip(),
        data=json.dumps(request_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except (urllib_error.HTTPError, OSError, ValueError):
        return None

    model_text = _extract_completion_text(response_payload)
    parsed = ExpertCommunicationReply.from_payload(_extract_json_object(model_text))
    if parsed is None:
        return None
    parsed.composer_mode = "openai_rewrite"
    parsed.model = str(response_payload.get("model") or request_payload.get("model") or "").strip()[:64]
    return _apply_response_mode_to_reply(
        reply=parsed,
        response_mode=_response_mode_for_context(user_input=user_input, context=context),
    )


async def compose_expert_communication_reply(
    *,
    user_input: str,
    context: dict[str, Any] | None,
    fallback_reply: str,
    runtime_diagnostics: dict[str, Any] | None = None,
) -> ExpertCommunicationReply:
    normalized_context = context if isinstance(context, dict) else {}
    deterministic_reply = build_deterministic_communication_reply(
        user_input=user_input,
        context=normalized_context,
        fallback_reply=fallback_reply,
    )
    if bool(normalized_context.get("force_deterministic_communication")):
        _record_runtime_diagnostics(
            runtime_diagnostics,
            composer_mode="deterministic_forced",
            composer_reason="force_deterministic_communication",
        )
        return deterministic_reply
    if _should_preserve_operational_fallback(
        user_input=user_input,
        context=normalized_context,
        fallback_reply=fallback_reply,
    ):
        _record_runtime_diagnostics(
            runtime_diagnostics,
            composer_mode="deterministic_preserved",
            composer_reason="operational_fallback",
        )
        return deterministic_reply
    if not _communication_openai_allowed():
        _record_runtime_diagnostics(
            runtime_diagnostics,
            composer_mode="deterministic_fallback",
            composer_reason="openai_disabled",
        )
        return deterministic_reply

    queue_timeout_seconds = DEFAULT_OPENAI_COMMUNICATION_QUEUE_TIMEOUT_SECONDS
    request_timeout_seconds = DEFAULT_OPENAI_COMMUNICATION_TIMEOUT_SECONDS

    try:
        await asyncio.wait_for(
            OPENAI_COMMUNICATION_SEMAPHORE.acquire(),
            timeout=queue_timeout_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "communication composer saturated; falling back to deterministic reply"
        )
        _record_runtime_diagnostics(
            runtime_diagnostics,
            composer_mode="deterministic_fallback",
            composer_reason="rewrite_queue_timeout",
            degraded=True,
        )
        return deterministic_reply

    try:
        try:
            model_reply = await asyncio.wait_for(
                asyncio.to_thread(
                    _compose_with_openai_sync,
                    user_input=user_input,
                    context=normalized_context,
                    fallback_reply=fallback_reply,
                    deterministic_reply=deterministic_reply,
                    timeout_seconds=request_timeout_seconds,
                ),
                timeout=request_timeout_seconds + 1.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "communication composer rewrite timed out; falling back to deterministic reply"
            )
            _record_runtime_diagnostics(
                runtime_diagnostics,
                composer_mode="deterministic_fallback",
                composer_reason="rewrite_timeout",
                degraded=True,
            )
            return deterministic_reply
    finally:
        OPENAI_COMMUNICATION_SEMAPHORE.release()

    if model_reply is not None and str(model_reply.reply_text or "").strip():
        raw_model_reply_text = " ".join(str(model_reply.reply_text or "").strip().split())
        cleaned_model_reply_text = sanitize_user_facing_reply_text(raw_model_reply_text)
        if cleaned_model_reply_text and cleaned_model_reply_text != raw_model_reply_text:
            _record_runtime_diagnostics(
                runtime_diagnostics,
                meta_prefix_removed=True,
                raw_model_reply_text=raw_model_reply_text,
                cleaned_model_reply_text=cleaned_model_reply_text,
            )
            model_reply.reply_text = cleaned_model_reply_text
        _record_runtime_diagnostics(
            runtime_diagnostics,
            composer_mode=str(model_reply.composer_mode or "openai_rewrite").strip(),
            composer_reason="rewrite_completed",
        )
        return _apply_response_mode_to_reply(
            reply=model_reply,
            response_mode=deterministic_reply.response_mode,
        )
    _record_runtime_diagnostics(
        runtime_diagnostics,
        composer_mode="deterministic_fallback",
        composer_reason="rewrite_empty",
    )
    return deterministic_reply