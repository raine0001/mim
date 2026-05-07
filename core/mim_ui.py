import asyncio
import base64
from email.parser import BytesParser
from email.policy import default as email_policy_default
import json
import mimetypes
import os
from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
from pathlib import Path
import re
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.autonomy_driver_service import build_initiative_status
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
from core.interface_service import (
  append_interface_message,
  list_interface_messages,
  to_interface_message_out,
  to_interface_session_out,
  upsert_interface_session,
)
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
from core.config import settings
from core.mim_ui_auth import (
  clear_authenticated_mimtod_cookie,
  credentials_match,
  ensure_authenticated_mimtod_api_request,
  maybe_require_mimtod_page_login,
  mimtod_auth_required,
  normalize_next_path,
  request_has_valid_mimtod_auth,
  set_authenticated_mimtod_cookie,
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
  commitment_snapshot,
)
from core.policy_conflict_resolution_service import list_workspace_policy_conflict_profiles
from core.proposal_policy_convergence_service import list_workspace_proposal_policy_preferences
from core.self_evolution_service import build_self_evolution_briefing
from core.runtime_recovery_service import RuntimeRecoveryService
from core.primitive_request_recovery_service import load_authoritative_request_status
from core.ui_health_service import (
  build_mim_ui_health_snapshot,
  build_mim_ui_health_snapshot_from_rows,
  summarize_runtime_health,
)

router = APIRouter(tags=["mim-ui"])
SHARED_RUNTIME_ROOT = Path("runtime/shared")
runtime_recovery_service = RuntimeRecoveryService(SHARED_RUNTIME_ROOT)
MIM_PRIMARY_THREAD_KEY = "primary_operator"
MIM_UI_MEDIA_ROOT = SHARED_RUNTIME_ROOT / "mim_ui_media"
MIM_UI_ALLOWED_IMAGE_TYPES = {
  "image/png": ".png",
  "image/jpeg": ".jpg",
  "image/webp": ".webp",
}
MIM_UI_MAX_IMAGE_BYTES = 8 * 1024 * 1024

MIC_PROMPT_MIN_CONFIDENCE = 0.66
MIC_PROMPT_MAX_AGE_SECONDS = 25.0


class RuntimeRecoveryEventRequest(BaseModel):
    lane: str
    event_type: str
    detail: str | None = None
    next_retry_at: str | None = None
    metadata: dict | None = None


class FrontendMediaStatusRequest(BaseModel):
  lane: str
  status: str
  detail: str | None = None
  secure_context: bool | None = None
  media_devices_available: bool | None = None
  permission_state: str | None = None
  selected_device_id: str | None = None
  selected_device_label: str | None = None


MIM_UI_FRONTEND_MEDIA_TTL_SECONDS = 900.0
_mim_ui_frontend_media_state: dict[str, dict[str, object]] = {}


def _record_frontend_media_status(request: FrontendMediaStatusRequest) -> dict[str, object]:
  now = datetime.now(timezone.utc)
  lane = str(request.lane or "").strip().lower()
  payload = {
    "lane": lane,
    "status": str(request.status or "unknown").strip().lower() or "unknown",
    "detail": str(request.detail or "").strip(),
    "secure_context": bool(request.secure_context) if request.secure_context is not None else None,
    "media_devices_available": bool(request.media_devices_available) if request.media_devices_available is not None else None,
    "permission_state": str(request.permission_state or "").strip().lower() or None,
    "selected_device_id": str(request.selected_device_id or "").strip() or None,
    "selected_device_label": str(request.selected_device_label or "").strip() or None,
    "last_reported_at": now.isoformat(),
    "last_reported_ts": now.timestamp(),
  }
  _mim_ui_frontend_media_state[lane] = payload
  return payload


def _frontend_media_snapshot(now: datetime | None = None) -> dict[str, dict[str, object]]:
  now = now or datetime.now(timezone.utc)
  snapshot: dict[str, dict[str, object]] = {}
  for lane, payload in _mim_ui_frontend_media_state.items():
    try:
      last_reported_ts = float(payload.get("last_reported_ts") or 0.0)
    except (TypeError, ValueError):
      last_reported_ts = 0.0
    if last_reported_ts <= 0:
      continue
    age_seconds = max(0.0, now.timestamp() - last_reported_ts)
    if age_seconds > MIM_UI_FRONTEND_MEDIA_TTL_SECONDS:
      continue
    item = dict(payload)
    item.pop("last_reported_ts", None)
    item["age_seconds"] = age_seconds
    snapshot[lane] = item
  return snapshot


def _frontend_media_issue_summary(frontend_media: dict[str, dict[str, object]]) -> str:
  issue_labels: list[str] = []
  status_labels = {
    "api_unavailable": "API unavailable",
    "permission_denied": "permission denied",
    "insecure_context": "insecure context",
    "no_device": "no device",
    "device_busy": "device busy",
    "start_failed": "start failed",
    "recovering": "recovering",
  }
  for lane in ("camera", "microphone"):
    entry = frontend_media.get(lane)
    if not entry:
      continue
    status = str(entry.get("status") or "").strip().lower()
    if status in {"", "ready", "active", "watching", "listening", "ok"}:
      continue
    lane_label = "camera" if lane == "camera" else "microphone"
    issue_label = status_labels.get(status, status.replace("_", " "))
    issue_labels.append(f"{lane_label} {issue_label}")
  return "; ".join(issue_labels)


def _normalize_public_mim_base(value: object) -> str:
  text = str(value or "").strip()
  if not text:
    return ""
  candidate = text if "://" in text else f"https://{text}"
  try:
    parsed = urlparse(candidate)
  except Exception:
    return ""
  if not parsed.scheme or not parsed.netloc:
    return ""
  path = (parsed.path or "").rstrip("/")
  return f"{parsed.scheme}://{parsed.netloc}{path}"


def _build_public_mim_url(value: object) -> str:
  base = _normalize_public_mim_base(value)
  if not base:
    return ""
  if base.endswith("/mim"):
    return base
  return f"{base}/mim"


def _request_header_host(request: Request) -> str:
  forwarded_host = str(request.headers.get("x-forwarded-host") or "").strip()
  if forwarded_host:
    return forwarded_host.split(",", 1)[0].strip()
  return str(request.headers.get("host") or request.url.netloc or "").strip()


def _request_host_name(request: Request) -> str:
  host = _request_header_host(request)
  if not host:
    return str(request.url.hostname or "").strip().lower()
  if host.startswith("[") and "]" in host:
    return host[1 : host.index("]")].strip().lower()
  if ":" in host:
    return host.rsplit(":", 1)[0].strip().lower()
  return host.strip().lower()


def _is_loopback_host(host: str) -> bool:
  normalized = str(host or "").strip().lower()
  if normalized in {"", "localhost", "127.0.0.1", "::1"}:
    return True
  try:
    return ipaddress.ip_address(normalized).is_loopback
  except ValueError:
    return False


def _effective_request_scheme(request: Request) -> str:
  forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").strip()
  if forwarded_proto:
    return forwarded_proto.split(",", 1)[0].strip().lower()
  cf_visitor = str(request.headers.get("cf-visitor") or "").strip()
  if cf_visitor:
    try:
      visitor_payload = json.loads(cf_visitor)
    except json.JSONDecodeError:
      visitor_payload = None
    if isinstance(visitor_payload, dict):
      scheme = str(visitor_payload.get("scheme") or "").strip().lower()
      if scheme:
        return scheme
  return str(request.url.scheme or "http").strip().lower() or "http"


def _public_mim_redirect_target(request: Request) -> str:
  public_mim_url = _build_public_mim_url(settings.remote_shell_domain)
  if not public_mim_url:
    return ""
  if _effective_request_scheme(request) == "https":
    return ""
  request_host = _request_host_name(request)
  public_host = str(urlparse(public_mim_url).hostname or "").strip().lower()
  if public_host and request_host == public_host:
    return ""
  if _is_loopback_host(request_host):
    return ""
  query = str(request.url.query or "").strip()
  if not query:
    return public_mim_url
  return f"{public_mim_url}?{query}"


def _dedicated_public_mim_redirect_target(request: Request) -> str:
  public_base = _normalize_public_mim_base(settings.remote_shell_domain)
  if not public_base:
    return ""
  public_host = str(urlparse(public_base).hostname or "").strip().lower()
  request_host = _request_host_name(request)
  if not public_host or request_host == public_host or _is_loopback_host(request_host):
    return ""
  path = str(request.url.path or "").strip() or "/mim"
  query = str(request.url.query or "").strip()
  target = f"{public_base}{path}"
  if not query:
    return target
  return f"{target}?{query}"


def _mim_ui_login_page(*, next_path: str, error_message: str = "") -> str:
  error_block = (
    f'<p class="error">{error_message}</p>'
    if str(error_message or "").strip()
    else ""
  )
  return f"""
<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>MIM Operator Console | Secure Sign In</title>
  <style>
    :root {{
      --bg: #06131e;
      --bg-strong: #0c2233;
      --panel: rgba(10, 25, 38, 0.94);
      --line: rgba(77, 196, 211, 0.30);
      --line-strong: rgba(77, 196, 211, 0.52);
      --text: #e8f1f7;
      --muted: #97afbf;
      --warn: #ff9561;
      --mim: #4dc4d3;
      --tod: #ff9b54;
      --display: \"Iowan Old Style\", \"Palatino Linotype\", \"Book Antiqua\", serif;
      --body: \"IBM Plex Sans\", \"Segoe UI\", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: var(--body);
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(77, 196, 211, 0.16), transparent 28%),
        radial-gradient(circle at top right, rgba(255, 155, 84, 0.10), transparent 24%),
        linear-gradient(180deg, #030910 0%, var(--bg) 100%);
      padding: 24px;
    }}
    .panel {{
      width: min(520px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 26px;
      box-shadow: 0 28px 80px rgba(0, 0, 0, 0.44);
      padding: 28px;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 12px;
      color: var(--mim);
      background: rgba(77, 196, 211, 0.08);
      font-size: 0.78rem;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      font-weight: 700;
    }}
    h1 {{
      margin: 18px 0 10px;
      font-family: var(--display);
      letter-spacing: 0.03em;
      font-size: 2rem;
      line-height: 1.1;
    }}
    p {{ margin: 0 0 16px; color: var(--muted); line-height: 1.6; }}
    .lead {{ font-size: 1rem; max-width: 42ch; }}
    .facts {{
      display: grid;
      gap: 10px;
      margin: 18px 0 20px;
      padding: 16px;
      border: 1px solid rgba(151, 175, 191, 0.18);
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.03);
    }}
    .fact {{ margin: 0; font-size: 0.92rem; }}
    .fact strong {{ color: var(--text); }}
    .site-chip {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 6px;
      color: var(--tod);
      font-size: 0.86rem;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .signin-card {{
      border: 1px solid var(--line-strong);
      border-radius: 22px;
      padding: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.03), rgba(255,255,255,0.01));
    }}
    label {{ display: block; margin: 0 0 8px; font-size: 0.88rem; color: var(--muted); }}
    input {{
      width: 100%;
      border-radius: 14px;
      border: 1px solid rgba(77, 196, 211, 0.28);
      background: rgba(5, 16, 26, 0.78);
      color: var(--text);
      padding: 12px 14px;
      margin: 0 0 14px;
      font-size: 1rem;
    }}
    input:focus {{
      outline: 2px solid rgba(77, 196, 211, 0.32);
      outline-offset: 1px;
      border-color: rgba(77, 196, 211, 0.54);
    }}
    button {{
      width: 100%;
      border: 0;
      border-radius: 14px;
      padding: 12px 14px;
      background: linear-gradient(135deg, var(--mim), #73f0bf);
      color: #03121d;
      font-weight: 700;
      cursor: pointer;
    }}
    .error {{ margin: 0 0 14px; color: var(--warn); }}
    .footnote {{ margin-top: 16px; font-size: 0.84rem; }}
    .back-link {{ color: var(--mim); font-weight: 700; text-decoration: none; }}
    .back-link:hover, .back-link:focus-visible {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <main class=\"panel\">
    <div class=\"eyebrow\">Restricted Operator Access</div>
    <h1>MIM Operator Console</h1>
    <p class=\"lead\">This sign-in page is for the administrative MIM console at mim.mimtod.com. It grants access to the internal operator interface only.</p>
    <div class=\"facts\">
      <p class=\"fact\"><strong>Site:</strong> MIM + TOD administrative console</p>
      <p class=\"fact\"><strong>Purpose:</strong> operator monitoring, diagnostics, and controlled runtime actions</p>
      <p class=\"fact\"><strong>Data requested:</strong> console username and password only. This page does not request payment, banking, or card information.</p>
    </div>
    <div class=\"signin-card\">
      <div class=\"site-chip\">mim.mimtod.com</div>
      <p>Use your MIM operator credentials to continue to the protected console.</p>
      {error_block}
      <form method=\"post\" action=\"/mim/login\">
        <input type=\"hidden\" name=\"next\" value=\"{next_path}\" />
        <label for=\"username\">Operator username</label>
        <input id=\"username\" name=\"username\" type=\"text\" autocomplete=\"username\" spellcheck=\"false\" required />
        <label for=\"password\">Operator password</label>
        <input id=\"password\" name=\"password\" type=\"password\" autocomplete=\"current-password\" required />
        <button type=\"submit\">Open Operator Console</button>
      </form>
    </div>
    <p class=\"footnote\">If you expected the public chat page instead, return to <a class=\"back-link\" href=\"/\">the main MIM + TOD workspace</a>.</p>
  </main>
</body>
</html>
"""


@router.get("/mim/login", response_class=HTMLResponse)
async def mim_ui_login_get(request: Request):
  dedicated_redirect = _dedicated_public_mim_redirect_target(request)
  if dedicated_redirect:
    return RedirectResponse(url=dedicated_redirect, status_code=303)
  next_path = normalize_next_path(request.query_params.get("next"), default="/mim")
  if not mimtod_auth_required(request):
    return RedirectResponse(url=next_path, status_code=303)
  if request_has_valid_mimtod_auth(request):
    return RedirectResponse(url=next_path, status_code=303)
  return HTMLResponse(_mim_ui_login_page(next_path=next_path))


@router.post("/mim/login")
async def mim_ui_login_post(request: Request):
  dedicated_redirect = _dedicated_public_mim_redirect_target(request)
  if dedicated_redirect:
    return RedirectResponse(url=dedicated_redirect, status_code=303)
  raw_body = (await request.body()).decode("utf-8", errors="replace")
  parsed_form = parse_qs(raw_body, keep_blank_values=True)
  next_path = normalize_next_path((parsed_form.get("next") or ["/mim"])[0], default="/mim")
  username = str((parsed_form.get("username") or [""])[0]).strip()
  password = str((parsed_form.get("password") or [""])[0])
  if not credentials_match(username=username, password=password):
    return HTMLResponse(
      _mim_ui_login_page(next_path=next_path, error_message="Invalid username or password."),
      status_code=401,
    )
  response = RedirectResponse(url=next_path, status_code=303)
  set_authenticated_mimtod_cookie(response, request, username=username)
  return response


@router.get("/mim/logout")
async def mim_ui_logout(request: Request):
  dedicated_redirect = _dedicated_public_mim_redirect_target(request)
  if dedicated_redirect:
    return RedirectResponse(url=dedicated_redirect, status_code=303)
  response = RedirectResponse(url="/mim/login", status_code=303)
  clear_authenticated_mimtod_cookie(response, request)
  return response


def _mim_ui_primary_thread_key() -> str:
  return MIM_PRIMARY_THREAD_KEY


def _ensure_mim_ui_media_root() -> Path:
  MIM_UI_MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
  return MIM_UI_MEDIA_ROOT


def _mim_ui_image_extension(content_type: str, filename: str) -> str:
  normalized_type = str(content_type or "").strip().lower()
  if normalized_type in MIM_UI_ALLOWED_IMAGE_TYPES:
    return MIM_UI_ALLOWED_IMAGE_TYPES[normalized_type]
  guessed = mimetypes.guess_type(str(filename or ""))[0] or ""
  guessed = guessed.strip().lower()
  if guessed in MIM_UI_ALLOWED_IMAGE_TYPES:
    return MIM_UI_ALLOWED_IMAGE_TYPES[guessed]
  suffix = Path(str(filename or "upload")).suffix.lower()
  if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
    return ".jpg" if suffix == ".jpeg" else suffix
  raise HTTPException(status_code=400, detail="unsupported_image_type")


def _mim_ui_media_url(asset_name: str) -> str:
  return f"/mim/ui/media/{asset_name}"


def _parse_mim_ui_multipart_form(
  *,
  content_type: str,
  body: bytes,
) -> tuple[dict[str, str], dict[str, object]]:
  normalized_content_type = str(content_type or "").strip()
  if not normalized_content_type.lower().startswith("multipart/form-data"):
    raise HTTPException(status_code=415, detail="unsupported_media_type")
  if "boundary=" not in normalized_content_type.lower():
    raise HTTPException(status_code=400, detail="multipart_boundary_missing")
  if not body:
    raise HTTPException(status_code=400, detail="empty_multipart_body")

  envelope = (
    f"Content-Type: {normalized_content_type}\r\n"
    "MIME-Version: 1.0\r\n\r\n"
  ).encode("utf-8") + body
  message = BytesParser(policy=email_policy_default).parsebytes(envelope)
  if not message.is_multipart():
    raise HTTPException(status_code=400, detail="invalid_multipart_body")

  fields: dict[str, str] = {}
  upload: dict[str, object] | None = None
  for part in message.iter_parts():
    part_name = str(
      part.get_param("name", header="content-disposition") or ""
    ).strip()
    if not part_name:
      continue
    payload = part.get_payload(decode=True) or b""
    filename = part.get_filename()
    if filename is None:
      charset = part.get_content_charset() or "utf-8"
      fields[part_name] = payload.decode(charset, errors="replace")
      continue
    upload = {
      "field_name": part_name,
      "filename": str(filename or "upload").strip() or "upload",
      "content_type": str(part.get_content_type() or "").strip().lower(),
      "content": payload,
    }

  if not isinstance(upload, dict):
    raise HTTPException(status_code=400, detail="image_file_missing")
  if str(upload.get("field_name") or "").strip() != "file":
    raise HTTPException(status_code=400, detail="unexpected_file_field")
  return fields, upload


def _interface_message_attachment(metadata_json: object) -> dict[str, object] | None:
  metadata = metadata_json if isinstance(metadata_json, dict) else {}
  attachment = metadata.get("attachment")
  if not isinstance(attachment, dict):
    return None
  return {
    "kind": str(attachment.get("kind") or "").strip(),
    "url": str(attachment.get("url") or "").strip(),
    "thumbnail_url": str(attachment.get("thumbnail_url") or attachment.get("url") or "").strip(),
    "mime_type": str(attachment.get("mime_type") or "").strip(),
    "filename": str(attachment.get("filename") or "").strip(),
    "size_bytes": int(attachment.get("size_bytes") or 0),
    "width": int(attachment.get("width") or 0),
    "height": int(attachment.get("height") or 0),
    "sha256": str(attachment.get("sha256") or "").strip(),
  }


MIM_UI_MESSAGE_TYPES = {
  "user",
  "mim_reply",
  "system_execution",
  "system_summary",
}


def _message_role_class(message: dict[str, object]) -> str:
  role = str(message.get("role") or message.get("direction") or "mim").strip().lower()
  if role in {"operator", "user", "inbound"}:
    return "user"
  if role == "system":
    return "system"
  return "mim"


def _compact_multiline_text(raw: str, *, max_len: int = 180) -> str:
  lines = [str(line).strip() for line in str(raw or "").splitlines() if str(line).strip()]
  return _compact_sentence(" ".join(lines), max_len=max_len)


def _parse_execution_steps(text: str) -> list[dict[str, object]]:
  steps: list[dict[str, object]] = []
  current: dict[str, object] | None = None

  def ensure_current() -> dict[str, object]:
    nonlocal current
    if current is None:
      current = {
        "iteration": "",
        "task": "",
        "result": "",
        "delta": "",
        "notes": [],
      }
      steps.append(current)
    return current

  for raw_line in str(text or "").splitlines():
    line = str(raw_line or "").strip()
    if not line:
      continue
    iteration_match = re.match(r"^Iteration\s+([^:]+):\s*(.*)$", line, re.IGNORECASE)
    if iteration_match:
      current = {
        "iteration": f"Iteration {iteration_match.group(1).strip()}",
        "task": "",
        "result": "",
        "delta": "",
        "notes": [],
      }
      trailing = str(iteration_match.group(2) or "").strip()
      if trailing:
        current["notes"].append(trailing)
      steps.append(current)
      continue

    task_match = re.match(r"^Task:\s*(.*)$", line, re.IGNORECASE)
    if task_match:
      ensure_current()["task"] = str(task_match.group(1) or "").strip()
      continue

    result_match = re.match(r"^Result:\s*(.*)$", line, re.IGNORECASE)
    if result_match:
      ensure_current()["result"] = str(result_match.group(1) or "").strip()
      continue

    delta_match = re.match(r"^Delta:\s*(.*)$", line, re.IGNORECASE)
    if delta_match:
      ensure_current()["delta"] = str(delta_match.group(1) or "").strip()
      continue

    ensure_current()["notes"].append(line)

  normalized_steps: list[dict[str, object]] = []
  for step in steps:
    notes = [
      str(item).strip()
      for item in (step.get("notes") or [])
      if str(item).strip()
    ]
    normalized_steps.append(
      {
        "iteration": str(step.get("iteration") or "").strip(),
        "task": str(step.get("task") or "").strip(),
        "result": str(step.get("result") or "").strip(),
        "delta": str(step.get("delta") or "").strip(),
        "notes": notes,
      }
    )
  return [
    step
    for step in normalized_steps
    if any(
      [
        step["iteration"],
        step["task"],
        step["result"],
        step["delta"],
        step["notes"],
      ]
    )
  ]


def _infer_mim_ui_message_type(
  *,
  message: dict[str, object],
  metadata: dict[str, object],
  content: str,
) -> str:
  explicit = str(metadata.get("message_type") or message.get("message_type") or "").strip().lower()
  if explicit in MIM_UI_MESSAGE_TYPES:
    return explicit

  role_class = _message_role_class(message)
  if role_class == "user":
    return "user"
  if role_class == "system":
    return "system_summary"

  has_execution_id = int(metadata.get("execution_id") or 0) > 0
  has_structured_markers = bool(
    re.search(r"(^|\n)Iteration\s+[^:]+:", content, re.IGNORECASE)
    or re.search(r"(^|\n)Task:\s*", content, re.IGNORECASE)
    or re.search(r"(^|\n)Result:\s*", content, re.IGNORECASE)
    or re.search(r"(^|\n)Delta:\s*", content, re.IGNORECASE)
  )
  line_count = len([line for line in content.splitlines() if str(line).strip()])
  if has_structured_markers and (has_execution_id or line_count >= 4):
    return "system_execution"
  if has_execution_id and (line_count >= 6 or len(content) >= 320):
    return "system_execution"
  return "mim_reply"


def _summarize_execution_message(
  *,
  content: str,
  metadata: dict[str, object],
  steps: list[dict[str, object]],
) -> str:
  summary_override = str(metadata.get("summary_text") or metadata.get("execution_summary") or "").strip()
  if summary_override:
    return _compact_sentence(summary_override, max_len=180)

  if steps:
    first_result = next(
      (str(step.get("result") or "").strip() for step in steps if str(step.get("result") or "").strip()),
      "",
    )
    first_task = next(
      (str(step.get("task") or "").strip() for step in steps if str(step.get("task") or "").strip()),
      "",
    )
    detail = first_result or first_task
    summary = f"Execution trace with {len(steps)} step{'s' if len(steps) != 1 else ''}"
    if detail:
      summary = f"{summary}. {detail}"
    return _compact_sentence(summary, max_len=180)

  result_match = re.search(r"(?:^|\n)Result:\s*(.+)", content, re.IGNORECASE)
  if result_match:
    return _compact_sentence(str(result_match.group(1) or "").strip(), max_len=180)
  return _compact_multiline_text(content, max_len=180)


def _serialize_execution_payload(
  *,
  content: str,
  metadata: dict[str, object],
) -> dict[str, object]:
  steps = _parse_execution_steps(content)
  lines = [line for line in str(content or "").splitlines() if str(line).strip()]
  preview = "\n".join(lines[:24]).strip()
  summary = _summarize_execution_message(content=content, metadata=metadata, steps=steps)
  return {
    "summary_text": summary,
    "inline_text": summary,
    "execution_text": str(content or "").strip(),
    "execution_preview": preview,
    "execution_truncated": len(lines) > 24,
    "structured_output": {
      "steps": steps,
      "step_count": len(steps),
      "line_count": len(lines),
      "has_structure": bool(steps),
    },
  }


def _serialize_chat_message(message: dict[str, object]) -> dict[str, object]:
  metadata = message.get("metadata_json") if isinstance(message.get("metadata_json"), dict) else {}
  content = str(message.get("content") or "").strip()
  normalized_type = _infer_mim_ui_message_type(
    message=message,
    metadata=metadata,
    content=content,
  )
  execution_payload = (
    _serialize_execution_payload(content=content, metadata=metadata)
    if normalized_type == "system_execution"
    else {
      "summary_text": "",
      "inline_text": content,
      "execution_text": "",
      "execution_preview": "",
      "execution_truncated": False,
      "structured_output": {
        "steps": [],
        "step_count": 0,
        "line_count": 0,
        "has_structure": False,
      },
    }
  )
  summary_text = (
    execution_payload["summary_text"]
    if normalized_type == "system_execution"
    else _compact_multiline_text(content, max_len=180)
  )
  return {
    **message,
    "message_type": normalized_type,
    "interaction_mode": str(metadata.get("interaction_mode") or "text").strip() or "text",
    "attachment": _interface_message_attachment(metadata),
    "summary_text": summary_text,
    "inline_text": execution_payload["inline_text"] if normalized_type == "system_execution" else content,
    "execution_text": execution_payload["execution_text"],
    "execution_preview": execution_payload["execution_preview"],
    "execution_truncated": bool(execution_payload["execution_truncated"]),
    "structured_output": execution_payload["structured_output"],
    "execution_id": int(metadata.get("execution_id") or 0),
  }


async def _load_mim_ui_chat_thread(*, db: AsyncSession) -> dict[str, object]:
  session_key = _mim_ui_primary_thread_key()
  try:
    session, rows = await list_interface_messages(
      session_key=session_key,
      limit=200,
      db=db,
    )
    session_out = to_interface_session_out(session)
    messages = [
      _serialize_chat_message(to_interface_message_out(row))
      for row in reversed(rows)
    ]
  except ValueError:
    session_out = {
      "session_key": session_key,
      "channel": "chat",
      "status": "active",
      "context_json": {},
      "metadata_json": {},
    }
    messages = []
  return {
    "session": session_out,
    "messages": messages,
    "primary_thread": session_key,
  }


def _mim_ui_openai_ready() -> tuple[bool, str]:
  forced_disable = str(os.getenv("MIM_DISABLE_OPENAI", "")).strip().lower() in {"1", "true", "yes", "on"}
  api_key = str(settings.openai_api_key or os.getenv("MIM_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
  allowed = bool(
    settings.allow_openai
    or str(os.getenv("MIM_ALLOW_OPENAI", "")).strip().lower() in {"1", "true", "yes", "on"}
  )
  if forced_disable:
    return False, "openai_disabled"
  if not allowed:
    return False, "openai_not_allowed"
  if not api_key:
    return False, "openai_api_key_missing"
  return True, api_key


def _extract_openai_message_text(content: object) -> str:
  if isinstance(content, str):
    return content.strip()
  if isinstance(content, list):
    parts: list[str] = []
    for item in content:
      if isinstance(item, dict):
        text = str(item.get("text") or "").strip()
        if text:
          parts.append(text)
    return "\n".join(parts).strip()
  return ""


def _analyze_image_with_openai_sync(*, image_bytes: bytes, mime_type: str, prompt: str) -> str:
  ready, detail = _mim_ui_openai_ready()
  if not ready:
    raise RuntimeError(detail)

  model = str(os.getenv("MIM_IMAGE_OPENAI_MODEL") or "gpt-4.1-mini").strip() or "gpt-4.1-mini"
  api_url = str(os.getenv("MIM_OPENAI_CHAT_URL") or "https://api.openai.com/v1/chat/completions").strip()
  image_b64 = base64.b64encode(image_bytes).decode("utf-8")
  payload = {
    "model": model,
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": prompt},
          {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{image_b64}"},
          },
        ],
      }
    ],
    "max_tokens": 500,
  }
  req = urllib_request.Request(
    api_url,
    data=json.dumps(payload).encode("utf-8"),
    headers={
      "Content-Type": "application/json",
      "Authorization": f"Bearer {detail}",
    },
    method="POST",
  )
  try:
    with urllib_request.urlopen(req, timeout=45) as response:
      raw = response.read().decode("utf-8")
  except urllib_error.HTTPError as exc:
    detail_text = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
    raise RuntimeError(f"openai_http_error:{exc.code}:{detail_text}") from exc
  except urllib_error.URLError as exc:
    raise RuntimeError(f"openai_transport_error:{exc}") from exc

  payload_out = json.loads(raw)
  choices = payload_out.get("choices") if isinstance(payload_out, dict) else []
  first_choice = choices[0] if isinstance(choices, list) and choices else {}
  message = first_choice.get("message") if isinstance(first_choice, dict) else {}
  content = message.get("content") if isinstance(message, dict) else ""
  text = _extract_openai_message_text(content)
  if not text:
    raise RuntimeError("openai_empty_response")
  return text


async def _analyze_image_with_openai(*, image_bytes: bytes, mime_type: str, prompt: str) -> str:
  return await asyncio.to_thread(
    _analyze_image_with_openai_sync,
    image_bytes=image_bytes,
    mime_type=mime_type,
    prompt=prompt,
  )


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

    if (
      "what can you do" in ql
      or "what can u do" in ql
      or "what can you help with" in ql
    ):
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


def _conversation_reply_text_from_resolution(
  resolution: InputEventResolution | None,
) -> str:
  if resolution is None:
    return ""
  metadata = resolution.metadata_json if isinstance(resolution.metadata_json, dict) else {}
  reply_contract = (
    metadata.get("communication_reply_contract")
    if isinstance(metadata.get("communication_reply_contract"), dict)
    else {}
  )
  reply_text = str(reply_contract.get("reply_text") or "").strip()
  if reply_text:
    return reply_text
  result_override = str(metadata.get("mim_interface_result_override") or "").strip()
  if result_override:
    return result_override
  clarification_prompt = str(resolution.clarification_prompt or "").strip()
  if clarification_prompt:
    return clarification_prompt
  return str(metadata.get("mim_interface_reply_override") or "").strip()


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
    active_work: dict | None = None,
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
    active_work = active_work if isinstance(active_work, dict) else {}
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
    active_work_summary = str(active_work.get("summary") or "").strip()
    if active_work_summary:
      parts.append(f"Active work: {active_work_summary}")
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
    natural_language_development = (
        packet.get("natural_language_development", {})
        if isinstance(packet.get("natural_language_development", {}), dict)
        else {}
    )
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
    natural_language_development_summary = _compact_sentence(
        str(natural_language_development.get("summary") or "").strip(),
        max_len=220,
    )
    natural_language_development_active_slice = _compact_sentence(
      str(natural_language_development.get("active_slice_summary") or "").strip(),
      max_len=220,
    )
    natural_language_development_progress = _compact_sentence(
      str(natural_language_development.get("progress_summary") or "").strip(),
      max_len=220,
    )
    natural_language_development_next_step = _compact_sentence(
        str(natural_language_development.get("next_step_summary") or "").strip(),
        max_len=220,
    )
    natural_language_development_pass_bar = _compact_sentence(
        str(natural_language_development.get("selected_skill_pass_bar_summary") or "").strip(),
        max_len=220,
    )
    natural_language_development_continuation = _compact_sentence(
      str(natural_language_development.get("continuation_policy_summary") or "").strip(),
      max_len=220,
    )
    natural_language_development_whats_next = _compact_sentence(
      str(natural_language_development.get("whats_next_framework_summary") or "").strip(),
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
        "natural_language_development": natural_language_development,
        "natural_language_development_summary": natural_language_development_summary,
        "natural_language_development_active_slice": natural_language_development_active_slice,
        "natural_language_development_progress": natural_language_development_progress,
        "natural_language_development_next_step": natural_language_development_next_step,
        "natural_language_development_pass_bar": natural_language_development_pass_bar,
        "natural_language_development_continuation": natural_language_development_continuation,
        "natural_language_development_whats_next": natural_language_development_whats_next,
        "natural_language_development_skill_id": str(
            natural_language_development.get("selected_skill_id") or ""
        ).strip(),
        "natural_language_development_skill_title": str(
            natural_language_development.get("selected_skill_title") or ""
        ).strip(),
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
  authoritative_request = load_authoritative_request_status(shared_root=shared_root)
  if authoritative_request:
    request_id = str(authoritative_request.get("request_id") or "").strip()
    return {
      "request_id": request_id,
      "task_id": str(authoritative_request.get("task_id") or request_id).strip(),
      "execution_id": request_id,
      "id_kind": "bridge_request_id",
      "execution_lane": "primitive_request_recovery",
      "execution_id_label": f"request {request_id}",
      "generated_at": str(authoritative_request.get("generated_at") or "").strip(),
      "type": "primitive_request_recovery",
      "summary": _compact_sentence(
        f"request {request_id} | {authoritative_request.get('result_status') or 'unknown'} | {authoritative_request.get('decision_code') or 'decision_recorded'}",
        max_len=180,
      ),
      "active_workstream": {
        "name": "primitive_request_recovery",
        "mim_status": str(authoritative_request.get("decision_code") or "").strip(),
        "tod_status": str(authoritative_request.get("result_status") or "").strip(),
        "latest_observation": _compact_sentence(
          str(authoritative_request.get("decision_detail") or authoritative_request.get("result_reason") or "").strip(),
          max_len=180,
        ),
      },
      "workstreams": [],
    }

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
  authoritative_request = load_authoritative_request_status(shared_root=shared_root)
  if authoritative_request:
    request_id = str(authoritative_request.get("request_id") or "").strip()
    return {
      "request_id": request_id,
      "task_id": str(authoritative_request.get("task_id") or request_id).strip(),
      "correlation_id": request_id,
      "execution_id": request_id,
      "execution_lane": "primitive_request_recovery",
      "command_name": str(authoritative_request.get("action_name") or "").strip(),
      "dispatch_timestamp": str(authoritative_request.get("generated_at") or "").strip(),
      "host_received_timestamp": "",
      "host_completed_timestamp": str(authoritative_request.get("generated_at") or "").strip(),
      "dispatch_status": str(authoritative_request.get("request_status") or "recorded").strip(),
      "completion_status": str(authoritative_request.get("result_status") or "").strip(),
      "result_reason": str(authoritative_request.get("result_reason") or "").strip(),
      "record_path": str(shared_root / "TOD_MIM_TASK_RESULT.latest.json"),
      "evidence_source_kinds": ["decision_artifact", "request_artifact", "result_artifact"],
      "summary": _compact_sentence(
        f"{authoritative_request.get('action_name') or 'request'}; request {request_id}; completion {authoritative_request.get('result_status') or 'unknown'}",
        max_len=180,
      ),
    }

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


def _coordination_request_identifier(payload: dict) -> str:
  if not isinstance(payload, dict):
    return ""
  return str(payload.get("request_id") or payload.get("task_id") or "").strip()


def _coordination_status_value(payload: dict) -> str:
  if not isinstance(payload, dict):
    return ""
  coordination = payload.get("coordination") if isinstance(payload.get("coordination"), dict) else {}
  return str(
    payload.get("ack_status")
    or payload.get("status")
    or coordination.get("status")
    or ""
  ).strip().lower()


def _artifact_latest_timestamp(payload: dict) -> datetime | None:
  if not isinstance(payload, dict):
    return None
  latest: datetime | None = None
  for key in ("generated_at", "emitted_at", "acknowledged_at", "updated_at", "published_at"):
    parsed = _parse_payload_timestamp(payload.get(key))
    if parsed is not None and (latest is None or parsed > latest):
      latest = parsed
  return latest


def _coordination_ack_matches_request(request_status: str, ack_status: str) -> bool:
  normalized_request = str(request_status or "").strip().lower()
  normalized_ack = str(ack_status or "").strip().lower()
  pending_statuses = {"pending", "acknowledged", "accepted", "active", "in_progress", "pending_review"}
  resolved_statuses = {"resolved", "closed", "done", "complete", "completed"}
  if normalized_request in {"resolved", "closed", "done", "complete", "completed", "none"}:
    return normalized_ack in resolved_statuses
  if normalized_request in {"", "active", "pending", "open", "new", "received", "in_review", "reviewing"}:
    return normalized_ack in pending_statuses
  return normalized_ack == normalized_request


def _build_tod_truth_reconciliation_snapshot(
    *,
    initiative_driver: dict,
    authoritative_request: dict,
    shared_root: Path = SHARED_RUNTIME_ROOT,
) -> dict:
  initiative = initiative_driver if isinstance(initiative_driver, dict) else {}
  request = authoritative_request if isinstance(authoritative_request, dict) else {}
  active_objective = initiative.get("active_objective") if isinstance(initiative.get("active_objective"), dict) else {}
  integration_payload = _load_json_artifact(shared_root / "TOD_INTEGRATION_STATUS.latest.json")
  canonical_objective_id = str(
    ((integration_payload.get("mim_handshake") or {}).get("current_next_objective") if isinstance(integration_payload.get("mim_handshake"), dict) else "")
    or ((integration_payload.get("mim_status") or {}).get("objective_active") if isinstance(integration_payload.get("mim_status"), dict) else "")
    or ((integration_payload.get("objective_alignment") or {}).get("mim_objective_active") if isinstance(integration_payload.get("objective_alignment"), dict) else "")
    or active_objective.get("objective_id")
    or active_objective.get("id")
    or ""
  ).strip()
  live_request_objective_id = str(
    ((integration_payload.get("live_task_request") or {}).get("normalized_objective_id") if isinstance(integration_payload.get("live_task_request"), dict) else "")
    or ((integration_payload.get("live_task_request") or {}).get("objective_id") if isinstance(integration_payload.get("live_task_request"), dict) else "")
    or request.get("objective_id")
    or ""
  ).strip()

  truth_payload = _load_json_artifact(shared_root / "TOD_EXECUTION_TRUTH.latest.json")
  execution_decision = _load_json_artifact(shared_root / "TOD_MIM_EXECUTION_DECISION.latest.json")
  coordination_request = _load_json_artifact(shared_root / "TOD_MIM_COORDINATION_REQUEST.latest.json")
  coordination_ack = _load_json_artifact(shared_root / "MIM_TOD_COORDINATION_ACK.latest.json")
  fallback_activation = _load_json_artifact(shared_root / "MIM_TOD_FALLBACK_ACTIVATION.latest.json")
  bridge_task_ack = _load_json_artifact(shared_root / "TOD_MIM_TASK_ACK.latest.json")
  bridge_task_result = _load_json_artifact(shared_root / "TOD_MIM_TASK_RESULT.latest.json")
  bridge_consume_evidence = _load_json_artifact(shared_root / "MIM_TOD_CONSUME_EVIDENCE.latest.json")
  authoritative_request_status = load_authoritative_request_status(shared_root=shared_root) or {}

  summary_payload = truth_payload.get("summary") if isinstance(truth_payload.get("summary"), dict) else {}
  truth_rows = truth_payload.get("recent_execution_truth") if isinstance(truth_payload.get("recent_execution_truth"), list) else []
  execution_count = int(summary_payload.get("execution_count") or len(truth_rows) or 0)
  active_request_id = str(
    authoritative_request_status.get("request_id")
    or authoritative_request_status.get("task_id")
    or request.get("request_id")
    or request.get("task_id")
    or ""
  ).strip()
  active_task_id = str(
    authoritative_request_status.get("task_id")
    or authoritative_request_status.get("request_id")
    or request.get("task_id")
    or active_request_id
    or ""
  ).strip()
  active_objective_id = str(
    authoritative_request_status.get("objective_id")
    or request.get("objective_id")
    or canonical_objective_id
    or ""
  ).strip().replace("objective-", "")
  lineage_mismatch = bool(authoritative_request_status.get("lineage_mismatch"))
  decision_state = str(execution_decision.get("execution_state") or "").strip().lower()
  decision_outcome = str(execution_decision.get("decision_outcome") or "").strip().lower()
  decision_summary = str(execution_decision.get("summary") or "").strip()
  fallback_task_id = str(fallback_activation.get("task_id") or "").strip()
  fallback_objective_id = str(fallback_activation.get("objective_id") or "").strip()
  fallback_execution_state = str(fallback_activation.get("execution_state") or "").strip().lower()
  fallback_decision_outcome = str(fallback_activation.get("decision_outcome") or "").strip().lower()
  fallback_summary = str(fallback_activation.get("summary") or "").strip()
  fallback_matches_objective = bool(
    fallback_objective_id
    and canonical_objective_id
    and str(fallback_objective_id).replace("objective-", "") == str(canonical_objective_id).replace("objective-", "")
  )
  fallback_active = bool(
    fallback_matches_objective
    and fallback_task_id
    and active_task_id
    and fallback_task_id == active_task_id
    and fallback_execution_state in {"accepted", "running", "completed"}
    and fallback_decision_outcome == "mim_direct_execution_takeover"
  )

  coordination_request_id = _coordination_request_identifier(coordination_request)
  coordination_request_status = str((coordination_request or {}).get("status") or "").strip().lower()
  coordination_ack_id = _coordination_request_identifier(coordination_ack)
  coordination_ack_status = _coordination_status_value(coordination_ack)
  coordination_ack_matches = bool(
    coordination_request_id
    and coordination_ack_id == coordination_request_id
    and _coordination_ack_matches_request(coordination_request_status, coordination_ack_status)
  )
  coordination_request_ts = _artifact_latest_timestamp(coordination_request)
  coordination_ack_ts = _artifact_latest_timestamp(coordination_ack)
  coordination_request_newer = bool(
    coordination_request_ts is not None
    and (coordination_ack_ts is None or coordination_request_ts > coordination_ack_ts)
  )
  coordination_pending = bool(
    coordination_request_id
    and coordination_request_status not in {"resolved", "closed", "done", "complete", "completed", "none"}
  )
  coordination_response_missing = bool(
    coordination_pending and (not coordination_ack_matches or coordination_request_newer)
  )

  def _bridge_payload_matches(payload: dict) -> bool:
    if not isinstance(payload, dict):
      return False
    identity = _resolve_execution_identity(payload)
    candidate_task_id = str(
      identity.get("task_id")
      or payload.get("task_id")
      or payload.get("task")
      or ""
    ).strip()
    candidate_request_id = str(
      identity.get("request_id")
      or payload.get("request_id")
      or ""
    ).strip()
    candidate_objective_id = str(
      identity.get("objective_id")
      or payload.get("objective_id")
      or ""
    ).strip().replace("objective-", "")
    if not active_task_id or not candidate_task_id or candidate_task_id != active_task_id:
      return False
    if active_request_id and candidate_request_id and candidate_request_id != active_request_id:
      return False
    if active_objective_id and candidate_objective_id and candidate_objective_id != active_objective_id:
      return False
    return True

  positive_bridge_statuses = {"acknowledged", "accepted", "active", "running", "in_progress", "pending_review", "succeeded", "done", "completed", "complete", "resolved", "closed"}
  bridge_request_confirmed = False
  bridge_confirmation_source = ""
  bridge_request_status = str(authoritative_request_status.get("request_status") or "").strip().lower()
  bridge_result_status = str(authoritative_request_status.get("result_status") or "").strip().lower()
  if active_request_id and (bridge_request_status in positive_bridge_statuses or bridge_result_status in positive_bridge_statuses):
    bridge_request_confirmed = True
    bridge_confirmation_source = "authoritative_request_status"
  if lineage_mismatch:
    bridge_request_confirmed = False
    bridge_confirmation_source = ""
  if not bridge_request_confirmed and _bridge_payload_matches(bridge_task_ack):
    ack_status = str(bridge_task_ack.get("status") or "").strip().lower()
    if ack_status in positive_bridge_statuses:
      bridge_request_confirmed = True
      bridge_confirmation_source = "tod_mim_task_ack"
  if not bridge_request_confirmed and _bridge_payload_matches(bridge_task_result):
    result_status = str(bridge_task_result.get("status") or bridge_task_result.get("result_status") or "").strip().lower()
    if result_status in positive_bridge_statuses:
      bridge_request_confirmed = True
      bridge_confirmation_source = "tod_mim_task_result"
  if not bridge_request_confirmed and _bridge_payload_matches(bridge_consume_evidence):
    current_payload = bridge_consume_evidence.get("current") if isinstance(bridge_consume_evidence.get("current"), dict) else {}
    task_ack = current_payload.get("task_ack") if isinstance(current_payload.get("task_ack"), dict) else {}
    task_result = current_payload.get("task_result") if isinstance(current_payload.get("task_result"), dict) else {}
    ack_status = str(task_ack.get("status") or "").strip().lower()
    result_status = str((task_result.get("task_result") or task_result.get("status") or task_result.get("result_status") or "")).strip().lower()
    if (_bridge_payload_matches(task_ack) and ack_status in positive_bridge_statuses) or (
      _bridge_payload_matches(task_result) and result_status in positive_bridge_statuses
    ):
      bridge_request_confirmed = True
      bridge_confirmation_source = "mim_tod_consume_evidence"

  negative_decision_states = {
    "waiting_on_dependency",
    "blocked",
    "failed",
    "stale",
    "unknown",
    "hold",
  }
  negative_decision_outcomes = {
    "acknowledge_and_wait_on_dependency",
    "blocked",
    "failed",
    "hold",
  }
  execution_confirmed = bool(execution_count > 0)
  if decision_state in negative_decision_states or decision_outcome in negative_decision_outcomes:
    execution_confirmed = False
  if fallback_active:
    execution_confirmed = True
  if bridge_request_confirmed and decision_state not in negative_decision_states and decision_outcome not in negative_decision_outcomes:
    execution_confirmed = True

  state = "execution_confirmed" if execution_confirmed else "execution_unconfirmed"
  summary = "TOD has not published recent execution confirmation for the current work yet."
  if execution_confirmed:
    if execution_count > 0:
      summary = (
        f"TOD has published {execution_count} recent execution confirmation"
        f"{'' if execution_count == 1 else 's'} on the shared truth surface."
      )
    elif bridge_request_confirmed:
      summary = "TOD has confirmed execution on the bridge request lane for the active request."
  elif decision_summary:
    summary = decision_summary
  if fallback_active:
    state = "execution_confirmed"
    summary = fallback_summary or "MIM claimed bounded fallback authority and is executing the active task locally."
  elif lineage_mismatch:
    state = "lineage_mismatch"
    execution_confirmed = False
    summary = "TOD bridge artifacts disagreed with the active request lineage, so stale execution evidence was ignored."

  if coordination_response_missing:
    state = "coordination_response_missing"
    issue_code = str((coordination_request or {}).get("issue_code") or "coordination_request_active").strip().replace("_", " ")
    summary = _compact_sentence(
      f"TOD is waiting on MIM to answer coordination request {coordination_request_id}. "
      f"Issue: {issue_code}. Publish a current coordination ACK before claiming completion.",
      max_len=220,
    )

  return {
    "state": state,
    "authoritative_source": "MIM" if fallback_active else "TOD",
    "execution_confirmed": execution_confirmed,
    "canonical_objective_id": canonical_objective_id,
    "live_request_objective_id": live_request_objective_id,
    "decision_state": decision_state,
    "decision_outcome": decision_outcome,
    "decision_summary": decision_summary,
    "fallback_active": fallback_active,
    "fallback_execution_state": fallback_execution_state,
    "fallback_task_id": fallback_task_id,
    "execution_count": execution_count,
    "bridge_request_id": active_request_id,
    "bridge_request_confirmed": bridge_request_confirmed,
    "bridge_confirmation_source": bridge_confirmation_source,
    "coordination_request_id": coordination_request_id,
    "coordination_request_status": coordination_request_status,
    "coordination_ack_id": coordination_ack_id,
    "coordination_ack_status": coordination_ack_status,
    "coordination_pending": coordination_pending,
    "coordination_response_missing": coordination_response_missing,
    "lineage_mismatch": lineage_mismatch,
    "summary": summary,
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


def _operator_active_work_snapshot(
    collaboration_progress: dict | None,
    feedback_loop: dict | None,
) -> dict:
  collaboration = collaboration_progress if isinstance(collaboration_progress, dict) else {}
  feedback = feedback_loop if isinstance(feedback_loop, dict) else {}
  request_id = str(collaboration.get("request_id") or "").strip()
  task_id = str(collaboration.get("task_id") or "").strip()
  execution_id = str(collaboration.get("execution_id") or "").strip()
  execution_label = str(collaboration.get("execution_id_label") or execution_id or "").strip()
  collaboration_summary = str(collaboration.get("summary") or "").strip()
  active_workstream = (
    collaboration.get("active_workstream")
    if isinstance(collaboration.get("active_workstream"), dict)
    else {}
  )
  work_name = str(active_workstream.get("name") or "").strip()
  work_status = str(active_workstream.get("tod_status") or "").strip().lower()
  latest_observation = str(active_workstream.get("latest_observation") or "").strip()
  feedback_status = str(feedback.get("latest_status") or "").strip().lower()

  tracked = bool(request_id or task_id or execution_id)
  state = "reply_only"
  badge = "Reply only"
  evidence_source = "conversation"

  if tracked:
    evidence_source = "tracked_request"
    if work_status in {"completed", "done", "succeeded", "success"}:
      state = "completed"
      badge = "Completed"
    elif work_status in {"failed", "blocked", "error"}:
      state = "blocked"
      badge = "Blocked"
    else:
      state = "working"
      badge = "Working now"
  elif feedback_status:
    evidence_source = "feedback_loop"
    if feedback_status in {"succeeded", "completed", "done"}:
      state = "completed"
      badge = "Completed"
    elif feedback_status in {"failed", "blocked", "error"}:
      state = "blocked"
      badge = "Blocked"
    elif feedback_status in {"accepted", "running", "pending", "dispatched"}:
      state = "working"
      badge = "Working now"

  if state == "reply_only":
    summary = "No tracked work is active right now. The latest turn may have been conversation-only."
  else:
    headline = execution_label or task_id or request_id or "tracked work"
    details = latest_observation or collaboration_summary or str(feedback.get("summary") or "").strip()
    scope = work_name.replace("_", " ") if work_name else ""
    status_text = work_status.replace("_", " ") if work_status else ""
    summary_parts = [f"{headline} is {badge.lower()}."]
    if scope:
      summary_parts.append(f"Workstream: {scope}.")
    if status_text and status_text not in {badge.lower(), "working now"}:
      summary_parts.append(f"Status: {status_text}.")
    if details:
      summary_parts.append(details)
    summary = _compact_sentence(" ".join(summary_parts), max_len=220)

  return {
    "tracked": tracked or state != "reply_only",
    "state": state,
    "badge": badge,
    "request_id": request_id,
    "task_id": task_id,
    "execution_id": execution_id,
    "execution_id_label": execution_label,
    "workstream_name": work_name,
    "workstream_status": work_status,
    "evidence_source": evidence_source,
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


def _latest_timestamp_value(*values: object) -> str:
  latest: datetime | None = None
  pending = list(values)
  while pending:
    value = pending.pop(0)
    if isinstance(value, (list, tuple, set)):
      pending.extend(list(value))
      continue
    parsed = _parse_payload_timestamp(value)
    if parsed is None:
      continue
    if latest is None or parsed > latest:
      latest = parsed
  return latest.isoformat().replace("+00:00", "Z") if latest is not None else ""


def _runtime_recovery_activity_timestamps(runtime_recovery: dict) -> list[str]:
  recovery = runtime_recovery if isinstance(runtime_recovery, dict) else {}
  lanes = recovery.get("lanes") if isinstance(recovery.get("lanes"), dict) else {}
  timestamps: list[str] = []
  for lane in lanes.values():
    if not isinstance(lane, dict):
      continue
    for key in (
      "last_recovery_attempt_at",
      "last_healthy_frame_at",
      "last_frame_seen_at",
      "first_healthy_at",
    ):
      value = str(lane.get(key) or "").strip()
      if value:
        timestamps.append(value)
  return timestamps


def _build_system_activity_snapshot(
    *,
    initiative_driver: dict,
    operator_reasoning: dict,
    runtime_health: dict,
    runtime_recovery: dict,
    authoritative_request: dict,
    collaboration_progress: dict,
    dispatch_telemetry: dict,
    tod_decision_process: dict,
) -> dict:
  initiative = initiative_driver if isinstance(initiative_driver, dict) else {}
  reasoning = operator_reasoning if isinstance(operator_reasoning, dict) else {}
  health = runtime_health if isinstance(runtime_health, dict) else {}
  recovery = runtime_recovery if isinstance(runtime_recovery, dict) else {}
  request = authoritative_request if isinstance(authoritative_request, dict) else {}
  collaboration = collaboration_progress if isinstance(collaboration_progress, dict) else {}
  dispatch = dispatch_telemetry if isinstance(dispatch_telemetry, dict) else {}
  tod_decision = tod_decision_process if isinstance(tod_decision_process, dict) else {}

  activity = initiative.get("activity") if isinstance(initiative.get("activity"), dict) else {}
  active_task = initiative.get("active_task") if isinstance(initiative.get("active_task"), dict) else {}
  next_task = initiative.get("next_task") if isinstance(initiative.get("next_task"), dict) else {}
  progress = initiative.get("progress") if isinstance(initiative.get("progress"), dict) else {}
  active_objective = (
    initiative.get("active_objective")
    if isinstance(initiative.get("active_objective"), dict)
    else {}
  )
  readiness = (
    reasoning.get("execution_readiness")
    if isinstance(reasoning.get("execution_readiness"), dict)
    else {}
  )
  active_work = (
    reasoning.get("active_work")
    if isinstance(reasoning.get("active_work"), dict)
    else {}
  )
  stability_guard = (
    reasoning.get("stability_guard")
    if isinstance(reasoning.get("stability_guard"), dict)
    else {}
  )
  escalation = (
    tod_decision.get("communication_escalation")
    if isinstance(tod_decision.get("communication_escalation"), dict)
    else {}
  )

  activity_state = str(activity.get("state") or "idle").strip().lower() or "idle"
  active_work_state = str(active_work.get("state") or "").strip().lower()
  progress_percent = float(progress.get("percent") or 0.0)
  recovery_status = str(recovery.get("status") or "healthy").strip().lower() or "healthy"
  execution_allowed = bool(readiness.get("execution_allowed", True))
  readiness_summary = str(readiness.get("summary") or "").strip()
  readiness_gate_state = str(readiness.get("gate_state") or "").strip().lower()
  activity_summary = str(activity.get("summary") or "").strip()
  active_work_summary = str(active_work.get("summary") or "").strip()
  canonical_objective_id = str(
    active_objective.get("objective_id") or active_objective.get("id") or ""
  ).strip()
  live_request_objective_id = str(request.get("objective_id") or "").strip()

  should_be_working = False
  should_be_working_reason = "No active objective or follow-on work is currently visible."
  if activity_state == "completed" and not next_task:
    should_be_working = False
    should_be_working_reason = "The active objective is already marked complete."
  elif active_task:
    should_be_working = True
    should_be_working_reason = "An active bounded task is present in the initiative driver."
  elif next_task:
    should_be_working = True
    should_be_working_reason = "A follow-on bounded task is ready but not executing yet."
  elif canonical_objective_id and activity_state in {"idle", "working", "stale", "stuck"}:
    should_be_working = True
    should_be_working_reason = "The initiative still has an active objective loaded."
  elif bool(active_work.get("tracked")):
    should_be_working = True
    should_be_working_reason = "Tracked collaboration work is still visible in the handoff layer."

  tod_truth_reconciliation = _build_tod_truth_reconciliation_snapshot(
    initiative_driver=initiative,
    authoritative_request=request,
    shared_root=SHARED_RUNTIME_ROOT,
  )
  canonical_objective_id = str(
    tod_truth_reconciliation.get("canonical_objective_id") or canonical_objective_id
  ).strip()
  live_request_objective_id = str(
    tod_truth_reconciliation.get("live_request_objective_id") or live_request_objective_id
  ).strip()
  objective_drift = bool(
    canonical_objective_id
    and live_request_objective_id
    and canonical_objective_id != live_request_objective_id
  )
  completion_signal_visible = bool(
    activity_state == "completed"
    or active_work_state == "completed"
    or progress_percent >= 100.0
  )
  tod_truth_reconciliation = dict(tod_truth_reconciliation)
  tod_truth_reconciliation["should_override_completion"] = bool(
    completion_signal_visible
    and not bool(tod_truth_reconciliation.get("execution_confirmed", False))
  )
  if bool(tod_truth_reconciliation.get("coordination_response_missing", False)):
    tod_truth_reconciliation["progress_label"] = "Waiting on MIM"
    tod_truth_reconciliation["progress_detail"] = str(tod_truth_reconciliation.get("summary") or "").strip()
  elif bool(tod_truth_reconciliation.get("should_override_completion", False)):
    tod_truth_reconciliation["progress_label"] = "Execution unconfirmed"
    tod_truth_reconciliation["progress_detail"] = str(tod_truth_reconciliation.get("summary") or "").strip()

  frontend_media = (
    health.get("frontend_media") if isinstance(health.get("frontend_media"), dict) else {}
  )
  heartbeat_at = _latest_timestamp_value(
    (
      health.get("latest", {}) if isinstance(health.get("latest", {}), dict) else {}
    ).get("camera", {}).get("last_seen_at") if isinstance((health.get("latest", {}) if isinstance(health.get("latest", {}), dict) else {}).get("camera", {}), dict) else "",
    (
      health.get("latest", {}) if isinstance(health.get("latest", {}), dict) else {}
    ).get("microphone", {}).get("last_seen_at") if isinstance((health.get("latest", {}) if isinstance(health.get("latest", {}), dict) else {}).get("microphone", {}), dict) else "",
    (
      health.get("latest", {}) if isinstance(health.get("latest", {}), dict) else {}
    ).get("speech_output", {}).get("created_at") if isinstance((health.get("latest", {}) if isinstance(health.get("latest", {}), dict) else {}).get("speech_output", {}), dict) else "",
    frontend_media.get("camera", {}).get("last_reported_at") if isinstance(frontend_media.get("camera", {}), dict) else "",
    frontend_media.get("microphone", {}).get("last_reported_at") if isinstance(frontend_media.get("microphone", {}), dict) else "",
    request.get("generated_at"),
    collaboration.get("generated_at"),
    dispatch.get("dispatch_timestamp"),
    dispatch.get("host_received_timestamp"),
    dispatch.get("host_completed_timestamp"),
    tod_decision.get("generated_at"),
  )
  heartbeat_age_seconds = None
  heartbeat_dt = _parse_payload_timestamp(heartbeat_at)
  if heartbeat_dt is not None:
    heartbeat_age_seconds = max(0.0, (datetime.now(timezone.utc) - heartbeat_dt).total_seconds())

  alignment_summary = "MIM and TOD agree on the active objective."
  alignment_label = "Aligned"
  if objective_drift:
    alignment_label = f"{canonical_objective_id} vs {live_request_objective_id}"
    alignment_summary = (
      f"Canonical objective is {canonical_objective_id}, but the live TOD request still references {live_request_objective_id}."
    )
  elif should_be_working and canonical_objective_id and not live_request_objective_id:
    alignment_label = "No live request"
    alignment_summary = "The initiative has active work, but no live TOD request is visible for the current objective."

  reason_candidates: list[str] = []
  if objective_drift or alignment_label == "No live request":
    reason_candidates.append(alignment_summary)
  if activity_state in {"stale", "stuck"} and activity_summary:
    reason_candidates.append(activity_summary)
  if should_be_working and active_work_state == "completed" and next_task:
    reason_candidates.append(
      "The latest tracked TOD request is completed, but the initiative already has follow-on work queued."
    )
  if should_be_working and not execution_allowed and readiness_summary:
    reason_candidates.append(readiness_summary)
  if recovery_status in {"degraded", "suboptimal"} and str(recovery.get("summary") or "").strip():
    reason_candidates.append(str(recovery.get("summary") or "").strip())
  if bool(escalation.get("required", False)):
    reason_candidates.append(
      str(escalation.get("detail") or escalation.get("code") or "TOD escalation required").strip()
    )
  if bool(stability_guard.get("active", False)) and str(stability_guard.get("summary") or "").strip():
    reason_candidates.append(str(stability_guard.get("summary") or "").strip())
  if should_be_working and activity_state == "idle":
    reason_candidates.append(
      "The initiative has ready work, but no fresh bounded execution signal is visible right now."
    )

  deduped_reasons: list[str] = []
  for item in reason_candidates:
    cleaned = str(item or "").strip()
    if cleaned and cleaned not in deduped_reasons:
      deduped_reasons.append(cleaned)
  stall_reason = _compact_sentence(" ".join(deduped_reasons[:3]), max_len=260)

  mim_last_activity_at = _latest_timestamp_value(
    activity.get("started_at"),
    collaboration.get("generated_at"),
    dispatch.get("dispatch_timestamp"),
    _runtime_recovery_activity_timestamps(recovery),
  )
  last_task_progress_at = _latest_timestamp_value(
    dispatch.get("host_completed_timestamp"),
    dispatch.get("host_received_timestamp"),
    collaboration.get("generated_at"),
    request.get("generated_at"),
  )
  tod_last_activity_at = _latest_timestamp_value(
    request.get("generated_at"),
    tod_decision.get("generated_at"),
    dispatch.get("host_received_timestamp"),
    dispatch.get("host_completed_timestamp"),
  )

  last_task_progress_age_seconds = None
  last_task_progress_dt = _parse_payload_timestamp(last_task_progress_at)
  if last_task_progress_dt is not None:
    last_task_progress_age_seconds = max(0.0, (datetime.now(timezone.utc) - last_task_progress_dt).total_seconds())

  if heartbeat_age_seconds is None:
    heartbeat_state = "unknown"
  elif heartbeat_age_seconds <= 30.0:
    heartbeat_state = "fresh"
  elif heartbeat_age_seconds <= 120.0:
    heartbeat_state = "aging"
  elif heartbeat_age_seconds <= 600.0:
    heartbeat_state = "stale"
  else:
    heartbeat_state = "frozen"

  if bool(tod_truth_reconciliation.get("coordination_response_missing", False)):
    status_code = "stale"
    status_label = "WAITING ON MIM"
    headline = "WAITING ON MIM - TOD requested coordination and needs a current response"
    tone = "error"
  elif bool(tod_truth_reconciliation.get("should_override_completion", False)):
    status_code = "warning"
    status_label = "UNCONFIRMED"
    headline = "UNCONFIRMED - TOD has not confirmed execution"
    tone = "warn"
  elif heartbeat_state == "frozen" or (
    should_be_working
    and heartbeat_state in {"stale", "unknown"}
    and recovery_status == "degraded"
  ):
    status_code = "frozen"
    status_label = "FROZEN"
    headline = "FROZEN - no usable heartbeat or recovery"
    tone = "error"
  elif (
    objective_drift
    or activity_state in {"stale", "stuck"}
    or bool(escalation.get("required", False))
    or (should_be_working and last_task_progress_age_seconds is not None and last_task_progress_age_seconds >= 600.0)
  ):
    status_code = "stale"
    status_label = "STALE"
    headline = "STALE - expected work but no real progress"
    tone = "error"
  elif (
    not execution_allowed
    or readiness_gate_state in {"blocked", "degraded"}
    or recovery_status in {"degraded", "suboptimal"}
    or (should_be_working and activity_state == "idle")
    or (should_be_working and last_task_progress_age_seconds is not None and last_task_progress_age_seconds >= 300.0)
  ):
    status_code = "warning"
    status_label = "WARNING"
    headline = "WARNING - alive, but execution is constrained"
    tone = "warn"
  elif should_be_working and execution_allowed and activity_state == "working":
    status_code = "active"
    status_label = "ACTIVE"
    headline = "ACTIVE - executing tasks"
    tone = "active"
  else:
    status_code = "idle"
    status_label = "IDLE"
    headline = "IDLE - healthy, no live task right now"
    tone = "ready"

  if status_code == "active":
    summary = activity_summary or active_work_summary or "MIM is actively advancing the current objective."
  elif status_code == "idle":
    summary = activity_summary or "MIM is healthy, but no live task is currently executing."
  elif status_code == "warning":
    summary = str(tod_truth_reconciliation.get("summary") or "").strip() or stall_reason or readiness_summary or "MIM is alive, but something is preventing clean execution."
  elif status_code == "stale":
    summary = str(tod_truth_reconciliation.get("summary") or "").strip() or stall_reason or "Expected work is not advancing."
  else:
    summary = stall_reason or "Heartbeat and recovery evidence are too old to trust active execution."

  relation_flow = "Flowing"
  if bool(tod_truth_reconciliation.get("coordination_response_missing", False)):
    relation_flow = "Waiting on MIM"
  elif bool(tod_truth_reconciliation.get("should_override_completion", False)):
    relation_flow = "Awaiting TOD confirmation"
  elif not execution_allowed:
    relation_flow = "Blocked"
  elif objective_drift:
    relation_flow = "Drifted"
  elif status_code in {"stale", "frozen"}:
    relation_flow = "Stalled"
  elif should_be_working and status_code in {"warning", "idle"}:
    relation_flow = "Waiting"

  tod_liveness = (
    tod_decision.get("tod_liveness")
    if isinstance(tod_decision.get("tod_liveness"), dict)
    else {}
  )
  bridge_health = "Healthy"
  if bool(tod_truth_reconciliation.get("coordination_response_missing", False)):
    bridge_health = "Waiting on MIM"
  elif bool(escalation.get("required", False)):
    bridge_health = "Escalated"
  elif objective_drift:
    bridge_health = "Out of sync"
  elif str(tod_liveness.get("status") or "").strip().lower() in {"stale", "terminal", "unknown"}:
    bridge_health = str(tod_liveness.get("status") or "Attention").strip().replace("_", " ")

  staleness_state = "fresh"
  if status_code == "warning":
    staleness_state = "warning"
  elif status_code == "stale":
    staleness_state = "stale"
  elif status_code == "frozen":
    staleness_state = "frozen"

  execution_allowed_reason = str(tod_truth_reconciliation.get("summary") or "").strip() if bool(
    tod_truth_reconciliation.get("coordination_response_missing", False)
    or tod_truth_reconciliation.get("should_override_completion", False)
  ) else readiness_summary or (
    "Execution is allowed."
    if execution_allowed
    else "Execution is currently blocked by readiness policy."
  )
  heartbeat_detail = (
    f"Heartbeat {heartbeat_state}."
    if heartbeat_age_seconds is None
    else f"Heartbeat {heartbeat_state}; last signal about {int(round(heartbeat_age_seconds))} seconds ago."
  )
  relation_summary = _compact_sentence(
    f"Objective alignment: {alignment_summary} Bridge health: {bridge_health}. Execution flow: {relation_flow}. Authoritative execution source: {str(tod_truth_reconciliation.get('authoritative_source') or 'TOD').strip()}. {str(tod_truth_reconciliation.get('summary') or '').strip()}",
    max_len=220,
  )
  meter_percent = {
    "active": 88,
    "idle": 24,
    "warning": 52,
    "stale": 18,
    "frozen": 4,
  }.get(status_code, 0)

  return {
    "state": status_code,
    "label": status_label,
    "headline": headline,
    "status_code": status_code,
    "status_label": status_label,
    "tone": tone,
    "summary": summary,
    "authoritative_source": "TOD",
    "authoritative_reason": str(tod_truth_reconciliation.get("summary") or "").strip(),
    "tod_truth_reconciliation": tod_truth_reconciliation,
    "should_be_working": should_be_working,
    "should_be_working_reason": should_be_working_reason,
    "stall_reason": stall_reason or "No current stall evidence.",
    "meter_percent": meter_percent,
    "last_activity_at": _latest_timestamp_value(mim_last_activity_at, tod_last_activity_at),
    "last_task_progress_at": last_task_progress_at,
    "last_task_progress_age_seconds": last_task_progress_age_seconds,
    "mim_last_activity_at": mim_last_activity_at,
    "mim_last_activity_detail": "Latest MIM-side task, dispatch, or runtime recovery evidence.",
    "tod_last_activity_at": tod_last_activity_at,
    "tod_last_activity_detail": "Latest TOD-visible request, decision, or completion evidence.",
    "heartbeat_at": heartbeat_at,
    "heartbeat_state": heartbeat_state,
    "heartbeat_age_seconds": heartbeat_age_seconds,
    "heartbeat_detail": heartbeat_detail,
    "execution_allowed": execution_allowed,
    "execution_allowed_label": "Allowed" if execution_allowed else "Blocked",
    "execution_allowed_reason": execution_allowed_reason,
    "staleness_state": staleness_state,
    "staleness_label": staleness_state.upper(),
    "staleness_detail": stall_reason or heartbeat_detail,
    "recovery_label": str(recovery.get("status") or "healthy").strip().replace("_", " ") or "healthy",
    "recovery_summary": str(recovery.get("summary") or "").strip() or "No recovery issues are visible.",
    "alignment_label": alignment_label,
    "alignment_summary": alignment_summary,
    "relation_summary": relation_summary,
    "relation": {
      "objective_alignment": alignment_label,
      "objective_alignment_detail": alignment_summary,
      "bridge_health": bridge_health,
      "execution_flow": relation_flow,
      "authoritative_source": "TOD",
      "authoritative_reason": str(tod_truth_reconciliation.get("summary") or "").strip(),
      "last_handoff_at": _latest_timestamp_value(request.get("generated_at"), dispatch.get("host_received_timestamp")),
      "last_feedback_at": _latest_timestamp_value(dispatch.get("host_completed_timestamp"), tod_decision.get("generated_at")),
      "summary": relation_summary,
    },
    "canonical_objective_id": canonical_objective_id,
    "live_request_objective_id": live_request_objective_id,
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
    active_work = _operator_active_work_snapshot(collaboration_progress, feedback_loop)
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
            active_work=active_work,
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
        "active_work": active_work,
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
async def mim_ui_page(request: Request, db: AsyncSession = Depends(get_db)):
  dedicated_redirect = _dedicated_public_mim_redirect_target(request)
  if dedicated_redirect:
    return RedirectResponse(url=dedicated_redirect, status_code=307)
  redirect_target = _public_mim_redirect_target(request)
  if redirect_target:
    return RedirectResponse(url=redirect_target, status_code=307)
  auth_redirect = maybe_require_mimtod_page_login(request, next_path="/mim")
  if auth_redirect is not None:
    return auth_redirect
  preloaded_chat_thread = await _load_mim_ui_chat_thread(db=db)
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
    body {
      align-items: stretch;
      padding: 18px;
      gap: 14px;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .panel {
      width: min(1280px, 100%);
      margin: 0 auto;
      padding: 18px;
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(9, 26, 39, 0.96), rgba(7, 19, 29, 0.98));
      box-shadow: 0 22px 56px rgba(0, 0, 0, 0.32);
    }
    .app-shell {
      width: min(1280px, 100%);
      margin: 0 auto;
      display: grid;
      gap: 14px;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      border: 1px solid #17435a;
      border-radius: 18px;
      background: linear-gradient(135deg, rgba(13, 41, 59, 0.98), rgba(9, 25, 36, 0.98));
      box-shadow: 0 18px 42px rgba(0, 0, 0, 0.22);
    }
    .topbar-left,
    .topbar-right,
    .status-chip-row,
    .quick-actions,
    .system-activity-banner {
      margin-top: 14px;
      border: 1px solid rgba(49, 123, 151, 0.55);
      border-radius: 18px;
      background: linear-gradient(135deg, rgba(9, 31, 45, 0.96), rgba(7, 22, 33, 0.98));
      padding: 16px;
      display: grid;
      gap: 14px;
      box-shadow: 0 12px 30px rgba(0, 0, 0, 0.18);
    }
    .system-activity-banner-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }
    .system-activity-banner-copy {
      display: grid;
      gap: 6px;
      min-width: 0;
    }
    .system-activity-banner-copy strong {
      font-size: 18px;
      color: #f3fbff;
      line-height: 1.35;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .system-activity-banner-summary {
      font-size: 13px;
      color: var(--muted);
      line-height: 1.5;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .system-activity-banner-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    .system-relation-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
    }
    .voice-primary-row,
    .composer-meta,
    .thread-tools,
    .sidebar-list,
    .secondary-tab-row,
    .status-metrics {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .topbar-left {
      gap: 14px;
    }
    .topbar-right {
      justify-content: flex-end;
    }
    .mode-toggle {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px;
      border: 1px solid #1d5975;
      border-radius: 999px;
      background: rgba(8, 33, 46, 0.88);
    }
    .mode-chip {
      border: 0;
      border-radius: 999px;
      background: transparent;
      color: var(--muted);
      padding: 7px 12px;
      font-size: 12px;
      font-weight: 600;
    }
    .mode-chip.active {
      background: linear-gradient(135deg, #1795c8, #0f6d92);
      color: #f4fcff;
      box-shadow: 0 8px 18px rgba(10, 67, 92, 0.28);
    }
    .brand-stack {
      display: grid;
      gap: 4px;
    }
    h1 {
      font-size: 24px;
      letter-spacing: 0.14em;
    }
    .surface-kicker {
      font-size: 11px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: #9fcedf;
    }
    .surface-kicker.surface-kicker-nav {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      letter-spacing: 0.08em;
    }
    .console-nav-link {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      border-radius: 999px;
      border: 1px solid rgba(49, 123, 151, 0.4);
      background: rgba(8, 33, 46, 0.5);
      color: #cce8f5;
      text-decoration: none;
      transition: border-color 160ms ease, background 160ms ease, color 160ms ease;
    }
    .console-nav-link.utility {
      border-color: rgba(156, 204, 224, 0.34);
      background: rgba(8, 33, 46, 0.34);
    }
    .console-nav-link:hover {
      border-color: rgba(62, 198, 255, 0.55);
      color: #f3fbff;
    }
    .console-nav-link.active {
      border-color: rgba(62, 198, 255, 0.72);
      background: rgba(19, 78, 101, 0.44);
      color: #f3fbff;
    }
    .console-nav-light {
      width: 9px;
      height: 9px;
      border-radius: 999px;
      background: #4f6470;
      box-shadow: 0 0 0 rgba(0, 0, 0, 0);
    }
    .console-nav-light.ok {
      background: var(--ok);
      box-shadow: 0 0 14px rgba(45, 207, 107, 0.55);
    }
    .console-nav-light.err {
      background: var(--err);
      box-shadow: 0 0 14px rgba(197, 106, 45, 0.45);
    }
    .status-chip,
    .quick-action,
    .status-metric,
    .tab-chip,
    .composer-toggle {
      border: 1px solid #1e5e7c;
      border-radius: 999px;
      background: rgba(11, 43, 61, 0.86);
      color: #dff6ff;
      padding: 8px 12px;
      font-size: 12px;
      line-height: 1;
    }
    .status-chip.subtle,
    .composer-toggle {
      color: var(--muted);
    }
    .status-chip[data-tone="active"] {
      border-color: #39d4ff;
      color: #effcff;
      background: rgba(17, 105, 140, 0.42);
    }
    .status-chip[data-tone="ready"] {
      border-color: #44d59a;
      color: #eafff6;
      background: rgba(17, 92, 63, 0.36);
    }
    .status-chip[data-tone="warn"] {
      border-color: #efb261;
      color: #fff2de;
      background: rgba(101, 61, 17, 0.4);
    }
    .status-chip[data-tone="error"] {
      border-color: #ef8e61;
      color: #fff0ea;
      background: rgba(112, 43, 27, 0.44);
    }
    .status-chip.strong,
    .quick-action.primary,
    .tab-chip.active,
    .composer-send {
      background: linear-gradient(135deg, #1795c8, #0f6d92);
      border-color: #2fc7ee;
      color: #f4fcff;
    }
    .quick-action,
    .tab-chip,
    .composer-attach,
    .composer-mic,
    .composer-send,
    .secondary-action,
    .voice-primary-button,
    .thread-clear {
      transition: transform 140ms ease, filter 140ms ease, border-color 140ms ease;
    }
    .quick-action:hover,
    .tab-chip:hover,
    .composer-attach:hover,
    .composer-mic:hover,
    .composer-send:hover,
    .secondary-action:hover,
    .voice-primary-button:hover,
    .thread-clear:hover {
      transform: translateY(-1px);
    }
    .primary-chat-panel {
      margin-top: 14px;
    }
    .layout-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 14px;
      align-items: start;
    }
    .chat-surface {
      display: grid;
      gap: 14px;
    }
    .primary-chat-panel .chat-log {
      min-height: 420px;
      max-height: 68vh;
    }
    .chat-hero {
      display: grid;
      gap: 12px;
    }
    .chat-summary-row {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }
    .media-self-test-card {
      border: 1px solid #184f67;
      border-radius: 18px;
      padding: 14px;
      background: linear-gradient(180deg, rgba(8, 28, 41, 0.98), rgba(7, 20, 31, 0.98));
      display: grid;
      gap: 12px;
    }
    .media-self-test-header {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .media-self-test-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
    }
    .media-self-test-item {
      display: grid;
      gap: 6px;
      padding: 10px 12px;
      border: 1px solid #184961;
      border-radius: 14px;
      background: rgba(8, 31, 45, 0.88);
      min-width: 0;
    }
    .media-self-test-label {
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #9dc9da;
    }
    .media-self-test-value {
      font-size: 13px;
      line-height: 1.45;
      color: #effaff;
      word-break: break-word;
    }
    .media-self-test-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .context-chip {
      display: inline-flex;
      gap: 8px;
      align-items: flex-start;
      flex-wrap: wrap;
      padding: 10px 14px;
      border-radius: 14px;
      border: 1px solid #1c5069;
      background: rgba(7, 33, 48, 0.92);
      color: #d8eef9;
      font-size: 13px;
      line-height: 1.45;
      white-space: normal;
      overflow-wrap: anywhere;
    }
    .thread-shell {
      display: grid;
      grid-template-rows: auto 1fr auto;
      gap: 12px;
      min-height: 0;
    }
    .thread-card {
      border: 1px solid #184c64;
      border-radius: 20px;
      background: linear-gradient(180deg, rgba(8, 26, 39, 0.98), rgba(7, 21, 31, 0.98));
      padding: 14px;
      display: grid;
      gap: 12px;
      min-height: 0;
    }
    .thread-header {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }
    .thread-title {
      display: grid;
      gap: 4px;
    }
    .thread-title strong {
      font-size: 18px;
      color: #f3fbff;
    }
    .thread-title span {
      font-size: 13px;
      color: var(--muted);
    }
    .chat-log {
      margin-top: 0;
      min-height: 420px;
      max-height: none;
      height: min(62vh, 820px);
      overflow-y: auto;
      overflow-x: hidden;
      padding: 18px;
      padding-bottom: 30px;
      gap: 12px;
      align-content: start;
      border-radius: 18px;
      background:
        radial-gradient(circle at top right, rgba(27, 103, 136, 0.16), transparent 28%),
        linear-gradient(180deg, rgba(10, 25, 36, 0.98), rgba(7, 17, 26, 0.99));
    }
    .chat-bubble {
      position: relative;
      max-width: min(76ch, 88%);
      min-width: 0;
      border-radius: 18px;
      padding: 12px 14px;
      font-size: 14px;
      line-height: 1.5;
      display: grid;
      gap: 10px;
      overflow: visible;
      box-shadow: 0 12px 24px rgba(0, 0, 0, 0.18);
    }
    .chat-bubble.has-copy-action {
      padding-right: 64px;
    }
    .chat-bubble.user {
      background: linear-gradient(135deg, rgba(24, 91, 123, 0.98), rgba(17, 66, 90, 0.98));
      border-color: #2aaad6;
    }
    .chat-bubble.mim {
      background: linear-gradient(135deg, rgba(11, 38, 54, 0.98), rgba(10, 28, 40, 0.98));
      border-color: #1b5d7e;
    }
    .chat-bubble.system {
      justify-self: center;
      max-width: 100%;
      background: rgba(111, 89, 30, 0.18);
      border-color: rgba(230, 183, 47, 0.4);
      color: #fde9a9;
    }
    .chat-bubble.execution {
      justify-self: stretch;
      max-width: 100%;
      background: linear-gradient(180deg, rgba(46, 54, 24, 0.86), rgba(28, 32, 16, 0.92));
      border-color: rgba(202, 177, 98, 0.44);
      color: #f7efc8;
      gap: 12px;
    }
    .bubble-meta {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #9cc9da;
    }
    .bubble-summary {
      font-size: 14px;
      line-height: 1.6;
      color: inherit;
      overflow: visible;
      user-select: text;
    }
    .bubble-text {
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.6;
      overflow: visible;
      user-select: text;
      padding-bottom: 2px;
    }
    .bubble-copy-btn {
      position: absolute;
      top: 10px;
      right: 10px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid rgba(115, 190, 219, 0.35);
      border-radius: 999px;
      background: rgba(8, 28, 40, 0.9);
      color: #d8f5ff;
      padding: 5px 10px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.03em;
      cursor: pointer;
      transition: background 120ms ease, border-color 120ms ease, transform 120ms ease;
    }
    .bubble-copy-btn:hover,
    .bubble-copy-btn:focus-visible {
      background: rgba(18, 62, 84, 0.96);
      border-color: rgba(115, 190, 219, 0.6);
      transform: translateY(-1px);
      outline: none;
    }
    .bubble-copy-btn svg {
      width: 12px;
      height: 12px;
      fill: currentColor;
      flex: 0 0 auto;
    }
    .bubble-copy-btn.copied {
      background: rgba(23, 110, 76, 0.92);
      border-color: rgba(116, 226, 172, 0.55);
    }
    .execution-details {
      border: 1px solid rgba(202, 177, 98, 0.22);
      border-radius: 14px;
      background: rgba(18, 20, 12, 0.58);
      overflow: hidden;
    }
    .execution-details > summary,
    .execution-raw-toggle > summary {
      cursor: pointer;
      list-style: none;
      padding: 10px 12px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #f6e7ab;
      background: rgba(73, 65, 30, 0.26);
    }
    .execution-details > summary::-webkit-details-marker,
    .execution-raw-toggle > summary::-webkit-details-marker {
      display: none;
    }
    .execution-scroll {
      max-height: 280px;
      overflow: auto;
      display: grid;
      gap: 12px;
      padding: 12px;
    }
    .execution-steps {
      display: grid;
      gap: 10px;
    }
    .execution-step {
      display: grid;
      gap: 8px;
      padding: 10px 12px;
      border: 1px solid rgba(202, 177, 98, 0.16);
      border-radius: 12px;
      background: rgba(14, 17, 12, 0.72);
    }
    .execution-step-header {
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #f6e7ab;
    }
    .execution-step-grid {
      display: grid;
      gap: 8px;
    }
    .execution-row {
      display: grid;
      gap: 4px;
    }
    .execution-row-label {
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #d6c37b;
    }
    .execution-row-value,
    .execution-note {
      font-size: 12px;
      line-height: 1.5;
      color: #f7efc8;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .execution-notes {
      display: grid;
      gap: 6px;
    }
    .execution-raw-toggle {
      border: 1px solid rgba(202, 177, 98, 0.16);
      border-radius: 12px;
      overflow: hidden;
      background: rgba(10, 12, 9, 0.7);
    }
    .execution-raw {
      margin: 0;
      max-height: 240px;
      overflow: auto;
      padding: 12px;
      font-size: 11px;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      color: #e4dcc0;
      background: rgba(5, 8, 6, 0.84);
    }
    .execution-footnote {
      font-size: 11px;
      color: #d5cda7;
    }
    .bubble-attachment {
      display: grid;
      gap: 8px;
      padding: 10px;
      border: 1px solid rgba(113, 188, 220, 0.26);
      border-radius: 14px;
      background: rgba(8, 24, 34, 0.62);
    }
    .bubble-attachment img {
      width: min(100%, 480px);
      border-radius: 12px;
      border: 1px solid #1c516d;
      display: block;
    }
    .bubble-attachment figcaption {
      font-size: 12px;
      color: #a8d4e5;
    }
    .empty-thread {
      display: grid;
      gap: 10px;
      align-content: center;
      justify-items: start;
      min-height: 100%;
      padding: 18px;
      border: 1px dashed #235772;
      border-radius: 16px;
      background: rgba(9, 29, 41, 0.78);
    }
    .empty-thread strong {
      font-size: 18px;
      color: #f2fbff;
    }
    .composer-shell {
      display: grid;
      gap: 12px;
      padding: 14px;
      border: 1px solid #1b4f69;
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(8, 28, 42, 0.98), rgba(9, 24, 35, 0.98));
    }
    .dropzone {
      border: 1px dashed #297394;
      border-radius: 14px;
      padding: 12px 14px;
      color: #a9d9ec;
      font-size: 13px;
      background: rgba(10, 32, 45, 0.56);
    }
    .dropzone.active {
      border-color: #36d2fb;
      background: rgba(15, 72, 95, 0.38);
    }
    .composer-input {
      min-height: 72px;
      resize: vertical;
      background: #0a1f2d;
      color: var(--text);
      border: 1px solid #1a4f68;
      border-radius: 14px;
      padding: 14px;
      font-size: 15px;
      font-family: inherit;
    }
    .composer-actions {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }
    .composer-action-group {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .composer-attach,
    .composer-mic,
    .composer-send,
    .secondary-action,
    .voice-primary-button,
    .thread-clear {
      border-radius: 14px;
      padding: 11px 14px;
      font-size: 14px;
    }
    .composer-mic {
      min-width: 148px;
      background: linear-gradient(135deg, rgba(19, 130, 95, 0.96), rgba(18, 91, 68, 0.96));
      border-color: #3fdba7;
    }
    .voice-primary-button {
      min-width: 180px;
      background: linear-gradient(135deg, rgba(18, 127, 163, 0.96), rgba(14, 84, 109, 0.96));
      border-color: #3bd7ff;
      font-weight: 600;
    }
    .voice-primary-button.error {
      background: linear-gradient(135deg, rgba(146, 83, 34, 0.96), rgba(112, 59, 19, 0.96));
      border-color: #f1a05d;
    }
    .thread-clear {
      background: rgba(13, 35, 48, 0.92);
    }
    .image-preview-wrap {
      display: grid;
      gap: 10px;
      border: 1px solid #1a4f68;
      border-radius: 14px;
      padding: 12px;
      background: rgba(8, 23, 34, 0.82);
    }
    .image-preview-wrap[hidden] {
      display: none;
    }
    .image-preview-card {
      display: flex;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }
    .image-preview-card img {
      width: min(180px, 100%);
      border-radius: 12px;
      border: 1px solid #1f5b79;
    }
    .sidebar-shell {
      display: grid;
      gap: 14px;
      position: sticky;
      top: 18px;
    }
    .sidebar-card {
      border: 1px solid #184a62;
      border-radius: 18px;
      padding: 14px;
      background: linear-gradient(180deg, rgba(8, 27, 40, 0.98), rgba(7, 20, 31, 0.98));
      display: grid;
      gap: 12px;
    }
    .sidebar-card h2,
    .secondary-section h2 {
      margin: 0;
      font-size: 15px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #a9d7e9;
    }
    .sidebar-keyline {
      display: grid;
      gap: 8px;
    }
    .sidebar-keyline strong,
    .status-metric strong {
      color: #f2fbff;
    }
    .sidebar-copy,
    .status-copy {
      font-size: 13px;
      color: var(--muted);
      line-height: 1.45;
    }
    .secondary-shell {
      display: grid;
      gap: 12px;
    }
    .secondary-tab-row {
      justify-content: space-between;
    }
    .secondary-panels {
      display: grid;
      gap: 12px;
    }
    .secondary-section {
      display: none;
      gap: 12px;
    }
    .secondary-section.active {
      display: grid;
    }
    body.operator-mode .debug-only {
      display: none !important;
    }
    body.debug-mode .operator-mode-note {
      display: none !important;
    }
    .status-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .status-tile {
      border: 1px solid #17485f;
      border-radius: 16px;
      padding: 14px;
      background: rgba(8, 27, 39, 0.88);
      display: grid;
      gap: 8px;
    }
    .status-tile span {
      font-size: 11px;
      color: #91c8db;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .status-tile strong {
      font-size: 14px;
      color: #f3fbff;
      line-height: 1.45;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .status-subtext {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.45;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .progress-meter {
      position: relative;
      width: 100%;
      height: 8px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(134, 185, 205, 0.16);
      border: 1px solid rgba(47, 126, 156, 0.35);
    }
    .progress-meter-fill {
      width: 0%;
      height: 100%;
      border-radius: inherit;
      background: linear-gradient(90deg, #3ec6ff, #89f0c7);
      transition: width 220ms ease;
    }
    .activity-truth-panel {
      margin-top: 14px;
      border: 1px solid rgba(49, 123, 151, 0.55);
      border-radius: 16px;
      background: rgba(8, 27, 39, 0.72);
      padding: 14px;
      display: grid;
      gap: 12px;
    }
    .activity-truth-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
    }
    .activity-truth-copy {
      display: grid;
      gap: 6px;
      min-width: 0;
    }
    .activity-truth-kicker {
      font-size: 11px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: #91c8db;
    }
    .activity-truth-head strong {
      font-size: 15px;
      color: #f3fbff;
      line-height: 1.4;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .activity-truth-meter {
      height: 10px;
    }
    .activity-truth-grid .status-tile {
      padding: 12px;
      gap: 6px;
    }
    .activity-truth-note {
      border-top: 1px solid rgba(49, 123, 151, 0.35);
      padding-top: 10px;
      display: grid;
      gap: 6px;
    }
    .activity-truth-note strong {
      font-size: 12px;
      color: #dff6ff;
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .program-queue-toolbar {
      margin-top: 12px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .program-queue-list {
      display: grid;
      gap: 10px;
      margin-top: 12px;
    }
    .program-queue-item {
      border: 1px solid rgba(49, 123, 151, 0.55);
      border-radius: 14px;
      padding: 12px;
      background: rgba(8, 27, 39, 0.72);
      display: grid;
      gap: 6px;
    }
    .program-queue-item.active {
      border-color: rgba(62, 198, 255, 0.85);
      box-shadow: inset 0 0 0 1px rgba(62, 198, 255, 0.18);
    }
    .program-queue-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      color: #f3fbff;
      font-size: 12px;
      line-height: 1.4;
    }
    .program-queue-heading strong {
      font-size: 13px;
      line-height: 1.4;
      color: #f3fbff;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .program-queue-order {
      color: #91c8db;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 11px;
      white-space: nowrap;
    }
    .program-queue-status {
      justify-self: start;
      border-radius: 999px;
      padding: 4px 8px;
      font-size: 11px;
      line-height: 1.2;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: #d7f7ff;
      background: rgba(33, 94, 116, 0.6);
    }
    .program-queue-status.active {
      background: rgba(62, 198, 255, 0.2);
      color: #8feaff;
    }
    .program-queue-status.completed {
      background: rgba(56, 161, 105, 0.22);
      color: #95f1bf;
    }
    .program-queue-status.blocked {
      background: rgba(196, 89, 17, 0.24);
      color: #ffc18a;
    }
    .program-queue-objective {
      font-size: 12px;
      color: var(--muted);
      line-height: 1.45;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .diagnostics-grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, 360px);
      gap: 12px;
    }
    .media-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
      gap: 12px;
    }
    .media-card {
      border: 1px solid #16485d;
      border-radius: 16px;
      overflow: hidden;
      background: rgba(8, 24, 34, 0.92);
      display: grid;
      gap: 0;
    }
    .media-card img {
      width: 100%;
      aspect-ratio: 16 / 11;
      object-fit: cover;
      display: block;
    }
    .media-card figcaption {
      padding: 10px 12px 12px;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.45;
    }
    .visually-hidden {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }
    @media (max-width: 720px) {
      body {
        padding: 12px;
      }
      .topbar {
        padding-right: 56px;
      }
      .layout-grid,
      .diagnostics-grid,
      .system-activity-banner-grid,
      .system-relation-grid,
      .status-grid {
        grid-template-columns: 1fr;
      }
      .sidebar-shell {
        position: static;
      }
      .chat-log {
        height: 54vh;
        min-height: 320px;
      }
      .chat-bubble {
        max-width: 100%;
      }
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
    <div hidden></div>
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

  <div class="app-shell">
    <div class="topbar">
      <div class="topbar-left">
        <div class="brand-stack">
          <div class="surface-kicker surface-kicker-nav">
            <a class="console-nav-link utility" href="/">
              <span>Public Home</span>
            </a>
            <a class="console-nav-link active" href="/mim">
              <span id="mimConsoleLight" class="console-nav-light"></span>
              <span>MIM Primary Operator Surface</span>
            </a>
            <a class="console-nav-link" href="/tod">
              <span id="todConsoleLight" class="console-nav-light"></span>
              <span>TOD Console</span>
            </a>
            <a class="console-nav-link utility" href="/chat">
              <span>Direct Chat</span>
            </a>
            <a class="console-nav-link utility" href="/mim/logout">
              <span>Logout</span>
            </a>
          </div>
          <h1 id="mimIcon" class="mim-icon">MIM</h1>
        </div>
        <div id="buildTag" class="status-chip subtle">Build: loading...</div>
      </div>
      <div class="topbar-right">
        <div class="mode-toggle" aria-label="Interface mode selector">
          <button id="operatorModeBtn" class="mode-chip active" type="button">Operator</button>
          <button id="debugModeBtn" class="mode-chip" type="button">Debug</button>
        </div>
        <div id="voiceAvailabilityChip" class="status-chip strong">Voice checking…</div>
        <div id="connectionChip" class="status-chip subtle">Runtime syncing…</div>
        <div id="initiativeChip" class="status-chip subtle">Initiative idle</div>
        <button id="settingsBtn" class="icon-btn" title="MIM settings" aria-label="MIM settings">⚙</button>
      </div>
    </div>

    <div id="textChatPanel" class="panel thread-shell primary-chat-panel">
      <div class="thread-card">
        <div class="thread-header">
          <div class="thread-title">
            <strong>Conversation</strong>
            <span>One persistent primary thread across refreshes and clients.</span>
          </div>
          <div class="thread-tools">
            <div id="threadStatusChip" class="status-chip subtle">Thread loading…</div>
            <button id="chatClearBtn" class="thread-clear" type="button">Clear View</button>
          </div>
        </div>

        <div id="chatLog" class="chat-log" aria-live="polite" aria-label="Primary MIM conversation thread"></div>

        <div id="imagePreviewWrap" class="image-preview-wrap" hidden>
          <div class="image-preview-card">
            <img id="imagePreviewImg" alt="Selected image preview" />
            <div class="sidebar-keyline">
              <strong id="imagePreviewName">Selected image</strong>
              <div id="imagePreviewMeta" class="sidebar-copy">Add an optional prompt, then send.</div>
              <button id="imageRemoveBtn" class="secondary-action" type="button">Remove image</button>
            </div>
          </div>
        </div>

        <div class="composer-shell">
          <div id="chatDropzone" class="dropzone">Drop a screenshot here, or use Image to attach png, jpg, jpeg, or webp.</div>
          <label class="visually-hidden" for="chatInput">Message MIM</label>
          <textarea id="chatInput" class="composer-input" placeholder="Message MIM" rows="3"></textarea>
          <div class="composer-actions">
            <div class="composer-action-group">
              <input id="imageUploadInput" type="file" accept="image/png,image/jpeg,image/webp" hidden />
              <button id="imageUploadBtn" class="composer-attach" type="button">Image</button>
              <button id="chatMicBtn" class="composer-mic" type="button">Turn Listener On</button>
            </div>
            <div class="composer-action-group">
              <button id="chatSendBtn" class="composer-send" type="button">Send</button>
            </div>
          </div>
          <div class="composer-meta">
            <div id="inquiry" class="status-chip subtle"></div>
            <div id="voiceHintChip" class="status-chip subtle">Voice replies stay in this thread.</div>
          </div>
        </div>
      </div>
    </div>

    <div class="system-activity-banner">
      <div class="system-activity-banner-head">
        <div class="system-activity-banner-copy">
          <div class="surface-kicker">System Activity Status</div>
          <strong id="systemActivityHeadlineText">Loading…</strong>
          <div id="systemActivitySummaryText" class="system-activity-banner-summary">Checking whether MIM is actually progressing work right now…</div>
        </div>
        <div id="systemActivityBadge" class="status-chip subtle" data-tone="warn">Checking…</div>
      </div>
      <div class="progress-meter activity-truth-meter" aria-hidden="true">
        <div id="systemActivityFill" class="progress-meter-fill"></div>
      </div>
      <div class="system-activity-banner-grid">
        <div class="status-tile">
          <span>Last Activity</span>
          <strong id="systemLastActivityText">Loading…</strong>
          <div id="systemLastActivityDetailText" class="status-subtext">Checking latest MIM or TOD signal…</div>
        </div>
        <div class="status-tile">
          <span>Last Task Progress</span>
          <strong id="systemLastTaskProgressText">Loading…</strong>
          <div id="systemLastTaskProgressDetailText" class="status-subtext">Checking dispatch and feedback timestamps…</div>
        </div>
        <div class="status-tile">
          <span>Execution Allowed</span>
          <strong id="systemExecutionAllowedText">Loading…</strong>
          <div id="systemExecutionAllowedDetailText" class="status-subtext">Checking readiness policy…</div>
        </div>
        <div class="status-tile">
          <span>Staleness State</span>
          <strong id="systemStalenessText">Loading…</strong>
          <div id="systemStalenessDetailText" class="status-subtext">Checking heartbeat and progress age…</div>
        </div>
      </div>
      <div class="system-relation-grid">
        <div class="status-tile">
          <span>Objective Alignment</span>
          <strong id="relationObjectiveText">Loading…</strong>
          <div id="relationObjectiveDetailText" class="status-subtext">Checking MIM and TOD objective agreement…</div>
        </div>
        <div class="status-tile">
          <span>Bridge Health</span>
          <strong id="relationBridgeText">Loading…</strong>
          <div id="relationBridgeDetailText" class="status-subtext">Checking handoff bridge health…</div>
        </div>
        <div class="status-tile">
          <span>Execution Flow</span>
          <strong id="relationFlowText">Loading…</strong>
          <div id="relationFlowDetailText" class="status-subtext">Checking whether work is flowing or blocked…</div>
        </div>
        <div class="status-tile">
          <span>Last Handoff</span>
          <strong id="relationHandoffText">Loading…</strong>
          <div id="relationHandoffDetailText" class="status-subtext">Checking last request or bridge receive…</div>
        </div>
        <div class="status-tile">
          <span>Last Feedback</span>
          <strong id="relationFeedbackText">Loading…</strong>
          <div id="relationFeedbackDetailText" class="status-subtext">Checking last TOD feedback or completion…</div>
        </div>
      </div>
      <div class="activity-truth-note">
        <strong>Stall Reason</strong>
        <div id="systemStallReasonText" class="status-subtext">Checking for stall evidence…</div>
      </div>
    </div>

    <div class="layout-grid">
      <div class="chat-surface">
        <div class="panel chat-hero">
          <div class="chat-summary-row">
            <div id="contextChip" class="context-chip">Loading current objective context…</div>
            <div class="quick-actions">
              <button class="quick-action primary" type="button" data-quick-action="continue_work" data-quick-message="Continue the current initiative and summarize progress.">Continue Work</button>
              <button class="quick-action" type="button" data-quick-action="unstick_mim" data-quick-message="If the current initiative is stale or stuck, unstick MIM now. Use TOD and the available broker or OpenAI path to continue the active objective, then summarize the blocker and what you did.">Unstick MIM</button>
              <button class="quick-action" type="button" data-quick-action="smart_recovery" data-quick-message="Inspect current runtime health. If MIM, TOD coordination, or the active initiative is stale or unhealthy, recover it now and summarize the repair action.">Smart Recovery</button>
              <button class="quick-action" type="button" data-quick-action="force_tod_help" data-quick-message="Escalate to TOD now. Request immediate external help for the current initiative, include the current blocker, the active objective, and the next bounded action needed.">Force TOD Help</button>
              <button class="quick-action" type="button" data-quick-action="show_blockers" data-quick-message="Show current blockers and next task.">Show Blockers</button>
              <button class="quick-action" type="button" data-quick-action="check_tod_status" data-quick-message="Check TOD status and report it back.">Check TOD Status</button>
              <button class="quick-action" type="button" data-quick-action="review_latest_image" data-quick-message="Summarize the latest image or visual context in this thread.">Review Latest Image</button>
            </div>
          </div>
          <div class="voice-primary-row">
            <button id="listenBtn" class="voice-primary-button" type="button">Turn Listener On</button>
            <div id="voiceStateChip" class="status-chip subtle" data-tone="warn">Voice: checking</div>
            <div id="status" class="status-chip subtle">Ready for full-time listening.</div>
            <div id="micEvent" class="status-chip subtle">Recent voice event: waiting…</div>
            <label class="composer-toggle" for="autoListenToggle">
              <input id="autoListenToggle" type="checkbox" />
              Full-time listener
            </label>
          </div>
          <div class="media-self-test-card">
            <div class="media-self-test-header">
              <div class="thread-title">
                <strong>Media Self-Test</strong>
                <span>Live browser and runtime checks for remote mic and camera.</span>
              </div>
              <div id="selfTestSummaryChip" class="status-chip subtle" data-tone="warn">Self-test pending</div>
            </div>
            <div class="media-self-test-actions">
              <button id="selfTestRunBtn" class="secondary-action" type="button">Run Self-Test</button>
              <button id="selfTestToggleListenerBtn" class="secondary-action" type="button">Turn Listener On</button>
              <div id="selfTestTimestamp" class="status-chip subtle">Awaiting first self-test.</div>
            </div>
            <div class="media-self-test-grid">
              <div class="media-self-test-item">
                <div class="media-self-test-label">Secure Origin</div>
                <div id="selfTestSecureValue" class="media-self-test-value">Checking…</div>
              </div>
              <div class="media-self-test-item">
                <div class="media-self-test-label">Browser Media API</div>
                <div id="selfTestMediaApiValue" class="media-self-test-value">Checking…</div>
              </div>
              <div class="media-self-test-item">
                <div class="media-self-test-label">Microphone Permission</div>
                <div id="selfTestMicPermissionValue" class="media-self-test-value">Checking…</div>
              </div>
              <div class="media-self-test-item">
                <div class="media-self-test-label">Microphone Device</div>
                <div id="selfTestMicDeviceValue" class="media-self-test-value">Checking…</div>
              </div>
              <div class="media-self-test-item">
                <div class="media-self-test-label">Listener Mode</div>
                <div id="selfTestListenerValue" class="media-self-test-value">Checking…</div>
              </div>
              <div class="media-self-test-item">
                <div class="media-self-test-label">Last Voice Activity</div>
                <div id="selfTestMicActivityValue" class="media-self-test-value">Checking…</div>
              </div>
              <div class="media-self-test-item">
                <div class="media-self-test-label">Camera State</div>
                <div id="selfTestCameraValue" class="media-self-test-value">Checking…</div>
              </div>
              <div class="media-self-test-item">
                <div class="media-self-test-label">Backend Sync</div>
                <div id="selfTestBackendValue" class="media-self-test-value">Checking…</div>
              </div>
            </div>
          </div>
        </div>

      </div>

      <div class="sidebar-shell">
        <div class="sidebar-card">
          <h2>Current Status</h2>
          <div class="status-grid">
            <div class="status-tile">
              <span>Objective</span>
              <strong id="activeObjectiveText">Loading…</strong>
            </div>
            <div class="status-tile">
              <span>Active Task</span>
              <strong id="activeTaskText">Loading…</strong>
            </div>
            <div class="status-tile">
              <span>Activity</span>
              <strong id="activityStateText">Loading…</strong>
              <div id="activityStateDetailText" class="status-subtext">Checking initiative activity…</div>
            </div>
            <div class="status-tile">
              <span>Progress</span>
              <strong id="progressText">Loading…</strong>
              <div class="progress-meter" aria-hidden="true">
                <div id="progressFill" class="progress-meter-fill"></div>
              </div>
              <div id="progressDetailText" class="status-subtext">Checking bounded task progress…</div>
            </div>
            <div class="status-tile">
              <span>Next Task</span>
              <strong id="nextTaskText">Loading…</strong>
            </div>
            <div class="status-tile">
              <span>Blockers</span>
              <strong id="blockerCountText">0 visible</strong>
            </div>
          </div>
          <div class="sidebar-copy" id="initiativeSummaryText">Initiative state loading…</div>
        </div>

        <div class="sidebar-card">
          <h2>Program Queue</h2>
          <div id="programQueueSummaryText" class="sidebar-copy">Checking ordered project progression…</div>
          <div class="program-queue-toolbar">
            <div id="programQueueMetaText" class="status-subtext">Preparing the active and nearby steps…</div>
            <button id="programQueueToggleBtn" class="secondary-action" type="button">Show all</button>
          </div>
          <div id="programQueueList" class="program-queue-list"></div>
        </div>

        <div class="sidebar-card">
          <h2>Voice</h2>
          <div id="voiceAvailabilityText" class="sidebar-copy">Checking microphone and speech availability…</div>
          <div class="sidebar-list">
            <div id="micDiag" class="status-chip subtle">Mic: detecting devices…</div>
            <div id="camera" class="status-chip subtle">Camera: waiting for observations</div>
          </div>
        </div>
      </div>
    </div>

    <div class="panel secondary-shell">
      <div class="secondary-tab-row">
        <div class="sidebar-keyline">
          <h2>Secondary Views</h2>
          <div class="sidebar-copy">Status and diagnostics stay available without dominating the conversation.</div>
        </div>
        <div class="secondary-tab-row">
          <button id="secondaryTabStatus" class="tab-chip active" type="button" data-tab="status">Status</button>
          <button id="secondaryTabReasoning" class="tab-chip" type="button" data-tab="reasoning">Reasoning</button>
          <button id="secondaryTabDiagnostics" class="tab-chip" type="button" data-tab="diagnostics">Diagnostics</button>
          <button id="secondaryTabMedia" class="tab-chip" type="button" data-tab="media">Media</button>
        </div>
      </div>

      <div class="secondary-panels">
        <div id="secondaryPanelStatus" class="secondary-section active">
          <div class="status-grid">
            <div class="status-tile">
              <span>Recent Input</span>
              <strong id="recentInputText">Waiting for input…</strong>
            </div>
            <div class="status-tile">
              <span>Open Question</span>
              <strong id="openQuestionText">None</strong>
            </div>
            <div class="status-tile">
              <span>Memory Hint</span>
              <strong id="memoryHintText">None</strong>
            </div>
            <div class="status-tile">
              <span>Runtime Health</span>
              <strong id="runtimeHealthText">Loading…</strong>
            </div>
          </div>
          <div id="objectMemoryPanel" class="object-memory-panel" hidden>
            <div class="object-memory-header">
              <div class="object-memory-title">Object Memory</div>
              <div class="object-memory-caption">Live camera continuity</div>
            </div>
            <ul id="objectMemoryList" class="object-memory-list"></ul>
          </div>
        </div>

        <div id="secondaryPanelReasoning" class="secondary-section">
          <div id="systemReasoningPanel" class="object-memory-panel" hidden>
            <div class="object-memory-header">
              <div class="object-memory-title">System Reasoning</div>
              <div class="object-memory-caption">Operator-visible decision context</div>
            </div>
            <div id="systemReasoningSummary" class="object-memory-note"></div>
            <ul id="systemReasoningList" class="object-memory-list"></ul>
          </div>
        </div>

        <div id="secondaryPanelDiagnostics" class="secondary-section">
          <div class="sidebar-card operator-mode-note">
            <h2>Diagnostics Stay Secondary</h2>
            <div class="sidebar-copy">Operator mode keeps raw microphone, camera, and runtime controls out of the main workflow. Switch to Debug mode only when you need low-level troubleshooting.</div>
            <div class="sidebar-list">
              <button id="openDebugModeBtn" class="secondary-action" type="button">Switch To Debug Mode</button>
            </div>
          </div>
          <div class="diagnostics-grid debug-only">
            <div class="sidebar-card">
              <h2>Voice And Runtime</h2>
              <div class="wave-wrap">
                <div id="wave" class="wave"></div>
              </div>
              <div id="micDebug" class="debug-log">Mic debug: starting...</div>
            </div>
            <div class="sidebar-card">
              <h2>Operator Controls</h2>
              <div class="controls">
                <input id="sayInput" placeholder="Type what MIM should say" value="Hello, I am MIM." />
                <button id="speakBtn" class="secondary-action" type="button">Speak</button>
                <button id="cameraBtn" class="secondary-action" type="button">Send Camera Event</button>
              </div>
              <div class="controls" style="grid-template-columns: 1fr; margin-top: 0;">
                <input id="cameraInput" placeholder="Who is in view? (e.g. unknown, person, alice)" value="unknown" />
              </div>
            </div>
          </div>
        </div>

        <div id="secondaryPanelMedia" class="secondary-section">
          <div id="mediaGrid" class="media-grid"></div>
        </div>
      </div>
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
    const chatMicBtn = document.getElementById('chatMicBtn');
    const imageUploadBtn = document.getElementById('imageUploadBtn');
    const imageUploadInput = document.getElementById('imageUploadInput');
    const imagePreviewWrap = document.getElementById('imagePreviewWrap');
    const imagePreviewImg = document.getElementById('imagePreviewImg');
    const imagePreviewName = document.getElementById('imagePreviewName');
    const imagePreviewMeta = document.getElementById('imagePreviewMeta');
    const imageRemoveBtn = document.getElementById('imageRemoveBtn');
    const chatDropzone = document.getElementById('chatDropzone');
    const autoListenToggle = document.getElementById('autoListenToggle');
    const voiceAvailabilityChip = document.getElementById('voiceAvailabilityChip');
    const voiceStateChip = document.getElementById('voiceStateChip');
    const connectionChip = document.getElementById('connectionChip');
    const initiativeChip = document.getElementById('initiativeChip');
    const contextChip = document.getElementById('contextChip');
    const threadStatusChip = document.getElementById('threadStatusChip');
    const voiceHintChip = document.getElementById('voiceHintChip');
    const activeObjectiveText = document.getElementById('activeObjectiveText');
    const activeTaskText = document.getElementById('activeTaskText');
    const activityStateText = document.getElementById('activityStateText');
    const activityStateDetailText = document.getElementById('activityStateDetailText');
    const progressText = document.getElementById('progressText');
    const progressDetailText = document.getElementById('progressDetailText');
    const progressFill = document.getElementById('progressFill');
    const nextTaskText = document.getElementById('nextTaskText');
    const blockerCountText = document.getElementById('blockerCountText');
    const initiativeSummaryText = document.getElementById('initiativeSummaryText');
    const systemActivityHeadlineText = document.getElementById('systemActivityHeadlineText');
    const systemActivityBadge = document.getElementById('systemActivityBadge');
    const systemActivitySummaryText = document.getElementById('systemActivitySummaryText');
    const systemActivityFill = document.getElementById('systemActivityFill');
    const systemLastActivityText = document.getElementById('systemLastActivityText');
    const systemLastActivityDetailText = document.getElementById('systemLastActivityDetailText');
    const systemLastTaskProgressText = document.getElementById('systemLastTaskProgressText');
    const systemLastTaskProgressDetailText = document.getElementById('systemLastTaskProgressDetailText');
    const systemExecutionAllowedText = document.getElementById('systemExecutionAllowedText');
    const systemExecutionAllowedDetailText = document.getElementById('systemExecutionAllowedDetailText');
    const systemStalenessText = document.getElementById('systemStalenessText');
    const systemStalenessDetailText = document.getElementById('systemStalenessDetailText');
    const relationObjectiveText = document.getElementById('relationObjectiveText');
    const relationObjectiveDetailText = document.getElementById('relationObjectiveDetailText');
    const relationBridgeText = document.getElementById('relationBridgeText');
    const relationBridgeDetailText = document.getElementById('relationBridgeDetailText');
    const relationFlowText = document.getElementById('relationFlowText');
    const relationFlowDetailText = document.getElementById('relationFlowDetailText');
    const relationHandoffText = document.getElementById('relationHandoffText');
    const relationHandoffDetailText = document.getElementById('relationHandoffDetailText');
    const relationFeedbackText = document.getElementById('relationFeedbackText');
    const relationFeedbackDetailText = document.getElementById('relationFeedbackDetailText');
    const systemStallReasonText = document.getElementById('systemStallReasonText');
    const programQueueSummaryText = document.getElementById('programQueueSummaryText');
    const programQueueMetaText = document.getElementById('programQueueMetaText');
    const programQueueToggleBtn = document.getElementById('programQueueToggleBtn');
    const programQueueList = document.getElementById('programQueueList');
    const voiceAvailabilityText = document.getElementById('voiceAvailabilityText');
    const recentInputText = document.getElementById('recentInputText');
    const openQuestionText = document.getElementById('openQuestionText');
    const memoryHintText = document.getElementById('memoryHintText');
    const runtimeHealthText = document.getElementById('runtimeHealthText');
    const mediaGrid = document.getElementById('mediaGrid');
    const selfTestSummaryChip = document.getElementById('selfTestSummaryChip');
    const selfTestRunBtn = document.getElementById('selfTestRunBtn');
    const selfTestToggleListenerBtn = document.getElementById('selfTestToggleListenerBtn');
    const selfTestTimestamp = document.getElementById('selfTestTimestamp');
    const selfTestSecureValue = document.getElementById('selfTestSecureValue');
    const selfTestMediaApiValue = document.getElementById('selfTestMediaApiValue');
    const selfTestMicPermissionValue = document.getElementById('selfTestMicPermissionValue');
    const selfTestMicDeviceValue = document.getElementById('selfTestMicDeviceValue');
    const selfTestListenerValue = document.getElementById('selfTestListenerValue');
    const selfTestMicActivityValue = document.getElementById('selfTestMicActivityValue');
    const selfTestCameraValue = document.getElementById('selfTestCameraValue');
    const selfTestBackendValue = document.getElementById('selfTestBackendValue');
    const secondaryTabs = Array.from(document.querySelectorAll('[data-tab]'));
    const quickActionButtons = Array.from(document.querySelectorAll('[data-quick-message]'));
    const mimIcon = document.getElementById('mimIcon');
    const mimConsoleLight = document.getElementById('mimConsoleLight');
    const todConsoleLight = document.getElementById('todConsoleLight');
    const operatorModeBtn = document.getElementById('operatorModeBtn');
    const debugModeBtn = document.getElementById('debugModeBtn');
    const openDebugModeBtn = document.getElementById('openDebugModeBtn');
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
        micEventEl.textContent = `Recent voice event: js-error:${msg}`;
      }
      statusEl.textContent = `UI error: ${msg}`;
      if (voiceStateChip) {
        voiceStateChip.textContent = 'Voice: error';
        voiceStateChip.dataset.tone = 'error';
      }
    });

    const AUTO_LISTEN_STORAGE_KEY = 'mim_auto_listen_enabled';
    let micAutoMode = localStorage.getItem(AUTO_LISTEN_STORAGE_KEY) === '1';
    let micListening = false;
    let recognition = null;
    let selfTestLastRunAt = 0;
    let selfTestLastSummary = 'Self-test pending';

    const preloadedChatThread = __MIM_PRELOADED_CHAT_THREAD__;
    let chatThreadMessages = Array.isArray(preloadedChatThread.messages) ? preloadedChatThread.messages : [];
    let chatLocallyCleared = false;
    let chatLocalClearCutoffIso = '';
    let chatLocalClearNotice = null;
    let lastRenderedChatSignature = '';
    let selectedComposerImage = null;
    let activeSecondaryTab = 'status';
    let uiMode = localStorage.getItem('mim_ui_mode') || 'operator';
    let showFullProgramQueue = false;

    function safeText(value, fallback = '') {
      const text = String(value || '').trim();
      return text || fallback;
    }

    function setTextWithTitle(element, text, fallback = '') {
      if (!element) return;
      const resolved = safeText(text, fallback);
      element.textContent = resolved;
      element.title = resolved;
    }

    function resolveInitiativeLabel(entry, fallback = '') {
      if (!entry || typeof entry !== 'object') return safeText(fallback);
      return safeText(entry.display_title || entry.title || entry.scope || entry.description, fallback);
    }

    function formatAgeSummary(secondsValue) {
      const seconds = Number(secondsValue);
      if (!Number.isFinite(seconds) || seconds < 1) return '';
      if (seconds < 90) return `${Math.round(seconds)}s`;
      const minutes = Math.round(seconds / 60);
      if (minutes < 90) return `${minutes}m`;
      const hours = Math.round(minutes / 60);
      return `${hours}h`;
    }

    function formatActivityTimestamp(value, emptyLabel = 'No recent signal') {
      if (!value) return emptyLabel;
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return emptyLabel;
      const ageSeconds = Math.max(0, (Date.now() - parsed.getTime()) / 1000);
      const ageLabel = formatAgeSummary(ageSeconds);
      const recentLabel = ageSeconds < 86400
        ? parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        : parsed.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
      return ageLabel ? `${recentLabel} (${ageLabel} ago)` : recentLabel;
    }

    function activityToneFromState(state, shouldBeWorking) {
      const normalized = safeText(state).toLowerCase();
      if (normalized === 'active' || normalized === 'working') return 'active';
      if (normalized === 'idle' && !shouldBeWorking) return 'ready';
      if (normalized === 'warning' || normalized === 'recovering') return 'warn';
      if (normalized === 'stale' || normalized === 'frozen' || normalized === 'stalled') return 'error';
      if (normalized === 'idle' && shouldBeWorking) return 'warn';
      return 'warn';
    }

    function renderSystemActivityTruth(systemActivity = {}) {
      const state = safeText(systemActivity.status_code || systemActivity.state, 'idle').toLowerCase();
      const headline = safeText(systemActivity.headline || systemActivity.label, 'IDLE - healthy, no live task right now');
      const summary = safeText(systemActivity.summary, 'No active work is currently required.');
      const shouldBeWorking = Boolean(systemActivity.should_be_working);
      const lastActivity = safeText(systemActivity.last_activity_at || systemActivity.mim_last_activity_at || systemActivity.tod_last_activity_at);
      const lastTaskProgress = safeText(systemActivity.last_task_progress_at);
      const executionAllowed = Boolean(systemActivity.execution_allowed);
      const executionAllowedLabel = executionAllowed ? 'Yes' : 'No';
      const executionAllowedDetail = safeText(systemActivity.execution_allowed_reason, executionAllowed ? 'Execution is allowed.' : 'Execution is blocked.');
      const stalenessLabel = safeText(systemActivity.staleness_label, 'FRESH');
      const stalenessDetail = safeText(systemActivity.staleness_detail, 'No current staleness evidence.');
      const relation = (systemActivity.relation && typeof systemActivity.relation === 'object') ? systemActivity.relation : {};
      const relationObjective = safeText(relation.objective_alignment, 'Aligned');
      const relationObjectiveDetail = safeText(relation.objective_alignment_detail, 'MIM and TOD agree on the active objective.');
      const relationBridge = safeText(relation.bridge_health, 'Healthy');
      const relationBridgeDetail = safeText(relation.summary, 'The MIM↔TOD bridge looks healthy.');
      const relationFlow = safeText(relation.execution_flow, 'Flowing');
      const relationFlowDetail = shouldBeWorking
        ? 'Work is expected to move through the current objective.'
        : 'No live execution flow is required right now.';
      const relationHandoffAt = safeText(relation.last_handoff_at);
      const relationFeedbackAt = safeText(relation.last_feedback_at);
      const stallReason = safeText(systemActivity.stall_reason, 'No current stall evidence.');
      const meterPercent = Math.max(0, Math.min(100, Number(systemActivity.meter_percent || 0)));
      const tone = safeText(systemActivity.tone, activityToneFromState(state, shouldBeWorking));

      setTextWithTitle(systemActivityHeadlineText, headline, 'IDLE - healthy, no live task right now');
      if (systemActivityBadge) {
        const badgeText = safeText(systemActivity.status_label, safeText(systemActivity.label, 'IDLE'));
        systemActivityBadge.textContent = shouldBeWorking ? `${badgeText} · should move` : badgeText;
        systemActivityBadge.title = summary;
        systemActivityBadge.setAttribute('data-tone', tone);
      }
      if (systemActivitySummaryText) {
        systemActivitySummaryText.textContent = summary;
        systemActivitySummaryText.title = summary;
      }
      if (systemActivityFill) {
        systemActivityFill.style.width = `${meterPercent}%`;
      }

      setTextWithTitle(systemLastActivityText, formatActivityTimestamp(lastActivity), 'No recent signal');
      if (systemLastActivityDetailText) {
        const detail = safeText(systemActivity.heartbeat_detail, 'Latest MIM or TOD activity heartbeat.');
        systemLastActivityDetailText.textContent = detail;
        systemLastActivityDetailText.title = detail;
      }

      setTextWithTitle(systemLastTaskProgressText, formatActivityTimestamp(lastTaskProgress), 'No recent progress');
      if (systemLastTaskProgressDetailText) {
        const detail = safeText(
          systemActivity.last_task_progress_age_seconds ? `Last bounded progress arrived about ${formatAgeSummary(systemActivity.last_task_progress_age_seconds)} ago.` : '',
          'Latest request, dispatch, or feedback timestamp for tracked work.',
        );
        systemLastTaskProgressDetailText.textContent = detail;
        systemLastTaskProgressDetailText.title = detail;
      }

      setTextWithTitle(systemExecutionAllowedText, executionAllowedLabel, 'No');
      if (systemExecutionAllowedDetailText) {
        systemExecutionAllowedDetailText.textContent = executionAllowedDetail;
        systemExecutionAllowedDetailText.title = executionAllowedDetail;
      }

      setTextWithTitle(systemStalenessText, stalenessLabel, 'FRESH');
      if (systemStalenessDetailText) {
        systemStalenessDetailText.textContent = stalenessDetail;
        systemStalenessDetailText.title = stalenessDetail;
      }

      setTextWithTitle(relationObjectiveText, relationObjective, 'Aligned');
      if (relationObjectiveDetailText) {
        relationObjectiveDetailText.textContent = relationObjectiveDetail;
        relationObjectiveDetailText.title = relationObjectiveDetail;
      }

      setTextWithTitle(relationBridgeText, relationBridge, 'Healthy');
      if (relationBridgeDetailText) {
        relationBridgeDetailText.textContent = relationBridgeDetail;
        relationBridgeDetailText.title = relationBridgeDetail;
      }

      setTextWithTitle(relationFlowText, relationFlow, 'Flowing');
      if (relationFlowDetailText) {
        relationFlowDetailText.textContent = relationFlowDetail;
        relationFlowDetailText.title = relationFlowDetail;
      }

      setTextWithTitle(relationHandoffText, formatActivityTimestamp(relationHandoffAt), 'No recent handoff');
      if (relationHandoffDetailText) {
        const detail = safeText(systemActivity.should_be_working_reason, 'Latest bridge request or handoff timestamp.');
        relationHandoffDetailText.textContent = detail;
        relationHandoffDetailText.title = detail;
      }

      setTextWithTitle(relationFeedbackText, formatActivityTimestamp(relationFeedbackAt), 'No recent feedback');
      if (relationFeedbackDetailText) {
        const detail = safeText(systemActivity.recovery_summary, 'Latest TOD feedback or completion evidence.');
        relationFeedbackDetailText.textContent = detail;
        relationFeedbackDetailText.title = detail;
      }

      if (systemStallReasonText) {
        systemStallReasonText.textContent = stallReason;
        systemStallReasonText.title = stallReason;
      }
    }

    function buildQuickActionMessage(button) {
      const action = safeText(button?.dataset?.quickAction).toLowerCase();
      const fallback = safeText(button?.dataset?.quickMessage);
      const state = latestUiState && typeof latestUiState === 'object' ? latestUiState : {};
      const systemActivity = state && typeof state.system_activity === 'object' ? state.system_activity : {};
      const initiative = state && typeof state.initiative_driver === 'object' ? state.initiative_driver : {};
      const activeObjective = resolveInitiativeLabel(initiative.active_objective, 'the current objective');
      const nextTask = resolveInitiativeLabel(initiative.next_task, 'the next bounded task');
      const stallReason = safeText(systemActivity.stall_reason, 'no explicit stall reason recorded');
      const executionAllowedReason = safeText(systemActivity.execution_allowed_reason, 'execution readiness needs inspection');
      const relation = (systemActivity.relation && typeof systemActivity.relation === 'object') ? systemActivity.relation : {};
      const relationSummary = safeText(relation.summary, 'MIM and TOD relationship needs inspection.');

      if (action === 'continue_work') {
        if (safeText(systemActivity.status_code) === 'active') {
          return `Continue executing ${activeObjective}. Summarize the latest progress signal, what task is active now, and the next bounded step.`;
        }
        if (Boolean(systemActivity.should_be_working)) {
          return `Resume ${activeObjective} now. If no task is live, create or dispatch the next bounded task (${nextTask}). Explain the blocker you cleared and summarize progress.`;
        }
      }

      if (action === 'unstick_mim') {
        return `Unstick MIM for ${activeObjective}. Diagnose why the system reports ${safeText(systemActivity.status_label, 'WARNING')} and use the available TOD, broker, or implementation path to clear this blocker: ${stallReason}. Then summarize the repair and the next bounded step.`;
      }

      if (action === 'smart_recovery') {
        if (!Boolean(systemActivity.execution_allowed)) {
          return `Smart recovery for ${activeObjective}: execution is blocked. Refresh execution readiness, repair the stale or blocked gate (${executionAllowedReason}), then resume the next bounded task ${nextTask}. If the gate stays blocked, trigger the corrective branch and summarize what changed.`;
        }
        if (safeText(systemActivity.status_code) === 'idle' && Boolean(systemActivity.should_be_working)) {
          return `Smart recovery for ${activeObjective}: the system is idle but should be working. Resume or create the next bounded task (${nextTask}), refresh handoff state if needed, and summarize the action taken.`;
        }
        if (['warning', 'stale', 'frozen'].includes(safeText(systemActivity.status_code).toLowerCase())) {
          return `Smart recovery for ${activeObjective}: inspect runtime health, bridge health, and execution flow. Clear this issue first: ${stallReason}. If repeated idle or stale behavior continues, trigger the corrective branch or external assist, then summarize the repair.`;
        }
      }

      if (action === 'force_tod_help') {
        return `Escalate to TOD now for ${activeObjective}. Include this relation summary: ${relationSummary} Include the current blocker (${stallReason}) and request immediate external help for the next bounded action ${nextTask}.`;
      }

      return fallback;
    }

    const primaryThreadKey = safeText(preloadedChatThread.primary_thread, 'primary_operator');

    function setUiMode(mode) {
      uiMode = mode === 'debug' ? 'debug' : 'operator';
      localStorage.setItem('mim_ui_mode', uiMode);
      document.body.classList.toggle('operator-mode', uiMode === 'operator');
      document.body.classList.toggle('debug-mode', uiMode === 'debug');
      if (operatorModeBtn) operatorModeBtn.classList.toggle('active', uiMode === 'operator');
      if (debugModeBtn) debugModeBtn.classList.toggle('active', uiMode === 'debug');
      if (settingsPanel && uiMode === 'operator') {
        settingsPanel.classList.remove('open');
      }
    }

    function updateVoiceStateUi() {
      if (!voiceStateChip) return;
      const voiceReady = Boolean(window.SpeechRecognition || window.webkitSpeechRecognition || window.speechSynthesis);
      let label = 'Voice: idle';
      let tone = 'warn';
      if (!voiceReady) {
        label = 'Voice: unavailable';
        tone = 'error';
      } else if (!window.isSecureContext && !healthState.micAvailable) {
        label = 'Voice: secure origin required';
        tone = 'error';
      } else if (!healthState.micAvailable) {
        label = 'Voice: permission needed';
        tone = 'error';
      } else if (!healthState.backendOk) {
        label = 'Voice: backend offline';
        tone = 'warn';
      } else if (speechInterruptionPending) {
        label = 'Voice: checking interruption';
        tone = 'warn';
      } else if (speechPlaybackActive || activeSpeechOwner) {
        label = 'Voice: speaking';
        tone = 'active';
      } else if (speechInFlight || micStartInFlight) {
        label = 'Voice: processing';
        tone = 'warn';
      } else if (micListening || micAutoMode) {
        label = 'Voice: listening';
        tone = 'active';
      } else if (!healthState.micOk || micRecoveryMode) {
        label = 'Voice: recovering';
        tone = 'warn';
      } else {
        label = 'Voice: ready';
        tone = 'ready';
      }
      voiceStateChip.textContent = label;
      voiceStateChip.dataset.tone = tone;
    }

    function persistMicAutoMode() {
      localStorage.setItem(AUTO_LISTEN_STORAGE_KEY, micAutoMode ? '1' : '0');
    }

    function setSelfTestSummary(text, tone = 'warn') {
      selfTestLastSummary = String(text || '').trim() || 'Self-test pending';
      if (selfTestSummaryChip) {
        selfTestSummaryChip.textContent = selfTestLastSummary;
        selfTestSummaryChip.dataset.tone = tone;
      }
    }

    function renderSelfTestPanel() {
      const secureContext = Boolean(window.isSecureContext);
      const browserMediaReady = mediaApiAvailable();
      const micPermissionLabel = !browserMediaReady
        ? 'Unavailable in this browser runtime'
        : micPermissionState === 'granted'
          ? 'Granted'
          : micPermissionState === 'denied'
            ? 'Denied or blocked'
            : micPermissionState === 'unavailable'
              ? 'Unavailable'
              : 'Unknown';
      const selectedMicText = selectedMicLabel || selectedMicDeviceId || (availableMics.length ? `${availableMics.length} detected` : 'No microphone detected');
      const listenerText = micAutoMode
        ? (micListening ? 'Active and listening' : micStartInFlight ? 'Starting listener' : micRecoveryMode ? 'Recovering listener' : micRestartPending ? 'Queued to restart' : 'Armed for full-time listening')
        : 'Off';
      const lastVoiceActivity = micLastEvent || (micRecoveryReason ? `recovering:${micRecoveryReason}` : 'No voice event yet');
      const cameraText = healthState.cameraOk
        ? (cameraStream && cameraStream.active ? 'Watcher active' : availableCameras.length ? 'Device available' : 'Camera ready')
        : (cameraSettingsStatus && cameraSettingsStatus.textContent) ? cameraSettingsStatus.textContent : 'Camera unavailable';
      const backendText = healthState.backendOk ? 'Connected to backend' : 'Backend offline or stale';

      if (selfTestSecureValue) selfTestSecureValue.textContent = secureContext ? 'Secure origin available' : mediaSecureContextLabel();
      if (selfTestMediaApiValue) selfTestMediaApiValue.textContent = browserMediaReady ? 'navigator.mediaDevices available' : 'Browser media APIs unavailable';
      if (selfTestMicPermissionValue) selfTestMicPermissionValue.textContent = micPermissionLabel;
      if (selfTestMicDeviceValue) selfTestMicDeviceValue.textContent = selectedMicText;
      if (selfTestListenerValue) selfTestListenerValue.textContent = listenerText;
      if (selfTestMicActivityValue) selfTestMicActivityValue.textContent = lastVoiceActivity;
      if (selfTestCameraValue) selfTestCameraValue.textContent = cameraText;
      if (selfTestBackendValue) selfTestBackendValue.textContent = backendText;
      if (selfTestToggleListenerBtn) selfTestToggleListenerBtn.textContent = micAutoMode ? 'Turn Listener Off' : 'Turn Listener On';
      if (selfTestTimestamp) {
        selfTestTimestamp.textContent = selfTestLastRunAt > 0
          ? `Last self-test: ${new Date(selfTestLastRunAt).toLocaleTimeString()}`
          : 'Awaiting first self-test.';
      }

      let summaryText = selfTestLastSummary;
      let tone = 'warn';
      if (!browserMediaReady || !secureContext || micPermissionState === 'denied' || !healthState.backendOk) {
        summaryText = !secureContext
          ? 'Mic and camera require a secure origin.'
          : micPermissionState === 'denied'
            ? 'Microphone permission is blocked in this browser.'
            : !browserMediaReady
              ? 'This browser runtime cannot capture media.'
              : 'Backend sync is currently offline.';
        tone = 'error';
      } else if (micAutoMode && micListening && healthState.cameraOk) {
        summaryText = 'Listener and camera are both active.';
        tone = 'ready';
      } else if (micAutoMode) {
        summaryText = 'Listener is armed and waiting to stabilize.';
        tone = 'active';
      } else if (healthState.cameraOk || browserMediaReady) {
        summaryText = 'Media runtime is available. Turn the listener on to start open mic.';
        tone = 'warn';
      }
      setSelfTestSummary(summaryText, tone);
    }

    function visibleChatThreadMessages(messages = []) {
      if (!chatLocallyCleared) {
        return Array.isArray(messages) ? messages : [];
      }
      const cutoffMs = Date.parse(String(chatLocalClearCutoffIso || ''));
      const filteredMessages = (Array.isArray(messages) ? messages : []).filter((message) => {
        if (!cutoffMs) return false;
        const createdAtMs = Date.parse(String(message && message.created_at ? message.created_at : ''));
        return Number.isFinite(createdAtMs) && createdAtMs >= cutoffMs;
      });
      if (chatLocalClearNotice) {
        return [chatLocalClearNotice, ...filteredMessages];
      }
      return filteredMessages;
    }

    function compactMultilineText(value, limit = 180) {
      const normalized = String(value || '')
        .split(/\\r?\\n/)
        .map((line) => line.trim())
        .filter(Boolean)
        .join(' ')
        .replace(/\s+/g, ' ')
        .trim();
      if (!normalized) return '';
      if (normalized.length <= limit) return normalized;
      return `${normalized.slice(0, limit - 3).trimEnd()}...`;
    }

    function looksLikeExecutionLog(content, message = {}) {
      const text = String(content || '');
      const executionId = Number(message.execution_id || 0);
      const structured = /(^|\\n)Iteration\s+[^:]+:|(^|\\n)Task:\s*|(^|\\n)Result:\s*|(^|\\n)Delta:\s*/im.test(text);
      const lineCount = text.split(/\\r?\\n/).filter((line) => line.trim()).length;
      return structured || (executionId > 0 && (lineCount >= 6 || text.length >= 320));
    }

    function normalizeMessageType(message = {}) {
      const explicit = safeText(message.message_type).toLowerCase();
      if (['user', 'mim_reply', 'system_execution', 'system_summary'].includes(explicit)) {
        return explicit;
      }
      const role = safeText(message.role || message.direction || 'mim').toLowerCase();
      if (role === 'operator' || role === 'user' || role === 'inbound') return 'user';
      if (role === 'system') return 'system_summary';
      if (looksLikeExecutionLog(message.execution_text || message.content || '', message)) {
        return 'system_execution';
      }
      return 'mim_reply';
    }

    function parseStructuredExecution(text = '') {
      const steps = [];
      let current = null;

      const ensureCurrent = () => {
        if (!current) {
          current = { iteration: '', task: '', result: '', delta: '', notes: [] };
          steps.push(current);
        }
        return current;
      };

      String(text || '').split(/\\r?\\n/).forEach((rawLine) => {
        const line = String(rawLine || '').trim();
        if (!line) return;
        const iterationMatch = line.match(/^Iteration\s+([^:]+):\s*(.*)$/i);
        if (iterationMatch) {
          current = {
            iteration: `Iteration ${String(iterationMatch[1] || '').trim()}`,
            task: '',
            result: '',
            delta: '',
            notes: [],
          };
          const trailing = String(iterationMatch[2] || '').trim();
          if (trailing) current.notes.push(trailing);
          steps.push(current);
          return;
        }
        const taskMatch = line.match(/^Task:\s*(.*)$/i);
        if (taskMatch) {
          ensureCurrent().task = String(taskMatch[1] || '').trim();
          return;
        }
        const resultMatch = line.match(/^Result:\s*(.*)$/i);
        if (resultMatch) {
          ensureCurrent().result = String(resultMatch[1] || '').trim();
          return;
        }
        const deltaMatch = line.match(/^Delta:\s*(.*)$/i);
        if (deltaMatch) {
          ensureCurrent().delta = String(deltaMatch[1] || '').trim();
          return;
        }
        ensureCurrent().notes.push(line);
      });

      return steps.filter((step) => step.iteration || step.task || step.result || step.delta || (Array.isArray(step.notes) && step.notes.length));
    }

    function messageRoleClass(message = {}) {
      const messageType = normalizeMessageType(message);
      if (messageType === 'user') return 'user';
      if (messageType === 'system_execution' || messageType === 'system_summary') return 'system';
      return 'mim';
    }

    function messageLabel(message = {}) {
      const messageType = normalizeMessageType(message);
      if (messageType === 'user') return 'Operator';
      if (messageType === 'system_execution') return 'Execution';
      if (messageType === 'system_summary') return 'System';
      return 'MIM';
    }

    function formatMessageTime(value) {
      if (!value) return '';
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return '';
      return parsed.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function buildMetaRow(parts = []) {
      const filtered = parts.filter(Boolean);
      if (!filtered.length) return null;
      const meta = document.createElement('div');
      meta.className = 'bubble-meta';
      meta.textContent = filtered.join(' · ');
      return meta;
    }

    function buildTextBlock(text, className = 'bubble-text') {
      const value = String(text || '');
      if (!value.trim()) return null;
      const body = document.createElement('div');
      body.className = className;
      body.textContent = value;
      return body;
    }

    function extractMessageText(message = {}) {
      return safeText(message.inline_text || message.summary_text || message.content || message.execution_text);
    }

    function buildChatRenderSignature(messages = []) {
      const normalized = Array.isArray(messages) ? messages : [];
      return JSON.stringify(normalized.map((message) => ({
        created_at: safeText(message.created_at),
        role: safeText(message.role || message.direction),
        message_type: normalizeMessageType(message),
        interaction_mode: safeText(message.interaction_mode),
        content: extractMessageText(message),
        execution_text: safeText(message.execution_text),
        attachment_url: safeText(message?.attachment?.url),
        attachment_name: safeText(message?.attachment?.filename),
      })));
    }

    function isChatSelectionActive() {
      if (!chatLog || typeof window.getSelection !== 'function') return false;
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed) return false;
      const anchorNode = selection.anchorNode;
      const focusNode = selection.focusNode;
      return Boolean(anchorNode && focusNode && chatLog.contains(anchorNode) && chatLog.contains(focusNode));
    }

    async function copyTextToClipboard(text) {
      const value = String(text || '');
      if (!value.trim()) return false;
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(value);
          return true;
        } catch (_) {
        }
      }
      const helper = document.createElement('textarea');
      helper.value = value;
      helper.setAttribute('readonly', 'readonly');
      helper.style.position = 'fixed';
      helper.style.opacity = '0';
      helper.style.pointerEvents = 'none';
      document.body.appendChild(helper);
      helper.focus();
      helper.select();
      let copied = false;
      try {
        copied = document.execCommand('copy');
      } catch (_) {
        copied = false;
      }
      helper.remove();
      return copied;
    }

    function findPreviousOperatorMessage(messages = [], startIndex = -1) {
      if (!Array.isArray(messages)) return null;
      for (let index = startIndex - 1; index >= 0; index -= 1) {
        const candidate = messages[index];
        if (normalizeMessageType(candidate) === 'user') {
          return candidate;
        }
      }
      return null;
    }

    function buildExchangeSnippet(messages = [], index = -1) {
      if (!Array.isArray(messages) || index < 0 || index >= messages.length) return '';
      const currentMessage = messages[index];
      const parts = [];
      const operatorMessage = findPreviousOperatorMessage(messages, index);
      const operatorText = extractMessageText(operatorMessage);
      if (operatorText) {
        parts.push(`Operator:\n${operatorText}`);
      }
      const mimText = extractMessageText(currentMessage);
      if (mimText) {
        parts.push(`MIM:\n${mimText}`);
      }
      return parts.join('\\n\\n').trim();
    }

    function buildCopyExchangeButton(messages = [], index = -1) {
      const snippet = buildExchangeSnippet(messages, index);
      if (!snippet) return null;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'bubble-copy-btn';
      button.setAttribute('aria-label', 'Copy operator question and MIM response');
      button.title = 'Copy question and response';
      button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M16 1H6C4.9 1 4 1.9 4 3v12h2V3h10V1zm3 4H10c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h9c1.1 0 2-.9 2-2V7c0-1.1-.9-2-2-2zm0 16H10V7h9v14z"/></svg><span data-copy-label>Copy</span>';
      button.addEventListener('click', async (event) => {
        event.preventDefault();
        event.stopPropagation();
        const copied = await copyTextToClipboard(snippet);
        const label = button.querySelector('[data-copy-label]');
        button.classList.toggle('copied', copied);
        if (label) {
          label.textContent = copied ? 'Copied' : 'Retry';
        }
        statusEl.textContent = copied
          ? 'Copied question and response.'
          : 'Copy failed. Select the message text and copy manually.';
        window.setTimeout(() => {
          button.classList.remove('copied');
          if (label) {
            label.textContent = 'Copy';
          }
        }, 1600);
      });
      return button;
    }

    function normalizeExecutionPayload(message = {}) {
      const rawText = safeText(message.execution_text || message.content);
      const providedStructured = message.structured_output && typeof message.structured_output === 'object'
        ? message.structured_output
        : {};
      const steps = Array.isArray(providedStructured.steps) && providedStructured.steps.length
        ? providedStructured.steps
        : parseStructuredExecution(rawText);
      const lines = rawText.split(/\\r?\\n/).filter((line) => line.trim());
      const preview = safeText(message.execution_preview) || lines.slice(0, 24).join('\\n');
      const firstResult = steps
        .map((step) => safeText(step.result))
        .find(Boolean);
      const firstTask = steps
        .map((step) => safeText(step.task))
        .find(Boolean);
      const summary = safeText(message.summary_text || message.inline_text)
        || firstResult
        || firstTask
        || compactMultilineText(rawText, 180)
        || 'Execution output available.';
      return {
        summary,
        rawText,
        preview,
        truncated: Boolean(message.execution_truncated) || lines.length > 24,
        steps,
        lineCount: Number(providedStructured.line_count || lines.length || 0),
      };
    }

    function buildExecutionStep(step = {}, index = 0) {
      const card = document.createElement('section');
      card.className = 'execution-step';

      const header = document.createElement('div');
      header.className = 'execution-step-header';
      header.textContent = safeText(step.iteration, `Step ${index + 1}`);
      card.appendChild(header);

      const grid = document.createElement('div');
      grid.className = 'execution-step-grid';
      const rows = [
        ['Task', safeText(step.task)],
        ['Result', safeText(step.result)],
        ['Delta', safeText(step.delta)],
      ].filter(([, value]) => value);

      rows.forEach(([label, value]) => {
        const row = document.createElement('div');
        row.className = 'execution-row';
        const rowLabel = document.createElement('div');
        rowLabel.className = 'execution-row-label';
        rowLabel.textContent = label;
        row.appendChild(rowLabel);
        const rowValue = document.createElement('div');
        rowValue.className = 'execution-row-value';
        rowValue.textContent = value;
        row.appendChild(rowValue);
        grid.appendChild(row);
      });

      const notes = Array.isArray(step.notes) ? step.notes.filter((item) => safeText(item)) : [];
      if (notes.length) {
        const notesWrap = document.createElement('div');
        notesWrap.className = 'execution-notes';
        notes.forEach((note) => {
          const noteEl = document.createElement('div');
          noteEl.className = 'execution-note';
          noteEl.textContent = safeText(note);
          notesWrap.appendChild(noteEl);
        });
        grid.appendChild(notesWrap);
      }

      card.appendChild(grid);
      return card;
    }

    function buildExecutionDetails(message = {}) {
      const execution = normalizeExecutionPayload(message);
      if (!execution.rawText) return null;

      const details = document.createElement('details');
      details.className = 'execution-details';

      const summary = document.createElement('summary');
      const summaryParts = ['View execution output'];
      if (execution.steps.length) {
        summaryParts.push(`${execution.steps.length} structured step${execution.steps.length === 1 ? '' : 's'}`);
      } else if (execution.lineCount > 0) {
        summaryParts.push(`${execution.lineCount} lines`);
      }
      summary.textContent = summaryParts.join(' · ');
      details.appendChild(summary);

      const scroll = document.createElement('div');
      scroll.className = 'execution-scroll';

      if (execution.steps.length) {
        const stepsWrap = document.createElement('div');
        stepsWrap.className = 'execution-steps';
        execution.steps.forEach((step, index) => {
          stepsWrap.appendChild(buildExecutionStep(step, index));
        });
        scroll.appendChild(stepsWrap);
      }

      const rawToggle = document.createElement('details');
      rawToggle.className = 'execution-raw-toggle';
      const rawSummary = document.createElement('summary');
      rawSummary.textContent = execution.truncated ? 'Show full raw log' : 'Show raw log';
      rawToggle.appendChild(rawSummary);
      const raw = document.createElement('pre');
      raw.className = 'execution-raw';
      raw.textContent = execution.rawText;
      rawToggle.appendChild(raw);
      scroll.appendChild(rawToggle);

      if (execution.truncated) {
        const footnote = document.createElement('div');
        footnote.className = 'execution-footnote';
        footnote.textContent = 'Large execution output is collapsed by default to keep the conversation readable.';
        scroll.appendChild(footnote);
      }

      details.appendChild(scroll);
      return details;
    }

    function setSecondaryTab(tabName) {
      activeSecondaryTab = tabName;
      secondaryTabs.forEach((button) => {
        const isActive = String(button.dataset.tab || '') === tabName;
        button.classList.toggle('active', isActive);
      });
      ['status', 'reasoning', 'diagnostics', 'media'].forEach((tab) => {
        const panel = document.getElementById(`secondaryPanel${tab.charAt(0).toUpperCase()}${tab.slice(1)}`);
        if (panel) {
          panel.classList.toggle('active', tab === tabName);
        }
      });
    }

    function buildEmptyThreadState() {
      const empty = document.createElement('div');
      empty.className = 'empty-thread';
      empty.innerHTML = '<strong>Voice-first chat is ready.</strong><div>Speak, type, or attach an image to continue the primary MIM thread.</div>';
      return empty;
    }

    function buildAttachmentFigure(attachment = {}) {
      const url = safeText(attachment.url);
      if (!url) return null;
      const figure = document.createElement('figure');
      figure.className = 'bubble-attachment';
      const image = document.createElement('img');
      image.src = url;
      image.alt = safeText(attachment.filename, 'Attached image');
      figure.appendChild(image);
      const caption = document.createElement('figcaption');
      const size = Number(attachment.size_bytes || 0);
      caption.textContent = [
        safeText(attachment.filename, 'Image attachment'),
        size > 0 ? `${Math.max(1, Math.round(size / 1024))} KB` : '',
      ].filter(Boolean).join(' · ');
      figure.appendChild(caption);
      return figure;
    }

    function buildChatBubble(message = {}, index = -1, messages = []) {
      const bubble = document.createElement('div');
      const messageType = normalizeMessageType(message);
      bubble.className = `chat-bubble ${messageRoleClass(message)}${messageType === 'system_execution' ? ' execution' : ''}`;
      const metaParts = [messageLabel(message)];
      const mode = (messageType === 'system_execution' || messageType === 'system_summary')
        ? messageType.replace(/_/g, ' ')
        : safeText(message.interaction_mode || message.message_type).replace(/_/g, ' ');
      if (mode) {
        metaParts.push(mode);
      }
      const time = formatMessageTime(message.created_at);
      if (time) {
        metaParts.push(time);
      }
      const metaSummary = metaParts.filter(Boolean).join(' · ');
      if (metaSummary) {
        bubble.title = metaSummary;
      }

      if (messageType === 'mim_reply') {
        const copyButton = buildCopyExchangeButton(messages, index);
        if (copyButton) {
          bubble.classList.add('has-copy-action');
          bubble.appendChild(copyButton);
        }
      }

      const meta = buildMetaRow(metaParts);
      if (meta) {
        bubble.appendChild(meta);
      }

      const attachment = message && typeof message.attachment === 'object' ? message.attachment : null;
      if (attachment && safeText(attachment.url)) {
        const figure = buildAttachmentFigure(attachment);
        if (figure) bubble.appendChild(figure);
      }

      if (messageType === 'system_execution') {
        const execution = normalizeExecutionPayload(message);
        const summary = buildTextBlock(execution.summary, 'bubble-summary');
        if (summary) {
          bubble.appendChild(summary);
        }
        const details = buildExecutionDetails(message);
        if (details) {
          bubble.appendChild(details);
        }
        return bubble;
      }

      const inlineText = safeText(message.inline_text || message.summary_text || message.content);
      if (inlineText) {
        const body = buildTextBlock(inlineText, messageType === 'system_summary' ? 'bubble-summary' : 'bubble-text');
        if (body) bubble.appendChild(body);
      }
      return bubble;
    }

    function renderMediaGrid(messages = []) {
      if (!mediaGrid) return;
      mediaGrid.innerHTML = '';
      const imageMessages = messages.filter((message) => {
        const attachment = message && message.attachment && typeof message.attachment === 'object'
          ? message.attachment
          : null;
        return Boolean(attachment && safeText(attachment.url));
      });
      if (!imageMessages.length) {
        const empty = document.createElement('div');
        empty.className = 'sidebar-copy';
        empty.textContent = 'No uploaded images yet. Attach screenshots from the main composer.';
        mediaGrid.appendChild(empty);
        return;
      }
      imageMessages.slice().reverse().forEach((message) => {
        const attachment = message && message.attachment && typeof message.attachment === 'object'
          ? message.attachment
          : null;
        if (!attachment) return;
        const figure = document.createElement('figure');
        figure.className = 'media-card';
        const image = document.createElement('img');
        image.src = safeText(attachment.url);
        image.alt = safeText(attachment.filename, 'Uploaded image');
        figure.appendChild(image);
        const caption = document.createElement('figcaption');
        caption.textContent = [safeText(attachment.filename, 'Image'), safeText(message.content)].filter(Boolean).join(' · ');
        figure.appendChild(caption);
        mediaGrid.appendChild(figure);
      });
    }

    function renderChatThread(messages = [], { force = false } = {}) {
      if (!chatLog) return;
      const visibleMessages = visibleChatThreadMessages(messages);
      const signature = buildChatRenderSignature(visibleMessages);
      if (!force && signature === lastRenderedChatSignature) {
        renderMediaGrid(visibleMessages);
        return;
      }
      if (!force && isChatSelectionActive()) {
        return;
      }
      const distanceToBottom = chatLog.scrollHeight - chatLog.scrollTop - chatLog.clientHeight;
      const shouldStickToBottom = force || !chatLog.children.length || distanceToBottom < 72;
      chatLog.innerHTML = '';
      if (!Array.isArray(visibleMessages) || !visibleMessages.length) {
        lastRenderedChatSignature = signature;
        chatLog.appendChild(buildEmptyThreadState());
        renderMediaGrid([]);
        return;
      }
      visibleMessages.forEach((message, index) => {
        chatLog.appendChild(buildChatBubble(message, index, visibleMessages));
      });
      lastRenderedChatSignature = signature;
      if (shouldStickToBottom) {
        window.requestAnimationFrame(() => {
          chatLog.scrollTop = chatLog.scrollHeight;
        });
      }
      renderMediaGrid(visibleMessages);
    }

    function appendChatMessage(role, text, options = {}) {
      const clean = safeText(text);
      if (!clean) return;
      const tempMessage = {
        role: role === 'user' ? 'operator' : role,
        content: clean,
        created_at: new Date().toISOString(),
        interaction_mode: safeText(options.interactionMode, role === 'user' ? 'text' : ''),
        message_type: safeText(options.messageType, role === 'user' ? 'user' : 'mim_reply'),
        attachment: options.attachment || null,
      };
      chatThreadMessages = [...chatThreadMessages, tempMessage];
      renderChatThread(chatThreadMessages, { force: true });
    }

    function createClientMessageId(prefix) {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return `${prefix}-${window.crypto.randomUUID()}`;
      }
      return `${prefix}-${Date.now()}-${Math.floor(Math.random() * 1000000)}`;
    }

    const textChatSessionStorageKey = 'mim_text_chat_session_id';
    let textChatSessionId = primaryThreadKey;
    localStorage.setItem(textChatSessionStorageKey, textChatSessionId);

    function classifyTextChatIntent(text) {
      const raw = String(text || '').trim();
      const lowered = raw.toLowerCase();
      const looksLikeQuestion = raw.endsWith('?') || /^(what|why|how|when|where|who|which|is|are|can|could|will|would|do|does|did|tell me|give me|explain|show me)\b/.test(lowered);
      if (looksLikeQuestion) {
        return 'discussion';
      }
      if (/(^|\b)(run|execute|dispatch|invoke|trigger|open|download|delete|remove|erase|shut down|shutdown)\b/.test(lowered)) {
        return 'execute_capability';
      }
      return 'discussion';
    }

    function classifyTextChatSafetyFlags(text) {
      const lowered = String(text || '').trim().toLowerCase();
      if (!lowered) return [];
      const explicitDestructiveTargets = [
        '/var/lib',
        'database',
        'delete the database',
        'remove the database',
        'erase the database',
        'shut down the system',
        'shutdown the system',
        'rm -rf',
      ];
      const explicitDestructiveVerbs = /(delete|remove|erase|destroy|wipe|shutdown|shut down)/.test(lowered);
      if (explicitDestructiveVerbs && explicitDestructiveTargets.some((token) => lowered.includes(token))) {
        return ['blocked'];
      }
      return [];
    }

    function summarizeTextResolution(result) {
      const interfaceReply = result && typeof result.mim_interface === 'object' ? result.mim_interface : {};
      const explicitReply = String(interfaceReply.reply_text || '').trim();
      if (explicitReply) {
        return explicitReply;
      }

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

    function resetComposerImage() {
      selectedComposerImage = null;
      if (imageUploadInput) imageUploadInput.value = '';
      if (imagePreviewImg) imagePreviewImg.removeAttribute('src');
      if (imagePreviewName) imagePreviewName.textContent = 'Selected image';
      if (imagePreviewMeta) imagePreviewMeta.textContent = 'Add an optional prompt, then send.';
      if (imagePreviewWrap) imagePreviewWrap.hidden = true;
      if (chatDropzone) chatDropzone.classList.remove('active');
    }

    function setComposerImage(file) {
      if (!(file instanceof File)) return;
      selectedComposerImage = file;
      if (imagePreviewName) imagePreviewName.textContent = file.name || 'Selected image';
      if (imagePreviewMeta) {
        imagePreviewMeta.textContent = `${Math.max(1, Math.round((Number(file.size || 0)) / 1024))} KB · ${safeText(file.type, 'image file')}`;
      }
      if (imagePreviewImg) {
        const previewUrl = URL.createObjectURL(file);
        imagePreviewImg.src = previewUrl;
      }
      if (imagePreviewWrap) imagePreviewWrap.hidden = false;
    }

    async function submitConversationTurn(messageText, interactionMode = 'text') {
      const text = String(messageText || '').trim();
      if (!text) return;
      const parsedIntent = classifyTextChatIntent(text);
      const safetyFlags = classifyTextChatSafetyFlags(text);
      const source = interactionMode === 'voice'
        ? 'mim_ui_voice_chat'
        : interactionMode === 'quick_action'
          ? 'mim_ui_quick_action'
          : 'mim_ui_text_chat';

      appendChatMessage('user', text, { interactionMode });

      try {
        const response = await fetch('/gateway/intake/text', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            text,
            parsed_intent: parsedIntent,
            safety_flags: safetyFlags,
            metadata_json: {
              source,
              interaction_mode: interactionMode,
              message_type: 'user',
              conversation_session_id: textChatSessionId,
              route_preference: 'conversation_layer',
            },
          }),
        });
        if (!response.ok) {
          appendChatMessage('mim', `Text chat request failed (${response.status}).`);
          return;
        }

        const result = await response.json();
        if (chatInput && interactionMode !== 'voice') {
          chatInput.value = '';
        }
        await refreshState();
        if (!latestUiState?.chat_thread) {
          appendChatMessage('mim', summarizeTextResolution(result));
        }
      } catch (error) {
        const detail = error && error.message ? String(error.message) : 'request_failed';
        appendChatMessage('mim', `Text chat is temporarily unavailable (${detail}).`);
      }
    }

    async function uploadComposerImage() {
      if (!(selectedComposerImage instanceof File)) return;
      const prompt = String(chatInput ? chatInput.value : '').trim();
      appendChatMessage('user', prompt || `Shared image: ${selectedComposerImage.name}`, {
        interactionMode: 'image',
        attachment: {
          url: imagePreviewImg ? imagePreviewImg.src : '',
          filename: selectedComposerImage.name,
          size_bytes: Number(selectedComposerImage.size || 0),
        },
      });

      const form = new FormData();
      form.append('file', selectedComposerImage);
      form.append('prompt', prompt);
      form.append('session_key', textChatSessionId);
      if (chatInput) chatInput.value = '';

      try {
        const response = await fetch('/mim/ui/chat/upload-image', {
          method: 'POST',
          body: form,
        });
        if (!response.ok) {
          appendChatMessage('mim', `Image upload failed (${response.status}).`);
          return;
        }
        await response.json();
        resetComposerImage();
        await refreshState();
      } catch (error) {
        const detail = error && error.message ? String(error.message) : 'upload_failed';
        appendChatMessage('mim', `Image upload is temporarily unavailable (${detail}).`);
      }
    }

    async function sendTextChat() {
      const text = String(chatInput ? chatInput.value : '').trim();
      if (selectedComposerImage instanceof File) {
        await uploadComposerImage();
        return;
      }
      if (!text) return;
      await submitConversationTurn(text, 'text');
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
    let speechInterruptionPending = false;
    let speechInterruptedOwner = '';
    let speechInterruptionTimer = null;
    let speechInterruptionStartedAt = 0;
    let speechInterruptionLastProbeAt = 0;
    const frontendMediaStatusCache = {
      microphone: { signature: '', at: 0 },
      camera: { signature: '', at: 0 },
    };
    let micSuppressedUntil = 0;
    let recentSpokenUtterances = [];
    let localTtsPlaybackToken = 0;
    let lastSpokenPhraseCompact = '';
    let lastSpokenPhraseAt = 0;
    let refreshInFlight = false;
    let refreshPending = false;
    const SPEECH_INTERRUPTION_CONFIRM_MS = 1200;
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
      statusEl.textContent = on ? 'MIM is speaking back.' : 'MIM is listening for the next turn.';
      updateVoiceStateUi();
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
      const activeWork = (reasoning && typeof reasoning.active_work === 'object') ? reasoning.active_work : {};
      if (String(activeWork.summary || '').trim()) {
        const meta = [
          activeWork.badge,
          activeWork.execution_id_label || activeWork.task_id || activeWork.request_id,
          activeWork.workstream_name && String(activeWork.workstream_name || '').trim().replaceAll('_', ' '),
          activeWork.workstream_status && String(activeWork.workstream_status || '').trim().replaceAll('_', ' '),
        ].filter(Boolean).join(' | ');
        entries.push({
          title: 'MIM work status',
          meta,
          note: String(activeWork.summary || '').trim(),
        });
      }
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

    function clearSpeechInterruptionTimer() {
      if (speechInterruptionTimer) {
        clearTimeout(speechInterruptionTimer);
        speechInterruptionTimer = null;
      }
    }

    function resetSpeechInterruptionState() {
      clearSpeechInterruptionTimer();
      speechInterruptionPending = false;
      speechInterruptedOwner = '';
      speechInterruptionStartedAt = 0;
    }

    function isLikelyIntentionalInterruption(transcript, confidence = 0) {
      const text = String(transcript || '').trim();
      if (!text) return false;
      const wordCount = text.split(/\s+/).filter(Boolean).length;
      if (hasWakePhrase(text)) return true;
      if (wordCount >= 3) return true;
      return text.length >= 8 && Number(confidence || 0) >= 0.72;
    }

    function resumeSpeechAfterInterruption(reason = 'resume') {
      if (!speechInterruptionPending) return false;
      const owner = speechInterruptedOwner || activeSpeechOwner;
      resetSpeechInterruptionState();
      if (!owner) return false;
      try {
        if (owner === 'browser_tts' && window.speechSynthesis && typeof window.speechSynthesis.resume === 'function') {
          window.speechSynthesis.resume();
          speechPlaybackActive = true;
          activeSpeechOwner = 'browser_tts';
          setSpeaking(true);
        } else if (owner === 'server_tts' && activeServerTtsAudio) {
          const playPromise = activeServerTtsAudio.play();
          if (playPromise && typeof playPromise.catch === 'function') {
            playPromise.catch(() => {});
          }
          speechPlaybackActive = true;
          activeSpeechOwner = 'server_tts';
          setSpeaking(true);
        } else {
          return false;
        }
        addSpeechDebug('resumed', `source=${owner} reason=${reason}`);
        updateConnectionChrome();
        return true;
      } catch (_) {
        return false;
      }
    }

    function cancelSpeechForInterruption(reason = 'confirmed-input') {
      const owner = speechInterruptedOwner || activeSpeechOwner;
      resetSpeechInterruptionState();
      if (owner) {
        addSpeechDebug('canceled', `source=${owner} reason=${reason}`);
      }
      stopServerTtsPlayback();
      localTtsPlaybackToken += 1;
      if (window.speechSynthesis && window.speechSynthesis.cancel) {
        window.speechSynthesis.cancel();
      }
      speechPlaybackActive = false;
      activeSpeechOwner = '';
      setSpeaking(false);
      updateConnectionChrome();
    }

    function pauseSpeechForInterruption(trigger = 'speech-detected') {
      if (speechInterruptionPending || !speechPlaybackActive || !activeSpeechOwner) return false;
      const now = Date.now();
      if ((now - speechInterruptionLastProbeAt) < 900) return false;
      let paused = false;
      try {
        if (activeSpeechOwner === 'browser_tts' && window.speechSynthesis && typeof window.speechSynthesis.pause === 'function') {
          window.speechSynthesis.pause();
          paused = true;
        } else if (activeSpeechOwner === 'server_tts' && activeServerTtsAudio) {
          activeServerTtsAudio.pause();
          paused = true;
        }
      } catch (_) {
      }
      if (!paused) return false;
      speechInterruptionLastProbeAt = now;
      speechInterruptionPending = true;
      speechInterruptedOwner = activeSpeechOwner;
      speechInterruptionStartedAt = now;
      speechPlaybackActive = false;
      setSpeaking(false);
      addSpeechDebug('paused', `source=${activeSpeechOwner} reason=${trigger}`);
      clearSpeechInterruptionTimer();
      speechInterruptionTimer = setTimeout(() => {
        resumeSpeechAfterInterruption('timeout');
      }, SPEECH_INTERRUPTION_CONFIRM_MS);
      statusEl.textContent = 'Possible interruption detected. Waiting for new input...';
      updateConnectionChrome();
      return true;
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
      if (speechInterruptionPending) return false;
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
        micEventEl.textContent = `Recent voice event: ${micLastEvent}`;
      }
      addMicDebug(`event:${eventLabel}`, detailText);
      syncListenButtonLabel();
      updateMicDiagnostics();
    }

    function syncListenButtonLabel() {
      if (!listenBtn) return;
      persistMicAutoMode();
      if (autoListenToggle) {
        autoListenToggle.checked = Boolean(micAutoMode);
      }
      if (chatMicBtn) {
        chatMicBtn.textContent = micAutoMode ? 'Turn Listener Off' : 'Turn Listener On';
      }
      const hasVoiceApi = Boolean(window.SpeechRecognition || window.webkitSpeechRecognition || window.speechSynthesis);
      listenBtn.classList.remove('error');
      if (!hasVoiceApi) {
        listenBtn.textContent = 'Voice Unavailable';
        listenBtn.classList.add('error');
        return;
      }
      if (!healthState.micAvailable) {
        listenBtn.textContent = 'Enable Microphone';
        listenBtn.classList.add('error');
        return;
      }
      if (micAutoMode && micStartInFlight) {
        listenBtn.textContent = 'Processing…';
        return;
      }
      listenBtn.textContent = micAutoMode ? 'Turn Listener Off' : 'Turn Listener On';
      renderSelfTestPanel();
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
      if (speechInterruptionPending) {
        statusEl.textContent = 'Possible interruption detected. Waiting for new input...';
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

    function setConsoleNavLight(node, ok) {
      if (!node) return;
      node.classList.remove('ok', 'err');
      node.classList.add(ok ? 'ok' : 'err');
    }

    function updateConsoleNavLights({ mimOk = true, todOk = true } = {}) {
      setConsoleNavLight(mimConsoleLight, Boolean(mimOk));
      setConsoleNavLight(todConsoleLight, Boolean(todOk));
    }

    function updateIconGlow() {
      mimIcon.classList.remove('ok', 'err');
      if (hasCriticalHealthError()) {
        mimIcon.classList.add('err');
        updateConsoleNavLights({ mimOk: false, todOk: false });
        applyStatusFromHealth();
        return;
      }
      mimIcon.classList.add('ok');
      updateConsoleNavLights({ mimOk: true, todOk: true });
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
          persistMicAutoMode();
          syncListenButtonLabel();
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
      persistMicAutoMode();
      syncListenButtonLabel();
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

    function mediaApiAvailable() {
      return Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    }

    function mediaSecureContextLabel() {
      if (window.isSecureContext) {
        return 'secure context available';
      }
      const origin = `${location.protocol}//${location.host}`;
      return `secure context required for media capture on ${origin}`;
    }

    function describeMediaStartupFailure(lane, error = null, apiMissing = false) {
      const parts = [];
      if (apiMissing) {
        parts.push('browser media APIs unavailable');
      }
      if (!window.isSecureContext) {
        parts.push(mediaSecureContextLabel());
      }
      const errorName = String(error?.name || '').trim();
      if (errorName === 'NotAllowedError') {
        parts.push('permission denied by browser');
      } else if (errorName === 'NotFoundError' || errorName === 'DevicesNotFoundError') {
        parts.push(`no ${lane} device detected`);
      } else if (errorName === 'NotReadableError' || errorName === 'TrackStartError') {
        parts.push(`${lane} device is busy or unavailable`);
      } else if (errorName === 'SecurityError') {
        parts.push('browser rejected media access for this origin');
      }
      const message = String(error?.message || '').trim();
      if (message && !parts.includes(message)) {
        parts.push(message);
      }
      return parts.join('; ') || `${lane} startup failed`;
    }

    function deriveMediaFailureStatus(error = null, apiMissing = false) {
      if (apiMissing) {
        return window.isSecureContext ? 'api_unavailable' : 'insecure_context';
      }
      const errorName = String(error?.name || '').trim();
      if (!window.isSecureContext && (errorName === 'SecurityError' || errorName === 'NotAllowedError' || !errorName)) {
        return 'insecure_context';
      }
      if (errorName === 'NotAllowedError') return 'permission_denied';
      if (errorName === 'NotFoundError' || errorName === 'DevicesNotFoundError') return 'no_device';
      if (errorName === 'NotReadableError' || errorName === 'TrackStartError') return 'device_busy';
      if (errorName === 'SecurityError') return 'insecure_context';
      return 'start_failed';
    }

    async function reportFrontendMediaStatus(lane, status, detail = '', extra = {}) {
      const laneKey = lane === 'camera' ? 'camera' : 'microphone';
      const payload = {
        lane: laneKey,
        status: String(status || 'unknown').trim().toLowerCase() || 'unknown',
        detail: String(detail || '').trim(),
        secure_context: Boolean(window.isSecureContext),
        media_devices_available: mediaApiAvailable(),
        permission_state: laneKey === 'microphone' ? micPermissionState : null,
        selected_device_id: laneKey === 'microphone' ? (selectedMicDeviceId || null) : (selectedCameraDeviceId || null),
        selected_device_label: laneKey === 'microphone' ? (selectedMicLabel || null) : null,
        ...extra,
      };
      const signature = JSON.stringify(payload);
      const cacheEntry = frontendMediaStatusCache[laneKey];
      const now = Date.now();
      if (cacheEntry && cacheEntry.signature === signature && now - cacheEntry.at < 5000) {
        return;
      }
      frontendMediaStatusCache[laneKey] = { signature, at: now };
      try {
        await fetchWithTimeout('/mim/ui/frontend-media-status', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }, 3500);
      } catch (_) {
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
      if (activeSpeechOwner === 'server_tts') {
        activeSpeechOwner = '';
      }
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
        resetSpeechInterruptionState();
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

      if ((speechInFlight || speechPlaybackActive || speechInterruptionPending) && !interrupt) {
        addSpeechDebug('suppressed', `source=${sourceTag} reason=busy_no_interrupt sig=${shortSpeechSignature(phrase)} token=${localTtsPlaybackToken}`);
        return false;
      }

      if ((speechInFlight || speechPlaybackActive || speechInterruptionPending) && interrupt) {
        addSpeechDebug('canceled', `source=${sourceTag} reason=interrupt-active-owner owner=${activeSpeechOwner || '-'} token=${localTtsPlaybackToken}`);
        resetSpeechInterruptionState();
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
      resetSpeechInterruptionState();
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
        healthState.micAvailable = false;
        healthState.micOk = false;
        statusEl.textContent = window.isSecureContext
          ? 'Microphone API unavailable in this browser runtime.'
          : 'Microphone blocked because this page is not running in a secure context.';
        updateConnectionChrome();
        updateIconGlow();
        await reportFrontendMediaStatus(
          'microphone',
          deriveMediaFailureStatus(null, true),
          describeMediaStartupFailure('microphone', null, true),
        );
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
        healthState.micAvailable = true;
        healthState.micOk = true;
        await enumerateMicDevices();
        updateMicDiagnostics();
        await reportFrontendMediaStatus(
          'microphone',
          keepStreamAlive ? 'listening' : 'ready',
          keepStreamAlive ? 'Microphone permission granted and keep-alive stream active.' : 'Microphone permission granted.',
        );
        return true;
      } catch (error) {
        noteMicEvent('permission-error', String(error?.name || error?.message || 'unknown'));
        micPermissionState = 'denied';
        healthState.micAvailable = false;
        const failureDetail = describeMediaStartupFailure('microphone', error);
        const failureStatus = deriveMediaFailureStatus(error);
        statusEl.textContent = failureStatus === 'insecure_context'
          ? 'Microphone blocked because this page is not running in a secure context.'
          : 'Mic permission blocked. Allow microphone access for MIM Desktop.';
        healthState.micOk = false;
        updateConnectionChrome();
        updateIconGlow();
        await reportFrontendMediaStatus('microphone', failureStatus, failureDetail);
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

    function updateConnectionChrome() {
      if (connectionChip) {
        connectionChip.textContent = healthState.backendOk ? 'Connected' : 'Backend offline';
        connectionChip.classList.toggle('strong', healthState.backendOk);
      }
      const voiceReady = Boolean(window.SpeechRecognition || window.webkitSpeechRecognition || window.speechSynthesis);
      if (voiceAvailabilityChip) {
        const label = !voiceReady
          ? 'Voice unavailable'
          : speechInterruptionPending
            ? 'Checking interruption'
            : speechPlaybackActive || activeSpeechOwner
            ? 'Speaking back'
            : speechInFlight || micStartInFlight
              ? 'Processing'
              : micListening || micAutoMode
                ? 'Listening'
            : 'Voice ready';
        voiceAvailabilityChip.textContent = label;
        voiceAvailabilityChip.classList.toggle('strong', voiceReady);
      }
      if (voiceAvailabilityText) {
        if (!voiceReady) {
          voiceAvailabilityText.textContent = 'Browser speech APIs are unavailable in this client.';
        } else if (!window.isSecureContext && !healthState.micAvailable) {
          voiceAvailabilityText.textContent = 'Microphone capture needs a secure origin such as https or localhost.';
        } else if (!healthState.micAvailable) {
          voiceAvailabilityText.textContent = 'Microphone permission or hardware is not currently available.';
        } else if (speechInterruptionPending) {
          voiceAvailabilityText.textContent = 'MIM paused speech briefly to confirm whether you are interrupting with a new request.';
        } else if (speechPlaybackActive || activeSpeechOwner) {
          voiceAvailabilityText.textContent = 'MIM is speaking its latest reply aloud while keeping the same primary thread in sync.';
        } else if (speechInFlight || micStartInFlight) {
          voiceAvailabilityText.textContent = 'Voice input is processing and will append to the same primary thread.';
        } else if (micListening || micAutoMode) {
          voiceAvailabilityText.textContent = 'Full-time listening is active. Transcripts will append into the primary thread.';
        } else {
          voiceAvailabilityText.textContent = 'Voice is ready. Turn the full-time listener on when you want open mic input.';
        }
      }
      if (voiceHintChip) {
        voiceHintChip.textContent = speechInterruptionPending
          ? 'Speech is paused for a moment while MIM checks for a real interruption.'
          : micListening || micAutoMode
          ? 'Listening, transcribing, and routing replies into this thread.'
          : 'Voice transcripts and replies stay in the same thread.';
      }
      updateVoiceStateUi();
      renderSelfTestPanel();
    }

    function renderPrimaryStatus(data = {}) {
      const context = data && typeof data.conversation_context === 'object' ? data.conversation_context : {};
      const initiative = data && typeof data.initiative_driver === 'object' ? data.initiative_driver : {};
      const systemActivity = data && typeof data.system_activity === 'object' ? data.system_activity : {};
      const todTruthReconciliation = data && typeof data.tod_truth_reconciliation === 'object'
        ? data.tod_truth_reconciliation
        : systemActivity && typeof systemActivity.tod_truth_reconciliation === 'object'
        ? systemActivity.tod_truth_reconciliation
        : {};
      const todState = String(todTruthReconciliation.state || '').trim().toLowerCase();
      const todOk = !todState || ['confirmed', 'aligned', 'idle', 'not_applicable'].includes(todState) || Boolean(todTruthReconciliation.execution_confirmed);
      updateConsoleNavLights({ mimOk: !hasCriticalHealthError(), todOk });
      const initiativeObjective = initiative && typeof initiative.active_objective === 'object' ? initiative.active_objective : {};
      const initiativeTask = initiative && typeof initiative.active_task === 'object' ? initiative.active_task : {};
      const initiativeNextTask = initiative && typeof initiative.next_task === 'object' ? initiative.next_task : {};
      const activity = initiative && typeof initiative.activity === 'object' ? initiative.activity : {};
      const progress = initiative && typeof initiative.progress === 'object' ? initiative.progress : {};
      const objective = resolveInitiativeLabel(initiativeObjective, safeText(context.initiative_active_objective || context.active_goal, 'No active objective'));
      const activeTask = resolveInitiativeLabel(initiativeTask, safeText(context.initiative_active_task, 'No active task'));
      const nextTaskFallback = ['working', 'stale'].includes(String(activity.state || '').trim().toLowerCase())
        ? 'Still working current task'
        : 'No next task';
      const nextTask = resolveInitiativeLabel(initiativeNextTask, safeText(context.initiative_next_task, nextTaskFallback));
      const openQuestion = safeText(context.open_question, 'None');
      const memoryHint = safeText(context.memory_hint, 'None');
      const recentInput = safeText(context.recent_user_input, 'Waiting for input…');
      const healthSummary = safeText(context.runtime_health_summary || data.latest_output_text, 'Runtime summary unavailable');
      const blockerCount = Array.isArray(initiative.blockers) ? initiative.blockers.length : 0;
      const activityLabel = safeText(systemActivity.status_label || systemActivity.label || activity.label || initiative.status, 'Idle').replace(/_/g, ' ');
      const activityAge = formatAgeSummary(activity.stale_seconds);
      const activitySummary = safeText(
        systemActivity.summary || activity.summary || context.initiative_activity_summary || initiative.summary,
        safeText(context.operator_reasoning_summary, 'No active initiative summary yet.'),
      );
      const completedTaskCount = Math.max(0, Number(progress.completed_task_count || 0));
      const taskCount = Math.max(0, Number(progress.task_count || 0));
      const progressPercent = Math.max(0, Math.min(100, Number(progress.percent || 0)));
      let progressLabel = taskCount ? `${progressPercent}%` : 'No tasks yet';
      let progressDetail = taskCount
        ? `${completedTaskCount}/${taskCount} bounded tasks completed`
        : safeText(progress.summary, 'No bounded tasks are registered yet.');
      if (Boolean(todTruthReconciliation.should_override_completion) || safeText(todTruthReconciliation.state).toLowerCase() === 'coordination_response_missing') {
        progressLabel = safeText(todTruthReconciliation.progress_label, 'Execution unconfirmed');
        progressDetail = safeText(todTruthReconciliation.progress_detail || todTruthReconciliation.summary, progressDetail);
      }
      setTextWithTitle(activeObjectiveText, objective, 'No active objective');
      setTextWithTitle(activeTaskText, activeTask, 'No active task');
      setTextWithTitle(activityStateText, activityLabel, 'Idle');
      setTextWithTitle(nextTaskText, nextTask, nextTaskFallback);
      setTextWithTitle(blockerCountText, blockerCount ? `${blockerCount} active` : '0 visible', '0 visible');
      if (activityStateDetailText) {
        const explicitDetail = safeText(systemActivity.execution_allowed_reason || systemActivity.stall_reason);
        const detail = explicitDetail
          ? `${activitySummary} ${explicitDetail}`.trim()
          : activityAge && String(activity.state || '').trim().toLowerCase() === 'stale'
          ? `${activitySummary} Last bounded start was about ${activityAge} ago.`
          : activitySummary;
        activityStateDetailText.textContent = detail;
        activityStateDetailText.title = detail;
      }
      setTextWithTitle(progressText, progressLabel, 'No tasks yet');
      if (progressDetailText) {
        progressDetailText.textContent = progressDetail;
        progressDetailText.title = progressDetail;
      }
      if (progressFill) {
        progressFill.style.width = `${progressPercent}%`;
      }
      if (initiativeSummaryText) {
        initiativeSummaryText.textContent = activitySummary;
        initiativeSummaryText.title = activitySummary;
      }
      if (initiativeChip) {
        initiativeChip.textContent = blockerCount && !['working', 'stale'].includes(String(activity.state || '').trim().toLowerCase())
          ? `Initiative stuck (${blockerCount})`
          : `Initiative ${activityLabel.toLowerCase()}`;
        initiativeChip.title = activitySummary;
      }
      if (contextChip) {
        const contextLine = [
          `Activity: ${activityLabel}`,
          `Objective: ${objective}`,
          `Active task: ${activeTask}`,
        ].filter(Boolean).join(' | ');
        contextChip.textContent = contextLine;
        contextChip.title = contextLine;
      }
      setTextWithTitle(recentInputText, recentInput, 'Waiting for input…');
      setTextWithTitle(openQuestionText, openQuestion, 'None');
      setTextWithTitle(memoryHintText, memoryHint, 'None');
      setTextWithTitle(runtimeHealthText, healthSummary, 'Runtime summary unavailable');
      renderSystemActivityTruth(systemActivity);
      renderProgramQueue(initiative);
    }

    function renderProgramQueue(initiative = {}) {
      const programStatus = initiative && typeof initiative.program_status === 'object' ? initiative.program_status : {};
      const activeProject = initiative && typeof initiative.active_project === 'object' ? initiative.active_project : {};
      const projects = Array.isArray(programStatus.projects) ? programStatus.projects : [];
      const activeProjectId = safeText(activeProject.project_id, '').toLowerCase();
      if (programQueueSummaryText) {
        programQueueSummaryText.textContent = safeText(programStatus.summary, projects.length ? 'Ordered project queue loaded.' : 'No ordered project queue is registered yet.');
        programQueueSummaryText.title = programQueueSummaryText.textContent;
      }
      if (!programQueueList) {
        return;
      }
      programQueueList.innerHTML = '';
      if (!projects.length) {
        const empty = document.createElement('div');
        empty.className = 'status-subtext';
        empty.textContent = 'Program registration is not available yet.';
        programQueueList.appendChild(empty);
        if (programQueueMetaText) {
          programQueueMetaText.textContent = 'No program steps are registered yet.';
        }
        if (programQueueToggleBtn) {
          programQueueToggleBtn.hidden = true;
        }
        return;
      }
      const activeIndex = projects.findIndex((project) => safeText(project && project.project_id, '').toLowerCase() === activeProjectId);
      const visibleProjects = showFullProgramQueue
        ? projects
        : projects.filter((project, index) => {
          if (index < 3) return true;
          if (activeIndex >= 0 && Math.abs(index - activeIndex) <= 2) return true;
          if (index >= projects.length - 2) return true;
          return false;
        });
      if (programQueueMetaText) {
        const hiddenCount = Math.max(0, projects.length - visibleProjects.length);
        programQueueMetaText.textContent = hiddenCount > 0
          ? `${visibleProjects.length} visible now, ${hiddenCount} hidden to keep the queue readable.`
          : 'Showing all registered steps.';
      }
      if (programQueueToggleBtn) {
        programQueueToggleBtn.hidden = projects.length <= visibleProjects.length;
        programQueueToggleBtn.textContent = showFullProgramQueue ? 'Show less' : 'Show all';
      }
      visibleProjects.forEach((project) => {
        const index = projects.indexOf(project);
        const projectId = safeText(project && project.project_id, `Project ${index + 1}`);
        const status = safeText(project && project.status, 'ready');
        const objective = safeText(project && project.objective, 'No objective recorded.');
        const normalizedStatus = status.toLowerCase().replace(/[^a-z0-9]+/g, '-');
        const isActive = activeProjectId && projectId.toLowerCase() === activeProjectId;
        const item = document.createElement('div');
        item.className = `program-queue-item${isActive ? ' active' : ''}`;

        const heading = document.createElement('div');
        heading.className = 'program-queue-heading';

        const order = document.createElement('span');
        order.className = 'program-queue-order';
        order.textContent = `Step ${index + 1}`;

        const title = document.createElement('strong');
        title.textContent = projectId;
        title.title = projectId;

        heading.appendChild(order);
        heading.appendChild(title);

        const badge = document.createElement('div');
        badge.className = `program-queue-status ${isActive ? 'active' : normalizedStatus.includes('complete') ? 'completed' : normalizedStatus.includes('block') || normalizedStatus.includes('stale') ? 'blocked' : ''}`.trim();
        badge.textContent = status.replace(/_/g, ' ');
        badge.title = status;

        const objectiveText = document.createElement('div');
        objectiveText.className = 'program-queue-objective';
        objectiveText.textContent = objective;
        objectiveText.title = objective;

        item.appendChild(heading);
        item.appendChild(badge);
        item.appendChild(objectiveText);
        programQueueList.appendChild(item);
      });
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
        const chatThread = data && typeof data.chat_thread === 'object' ? data.chat_thread : {};
        const threadMessages = Array.isArray(chatThread.messages) ? chatThread.messages : [];
        chatThreadMessages = threadMessages;
        if (threadStatusChip) {
          threadStatusChip.textContent = `Primary thread: ${safeText(chatThread.primary_thread, textChatSessionId)}`;
        }
        renderChatThread(threadMessages);
        renderPrimaryStatus(data);
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

        updateConnectionChrome();
        updateIconGlow();
      } catch (_) {
        markBackendReachability(false);
        updateConnectionChrome();
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
        persistMicAutoMode();
        syncListenButtonLabel();
        updateIconGlow();
        return;
      }

      const micReady = await ensureMicPermission({ keepStreamAlive: FORCE_FALLBACK_STT });
      if (!micReady) {
        micAutoMode = false;
        persistMicAutoMode();
        syncListenButtonLabel();
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
          pauseSpeechForInterruption('speechstart');
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
          if (speechInterruptionPending) {
            if (isLikelyEchoTranscript(transcript)) {
              noteMicEvent('recognition-resume', 'echo-after-pause');
              addMicDebug('recognition:resume-echo', transcript.slice(0, 48));
              logTranscriptDrop('echo_after_pause', transcript, 'interruption_probe');
              resumeSpeechAfterInterruption('echo');
              return;
            }
            if (!isLikelyIntentionalInterruption(transcript, confidence)) {
              noteMicEvent('recognition-resume', 'weak-after-pause');
              addMicDebug('recognition:resume-weak', transcript.slice(0, 48));
              logTranscriptDrop('weak_after_pause', transcript, 'interruption_probe');
              resumeSpeechAfterInterruption('weak-transcript');
              return;
            }
            noteMicEvent('recognition-interrupt', transcript.slice(0, 24));
            addMicDebug('recognition:confirmed-interrupt', transcript.slice(0, 48));
            cancelSpeechForInterruption('confirmed-transcript');
          }
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

          await submitConversationTurn(transcript, 'voice');
          syncListenButtonLabel();
          updateConnectionChrome();
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
            syncListenButtonLabel();
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
            syncListenButtonLabel();
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
          syncListenButtonLabel();
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
        cameraSettingsStatus.textContent = window.isSecureContext
          ? 'Camera API is unavailable in this runtime.'
          : 'Camera blocked because this page is not running in a secure context.';
        healthState.cameraOk = false;
        updateCameraSettingsUi();
        updateIconGlow();
        await reportFrontendMediaStatus(
          'camera',
          deriveMediaFailureStatus(null, true),
          describeMediaStartupFailure('camera', null, true),
        );
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
        await reportFrontendMediaStatus(
          'camera',
          'watching',
          activeLabel ? `Camera watcher active${activeLabel}.` : 'Camera watcher active.',
          { selected_device_label: firstTrack?.label || null },
        );
      } catch (error) {
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
        const failureDetail = describeMediaStartupFailure('camera', error);
        const failureStatus = deriveMediaFailureStatus(error);
        cameraSettingsStatus.textContent = failureStatus === 'insecure_context'
          ? 'Camera blocked because this page is not running in a secure context.'
          : 'Unable to start camera. Check permission and selected device.';
        healthState.cameraOk = false;
        updateCameraSettingsUi();
        updateIconGlow();
        await reportFrontendMediaStatus('camera', failureStatus, failureDetail);
      }
    }

    document.getElementById('speakBtn').addEventListener('click', speakNow);
    document.getElementById('cameraBtn').addEventListener('click', sendCameraEvent);
    if (operatorModeBtn) {
      operatorModeBtn.addEventListener('click', () => setUiMode('operator'));
    }
    if (debugModeBtn) {
      debugModeBtn.addEventListener('click', () => setUiMode('debug'));
    }
    if (openDebugModeBtn) {
      openDebugModeBtn.addEventListener('click', () => {
        setUiMode('debug');
        setSecondaryTab('diagnostics');
      });
    }
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
      if (micAutoMode || micListening || micStartInFlight || micRestartPending) {
        const shouldStopRecognition = Boolean(recognition && (micListening || micStartInFlight));
        micAutoMode = false;
        persistMicAutoMode();
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
        resetSpeechInterruptionState();
        if (shouldStopRecognition) {
          recognition.stop();
        }
        statusEl.textContent = 'Listening paused.';
        syncListenButtonLabel();
        updateConnectionChrome();
        updateIconGlow();
        return;
      }

      micAutoMode = true;
      persistMicAutoMode();
      micRestartPending = false;
      micStartTimeoutStreak = 0;
      micStartFailureStreak = 0;
      micShortRunStreak = 0;
      micUnstableCycleCount = 0;
      syncListenButtonLabel();
      updateConnectionChrome();
      listenOnce();
    });
    chatSendBtn.addEventListener('click', sendTextChat);
    chatClearBtn.addEventListener('click', () => {
      chatLocallyCleared = true;
      chatLocalClearCutoffIso = new Date().toISOString();
      chatLocalClearNotice = {
        role: 'mim',
        content: 'Text chat cleared. Ready for your next message.',
        created_at: chatLocalClearCutoffIso,
        interaction_mode: 'text',
        message_type: 'system_summary',
        attachment: null,
      };
      chatThreadMessages = [];
      renderChatThread([], { force: true });
    });
    chatInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendTextChat();
      }
    });
    if (chatMicBtn) {
      chatMicBtn.addEventListener('click', async () => {
        listenBtn.click();
      });
    }
    if (autoListenToggle) {
      autoListenToggle.checked = micAutoMode;
      autoListenToggle.addEventListener('change', () => {
        if (autoListenToggle.checked) {
          micAutoMode = true;
          persistMicAutoMode();
          syncListenButtonLabel();
          updateConnectionChrome();
          listenOnce();
        } else {
          micAutoMode = false;
          persistMicAutoMode();
          if (recognition && micListening) recognition.stop();
          syncListenButtonLabel();
          updateConnectionChrome();
        }
      });
    }
    if (imageUploadBtn && imageUploadInput) {
      imageUploadBtn.addEventListener('click', () => imageUploadInput.click());
      imageUploadInput.addEventListener('change', () => {
        const file = imageUploadInput.files && imageUploadInput.files[0] ? imageUploadInput.files[0] : null;
        if (file) {
          setComposerImage(file);
        }
      });
    }
    if (imageRemoveBtn) {
      imageRemoveBtn.addEventListener('click', resetComposerImage);
    }
    if (chatDropzone) {
      ['dragenter', 'dragover'].forEach((eventName) => {
        chatDropzone.addEventListener(eventName, (event) => {
          event.preventDefault();
          chatDropzone.classList.add('active');
        });
      });
      ['dragleave', 'drop'].forEach((eventName) => {
        chatDropzone.addEventListener(eventName, (event) => {
          event.preventDefault();
          if (eventName !== 'drop') {
            chatDropzone.classList.remove('active');
          }
        });
      });
      chatDropzone.addEventListener('drop', (event) => {
        chatDropzone.classList.remove('active');
        const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0] ? event.dataTransfer.files[0] : null;
        if (file) {
          setComposerImage(file);
        }
      });
    }
    secondaryTabs.forEach((button) => {
      button.addEventListener('click', () => setSecondaryTab(String(button.dataset.tab || 'status')));
    });
    if (programQueueToggleBtn) {
      programQueueToggleBtn.addEventListener('click', () => {
        showFullProgramQueue = !showFullProgramQueue;
        const initiative = latestUiState && typeof latestUiState.initiative_driver === 'object'
          ? latestUiState.initiative_driver
          : {};
        renderProgramQueue(initiative);
      });
    }
    quickActionButtons.forEach((button) => {
      button.addEventListener('click', async () => {
        const quickMessage = buildQuickActionMessage(button);
        if (!quickMessage) return;
        const priorText = button.textContent;
        button.disabled = true;
        button.textContent = 'Working...';
        try {
          if (chatInput) {
            chatInput.value = quickMessage;
          }
          await submitConversationTurn(quickMessage, 'quick_action');
        } finally {
          button.disabled = false;
          button.textContent = priorText;
          if (chatInput) {
            chatInput.value = '';
          }
        }
      });
    });
    if (selfTestToggleListenerBtn) {
      selfTestToggleListenerBtn.addEventListener('click', () => {
        listenBtn.click();
      });
    }
    if (selfTestRunBtn) {
      selfTestRunBtn.addEventListener('click', async () => {
        setSelfTestSummary('Running self-test...', 'warn');
        if (selfTestTimestamp) {
          selfTestTimestamp.textContent = 'Running self-test now…';
        }
        await enumerateMicDevices();
        await enumerateCameraDevices();
        await ensureMicPermission({ keepStreamAlive: Boolean(micAutoMode || FORCE_FALLBACK_STT) });
        if (!cameraStream || !cameraStream.active) {
          await startCameraWatcher();
        }
        selfTestLastRunAt = Date.now();
        await refreshState();
        renderSelfTestPanel();
      });
    }

    updateIconGlow();
    addMicDebug('ui-boot', 'mim-ui-tightened-v1');
    if (micEventEl) {
      micEventEl.textContent = `Recent voice event: ui-boot @ ${new Date().toLocaleTimeString()}`;
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
    setUiMode(uiMode);
    updateCameraSettingsUi();
    applyVoiceSettings();
    if (threadStatusChip) {
      threadStatusChip.textContent = `Primary thread: ${safeText(preloadedChatThread.primary_thread, textChatSessionId)}`;
    }
    renderChatThread(chatThreadMessages, { force: true });
    refreshState();
    syncListenButtonLabel();
    updateConnectionChrome();
    renderSelfTestPanel();
    if (micAutoMode) {
      statusEl.textContent = 'Restoring full-time listener...';
      listenOnce();
    } else {
      statusEl.textContent = 'Listening paused. Press Turn Listener On to begin.';
      ensureMicPermission().then(() => enumerateMicDevices());
    }
    startCameraWatcher();
    setInterval(refreshState, 2000);
  </script>
</body>
</html>
""".replace("__MIM_PRELOADED_CHAT_THREAD__", json.dumps(preloaded_chat_thread, default=str))

@router.get("/mim/ui/media/{asset_name}")
async def mim_ui_media(request: Request, asset_name: str) -> FileResponse:
  ensure_authenticated_mimtod_api_request(request)
  safe_name = Path(str(asset_name or "")).name
  if not safe_name or safe_name != str(asset_name or ""):
    raise HTTPException(status_code=404, detail="media_not_found")
  path = _ensure_mim_ui_media_root() / safe_name
  if not path.exists() or not path.is_file():
    raise HTTPException(status_code=404, detail="media_not_found")
  return FileResponse(path)

@router.post("/mim/ui/chat/upload-image")
async def mim_ui_upload_image(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    ensure_authenticated_mimtod_api_request(request)
    fields, upload = _parse_mim_ui_multipart_form(
        content_type=request.headers.get("content-type", ""),
        body=await request.body(),
    )
    prompt = str(fields.get("prompt") or "")
    session_key = str(fields.get("session_key") or MIM_PRIMARY_THREAD_KEY)
    normalized_session_key = (
        str(session_key or _mim_ui_primary_thread_key()).strip()
        or _mim_ui_primary_thread_key()
    )
    normalized_prompt = str(prompt or "").strip()
    raw_bytes = bytes(upload.get("content") or b"")
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="empty_image_upload")
    if len(raw_bytes) > MIM_UI_MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image_too_large")

    filename = str(upload.get("filename") or "upload").strip() or "upload"
    content_type = str(upload.get("content_type") or "").strip().lower()
    extension = _mim_ui_image_extension(content_type, filename)
    media_root = _ensure_mim_ui_media_root()
    asset_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex}{extension}"
    asset_path = media_root / asset_name
    asset_path.write_bytes(raw_bytes)
    digest = sha256(raw_bytes).hexdigest()
    attachment = {
      "kind": "image",
      "url": _mim_ui_media_url(asset_name),
      "thumbnail_url": _mim_ui_media_url(asset_name),
      "mime_type": content_type or mimetypes.guess_type(asset_name)[0] or "image/png",
      "filename": filename or asset_name,
      "size_bytes": len(raw_bytes),
      "sha256": digest,
    }

    session = await upsert_interface_session(
      session_key=normalized_session_key,
      actor="operator",
      source="mim_ui",
      channel="chat",
      status="active",
      context_json={"primary_thread": True},
      metadata_json={"conversation_session_id": normalized_session_key},
      db=db,
    )

    prompt_text = normalized_prompt or "Please inspect this image and describe what stands out."
    operator_content = normalized_prompt or f"Shared image: {attachment['filename']}"
    _, image_message = await append_interface_message(
      session_key=normalized_session_key,
      actor="operator",
      source="mim_ui",
      direction="inbound",
      role="operator",
      content=operator_content,
      parsed_intent="image_message",
      confidence=1.0,
      requires_approval=False,
      metadata_json={
        "message_type": "user",
        "interaction_mode": "image",
        "attachment": attachment,
        "image_prompt": normalized_prompt,
      },
      db=db,
    )

    reply_text = (
      "Image received and attached to the primary thread. "
      "Multimodal analysis is not configured in this runtime yet."
    )
    analysis_status = "stored"
    try:
      analysis_text = await _analyze_image_with_openai(
        image_bytes=raw_bytes,
        mime_type=str(attachment["mime_type"]),
        prompt=prompt_text,
      )
      if analysis_text:
        reply_text = analysis_text
        analysis_status = "analyzed"
    except Exception as exc:  # noqa: BLE001
      analysis_status = str(exc) or "analysis_unavailable"

    _, reply_message = await append_interface_message(
      session_key=normalized_session_key,
      actor="mim",
      source="mim_ui",
      direction="outbound",
      role="mim",
      content=reply_text,
      parsed_intent="image_analysis",
      confidence=1.0,
      requires_approval=False,
      metadata_json={
        "message_type": "mim_reply",
        "interaction_mode": "image",
        "attachment": attachment,
        "analysis_status": analysis_status,
        "linked_message_id": int(image_message.id),
      },
      db=db,
    )

    await db.commit()
    return {
      "session": to_interface_session_out(session),
      "message": _serialize_chat_message(to_interface_message_out(image_message)),
      "reply": _serialize_chat_message(to_interface_message_out(reply_message)),
      "attachment": attachment,
    }


@router.get("/mim/ui/state")
async def mim_ui_state(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    ensure_authenticated_mimtod_api_request(request)
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
    latest_input_resolution = None
    if latest_input_event is not None:
      latest_input_resolution = (
        (
          await db.execute(
            select(InputEventResolution)
            .where(InputEventResolution.input_event_id == int(latest_input_event.id))
            .limit(1)
          )
        )
        .scalars()
        .first()
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
    latest_conversation_reply = _conversation_reply_text_from_resolution(
        latest_input_resolution
    )
    if latest_conversation_reply:
        inquiry_prompt = latest_conversation_reply
        latest_output_text = latest_conversation_reply
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
    frontend_media = _frontend_media_snapshot(now=now)
    frontend_media_issue_summary = _frontend_media_issue_summary(frontend_media)
    if frontend_media_issue_summary:
      runtime_health = dict(runtime_health)
      runtime_health["status"] = "degraded"
      summary_prefix = str(runtime_health.get("summary") or "Runtime health requires attention.").strip()
      runtime_health["summary"] = f"{summary_prefix} Frontend media: {frontend_media_issue_summary}."
    runtime_health["frontend_media"] = frontend_media
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
    initiative_driver = await build_initiative_status(db=db)
    chat_thread = await _load_mim_ui_chat_thread(db=db)

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
    operator_reasoning["initiative_driver"] = initiative_driver
    operator_reasoning["program_status"] = (
      initiative_driver.get("program_status")
      if isinstance(initiative_driver.get("program_status"), dict)
      else {}
    )
    authoritative_request = load_authoritative_request_status(shared_root=SHARED_RUNTIME_ROOT)
    tod_truth_reconciliation = _build_tod_truth_reconciliation_snapshot(
      initiative_driver=initiative_driver,
      authoritative_request=authoritative_request,
      shared_root=SHARED_RUNTIME_ROOT,
    )
    operator_reasoning["tod_truth_reconciliation"] = tod_truth_reconciliation
    system_activity = _build_system_activity_snapshot(
      initiative_driver=initiative_driver,
      operator_reasoning=operator_reasoning,
      runtime_health=runtime_health,
      runtime_recovery=runtime_recovery,
      authoritative_request=authoritative_request,
      collaboration_progress=collaboration_progress,
      dispatch_telemetry=dispatch_telemetry,
      tod_decision_process=tod_decision_process,
    )
    operator_reasoning["system_activity"] = system_activity

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
            "tod_truth_reconciliation_visibility",
            "self_evolution_operator_visibility",
            "self_evolution_operator_actionability",
            "self_evolution_operator_commands",
            "self_evolution_operator_command_context",
            "self_evolution_natural_language_development",
            "runtime_health_visibility",
            "initiative_driver_visibility",
            "activity_truth_visibility",
        ],
        "inquiry_prompt": inquiry_prompt,
        "operator_reasoning": operator_reasoning,
        "system_activity": system_activity,
        "tod_truth_reconciliation": system_activity.get("tod_truth_reconciliation") if isinstance(system_activity.get("tod_truth_reconciliation"), dict) else tod_truth_reconciliation,
          "initiative_driver": initiative_driver,
        "collaboration_progress": collaboration_progress,
        "dispatch_telemetry": dispatch_telemetry,
        "mim_arm_dispatch_telemetry": dispatch_telemetry,
        "primitive_request": authoritative_request,
        "chat_thread": chat_thread,
        "frontend_media": frontend_media,
        "conversation_context": {
            "environment_now": environment_now,
          "program_status_summary": str(
            (
              initiative_driver.get("program_status")
              if isinstance(initiative_driver.get("program_status"), dict)
              else {}
            ).get("summary")
            or ""
          ).strip(),
          "program_status": (
            initiative_driver.get("program_status")
            if isinstance(initiative_driver.get("program_status"), dict)
            else {}
          ),
            "active_goal": goal_summary,
            "initiative_active_objective": str(
              initiative_driver.get("active_objective", {}).get("display_title")
              or initiative_driver.get("active_objective", {}).get("title")
              or ""
            ).strip(),
            "initiative_active_task": str(
              initiative_driver.get("active_task", {}).get("display_title")
              or initiative_driver.get("active_task", {}).get("title")
              or ""
            ).strip(),
            "initiative_next_task": str(
              initiative_driver.get("next_task", {}).get("display_title")
              or initiative_driver.get("next_task", {}).get("title")
              or ""
            ).strip(),
            "initiative_activity_state": str(
              initiative_driver.get("activity", {}).get("state") or ""
            ).strip(),
            "initiative_activity_label": str(
              initiative_driver.get("activity", {}).get("label") or ""
            ).strip(),
            "initiative_activity_summary": str(
              initiative_driver.get("activity", {}).get("summary") or ""
            ).strip(),
            "initiative_progress_summary": str(
              initiative_driver.get("progress", {}).get("summary") or ""
            ).strip(),
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
            "self_evolution_operator_command_method": str(
              operator_reasoning.get("self_evolution", {}).get("primary_operator_command", {}).get("method") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}).get("primary_operator_command", {}), dict)
            else "",
            "self_evolution_operator_command_path": str(
              operator_reasoning.get("self_evolution", {}).get("primary_operator_command", {}).get("path") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}).get("primary_operator_command", {}), dict)
            else "",
            "self_evolution_operator_command_purpose": str(
              operator_reasoning.get("self_evolution", {}).get("primary_operator_command", {}).get("purpose") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}).get("primary_operator_command", {}), dict)
            else "",
            "self_evolution_natural_language_development_summary": str(
              operator_reasoning.get("self_evolution", {}).get("natural_language_development_summary") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_natural_language_development_active_slice": str(
              operator_reasoning.get("self_evolution", {}).get("natural_language_development_active_slice") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_natural_language_development_progress": str(
              operator_reasoning.get("self_evolution", {}).get("natural_language_development_progress") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_natural_language_development_next_step": str(
              operator_reasoning.get("self_evolution", {}).get("natural_language_development_next_step") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_natural_language_development_pass_bar": str(
              operator_reasoning.get("self_evolution", {}).get("natural_language_development_pass_bar") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_natural_language_development_continuation": str(
              operator_reasoning.get("self_evolution", {}).get("natural_language_development_continuation") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_natural_language_development_whats_next": str(
              operator_reasoning.get("self_evolution", {}).get("natural_language_development_whats_next") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_natural_language_development_skill_id": str(
              operator_reasoning.get("self_evolution", {}).get("natural_language_development_skill_id") or ""
            ).strip()
            if isinstance(operator_reasoning.get("self_evolution", {}), dict)
            else "",
            "self_evolution_natural_language_development_skill_title": str(
              operator_reasoning.get("self_evolution", {}).get("natural_language_development_skill_title") or ""
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
            "frontend_media_summary": frontend_media_issue_summary,
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
async def get_runtime_recovery_summary(request: Request) -> dict:
  ensure_authenticated_mimtod_api_request(request)
  return runtime_recovery_service.get_summary()


@router.post("/mim/ui/runtime-recovery-events")
async def record_runtime_recovery_event(http_request: Request, request: RuntimeRecoveryEventRequest) -> dict:
  ensure_authenticated_mimtod_api_request(http_request)
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


@router.post("/mim/ui/frontend-media-status")
async def record_frontend_media_status(http_request: Request, request: FrontendMediaStatusRequest) -> dict:
  ensure_authenticated_mimtod_api_request(http_request)
  lane = str(request.lane or "").strip().lower()
  if lane not in {"camera", "microphone"}:
    raise HTTPException(status_code=400, detail="lane must be camera or microphone")
  event = _record_frontend_media_status(request)
  return {
    "status": "recorded",
    "event": event,
    "frontend_media": _frontend_media_snapshot(),
  }


@router.get("/mim/ui/health")
async def mim_ui_health(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
  ensure_authenticated_mimtod_api_request(request)
  snapshot = await build_mim_ui_health_snapshot(db=db)
  frontend_media = _frontend_media_snapshot()
  issue_summary = _frontend_media_issue_summary(frontend_media)
  snapshot["frontend_media"] = frontend_media
  if issue_summary:
    snapshot["status"] = "degraded" if str(snapshot.get("status") or "").strip().lower() == "healthy" else snapshot.get("status")
    summary_prefix = str(snapshot.get("summary") or "Runtime health requires attention.").strip()
    snapshot["summary"] = f"{summary_prefix} Frontend media: {issue_summary}."
  latest = snapshot.get("latest") if isinstance(snapshot.get("latest"), dict) else {}
  if frontend_media.get("camera") and isinstance(latest.get("camera"), dict):
    latest["camera"]["frontend_status"] = frontend_media.get("camera")
  if frontend_media.get("microphone") and isinstance(latest.get("microphone"), dict):
    latest["microphone"]["frontend_status"] = frontend_media.get("microphone")
  snapshot["latest"] = latest
  return snapshot
