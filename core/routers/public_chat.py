from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.communication_composer import compose_expert_communication_reply
from core.config import settings
from core.db import get_db
from core.identity import MIM_LEGAL_CONTACT_EMAIL
from core.identity import MIM_LEGAL_ENTITY_NAME
from core.identity import MIM_LEGAL_JURISDICTION
from core.identity import mim_public_identity_summary
from core.identity import public_channel_definition
from core.identity import public_system_identity_summary
from core.identity import tod_public_identity_summary
from core.interface_service import (
    append_interface_message,
    get_interface_session,
    list_interface_messages,
    to_interface_message_out,
    to_interface_session_out,
    upsert_interface_session,
)
from core.models import MemoryEntry


router = APIRouter(tags=["public-chat"])

PUBLIC_CHAT_MESSAGE_LIMIT = 100
PUBLIC_CHAT_UPLOAD_LIMIT_BYTES = 262_144
PUBLIC_PROFILE_SCAN_LIMIT = 200
PUBLIC_PRIVACY_POLICY_PATH = "/privacy"
PUBLIC_TEXT_UPLOAD_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".css",
    ".html",
    ".sql",
    ".csv",
    ".toml",
    ".ini",
    ".log",
    ".xml",
}

PUBLIC_OPERATOR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(restart|reboot|shutdown|deploy|dispatch|approve|merge|commit|push|reset|wipe|delete|drop|truncate|kill|stop|start)\b.*\b(server|service|runtime|database|repo|repository|branch|task|objective|job|worker|system|host)\b",
            re.IGNORECASE,
        ),
        "Public chat cannot execute operator actions against the live system.",
    ),
    (
        re.compile(
            r"\b(sudo|systemctl|rm\s+-rf|git\s+reset|git\s+push|git\s+commit|kubectl|docker\s+(?:compose\s+)?(?:up|down|restart)|psql)\b",
            re.IGNORECASE,
        ),
        "Public chat does not run shell, git, deployment, or database commands.",
    ),
    (
        re.compile(r"(?:^|\s)/(?:mim|tod)\b", re.IGNORECASE),
        "Public chat does not accept operator-console commands.",
    ),
    (
        re.compile(r"\b(?:objective|task)[-\s#:]*\d+\b", re.IGNORECASE),
        "Public chat does not mutate tracked objectives or tasks.",
    ),
)

NAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmy name is\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", re.IGNORECASE),
    re.compile(r"\bi am\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", re.IGNORECASE),
    re.compile(r"\bi'm\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b", re.IGNORECASE),
)
GOAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmy goal is\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"\bi want to\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"\bi'm here to\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"\bi am here to\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
)
SPECIAL_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:my\s+)?birthday\s+is\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"\b(?:my\s+)?anniversary\s+is\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"\b(?:my\s+)?deadline\s+is\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
)
INTEREST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bi (?:love|like|enjoy)\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
)


class PublicChatMessageRequest(BaseModel):
    message: str = Field(min_length=1)
    mode: Literal["mim", "tod"] = "mim"
    session_key: str = Field(min_length=1)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact_text(value: Any, limit: int = 240) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _normalize_mode(value: object) -> str:
    return "tod" if str(value or "").strip().lower() == "tod" else "mim"


def _normalize_session_key(value: object) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._:-]+", "-", str(value or "").strip())
    normalized = normalized.strip("-._:")
    if not normalized:
        raise HTTPException(status_code=422, detail="session_key_required")
    return normalized[:120]


def _serialize_message(row: object) -> dict[str, object]:
    payload = to_interface_message_out(row)
    metadata = payload.get("metadata_json") if isinstance(payload.get("metadata_json"), dict) else {}
    return {
        "message_id": int(payload.get("message_id") or 0),
        "role": str(payload.get("role") or "mim").strip(),
        "direction": str(payload.get("direction") or "outbound").strip(),
        "content": str(payload.get("content") or "").strip(),
        "created_at": payload.get("created_at"),
        "message_type": str(metadata.get("message_type") or "message").strip(),
        "mode": str(metadata.get("mode") or "mim").strip(),
        "attachment": metadata.get("attachment") if isinstance(metadata.get("attachment"), dict) else None,
    }


def _dedupe_strings(values: list[str], *, limit: int = 6) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _compact_text(value, 120)
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique.append(cleaned)
        if len(unique) >= limit:
            break
    return unique


def _extract_profile_updates(message: str) -> dict[str, Any]:
    text = str(message or "").strip()
    updates: dict[str, Any] = {"goals": [], "special_dates": [], "interests": []}
    for pattern in NAME_PATTERNS:
        match = pattern.search(text)
        if match:
            updates["name"] = match.group(1).strip()
            break
    for pattern in GOAL_PATTERNS:
        match = pattern.search(text)
        if match:
            updates["goals"].append(match.group(1).strip())
    for pattern in SPECIAL_DATE_PATTERNS:
        match = pattern.search(text)
        if match:
            updates["special_dates"].append(match.group(1).strip())
    for pattern in INTEREST_PATTERNS:
        match = pattern.search(text)
        if match:
            updates["interests"].append(match.group(1).strip())
    updates["goals"] = _dedupe_strings(updates["goals"], limit=6)
    updates["special_dates"] = _dedupe_strings(updates["special_dates"], limit=6)
    updates["interests"] = _dedupe_strings(updates["interests"], limit=6)
    return updates


def _merge_profile(existing: dict[str, Any] | None, updates: dict[str, Any] | None) -> dict[str, Any]:
    current = existing.copy() if isinstance(existing, dict) else {}
    incoming = updates if isinstance(updates, dict) else {}
    if str(incoming.get("name") or "").strip():
        current["name"] = str(incoming.get("name") or "").strip()
    for key in ("goals", "special_dates", "interests"):
        merged = list(current.get(key) or []) + list(incoming.get(key) or [])
        current[key] = _dedupe_strings([str(item) for item in merged], limit=8)
    return current


def _public_command_block_reason(message: str) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    for pattern, reason in PUBLIC_OPERATOR_PATTERNS:
        if pattern.search(text):
            return reason
    return ""


def _client_ip(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return str(request.client.host).strip()
    return "unknown"


def _ip_hash(ip_value: str) -> str:
    return hashlib.sha256(str(ip_value or "unknown").encode("utf-8")).hexdigest()[:16]


def _visitor_key_from_session(session_key: str, request: Request) -> tuple[str, str]:
    session_value = _normalize_session_key(session_key)
    base_key = re.sub(r"-(?:mim|tod)$", "", session_value, flags=re.IGNORECASE)
    ip_hash = _ip_hash(_client_ip(request))
    visitor_key = f"public:{base_key or ip_hash}"
    return visitor_key[:140], ip_hash


async def _latest_public_profile(*, visitor_key: str, ip_hash: str, db: AsyncSession) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(MemoryEntry)
            .where(MemoryEntry.memory_class == "public_guest_profile")
            .order_by(MemoryEntry.id.desc())
            .limit(PUBLIC_PROFILE_SCAN_LIMIT)
        )
    ).scalars().all()
    for row in rows:
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        if metadata.get("visitor_key") == visitor_key or metadata.get("ip_hash") == ip_hash:
            profile = metadata.get("profile") if isinstance(metadata.get("profile"), dict) else {}
            return {
                **profile,
                "visit_count": int(metadata.get("visit_count") or profile.get("visit_count") or 0),
                "last_seen_at": str(metadata.get("last_seen_at") or profile.get("last_seen_at") or row.created_at),
            }
    return {"goals": [], "special_dates": [], "interests": [], "visit_count": 0, "last_seen_at": ""}


async def _remember_profile(
    *,
    visitor_key: str,
    ip_hash: str,
    profile: dict[str, Any],
    db: AsyncSession,
) -> None:
    entry = MemoryEntry(
        memory_class="public_guest_profile",
        content=json.dumps(profile, ensure_ascii=True, sort_keys=True),
        summary=_compact_text(
            f"Public guest profile for {profile.get('name') or visitor_key}: goals={', '.join(profile.get('goals') or []) or 'none'}; dates={', '.join(profile.get('special_dates') or []) or 'none'}",
            220,
        ),
        metadata_json={
            "visitor_key": visitor_key,
            "ip_hash": ip_hash,
            "profile": profile,
            "visit_count": int(profile.get("visit_count") or 0),
            "last_seen_at": str(profile.get("last_seen_at") or ""),
        },
    )
    db.add(entry)
    await db.flush()


async def _remember_turn(
    *,
    visitor_key: str,
    ip_hash: str,
    session_key: str,
    role: str,
    mode: str,
    content: str,
    db: AsyncSession,
    attachment: dict[str, Any] | None = None,
) -> None:
    entry = MemoryEntry(
        memory_class="public_guest_turn",
        content=str(content or ""),
        summary=_compact_text(content, 180),
        metadata_json={
            "visitor_key": visitor_key,
            "ip_hash": ip_hash,
            "session_key": session_key,
            "role": role,
            "mode": mode,
            "attachment": attachment if isinstance(attachment, dict) else {},
            "recorded_at": _utc_now_iso(),
        },
    )
    db.add(entry)
    await db.flush()


async def _ensure_public_session(
    *,
    session_key: str,
    visitor_key: str,
    ip_hash: str,
    mode: str,
    db: AsyncSession,
) -> tuple[object, bool]:
    existing = await get_interface_session(session_key=session_key, db=db)
    existing_context = existing.context_json if existing is not None and isinstance(existing.context_json, dict) else {}
    existing_metadata = existing.metadata_json if existing is not None and isinstance(existing.metadata_json, dict) else {}
    channel_context = _public_channel_context(mode)
    row = await upsert_interface_session(
        session_key=session_key,
        actor="visitor",
        source="public_chat",
        channel=str(channel_context["channel"]),
        status="active",
        context_json={
            **existing_context,
            "public_guest_chat": True,
            "visitor_key": visitor_key,
            "last_mode": mode,
            "public_channel": channel_context["channel"],
            "public_application": channel_context["application_name"],
        },
        metadata_json={
            **existing_metadata,
            "public_guest_chat": True,
            "visitor_key": visitor_key,
            "ip_hash": ip_hash,
            "last_mode": mode,
            "public_channel": channel_context["channel"],
            "public_application": channel_context["application_name"],
        },
        db=db,
    )
    return row, existing is None


def _profile_summary(profile: dict[str, Any]) -> str:
    if not isinstance(profile, dict):
        return ""
    parts: list[str] = []
    if str(profile.get("name") or "").strip():
        parts.append(f"I remember your name is {profile['name']}.")
    goals = [str(item) for item in profile.get("goals") or [] if str(item).strip()]
    if goals:
        parts.append(f"Your current goal is {goals[0]}.")
    dates = [str(item) for item in profile.get("special_dates") or [] if str(item).strip()]
    if dates:
        parts.append(f"A date you asked me to remember is {dates[0]}.")
    return " ".join(parts[:3])


def _next_learning_prompt(profile: dict[str, Any], mode: str) -> str:
    name = str(profile.get("name") or "").strip()
    goals = [str(item) for item in profile.get("goals") or [] if str(item).strip()]
    dates = [str(item) for item in profile.get("special_dates") or [] if str(item).strip()]
    if not name:
        return "What should I call you so I can remember you properly next time?"
    if not goals:
        return "What are you trying to make progress on right now?"
    if mode == "mim" and not dates:
        return "Are there any dates, milestones, or personal context points you want me to remember for future chats?"
    return "What is the next thing you want me to remember or help you explore?"


def _public_channel_context(mode: str) -> dict[str, object]:
    return public_channel_definition(_normalize_mode(mode))


def _build_public_fallback_reply(
    *,
    message: str,
    mode: str,
    profile: dict[str, Any],
    recall_summary: str,
    block_reason: str = "",
    upload_summary: str = "",
) -> str:
    normalized_mode = _normalize_mode(mode)
    channel_context = _public_channel_context(normalized_mode)
    query = " ".join(str(message or "").strip().split())
    lowered = query.lower()
    asks_about_mim = "mim" in lowered
    asks_about_tod = "tod" in lowered
    greeting = any(token in lowered for token in ("hello", "hi", "hey", "good morning", "good evening"))
    identity_prompt = any(
        token in lowered
        for token in (
            "who are you",
            "what are you",
            "what is mim",
            "what is tod",
            "what is mim and tod",
            "what are mim and tod",
            "what makes you different",
            "your mission",
            "about mim",
        )
    )
    code_prompt = any(token in lowered for token in ("code", "bug", "debug", "function", "python", "javascript", "typescript", "refactor", "test"))
    image_prompt = any(token in lowered for token in ("image", "logo", "illustration", "poster", "render", "visual"))
    content_prompt = any(token in lowered for token in ("write", "draft", "outline", "story", "post", "email", "content"))
    resource_prompt = any(token in lowered for token in ("resource", "website", "web", "article", "docs", "reference"))

    if block_reason:
        return (
            f"{block_reason} I can still help in conversation mode by planning the work, drafting code, reviewing pasted text, or explaining the next safe steps without touching the live MIM or TOD consoles. "
            f"{_next_learning_prompt(profile, normalized_mode)}"
        )

    if upload_summary:
        base = (
            f"I pulled in your file. {upload_summary} "
            "I can review structure, explain what it does, point out risks, or help you turn it into a sharper draft without touching the live repo."
        )
        return f"{base} {_next_learning_prompt(profile, normalized_mode)}"

    if normalized_mode == "mim":
        if identity_prompt:
            recall_prefix = f"{recall_summary} " if recall_summary else ""
            if asks_about_mim and asks_about_tod:
                return (
                    f"{recall_prefix}I'm {channel_context['application_name']}, the operator-facing application and public channel in the system. {mim_public_identity_summary()} "
                    f"TOD is the separate execution and validation application. {tod_public_identity_summary()} "
                    f"{public_system_identity_summary()}"
                )
            if asks_about_tod and not asks_about_mim:
                return (
                    f"{recall_prefix}TOD is the separate execution-facing application and channel behind the system. {tod_public_identity_summary()} "
                    f"{public_system_identity_summary()}"
                )
            return (
                f"{recall_prefix}I'm {channel_context['application_name']}, the operator-facing application and channel of this multi-agent system. {mim_public_identity_summary()} "
                f"{public_system_identity_summary()}"
            )
        if greeting:
            welcome = "Welcome back. " if int(profile.get("visit_count") or 0) > 1 else ""
            recall_prefix = f"{recall_summary} " if recall_summary else ""
            return (
                f"{welcome}{recall_prefix}You're talking directly to the MIM channel. I'm ready for general chat, planning, content work, idea exploration, and follow-up conversation. "
                f"{_next_learning_prompt(profile, normalized_mode)}"
            )
        if image_prompt:
            return (
                "I can help you shape image prompts, visual direction, brand language, scene composition, and iteration notes. "
                "If you tell me the subject, mood, style, and constraints, I'll turn that into a cleaner creative brief. "
                f"{_next_learning_prompt(profile, normalized_mode)}"
            )
        if content_prompt:
            return (
                "I can draft content in your tone, tighten an outline, generate options, or rewrite a rough idea into something publishable. "
                "Tell me the audience, goal, tone, and length you want. "
                f"{_next_learning_prompt(profile, normalized_mode)}"
            )
        if resource_prompt:
            return (
                "I can help you compare resources, frame better search angles, or analyze excerpts and links you paste here. "
                "If you want a specific source evaluated, send the URL or upload the text and I'll work from that material directly. "
                f"{_next_learning_prompt(profile, normalized_mode)}"
            )
        recall_prefix = f"{recall_summary} " if recall_summary else ""
        return (
            f"{recall_prefix}This is the MIM channel, so I can help you think through ideas, personal goals, creative work, products, writing, and broader questions without switching into operator mode. "
            f"Tell me what you're exploring and I'll stay with the thread. {_next_learning_prompt(profile, normalized_mode)}"
        )

    if code_prompt:
        recall_prefix = f"{recall_summary} " if recall_summary else ""
        return (
            f"{recall_prefix}You're talking directly to TOD, the execution-facing channel, so I can help with architecture, debugging, refactors, tests, APIs, code review, and evidence-backed implementation reasoning. "
            "Paste the code, error, or requirement and I'll reason through it without touching the live repository or execution lanes. "
            f"{_next_learning_prompt(profile, normalized_mode)}"
        )
    if identity_prompt:
        return (
            f"TOD is a separate execution-facing application and public channel behind the system. {tod_public_identity_summary()} "
            f"{public_system_identity_summary()}"
        )
    if greeting:
        welcome = "Welcome back. " if int(profile.get("visit_count") or 0) > 1 else ""
        recall_prefix = f"{recall_summary} " if recall_summary else ""
        return (
            f"{welcome}{recall_prefix}You're talking directly to TOD. I answer from the execution, validation, and evidence side of the system. "
            f"Ask about system state, what ran, what failed, what changed, or bring code and implementation questions. {_next_learning_prompt(profile, normalized_mode)}"
        )
    return (
        "This is the TOD channel, so I answer from the execution and verification side: what changed, what ran, what failed, what evidence exists, and how an implementation should behave. "
        "I can also help with programming conversation, debugging, code explanation, tradeoffs, and implementation planning without touching the live repo. "
        f"{_next_learning_prompt(profile, normalized_mode)}"
    )


async def _compose_public_reply(
    *,
    message: str,
    mode: str,
    profile: dict[str, Any],
    recall_summary: str,
    block_reason: str = "",
    upload_summary: str = "",
) -> str:
    fallback_reply = _build_public_fallback_reply(
        message=message,
        mode=mode,
        profile=profile,
        recall_summary=recall_summary,
        block_reason=block_reason,
        upload_summary=upload_summary,
    )
    channel_context = _public_channel_context(mode)
    counterpart_context = _public_channel_context("mim" if _normalize_mode(mode) == "tod" else "tod")
    context = {
        "assistant_name": str(channel_context["application_name"]),
        "mode": _normalize_mode(mode),
        "public_guest_chat": True,
        "response_mode": "conversational_confident",
        "identity": str(channel_context["identity"]),
        "assistant_identity": str(channel_context["identity"]),
        "assistant_application": str(channel_context["application_name"]),
        "assistant_channel": str(channel_context["channel"]),
        "assistant_scope": str(channel_context["scope"]),
        "assistant_capabilities": str(channel_context["capabilities"]),
        "counterpart_identity": str(counterpart_context["identity"]),
        "counterpart_application": str(counterpart_context["application_name"]),
        "counterpart_channel": str(counterpart_context["channel"]),
        "system_identity": public_system_identity_summary(),
        "visitor_profile": profile,
        "recall_summary": recall_summary,
        "guardrails": [
            "no operator commands",
            "no live system execution",
            "conversation and advisory mode only",
        ],
        "upload_summary": upload_summary,
    }
    reply_contract = await compose_expert_communication_reply(
        user_input=message,
        context=context,
        fallback_reply=fallback_reply,
    )
    return _compact_text(reply_contract.reply_text or fallback_reply, 1400)


async def _build_public_state(
    *,
    session_key: str,
    mode: str,
    request: Request,
    db: AsyncSession,
) -> dict[str, Any]:
    normalized_session = _normalize_session_key(session_key)
    normalized_mode = _normalize_mode(mode)
    visitor_key, ip_hash = _visitor_key_from_session(normalized_session, request)
    profile = await _latest_public_profile(visitor_key=visitor_key, ip_hash=ip_hash, db=db)
    session, is_new = await _ensure_public_session(
        session_key=normalized_session,
        visitor_key=visitor_key,
        ip_hash=ip_hash,
        mode=normalized_mode,
        db=db,
    )
    if is_new:
        updated_profile = {
            **profile,
            "visit_count": int(profile.get("visit_count") or 0) + 1,
            "last_seen_at": _utc_now_iso(),
        }
        profile = _merge_profile(profile, updated_profile)
        await _remember_profile(visitor_key=visitor_key, ip_hash=ip_hash, profile=profile, db=db)
    _, rows = await list_interface_messages(session_key=normalized_session, limit=PUBLIC_CHAT_MESSAGE_LIMIT, db=db)
    recall_summary = _profile_summary(profile)
    return {
        "generated_at": _utc_now_iso(),
        "session": to_interface_session_out(session),
        "messages": [_serialize_message(row) for row in reversed(rows)],
        "mode": normalized_mode,
        "visitor": {
            "visitor_key": visitor_key,
            "visit_count": int(profile.get("visit_count") or 0),
            "name": str(profile.get("name") or "").strip(),
            "goals": [str(item) for item in profile.get("goals") or [] if str(item).strip()],
            "special_dates": [str(item) for item in profile.get("special_dates") or [] if str(item).strip()],
            "memory_summary": recall_summary,
            "ip_hash": ip_hash,
        },
        "guardrails": {
            "commands_blocked": True,
            "live_execution_blocked": True,
            "public_modes": ["mim", "tod"],
        },
    }


def _upload_text_summary(filename: str, content_type: str, text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    first_line = _compact_text(lines[0], 120) if lines else "No non-empty lines detected."
    return _compact_text(
        f"{filename} ({content_type or 'text'}) looks text-based. First meaningful line: {first_line}",
        220,
    )


@router.get(PUBLIC_PRIVACY_POLICY_PATH, response_class=HTMLResponse)
@router.get("/privacy-policy", response_class=HTMLResponse)
async def public_privacy_policy() -> HTMLResponse:
        title = html.escape(f"Privacy Policy | {settings.app_name}")
        return HTMLResponse(
                f"""
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <style>
        :root {{
            --bg: #f5efe6;
            --ink: #102234;
            --muted: #5f6c76;
            --panel: rgba(255,255,255,0.90);
            --line: rgba(16,34,52,0.10);
            --accent: #0b6b74;
            --shadow: 0 24px 64px rgba(16,34,52,0.14);
            --display: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", serif;
            --body: "IBM Plex Sans", "Avenir Next", "Segoe UI", sans-serif;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            color: var(--ink);
            font-family: var(--body);
            background:
                radial-gradient(circle at top left, rgba(11,107,116,0.18), transparent 30%),
                radial-gradient(circle at bottom right, rgba(180,83,9,0.12), transparent 24%),
                linear-gradient(180deg, #fbf8f2 0%, var(--bg) 100%);
            padding: 24px;
        }}
        .page {{
            max-width: 920px;
            margin: 0 auto;
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 28px;
            box-shadow: var(--shadow);
            overflow: hidden;
        }}
        .hero {{
            padding: 28px 28px 20px;
            background: linear-gradient(135deg, rgba(11,107,116,0.10), rgba(255,255,255,0.72));
            border-bottom: 1px solid var(--line);
        }}
        .eyebrow {{
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.14em;
            color: var(--accent);
            font-weight: 800;
        }}
        h1 {{
            margin: 10px 0 8px;
            font-family: var(--display);
            font-size: clamp(30px, 6vw, 50px);
            line-height: 0.95;
        }}
        .intro {{ color: var(--muted); font-size: 15px; line-height: 1.55; max-width: 720px; }}
        .content {{ padding: 24px 28px 28px; display: grid; gap: 22px; }}
        section {{ display: grid; gap: 8px; }}
        h2 {{ margin: 0; font-size: 18px; }}
        p, li {{ margin: 0; color: var(--muted); font-size: 14px; line-height: 1.6; }}
        ul {{ margin: 0; padding-left: 20px; display: grid; gap: 8px; }}
        a {{ color: var(--accent); font-weight: 700; }}
        .back-link {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            text-decoration: none;
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 10px 14px;
            background: rgba(255,255,255,0.82);
            width: fit-content;
        }}
    </style>
</head>
<body>
    <main class="page">
        <header class="hero">
            <div class="eyebrow">Public Policy</div>
            <h1>Privacy Policy</h1>
            <div class="intro">This policy applies to the public MIM and TOD chat surface at todmim.com and mimtod.com. Public chats are recorded so the service can preserve conversation continuity, improve responses, and review safety behavior.</div>
        </header>
        <div class="content">
            <a class="back-link" href="/">Return to Public Chat</a>

            <section>
                <h2>What We Collect</h2>
                <p>We collect the messages you send through the public chat, files you upload for review, lightweight session identifiers stored in your browser, and limited technical metadata such as timestamps and network-derived identifiers used to keep the service stable and resistant to abuse.</p>
            </section>

            <section>
                <h2>Why We Record Chats</h2>
                <p>Public chats are recorded to improve the service, preserve follow-up context, evaluate quality, and review safety issues. This includes helping MIM remember information you intentionally share for future conversations on the same public surface.</p>
            </section>

            <section>
                <h2>How We Use Information</h2>
                <ul>
                    <li>To respond to your messages and uploaded content.</li>
                    <li>To maintain visitor memory and conversation continuity.</li>
                    <li>To analyze failures, misuse, and safety issues.</li>
                    <li>To improve product quality, prompts, routing, and moderation.</li>
                </ul>
            </section>

            <section>
                <h2>Public Surface Limits</h2>
                <p>The public chat is a conversational surface only. It is not an operator console and it does not execute live commands against MIM, TOD, the repository, or runtime systems.</p>
            </section>

            <section>
                <h2>Sensitive Information</h2>
                <p>Do not share passwords, private keys, financial account numbers, or other highly sensitive information through the public chat. If you upload files, only upload material you are comfortable having processed for conversational review and service improvement.</p>
            </section>

            <section>
                <h2>Retention</h2>
                <p>We may retain public chat records, uploads, and derived memory summaries for continuity, auditing, and improvement purposes. Retention periods may vary based on operational, safety, and debugging needs.</p>
            </section>

            <section>
                <h2>Contact</h2>
                <p>Entity: {MIM_LEGAL_ENTITY_NAME}. Contact: <a href="mailto:{MIM_LEGAL_CONTACT_EMAIL}">{MIM_LEGAL_CONTACT_EMAIL}</a>. Jurisdiction: {MIM_LEGAL_JURISDICTION}.</p>
            </section>
        </div>
    </main>
</body>
</html>
                """
        )


@router.get("/", response_class=HTMLResponse)
async def public_chat_home() -> HTMLResponse:
        title = html.escape(f"{settings.app_name} | MIM + TOD")
        login_href = "/mim/login?next=/mim"
        configured_mim_domain = str(settings.remote_shell_domain or "").strip().rstrip("/")
        if configured_mim_domain:
            login_href = f"{configured_mim_domain}/mim/login?next=/mim"
        return HTMLResponse(
                f"""
<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>{title}</title>
    <style>
        :root {{
            --bg: #071019;
            --bg-strong: #0b1621;
            --panel: rgba(12, 24, 35, 0.92);
            --panel-strong: rgba(15, 30, 43, 0.98);
            --ink: #e9f0f5;
            --muted: #8ea2b4;
            --line: rgba(143, 169, 187, 0.18);
            --mim: #4dc4d3;
            --mim-strong: #8ce8f2;
            --tod: #ff9b54;
            --tod-strong: #ffc089;
            --shadow: 0 28px 80px rgba(0, 0, 0, 0.42);
            --display: \"Iowan Old Style\", \"Palatino Linotype\", \"Book Antiqua\", serif;
            --body: \"IBM Plex Sans\", \"Avenir Next\", \"Segoe UI\", sans-serif;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            min-height: 100vh;
            color: var(--ink);
            font-family: var(--body);
            background:
                radial-gradient(circle at top left, rgba(77,196,211,0.16), transparent 26%),
                radial-gradient(circle at top right, rgba(255,155,84,0.12), transparent 24%),
                linear-gradient(180deg, #040a11 0%, var(--bg) 100%);
        }}
        .shell {{
            max-width: 1220px;
            margin: 0 auto;
            padding: 20px;
            display: grid;
            gap: 18px;
            min-height: 100vh;
        }}
        .topbar, .stage {{
            border: 1px solid var(--line);
            background: var(--panel);
            backdrop-filter: blur(16px);
            box-shadow: var(--shadow);
            border-radius: 28px;
        }}
        .topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            background: rgba(9, 19, 28, 0.88);
        }}
        .topbar-title {{
            margin: 0;
            font-family: var(--display);
            font-size: 26px;
            letter-spacing: 0.04em;
            color: var(--ink);
        }}
        .login-icon {{
            width: 42px;
            height: 42px;
            border-radius: 999px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: var(--muted);
            text-decoration: none;
            border: 1px solid transparent;
            background: rgba(255,255,255,0.02);
            transition: border-color 120ms ease, color 120ms ease, background 120ms ease;
        }}
        .login-icon:hover,
        .login-icon:focus-visible {{
            color: var(--ink);
            border-color: var(--line);
            background: rgba(255,255,255,0.05);
            outline: none;
        }}
        .login-icon svg {{ width: 20px; height: 20px; display: block; }}
        .stage {{ display: grid; grid-template-rows: auto 1fr auto; min-height: calc(100vh - 90px); overflow: hidden; }}
        .stage-head {{ padding: 26px 28px 20px; border-bottom: 1px solid var(--line); display: grid; gap: 16px; background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0)); }}
        .stage-copy {{ color: var(--muted); font-size: 14px; line-height: 1.55; max-width: 720px; }}
        .mode-row {{ display: flex; gap: 10px; flex-wrap: wrap; }}
        .mode-btn {{
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 10px 16px;
            text-align: left;
            background: rgba(255,255,255,0.03);
            color: var(--ink);
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 13px;
            font-weight: 700;
        }}
        .mode-btn.active[data-mode=\"mim\"] {{ border-color: rgba(77,196,211,0.48); box-shadow: inset 0 0 0 1px rgba(77,196,211,0.18); color: var(--mim-strong); }}
        .mode-btn.active[data-mode=\"tod\"] {{ border-color: rgba(255,155,84,0.52); box-shadow: inset 0 0 0 1px rgba(255,155,84,0.18); color: var(--tod-strong); }}
        .messages {{ padding: 20px; overflow: auto; display: flex; flex-direction: column; gap: 14px; background: linear-gradient(180deg, rgba(255,255,255,0.04), rgba(255,255,255,0.01)); }}
        .message {{ max-width: 860px; border-radius: 24px; padding: 16px 18px; border: 1px solid var(--line); background: rgba(19, 35, 48, 0.86); box-shadow: 0 10px 24px rgba(0,0,0,0.18); }}
        .message.user {{ margin-left: auto; background: linear-gradient(135deg, #11293b, #183b4e); color: white; border-color: rgba(17,41,59,0.92); }}
        .message.system {{ background: rgba(255,155,84,0.08); border-color: rgba(255,155,84,0.18); }}
        .message-meta {{ font-size: 11px; letter-spacing: 0.10em; text-transform: uppercase; opacity: 0.7; margin-bottom: 8px; }}
        .message-content {{ white-space: pre-wrap; line-height: 1.6; font-size: 15px; }}
        .message-content.intro {{ white-space: normal; line-height: 1.45; }}
        .intro-list {{ margin: 4px 0 6px; padding-left: 18px; color: inherit; }}
        .intro-list li {{ margin: 0 0 2px; }}
        .intro-list li:last-child {{ margin-bottom: 0; }}
        .intro-copy {{ display: block; margin: 0 0 6px; white-space: normal; }}
        .intro-copy:last-child {{ margin-bottom: 0; }}
        .intro-note {{ color: var(--muted); }}
        .composer {{ padding: 18px 20px 20px; border-top: 1px solid var(--line); display: grid; gap: 12px; background: rgba(8,16,24,0.94); }}
        .composer-tools {{ display: grid; gap: 12px; }}
        .hint {{ color: var(--muted); font-size: 12px; }}
        .disclaimer {{ color: var(--muted); font-size: 12px; line-height: 1.5; }}
        .disclaimer a {{ color: var(--mim-strong); font-weight: 800; }}
        .tool-row {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
        .upload-btn, .send-btn {{
            border: 0;
            border-radius: 16px;
            padding: 12px 16px;
            font: inherit;
            font-weight: 800;
            cursor: pointer;
            color: white;
        }}
        .upload-btn {{ background: linear-gradient(135deg, #254054, #183141); }}
        .send-btn {{ background: linear-gradient(135deg, var(--mim), var(--mim-strong)); min-width: 120px; }}
        .composer-row {{ display: grid; grid-template-columns: minmax(0, 1fr) 132px; gap: 12px; }}
        textarea {{ min-height: 120px; resize: vertical; border-radius: 20px; border: 1px solid var(--line); padding: 16px; font: inherit; background: rgba(255,255,255,0.03); color: var(--ink); }}
        .upload-status {{ color: var(--muted); font-size: 13px; min-height: 20px; }}
        .starter-row {{ display: flex; gap: 10px; flex-wrap: wrap; }}
        .starter-chip {{ border: 1px solid var(--line); color: var(--ink); background: rgba(255,255,255,0.03); border-radius: 999px; padding: 10px 12px; font-size: 12px; cursor: pointer; }}
        .dropzone {{
            border: 1px dashed rgba(143, 169, 187, 0.34);
            border-radius: 22px;
            padding: 18px;
            display: grid;
            gap: 8px;
            background: rgba(255,255,255,0.02);
            transition: border-color 120ms ease, background 120ms ease;
        }}
        .dropzone.active {{
            border-color: rgba(77,196,211,0.58);
            background: rgba(77,196,211,0.08);
        }}
        .dropzone-title {{ font-size: 13px; font-weight: 700; color: var(--ink); }}
        .dropzone-copy {{ color: var(--muted); font-size: 12px; line-height: 1.5; }}
        input[type=file] {{ display: none; }}
        @media (max-width: 720px) {{
            .shell {{ padding: 12px; gap: 12px; }}
            .stage-head, .messages, .composer {{ padding-left: 14px; padding-right: 14px; }}
            .composer-row {{ grid-template-columns: 1fr; }}
            .send-btn {{ width: 100%; }}
            .message {{ max-width: 100%; }}
        }}
    </style>
</head>
<body>
    <main class=\"shell\">
        <header class=\"topbar\">
            <h1 class=\"topbar-title\">MIM &amp; TOD</h1>
            <a class=\"login-icon\" href=\"{login_href}\" aria-label=\"Login\" title=\"Login\">
                <svg viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"1.7\" stroke-linecap=\"round\" stroke-linejoin=\"round\" aria-hidden=\"true\">
                    <path d=\"M9 4 7 2\" />
                    <path d=\"M15 4 17 2\" />
                    <path d=\"M6.5 14.5c0-4 2.4-7.5 5.5-7.5s5.5 3.5 5.5 7.5c0 3.1-2.5 5.5-5.5 5.5s-5.5-2.4-5.5-5.5Z\" />
                    <circle cx=\"9.5\" cy=\"13\" r=\"1\" fill=\"currentColor\" stroke=\"none\" />
                    <circle cx=\"14.5\" cy=\"13\" r=\"1\" fill=\"currentColor\" stroke=\"none\" />
                    <path d=\"M9.5 16.5c1 .7 4 .7 5 0\" />
                </svg>
            </a>
        </header>

        <section class=\"stage\">
            <header class=\"stage-head\">
                <div>
                    <div id="stageCopy" class="stage-copy">Talk to a system that doesn't just respond. It tries to act, verify, and improve.</div>
                </div>
                <div class=\"mode-row\">
                    <button class=\"mode-btn active\" data-mode=\"mim\" type=\"button\">MIM</button>
                    <button class=\"mode-btn\" data-mode=\"tod\" type=\"button\">TOD</button>
                </div>
            </header>

            <section id=\"messages\" class=\"messages\"></section>

            <section class=\"composer\">
                <div class=\"starter-row\">
                    <button class="starter-chip" type="button" data-starter="What is this system?">What is this system?</button>
                    <button class="starter-chip" type="button" data-starter="What are you working on right now?">What are you working on right now?</button>
                    <button class="starter-chip" type="button" data-starter="Show me how you execute a task.">Show me how you execute a task.</button>
                </div>
                <div class=\"composer-tools\">
                    <div class=\"hint\">Drop text, code, docs, or image references here for review.</div>
                    <div id=\"dropzone\" class=\"dropzone\" role=\"button\" tabindex=\"0\" aria-label=\"Drop file here or upload a file\">
                        <div class=\"dropzone-title\">Drop file here</div>
                        <div class=\"dropzone-copy\">Or upload a file if you prefer.</div>
                        <div class=\"tool-row\">
                            <label class=\"upload-btn\" for=\"fileInput\">Upload File</label>
                        </div>
                        <input id=\"fileInput\" type=\"file\" />
                    </div>
                </div>
                <div class=\"disclaimer\">Chats are recorded to improve the service and are processed in accordance with our <a href=\"{PUBLIC_PRIVACY_POLICY_PATH}\">Privacy Policy</a>.</div>
                <div id=\"uploadStatus\" class=\"upload-status\"></div>
                <div class=\"composer-row\">
                    <textarea id=\"messageInput\" placeholder=\"Ask a question, paste code, request a draft, or start a conversation...\"></textarea>
                    <button id=\"sendBtn\" class=\"send-btn\" type=\"button\">Send</button>
                </div>
            </section>
        </section>
    </main>

    <script>
        const modeButtons = Array.from(document.querySelectorAll('[data-mode]'));
        const starterButtons = Array.from(document.querySelectorAll('[data-starter]'));
        const stageCopy = document.getElementById('stageCopy');
        const messagesEl = document.getElementById('messages');
        const messageInput = document.getElementById('messageInput');
        const sendBtn = document.getElementById('sendBtn');
        const uploadStatus = document.getElementById('uploadStatus');
        const fileInput = document.getElementById('fileInput');
        const dropzone = document.getElementById('dropzone');

        const MODE_COPY = {{
            mim: {{
                placeholder: 'Ask MIM a question, request a draft, or start a conversation...'
            }},
            tod: {{
                placeholder: 'Paste code, describe the bug, or ask for architecture help...'
            }}
        }};

        function safeText(value, fallback = '') {{
            const text = String(value || '').trim();
            return text || fallback;
        }}

        function currentMode() {{
            const stored = safeText(localStorage.getItem('mim_public_mode'), 'mim').toLowerCase();
            return stored === 'tod' ? 'tod' : 'mim';
        }}

        function baseVisitorId() {{
            let value = safeText(localStorage.getItem('mim_public_visitor_id'));
            if (!value) {{
                if (window.crypto && typeof window.crypto.randomUUID === 'function') {{
                    value = `visitor-${{window.crypto.randomUUID()}}`;
                }} else {{
                    value = `visitor-${{Date.now().toString(36)}}-${{Math.random().toString(36).slice(2, 10)}}`;
                }}
                localStorage.setItem('mim_public_visitor_id', value);
            }}
            return value;
        }}

        function sessionKeyForMode(mode) {{
            return `${{baseVisitorId()}}-${{mode}}`;
        }}

        function applyMode(mode) {{
            localStorage.setItem('mim_public_mode', mode);
            for (const button of modeButtons) {{
                button.classList.toggle('active', button.dataset.mode === mode);
            }}
            const copy = MODE_COPY[mode] || MODE_COPY.mim;
            messageInput.placeholder = copy.placeholder;
        }}

        function escapeHtml(value) {{
            return String(value || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }}

        function visitorFirstName(visitor) {{
            const fullName = safeText(visitor && visitor.name);
            return fullName ? fullName.split(/\\s+/)[0] : '';
        }}

        function firstVisitIntro(mode) {{
            if (mode === 'tod') {{
                return `
                    <div class="message-meta">TOD</div>
                    <div class="message-content intro">
                        <div class="intro-copy">Hi - I'm TOD. I verify execution. If something actually happens, I'm the part of the system that confirms it. MIM coordinates what should happen. I help show what actually did.</div>
                        <div class="intro-copy">You can ask me anything, or try something like:</div>
                        <ul class="intro-list">
                            <li>"What is this system?"</li>
                            <li>"What are you working on right now?"</li>
                            <li>"Show me how you execute a task"</li>
                        </ul>
                        <div class="intro-copy">If something doesn't make sense, I'll try to explain it. If something fails, I'll show you that too.</div>
                    </div>
                `;
            }}
            return `
                <div class="message-meta">MIM</div>
                <div class="message-content intro">
                    <div class="intro-copy">Hi - I'm MIM. I help coordinate what should happen, and I work with TOD to verify what actually does.</div>
                    <div class="intro-copy">You can ask me anything, or try something like:</div>
                    <ul class="intro-list">
                        <li>"What is this system?"</li>
                        <li>"What are you working on right now?"</li>
                        <li>"Show me how you execute a task"</li>
                    </ul>
                    <div class="intro-copy">If something doesn't make sense, I'll try to explain it. If something fails, I'll show you that too.</div>
                    <div class="intro-copy intro-note">TOD is the part of the system that verifies execution. If something actually happens, TOD is the one that confirms it.</div>
                </div>
            `;
        }}

        function returningIntro(visitor, mode) {{
            const firstName = visitorFirstName(visitor);
            const greeting = `Hi${{firstName ? ` ${{escapeHtml(firstName)}}` : ''}} - `;
            const goals = Array.isArray(visitor && visitor.goals) ? visitor.goals : [];
            const leadGoal = safeText(goals[0]);
            const summary = safeText(visitor && visitor.memory_summary);
            const base = mode === 'tod'
                ? 'Want to keep going on that, debug something new, or review a file?'
                : 'What do you want to explore next?';

            if (leadGoal) {{
                return `
                    <div class="message-meta">${{mode === 'tod' ? 'TOD' : 'MIM'}}</div>
                    <div class="message-content intro">
                        <div class="intro-copy">${{greeting}}last time we chatted you were focused on ${{escapeHtml(leadGoal)}}.</div>
                        <div class="intro-copy">${{base}}</div>
                    </div>
                `;
            }}

            if (summary) {{
                return `
                    <div class="message-meta">${{mode === 'tod' ? 'TOD' : 'MIM'}}</div>
                    <div class="message-content intro">
                        <div class="intro-copy">${{greeting}}last time we chatted, we left off with some context I still have in view.</div>
                        <div class="intro-copy">${{escapeHtml(summary)}}</div>
                        <div class="intro-copy">${{base}}</div>
                    </div>
                `;
            }}

            return `
                <div class="message-meta">${{mode === 'tod' ? 'TOD' : 'MIM'}}</div>
                <div class="message-content intro">
                    <div class="intro-copy">${{greeting}}good to see you again.</div>
                    <div class="intro-copy">${{base}}</div>
                </div>
            `;
        }}

        function emptyStateMarkup(visitor, mode) {{
            const visitCount = Number((visitor && visitor.visit_count) || 0);
            if (visitCount > 1) {{
                return returningIntro(visitor || {{}}, mode);
            }}
            return firstVisitIntro(mode);
        }}

        function renderMessages(messages, visitor, mode) {{
            messagesEl.innerHTML = '';
            if (!Array.isArray(messages) || !messages.length) {{
                const empty = document.createElement('article');
                empty.className = 'message system';
                empty.innerHTML = emptyStateMarkup(visitor, mode);
                messagesEl.appendChild(empty);
                return;
            }}
            for (const message of messages) {{
                const role = safeText(message.role, 'mim').toLowerCase();
                const article = document.createElement('article');
                article.className = `message ${{role === 'visitor' || role === 'operator' ? 'user' : role === 'system' ? 'system' : ''}}`.trim();
                const meta = document.createElement('div');
                meta.className = 'message-meta';
                meta.textContent = `${{safeText(message.role, 'mim')}} · ${{safeText(message.created_at, 'now')}}`;
                const content = document.createElement('div');
                content.className = 'message-content';
                content.textContent = safeText(message.content, '');
                article.appendChild(meta);
                article.appendChild(content);
                if (message.attachment && typeof message.attachment === 'object') {{
                    const attachment = document.createElement('div');
                    attachment.className = 'message-meta';
                    attachment.textContent = `attachment · ${{safeText(message.attachment.filename, 'file')}}`;
                    article.appendChild(attachment);
                }}
                messagesEl.appendChild(article);
            }}
            messagesEl.scrollTop = messagesEl.scrollHeight;
        }}

        async function refreshState() {{
            const mode = currentMode();
            const sessionKey = sessionKeyForMode(mode);
            const res = await fetch(`/public/chat/state?session_key=${{encodeURIComponent(sessionKey)}}&mode=${{encodeURIComponent(mode)}}`, {{ cache: 'no-store' }});
            const payload = await res.json();
            applyMode(mode);
            renderMessages(payload.messages || [], payload.visitor || {{}}, mode);
        }}

        async function sendMessage() {{
            const message = safeText(messageInput.value);
            if (!message) return;
            const mode = currentMode();
            const sessionKey = sessionKeyForMode(mode);
            sendBtn.disabled = true;
            try {{
                const res = await fetch('/public/chat/message', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ message, mode, session_key: sessionKey }}),
                }});
                await res.json();
                messageInput.value = '';
                await refreshState();
            }} finally {{
                sendBtn.disabled = false;
            }}
        }}

        async function uploadFile(file) {{
            if (!file) return;
            const mode = currentMode();
            const sessionKey = sessionKeyForMode(mode);
            uploadStatus.textContent = `Uploading ${{file.name}}...`;
            const formData = new FormData();
            formData.append('session_key', sessionKey);
            formData.append('mode', mode);
            formData.append('file', file);
            const res = await fetch('/public/chat/upload', {{ method: 'POST', body: formData }});
            const payload = await res.json();
            uploadStatus.textContent = safeText(payload.summary, `Uploaded ${{file.name}}.`);
            fileInput.value = '';
            await refreshState();
        }}

        modeButtons.forEach((button) => {{
            button.addEventListener('click', async () => {{
                applyMode(button.dataset.mode);
                await refreshState();
            }});
        }});

        starterButtons.forEach((button) => {{
            button.addEventListener('click', () => {{
                messageInput.value = safeText(button.dataset.starter);
                messageInput.focus();
            }});
        }});

        sendBtn.addEventListener('click', sendMessage);
        messageInput.addEventListener('keydown', (event) => {{
            if (event.key === 'Enter' && !event.shiftKey) {{
                event.preventDefault();
                sendMessage();
            }}
        }});
        fileInput.addEventListener('change', (event) => uploadFile(event.target.files && event.target.files[0]));
        dropzone.addEventListener('click', () => fileInput.click());
        dropzone.addEventListener('keydown', (event) => {{
            if (event.key === 'Enter' || event.key === ' ') {{
                event.preventDefault();
                fileInput.click();
            }}
        }});
        ['dragenter', 'dragover'].forEach((eventName) => {{
            dropzone.addEventListener(eventName, (event) => {{
                event.preventDefault();
                dropzone.classList.add('active');
            }});
        }});
        ['dragleave', 'dragend', 'drop'].forEach((eventName) => {{
            dropzone.addEventListener(eventName, (event) => {{
                event.preventDefault();
                dropzone.classList.remove('active');
            }});
        }});
        dropzone.addEventListener('drop', (event) => {{
            const files = event.dataTransfer && event.dataTransfer.files;
            uploadFile(files && files[0]);
        }});

        applyMode(currentMode());
        refreshState();
    </script>
</body>
</html>
                """
        )


@router.get("/public/chat/state")
async def public_chat_state(
    request: Request,
    session_key: str = Query(...),
    mode: str = Query(default="mim"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await _build_public_state(
        session_key=session_key,
        mode=mode,
        request=request,
        db=db,
    )


@router.post("/public/chat/message")
async def public_chat_message(
    payload: PublicChatMessageRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    normalized_mode = _normalize_mode(payload.mode)
    channel_context = _public_channel_context(normalized_mode)
    normalized_session = _normalize_session_key(payload.session_key)
    visitor_key, ip_hash = _visitor_key_from_session(normalized_session, request)
    profile = await _latest_public_profile(visitor_key=visitor_key, ip_hash=ip_hash, db=db)
    profile_updates = _extract_profile_updates(payload.message)
    updated_profile = _merge_profile(profile, profile_updates)
    updated_profile["visit_count"] = max(1, int(profile.get("visit_count") or 0))
    updated_profile["last_seen_at"] = _utc_now_iso()
    recall_summary = _profile_summary(profile)
    session, _ = await _ensure_public_session(
        session_key=normalized_session,
        visitor_key=visitor_key,
        ip_hash=ip_hash,
        mode=normalized_mode,
        db=db,
    )
    _, inbound = await append_interface_message(
        session_key=normalized_session,
        actor="visitor",
        source="public_chat",
        direction="inbound",
        role="visitor",
        content=str(payload.message).strip(),
        parsed_intent=f"public_{normalized_mode}_chat",
        confidence=1.0,
        requires_approval=False,
        metadata_json={
            "message_type": "visitor_message",
            "mode": normalized_mode,
            "public_guest_chat": True,
            "public_channel": channel_context["channel"],
            "public_application": channel_context["application_name"],
        },
        db=db,
    )
    await _remember_turn(
        visitor_key=visitor_key,
        ip_hash=ip_hash,
        session_key=normalized_session,
        role="visitor",
        mode=normalized_mode,
        content=str(payload.message).strip(),
        db=db,
    )
    block_reason = _public_command_block_reason(payload.message)
    reply_text = await _compose_public_reply(
        message=str(payload.message).strip(),
        mode=normalized_mode,
        profile=updated_profile,
        recall_summary=recall_summary,
        block_reason=block_reason,
    )
    _, reply = await append_interface_message(
        session_key=normalized_session,
        actor="mim" if normalized_mode == "mim" else "tod",
        source="public_chat",
        direction="outbound",
        role="mim" if normalized_mode == "mim" else "tod",
        content=reply_text,
        parsed_intent="public_chat_reply",
        confidence=1.0,
        requires_approval=False,
        metadata_json={
            "message_type": "assistant_reply",
            "mode": normalized_mode,
            "public_guest_chat": True,
            "public_channel": channel_context["channel"],
            "public_application": channel_context["application_name"],
        },
        db=db,
    )
    await _remember_turn(
        visitor_key=visitor_key,
        ip_hash=ip_hash,
        session_key=normalized_session,
        role="assistant",
        mode=normalized_mode,
        content=reply_text,
        db=db,
    )
    await _remember_profile(visitor_key=visitor_key, ip_hash=ip_hash, profile=updated_profile, db=db)
    await db.commit()
    return {
        "status": "accepted",
        "session": to_interface_session_out(session),
        "message": _serialize_message(inbound),
        "reply": _serialize_message(reply),
        "blocked": bool(block_reason),
        "block_reason": block_reason,
    }


@router.post("/public/chat/upload")
async def public_chat_upload(
    request: Request,
    session_key: str = Form(...),
    mode: str = Form(default="mim"),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    normalized_mode = _normalize_mode(mode)
    channel_context = _public_channel_context(normalized_mode)
    normalized_session = _normalize_session_key(session_key)
    visitor_key, ip_hash = _visitor_key_from_session(normalized_session, request)
    profile = await _latest_public_profile(visitor_key=visitor_key, ip_hash=ip_hash, db=db)
    await _ensure_public_session(
        session_key=normalized_session,
        visitor_key=visitor_key,
        ip_hash=ip_hash,
        mode=normalized_mode,
        db=db,
    )
    raw_bytes = await file.read(PUBLIC_CHAT_UPLOAD_LIMIT_BYTES + 1)
    if len(raw_bytes) > PUBLIC_CHAT_UPLOAD_LIMIT_BYTES:
        raise HTTPException(status_code=413, detail="public_upload_too_large")

    filename = str(file.filename or "upload").strip() or "upload"
    content_type = str(file.content_type or "application/octet-stream").strip()
    extension = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    text_preview = ""
    upload_summary = ""
    attachment = {
        "filename": filename,
        "content_type": content_type,
        "size_bytes": len(raw_bytes),
    }
    if content_type.startswith("text/") or extension in PUBLIC_TEXT_UPLOAD_EXTENSIONS:
        text_preview = raw_bytes.decode("utf-8", errors="replace")[:8000]
        attachment["preview"] = text_preview[:1200]
        upload_summary = _upload_text_summary(filename, content_type, text_preview)
    elif content_type.startswith("image/"):
        upload_summary = _compact_text(
            f"{filename} is an image reference ({content_type or 'image'}). I can help with prompt design, composition, style direction, and critique based on the file you uploaded.",
            220,
        )
    else:
        upload_summary = _compact_text(
            f"{filename} uploaded successfully. I can use the file metadata and any pasted excerpts you provide to discuss it in conversation mode.",
            220,
        )

    inbound_text = f"Uploaded file: {filename}"
    if text_preview:
        inbound_text = f"Uploaded file: {filename}\n\n{text_preview[:2000]}"
    _, inbound = await append_interface_message(
        session_key=normalized_session,
        actor="visitor",
        source="public_chat_upload",
        direction="inbound",
        role="visitor",
        content=inbound_text,
        parsed_intent="public_chat_upload",
        confidence=1.0,
        requires_approval=False,
        metadata_json={
            "message_type": "upload",
            "mode": normalized_mode,
            "attachment": attachment,
            "public_guest_chat": True,
            "public_channel": channel_context["channel"],
            "public_application": channel_context["application_name"],
        },
        db=db,
    )
    reply_text = await _compose_public_reply(
        message=f"uploaded file {filename}",
        mode=normalized_mode,
        profile=profile,
        recall_summary=_profile_summary(profile),
        upload_summary=upload_summary,
    )
    _, reply = await append_interface_message(
        session_key=normalized_session,
        actor="mim" if normalized_mode == "mim" else "tod",
        source="public_chat_upload",
        direction="outbound",
        role="mim" if normalized_mode == "mim" else "tod",
        content=reply_text,
        parsed_intent="public_chat_upload_reply",
        confidence=1.0,
        requires_approval=False,
        metadata_json={
            "message_type": "assistant_reply",
            "mode": normalized_mode,
            "attachment": attachment,
            "public_guest_chat": True,
            "public_channel": channel_context["channel"],
            "public_application": channel_context["application_name"],
        },
        db=db,
    )
    await _remember_turn(
        visitor_key=visitor_key,
        ip_hash=ip_hash,
        session_key=normalized_session,
        role="visitor",
        mode=normalized_mode,
        content=inbound_text,
        db=db,
        attachment=attachment,
    )
    await _remember_turn(
        visitor_key=visitor_key,
        ip_hash=ip_hash,
        session_key=normalized_session,
        role="assistant",
        mode=normalized_mode,
        content=reply_text,
        db=db,
        attachment=attachment,
    )
    await db.commit()
    return {
        "status": "accepted",
        "summary": upload_summary,
        "message": _serialize_message(inbound),
        "reply": _serialize_message(reply),
        "attachment": attachment,
    }