from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import html
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core.autonomy_driver_service import build_initiative_status
from core.config import settings
from core.db import get_db
from core.interface_service import (
    append_interface_message,
    get_interface_session,
    list_interface_messages,
    to_interface_message_out,
    to_interface_session_out,
    upsert_interface_session,
)
from core.schemas import TextInputAdapterRequest
from core.ui_health_service import build_mim_ui_health_snapshot


router = APIRouter(tags=["shell"])

DEFAULT_SHELL_SESSION_KEY = "travel_shell"
SHELL_MESSAGE_LIMIT = 120

DESTRUCTIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(delete|destroy|wipe|erase|drop|remove)\b.*\b(repo|database|runtime|logs|history|records|files?)\b", re.IGNORECASE),
    re.compile(r"\b(git\s+reset\s+--hard|rm\s+-rf|format\s+disk|truncate\s+database)\b", re.IGNORECASE),
)
LARGE_REFACTOR_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(large\s+refactor|full\s+rewrite|rewrite\s+the\s+system|rename\s+across\s+the\s+repo|mass\s+rename)\b", re.IGNORECASE),
    re.compile(r"\b(refactor|rewrite)\b.*\b(entire|whole|all|across\s+the\s+repo|global)\b", re.IGNORECASE),
)
HARDWARE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(reboot|shutdown|power\s*off|systemctl|service\s+restart)\b", re.IGNORECASE),
    re.compile(r"\b(robot|arm|gripper|motor|camera\s+mount|hardware)\b.*\b(move|open|close|reset|home|drive|actuate)\b", re.IGNORECASE),
)


class ShellChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_key: str = DEFAULT_SHELL_SESSION_KEY


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_shell_session_key(value: object) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._:-]+", "-", str(value or "").strip())
    return normalized.strip("-._:") or DEFAULT_SHELL_SESSION_KEY


def _shell_travel_mode_policy() -> dict[str, object]:
    return {
        "enabled": bool(settings.travel_mode_enabled),
        "allow_destructive": bool(settings.travel_mode_allow_destructive),
        "allow_large_refactors": bool(settings.travel_mode_allow_large_refactors),
        "allow_hardware_actions": bool(settings.travel_mode_allow_hardware_actions),
        "allowed_work": [
            "bounded_training",
            "ui_improvements",
            "state_validation",
            "parsing_fixes",
            "bounded_runtime_patches",
        ],
        "blocked_categories": [
            category
            for category, allowed in (
                ("destructive_changes", settings.travel_mode_allow_destructive),
                ("large_refactors", settings.travel_mode_allow_large_refactors),
                ("hardware_actions", settings.travel_mode_allow_hardware_actions),
            )
            if not allowed
        ],
    }


def _travel_mode_block_reason(message: str) -> str:
    policy = _shell_travel_mode_policy()
    if not bool(policy.get("enabled")):
        return ""
    text = str(message or "").strip()
    if not text:
        return ""
    if not bool(policy.get("allow_destructive")) and any(pattern.search(text) for pattern in DESTRUCTIVE_PATTERNS):
        return "Travel mode blocks destructive changes from the remote shell."
    if not bool(policy.get("allow_large_refactors")) and any(pattern.search(text) for pattern in LARGE_REFACTOR_PATTERNS):
        return "Travel mode blocks large refactors from the remote shell."
    if not bool(policy.get("allow_hardware_actions")) and any(pattern.search(text) for pattern in HARDWARE_PATTERNS):
        return "Travel mode blocks hardware and host-control actions from the remote shell."
    return ""


def _compact_text(value: object, limit: int = 220) -> str:
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _shell_health_flags(health: dict[str, object]) -> tuple[bool, bool]:
    checks = health.get("checks") if isinstance(health.get("checks"), dict) else {}
    backend_check = checks.get("backend") if isinstance(checks.get("backend"), dict) else {}
    database_check = checks.get("database") if isinstance(checks.get("database"), dict) else {}

    db_ok = health.get("db_ok")
    if db_ok is None:
        db_ok = database_check.get("ok")
    if db_ok is None:
        db_ok = str(health.get("status") or "").strip().lower() in {"healthy", "ok"}

    runtime_ready = health.get("runtime_ready")
    if runtime_ready is None:
        runtime_ready = backend_check.get("ok")
    if runtime_ready is None:
        runtime_ready = str(health.get("status") or "").strip().lower() in {"healthy", "ok"}

    return bool(db_ok), bool(runtime_ready)


def _normalize_remote_shell_public_base(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = text if "://" in text else f"https://{text}"
    try:
        parsed = SimpleNamespace(url=re.sub(r"/$", "", candidate))
        from urllib.parse import urlparse

        parts = urlparse(parsed.url)
    except Exception:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    path = (parts.path or "").rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def _build_public_shell_urls(value: object) -> dict[str, str]:
    base = _normalize_remote_shell_public_base(value)
    if not base:
        return {
            "remote_shell_domain": "",
            "public_base_url": "",
            "public_shell_url": "",
            "public_state_url": "",
            "public_health_url": "",
        }
    if base.endswith("/shell"):
        public_shell_url = base
        public_base_url = base[: -len("/shell")]
    else:
        public_base_url = base
        public_shell_url = f"{public_base_url}/shell"
    return {
        "remote_shell_domain": base,
        "public_base_url": public_base_url,
        "public_shell_url": public_shell_url,
        "public_state_url": f"{public_shell_url}/state",
        "public_health_url": f"{public_shell_url}/health",
    }


def _serialize_shell_message(row: object) -> dict[str, object]:
    payload = to_interface_message_out(row)
    metadata = payload.get("metadata_json") if isinstance(payload.get("metadata_json"), dict) else {}
    return {
        "message_id": int(payload.get("message_id") or 0),
        "role": str(payload.get("role") or "mim").strip(),
        "direction": str(payload.get("direction") or "outbound").strip(),
        "content": str(payload.get("content") or "").strip(),
        "created_at": payload.get("created_at"),
        "message_type": str(metadata.get("message_type") or "message").strip(),
        "delivery_status": str(payload.get("delivery_status") or "accepted").strip(),
    }


async def _ensure_shell_session(*, session_key: str, db: AsyncSession):
    existing = await get_interface_session(session_key=session_key, db=db)
    if existing is not None:
        return existing
    return await upsert_interface_session(
        session_key=session_key,
        actor="operator",
        source="shell",
        channel="chat",
        status="active",
        context_json={"travel_shell": True},
        metadata_json={"conversation_session_id": session_key, "travel_shell": True},
        db=db,
    )


async def _load_shell_thread(*, session_key: str, db: AsyncSession) -> dict[str, object]:
    session = await _ensure_shell_session(session_key=session_key, db=db)
    _, rows = await list_interface_messages(session_key=session_key, limit=SHELL_MESSAGE_LIMIT, db=db)
    return {
        "session": to_interface_session_out(session),
        "messages": [_serialize_shell_message(row) for row in reversed(rows)],
        "primary_thread": session_key,
    }


def _build_shell_blockers(*, initiative: dict[str, object], health: dict[str, object]) -> list[str]:
    blockers: list[str] = []
    if isinstance(initiative.get("blocked"), list):
        blockers.extend(str(item or "").strip() for item in initiative.get("blocked") if str(item or "").strip())
    active_task = initiative.get("active_task") if isinstance(initiative.get("active_task"), dict) else {}
    task_status = str(active_task.get("status") or "").strip().lower()
    if task_status in {"blocked", "failed"}:
        blockers.append(f"active task {task_status}")
    health_status = str(health.get("status") or "").strip().lower()
    if health_status and health_status not in {"ok", "healthy"}:
        blockers.append(f"runtime health {health_status}")
    unique: list[str] = []
    for item in blockers:
        if item and item not in unique:
            unique.append(item)
    return unique[:8]


def _build_shell_summary(*, initiative: dict[str, object], health: dict[str, object], blockers: list[str]) -> str:
    active_objective = initiative.get("active_objective") if isinstance(initiative.get("active_objective"), dict) else {}
    active_task = initiative.get("active_task") if isinstance(initiative.get("active_task"), dict) else {}
    objective_title = _shell_objective_label(active_objective)
    task_title = _shell_task_label(active_task, active_objective)
    health_summary = _compact_text(health.get("summary") or health.get("status"), 120)
    parts = []
    if objective_title:
        parts.append(f"Objective: {objective_title}")
    if task_title:
        parts.append(f"Task: {task_title}")
    if blockers:
        parts.append(f"Blockers: {', '.join(blockers[:3])}")
    elif health_summary:
        parts.append(f"Health: {health_summary}")
    return " | ".join(parts) if parts else "Shell ready."


def _shell_objective_label(active_objective: dict[str, object]) -> str:
    initiative_id = str(active_objective.get("initiative_id") or "").strip()
    if initiative_id:
        return f"Initiative {initiative_id}"
    return _compact_text(active_objective.get("title"), 140)


def _shell_task_label(active_task: dict[str, object], active_objective: dict[str, object]) -> str:
    title = str(active_task.get("title") or "").strip()
    lowered = title.lower()
    if lowered.startswith("implement bounded work for:"):
        initiative_id = str(active_objective.get("initiative_id") or "").strip()
        return "Implement bounded work" + (f" for {initiative_id}" if initiative_id else "")
    if lowered.startswith("validate the bounded implementation"):
        return "Validate bounded implementation"
    return _compact_text(title, 140)


async def _shell_local_command_response(*, message: str, session_key: str, db: AsyncSession) -> tuple[str, str] | None:
    lowered = str(message or "").strip().lower()
    if not lowered:
        return None
    state = await _build_shell_state(session_key=session_key, db=db)
    objective = state.get("objective") if isinstance(state.get("objective"), dict) else {}
    task = state.get("task") if isinstance(state.get("task"), dict) else {}
    blockers = state.get("blockers") if isinstance(state.get("blockers"), list) else []
    health = state.get("health") if isinstance(state.get("health"), dict) else {}

    if any(token in lowered for token in ("daily summary", "daily report", "today summary")):
        summary = (
            f"Daily summary: objective {objective.get('title') or 'none'} is {objective.get('status') or 'idle'}; "
            f"task {task.get('title') or 'none'} is {task.get('status') or 'idle'}; "
            f"blockers are {', '.join(blockers) if blockers else 'none'}; "
            f"health is {health.get('status') or 'unknown'}."
        )
        return summary, "shell_daily_summary"

    if any(token in lowered for token in ("blockers", "blocker report", "what is blocked")):
        summary = (
            f"Current blockers: {', '.join(blockers)}."
            if blockers
            else f"Current blockers: none. Health is {health.get('status') or 'unknown'} and the active task is {task.get('status') or 'idle'}."
        )
        return summary, "shell_blocker_summary"

    if any(token in lowered for token in ("shell state", "current state", "status", "current objective", "current task")):
        summary = (
            f"Shell state: objective {objective.get('title') or 'none'} is {objective.get('status') or 'idle'}; "
            f"task {task.get('title') or 'none'} is {task.get('status') or 'idle'} with dispatch {task.get('dispatch_status') or 'none'}; "
            f"health is {health.get('status') or 'unknown'}."
        )
        return summary, "shell_state_summary"

    return None


async def _build_shell_state(*, session_key: str, db: AsyncSession) -> dict[str, object]:
    shell_thread = await _load_shell_thread(session_key=session_key, db=db)
    health = await build_mim_ui_health_snapshot(db=db)
    db_ok, runtime_ready = _shell_health_flags(health)
    initiative = await build_initiative_status(db=db)
    active_objective = initiative.get("active_objective") if isinstance(initiative.get("active_objective"), dict) else {}
    active_task = initiative.get("active_task") if isinstance(initiative.get("active_task"), dict) else {}
    blockers = _build_shell_blockers(initiative=initiative, health=health)
    public_urls = _build_public_shell_urls(settings.remote_shell_domain)
    latest_reply = next(
        (
            str(message.get("content") or "").strip()
            for message in reversed(shell_thread.get("messages") or [])
            if str(message.get("direction") or "").strip() == "outbound"
        ),
        "",
    )
    return {
        "shell_version": "travel-shell-v1",
        "generated_at": _utc_now_iso(),
        "session_key": session_key,
        "title": settings.remote_shell_title,
        **public_urls,
        "travel_mode": _shell_travel_mode_policy(),
        "health": {
            "status": str(health.get("status") or "unknown").strip(),
            "summary": _compact_text(health.get("summary") or "", 220),
            "db_ok": db_ok,
            "runtime_ready": runtime_ready,
        },
        "objective": {
            "objective_id": active_objective.get("objective_id"),
            "title": _shell_objective_label(active_objective),
            "status": str(active_objective.get("status") or "").strip(),
            "initiative_id": str(active_objective.get("initiative_id") or "").strip(),
        },
        "task": {
            "task_id": active_task.get("task_id"),
            "title": _shell_task_label(active_task, active_objective),
            "status": str(active_task.get("status") or "").strip(),
            "dispatch_status": str(active_task.get("dispatch_status") or "").strip(),
            "execution_state": str(active_task.get("execution_state") or "").strip(),
        },
        "initiative": {
            "summary": _compact_text(initiative.get("summary"), 220),
            "execution_state": str(initiative.get("execution_state") or "").strip(),
            "completed_recently": initiative.get("completed_recently") if isinstance(initiative.get("completed_recently"), list) else [],
            "next_task": initiative.get("next_task") if isinstance(initiative.get("next_task"), dict) else {},
            "program_status": initiative.get("program_status") if isinstance(initiative.get("program_status"), dict) else {},
        },
        "blockers": blockers,
        "summary": _build_shell_summary(initiative=initiative, health=health, blockers=blockers),
        "latest_reply_text": latest_reply,
        "thread": shell_thread,
    }


def _extract_gateway_reply_text(payload: dict[str, object]) -> str:
    interface = payload.get("mim_interface") if isinstance(payload.get("mim_interface"), dict) else {}
    reply_text = str(interface.get("reply_text") or "").strip()
    if reply_text:
        return reply_text
    initiative_status = payload.get("initiative_status") if isinstance(payload.get("initiative_status"), dict) else {}
    summary = str(initiative_status.get("summary") or "").strip()
    if summary:
        return summary
    resolution = payload.get("resolution") if isinstance(payload.get("resolution"), dict) else {}
    clarification = str(resolution.get("clarification_prompt") or "").strip()
    if clarification:
        return clarification
    return "No reply text available."


@router.get("/shell", response_class=HTMLResponse)
async def shell_home() -> HTMLResponse:
    if not settings.remote_shell_enabled:
        raise HTTPException(status_code=404, detail="remote_shell_disabled")
    title = html.escape(str(settings.remote_shell_title or "MIM Travel Shell"))
    poll_interval_ms = max(1000, int(settings.remote_shell_poll_interval_ms or 2500))
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
      --bg: #f3efe6;
      --panel: rgba(255,255,255,0.86);
      --ink: #14213d;
      --muted: #5f6b7a;
      --accent: #0f766e;
      --accent-strong: #0b5d57;
      --warning: #b45309;
      --danger: #b42318;
      --line: rgba(20,33,61,0.12);
      --shadow: 0 18px 40px rgba(20,33,61,0.10);
      --font: \"IBM Plex Sans\", \"Avenir Next\", \"Segoe UI\", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: var(--font);
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15,118,110,0.18), transparent 34%),
        radial-gradient(circle at bottom right, rgba(180,83,9,0.14), transparent 28%),
        linear-gradient(180deg, #faf7f2 0%, var(--bg) 100%);
      min-height: 100vh;
    }}
    .shell {{ max-width: 820px; margin: 0 auto; padding: 18px 14px 32px; }}
    .card {{
      background: var(--panel);
      backdrop-filter: blur(12px);
      border: 1px solid var(--line);
      border-radius: 20px;
      box-shadow: var(--shadow);
    }}
    .hero {{ padding: 18px; margin-bottom: 14px; }}
    .eyebrow {{ font-size: 12px; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent); font-weight: 700; }}
    .title {{ margin: 6px 0 4px; font-size: 28px; line-height: 1.05; }}
    .summary {{ color: var(--muted); font-size: 14px; line-height: 1.45; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 14px; }}
    .metric {{ padding: 16px; min-height: 124px; }}
    .metric-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); }}
    .metric-value {{ margin-top: 8px; font-size: 18px; font-weight: 700; line-height: 1.3; }}
    .metric-meta {{ margin-top: 10px; font-size: 13px; line-height: 1.45; color: var(--muted); }}
    .thread {{ padding: 14px; margin-bottom: 14px; }}
    .thread-header {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 12px; }}
    .thread-title {{ font-weight: 700; font-size: 16px; }}
    .status-chip {{ border-radius: 999px; padding: 7px 10px; font-size: 12px; background: rgba(15,118,110,0.10); color: var(--accent-strong); }}
    .messages {{ display: flex; flex-direction: column; gap: 10px; max-height: 52vh; overflow: auto; padding-right: 4px; }}
    .message {{ border-radius: 16px; padding: 12px 13px; border: 1px solid var(--line); background: rgba(255,255,255,0.72); }}
    .message.user {{ background: rgba(20,33,61,0.92); color: white; border-color: rgba(20,33,61,0.92); margin-left: 24px; }}
    .message.system {{ background: rgba(180,83,9,0.10); border-color: rgba(180,83,9,0.20); }}
    .message-meta {{ font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; opacity: 0.72; margin-bottom: 6px; }}
    .composer {{ padding: 14px; }}
    .composer-row {{ display: flex; gap: 10px; align-items: flex-end; }}
    textarea {{
      flex: 1; min-height: 92px; resize: vertical; border-radius: 16px; border: 1px solid var(--line);
      padding: 14px; font: inherit; background: rgba(255,255,255,0.82); color: var(--ink);
    }}
    button {{
      border: 0; border-radius: 16px; background: linear-gradient(135deg, var(--accent), var(--accent-strong));
      color: white; font: inherit; font-weight: 700; padding: 14px 18px; min-width: 112px; cursor: pointer;
    }}
    .footer {{ margin-top: 10px; font-size: 12px; color: var(--muted); display: flex; justify-content: space-between; gap: 10px; }}
    .blockers {{ color: var(--danger); font-weight: 600; }}
    @media (max-width: 720px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .composer-row {{ flex-direction: column; }}
      button {{ width: 100%; }}
      .message.user {{ margin-left: 0; }}
    }}
  </style>
</head>
<body>
  <main class=\"shell\">
    <section class=\"hero card\">
      <div class=\"eyebrow\">Travel Mode</div>
      <h1 class=\"title\">{title}</h1>
      <p class=\"summary\" id=\"summary\">Loading remote shell state…</p>
    </section>
    <section class=\"grid\">
      <article class=\"metric card\">
        <div class=\"metric-label\">Objective</div>
        <div class=\"metric-value\" id=\"objective-title\">Loading…</div>
        <div class=\"metric-meta\" id=\"objective-meta\"></div>
      </article>
      <article class=\"metric card\">
        <div class=\"metric-label\">Task</div>
        <div class=\"metric-value\" id=\"task-title\">Loading…</div>
        <div class=\"metric-meta\" id=\"task-meta\"></div>
      </article>
      <article class=\"metric card\">
        <div class=\"metric-label\">Health</div>
        <div class=\"metric-value\" id=\"health-status\">Loading…</div>
        <div class=\"metric-meta\" id=\"health-meta\"></div>
      </article>
      <article class=\"metric card\">
        <div class=\"metric-label\">Blockers</div>
        <div class=\"metric-value blockers\" id=\"blockers\">Loading…</div>
        <div class=\"metric-meta\" id=\"travel-mode\"></div>
      </article>
    </section>
    <section class=\"thread card\">
      <div class=\"thread-header\">
        <div class=\"thread-title\">Remote Conversation</div>
        <div class=\"status-chip\" id=\"status-chip\">Syncing…</div>
      </div>
      <div class=\"messages\" id=\"messages\"></div>
    </section>
    <section class=\"composer card\">
      <div class=\"composer-row\">
        <textarea id=\"message-input\" placeholder=\"Send a bounded request to MIM…\"></textarea>
        <button id=\"send-btn\" type=\"button\">Send</button>
      </div>
      <div class=\"footer\">
        <span id=\"footer-left\">Remote shell ready.</span>
        <span id=\"footer-right\">Poll: {poll_interval_ms} ms</span>
      </div>
    </section>
  </main>
  <script>
    const stateUrl = '/shell/state';
    const chatUrl = '/shell/chat';
    const pollIntervalMs = {poll_interval_ms};
    const summaryEl = document.getElementById('summary');
    const objectiveTitleEl = document.getElementById('objective-title');
    const objectiveMetaEl = document.getElementById('objective-meta');
    const taskTitleEl = document.getElementById('task-title');
    const taskMetaEl = document.getElementById('task-meta');
    const healthStatusEl = document.getElementById('health-status');
    const healthMetaEl = document.getElementById('health-meta');
    const blockersEl = document.getElementById('blockers');
    const travelModeEl = document.getElementById('travel-mode');
    const messagesEl = document.getElementById('messages');
    const inputEl = document.getElementById('message-input');
    const sendBtn = document.getElementById('send-btn');
    const statusChipEl = document.getElementById('status-chip');
    const footerLeftEl = document.getElementById('footer-left');
    const footerRightEl = document.getElementById('footer-right');

    function safeText(value, fallback = '') {{
      const text = String(value || '').trim();
      return text || fallback;
    }}

    function renderMessages(messages) {{
      messagesEl.innerHTML = '';
      if (!Array.isArray(messages) || !messages.length) {{
        const empty = document.createElement('div');
        empty.className = 'message system';
        empty.innerHTML = '<div class="message-meta">system</div><div>No messages yet.</div>';
        messagesEl.appendChild(empty);
        return;
      }}
      for (const message of messages) {{
        const card = document.createElement('article');
        const role = safeText(message.role, 'mim').toLowerCase();
        card.className = `message ${{role === 'operator' ? 'user' : role === 'system' ? 'system' : ''}}`.trim();
        const meta = document.createElement('div');
        meta.className = 'message-meta';
        meta.textContent = `${{safeText(message.role, 'mim')}} · ${{safeText(message.created_at, 'now')}}`;
        const content = document.createElement('div');
        content.textContent = safeText(message.content, '');
        card.appendChild(meta);
        card.appendChild(content);
        messagesEl.appendChild(card);
      }}
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }}

    async function refreshState() {{
      const res = await fetch(stateUrl, {{ cache: 'no-store' }});
      const data = await res.json();
      summaryEl.textContent = safeText(data.summary, 'Shell ready.');
      objectiveTitleEl.textContent = safeText(data.objective && data.objective.title, 'No active objective');
      objectiveMetaEl.textContent = `Status: ${{safeText(data.objective && data.objective.status, 'idle')}}${{data.objective && data.objective.initiative_id ? ` · Initiative: ${{data.objective.initiative_id}}` : ''}}`;
      taskTitleEl.textContent = safeText(data.task && data.task.title, 'No active task');
      taskMetaEl.textContent = `Status: ${{safeText(data.task && data.task.status, 'idle')}} · Dispatch: ${{safeText(data.task && data.task.dispatch_status, 'none')}} · Exec: ${{safeText(data.task && data.task.execution_state, 'none')}}`;
      healthStatusEl.textContent = safeText(data.health && data.health.status, 'unknown');
      healthMetaEl.textContent = safeText(data.health && data.health.summary, 'No health summary');
      blockersEl.textContent = Array.isArray(data.blockers) && data.blockers.length ? data.blockers.join(', ') : 'None';
      const travel = data.travel_mode || {{}};
      travelModeEl.textContent = `Travel mode: ${{travel.enabled ? 'enabled' : 'disabled'}} · Blocked: ${{Array.isArray(travel.blocked_categories) ? travel.blocked_categories.join(', ') : 'none'}}`;
      statusChipEl.textContent = safeText(data.initiative && data.initiative.execution_state, safeText(data.objective && data.objective.status, 'idle'));
    footerLeftEl.textContent = safeText(data.latest_reply_text, 'Remote shell ready.');
    footerRightEl.textContent = safeText(data.public_shell_url, '') ? `Public: ${{safeText(data.public_shell_url)}} · Poll: ${{pollIntervalMs}} ms` : `Poll: ${{pollIntervalMs}} ms`;
      const thread = data.thread || {{}};
      renderMessages(Array.isArray(thread.messages) ? thread.messages : []);
    }}

    async function sendMessage() {{
      const message = safeText(inputEl.value);
      if (!message) return;
      sendBtn.disabled = true;
      try {{
        const res = await fetch(chatUrl, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ message }}),
        }});
        const payload = await res.json();
        inputEl.value = '';
        footerLeftEl.textContent = safeText(payload.reply && payload.reply.content, safeText(payload.status, 'sent'));
        await refreshState();
      }} finally {{
        sendBtn.disabled = false;
      }}
    }}

    sendBtn.addEventListener('click', sendMessage);
    inputEl.addEventListener('keydown', (event) => {{
      if (event.key === 'Enter' && !event.shiftKey) {{
        event.preventDefault();
        sendMessage();
      }}
    }});

    refreshState();
    setInterval(refreshState, pollIntervalMs);
  </script>
</body>
</html>
        """
    )


@router.get("/shell/state")
async def shell_state(
    session_key: str = Query(default=DEFAULT_SHELL_SESSION_KEY),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    if not settings.remote_shell_enabled:
        raise HTTPException(status_code=404, detail="remote_shell_disabled")
    return await _build_shell_state(session_key=_normalize_shell_session_key(session_key), db=db)


@router.get("/shell/health")
async def shell_health(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    if not settings.remote_shell_enabled:
        raise HTTPException(status_code=404, detail="remote_shell_disabled")
    health = await build_mim_ui_health_snapshot(db=db)
    return {
        "status": str(health.get("status") or "unknown").strip(),
        "summary": _compact_text(health.get("summary") or "", 220),
        "travel_mode": _shell_travel_mode_policy(),
        "generated_at": _utc_now_iso(),
    }


@router.get("/shell/reports/daily")
async def shell_daily_report(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    state = await _build_shell_state(session_key=DEFAULT_SHELL_SESSION_KEY, db=db)
    objective = state.get("objective") if isinstance(state.get("objective"), dict) else {}
    task = state.get("task") if isinstance(state.get("task"), dict) else {}
    blockers = state.get("blockers") if isinstance(state.get("blockers"), list) else []
    summary = (
        f"Daily summary: objective={objective.get('title') or 'none'}; "
        f"objective_status={objective.get('status') or 'idle'}; "
        f"task={task.get('title') or 'none'}; "
        f"task_status={task.get('status') or 'idle'}; "
        f"blockers={', '.join(blockers) if blockers else 'none'}."
    )
    return {
        "generated_at": _utc_now_iso(),
        "summary": summary,
        "state": state,
    }


@router.get("/shell/reports/blockers")
async def shell_blocker_report(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    state = await _build_shell_state(session_key=DEFAULT_SHELL_SESSION_KEY, db=db)
    blockers = state.get("blockers") if isinstance(state.get("blockers"), list) else []
    return {
        "generated_at": _utc_now_iso(),
        "active": bool(blockers),
        "blockers": blockers,
        "summary": "No active blockers." if not blockers else f"Active blockers: {', '.join(blockers)}",
    }


@router.post("/shell/chat")
async def shell_chat(
    payload: ShellChatRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    if not settings.remote_shell_enabled:
        raise HTTPException(status_code=404, detail="remote_shell_disabled")

    session_key = _normalize_shell_session_key(payload.session_key)
    session = await _ensure_shell_session(session_key=session_key, db=db)
    _, inbound = await append_interface_message(
        session_key=session_key,
        actor="operator",
        source="shell",
        direction="inbound",
        role="operator",
        content=str(payload.message).strip(),
        parsed_intent="shell_text",
        confidence=1.0,
        requires_approval=False,
        metadata_json={"message_type": "user", "travel_shell": True},
        db=db,
    )

    block_reason = _travel_mode_block_reason(payload.message)
    if block_reason:
        _, reply = await append_interface_message(
            session_key=session_key,
            actor="mim",
            source="shell",
            direction="outbound",
            role="system",
            content=block_reason,
            parsed_intent="travel_mode_block",
            confidence=1.0,
            requires_approval=False,
            metadata_json={"message_type": "system_summary", "travel_shell": True},
            db=db,
        )
        await db.commit()
        return {
            "status": "blocked",
            "accepted": False,
            "session": to_interface_session_out(session),
            "message": _serialize_shell_message(inbound),
            "reply": _serialize_shell_message(reply),
            "travel_mode": _shell_travel_mode_policy(),
        }

    local_command = await _shell_local_command_response(
        message=payload.message,
        session_key=session_key,
        db=db,
    )
    if local_command is not None:
        reply_text, parsed_intent = local_command
        session = await upsert_interface_session(
            session_key=session_key,
            actor="operator",
            source="shell",
            channel="chat",
            status="active",
            context_json={"travel_shell": True},
            metadata_json={"conversation_session_id": session_key, "travel_shell": True},
            db=db,
        )
        _, reply = await append_interface_message(
            session_key=session_key,
            actor="mim",
            source="shell",
            direction="outbound",
            role="mim",
            content=reply_text,
            parsed_intent=parsed_intent,
            confidence=1.0,
            requires_approval=False,
            metadata_json={"message_type": "mim_reply", "travel_shell": True},
            db=db,
        )
        await db.commit()
        return {
            "status": "accepted",
            "accepted": True,
            "request_id": "",
            "session": to_interface_session_out(session),
            "message": _serialize_shell_message(inbound),
            "reply": _serialize_shell_message(reply),
            "gateway": {},
            "travel_mode": _shell_travel_mode_policy(),
        }

    from core.routers import gateway as gateway_router

    gateway_response = await gateway_router.intake_text(
        TextInputAdapterRequest(
            text=str(payload.message).strip(),
            parsed_intent="discussion",
            confidence=0.95,
            target_system="mim",
            metadata_json={
                "conversation_session_id": session_key,
                "adapter": "shell",
                "travel_shell": True,
            },
        ),
        db=db,
    )
    reply_text = _extract_gateway_reply_text(gateway_response if isinstance(gateway_response, dict) else {})
    session = await upsert_interface_session(
        session_key=session_key,
        actor="operator",
        source="shell",
        channel="chat",
        status="active",
        context_json={"travel_shell": True},
        metadata_json={"conversation_session_id": session_key, "travel_shell": True},
        db=db,
    )
    _, reply = await append_interface_message(
        session_key=session_key,
        actor="mim",
        source="shell",
        direction="outbound",
        role="mim",
        content=reply_text,
        parsed_intent="shell_reply",
        confidence=1.0,
        requires_approval=False,
        metadata_json={
            "message_type": "mim_reply",
            "travel_shell": True,
            "request_id": str((gateway_response or {}).get("request_id") or "").strip(),
        },
        db=db,
    )
    await db.commit()
    return {
        "status": "accepted",
        "accepted": True,
        "request_id": str((gateway_response or {}).get("request_id") or "").strip(),
        "session": to_interface_session_out(session),
        "message": _serialize_shell_message(inbound),
        "reply": _serialize_shell_message(reply),
        "gateway": gateway_response,
        "travel_mode": _shell_travel_mode_policy(),
    }