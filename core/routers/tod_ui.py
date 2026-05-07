from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from core.config import PROJECT_ROOT, settings
from core.tod_execution_loop import build_execution_loop_contract_artifacts, execute_bounded_local_inspection


router = APIRouter(tags=["tod-ui"])

WORKSPACE_ROOT = PROJECT_ROOT.parent if (PROJECT_ROOT.parent / "scripts").exists() else PROJECT_ROOT
SHARED_RUNTIME_ROOT = WORKSPACE_ROOT / "runtime" / "shared"
SHARED_STATE_ROOT = WORKSPACE_ROOT / "shared_state"
TOD_CONSOLE_CHAT_ROOT = SHARED_RUNTIME_ROOT / "tod_console_chat"
TOD_CONSOLE_CHAT_MEDIA_ROOT = SHARED_RUNTIME_ROOT / "tod_console_chat_media"
DIALOG_ROOT = SHARED_RUNTIME_ROOT / "dialog"
TOD_COPILOT_HANDOFF_ROOT = SHARED_RUNTIME_ROOT / "tod_copilot_handoff"
REMOTE_RECOVERY_ROOT = SHARED_RUNTIME_ROOT / "remote_recovery"
TOD_OPERATOR_ACTION_ROOT = SHARED_RUNTIME_ROOT / "tod_operator_actions"
DIALOG_SCHEMA_VERSION = "mim-tod-dialog-v1"
TOD_UI_ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
TOD_UI_MAX_IMAGE_BYTES = 2 * 1024 * 1024
TOD_EXECUTION_FEEDBACK_DEFAULT_BASE_URL = "http://127.0.0.1:18001"
TOD_OPERATOR_ACTION_LATEST_PATH = TOD_OPERATOR_ACTION_ROOT / "TOD_OPERATOR_ACTION.latest.json"
TOD_OPERATOR_ACTION_LOG_PATH = TOD_OPERATOR_ACTION_ROOT / "TOD_OPERATOR_ACTION.log.jsonl"
TOD_OPERATOR_EVIDENCE_PATH = TOD_OPERATOR_ACTION_ROOT / "TOD_OPERATOR_EVIDENCE.latest.json"
TOD_OPERATOR_ACTION_TIMEOUT_SECONDS = 180
UI_BUILD_ID = "task-identity-contention-repair-v1"
LOCAL_EXECUTOR_BINDING = "scripts/engines/LocalExecutionEngine.ps1::Invoke-LocalExecutionEngine"
LOCAL_EXECUTOR_BINDING_COMMAND = "execute-chat-task"
LEDGER_PHASE_A_COVERAGE_ARTIFACT = "runtime/shared/TOD_MIM_LEDGER_PHASE_A_COVERAGE.latest.json"

OPERATOR_ACTION_SPECS: dict[str, dict[str, Any]] = {
    "refresh_status": {
        "label": "Refresh Status",
        "description": "Run the shared-state sync to refresh current TOD and MIM status artifacts.",
    },
    "run_shared_truth_reconciliation": {
        "label": "Reconcile Truth",
        "description": "Rebuild the authoritative TOD/MIM shared-truth artifact from current evidence.",
    },
    "start_next_task": {
        "label": "Start Next Task",
        "description": "Publish a real TOD execution request for the current authoritative objective/task.",
    },
    "force_replay_current_task": {
        "label": "Force Replay",
        "description": "Run the forced execution replay wrapper for the current objective/task.",
        "requires_confirmation": True,
        "confirmation_text": "Force replay the current TOD task? This republishes the active task replay request.",
    },
    "validate_current_task": {
        "label": "Validate Task",
        "description": "Run the current task's next validation command when it matches the safe allowlist.",
    },
    "recover_stale_state": {
        "label": "Recover Stale State",
        "description": "Run the bounded TOD/MIM remote recovery wrapper.",
        "requires_confirmation": True,
        "confirmation_text": "Run stale-state recovery now? This can republish bridge and recovery artifacts.",
    },
    "show_evidence": {
        "label": "Show Evidence",
        "description": "Refresh the operator evidence snapshot artifact used by both consoles.",
    },
    "pause_current_objective": {
        "label": "Pause",
        "description": "Pause the active TOD objective on this control surface and publish the paused runtime state.",
        "requires_confirmation": True,
        "confirmation_text": "Pause the active TOD objective on this console?",
    },
    "resume_current_objective": {
        "label": "Resume",
        "description": "Resume the active TOD objective by publishing a fresh execution request and runtime transition.",
    },
    "rollback_current_task": {
        "label": "Rollback",
        "description": "Apply the latest published rollback hint for the active task and publish rollback evidence.",
        "requires_confirmation": True,
        "confirmation_text": "Apply the latest published rollback point for the active TOD task?",
    },
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _first_existing_payload(*paths: Path) -> tuple[dict[str, Any], str]:
    for path in paths:
        payload = _load_json(path)
        if payload:
            return payload, str(path)
    return {}, ""


def _compact_text(value: Any, limit: int = 220) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _trim_message_text(value: Any, limit: int = 2000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _repo_workspace_root() -> Path:
    return PROJECT_ROOT.parent if (PROJECT_ROOT.parent / "scripts").exists() else PROJECT_ROOT


def _script_path(script_name: str) -> Path:
    return _repo_workspace_root() / "scripts" / script_name


def _python_runner() -> str:
    return str(sys.executable or shutil.which("python") or "python")


def _resolve_operator_action_ids(state: dict[str, Any]) -> tuple[str, str]:
    execution = state.get("execution") if isinstance(state.get("execution"), dict) else {}
    shared_truth = state.get("shared_truth") if isinstance(state.get("shared_truth"), dict) else {}
    source_paths = state.get("source_paths") if isinstance(state.get("source_paths"), dict) else {}
    active_task_path = str(source_paths.get("active_task") or "").strip()
    active_task_payload = _load_json(Path(active_task_path)) if active_task_path else {}
    objective_alignment = state.get("objective_alignment") if isinstance(state.get("objective_alignment"), dict) else {}
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    shared_truth_superseded = bool(execution.get("shared_truth_superseded"))
    if shared_truth_superseded:
        objective_id = _pick_first_text(
            execution.get("objective_id"),
            active_task_payload.get("objective_id"),
            objective_alignment.get("tod_current_objective"),
            live_task.get("objective_id"),
            live_task.get("normalized_objective_id"),
            shared_truth.get("objective_id"),
        )
        task_id = _pick_first_text(
            execution.get("task_id"),
            active_task_payload.get("task_id"),
            active_task_payload.get("id"),
            live_task.get("task_id"),
            shared_truth.get("task_id"),
            shared_truth.get("current_task_id"),
        )
    else:
        objective_id = _pick_first_text(
            shared_truth.get("objective_id"),
            execution.get("objective_id"),
            active_task_payload.get("objective_id"),
            objective_alignment.get("tod_current_objective"),
            live_task.get("objective_id"),
            live_task.get("normalized_objective_id"),
        )
        task_id = _pick_first_text(
            shared_truth.get("task_id"),
            shared_truth.get("current_task_id"),
            execution.get("task_id"),
            active_task_payload.get("task_id"),
            active_task_payload.get("id"),
            live_task.get("task_id"),
        )
    return str(objective_id or "").strip(), str(task_id or "").strip()


def _resolve_safe_validation_command(state: dict[str, Any]) -> list[str]:
    next_validation = str(_next_validation_check(state) or "").strip()
    if not next_validation:
        return []
    python_match = re.match(r"^(?:[^\s]+python(?:\.exe)?|python(?:\.exe)?)\s+-m\s+unittest\s+(.+)$", next_validation, re.IGNORECASE)
    if python_match:
        remainder = str(python_match.group(1) or "").strip()
        try:
            parsed = shlex.split(remainder, posix=False)
        except Exception:
            parsed = remainder.split()
        return [_python_runner(), "-m", "unittest", *parsed]
    powershell_match = re.match(
        r"^(?:powershell(?:\.exe)?)\s+-NoProfile\s+-ExecutionPolicy\s+Bypass\s+-File\s+([\w./\\:-]+(?:\.ps1))(?P<rest>.*)$",
        next_validation,
        re.IGNORECASE,
    )
    if not powershell_match:
        return []
    script_value = str(powershell_match.group(1) or "").strip().strip('"')
    rest = str(powershell_match.group("rest") or "").strip()
    script_path = _repo_workspace_root() / script_value.replace("/", os.sep)
    if not script_path.exists():
        return []
    rest_args = shlex.split(rest, posix=False) if rest else []
    return [_powershell_runner(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), *rest_args]


def _operator_action_specs(state: dict[str, Any]) -> list[dict[str, Any]]:
    objective_id, task_id = _resolve_operator_action_ids(state)
    execution = state.get("execution") if isinstance(state.get("execution"), dict) else {}
    latest_action = _latest_operator_action_payload()
    rollback_metadata = _resolve_rollback_metadata(state)
    paused = str(execution.get("activity_state") or "").strip().lower() == "paused" or (
        _operator_action_applies_to_task(latest_action, objective_id, task_id)
        and str(latest_action.get("action") or "").strip() == "pause_current_objective"
        and str(latest_action.get("status") or "").strip().lower() not in {"failed", "started"}
    )
    actions: list[dict[str, Any]] = []
    for action_id, spec in OPERATOR_ACTION_SPECS.items():
        action_entry = {
            "id": action_id,
            "label": str(spec.get("label") or action_id).strip(),
            "description": str(spec.get("description") or "").strip(),
            "requires_confirmation": bool(spec.get("requires_confirmation")),
            "confirmation_text": str(spec.get("confirmation_text") or "").strip(),
            "enabled": True,
            "disabled_reason": "",
        }
        if action_id == "force_replay_current_task" and (not objective_id or not task_id):
            action_entry["enabled"] = False
            action_entry["disabled_reason"] = "Current objective/task identity is missing."
        if action_id == "validate_current_task" and not _resolve_safe_validation_command(state):
            action_entry["enabled"] = False
            action_entry["disabled_reason"] = "No safe validation command is available for the active task."
        if action_id == "pause_current_objective":
            if not objective_id or not task_id:
                action_entry["enabled"] = False
                action_entry["disabled_reason"] = "Current objective/task identity is missing."
            elif paused:
                action_entry["enabled"] = False
                action_entry["disabled_reason"] = "The active objective is already paused on this console."
        if action_id == "resume_current_objective":
            if not objective_id or not task_id:
                action_entry["enabled"] = False
                action_entry["disabled_reason"] = "Current objective/task identity is missing."
            elif not paused:
                action_entry["enabled"] = False
                action_entry["disabled_reason"] = "Resume becomes available after the current objective is paused."
        if action_id == "rollback_current_task":
            rollback_paths = _parse_copy_item_restore_paths(rollback_metadata.get("hint") or "")
            if not objective_id or not task_id:
                action_entry["enabled"] = False
                action_entry["disabled_reason"] = "Current objective/task identity is missing."
            elif rollback_metadata.get("state") not in {"available", "ready", "recommended"}:
                action_entry["enabled"] = False
                action_entry["disabled_reason"] = "No published rollback point is currently available for the active task."
            elif not all(rollback_paths):
                action_entry["enabled"] = False
                action_entry["disabled_reason"] = "The published rollback hint is missing or points outside the workspace."
        actions.append(action_entry)
    return actions


def _build_objective_cards(state: dict[str, Any]) -> list[dict[str, Any]]:
    execution = state.get("execution") if isinstance(state.get("execution"), dict) else {}
    shared_truth = state.get("shared_truth") if isinstance(state.get("shared_truth"), dict) else {}
    status = state.get("status") if isinstance(state.get("status"), dict) else {}
    alignment = state.get("objective_alignment") if isinstance(state.get("objective_alignment"), dict) else {}
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    conversation = state.get("conversation") if isinstance(state.get("conversation"), dict) else {}
    operator_actions = state.get("operator_actions") if isinstance(state.get("operator_actions"), list) else []
    planner_state = state.get("planner_state") if isinstance(state.get("planner_state"), dict) else {}
    live_state = execution.get("live_state") if isinstance(execution.get("live_state"), dict) else {}

    operator_action_map = {
        str(item.get("id") or "").strip(): item
        for item in operator_actions
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    quick_actions = {
        str(item.get("id") or "").strip(): item
        for item in (conversation.get("quick_actions") if isinstance(conversation.get("quick_actions"), list) else [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }

    planner_is_primary = bool(planner_state.get("available") and planner_state.get("is_newer_than_executor"))
    objective_id = _pick_first_text(
        live_task.get("objective_id") if planner_is_primary else "",
        live_task.get("normalized_objective_id") if planner_is_primary else "",
        execution.get("objective_id"),
        shared_truth.get("objective_id"),
        live_task.get("objective_id"),
        live_task.get("normalized_objective_id"),
        alignment.get("tod_current_objective"),
        alignment.get("mim_objective_active"),
    )
    task_id = _pick_first_text(
        live_task.get("task_id") if planner_is_primary else "",
        live_task.get("request_id") if planner_is_primary else "",
        execution.get("task_id"),
        shared_truth.get("task_id"),
        shared_truth.get("current_task_id"),
        live_task.get("task_id"),
    )
    title = _pick_first_text(
        live_task.get("title") if planner_is_primary else "",
        execution.get("title"),
        shared_truth.get("objective_title"),
        live_task.get("title"),
        objective_id,
        "No active objective",
    )
    summary = _pick_first_text(
        planner_state.get("summary") if planner_is_primary else "",
        live_state.get("status_detail"),
        execution.get("activity_summary"),
        execution.get("summary"),
        status.get("summary"),
        shared_truth.get("reason"),
        "No active objective is currently published on the TOD control surface.",
    )
    phase_progress = execution.get("phase_progress") if isinstance(execution.get("phase_progress"), dict) else {}
    milestones = phase_progress.get("milestones") if isinstance(phase_progress.get("milestones"), list) else []
    milestone_summary = [
        {
            "id": str(item.get("id") or "").strip(),
            "label": str(item.get("label") or "").strip(),
            "status": str(item.get("status") or "unknown").strip(),
            "complete": bool(item.get("complete")),
        }
        for item in milestones
        if isinstance(item, dict)
    ]

    card_actions: list[dict[str, Any]] = []
    for action_id in ("start_next_task", "show_evidence", "validate_current_task", "pause_current_objective", "resume_current_objective", "rollback_current_task"):
        spec = operator_action_map.get(action_id, {})
        card_actions.append(
            {
                "id": action_id,
                "label": str(spec.get("label") or action_id).strip(),
                "mode": "operator_action",
                "enabled": bool(spec.get("enabled")),
                "disabled_reason": str(spec.get("disabled_reason") or "").strip(),
                "requires_confirmation": bool(spec.get("requires_confirmation")),
                "confirmation_text": str(spec.get("confirmation_text") or "").strip(),
            }
        )

    card_actions.extend(
        [
            {
                "id": "show_plan",
                "label": "Show Plan",
                "mode": "local_view",
                "enabled": bool(phase_progress.get("available") or milestone_summary),
                "disabled_reason": "No phase progress plan is currently published." if not bool(phase_progress.get("available") or milestone_summary) else "",
                "plan_summary": str(phase_progress.get("summary") or "").strip(),
                "milestones": milestone_summary,
            },
        ]
    )

    handoff = quick_actions.get("send-to-copilot", {})
    card_actions.append(
        {
            "id": "send_to_codex",
            "label": str(handoff.get("label") or "Send To Codex").strip(),
            "mode": "chat_handoff",
            "enabled": bool(handoff),
            "disabled_reason": "Codex handoff is not published on this surface." if not handoff else "",
            "prompt": str(handoff.get("prompt") or "").strip(),
        }
    )

    return [
        {
            "id": str(objective_id or "no-active-objective").strip() or "no-active-objective",
            "objective_id": str(objective_id or "").strip(),
            "task_id": str(task_id or "").strip(),
            "title": _compact_text(title, 180),
            "summary": _compact_text(summary, 220),
            "status": _pick_first_text(planner_state.get("status_label") if planner_is_primary else "", execution.get("activity_label"), status.get("label"), "Idle"),
            "activity_state": str(planner_state.get("status") if planner_is_primary else execution.get("activity_state") or "idle").strip(),
            "live_state": {
                "status": str(live_state.get("status") or "").strip(),
                "status_label": str(live_state.get("status_label") or "").strip(),
                "status_detail": str(live_state.get("status_detail") or "").strip(),
                "stuck_on": str(live_state.get("stuck_on") or "").strip(),
                "next_to_progress": str(live_state.get("next_to_progress") or "").strip(),
                "is_stuck": bool(live_state.get("is_stuck") is True),
                "is_working_background": bool(live_state.get("is_working_background") is True),
                "mim_priority": bool(live_state.get("mim_priority") is True),
                "barriers": [
                    str(item).strip()
                    for item in (live_state.get("barriers") if isinstance(live_state.get("barriers"), list) else [])
                    if str(item).strip()
                ],
                "escalation_channels": [
                    str(item).strip()
                    for item in (live_state.get("escalation_channels") if isinstance(live_state.get("escalation_channels"), list) else [])
                    if str(item).strip()
                ],
            },
            "phase_progress": {
                "available": bool(phase_progress.get("available")),
                "label": str(phase_progress.get("label") or "").strip(),
                "percent_complete": int(phase_progress.get("percent_complete") or 0),
                "next_gate": str(phase_progress.get("next_gate") or "").strip(),
                "summary": str(phase_progress.get("summary") or "").strip(),
                "milestones": milestone_summary,
            },
            "planner_state": {
                "available": bool(planner_state.get("available")),
                "status": str(planner_state.get("status") or "").strip(),
                "status_label": str(planner_state.get("status_label") or "").strip(),
                "summary": str(planner_state.get("summary") or "").strip(),
                "current_step": str(planner_state.get("current_step") or "").strip(),
                "next_step": str(planner_state.get("next_step") or "").strip(),
                "updated_at": str(planner_state.get("updated_at") or "").strip(),
                "updated_age": str(planner_state.get("updated_age") or "").strip(),
                "assigned_executor": str(planner_state.get("assigned_executor") or "").strip(),
                "requested_outcome": str(planner_state.get("requested_outcome") or "").strip(),
                "is_newer_than_executor": bool(planner_state.get("is_newer_than_executor")),
            },
            "executor_state": {
                "status": str(execution.get("activity_state") or "").strip(),
                "status_label": str(execution.get("activity_label") or "").strip(),
                "summary": _compact_text(execution.get("activity_summary") or execution.get("summary"), 220),
                "current_action": str(execution.get("current_action") or "").strip(),
                "next_validation": str(execution.get("next_validation") or "").strip(),
                "updated_at": str(execution.get("updated_at") or "").strip(),
                "updated_age": str(execution.get("updated_age") or "").strip(),
            },
            "artifacts": {
                "updated_at": str(execution.get("updated_at") or "").strip(),
                "updated_age": str(execution.get("updated_age") or "").strip(),
                "files_changed": execution.get("files_changed") if isinstance(execution.get("files_changed"), list) else [],
                "validation_checks": execution.get("validation_checks") if isinstance(execution.get("validation_checks"), list) else [],
                "rollback_state": str(execution.get("rollback_state") or "not_needed").strip(),
                "recovery_state": str(execution.get("recovery_state") or "not_needed").strip(),
            },
            "actions": card_actions,
        }
    ]


def _read_jsonl_records(path: Path, limit: int = 8) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _artifact_timestamp_for_path(path_text: str) -> str:
    path_value = str(path_text or "").strip()
    if not path_value:
        return ""
    path = Path(path_value)
    if not path.exists():
        return ""
    payload = _load_json(path)
    if payload:
        timestamp_value = _pick_first_text(payload.get("generated_at"), payload.get("updated_at"), payload.get("emitted_at"))
        if timestamp_value:
            return timestamp_value
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _load_recent_operator_actions(limit: int = 8) -> list[dict[str, Any]]:
    return _read_jsonl_records(TOD_OPERATOR_ACTION_LOG_PATH, limit=limit)


def _build_operator_evidence(state: dict[str, Any]) -> dict[str, Any]:
    state = state if isinstance(state, dict) else {}
    source_paths = state.get("source_paths") if isinstance(state.get("source_paths"), dict) else {}
    execution = state.get("execution") if isinstance(state.get("execution"), dict) else {}
    shared_truth = state.get("shared_truth") if isinstance(state.get("shared_truth"), dict) else {}
    status = state.get("status") if isinstance(state.get("status"), dict) else {}
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    objective_id, task_id = _resolve_operator_action_ids(state)
    execution_result_path = str(source_paths.get("execution_result") or "").strip()
    validation_path = str(source_paths.get("validation_result") or "").strip()
    active_task_path = str(source_paths.get("active_task") or "").strip()
    execution_result_payload = _load_json(Path(execution_result_path)) if execution_result_path else {}
    validation_payload = _load_json(Path(validation_path)) if validation_path else {}
    active_task_payload = _load_json(Path(active_task_path)) if active_task_path else {}
    latest_action = _load_json(TOD_OPERATOR_ACTION_LATEST_PATH)
    files_changed = execution_result_payload.get("files_changed") if isinstance(execution_result_payload.get("files_changed"), list) else []
    commands_run = execution_result_payload.get("commands_run") if isinstance(execution_result_payload.get("commands_run"), list) else []
    validation_checks = execution.get("validation_checks") if isinstance(execution.get("validation_checks"), list) else []
    shared_truth_superseded = bool(execution.get("shared_truth_superseded"))
    stall_signal = execution.get("stall_signal") if isinstance(execution.get("stall_signal"), dict) else {}
    objective_title = _pick_first_text(
        execution.get("title"),
        active_task_payload.get("objective_title"),
        active_task_payload.get("title"),
        live_task.get("title"),
        shared_truth.get("objective_title"),
    ) if shared_truth_superseded else _pick_first_text(
        shared_truth.get("objective_title"),
        active_task_payload.get("objective_title"),
        active_task_payload.get("title"),
        execution.get("title"),
        live_task.get("title"),
    )
    task_title = _pick_first_text(
        active_task_payload.get("display_title"),
        active_task_payload.get("title"),
        execution.get("title"),
        execution.get("task_focus"),
        live_task.get("title"),
        shared_truth.get("task_title"),
    ) if shared_truth_superseded else _pick_first_text(
        shared_truth.get("task_title"),
        active_task_payload.get("display_title"),
        active_task_payload.get("title"),
        execution.get("title"),
        execution.get("task_focus"),
        live_task.get("title"),
    )
    blocker_code = _pick_first_text(
        stall_signal.get("level"),
        execution.get("blocker_code"),
        shared_truth.get("blocker_code"),
    ) if shared_truth_superseded else _pick_first_text(
        shared_truth.get("blocker_code"),
        execution.get("blocker_code"),
    )
    blocker_detail = _pick_first_text(
        execution.get("blocker_detail"),
        status.get("summary"),
        shared_truth.get("blocker_detail"),
        execution.get("blocker_detail"),
    ) if shared_truth_superseded else _pick_first_text(
        shared_truth.get("blocker_detail"),
        execution.get("blocker_detail"),
        status.get("summary"),
    )
    artifact_timestamps = {
        key: _artifact_timestamp_for_path(str(path_text or ""))
        for key, path_text in source_paths.items()
        if key in {"active_objective", "active_task", "validation_result", "execution_result", "execution_truth", "shared_truth", "remote_recovery"}
    }
    return {
        "active_objective": {
            "id": objective_id,
            "title": objective_title,
        },
        "active_task": {
            "id": task_id,
            "title": task_title,
        },
        "changed_files": files_changed[:12],
        "commands_run": commands_run[:12],
        "validation_status": _pick_first_text(execution.get("validation_status"), validation_payload.get("status"), validation_payload.get("validation_status")),
        "validation_checks": validation_checks[:8],
        "blocker_code": blocker_code,
        "blocker_detail": blocker_detail,
        "artifact_timestamps": artifact_timestamps,
        "latest_action": latest_action if isinstance(latest_action, dict) else {},
        "shared_truth_state": _pick_first_text(shared_truth.get("state"), shared_truth.get("status")),
        "next_validation": _next_validation_check(state),
    }


def _write_operator_evidence_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    payload = _build_operator_evidence(state)
    TOD_OPERATOR_ACTION_ROOT.mkdir(parents=True, exist_ok=True)
    TOD_OPERATOR_EVIDENCE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return payload


def _run_operator_command(command: list[str], timeout_seconds: int = TOD_OPERATOR_ACTION_TIMEOUT_SECONDS) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(_repo_workspace_root()),
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_seconds)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_text = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
        stderr_text = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
        return {
            "ok": False,
            "status": "timeout",
            "message": "The operator action timed out before completion.",
            "command": command,
            "stdout_excerpt": _trim_message_text(stdout_text, 1600),
            "stderr_excerpt": _trim_message_text(stderr_text, 1600),
            "exit_code": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "failed",
            "message": _compact_text(exc, 220),
            "command": command,
            "stdout_excerpt": "",
            "stderr_excerpt": "",
            "exit_code": None,
        }
    stdout_excerpt = _trim_message_text(completed.stdout, 1600)
    stderr_excerpt = _trim_message_text(completed.stderr, 1600)
    message = stdout_excerpt or stderr_excerpt or ("Command completed." if completed.returncode == 0 else "Command failed.")
    return {
        "ok": completed.returncode == 0,
        "status": "completed" if completed.returncode == 0 else "failed",
        "message": _compact_text(message, 220),
        "command": command,
        "stdout_excerpt": stdout_excerpt,
        "stderr_excerpt": stderr_excerpt,
        "exit_code": int(completed.returncode),
    }


def _run_operator_command_sequence(commands: list[list[str]], timeout_seconds: int = TOD_OPERATOR_ACTION_TIMEOUT_SECONDS) -> dict[str, Any]:
    executed: list[list[str]] = []
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    for command in commands:
        result = _run_operator_command(command, timeout_seconds=timeout_seconds)
        executed.append(command)
        stdout_excerpt = str(result.get("stdout_excerpt") or "").strip()
        stderr_excerpt = str(result.get("stderr_excerpt") or "").strip()
        if stdout_excerpt:
            stdout_parts.append(stdout_excerpt)
        if stderr_excerpt:
            stderr_parts.append(stderr_excerpt)
        if not bool(result.get("ok")):
            return {
                "ok": False,
                "status": str(result.get("status") or "failed").strip() or "failed",
                "message": str(result.get("message") or "Command failed.").strip() or "Command failed.",
                "command": executed,
                "stdout_excerpt": _trim_message_text("\n\n".join(stdout_parts), 1600),
                "stderr_excerpt": _trim_message_text("\n\n".join(stderr_parts), 1600),
                "exit_code": result.get("exit_code"),
            }
    message = stdout_parts[-1] if stdout_parts else (stderr_parts[-1] if stderr_parts else "Commands completed.")
    return {
        "ok": True,
        "status": "completed",
        "message": _compact_text(message, 220),
        "command": executed,
        "stdout_excerpt": _trim_message_text("\n\n".join(stdout_parts), 1600),
        "stderr_excerpt": _trim_message_text("\n\n".join(stderr_parts), 1600),
        "exit_code": 0,
    }


def _run_reconcile_shared_truth_action() -> dict[str, Any]:
    script_path = _script_path("reconcile_tod_mim_shared_truth.py")
    if not script_path.exists():
        return {
            "ok": False,
            "status": "failed",
            "message": "Shared-truth reconciliation script is missing.",
            "artifact_paths": [],
            "command": [],
        }
    command = [_python_runner(), str(script_path)]
    result = _run_operator_command(command)
    result["artifact_paths"] = [str(SHARED_RUNTIME_ROOT / "TOD_MIM_SHARED_TRUTH.latest.json")]
    return result


def _can_run_full_shared_state_sync() -> bool:
    required_paths = (
        _script_path("Invoke-TODSharedStateSync.ps1"),
        _script_path("TOD.ps1"),
        WORKSPACE_ROOT / "tod" / "config" / "tod-config.json",
        WORKSPACE_ROOT / "tod" / "data" / "state.json",
    )
    return all(path.exists() for path in required_paths)


def _run_refresh_status_action() -> dict[str, Any]:
    sync_script_path = _script_path("Invoke-TODSharedStateSync.ps1")
    if _can_run_full_shared_state_sync():
        command = [_powershell_runner(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(sync_script_path), "-RefreshAgentMimReadiness"]
        result = _run_operator_command(command)
        result["artifact_paths"] = [str(SHARED_RUNTIME_ROOT / "TOD_SHARED_STATE.latest.json")]
        return result

    export_script_path = _script_path("export_mim_context.py")
    rebuild_script_path = _script_path("rebuild_tod_integration_status.py")
    if not export_script_path.exists() or not rebuild_script_path.exists():
        missing = []
        if not sync_script_path.exists():
            missing.append("Invoke-TODSharedStateSync.ps1")
        if not export_script_path.exists():
            missing.append("export_mim_context.py")
        if not rebuild_script_path.exists():
            missing.append("rebuild_tod_integration_status.py")
        missing_text = ", ".join(missing) if missing else "required refresh helpers"
        return {
            "ok": False,
            "status": "failed",
            "message": f"Refresh helpers are missing: {missing_text}.",
            "artifact_paths": [],
            "command": [],
        }

    commands = [
        [_python_runner(), str(export_script_path), "--output-dir", str(SHARED_RUNTIME_ROOT)],
        [_python_runner(), str(rebuild_script_path)],
    ]
    result = _run_operator_command_sequence(commands)
    result["artifact_paths"] = [
        str(SHARED_RUNTIME_ROOT / "MIM_CONTEXT_EXPORT.latest.json"),
        str(SHARED_RUNTIME_ROOT / "TOD_INTEGRATION_STATUS.latest.json"),
    ]
    return result


def _age_seconds(value: Any) -> float | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _format_age(value: Any) -> str:
    seconds = _age_seconds(value)
    if seconds is None:
        return "Unknown"
    if seconds < 90:
        return f"{int(round(seconds))}s ago"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{int(round(minutes))}m ago"
    hours = minutes / 60.0
    if hours < 48:
        return f"{hours:.1f}h ago"
    days = hours / 24.0
    return f"{days:.1f}d ago"


def _normalize_objective_token(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    match = re.search(r"(\d+)$", text)
    if match:
        return match.group(1)
    if text.startswith("objective-"):
        return text[len("objective-") :]
    return text


def _objective_request_slug(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return re.sub(r"[^a-z0-9._-]+", "-", text).strip("-._")


def _same_objective(left: Any, right: Any) -> bool:
    left_token = _normalize_objective_token(left)
    right_token = _normalize_objective_token(right)
    return bool(left_token and right_token and left_token == right_token)


def _extract_objective_from_prefix(message: str) -> str:
    """Extract objective name from OBJECTIVE: prefix in message.

    Handles user-issued chat commands of the form:
        OBJECTIVE: TOD-NEW-OBJECTIVE-NAME  [optional Goal: / Title: etc.]
    Returns the first token after OBJECTIVE:, or empty string if not found.
    """
    text = str(message or "").strip()

    # Accept both line-start directives and inline one-line prompts such as:
    # "tod complete this objective: OBJECTIVE: TOD-MESSAGE-LEDGER-COVERAGE-REPORT"
    # Choose the last OBJECTIVE label to avoid earlier natural-language phrases.
    inline_matches = list(re.finditer(r"(?i)\bOBJECTIVE\s*:\s*(.+)", text))
    if inline_matches:
        name_raw = inline_matches[-1].group(1).strip().splitlines()[0].strip()
    else:
        m = re.match(r"(?i)^\s*OBJECTIVE\s*:\s*(.+)", text)
        if not m:
            return ""
        name_raw = m.group(1).strip().splitlines()[0].strip()

    # Normalize accidental nested labels such as "OBJECTIVE: OBJECTIVE: X".
    nested = re.match(r"(?i)^\s*OBJECTIVE\s*:\s*(.+)$", name_raw)
    while nested:
        name_raw = nested.group(1).strip()
        nested = re.match(r"(?i)^\s*OBJECTIVE\s*:\s*(.+)$", name_raw)

    # Strip any trailing labeled-field suffix (e.g. "Goal:", "Title:", "Mission:")
    name_raw = re.split(r"\s+(?:GOAL|TITLE|MISSION|PRIMARY\s+OUTCOME)\s*:", name_raw, flags=re.IGNORECASE)[0].strip()
    # Take the first whitespace-delimited token as the objective identifier
    name_token = name_raw.split()[0] if name_raw else ""
    return name_token or ""


def _message_declares_new_objective(message: str, authoritative_objective_id: str) -> bool:
    """Return True when message opens with OBJECTIVE: <name> that differs from the active objective.

    Handles user-issued chat commands of the form:
        OBJECTIVE: TOD-NEW-OBJECTIVE-NAME  [optional Goal: / Title: etc.]
    without requiring an explicit OBJECTIVE_ID: label.
    """
    name_token = _extract_objective_from_prefix(message)
    if not name_token:
        return False
    if not authoritative_objective_id:
        return True
    return not _same_objective(name_token, authoritative_objective_id)



def _same_task_identity(left: Any, right: Any) -> bool:
    left_text = str(left or "").strip().lower()
    right_text = str(right or "").strip().lower()
    return bool(left_text and right_text and left_text == right_text)


def _should_reuse_live_task_identity(live_task: dict[str, Any], prompt_objective_id: str, authoritative_task_id: str = "") -> bool:
    if not isinstance(live_task, dict) or not live_task:
        return False
    request_id = str(live_task.get("request_id") or "").strip()
    task_id = str(live_task.get("task_id") or "").strip()
    if not request_id and not task_id:
        return False
    desired_task_id = str(authoritative_task_id or "").strip()
    live_identity = task_id or request_id
    if desired_task_id and live_identity and not _same_task_identity(live_identity, desired_task_id):
        return False
    if not prompt_objective_id:
        return True
    prompt_text = str(prompt_objective_id or "").strip().lower()
    live_objective_id = str(
        live_task.get("objective_id")
        or live_task.get("normalized_objective_id")
        or ""
    ).strip()
    if prompt_text:
        request_text = request_id.lower()
        task_text = task_id.lower()
        live_objective_text = live_objective_id.lower()
        if prompt_text in request_text or prompt_text in task_text or prompt_text == live_objective_text:
            return True
    if not live_objective_id:
        return True
    return _same_objective(prompt_objective_id, live_objective_id)


def _select_runtime_live_task_request(integration_live_task: dict[str, Any], active_task: dict[str, Any]) -> dict[str, Any]:
    live_task = integration_live_task if isinstance(integration_live_task, dict) else {}
    runtime_task = active_task if isinstance(active_task, dict) else {}
    runtime_request_id = str(runtime_task.get("request_id") or "").strip()
    runtime_task_id = str(runtime_task.get("task_id") or "").strip()
    if not runtime_request_id and not runtime_task_id:
        return live_task

    runtime_objective = str(
        runtime_task.get("objective_id")
        or runtime_task.get("normalized_objective_id")
        or ""
    ).strip()
    runtime_identity = runtime_task_id or runtime_request_id
    live_objective = str(
        live_task.get("objective_id")
        or live_task.get("normalized_objective_id")
        or ""
    ).strip()
    live_identity = str(live_task.get("task_id") or live_task.get("request_id") or "").strip()
    if live_identity:
        # The live request packet is authoritative for request identity. Only let
        # executor runtime details fill gaps when they refer to the same request.
        if not runtime_identity or _same_task_identity(runtime_identity, live_identity):
            return live_task
        if live_objective and runtime_objective and not _same_objective(live_objective, runtime_objective):
            return live_task
    if live_task and _same_objective(runtime_objective, live_objective) and (not runtime_identity or not live_identity or _same_task_identity(runtime_identity, live_identity)):
        return live_task

    runtime_generated_at = str(runtime_task.get("updated_at") or runtime_task.get("generated_at") or "").strip()
    return {
        **live_task,
        "request_id": runtime_request_id or str(live_task.get("request_id") or "").strip(),
        "task_id": runtime_task_id or str(live_task.get("task_id") or "").strip(),
        "objective_id": runtime_objective or str(live_task.get("objective_id") or "").strip(),
        "normalized_objective_id": str(
            runtime_task.get("normalized_objective_id")
            or _normalize_objective_token(runtime_objective)
            or live_task.get("normalized_objective_id")
            or ""
        ).strip(),
        "generated_at": runtime_generated_at or str(live_task.get("generated_at") or "").strip(),
        "promotion_applied": bool(live_task.get("promotion_applied") is True),
        "promotion_reason": str(live_task.get("promotion_reason") or "").strip(),
    }


def _derive_task_identity_contention(
    *,
    canonical_objective: str,
    live_task_request: dict[str, Any],
    active_task_payload: dict[str, Any],
    listener_decision: dict[str, Any],
    decision_payload: dict[str, Any],
) -> dict[str, Any]:
    live_task = live_task_request if isinstance(live_task_request, dict) else {}
    active_task = active_task_payload if isinstance(active_task_payload, dict) else {}
    listener = listener_decision if isinstance(listener_decision, dict) else {}
    runtime_decision = decision_payload if isinstance(decision_payload, dict) else {}

    authoritative_objective_id = _pick_first_text(
        canonical_objective,
        active_task.get("objective_id"),
        active_task.get("normalized_objective_id"),
        live_task.get("objective_id"),
        live_task.get("normalized_objective_id"),
    )
    request_objective_id = _pick_first_text(
        live_task.get("objective_id"),
        live_task.get("normalized_objective_id"),
    )
    active_task_id = _pick_first_text(active_task.get("task_id"), active_task.get("request_id"))
    request_task_id = _pick_first_text(live_task.get("task_id"), live_task.get("request_id"))
    active_source = _pick_first_text(active_task.get("source_service"), active_task.get("source"))
    request_source = _pick_first_text(live_task.get("source_service"), live_task.get("source"))
    last_writer = request_source or active_source

    objective_aligned = bool(
        _same_objective(authoritative_objective_id, request_objective_id)
        or (
            canonical_objective
            and _same_objective(canonical_objective, request_objective_id)
            and _same_objective(canonical_objective, authoritative_objective_id or canonical_objective)
        )
    )
    task_mismatch = bool(active_task_id and request_task_id and not _same_task_identity(active_task_id, request_task_id))
    listener_reason = _pick_first_text(listener.get("reason_code"), runtime_decision.get("reason_code")).lower()
    listener_state = _pick_first_text(listener.get("execution_state"), runtime_decision.get("execution_state")).lower()
    listener_rejected = listener_state == "rejected" or listener_reason in {"objective_mismatch", "external_coordination_blocker"}
    detected = bool(objective_aligned and task_mismatch and listener_rejected)

    active_generated_at = _pick_latest_timestamp(active_task.get("updated_at"), active_task.get("generated_at"))
    request_generated_at = _pick_latest_timestamp(live_task.get("generated_at"), live_task.get("updated_at"))
    active_dt = _parse_timestamp(active_generated_at)
    request_dt = _parse_timestamp(request_generated_at)
    active_newer = bool(active_dt and request_dt and active_dt > request_dt)
    active_status = str(active_task.get("status") or "").strip().lower()
    active_not_terminal = active_status not in {"completed", "complete", "failed", "rejected", "superseded", "cancelled", "canceled", "expired"}
    request_from_watchdog = request_source == "tod_watchdog_autorepair"
    active_from_console = active_source.startswith("tod-ui")
    safe_self_repair = bool(detected and active_newer and active_not_terminal and request_from_watchdog and active_from_console)

    mismatch_type = "task_id_mismatch_same_objective" if detected else ""
    summary = (
        "Task ID mismatch inside the same objective. Console dispatch and watchdog repair are writing different task identities."
        if detected
        else ""
    )
    repair_step = (
        "Preserve the newer console task identity, suppress stale watchdog restore for this pair, republish execute-chat-task, then retry listener dispatch once."
        if detected
        else ""
    )

    return {
        "detected": detected,
        "reason_code": mismatch_type,
        "summary": summary,
        "blocker_type": "task_identity_contention" if detected else "",
        "mismatch_type": mismatch_type,
        "authoritative_objective_id": authoritative_objective_id,
        "request_objective_id": request_objective_id,
        "active_task_id": active_task_id,
        "request_task_id": request_task_id,
        "source_service": request_source,
        "last_writer": last_writer,
        "recommended_repair": repair_step,
        "safe_self_repair": safe_self_repair,
        "active_generated_at": active_generated_at,
        "request_generated_at": request_generated_at,
    }


def _record_task_identity_event(event: str, contention: dict[str, Any], details: dict[str, Any] | None = None) -> None:
    payload = {
        "generated_at": _utc_now_iso(),
        "action": event,
        "status": "completed",
        "objective_id": str(contention.get("authoritative_objective_id") or "").strip(),
        "task_id": str(contention.get("active_task_id") or "").strip(),
        "request_id": str(contention.get("request_task_id") or "").strip(),
        "source_service": str(contention.get("source_service") or "").strip(),
    }
    if isinstance(details, dict) and details:
        payload["details"] = details
    _record_operator_action(payload)


def _record_executor_binding_event(event: str, objective_id: str, task_id: str, details: dict[str, Any] | None = None) -> None:
    payload = {
        "generated_at": _utc_now_iso(),
        "action": event,
        "status": "completed",
        "objective_id": str(objective_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "source_service": "tod-ui-executor-binding-repair",
    }
    if isinstance(details, dict) and details:
        payload["details"] = details
    _record_operator_action(payload)


def _is_ledger_coverage_task(live_task: dict[str, Any], execution: dict[str, Any]) -> bool:
    metadata = live_task.get("metadata_json") if isinstance(live_task.get("metadata_json"), dict) else {}
    text_blob = " ".join(
        str(item or "")
        for item in (
            live_task.get("objective_id"),
            live_task.get("normalized_objective_id"),
            live_task.get("task_id"),
            live_task.get("title"),
            live_task.get("scope"),
            live_task.get("summary"),
            live_task.get("requested_outcome"),
            metadata.get("task_title"),
            metadata.get("task_acceptance_criteria"),
            execution.get("objective_id"),
            execution.get("task_id"),
            execution.get("title"),
            execution.get("summary"),
            execution.get("task_focus"),
        )
    ).lower()
    if "tod-message-ledger-coverage-report" in text_blob:
        return True
    return bool(("ledger" in text_blob or "message-ledger" in text_blob) and ("coverage" in text_blob or "phase a" in text_blob))


def _resolve_local_executor_mode() -> str:
    for mode in ("validation_only", "report_generation"):
        if mode == "validation_only":
            return mode
    return ""


def _attempt_executor_binding_materialization(
    live_task: dict[str, Any],
    execution: dict[str, Any],
    planner_state: dict[str, Any],
) -> dict[str, Any]:
    live_task = live_task if isinstance(live_task, dict) else {}
    execution = execution if isinstance(execution, dict) else {}
    planner_state = planner_state if isinstance(planner_state, dict) else {}
    metadata = live_task.get("metadata_json") if isinstance(live_task.get("metadata_json"), dict) else {}

    objective_id = str(live_task.get("objective_id") or execution.get("objective_id") or planner_state.get("objective_id") or "").strip()
    task_id = str(live_task.get("task_id") or live_task.get("request_id") or execution.get("task_id") or planner_state.get("task_id") or "").strip()
    planner_status = str(planner_state.get("status") or "").strip().lower()
    assigned_executor = str(live_task.get("assigned_executor") or metadata.get("assigned_executor") or planner_state.get("assigned_executor") or "").strip()
    selected_executor = str(live_task.get("selected_executor") or metadata.get("selected_executor") or "").strip()
    expected_executor = str(live_task.get("expected_executor") or metadata.get("expected_executor") or "").strip()
    active_engine = str(live_task.get("active_engine") or metadata.get("active_engine") or "").strip()
    executor_binding = str(live_task.get("executor_binding") or metadata.get("executor_binding") or "").strip()
    bounded_mode = str(live_task.get("bounded_edit_mode") or metadata.get("bounded_edit_mode") or "").strip()
    target_artifact_path = str(live_task.get("target_artifact_path") or metadata.get("target_artifact_path") or "").strip()

    if not expected_executor:
        expected_executor = "local" if _is_ledger_coverage_task(live_task, execution) else (assigned_executor or "local")

    missing_fields: list[str] = []
    if not selected_executor:
        missing_fields.append("selected_executor")
    if not active_engine:
        missing_fields.append("active_engine")
    if not executor_binding:
        missing_fields.append("executor_binding")
    if not bounded_mode:
        missing_fields.append("bounded_edit_mode")

    message = "Task identity is repaired, but no executor binding was produced for the queued next step."
    next_repair = (
        "Materialize a local executor binding and republish execute-chat-task with selected_executor=local, "
        f"active_engine=local, and executor_binding={LOCAL_EXECUTOR_BINDING}."
    )
    result = {
        "attempted": False,
        "published": False,
        "materialized": False,
        "status": "not_needed",
        "reason_code": "",
        "message": "",
        "task_id": task_id,
        "objective_id": objective_id,
        "assigned_executor": assigned_executor,
        "selected_executor": selected_executor,
        "expected_executor": expected_executor,
        "active_engine": active_engine,
        "executor_binding": executor_binding,
        "bounded_edit_mode": bounded_mode,
        "task_category": str(live_task.get("task_category") or metadata.get("task_category") or "").strip(),
        "target_artifact_path": target_artifact_path,
        "missing_field_or_function": ", ".join(missing_fields),
        "next_executable_repair": next_repair,
        "updated_live_task_request": None,
    }

    if planner_status != "queued":
        result["status"] = "not_queued"
        return result
    if not missing_fields:
        result["status"] = "already_bound"
        result["materialized"] = True
        return result

    result["status"] = "missing"
    result["reason_code"] = "planner_queued_without_executor_binding"
    result["message"] = message
    _record_executor_binding_event(
        "executor_binding_missing_detected",
        objective_id,
        task_id,
        {
            "assigned_executor": assigned_executor,
            "missing_field_or_function": result["missing_field_or_function"],
            "planner_status": planner_status,
        },
    )

    local_capable = _is_ledger_coverage_task(live_task, execution)
    _record_executor_binding_event(
        "local_suitability_evaluated",
        objective_id,
        task_id,
        {
            "local_capable": local_capable,
            "expected_executor": expected_executor,
            "task_category": result["task_category"],
        },
    )
    if not local_capable:
        result["reason_code"] = "local_suitability_not_materialized"
        _record_executor_binding_event(
            "blocked_missing_local_executor_binding",
            objective_id,
            task_id,
            {
                "reason_code": result["reason_code"],
                "missing_field_or_function": result["missing_field_or_function"],
            },
        )
        return result

    local_mode = _resolve_local_executor_mode()
    if not local_mode:
        result["reason_code"] = "blocked_missing_local_executor_binding"
        result["missing_field_or_function"] = "bounded_edit_mode"
        _record_executor_binding_event(
            "blocked_missing_local_executor_binding",
            objective_id,
            task_id,
            {
                "reason_code": result["reason_code"],
                "missing_field_or_function": result["missing_field_or_function"],
            },
        )
        return result

    marker_path = SHARED_RUNTIME_ROOT / "TOD_EXECUTOR_BINDING_REPAIR.latest.json"
    repair_key = "|".join((objective_id, task_id, str(live_task.get("request_id") or "").strip()))
    previous = _load_json(marker_path)
    if str(previous.get("repair_key") or "").strip() == repair_key and bool(previous.get("attempted") is True):
        result["reason_code"] = "executor_binding_already_attempted"
        result["status"] = "already_attempted"
        return result

    generated_at = _utc_now_iso()
    request_id = str(live_task.get("request_id") or task_id or "").strip()
    request_path = SHARED_RUNTIME_ROOT / "MIM_TOD_TASK_REQUEST.latest.json"
    trigger_path = SHARED_RUNTIME_ROOT / "MIM_TO_TOD_TRIGGER.latest.json"
    request_payload = {
        **live_task,
        "generated_at": generated_at,
        "source": "tod-ui-executor-binding-repair-v1",
        "source_service": "tod-ui-executor-binding-repair",
        "request_id": request_id,
        "task_id": task_id,
        "objective_id": objective_id,
        "correlation_id": str(live_task.get("correlation_id") or task_id or request_id).strip(),
        "tod_action": "execute-chat-task",
        "task_classification": "validation/reporting/diagnostic",
        "task_category": "validation",
        "assigned_executor": "local",
        "selected_executor": "local",
        "expected_executor": "local",
        "active_engine": "local",
        "executor_binding": LOCAL_EXECUTOR_BINDING,
        "bounded_edit_mode": local_mode,
        "target_artifact_path": LEDGER_PHASE_A_COVERAGE_ARTIFACT,
        "scope": _pick_first_text(
            live_task.get("scope"),
            "Validate message-ledger Phase A coverage artifacts and publish the local coverage report.",
        ),
        "requested_outcome": _pick_first_text(
            live_task.get("requested_outcome"),
            "Publish runtime/shared/TOD_MIM_LEDGER_PHASE_A_COVERAGE.latest.json via LocalExecutionEngine.",
        ),
    }
    request_payload["metadata_json"] = {
        **metadata,
        "task_category": "validation",
        "assigned_executor": "local",
        "selected_executor": "local",
        "expected_executor": "local",
        "active_engine": "local",
        "executor_binding": LOCAL_EXECUTOR_BINDING,
        "bounded_edit_mode": local_mode,
        "target_artifact_path": LEDGER_PHASE_A_COVERAGE_ARTIFACT,
        "task_source": "executor_binding_repair",
    }
    trigger_payload = {
        "packet_type": "mim-to-tod-trigger-v1",
        "generated_at": generated_at,
        "emitted_at": generated_at,
        "source_actor": "MIM",
        "target_actor": "TOD",
        "source_service": "tod-ui-executor-binding-repair",
        "trigger": request_id or task_id,
        "artifact": request_path.name,
        "artifact_path": str(request_path),
        "artifact_sha256": hashlib.sha256(json.dumps(request_payload, indent=2, ensure_ascii=True).encode("utf-8")).hexdigest(),
        "task_id": task_id,
        "request_id": request_id,
        "correlation_id": str(request_payload.get("correlation_id") or "").strip(),
    }

    _record_executor_binding_event(
        "local_executor_binding_materialized",
        objective_id,
        task_id,
        {
            "selected_executor": "local",
            "active_engine": "local",
            "executor_binding": LOCAL_EXECUTOR_BINDING,
            "bounded_edit_mode": local_mode,
        },
    )
    _record_executor_binding_event("dispatch_retry_started", objective_id, task_id, {"tod_action": "execute-chat-task"})
    result["attempted"] = True
    try:
        _write_shared_json(request_path, request_payload)
        _write_shared_json(trigger_path, trigger_payload)
        _write_shared_json(
            marker_path,
            {
                "generated_at": generated_at,
                "repair_key": repair_key,
                "attempted": True,
                "published": True,
                "task_id": task_id,
                "objective_id": objective_id,
            },
        )
        result.update(
            {
                "published": True,
                "materialized": True,
                "status": "materialized",
                "reason_code": "local_executor_binding_materialized",
                "selected_executor": "local",
                "expected_executor": "local",
                "active_engine": "local",
                "executor_binding": LOCAL_EXECUTOR_BINDING,
                "bounded_edit_mode": local_mode,
                "task_category": "validation/reporting/diagnostic",
                "target_artifact_path": LEDGER_PHASE_A_COVERAGE_ARTIFACT,
                "updated_live_task_request": request_payload,
                "missing_field_or_function": "",
            }
        )
        _record_executor_binding_event("dispatch_retry_result", objective_id, task_id, {"status": "published"})
        _record_executor_binding_event("local_executor_invoked", objective_id, task_id, {"dispatch": "queued_for_listener"})
    except Exception as exc:
        result["reason_code"] = "blocked_missing_local_executor_binding"
        result["status"] = "blocked"
        result["message"] = _compact_text(exc, 220)
        _record_executor_binding_event(
            "dispatch_retry_result",
            objective_id,
            task_id,
            {"status": "failed", "error": result["message"]},
        )
        _record_executor_binding_event(
            "blocked_missing_local_executor_binding",
            objective_id,
            task_id,
            {"missing_field_or_function": result["missing_field_or_function"], "error": result["message"]},
        )
        _write_shared_json(
            marker_path,
            {
                "generated_at": generated_at,
                "repair_key": repair_key,
                "attempted": True,
                "published": False,
                "task_id": task_id,
                "objective_id": objective_id,
                "error": result["message"],
            },
        )

    return result


def _attempt_task_identity_self_repair(contention: dict[str, Any]) -> dict[str, Any]:
    marker_path = SHARED_RUNTIME_ROOT / "TOD_TASK_IDENTITY_REPAIR.latest.json"
    repair_key = "|".join(
        [
            str(contention.get("authoritative_objective_id") or "").strip(),
            str(contention.get("active_task_id") or "").strip(),
            str(contention.get("request_task_id") or "").strip(),
        ]
    )
    previous = _load_json(marker_path)
    if str(previous.get("repair_key") or "") == repair_key and bool(previous.get("attempted") is True):
        return {
            "attempted": False,
            "reason": "already_attempted_for_pair",
        }

    if not bool(contention.get("safe_self_repair") is True):
        return {
            "attempted": False,
            "reason": "unsafe_or_not_needed",
        }

    _record_task_identity_event("task_identity_contention_detected", contention)
    _record_task_identity_event("watchdog_restore_suppressed_newer_console_task", contention)
    _record_task_identity_event("canonical_task_identity_selected", contention)

    objective_id = str(contention.get("authoritative_objective_id") or "").strip()
    task_id = str(contention.get("active_task_id") or "").strip()
    local_capable = _is_ledger_coverage_task(
        {
            "objective_id": objective_id,
            "task_id": task_id,
            "title": "Resume authoritative console task identity",
            "scope": "Preserve newer same-objective console task identity and retry listener dispatch once.",
        },
        {},
    )
    local_mode = _resolve_local_executor_mode() if local_capable else ""
    generated_at = _utc_now_iso()
    request_path = SHARED_RUNTIME_ROOT / "MIM_TOD_TASK_REQUEST.latest.json"
    trigger_path = SHARED_RUNTIME_ROOT / "MIM_TO_TOD_TRIGGER.latest.json"
    request_payload = {
        "packet_type": "mim-tod-task-request-v1",
        "generated_at": generated_at,
        "source": "tod-ui-task-identity-self-repair-v1",
        "source_service": "tod-ui-task-identity-self-repair",
        "target": "TOD",
        "request_id": task_id,
        "task_id": task_id,
        "objective_id": objective_id,
        "correlation_id": task_id,
        "sequence": int(datetime.now(timezone.utc).timestamp() * 1000),
        "tod_action": "execute-chat-task",
        "canonical_lane_source": "task_identity_arbitration",
        "title": "Resume authoritative console task identity",
        "description": "Task identity contention repair republished the authoritative console task for the same objective.",
        "priority": "high",
        "scope": "Preserve newer same-objective console task identity and retry listener dispatch once.",
        "acceptance_criteria": "Listener accepts the canonical same-objective task identity.",
        "success_criteria": "Listener accepts the canonical same-objective task identity.",
        "requested_outcome": "Listener accepts the canonical same-objective task identity.",
        "task_classification": "validation/reporting/diagnostic" if local_capable and local_mode else "programming",
        "task_category": "validation" if local_capable and local_mode else "chat_execution",
        "assigned_executor": "local" if local_capable and local_mode else "codex",
    }
    if local_capable and local_mode:
        request_payload.update(
            {
                "selected_executor": "local",
                "expected_executor": "local",
                "active_engine": "local",
                "executor_binding": LOCAL_EXECUTOR_BINDING,
                "bounded_edit_mode": local_mode,
                "target_artifact_path": LEDGER_PHASE_A_COVERAGE_ARTIFACT,
            }
        )
    request_text = json.dumps(request_payload, indent=2, ensure_ascii=True)
    request_sha256 = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
    trigger_payload = {
        "packet_type": "mim-to-tod-trigger-v1",
        "generated_at": generated_at,
        "emitted_at": generated_at,
        "source_actor": "MIM",
        "target_actor": "TOD",
        "source_service": "tod-ui-task-identity-self-repair",
        "trigger": task_id,
        "artifact": request_path.name,
        "artifact_path": str(request_path),
        "artifact_sha256": request_sha256,
        "task_id": task_id,
        "request_id": task_id,
        "correlation_id": task_id,
    }

    _record_task_identity_event("listener_retry_started", contention)
    result = {
        "attempted": True,
        "published": False,
        "request_path": str(request_path),
        "trigger_path": str(trigger_path),
    }
    try:
        _write_shared_json(request_path, request_payload)
        _write_shared_json(trigger_path, trigger_payload)
        result["published"] = True
        result["reason"] = "published"
        _record_task_identity_event("listener_retry_result", contention, {"status": "published"})
    except Exception as exc:
        result["reason"] = "publish_failed"
        result["error"] = _compact_text(exc, 220)
        _record_task_identity_event("listener_retry_result", contention, {"status": "failed", "error": result["error"]})

    _write_shared_json(
        marker_path,
        {
            "generated_at": generated_at,
            "repair_key": repair_key,
            "attempted": True,
            "published": bool(result.get("published")),
            "reason": str(result.get("reason") or "").strip(),
            "objective_id": objective_id,
            "active_task_id": task_id,
            "request_task_id": str(contention.get("request_task_id") or "").strip(),
        },
    )
    return result


def _detect_phase_label(active_task: dict[str, Any], execution_result: dict[str, Any]) -> str:
    active_contract = active_task.get("execution_contract") if isinstance(active_task.get("execution_contract"), dict) else {}
    result_contract = execution_result.get("execution_contract") if isinstance(execution_result.get("execution_contract"), dict) else {}
    active_intake = active_contract.get("task_intake") if isinstance(active_contract.get("task_intake"), dict) else {}
    result_intake = result_contract.get("task_intake") if isinstance(result_contract.get("task_intake"), dict) else {}
    active_planner = active_contract.get("bounded_step_planner") if isinstance(active_contract.get("bounded_step_planner"), dict) else {}
    result_planner = result_contract.get("bounded_step_planner") if isinstance(result_contract.get("bounded_step_planner"), dict) else {}
    active_step = active_planner.get("active_step") if isinstance(active_planner.get("active_step"), dict) else {}
    result_step = result_planner.get("active_step") if isinstance(result_planner.get("active_step"), dict) else {}
    for candidate in (
        active_task.get("objective_id"),
        active_task.get("normalized_objective_id"),
        active_task.get("title"),
        active_task.get("task_focus"),
        active_task.get("summary"),
        active_intake.get("task_focus"),
        active_intake.get("title"),
        active_intake.get("mission"),
        active_intake.get("primary_outcome"),
        active_planner.get("summary"),
        active_step.get("title"),
        active_step.get("summary"),
        execution_result.get("objective_id"),
        execution_result.get("normalized_objective_id"),
        execution_result.get("title"),
        execution_result.get("summary"),
        result_intake.get("task_focus"),
        result_intake.get("title"),
        result_intake.get("mission"),
        result_intake.get("primary_outcome"),
        result_planner.get("summary"),
        result_step.get("title"),
        result_step.get("summary"),
    ):
        text = str(candidate or "").strip()
        if not text:
            continue
        match = re.search(r"\bphase[\s_-]*(\d+)\b", text, re.IGNORECASE)
        if match:
            return f"Phase {match.group(1)}"
    return "Current objective"


def _load_remote_recovery_payload() -> tuple[dict[str, Any], str]:
    return _first_existing_payload(
        REMOTE_RECOVERY_ROOT / "TOD_MIM_REMOTE_RECOVERY.latest.json",
        SHARED_RUNTIME_ROOT / "TOD_MIM_REMOTE_RECOVERY.latest.json",
    )


def _load_existing_execution_runtime_payloads() -> dict[str, dict[str, Any]]:
    payloads = {
        "active_objective": _load_json(SHARED_RUNTIME_ROOT / "TOD_ACTIVE_OBJECTIVE.latest.json"),
        "active_task": _load_json(SHARED_RUNTIME_ROOT / "TOD_ACTIVE_TASK.latest.json"),
        "activity": _load_json(SHARED_RUNTIME_ROOT / "TOD_ACTIVITY_STREAM.latest.json"),
        "validation": _load_json(SHARED_RUNTIME_ROOT / "TOD_VALIDATION_RESULT.latest.json"),
        "execution_result": _load_json(SHARED_RUNTIME_ROOT / "TOD_EXECUTION_RESULT.latest.json"),
        "execution_truth": _load_json(SHARED_RUNTIME_ROOT / "TOD_EXECUTION_TRUTH.latest.json"),
    }
    return payloads


def _latest_operator_action_payload() -> dict[str, Any]:
    return _load_json(TOD_OPERATOR_ACTION_LATEST_PATH)


def _operator_action_applies_to_task(record: dict[str, Any], objective_id: str, task_id: str) -> bool:
    if not isinstance(record, dict) or not record:
        return False
    record_objective_id = str(record.get("objective_id") or "").strip()
    record_task_id = str(record.get("task_id") or record.get("request_id") or "").strip()
    if objective_id and record_objective_id and not _same_objective(record_objective_id, objective_id):
        return False
    if task_id and record_task_id and not _same_task_identity(record_task_id, task_id):
        return False
    return True


def _resolve_rollback_metadata(state: dict[str, Any]) -> dict[str, str]:
    state = state if isinstance(state, dict) else {}
    source_paths = state.get("source_paths") if isinstance(state.get("source_paths"), dict) else {}
    execution = state.get("execution") if isinstance(state.get("execution"), dict) else {}
    execution_result_payload = _load_json(Path(str(source_paths.get("execution_result") or "").strip())) if str(source_paths.get("execution_result") or "").strip() else {}
    active_task_payload = _load_json(Path(str(source_paths.get("active_task") or "").strip())) if str(source_paths.get("active_task") or "").strip() else {}
    execution_result_evidence = execution_result_payload.get("execution_evidence") if isinstance(execution_result_payload.get("execution_evidence"), dict) else {}
    active_task_evidence = active_task_payload.get("execution_evidence") if isinstance(active_task_payload.get("execution_evidence"), dict) else {}
    rollback_state = _pick_first_text(
        execution_result_payload.get("rollback_state"),
        active_task_payload.get("rollback_state"),
        execution_result_evidence.get("rollback_state"),
        active_task_evidence.get("rollback_state"),
        execution.get("rollback_state"),
    )
    rollback_hint = _pick_first_text(
        execution_result_payload.get("rollback_hint"),
        active_task_payload.get("rollback_hint"),
        execution_result_evidence.get("rollback_hint"),
        active_task_evidence.get("rollback_hint"),
    )
    return {
        "state": str(rollback_state or "").strip(),
        "hint": str(rollback_hint or "").strip(),
    }


def _derive_planner_state(
    live_task: dict[str, Any],
    listener_decision: dict[str, Any],
    execution: dict[str, Any],
    latest_action: dict[str, Any],
) -> dict[str, Any]:
    live_task = live_task if isinstance(live_task, dict) else {}
    listener_decision = listener_decision if isinstance(listener_decision, dict) else {}
    execution = execution if isinstance(execution, dict) else {}
    latest_action = latest_action if isinstance(latest_action, dict) else {}

    objective_id = str(live_task.get("objective_id") or live_task.get("normalized_objective_id") or execution.get("objective_id") or "").strip()
    task_id = str(live_task.get("task_id") or live_task.get("request_id") or execution.get("task_id") or "").strip()
    title = _pick_first_text(live_task.get("title"), execution.get("title"), task_id, objective_id)
    if not (objective_id or task_id or title):
        return {
            "available": False,
            "updated_at": "",
            "updated_age": "Unknown",
            "is_newer_than_executor": False,
        }

    relevant_action = latest_action if _operator_action_applies_to_task(latest_action, objective_id, task_id) else {}
    updated_at = _pick_latest_timestamp(live_task.get("generated_at"), relevant_action.get("generated_at"))
    request_dt = _parse_timestamp(updated_at)
    executor_dt = _parse_timestamp(execution.get("updated_at"))
    same_task_as_executor = _same_task_identity(task_id, execution.get("task_id")) if task_id and execution.get("task_id") else False
    is_newer_than_executor = bool(
        request_dt
        and (
            executor_dt is None
            or request_dt > executor_dt
            or (task_id and execution.get("task_id") and not same_task_as_executor)
        )
    )

    action_name = str(relevant_action.get("action") or "").strip()
    decision_state = str(listener_decision.get("execution_state") or "").strip().lower()
    decision_outcome = str(listener_decision.get("decision_outcome") or "").strip().lower()
    if action_name == "pause_current_objective":
        status = "paused"
        status_label = "Paused"
        summary = _pick_first_text(relevant_action.get("message"), "Paused the active TOD objective on this console.")
        current_step = "Operator pause requested"
    elif action_name == "resume_current_objective":
        status = "resume_requested"
        status_label = "Resume Requested"
        summary = _pick_first_text(relevant_action.get("message"), "Published a resume request and waiting for fresh execution evidence.")
        current_step = "Resume request published"
    elif action_name == "rollback_current_task":
        status = "rollback_applied" if str(relevant_action.get("status") or "").strip().lower() in {"completed", "applied"} else "rollback_requested"
        status_label = "Rollback Applied" if status == "rollback_applied" else "Rollback Requested"
        summary = _pick_first_text(relevant_action.get("message"), "Applied the latest rollback point for the active task.")
        current_step = "Rollback control executed"
    elif action_name in {"start_next_task", "publish_task_execution_request"}:
        status = "queued"
        status_label = "Queued"
        summary = _pick_first_text(
            live_task.get("task_request"),
            live_task.get("requested_outcome"),
            relevant_action.get("message"),
            "Published a fresh execution request and waiting for executor evidence.",
        )
        current_step = "Execution request published"
    elif decision_state in {"ready_to_execute", "ready", "execute_now"} or decision_outcome == "execute":
        status = "ready"
        status_label = "Ready"
        summary = _pick_first_text(listener_decision.get("summary"), "The current task request is aligned and ready to execute.")
        current_step = "Planner accepted the current request"
    elif decision_state in {"waiting_on_dependency", "waiting"}:
        status = "waiting"
        status_label = "Waiting"
        summary = _pick_first_text(listener_decision.get("summary"), "The planner is waiting on a prerequisite before execution can continue.")
        current_step = "Planner is waiting on a prerequisite"
    elif decision_state == "rejected" or decision_outcome.startswith("reject"):
        status = "blocked"
        status_label = "Blocked"
        summary = _pick_first_text(listener_decision.get("summary"), "The planner rejected the current task request.")
        current_step = "Planner blocked the current request"
    else:
        status = "queued"
        status_label = "Queued"
        summary = _pick_first_text(listener_decision.get("summary"), "The current task request is queued and waiting for fresh execution evidence.")
        current_step = "Planner request is queued"

    return {
        "available": True,
        "request_id": str(live_task.get("request_id") or "").strip(),
        "task_id": task_id,
        "objective_id": objective_id,
        "title": _compact_text(title, 180),
        "assigned_executor": str(live_task.get("assigned_executor") or execution.get("selected_executor") or "codex").strip(),
        "requested_outcome": str(live_task.get("requested_outcome") or live_task.get("acceptance_criteria") or "").strip(),
        "status": status,
        "status_label": status_label,
        "summary": _compact_text(summary, 220),
        "current_step": _compact_text(current_step, 180),
        "next_step": _compact_text(listener_decision.get("next_step_recommendation") or execution.get("next_step") or "Wait for fresh execution evidence from the active task.", 220),
        "updated_at": updated_at,
        "updated_age": _format_age(updated_at),
        "is_newer_than_executor": is_newer_than_executor,
        "same_task_as_executor": same_task_as_executor,
    }


def _path_is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def _parse_copy_item_restore_paths(command_text: str) -> tuple[Path | None, Path | None]:
    text = str(command_text or "").strip()
    if not text:
        return None, None
    match = re.search(r"-Path\s+['\"](?P<src>[^'\"]+)['\"].*?-Destination\s+['\"](?P<dst>[^'\"]+)['\"]", text, re.IGNORECASE)
    if not match:
        return None, None
    source = Path(str(match.group("src") or "").strip())
    destination = Path(str(match.group("dst") or "").strip())
    if not source.exists():
        return None, None
    if not (_path_is_within(source, WORKSPACE_ROOT) and _path_is_within(destination, WORKSPACE_ROOT)):
        return None, None
    return source, destination


def _write_execution_runtime_transition(
    state: dict[str, Any],
    *,
    action_id: str,
    status: str,
    execution_state: str,
    summary: str,
    current_action: str,
    next_step: str,
    wait_reason: str,
    wait_target: str = "tod_operator_console",
    wait_target_label: str = "TOD operator console",
    rollback_state: str = "",
    recovery_state: str = "",
    command_output: str = "",
    extra_evidence: dict[str, Any] | None = None,
) -> list[str]:
    runtime_payloads = _load_existing_execution_runtime_payloads()
    active_objective_payload = dict(runtime_payloads.get("active_objective") or {})
    active_task_payload = dict(runtime_payloads.get("active_task") or {})
    activity_payload = dict(runtime_payloads.get("activity") or {})
    validation_payload = dict(runtime_payloads.get("validation") or {})
    execution_result_payload = dict(runtime_payloads.get("execution_result") or {})
    execution_truth_payload = dict(runtime_payloads.get("execution_truth") or {})

    execution = state.get("execution") if isinstance(state.get("execution"), dict) else {}
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    shared_truth = state.get("shared_truth") if isinstance(state.get("shared_truth"), dict) else {}
    objective_id, task_id = _resolve_operator_action_ids(state)
    updated_at = _utc_now_iso()
    title = _pick_first_text(
        active_task_payload.get("title"),
        execution.get("title"),
        live_task.get("title"),
        shared_truth.get("objective_title"),
        objective_id,
        "TOD operator objective",
    )
    request_id = _pick_first_text(active_task_payload.get("request_id"), live_task.get("request_id"), task_id)
    execution_id = _pick_first_text(active_task_payload.get("execution_id"), execution_result_payload.get("execution_id"), task_id, request_id)
    rollback_metadata = _resolve_rollback_metadata(state)
    effective_rollback_state = str(rollback_state or rollback_metadata.get("state") or execution.get("rollback_state") or "not_needed").strip()
    effective_recovery_state = str(recovery_state or execution.get("recovery_state") or "not_needed").strip()
    execution_evidence = execution_result_payload.get("execution_evidence") if isinstance(execution_result_payload.get("execution_evidence"), dict) else active_task_payload.get("execution_evidence") if isinstance(active_task_payload.get("execution_evidence"), dict) else {}
    updated_evidence = {
        **execution_evidence,
        "status": status,
        "summary": _compact_text(summary, 220),
        "current_action": _compact_text(current_action, 220),
        "next_step": _compact_text(next_step, 220),
        "wait_target": str(wait_target or "").strip(),
        "wait_target_label": str(wait_target_label or "").strip(),
        "wait_reason": _compact_text(wait_reason, 220),
        "updated_at": updated_at,
        "rollback_state": effective_rollback_state,
        "recovery_state": effective_recovery_state,
        "transition_action": action_id,
    }
    if rollback_metadata.get("hint"):
        updated_evidence["rollback_hint"] = rollback_metadata.get("hint")
    if isinstance(extra_evidence, dict) and extra_evidence:
        updated_evidence.update(extra_evidence)

    active_objective_payload.update(
        {
            "objective_id": objective_id,
            "title": title,
            "summary": _compact_text(summary, 220),
            "updated_at": updated_at,
            "execution_evidence": updated_evidence,
            "rollback_state": effective_rollback_state,
        }
    )
    active_task_payload.update(
        {
            "request_id": request_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "objective_id": objective_id,
            "title": title,
            "summary": _compact_text(summary, 220),
            "status": status,
            "execution_state": execution_state,
            "current_action": _compact_text(current_action, 220),
            "next_step": _compact_text(next_step, 220),
            "next_validation": str(execution.get("next_validation") or active_task_payload.get("next_validation") or "").strip(),
            "wait_target": str(wait_target or "").strip(),
            "wait_target_label": str(wait_target_label or "").strip(),
            "wait_reason": _compact_text(wait_reason, 220),
            "updated_at": updated_at,
            "execution_evidence": updated_evidence,
            "rollback_state": effective_rollback_state,
            "recovery_state": effective_recovery_state,
        }
    )
    activity_payload.update(
        {
            "event": action_id,
            "status": status,
            "phase": str(activity_payload.get("phase") or execution_result_payload.get("phase") or "operator_control").strip(),
            "summary": _compact_text(summary, 220),
            "current_action": _compact_text(current_action, 220),
            "next_step": _compact_text(next_step, 220),
            "next_validation": str(active_task_payload.get("next_validation") or "").strip(),
            "wait_target": str(wait_target or "").strip(),
            "wait_target_label": str(wait_target_label or "").strip(),
            "wait_reason": _compact_text(wait_reason, 220),
            "updated_at": updated_at,
            "execution_state": execution_state,
            "execution_evidence": updated_evidence,
        }
    )
    validation_payload.update(
        {
            "updated_at": updated_at,
            "summary": _compact_text(summary, 220),
            "validation_target": str(active_task_payload.get("next_validation") or validation_payload.get("validation_target") or "").strip(),
        }
    )
    execution_result_payload.update(
        {
            "request_id": request_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "objective_id": objective_id,
            "title": title,
            "status": status,
            "execution_state": execution_state,
            "summary": _compact_text(summary, 220),
            "current_action": _compact_text(current_action, 220),
            "next_step": _compact_text(next_step, 220),
            "wait_target": str(wait_target or "").strip(),
            "wait_target_label": str(wait_target_label or "").strip(),
            "wait_reason": _compact_text(wait_reason, 220),
            "updated_at": updated_at,
            "validation_summary": _compact_text(summary, 220),
            "command_output": _compact_text(command_output or execution_result_payload.get("command_output") or "", 220),
            "rollback_state": effective_rollback_state,
            "recovery_state": effective_recovery_state,
            "execution_evidence": updated_evidence,
        }
    )
    truth_summary = execution_truth_payload.get("summary") if isinstance(execution_truth_payload.get("summary"), dict) else {}
    truth_summary.update(
        {
            "latest_execution_at": updated_at,
            "summary": _compact_text(summary, 220),
            "current_action": _compact_text(current_action, 220),
            "next_step": _compact_text(next_step, 220),
            "validation_passed": False,
        }
    )
    execution_truth_payload["generated_at"] = updated_at
    execution_truth_payload["summary"] = truth_summary
    recent_truth = execution_truth_payload.get("recent_execution_truth") if isinstance(execution_truth_payload.get("recent_execution_truth"), list) else []
    if recent_truth and isinstance(recent_truth[0], dict):
        recent_truth[0].update(
            {
                "generated_at": updated_at,
                "execution_state": execution_state,
                "status": status,
                "summary": _compact_text(summary, 220),
                "current_action": _compact_text(current_action, 220),
                "next_step": _compact_text(next_step, 220),
                "next_validation": str(active_task_payload.get("next_validation") or "").strip(),
                "validation_passed": False,
                "execution_evidence": updated_evidence,
            }
        )

    artifact_map = {
        SHARED_RUNTIME_ROOT / "TOD_ACTIVE_OBJECTIVE.latest.json": active_objective_payload,
        SHARED_RUNTIME_ROOT / "TOD_ACTIVE_TASK.latest.json": active_task_payload,
        SHARED_RUNTIME_ROOT / "TOD_ACTIVITY_STREAM.latest.json": activity_payload,
        SHARED_RUNTIME_ROOT / "TOD_VALIDATION_RESULT.latest.json": validation_payload,
        SHARED_RUNTIME_ROOT / "TOD_EXECUTION_RESULT.latest.json": execution_result_payload,
        SHARED_RUNTIME_ROOT / "TOD_EXECUTION_TRUTH.latest.json": execution_truth_payload,
    }
    for path, payload in artifact_map.items():
        _write_shared_json(path, payload)
    return [str(path) for path in artifact_map.keys()]


def _payload_matches_active_execution(payload: dict[str, Any], objective_id: str, task_id: str, execution_id: str) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    payload_task_id = str(payload.get("task_id") or "").strip()
    payload_execution_id = str(payload.get("execution_id") or "").strip()
    payload_objective_id = str(payload.get("objective_id") or "").strip()
    if not _same_objective(payload_objective_id, objective_id):
        return False
    if payload_task_id and payload_task_id == task_id:
        return True
    if payload_execution_id and payload_execution_id == execution_id:
        return True
    return False


def _existing_runtime_matches_active_execution(runtime_payloads: dict[str, dict[str, Any]], objective_id: str, task_id: str, execution_id: str) -> bool:
    if not runtime_payloads or not all(isinstance(payload, dict) and payload for payload in runtime_payloads.values()):
        return False
    return all(
        _payload_matches_active_execution(payload, objective_id, task_id, execution_id)
        for payload in runtime_payloads.values()
    )


def _pick_latest_timestamp(*values: Any) -> str:
    latest_value = ""
    latest_parsed: datetime | None = None
    for value in values:
        parsed = _parse_timestamp(value)
        if parsed is None:
            continue
        if latest_parsed is None or parsed > latest_parsed:
            latest_parsed = parsed
            latest_value = str(value or "").strip()
    return latest_value


def _normalize_stage_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _stage_is_complete(value: Any) -> bool:
    return _normalize_stage_status(value) in {"accepted", "complete", "completed", "passed", "success", "succeeded", "not_needed"}


def _stage_is_active(value: Any) -> bool:
    return _normalize_stage_status(value) in {"active", "accepted", "in_progress", "pending", "planned", "running", "waiting"}


def _derive_implementation_gate_percent(
    active_task: dict[str, Any],
    execution_result: dict[str, Any],
    validation: dict[str, Any],
    patch_status: str,
    command_status: str,
    result_publisher_status: str,
) -> int:
    gate_floor = 30
    execution_evidence = (
        execution_result.get("execution_evidence")
        if isinstance(execution_result.get("execution_evidence"), dict)
        else active_task.get("execution_evidence")
        if isinstance(active_task.get("execution_evidence"), dict)
        else {}
    )
    latest_update = _pick_latest_timestamp(
        execution_result.get("updated_at"),
        execution_result.get("generated_at"),
        validation.get("updated_at"),
        validation.get("generated_at"),
        active_task.get("updated_at"),
        active_task.get("generated_at"),
    )
    age_seconds = _age_seconds(latest_update)
    if age_seconds is None or age_seconds > 1800:
        return gate_floor

    progress_points = gate_floor
    active_implementation = _normalize_stage_status(patch_status) in {"active", "in_progress", "running"} or _normalize_stage_status(command_status) in {"active", "in_progress", "running"}
    if active_implementation:
        progress_points += 1

    detail_text = " ".join(
        str(item or "")
        for item in (
            execution_result.get("current_action"),
            execution_result.get("summary"),
            execution_result.get("next_step"),
            execution_result.get("wait_reason"),
            active_task.get("current_action"),
            active_task.get("summary"),
            active_task.get("next_step"),
            active_task.get("wait_reason"),
        )
    ).lower()
    files_changed = execution_result.get("files_changed") if isinstance(execution_result.get("files_changed"), list) else execution_evidence.get("files_changed") if isinstance(execution_evidence.get("files_changed"), list) else []
    if files_changed:
        progress_points += min(2, len(files_changed))

    implementation_evidence_seen = active_implementation or bool(files_changed)
    if implementation_evidence_seen and any(term in detail_text for term in ("implementation", "implement", "patch", "slice", "execution-loop")):
        progress_points += 1

    command_output = str(execution_result.get("command_output") or execution_evidence.get("command_output") or "").strip()
    if implementation_evidence_seen and command_output:
        progress_points += 1

    return max(gate_floor, min(69, int(progress_points)))


def _derive_phase_progress(
    active_task: dict[str, Any],
    execution_result: dict[str, Any],
    validation: dict[str, Any],
    activity_state: str,
    next_step: str,
    wait_reason: str,
) -> dict[str, Any]:
    phase_label = _detect_phase_label(active_task, execution_result)
    contract = (
        active_task.get("execution_contract")
        if isinstance(active_task.get("execution_contract"), dict)
        else execution_result.get("execution_contract")
        if isinstance(execution_result.get("execution_contract"), dict)
        else {}
    )
    intake = contract.get("task_intake") if isinstance(contract.get("task_intake"), dict) else {}
    planner = contract.get("bounded_step_planner") if isinstance(contract.get("bounded_step_planner"), dict) else {}
    command_runner = contract.get("command_runner") if isinstance(contract.get("command_runner"), dict) else {}
    patch_writer = contract.get("patch_writer") if isinstance(contract.get("patch_writer"), dict) else {}
    validator = contract.get("validator") if isinstance(contract.get("validator"), dict) else {}
    result_publisher = contract.get("result_publisher") if isinstance(contract.get("result_publisher"), dict) else {}
    patch_status = _normalize_stage_status(patch_writer.get("status"))
    command_status = _normalize_stage_status(command_runner.get("status"))
    result_publisher_status = _normalize_stage_status(result_publisher.get("status"))
    implementation_complete = _stage_is_complete(patch_status) or (not patch_status and _stage_is_complete(command_status))

    milestones = [
        {
            "id": "task_intake",
            "label": "Task intake",
            "weight": 10,
            "status": _normalize_stage_status(intake.get("status")),
            "complete": _stage_is_complete(intake.get("status")),
        },
        {
            "id": "inspection",
            "label": "Inspection and planning",
            "weight": 20,
            "status": _normalize_stage_status(planner.get("status") or (planner.get("active_step") if isinstance(planner.get("active_step"), dict) else {}).get("status")),
            "complete": _stage_is_complete(planner.get("status")) or _stage_is_complete((planner.get("active_step") if isinstance(planner.get("active_step"), dict) else {}).get("status")),
        },
        {
            "id": "implementation",
            "label": "Implementation",
            "weight": 35,
            "status": patch_status or command_status,
            "complete": implementation_complete,
        },
        {
            "id": "validation",
            "label": "Focused validation",
            "weight": 20,
            "status": _normalize_stage_status(validator.get("status") or validation.get("status")),
            "complete": _stage_is_complete(validator.get("status")) or _stage_is_complete(validation.get("status")),
        },
        {
            "id": "publication",
            "label": "Evidence publish",
            "weight": 15,
            "status": result_publisher_status,
            "complete": _stage_is_complete(result_publisher_status),
        },
    ]

    percent_complete = sum(item["weight"] for item in milestones if item["complete"])
    implementation_pending = not milestones[2]["complete"] and any(
        phrase in f"{next_step} {wait_reason}".lower()
        for phrase in ("implementation", "patch", "bounded execution-loop slice", "bounded local implementation step")
    )
    if implementation_pending:
        percent_complete = _derive_implementation_gate_percent(
            active_task,
            execution_result,
            validation,
            patch_status,
            command_status,
            result_publisher_status,
        )
    if activity_state == "complete":
        percent_complete = 100

    completed_count = sum(1 for item in milestones if item["complete"])
    total_count = len(milestones)
    next_gate = "Phase 2 handoff" if percent_complete >= 100 else "Implementation" if implementation_pending else "Focused validation" if not milestones[3]["complete"] else "Evidence publish" if not milestones[4]["complete"] else "Phase 1 closeout"
    if percent_complete >= 100:
        summary = f"{phase_label} complete and verified."
    elif implementation_pending:
        summary = f"{phase_label} is about {percent_complete}% complete within the implementation gate. Inspection is done; implementation is the next gate."
    elif not milestones[3]["complete"]:
        summary = f"{phase_label} is about {percent_complete}% complete. Focused validation is the next gate."
    elif not milestones[4]["complete"]:
        summary = f"{phase_label} is about {percent_complete}% complete. Evidence publish is the next gate."
    else:
        summary = f"{phase_label} is about {percent_complete}% complete. Final closeout is the next gate."

    return {
        "available": bool(contract) or bool(active_task) or bool(execution_result),
        "label": f"{phase_label} progress",
        "percent_complete": max(0, min(100, int(percent_complete))),
        "completed_milestones": completed_count,
        "total_milestones": total_count,
        "next_gate": next_gate,
        "summary": summary,
        "milestones": milestones,
    }


def _derive_stall_signal(activity_state: str, age_seconds: float | None, phase_progress: dict[str, Any], next_step: str, wait_reason: str) -> dict[str, Any]:
    normalized_state = str(activity_state or "idle").strip().lower() or "idle"
    if age_seconds is None or normalized_state in {"complete", "blocked", "idle", "paused"}:
        return {
            "flagged": False,
            "level": "ok",
            "threshold_seconds": None,
            "age_seconds": age_seconds,
            "summary": "",
        }

    progress_percent = int(phase_progress.get("percent_complete") or 0)
    progress_label = _compact_text(phase_progress.get("label"), 80) or "Phase progress"
    phase_display = progress_label[:-9] if progress_label.lower().endswith(" progress") else progress_label
    progress_summary = str(phase_progress.get("summary") or "Phase progress is published.").strip()
    detail = _compact_text(wait_reason or next_step or "No next bounded step detail is published.", 160)
    threshold_seconds = 1200 if normalized_state == "waiting" else 900 if normalized_state == "working" else 600 if normalized_state == "stalled" else None
    flagged = bool(threshold_seconds is not None and age_seconds >= threshold_seconds)
    if not flagged:
        implementation_pending = normalized_state == "waiting" and str(phase_progress.get("next_gate") or "").strip().lower() == "implementation" and progress_percent >= 30
        if implementation_pending:
            delay_minutes = max(1, int(round(age_seconds / 60.0)))
            freshness_text = (
                f"Fresh execution evidence landed {delay_minutes}m ago, so this wait is for the next implementation slice rather than stale output."
                if age_seconds <= 180
                else f"Latest execution evidence is {delay_minutes}m old and still inside the implementation wait window."
            )
            summary = (
                f"Held at implementation gate: {phase_display} is at {progress_percent}% until the next implementation slice starts. "
                f"{freshness_text} {progress_summary} {detail}"
            )
            return {
                "flagged": False,
                "level": "implementation_pending",
                "threshold_seconds": threshold_seconds,
                "age_seconds": age_seconds,
                "summary": _compact_text(summary, 220),
            }
        return {
            "flagged": False,
            "level": "ok",
            "threshold_seconds": threshold_seconds,
            "age_seconds": age_seconds,
            "summary": "",
        }

    delay_minutes = max(1, int(round(age_seconds / 60.0)))
    summary = (
        f"Probable stall: {phase_display} is holding at {progress_percent}% for about {delay_minutes}m without a newer execution update. "
        f"{progress_summary} {detail}"
    )
    return {
        "flagged": True,
        "level": "probable_stall",
        "threshold_seconds": threshold_seconds,
        "age_seconds": age_seconds,
        "summary": _compact_text(summary, 220),
    }


def _derive_execution_live_state(
    execution_status: dict[str, Any],
    planner_state: dict[str, Any],
    operator_actions: list[dict[str, Any]],
) -> dict[str, Any]:
    execution_status = execution_status if isinstance(execution_status, dict) else {}
    planner_state = planner_state if isinstance(planner_state, dict) else {}
    operator_actions = operator_actions if isinstance(operator_actions, list) else []

    activity_state = str(execution_status.get("activity_state") or "idle").strip().lower() or "idle"
    activity_label = str(execution_status.get("activity_label") or "Idle").strip() or "Idle"
    activity_summary = _compact_text(execution_status.get("activity_summary") or execution_status.get("summary"), 220)
    wait_reason = _compact_text(execution_status.get("wait_reason"), 220)
    next_step = _compact_text(execution_status.get("next_step"), 220)
    current_action = _compact_text(execution_status.get("current_action"), 220)
    stall_signal = execution_status.get("stall_signal") if isinstance(execution_status.get("stall_signal"), dict) else {}
    stall_flagged = bool(stall_signal.get("flagged") is True)
    stall_summary = _compact_text(stall_signal.get("summary"), 220)

    barriers: list[str] = []
    if stall_flagged:
        barriers.append(stall_summary or "Execution evidence is stale and TOD may be frozen on the current objective.")
    if activity_state in {"blocked", "stalled"} and activity_summary:
        barriers.append(activity_summary)
    if execution_status.get("executor_binding_status") == "missing":
        barriers.append(
            "Executor binding is missing for the queued objective step; dispatch cannot continue until the local binding is materialized."
        )
    if activity_state == "waiting" and wait_reason:
        barriers.append(wait_reason)
    if activity_state in {"waiting", "stalled", "blocked"} and not wait_reason and not next_step:
        barriers.append("No executable next step was published for the active objective.")

    unique_barriers: list[str] = []
    seen_barriers: set[str] = set()
    for barrier in barriers:
        text = _compact_text(barrier, 220)
        if not text:
            continue
        key = text.lower()
        if key in seen_barriers:
            continue
        seen_barriers.add(key)
        unique_barriers.append(text)

    action_map: dict[str, dict[str, Any]] = {}
    for item in operator_actions:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("id") or "").strip()
        if action_id:
            action_map[action_id] = item

    recommended_ids = [
        "run_shared_truth_reconciliation",
        "recover_stale_state",
        "force_replay_current_task",
        "start_next_task",
        "show_evidence",
    ]
    recommended_actions: list[dict[str, Any]] = []
    for action_id in recommended_ids:
        item = action_map.get(action_id)
        if not item:
            continue
        recommended_actions.append(
            {
                "id": action_id,
                "label": str(item.get("label") or action_id).strip(),
                "enabled": bool(item.get("enabled")),
                "disabled_reason": _compact_text(item.get("disabled_reason"), 180),
            }
        )

    has_active_objective = bool(
        str(execution_status.get("objective_id") or planner_state.get("objective_id") or "").strip()
        or str(execution_status.get("task_id") or planner_state.get("task_id") or "").strip()
    )
    is_working_background = activity_state in {"working"} or (activity_state == "waiting" and not stall_flagged and not unique_barriers)
    is_stuck = activity_state in {"stalled", "blocked"} or stall_flagged or bool(unique_barriers)
    prefer_execution_surface = bool(
        execution_status.get("available")
        and has_active_objective
        and activity_state in {"working", "waiting", "stalled", "blocked", "paused"}
    )
    mim_priority = is_stuck

    if is_working_background:
        status_detail = _pick_first_text(current_action, activity_summary, "TOD is actively executing the current objective in the background.")
    elif is_stuck:
        status_detail = _pick_first_text(unique_barriers[0] if unique_barriers else "", stall_summary, activity_summary, wait_reason, "TOD is blocked and requires escalation.")
    else:
        status_detail = _pick_first_text(activity_summary, current_action, "TOD is idle.")

    stuck_on = _pick_first_text(next_step, wait_reason, unique_barriers[0] if unique_barriers else "", status_detail)
    next_to_progress = _pick_first_text(
        next_step,
        "Run Reconcile Truth first (MIM priority), then Recover Stale State if TOD remains frozen.",
    )
    if is_stuck and not stuck_on:
        stuck_on = "Execution appears blocked, but no explicit blocker details were published."
    if not next_to_progress:
        next_to_progress = "Run Reconcile Truth first (MIM priority), then Recover Stale State if TOD remains frozen."
    if is_stuck and not unique_barriers and stuck_on:
        unique_barriers = [stuck_on]

    return {
        "has_active_objective": has_active_objective,
        "is_working_background": is_working_background,
        "is_stuck": is_stuck,
        "mim_priority": mim_priority,
        "prefer_execution_surface": prefer_execution_surface,
        "status": activity_state,
        "status_label": activity_label,
        "status_detail": status_detail,
        "stuck_on": stuck_on,
        "barriers": unique_barriers,
        "next_to_progress": next_to_progress,
        "recommended_actions": recommended_actions,
        "escalation_channels": ["MIM", "Codex", "Operator"],
    }


def _normalize_execution_status(
    active_objective_payload: Any,
    active_task_payload: Any,
    activity_payload: Any,
    validation_payload: Any,
    execution_result_payload: Any,
    truth_payload: Any,
) -> dict[str, Any]:
    active_objective = active_objective_payload if isinstance(active_objective_payload, dict) else {}
    active_task = active_task_payload if isinstance(active_task_payload, dict) else {}
    activity = activity_payload if isinstance(activity_payload, dict) else {}
    validation = validation_payload if isinstance(validation_payload, dict) else {}
    execution_result = execution_result_payload if isinstance(execution_result_payload, dict) else {}
    truth = truth_payload if isinstance(truth_payload, dict) else {}

    available = any(bool(payload) for payload in (active_objective, active_task, activity, validation, execution_result, truth))
    updated_at = _pick_latest_timestamp(
        execution_result.get("updated_at"),
        execution_result.get("generated_at"),
        validation.get("updated_at"),
        validation.get("generated_at"),
        activity.get("updated_at"),
        activity.get("generated_at"),
        active_task.get("updated_at"),
        active_task.get("generated_at"),
    )
    if not updated_at:
        updated_at = _pick_latest_timestamp(
            active_objective.get("updated_at"),
            active_objective.get("generated_at"),
            truth.get("generated_at"),
        )
    status = str(
        execution_result.get("status")
        or activity.get("status")
        or active_task.get("status")
        or truth.get("status")
        or ""
    ).strip().lower()
    execution_state = str(
        execution_result.get("execution_state")
        or activity.get("execution_state")
        or active_task.get("execution_state")
        or active_task.get("status")
        or ""
    ).strip().lower()
    phase = str(activity.get("phase") or execution_result.get("phase") or validation.get("phase") or "").strip().lower()
    current_action = _compact_text(
        execution_result.get("current_action")
        or activity.get("current_action")
        or active_task.get("current_action")
        or "",
        220,
    )
    next_step = _compact_text(
        execution_result.get("next_step")
        or activity.get("next_step")
        or active_task.get("next_step")
        or "",
        220,
    )
    next_validation = _compact_text(
        validation.get("validation_target")
        or active_task.get("next_validation")
        or activity.get("next_validation")
        or "",
        220,
    )
    summary = _compact_text(
        execution_result.get("summary")
        or active_task.get("summary")
        or activity.get("summary")
        or current_action
        or "No TOD execution activity is currently published.",
        220,
    )
    validation_status = str(validation.get("status") or "").strip().lower()
    validation_summary = _compact_text(validation.get("summary") or "", 220)
    execution_evidence = (
        execution_result.get("execution_evidence")
        if isinstance(execution_result.get("execution_evidence"), dict)
        else active_task.get("execution_evidence")
        if isinstance(active_task.get("execution_evidence"), dict)
        else activity.get("execution_evidence")
        if isinstance(activity.get("execution_evidence"), dict)
        else {}
    )
    command_output = _compact_text(
        execution_result.get("command_output")
        or execution_evidence.get("command_output")
        or "",
        220,
    )
    files_changed = [
        _compact_text(item, 180)
        for item in (
            execution_result.get("files_changed")
            if isinstance(execution_result.get("files_changed"), list)
            else execution_evidence.get("files_changed")
            if isinstance(execution_evidence.get("files_changed"), list)
            else []
        )[:8]
        if _compact_text(item, 180)
    ]
    matched_files = [
        _compact_text(item, 180)
        for item in (
            execution_evidence.get("matched_files")
            if isinstance(execution_evidence.get("matched_files"), list)
            else []
        )[:8]
        if _compact_text(item, 180)
    ]
    validation_checks = (
        execution_evidence.get("validation_checks")
        if isinstance(execution_evidence.get("validation_checks"), list)
        else validation.get("checks")
        if isinstance(validation.get("checks"), list)
        else []
    )
    wait_target = _compact_text(
        execution_result.get("wait_target")
        or active_task.get("wait_target")
        or activity.get("wait_target")
        or execution_evidence.get("wait_target")
        or "",
        120,
    )
    wait_target_label = _compact_text(
        execution_result.get("wait_target_label")
        or active_task.get("wait_target_label")
        or activity.get("wait_target_label")
        or execution_evidence.get("wait_target_label")
        or wait_target,
        120,
    )
    wait_reason = _compact_text(
        execution_result.get("wait_reason")
        or active_task.get("wait_reason")
        or activity.get("wait_reason")
        or execution_evidence.get("wait_reason")
        or "",
        220,
    )
    rollback_state = _compact_text(
        execution_result.get("rollback_state") or execution_evidence.get("rollback_state") or "not_needed",
        120,
    )
    recovery_state = _compact_text(
        execution_result.get("recovery_state") or execution_evidence.get("recovery_state") or "not_needed",
        120,
    )
    age_seconds = _age_seconds(updated_at)

    activity_state = "idle"
    activity_label = "Idle"
    active = False
    if not available:
        activity_summary = "No TOD execution artifact is currently published."
    elif status in {"failed", "error", "blocked"} or execution_state in {"failed", "error", "blocked"}:
        activity_state = "stalled"
        activity_label = "Blocked"
        activity_summary = summary or "TOD hit a blocking execution error."
    elif validation_status in {"pending", "waiting"} and next_validation:
        activity_state = "waiting"
        activity_label = "Waiting"
        active = True
        activity_summary = wait_reason or validation_summary or f"TOD is waiting on validation: {next_validation}."
    elif status in {"paused"} or execution_state in {"paused", "paused_by_operator", "paused_pending_resume"}:
        activity_state = "paused"
        activity_label = "Paused"
        activity_summary = wait_reason or summary or "TOD execution is paused on this console."
    elif status in {"waiting", "pending"} or execution_state in {"waiting", "waiting_on_next_step", "step_completed_waiting_next_selection", "resume_requested", "rollback_applied"}:
        activity_state = "waiting"
        activity_label = "Waiting"
        active = True
        activity_summary = wait_reason or current_action or summary or "TOD completed the latest bounded step and is waiting on the next step."
    elif status in {"completed", "complete", "success", "succeeded"} or execution_state in {"completed", "complete", "success", "succeeded"}:
        activity_state = "complete"
        activity_label = "Complete"
        activity_summary = summary or "TOD completed the current execution slice."
    elif status in {"running", "active", "in_progress"} or execution_state in {"accepted", "planned", "running", "active", "in_progress"}:
        if age_seconds is not None and age_seconds > 900:
            activity_state = "stalled"
            activity_label = "Stale"
            activity_summary = current_action or summary or "TOD execution artifacts are stale and need review."
        else:
            activity_state = "working"
            activity_label = "Working"
            active = True
            activity_summary = current_action or summary or "TOD is actively working the current execution slice."
    else:
        activity_summary = summary

    executor_binding_status = ""
    executor_binding_target = ""
    executor_binding_command = ""
    circular_block_converted = False
    if activity_state == "waiting" and _is_circular_local_executor_wait(wait_target_label or wait_target, wait_reason, next_step):
        executor_binding_status = "missing"
        executor_binding_target = "scripts/engines/LocalExecutionEngine.ps1::Invoke-LocalExecutionEngine"
        executor_binding_command = "execute-chat-task"
        circular_block_converted = True
        activity_state = "blocked"
        activity_label = "Binding Required"
        active = False
        wait_target = executor_binding_target
        wait_target_label = executor_binding_target
        wait_reason = (
            "Circular self-wait removed. The next bounded implementation slice has not been dispatched through "
            "execute-chat-task into scripts/engines/LocalExecutionEngine.ps1::Invoke-LocalExecutionEngine."
        )
        activity_summary = (
            "Missing local executor binding: dispatch the next bounded implementation slice through execute-chat-task into "
            "scripts/engines/LocalExecutionEngine.ps1::Invoke-LocalExecutionEngine."
        )

    phase_progress = _derive_phase_progress(
        active_task,
        execution_result,
        validation,
        activity_state,
        next_step,
        wait_reason,
    )
    stall_signal = _derive_stall_signal(activity_state, age_seconds, phase_progress, next_step, wait_reason)
    if stall_signal.get("flagged") and activity_state in {"waiting", "working", "stalled"}:
        activity_state = "stalled"
        activity_label = "Stalled"
        activity_summary = str(stall_signal.get("summary") or activity_summary).strip() or activity_summary
    elif stall_signal.get("level") == "implementation_pending" and activity_state == "waiting":
        activity_summary = str(stall_signal.get("summary") or activity_summary).strip() or activity_summary

    return {
        "available": available,
        "objective_id": str(active_objective.get("objective_id") or active_task.get("objective_id") or "").strip(),
        "task_id": str(active_task.get("task_id") or execution_result.get("task_id") or "").strip(),
        "execution_id": str(execution_result.get("execution_id") or active_task.get("execution_id") or "").strip(),
        "title": _compact_text(active_task.get("title") or active_objective.get("title") or "", 180),
        "task_focus": _compact_text(active_task.get("task_focus") or active_task.get("summary") or "", 220),
        "status": status,
        "execution_state": execution_state,
        "phase": phase,
        "current_action": current_action,
        "next_step": next_step,
        "next_validation": next_validation,
        "summary": summary,
        "validation_status": validation_status,
        "validation_summary": validation_summary,
        "command_output": command_output,
        "files_changed": files_changed,
        "matched_files": matched_files,
        "validation_checks": validation_checks,
        "wait_target": wait_target,
        "wait_target_label": wait_target_label,
        "wait_reason": wait_reason,
        "rollback_state": rollback_state,
        "recovery_state": recovery_state,
        "updated_at": updated_at,
        "updated_age": _format_age(updated_at),
        "last_update_age_seconds": age_seconds,
        "activity_state": activity_state,
        "activity_label": activity_label,
        "activity_summary": activity_summary,
        "phase_progress": phase_progress,
        "stall_signal": stall_signal,
        "active": active,
        "executor_binding_status": executor_binding_status,
        "executor_binding_target": executor_binding_target,
        "executor_binding_command": executor_binding_command,
        "circular_block_converted": circular_block_converted,
    }


def _normalize_guidance_items(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    items: list[dict[str, str]] = []
    for item in values[:8]:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "code": str(item.get("code") or "").strip(),
                "severity": str(item.get("severity") or "info").strip(),
                "summary": _compact_text(item.get("summary"), 180),
                "recommended_action": _compact_text(item.get("recommended_action"), 220),
            }
        )
    return items


def _load_shared_truth_payload() -> tuple[dict[str, Any], str]:
    path = SHARED_RUNTIME_ROOT / "TOD_MIM_SHARED_TRUTH.latest.json"
    try:
        if not path.exists() or not path.is_file():
            return {}, ""
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}, ""
    return payload if isinstance(payload, dict) else {}, str(path)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _friendly_person_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", text) if part]
    if not parts:
        return ""
    return " ".join(part[:1].upper() + part[1:] for part in parts[:3])


def _is_generic_public_name(value: Any) -> bool:
    tokens = [part.lower() for part in re.split(r"[^A-Za-z0-9]+", str(value or "").strip()) if part]
    if not tokens:
        return True
    generic_tokens = {
        "guest",
        "visitor",
        "public",
        "operator",
        "testpilot",
        "unknown",
        "anonymous",
        "user",
        "account",
        "local",
        "default",
    }
    return all(token in generic_tokens for token in tokens)


def _name_from_email(value: Any) -> str:
    text = str(value or "").strip()
    if "@" not in text:
        return ""
    return _friendly_person_name(text.split("@", 1)[0])


def _find_named_value(payload: Any, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(payload, dict):
        for key in ("display_name", "user_name", "username", "whoami", "user"):
            candidate = _friendly_person_name(payload.get(key))
            if candidate:
                return candidate
        for item in list(payload.values())[:16]:
            candidate = _find_named_value(item, depth + 1)
            if candidate:
                return candidate
    elif isinstance(payload, list):
        for item in payload[:16]:
            candidate = _find_named_value(item, depth + 1)
            if candidate:
                return candidate
    return ""


def _resolve_public_visitor_name() -> str:
    candidates = [
        os.getenv("TOD_PUBLIC_VISITOR_NAME"),
        _name_from_email(os.getenv("SUPER_USER_EMAIL")),
        os.getenv("SUPER_USER_NAME"),
    ]
    for candidate in candidates:
        friendly = _friendly_person_name(candidate)
        if friendly and not _is_generic_public_name(friendly):
            return friendly
    return "Dave"


def _normalize_string_list(values: Any, limit: int = 8, item_limit: int = 220) -> list[str]:
    if not isinstance(values, list):
        return []
    items: list[str] = []
    for item in values[:limit]:
        text = _compact_text(item, item_limit)
        if text:
            items.append(text)
    return items


def _normalize_training_events(values: Any, limit: int = 8) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    items: list[dict[str, str]] = []
    for item in values[-limit:]:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "generated_at": str(item.get("generated_at") or "").strip(),
                "generated_age": _format_age(item.get("generated_at")),
                "type": str(item.get("type") or "event").strip(),
                "summary": _compact_text(item.get("summary"), 220),
            }
        )
    return items


def _normalize_training_stages(values: Any, limit: int = 8) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    items: list[dict[str, str]] = []
    for item in values[:limit]:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "id": str(item.get("id") or "").strip(),
                "label": str(item.get("label") or item.get("id") or "stage").strip(),
                "status": str(item.get("status") or "unknown").strip(),
                "detail": _compact_text(item.get("detail"), 220),
                "started_at": str(item.get("started_at") or "").strip(),
                "completed_at": str(item.get("completed_at") or "").strip(),
            }
        )
    return items


def _normalize_training_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {
            "available": False,
            "generated_at": "",
            "generated_age": "Unknown",
            "source": "",
            "run_id": "",
            "state": "unknown",
            "state_label": "Unknown",
            "active": False,
            "started_at": "",
            "started_age": "Unknown",
            "updated_at": "",
            "updated_age": "Unknown",
            "runtime_seconds": 0,
            "percent_complete": 0,
            "completed_steps": 0,
            "failed_steps": 0,
            "total_steps": 0,
            "phase": "unknown",
            "phase_label": "Unknown",
            "phase_detail": "",
            "current_step": "",
            "eta_seconds": None,
            "expected_completion_utc": "",
            "latest_error": "",
            "latest_error_at": "",
            "latest_error_age": "Unknown",
            "latest_resolution": "",
            "latest_resolution_at": "",
            "latest_resolution_age": "Unknown",
            "summary": "No training status is currently published.",
            "warnings": [],
            "errors": [],
            "resolutions": [],
            "recent_events": [],
            "stages": [],
            "artifacts": {"output_dir": "", "trace_path": ""},
            "idle_policy": {},
        }

    state = str(value.get("state") or value.get("status") or "unknown").strip() or "unknown"
    state_label = str(value.get("state_label") or state.replace("_", " ").title()).strip() or "Unknown"
    phase = str(value.get("phase") or "unknown").strip() or "unknown"
    phase_label = str(value.get("phase_label") or phase.replace("_", " ").title()).strip() or "Unknown"
    eta_value = value.get("eta_seconds")
    eta_seconds = None if eta_value in (None, "") else _safe_int(eta_value, 0)
    artifacts = value.get("artifacts") if isinstance(value.get("artifacts"), dict) else {}

    return {
        "available": True,
        "generated_at": str(value.get("generated_at") or "").strip(),
        "generated_age": _format_age(value.get("generated_at")),
        "source": str(value.get("source") or "").strip(),
        "run_id": str(value.get("run_id") or "").strip(),
        "state": state,
        "state_label": state_label,
        "active": bool(value.get("active") is True or state in {"running", "active", "in_progress"}),
        "started_at": str(value.get("started_at") or "").strip(),
        "started_age": _format_age(value.get("started_at")),
        "updated_at": str(value.get("updated_at") or "").strip(),
        "updated_age": _format_age(value.get("updated_at")),
        "runtime_seconds": _safe_int(value.get("runtime_seconds"), 0),
        "percent_complete": max(0, min(100, _safe_int(value.get("percent_complete"), 0))),
        "completed_steps": _safe_int(value.get("completed_steps"), 0),
        "failed_steps": _safe_int(value.get("failed_steps"), 0),
        "total_steps": _safe_int(value.get("total_steps"), 0),
        "phase": phase,
        "phase_label": phase_label,
        "phase_detail": _compact_text(value.get("phase_detail"), 220),
        "current_step": _compact_text(value.get("current_step"), 160),
        "eta_seconds": eta_seconds,
        "expected_completion_utc": str(value.get("expected_completion_utc") or "").strip(),
        "latest_error": _compact_text(value.get("latest_error"), 220),
        "latest_error_at": str(value.get("latest_error_at") or "").strip(),
        "latest_error_age": _format_age(value.get("latest_error_at")),
        "latest_resolution": _compact_text(value.get("latest_resolution"), 220),
        "latest_resolution_at": str(value.get("latest_resolution_at") or "").strip(),
        "latest_resolution_age": _format_age(value.get("latest_resolution_at")),
        "summary": _compact_text(value.get("summary") or "No training status is currently published.", 220),
        "warnings": _normalize_string_list(value.get("warnings"), limit=8, item_limit=200),
        "errors": _normalize_string_list(value.get("errors"), limit=8, item_limit=200),
        "resolutions": _normalize_string_list(value.get("resolutions"), limit=8, item_limit=200),
        "recent_events": _normalize_training_events(value.get("recent_events") or value.get("events"), limit=8),
        "stages": _normalize_training_stages(value.get("stages"), limit=8),
        "artifacts": {
            "output_dir": str(artifacts.get("output_dir") or "").strip(),
            "trace_path": str(artifacts.get("trace_path") or "").strip(),
        },
        "idle_policy": {},
    }


def _normalize_idle_training_policy(value: Any, training_status: dict[str, Any]) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    tod_did_this = str(payload.get("tod_did_this") or "").strip()
    tod_next_action = _compact_text(payload.get("tod_next_action"), 220)
    current_tod_state = str(payload.get("current_tod_state") or "unknown").strip() or "unknown"
    current_mim_state = str(payload.get("current_mim_state") or "unknown").strip() or "unknown"
    current_profile = ""
    current_profile_label = ""
    match = re.match(r"^idle_training_profile_(?:started|failed):(?P<profile>[A-Za-z0-9_.-]+)(?::(?P<reason>.*))?$", tod_did_this)
    if match:
        current_profile = str(match.group("profile") or "").strip().lower()
    elif bool(training_status.get("active")):
        current_profile = "runtime_safe_subset"

    if current_profile == "repo_edit_test_recover":
        current_profile_label = "Repo edit / test / recover pack"
    elif current_profile == "runtime_safe_subset":
        current_profile_label = "Runtime-safe validation subset"

    activity_summary = tod_next_action or _compact_text(tod_did_this, 220) or "No autonomy activity is currently published."
    return {
        "continuous_idle_enabled": True,
        "idle_threshold_minutes": 0,
        "simulation_cooldown_minutes": 0,
        "solicitation_cooldown_minutes": 60,
        "long_idle_profile_threshold_minutes": 30,
        "short_idle_profile": "runtime_safe_subset",
        "short_idle_profile_label": "Runtime-safe validation subset",
        "long_idle_profile": "repo_edit_test_recover",
        "long_idle_profile_label": "Repo edit / test / recover pack",
        "policy_summary": "TOD should train on every idle cycle. Short idle windows stay on the runtime-safe subset, and long idle windows escalate into the repo edit / test / recover pack.",
        "activity_summary": activity_summary,
        "current_tod_state": current_tod_state,
        "current_mim_state": current_mim_state,
        "current_profile": current_profile,
        "current_profile_label": current_profile_label,
    }


def _sanitize_session_key(value: Any) -> str:
    raw = str(value or "tod-console-public").strip() or "tod-console-public"
    collapsed = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")
    if not collapsed:
        collapsed = "tod-console-public"
    if len(collapsed) <= 96:
        return collapsed
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{collapsed[:72]}-{digest}"


def _chat_session_path(session_key: str) -> Path:
    return TOD_CONSOLE_CHAT_ROOT / f"{_sanitize_session_key(session_key)}.json"


def _chat_state_marker(state: dict[str, Any]) -> dict[str, str]:
    quick_facts = state.get("quick_facts") if isinstance(state.get("quick_facts"), dict) else {}
    status = state.get("status") if isinstance(state.get("status"), dict) else {}
    return {
        "canonical_objective": _normalize_objective_token(quick_facts.get("canonical_objective")),
        "status_code": str(status.get("code") or "").strip().lower(),
    }


def _tod_ui_media_url(asset_name: str) -> str:
    return f"/tod/ui/chat/media/{asset_name}"


def _normalize_chat_attachment(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    url = str(value.get("url") or value.get("thumbnail_url") or "").strip()
    if not url:
        return None
    return {
        "kind": str(value.get("kind") or "image").strip() or "image",
        "url": url,
        "thumbnail_url": str(value.get("thumbnail_url") or url).strip() or url,
        "mime_type": str(value.get("mime_type") or "").strip().lower(),
        "filename": str(value.get("filename") or "image").strip() or "image",
        "size_bytes": _safe_int(value.get("size_bytes"), 0),
        "sha256": str(value.get("sha256") or "").strip(),
        "local_path": str(value.get("local_path") or "").strip(),
    }


def _persist_public_chat_image(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    mime_type = str(payload.get("mime_type") or "").strip().lower()
    if mime_type not in TOD_UI_ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="unsupported_image_type")
    data_url = str(payload.get("data_url") or "").strip()
    match = re.match(r"^data:(?P<mime>[-\w.+/]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$", data_url, re.DOTALL)
    if not match:
        raise HTTPException(status_code=400, detail="invalid_image_payload")
    matched_mime = str(match.group("mime") or "").strip().lower()
    if matched_mime != mime_type:
        raise HTTPException(status_code=400, detail="image_mime_mismatch")
    try:
        raw_bytes = base64.b64decode(match.group("data"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid_image_payload") from exc
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="empty_image_upload")
    if len(raw_bytes) > TOD_UI_MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image_too_large")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    filename = str(payload.get("filename") or "shared-image").strip() or "shared-image"
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(filename).stem).strip("-._") or "shared-image"
    extension = TOD_UI_ALLOWED_IMAGE_TYPES[mime_type]
    asset_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{digest[:12]}-{safe_stem[:48]}{extension}"
    TOD_CONSOLE_CHAT_MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    asset_path = TOD_CONSOLE_CHAT_MEDIA_ROOT / asset_name
    if not asset_path.exists():
        asset_path.write_bytes(raw_bytes)
    return {
        "kind": "image",
        "url": _tod_ui_media_url(asset_name),
        "thumbnail_url": _tod_ui_media_url(asset_name),
        "mime_type": mime_type,
        "filename": filename,
        "size_bytes": len(raw_bytes),
        "sha256": digest,
        "local_path": str(asset_path),
    }


def _should_reset_public_chat_session(payload: dict[str, Any], state: dict[str, Any]) -> bool:
    current_marker = _chat_state_marker(state)
    current_objective = current_marker.get("canonical_objective") or ""
    current_status = current_marker.get("status_code") or ""
    stored_marker = payload.get("state_marker") if isinstance(payload.get("state_marker"), dict) else {}
    stored_objective = _normalize_objective_token(stored_marker.get("canonical_objective"))
    stored_status = str(stored_marker.get("status_code") or "").strip().lower()

    if current_objective and stored_objective and current_objective != stored_objective:
        return True
    if current_status == "aligned" and stored_status and stored_status != "aligned":
        return True
    if current_status == "aligned" and not stored_marker:
        return True
    return False


def _has_only_generated_progress_messages(payload: dict[str, Any]) -> bool:
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    pending_progress = payload.get("pending_progress") if isinstance(payload.get("pending_progress"), list) else []
    if pending_progress or not messages:
        return False
    return all(isinstance(item, dict) and _is_generated_progress_message(item) for item in messages)


def _normalize_chat_entries(values: Any, limit: int = 40) -> list[dict[str, Any]]:
    values = values if isinstance(values, list) else []
    messages: list[dict[str, Any]] = []
    for item in values[-limit:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or item.get("actor") or "tod").strip().lower() or "tod"
        content = _compact_text(item.get("content") or item.get("message") or item.get("text"), 4000)
        created_at = str(item.get("created_at") or item.get("generated_at") or item.get("timestamp") or "").strip()
        if not content:
            continue
        normalized: dict[str, Any] = {
            "role": role,
            "content": content,
            "created_at": created_at or _utc_now_iso(),
        }
        author_name = _friendly_person_name(item.get("author_name"))
        if author_name:
            normalized["author_name"] = author_name
        attachment = _normalize_chat_attachment(item.get("attachment"))
        if attachment:
            normalized["attachment"] = attachment
        messages.append(normalized)
    return messages


def _load_chat_session_payload(session_key: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _load_json(_chat_session_path(session_key))
    safe_session_key = _sanitize_session_key(session_key)
    is_public_tod_session = safe_session_key.startswith("tod-console-public")
    if state and is_public_tod_session and (
        _should_reset_public_chat_session(payload, state)
        or _has_only_generated_progress_messages(payload)
    ):
        payload = {}
    return {
        "session_key": safe_session_key,
        "updated_at": str(payload.get("updated_at") or "").strip(),
        "messages": _normalize_chat_entries(payload.get("messages"), limit=40),
        "pending_progress": _normalize_chat_entries(payload.get("pending_progress"), limit=12),
    }


def _save_chat_session_payload(session_key: str, payload: dict[str, Any], state: dict[str, Any] | None = None) -> None:
    path = _chat_session_path(session_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "session_key": _sanitize_session_key(session_key),
        "updated_at": _utc_now_iso(),
        "messages": _normalize_chat_entries(payload.get("messages"), limit=40),
        "pending_progress": _normalize_chat_entries(payload.get("pending_progress"), limit=12),
    }
    if state:
        document["state_marker"] = _chat_state_marker(state)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def _load_chat_messages(session_key: str, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = _load_chat_session_payload(session_key, state)
    return list(payload.get("messages") or [])


def _save_chat_messages(session_key: str, messages: list[dict[str, Any]], state: dict[str, Any] | None = None) -> None:
    _save_chat_session_payload(
        session_key,
        {
            "messages": messages[-40:],
            "pending_progress": [],
        },
        state,
    )


def _summarize_requested_task(message: str, limit: int = 180) -> str:
    cleaned = re.sub(r"^\s*tod[\s,:-]*", "", str(message or ""), flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return _compact_text(cleaned or message or "the requested repair", limit)


def _recent_chat_attachments(messages: list[dict[str, Any]], limit: int = 1) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for item in reversed(messages):
        if not isinstance(item, dict):
            continue
        attachment = _normalize_chat_attachment(item.get("attachment"))
        if not attachment:
            continue
        attachments.append(attachment)
        if len(attachments) >= limit:
            break
    attachments.reverse()
    return attachments


def _advance_pending_chat_progress(session_key: str, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    payload = _load_chat_session_payload(session_key, state)
    pending = list(payload.get("pending_progress") or [])
    messages = list(payload.get("messages") or [])
    if not pending:
        return messages
    next_item = dict(pending.pop(0))
    next_item["created_at"] = str(next_item.get("created_at") or _utc_now_iso()).strip() or _utc_now_iso()
    messages.append(next_item)
    payload["messages"] = messages[-40:]
    payload["pending_progress"] = pending
    _save_chat_session_payload(session_key, payload, state)
    return list(payload.get("messages") or [])


def _candidate_script_paths(script_name: str) -> list[Path]:
    return [
        PROJECT_ROOT / "scripts" / script_name,
        PROJECT_ROOT.parent / "scripts" / script_name,
    ]


def _first_existing_path(*paths: Path) -> Path | None:
    for path in paths:
        try:
            if path.exists():
                return path
        except OSError:
            continue
    return None


def _powershell_runner() -> str:
    for candidate in ("pwsh", "powershell", "powershell.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return ""


def _systemctl_runner() -> str:
    return shutil.which("systemctl") or ""


def _resolve_training_objective_id(state: dict[str, Any]) -> str:
    quick_facts = state.get("quick_facts") if isinstance(state.get("quick_facts"), dict) else {}
    alignment = state.get("objective_alignment") if isinstance(state.get("objective_alignment"), dict) else {}
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    objective_text = _pick_first_text(
        live_task.get("objective_id"),
        live_task.get("normalized_objective_id"),
        quick_facts.get("live_request_objective"),
        quick_facts.get("canonical_objective"),
        alignment.get("tod_current_objective"),
        alignment.get("mim_objective_active"),
    )
    objective_token = _normalize_objective_token(objective_text)
    return f"objective-{objective_token}" if objective_token else ""


def _resolve_training_request() -> dict[str, Any]:
    request_path = SHARED_RUNTIME_ROOT / "MIM_TOD_TASK_REQUEST.latest.json"
    trigger_path = SHARED_RUNTIME_ROOT / "MIM_TO_TOD_TRIGGER.latest.json"
    return {
        "available": True,
        "reason": "ready",
        "launcher_type": "mim_to_tod_bridge_request",
        "request_path": str(request_path),
        "trigger_path": str(trigger_path),
        "tod_action": "start-training-runbook",
    }


def _publish_task_execution_request(message: str, state: dict[str, Any], surface: str, session_key: str) -> dict[str, Any]:
    started_at = _utc_now_iso()
    request_path = SHARED_RUNTIME_ROOT / "MIM_TOD_TASK_REQUEST.latest.json"
    trigger_path = SHARED_RUNTIME_ROOT / "MIM_TO_TOD_TRIGGER.latest.json"
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    quick_facts = state.get("quick_facts") if isinstance(state.get("quick_facts"), dict) else {}
    authoritative_objective_id, authoritative_task_id = _resolve_operator_action_ids(state)
    prompt_objective_id = _extract_labeled_prompt_value(message, "OBJECTIVE_ID")
    # If OBJECTIVE_ID: label not found, try extracting from OBJECTIVE: prefix
    if not prompt_objective_id:
        prompt_objective_id = _extract_objective_from_prefix(message)
    prompt_title = _extract_labeled_prompt_value(message, "TITLE")
    prompt_mission = _extract_labeled_prompt_value(message, "MISSION")
    prompt_primary_outcome = _extract_labeled_prompt_value(message, "PRIMARY OUTCOME")
    objective_id = _pick_first_text(
        prompt_objective_id,
        authoritative_objective_id,
        str(live_task.get("objective_id") or "").strip(),
        str(live_task.get("normalized_objective_id") or "").strip(),
        str(quick_facts.get("canonical_objective") or "").strip(),
    ) or "objective-unknown"
    normalized_objective = _normalize_objective_token(objective_id) or objective_id.lower().replace(" ", "-")
    prompt_starts_new_objective = bool(
        (
            prompt_objective_id
            and authoritative_objective_id
            and not _same_objective(prompt_objective_id, authoritative_objective_id)
        )
        or _message_declares_new_objective(message, authoritative_objective_id)
    )
    request_objective_slug = _objective_request_slug(prompt_objective_id if prompt_starts_new_objective else objective_id) or normalized_objective
    request_sequence = int(datetime.now(timezone.utc).timestamp() * 1000)
    reuse_live_identity = False if prompt_starts_new_objective else _should_reuse_live_task_identity(live_task, prompt_objective_id, authoritative_task_id)
    request_id = (
        str(live_task.get("request_id") or "").strip()
        if reuse_live_identity
        else ""
    ) or ("" if prompt_starts_new_objective else str(authoritative_task_id or "").strip()) or f"{request_objective_slug}-task-{request_sequence}"
    task_id = (
        str(live_task.get("task_id") or "").strip()
        if reuse_live_identity
        else ""
    ) or ("" if prompt_starts_new_objective else str(authoritative_task_id or "").strip()) or request_id
    correlation_id = str(("" if prompt_starts_new_objective else authoritative_task_id) or task_id or f"tod-chat-task-{request_sequence}").strip()
    title = _pick_first_text(prompt_title, _summarize_requested_task(message, 180), "TOD chat execution task")
    task_focus = _pick_first_text(prompt_title, title, _summarize_requested_task(message, 180), "the requested local execution task")
    next_validation = _next_validation_check(state)
    acceptance = _pick_first_text(prompt_primary_outcome, next_validation, "Publish bounded execution evidence and validation output.")
    description = _pick_first_text(prompt_mission, task_focus, _compact_text(message, 220), title)
    request_payload = {
        "packet_type": "mim-tod-task-request-v1",
        "generated_at": started_at,
        "source": f"tod-ui-{surface}-operator-v1",
        "target": "TOD",
        "request_id": request_id,
        "task_id": task_id,
        "objective_id": objective_id,
        "correlation_id": correlation_id,
        "sequence": request_sequence,
        "tod_action": "execute-chat-task",
        "canonical_lane_source": "shared_truth" if authoritative_task_id and not prompt_starts_new_objective else ("live_task_request" if reuse_live_identity else "ui_request"),
        "canonical_task_id": str(authoritative_task_id or task_id or "").strip() if not prompt_starts_new_objective else "",
        "title": title,
        "description": description,
        "priority": "high",
        "scope": task_focus,
        "acceptance_criteria": acceptance,
        "success_criteria": acceptance,
        "requested_outcome": acceptance,
        "task_classification": "programming",
        "capability_name": "tod_local_execution_chat_task",
        "assigned_executor": "codex",
        "content": _compact_text(message, 4000),
        "session_key": _sanitize_session_key(session_key),
    }
    request_text = json.dumps(request_payload, indent=2, ensure_ascii=True)
    request_sha256 = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
    trigger_payload = {
        "packet_type": "mim-to-tod-trigger-v1",
        "generated_at": started_at,
        "emitted_at": started_at,
        "source_actor": "MIM",
        "target_actor": "TOD",
        "source_service": f"tod-ui-{surface}",
        "trigger": request_id,
        "artifact": request_path.name,
        "artifact_path": str(request_path),
        "artifact_sha256": request_sha256,
        "task_id": task_id,
        "correlation_id": correlation_id,
    }
    record: dict[str, Any] = {
        "generated_at": started_at,
        "action": "publish_task_execution_request",
        "surface": surface,
        "session_key": _sanitize_session_key(session_key),
        "request_id": request_id,
        "task_id": task_id,
        "objective_id": objective_id,
        "tod_action": "execute-chat-task",
        "ok": False,
        "status": "failed",
    }
    try:
        _write_shared_json(request_path, request_payload)
        _write_shared_json(trigger_path, trigger_payload)
        record.update(
            {
                "ok": True,
                "status": "published",
                "request_path": str(request_path),
                "trigger_path": str(trigger_path),
                "scope": task_focus,
                "acceptance_criteria": acceptance,
            }
        )
    except Exception as exc:
        record.update({"error": _compact_text(exc, 220)})
    _record_operator_action(record)
    return record


def _record_operator_action(record: dict[str, Any]) -> None:
    TOD_OPERATOR_ACTION_ROOT.mkdir(parents=True, exist_ok=True)
    TOD_OPERATOR_ACTION_LATEST_PATH.write_text(json.dumps(record, indent=2), encoding="utf-8")
    _append_jsonl_record(TOD_OPERATOR_ACTION_LOG_PATH, record)


def _operator_action_refresh_status(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return _run_refresh_status_action()


def _operator_action_start_next_task(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    objective_id, task_id = _resolve_operator_action_ids(state)
    outcome = _pick_first_text(
        payload.get("requested_outcome"),
        _next_validation_check(state),
        "Publish fresh execution evidence for the next authoritative task and report any blocker immediately.",
    )
    message = "\n".join(
        [
            f"OBJECTIVE_ID: {objective_id or 'objective-unknown'}",
            f"TITLE: Start next task for {task_id or 'the active task'}",
            "MISSION: Resume the next bounded task from the authoritative TOD/MIM execution context and publish current execution evidence.",
            f"PRIMARY OUTCOME: {outcome}",
        ]
    )
    result = _publish_task_execution_request(message, state, surface="operator-actions", session_key="operator-actions")
    artifact_paths = [str(result.get("request_path") or "").strip(), str(result.get("trigger_path") or "").strip()]
    return {
        "ok": bool(result.get("ok")),
        "status": "queued" if bool(result.get("ok")) else "failed",
        "message": "Published a start-next-task execution request." if bool(result.get("ok")) else _pick_first_text(result.get("error"), "Unable to publish the next-task execution request."),
        "artifact_paths": [item for item in artifact_paths if item],
        "command": ["publish_task_execution_request"],
        "request": result,
    }


def _operator_action_force_replay(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    objective_id = str(payload.get("objective_id") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    if not objective_id or not task_id:
        derived_objective_id, derived_task_id = _resolve_operator_action_ids(state)
        objective_id = objective_id or derived_objective_id
        task_id = task_id or derived_task_id
    if not objective_id or not task_id:
        raise HTTPException(status_code=400, detail={"error": "missing_replay_context", "message": "Objective and task identifiers are required for forced replay."})
    script_path = _script_path("Invoke-TODForcedExecutionReplay.ps1")
    if not script_path.exists():
        return {"ok": False, "status": "failed", "message": "Forced replay script is missing.", "artifact_paths": [], "command": []}
    reason = _pick_first_text(payload.get("reason"), "Operator requested replay from the TOD/MIM operator console.")
    command = [
        _powershell_runner(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-ObjectiveId",
        objective_id,
        "-TaskId",
        task_id,
        "-Reason",
        reason,
    ]
    result = _run_operator_command(command)
    result["artifact_paths"] = [str(SHARED_RUNTIME_ROOT / "MIM_TOD_TASK_REQUEST.latest.json"), str(SHARED_RUNTIME_ROOT / "MIM_TO_TOD_TRIGGER.latest.json")]
    return result


def _operator_action_validate_current_task(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    command = _resolve_safe_validation_command(state)
    if not command:
        return {
            "ok": False,
            "status": "blocked",
            "message": "No safe validation command is available for the current task.",
            "artifact_paths": [],
            "command": [],
        }
    result = _run_operator_command(command)
    result["artifact_paths"] = [str(SHARED_RUNTIME_ROOT / "TOD_AGENT_MIM_LOCAL_VERIFICATION_RESULTS.latest.json")]
    return result


def _operator_action_recover_stale_state(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    script_path = _script_path("Invoke-TODMimRemoteRecovery.ps1")
    if not script_path.exists():
        return {"ok": False, "status": "failed", "message": "Remote recovery script is missing.", "artifact_paths": [], "command": []}
    command = [_powershell_runner(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), "-EmitJson"]
    result = _run_operator_command(command)
    result["artifact_paths"] = [str(SHARED_RUNTIME_ROOT / "remote_recovery" / "TOD_MIM_REMOTE_RECOVERY.latest.json")]
    return result


def _operator_action_show_evidence(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    _write_operator_evidence_snapshot(state)
    return {
        "ok": True,
        "status": "completed",
        "message": "Operator evidence snapshot refreshed.",
        "artifact_paths": [str(TOD_OPERATOR_EVIDENCE_PATH)],
        "command": ["write_operator_evidence_snapshot"],
    }


def _operator_action_pause_current_objective(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    artifact_paths = _write_execution_runtime_transition(
        state,
        action_id="pause_current_objective",
        status="paused",
        execution_state="paused_by_operator",
        summary="Paused the active TOD objective from the objective card control surface.",
        current_action="Paused the current objective on the TOD operator console.",
        next_step="Resume the current objective from the control surface when ready to continue execution.",
        wait_reason="Operator pause is active on this objective.",
        extra_evidence={"paused_by_operator": True},
    )
    return {
        "ok": True,
        "status": "completed",
        "message": "Paused the active objective and published paused runtime evidence.",
        "artifact_paths": artifact_paths,
        "command": ["write_execution_runtime_transition", "pause_current_objective"],
    }


def _operator_action_resume_current_objective(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    publish_result = _operator_action_start_next_task(payload, state)
    artifact_paths = _write_execution_runtime_transition(
        state,
        action_id="resume_current_objective",
        status="waiting",
        execution_state="resume_requested",
        summary="Published a resume request for the active TOD objective and waiting for fresh execution evidence.",
        current_action="Published the next execution request from the objective card resume control.",
        next_step="Wait for new execution evidence or force replay if the executor remains stale.",
        wait_reason="Resume was requested from the TOD operator console.",
        extra_evidence={"resume_requested": True},
    )
    publish_paths = publish_result.get("artifact_paths") if isinstance(publish_result.get("artifact_paths"), list) else []
    return {
        "ok": bool(publish_result.get("ok")),
        "status": "queued" if bool(publish_result.get("ok")) else "failed",
        "message": "Published a resume request and updated runtime state for the active objective." if bool(publish_result.get("ok")) else _pick_first_text(publish_result.get("message"), "Unable to publish a resume request for the active objective."),
        "artifact_paths": [*artifact_paths, *publish_paths],
        "command": ["publish_task_execution_request", "resume_current_objective"],
        "request": publish_result.get("request") if isinstance(publish_result.get("request"), dict) else {},
    }


def _operator_action_rollback_current_task(payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    rollback_metadata = _resolve_rollback_metadata(state)
    source_path, destination_path = _parse_copy_item_restore_paths(rollback_metadata.get("hint") or "")
    if source_path is None or destination_path is None:
        return {
            "ok": False,
            "status": "blocked",
            "message": "No safe rollback hint is available for the current task.",
            "artifact_paths": [],
            "command": [],
        }
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    artifact_paths = _write_execution_runtime_transition(
        state,
        action_id="rollback_current_task",
        status="waiting",
        execution_state="rollback_applied",
        summary=f"Applied rollback metadata for the active task and restored {destination_path.name}.",
        current_action=f"Restored {destination_path.name} from the latest published rollback point.",
        next_step="Re-run the focused validation path and republish fresh execution evidence for the restored task.",
        wait_reason="Rollback was applied from the TOD operator console.",
        rollback_state="applied",
        recovery_state="rollback_applied",
        command_output=f"Restored {destination_path} from {source_path}.",
        extra_evidence={
            "rollback_applied": True,
            "rollback_source_path": str(source_path),
            "rollback_destination_path": str(destination_path),
            "rollback_hint": rollback_metadata.get("hint") or "",
        },
    )
    return {
        "ok": True,
        "status": "completed",
        "message": f"Applied rollback metadata and restored {destination_path.name}.",
        "artifact_paths": [str(source_path), str(destination_path), *artifact_paths],
        "command": ["copy2", str(source_path), str(destination_path)],
    }


def _execute_operator_action(action: str, payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if action == "refresh_status":
        return _operator_action_refresh_status(payload, state)
    if action == "run_shared_truth_reconciliation":
        return _run_reconcile_shared_truth_action()
    if action == "start_next_task":
        return _operator_action_start_next_task(payload, state)
    if action == "force_replay_current_task":
        return _operator_action_force_replay(payload, state)
    if action == "validate_current_task":
        return _operator_action_validate_current_task(payload, state)
    if action == "recover_stale_state":
        return _operator_action_recover_stale_state(payload, state)
    if action == "show_evidence":
        return _operator_action_show_evidence(payload, state)
    if action == "pause_current_objective":
        return _operator_action_pause_current_objective(payload, state)
    if action == "resume_current_objective":
        return _operator_action_resume_current_objective(payload, state)
    if action == "rollback_current_task":
        return _operator_action_rollback_current_task(payload, state)
    raise HTTPException(status_code=400, detail={"error": "unknown_action", "message": f"Unknown operator action: {action}"})


def _extract_identifier_token(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"[A-Za-z0-9][A-Za-z0-9._:-]*", text)
    if not match:
        return _compact_text(text, 220)
    return match.group(0).rstrip(".,;:)]}>")


def _extract_labeled_prompt_value(message: str, label: str) -> str:
    text = str(message or "")
    lines = text.splitlines()
    normalized_label = re.sub(r"[_\s-]+", r"[_\\s-]+", str(label or "").strip())
    label_pattern = re.compile(rf"^\s*(?:(?:[-*]|#{{1,6}})\s*)?{normalized_label}\s*:\s*(.*)$", re.IGNORECASE)
    next_label_pattern = re.compile(r"^\s*(?:(?:[-*]|#{1,6})\s*)?[A-Z][A-Za-z0-9_]*(?:[ _-][A-Z][A-Za-z0-9_]*)*\s*:\s*(.*)$")
    identifier_labels = {"initiative_id", "objective_id", "task_id", "request_id"}
    normalized_label_key = re.sub(r"[^a-z0-9]+", "_", str(label or "").strip().lower()).strip("_")
    for index, line in enumerate(lines):
        match = label_pattern.match(line)
        if not match:
            continue
        if normalized_label_key in identifier_labels:
            return _extract_identifier_token(match.group(1))
        collected = [str(match.group(1) or "").strip()]
        for next_line in lines[index + 1 :]:
            if next_label_pattern.match(next_line):
                break
            stripped = str(next_line or "").strip()
            if stripped:
                collected.append(stripped)
        return _compact_text(" ".join(item for item in collected if item), 220)
    pattern = re.compile(
        rf"(?:^|\b){normalized_label}\s*:\s*(.+?)(?=\s+(?:(?:[-*]|#{{1,6}})\s*)?[A-Z][A-Za-z0-9_]*(?:[ _-][A-Z][A-Za-z0-9_]*)*\s*:|$)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return ""
    value = match.group(1)
    if normalized_label_key in identifier_labels:
        return _extract_identifier_token(value)
    return _compact_text(value, 220)


def _write_shared_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _load_execution_feedback_config() -> dict[str, Any]:
    config_path = PROJECT_ROOT / "tod" / "config" / "tod-config.json"
    payload = _load_json(config_path)
    feedback = payload.get("execution_feedback") if isinstance(payload.get("execution_feedback"), dict) else {}
    base_url = str(payload.get("mim_base_url") or "").strip() or TOD_EXECUTION_FEEDBACK_DEFAULT_BASE_URL
    timeout_seconds = payload.get("timeout_seconds")
    try:
        resolved_timeout = max(1, int(timeout_seconds))
    except Exception:
        resolved_timeout = 15
    return {
        "base_url": base_url.rstrip("/"),
        "source": str(feedback.get("source") or "tod").strip() or "tod",
        "auth_token": str(feedback.get("auth_token") or "").strip(),
        "timeout_seconds": resolved_timeout,
    }


def _post_execution_feedback(base_url: str, execution_id: str, payload: dict[str, Any], auth_token: str = "", timeout_seconds: int = 15) -> dict[str, Any]:
    normalized_base = str(base_url or "").strip().rstrip("/") or TOD_EXECUTION_FEEDBACK_DEFAULT_BASE_URL
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_execution_id:
        return {"ok": False, "reason": "missing_execution_id"}
    request_url = f"{normalized_base}/gateway/capabilities/executions/{normalized_execution_id}/feedback"
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    request = urllib.request.Request(request_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=max(1, int(timeout_seconds))) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            return {
                "ok": True,
                "status_code": int(getattr(response, "status", 200) or 200),
                "url": request_url,
                "response": _compact_text(response_body, 220),
            }
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status_code": int(exc.code),
            "url": request_url,
            "reason": "http_error",
            "error": _compact_text(error_body or exc.reason, 220),
        }
    except Exception as exc:
        return {
            "ok": False,
            "url": request_url,
            "reason": "error",
            "error": _compact_text(exc, 220),
        }


def _publish_execution_feedback_async(execution_id: str, task_id: str, objective_id: str, source: str, summary: str, current_action: str) -> dict[str, Any]:
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_execution_id:
        return {"queued": False, "reason": "missing_execution_id"}
    if not normalized_execution_id.isdigit():
        return {"queued": False, "reason": "non_numeric_execution_id", "execution_id": normalized_execution_id}

    config = _load_execution_feedback_config()
    accepted_payload = {
        "status": "accepted",
        "source": source,
        "task_id": task_id,
        "timestamp": _utc_now_iso(),
        "details": {
            "objective_id": objective_id,
            "reason": "tod accepted execution",
            "runtime_outcome": "",
            "recovery_state": "",
            "summary": summary,
        },
    }
    running_payload = {
        "status": "running",
        "source": source,
        "task_id": task_id,
        "timestamp": _utc_now_iso(),
        "details": {
            "objective_id": objective_id,
            "reason": current_action,
            "runtime_outcome": "",
            "recovery_state": "",
            "summary": summary,
        },
    }

    def _worker() -> None:
        attempts = [
            _post_execution_feedback(
                config["base_url"],
                normalized_execution_id,
                accepted_payload,
                auth_token=str(config.get("auth_token") or ""),
                timeout_seconds=int(config.get("timeout_seconds") or 15),
            ),
            _post_execution_feedback(
                config["base_url"],
                normalized_execution_id,
                running_payload,
                auth_token=str(config.get("auth_token") or ""),
                timeout_seconds=int(config.get("timeout_seconds") or 15),
            ),
        ]
        _record_operator_action(
            {
                "generated_at": _utc_now_iso(),
                "action": "publish_execution_feedback",
                "execution_id": normalized_execution_id,
                "task_id": task_id,
                "objective_id": objective_id,
                "ok": all(bool(item.get("ok")) for item in attempts),
                "status": "published" if all(bool(item.get("ok")) for item in attempts) else "partial_failure",
                "attempts": attempts,
            }
        )

    thread = threading.Thread(target=_worker, name=f"tod-ui-feedback-{normalized_execution_id}", daemon=True)
    thread.start()
    return {
        "queued": True,
        "base_url": config["base_url"],
        "execution_id": normalized_execution_id,
    }


def _publish_local_execution_ack(message: str, state: dict[str, Any], surface: str, session_key: str) -> dict[str, Any]:
    started_at = _utc_now_iso()
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    quick_facts = state.get("quick_facts") if isinstance(state.get("quick_facts"), dict) else {}
    authoritative_objective_id, authoritative_task_id = _resolve_operator_action_ids(state)
    prompt_objective_id = _extract_labeled_prompt_value(message, "OBJECTIVE_ID")
    # If OBJECTIVE_ID: label not found, try extracting from OBJECTIVE: prefix
    if not prompt_objective_id:
        prompt_objective_id = _extract_objective_from_prefix(message)
    prompt_title = _extract_labeled_prompt_value(message, "TITLE")
    prompt_mission = _extract_labeled_prompt_value(message, "MISSION")
    prompt_primary_outcome = _extract_labeled_prompt_value(message, "PRIMARY OUTCOME")
    objective_id = _pick_first_text(
        prompt_objective_id,
        authoritative_objective_id,
        str(live_task.get("objective_id") or "").strip(),
        str(live_task.get("normalized_objective_id") or "").strip(),
        str(quick_facts.get("canonical_objective") or "").strip(),
    ) or "objective-unknown"
    normalized_objective = _normalize_objective_token(objective_id) or objective_id.lower().replace(" ", "-")
    prompt_starts_new_objective = bool(
        (
            prompt_objective_id
            and authoritative_objective_id
            and not _same_objective(prompt_objective_id, authoritative_objective_id)
        )
        or _message_declares_new_objective(message, authoritative_objective_id)
    )
    request_objective_slug = _objective_request_slug(prompt_objective_id if prompt_starts_new_objective else objective_id) or normalized_objective
    request_sequence = int(datetime.now(timezone.utc).timestamp() * 1000)
    reuse_live_identity = False if prompt_starts_new_objective else _should_reuse_live_task_identity(live_task, prompt_objective_id, authoritative_task_id)
    request_id = (
        str(live_task.get("request_id") or "").strip()
        if reuse_live_identity
        else ""
    ) or ("" if prompt_starts_new_objective else str(authoritative_task_id or "").strip()) or f"{request_objective_slug}-task-{request_sequence}"
    task_id = (
        str(live_task.get("task_id") or "").strip()
        if reuse_live_identity
        else ""
    ) or ("" if prompt_starts_new_objective else str(authoritative_task_id or "").strip()) or request_id
    execution_id = (
        str(live_task.get("execution_id") or "").strip()
        if reuse_live_identity
        else ""
    ) or ("" if prompt_starts_new_objective else str(authoritative_task_id or "").strip()) or request_id
    existing_runtime = _load_existing_execution_runtime_payloads()
    if _existing_runtime_matches_active_execution(existing_runtime, objective_id, task_id, execution_id):
        existing_execution = existing_runtime.get("execution_result") if isinstance(existing_runtime.get("execution_result"), dict) else {}
        existing_active_task = existing_runtime.get("active_task") if isinstance(existing_runtime.get("active_task"), dict) else {}
        existing_summary = _compact_text(
            existing_execution.get("summary")
            or existing_active_task.get("summary")
            or "TOD preserved the current local execution runtime state for the active task.",
            220,
        )
        existing_current_action = _compact_text(
            existing_execution.get("current_action")
            or existing_active_task.get("current_action")
            or "Preserving the current local execution runtime state.",
            220,
        )
        existing_next_step = _compact_text(
            existing_execution.get("next_step")
            or existing_active_task.get("next_step")
            or "Continue the active bounded local execution without resetting runtime progress.",
            220,
        )
        existing_next_validation = str(
            existing_execution.get("next_validation")
            or existing_active_task.get("next_validation")
            or _next_validation_check(state)
        ).strip()
        feedback_publish = _publish_execution_feedback_async(
            execution_id=execution_id,
            task_id=task_id,
            objective_id=objective_id,
            source=f"tod-ui-{surface}-operator-v1",
            summary=existing_summary,
            current_action=existing_current_action,
        )
        record = {
            "generated_at": started_at,
            "action": "publish_local_execution_ack",
            "surface": surface,
            "request_id": request_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "objective_id": objective_id,
            "ok": True,
            "status": "preserved",
            "summary": existing_summary,
            "current_action": existing_current_action,
            "next_step": existing_next_step,
            "next_validation": existing_next_validation,
            "execution_summary": existing_summary,
            "execution_evidence": existing_execution.get("execution_evidence")
            if isinstance(existing_execution.get("execution_evidence"), dict)
            else existing_active_task.get("execution_evidence")
            if isinstance(existing_active_task.get("execution_evidence"), dict)
            else {},
            "gateway_feedback": feedback_publish,
            "preserved_existing_execution": True,
        }
        _record_operator_action(record)
        return record
    title = _pick_first_text(prompt_title, _summarize_requested_task(message, 180), "TOD local execution task")
    task_focus = _pick_first_text(_summarize_requested_task(message, 180), title, "the requested local execution task")
    next_validation = _next_validation_check(state)
    evidence = _strongest_evidence(state)
    summary = f"TOD accepted {task_focus} and published execution confirmation for the active objective."
    current_action = "Publishing local execution confirmation and phase-1 execution artifacts."
    next_step = "Continue the task through bounded step execution, validation, evidence publication, and next-step selection."
    artifacts = build_execution_loop_contract_artifacts(
        started_at=started_at,
        source=f"tod-ui-{surface}-operator-v1",
        surface=surface,
        session_key=_sanitize_session_key(session_key),
        request_id=request_id,
        task_id=task_id,
        execution_id=execution_id,
        objective_id=objective_id,
        normalized_objective_id=normalized_objective,
        title=title,
        summary=summary,
        task_focus=task_focus,
        mission=prompt_mission,
        primary_outcome=prompt_primary_outcome,
        strongest_evidence=evidence,
        next_validation=next_validation,
    )
    base_payload = artifacts["base_payload"]
    active_objective_payload = artifacts["active_objective_payload"]
    active_task_payload = artifacts["active_task_payload"]
    activity_event = artifacts["activity_event"]
    validation_payload = artifacts["validation_payload"]
    execution_result_payload = artifacts["execution_result_payload"]
    execution_truth_payload = artifacts["execution_truth_payload"]
    inspection_result = execute_bounded_local_inspection(
        workspace_root=PROJECT_ROOT.parent,
        project_root=PROJECT_ROOT,
        task_focus=task_focus,
        next_validation=next_validation,
    )
    inspection_status = str(inspection_result.get("status") or "blocked").strip().lower()
    inspection_ok = bool(inspection_result.get("validation_passed")) and inspection_status == "completed"
    inspection_updated_at = _utc_now_iso()
    current_action = _compact_text(inspection_result.get("current_action"), 220)
    next_step = _compact_text(inspection_result.get("next_step"), 220)
    execution_summary = _compact_text(inspection_result.get("summary"), 220)
    active_task_payload.update(
        {
            "status": "running" if inspection_ok else "blocked",
            "execution_state": "waiting_on_next_step" if inspection_ok else "blocked",
            "current_action": current_action,
            "next_step": next_step,
            "next_validation": str(inspection_result.get("next_validation") or next_validation).strip(),
            "wait_target": str(inspection_result.get("wait_target") or "").strip(),
            "wait_target_label": str(inspection_result.get("wait_target_label") or "").strip(),
            "wait_reason": _compact_text(inspection_result.get("wait_reason"), 220),
            "summary": execution_summary,
            "updated_at": inspection_updated_at,
            "execution_evidence": inspection_result,
        }
    )
    active_objective_payload.update(
        {
            "updated_at": inspection_updated_at,
            "summary": execution_summary,
            "execution_evidence": inspection_result,
        }
    )
    execution_contract = active_task_payload.get("execution_contract") if isinstance(active_task_payload.get("execution_contract"), dict) else {}
    if execution_contract:
        bounded_step_planner = execution_contract.get("bounded_step_planner") if isinstance(execution_contract.get("bounded_step_planner"), dict) else {}
        active_step = bounded_step_planner.get("active_step") if isinstance(bounded_step_planner.get("active_step"), dict) else {}
        if active_step:
            active_step.update(
                {
                    "status": inspection_status,
                    "summary": execution_summary,
                    "observed_files": inspection_result.get("matched_files") or [],
                }
            )
        bounded_step_planner.update(
            {
                "status": inspection_status,
                "next_validation": str(inspection_result.get("next_validation") or next_validation).strip(),
            }
        )
        command_runner = execution_contract.get("command_runner") if isinstance(execution_contract.get("command_runner"), dict) else {}
        command_runner.update(
            {
                "status": "completed" if inspection_ok else "blocked",
                "summary": _compact_text(inspection_result.get("command_output"), 220),
                "mode": "filesystem_inspection",
            }
        )
        patch_writer = execution_contract.get("patch_writer") if isinstance(execution_contract.get("patch_writer"), dict) else {}
        patch_writer.update(
            {
                "status": "pending",
                "summary": "No patch has been prepared yet; the local execution loop completed workspace inspection first.",
            }
        )
        validator = execution_contract.get("validator") if isinstance(execution_contract.get("validator"), dict) else {}
        validator.update(
            {
                "status": "passed" if inspection_ok else "blocked",
                "target": str(inspection_result.get("next_validation") or next_validation).strip(),
                "summary": execution_summary,
                "checks": inspection_result.get("validation_checks") or [],
            }
        )
        result_publisher = execution_contract.get("result_publisher") if isinstance(execution_contract.get("result_publisher"), dict) else {}
        result_publisher.update(
            {
                "status": "completed" if inspection_ok else "blocked",
                "latest_summary": execution_summary,
            }
        )
        execution_contract["status"] = "running" if inspection_ok else "blocked"
        active_task_payload["execution_contract"] = execution_contract
        active_objective_payload["execution_contract"] = execution_contract
    activity_event.update(
        {
            "event": "bounded_step_completed" if inspection_ok else "bounded_step_blocked",
            "status": "waiting" if inspection_ok else "blocked",
            "phase": "workspace_inspection",
            "current_action": current_action,
            "next_step": next_step,
            "next_validation": str(inspection_result.get("next_validation") or next_validation).strip(),
            "wait_target": str(inspection_result.get("wait_target") or "").strip(),
            "wait_target_label": str(inspection_result.get("wait_target_label") or "").strip(),
            "wait_reason": _compact_text(inspection_result.get("wait_reason"), 220),
            "summary": execution_summary,
            "updated_at": inspection_updated_at,
            "execution_state": "waiting_on_next_step" if inspection_ok else "blocked",
            "execution_evidence": inspection_result,
        }
    )
    validation_payload.update(
        {
            "status": "passed" if inspection_ok else "blocked",
            "phase": "workspace_inspection",
            "validation_target": str(inspection_result.get("next_validation") or next_validation).strip(),
            "summary": execution_summary,
            "updated_at": inspection_updated_at,
            "checks": inspection_result.get("validation_checks") or [],
            "evidence": {
                "matched_files": inspection_result.get("matched_files") or [],
                "command_output": inspection_result.get("command_output") or "",
            },
        }
    )
    execution_result_payload.update(
        {
            "execution_state": "waiting_on_next_step" if inspection_ok else "blocked",
            "status": "waiting" if inspection_ok else "blocked",
            "phase": "workspace_inspection",
            "summary": execution_summary,
            "current_action": current_action,
            "next_step": next_step,
            "wait_target": str(inspection_result.get("wait_target") or "").strip(),
            "wait_target_label": str(inspection_result.get("wait_target_label") or "").strip(),
            "wait_reason": _compact_text(inspection_result.get("wait_reason"), 220),
            "updated_at": inspection_updated_at,
            "validation_summary": execution_summary,
            "command_output": inspection_result.get("command_output") or "",
            "files_changed": inspection_result.get("files_changed") or [],
            "rollback_state": inspection_result.get("rollback_state") or "not_needed",
            "recovery_state": inspection_result.get("recovery_state") or "not_needed",
            "execution_evidence": inspection_result,
        }
    )
    truth_summary = execution_truth_payload.get("summary") if isinstance(execution_truth_payload.get("summary"), dict) else {}
    truth_summary.update(
        {
            "latest_execution_at": inspection_updated_at,
            "summary": execution_summary,
            "current_action": current_action,
            "next_step": next_step,
            "validation_passed": inspection_ok,
        }
    )
    execution_truth_payload["generated_at"] = inspection_updated_at
    execution_truth_payload["summary"] = truth_summary
    recent_truth = execution_truth_payload.get("recent_execution_truth") if isinstance(execution_truth_payload.get("recent_execution_truth"), list) else []
    if recent_truth and isinstance(recent_truth[0], dict):
        recent_truth[0].update(
            {
                "generated_at": inspection_updated_at,
                "execution_state": "waiting_on_next_step" if inspection_ok else "blocked",
                "status": "waiting" if inspection_ok else "blocked",
                "summary": execution_summary,
                "current_action": current_action,
                "next_step": next_step,
                "next_validation": str(inspection_result.get("next_validation") or next_validation).strip(),
                "validation_passed": inspection_ok,
                "execution_evidence": inspection_result,
            }
        )
    record = {
        "generated_at": started_at,
        "action": "publish_local_execution_ack",
        "surface": surface,
        "request_id": request_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "objective_id": objective_id,
        "ok": False,
        "status": "failed",
        "summary": summary,
    }
    try:
        _write_shared_json(SHARED_RUNTIME_ROOT / "TOD_ACTIVE_OBJECTIVE.latest.json", active_objective_payload)
        _write_shared_json(SHARED_RUNTIME_ROOT / "TOD_ACTIVE_TASK.latest.json", active_task_payload)
        _write_shared_json(SHARED_RUNTIME_ROOT / "TOD_ACTIVITY_STREAM.latest.json", activity_event)
        _write_shared_json(SHARED_RUNTIME_ROOT / "TOD_VALIDATION_RESULT.latest.json", validation_payload)
        _write_shared_json(SHARED_RUNTIME_ROOT / "TOD_EXECUTION_RESULT.latest.json", execution_result_payload)
        _write_shared_json(SHARED_RUNTIME_ROOT / "TOD_EXECUTION_TRUTH.latest.json", execution_truth_payload)
        feedback_publish = _publish_execution_feedback_async(
            execution_id=execution_id,
            task_id=task_id,
            objective_id=objective_id,
            source=str(base_payload["source"]),
            summary=summary,
            current_action=current_action,
        )
        record.update({
            "ok": True,
            "status": "published",
            "current_action": current_action,
            "next_step": next_step,
            "next_validation": str(inspection_result.get("next_validation") or next_validation).strip(),
            "execution_summary": execution_summary,
            "execution_evidence": inspection_result,
            "gateway_feedback": feedback_publish,
        })
    except Exception as exc:
        record.update({
            "error": _compact_text(exc, 220),
            "message": "TOD execution confirmation could not be published.",
        })
    _record_operator_action(record)
    return record


def _start_training_runbook(state: dict[str, Any]) -> dict[str, Any]:
    details = _resolve_training_request()
    started_at = _utc_now_iso()
    objective_id = _resolve_training_objective_id(state)
    request_timestamp = started_at.replace("-", "").replace(":", "").replace("Z", "")
    request_sequence = int(datetime.now(timezone.utc).timestamp() * 1000)
    request_id = f"{objective_id or 'objective-0'}-task-{request_sequence}"
    correlation_id = f"training-runbook-{request_timestamp}"
    sequence = request_sequence
    record: dict[str, Any] = {
        "generated_at": started_at,
        "action": "start_training_runbook",
        "details": details,
        "ok": False,
        "status": "unavailable",
    }
    if not details.get("available"):
        record["message"] = "Training request lane is not available from this host."
        _record_operator_action(record)
        return record

    request_path = Path(str(details.get("request_path") or "").strip())
    trigger_path = Path(str(details.get("trigger_path") or "").strip())
    request_payload = {
        "packet_type": "mim-tod-task-request-v1",
        "generated_at": started_at,
        "source": "tod-ui-chat-operator-v1",
        "target": "TOD",
        "request_id": request_id,
        "task_id": request_id,
        "objective_id": objective_id,
        "correlation_id": correlation_id,
        "sequence": sequence,
        "tod_action": str(details.get("tod_action") or "start-training-runbook").strip(),
        "title": "Start TOD 6h training runbook",
        "description": "Launch the bounded TOD 6-hour training runbook asynchronously from the operator chat surface.",
        "priority": "high",
        "success_criteria": "The TOD host launches the 6-hour training runbook and begins updating training status artifacts.",
    }
    request_text = json.dumps(request_payload, indent=2, ensure_ascii=True)
    request_sha256 = hashlib.sha256(request_text.encode("utf-8")).hexdigest()
    trigger_payload = {
        "packet_type": "mim-to-tod-trigger-v1",
        "generated_at": started_at,
        "emitted_at": started_at,
        "source_actor": "MIM",
        "target_actor": "TOD",
        "source_service": "tod-ui-chat",
        "trigger": request_id,
        "artifact": request_path.name,
        "artifact_path": str(request_path),
        "artifact_sha256": request_sha256,
        "task_id": request_id,
        "correlation_id": correlation_id,
        "action_required": "execute",
        "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json",
        "sequence": sequence,
    }
    try:
        request_path.parent.mkdir(parents=True, exist_ok=True)
        trigger_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(request_text, encoding="utf-8")
        trigger_path.write_text(json.dumps(trigger_payload, indent=2, ensure_ascii=True), encoding="utf-8")
        record.update(
            {
                "ok": True,
                "status": "queued",
                "message": "Training request published to the canonical MIM->TOD listener lane.",
                "request_id": request_id,
                "task_id": request_id,
                "objective_id": objective_id,
                "correlation_id": correlation_id,
                "request_sha256": request_sha256,
            }
        )
    except Exception as exc:
        record.update(
            {
                "status": "failed_to_queue",
                "error": _compact_text(exc, 220),
                "message": "Training request could not be published to the canonical listener lane.",
            }
        )
    _record_operator_action(record)
    return record


def _format_training_start_reply(result: dict[str, Any]) -> str:
    if result.get("ok"):
        return "\n".join(
            [
                "Training request queued.",
                f"Queued at: {str(result.get('generated_at') or '').strip() or _utc_now_iso()}",
                f"Request ID: {str(result.get('request_id') or 'unknown').strip() or 'unknown'}",
                f"Objective: {str(result.get('objective_id') or 'unknown').strip() or 'unknown'}",
                f"Launcher: {str((result.get('details') or {}).get('launcher_type') or 'unknown').strip() or 'unknown'}",
                f"Action: {str((result.get('details') or {}).get('tod_action') or 'unknown').strip() or 'unknown'}",
                "Next validation: wait for listener ACK/result activity, then refresh training status and verify the newest TOD training artifacts update on this surface.",
            ]
        )
    return "\n".join(
        [
            "Training request did not queue.",
            f"Reason: {str((result.get('details') or {}).get('reason') or result.get('status') or 'unknown').strip() or 'unknown'}",
            f"Request path: {str((result.get('details') or {}).get('request_path') or 'missing').strip() or 'missing'}",
            f"Trigger path: {str((result.get('details') or {}).get('trigger_path') or 'missing').strip() or 'missing'}",
            f"Detail: {str(result.get('error') or result.get('message') or 'No launch detail is available.').strip() or 'No launch detail is available.'}",
        ]
    )


def _append_jsonl_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, separators=(",", ":")))
        handle.write("\n")


def _session_preview(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "turn_id": message.get("turn_id"),
        "from": message.get("from"),
        "to": message.get("to"),
        "message_type": message.get("message_type"),
        "summary": message.get("summary"),
        "task_id": message.get("task_id"),
        "correlation_id": message.get("correlation_id"),
        "timestamp": message.get("timestamp"),
    }


def _dialog_session_paths(session_id: str) -> dict[str, Path]:
    safe_session_id = _sanitize_session_key(session_id)
    return {
        "session": DIALOG_ROOT / f"MIM_TOD_DIALOG.session-{safe_session_id}.jsonl",
        "latest": DIALOG_ROOT / f"MIM_TOD_DIALOG.session-{safe_session_id}.latest.json",
        "index": DIALOG_ROOT / "MIM_TOD_DIALOG.sessions.latest.json",
        "log": DIALOG_ROOT / "MIM_TOD_DIALOG.latest.jsonl",
    }


def _next_dialog_turn_id(session_path: Path) -> int:
    if not session_path.exists():
        return 1
    try:
        lines = session_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 1
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except Exception:
            continue
        turn_id = payload.get("turn_id")
        if isinstance(turn_id, int) and turn_id >= 1:
            return turn_id + 1
    return 1


def _upsert_dialog_session_index(session_state: dict[str, Any]) -> None:
    index_path = _dialog_session_paths(str(session_state.get("session_id") or "unknown"))["index"]
    payload = _load_json(index_path)
    sessions = payload.get("sessions") if isinstance(payload.get("sessions"), list) else []
    filtered = [
        item
        for item in sessions
        if isinstance(item, dict) and str(item.get("session_id") or "") != str(session_state.get("session_id") or "")
    ]
    updated = {
        "generated_at": _utc_now_iso(),
        "source": DIALOG_SCHEMA_VERSION,
        "sessions": [session_state, *filtered][:200],
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(updated, indent=2), encoding="utf-8")


def _build_copilot_handoff_paths(session_id: str) -> dict[str, Path]:
    safe_session_id = _sanitize_session_key(session_id)
    return {
        "session": TOD_COPILOT_HANDOFF_ROOT / f"TOD_COPILOT_HANDOFF.{safe_session_id}.json",
        "latest": TOD_COPILOT_HANDOFF_ROOT / "TOD_COPILOT_HANDOFF.latest.json",
    }


def _handoff_status_label(session_state: dict[str, Any]) -> str:
    status = str(session_state.get("status") or "unknown").strip().lower()
    last_message = session_state.get("last_message") if isinstance(session_state.get("last_message"), dict) else {}
    last_from = str(last_message.get("from") or "").strip().upper()
    if last_from == "MIM":
        return "Replied"
    if status == "timed_out":
        return "Timed Out"
    if status == "closed":
        return "Closed"
    if session_state.get("open_reply"):
        return "Awaiting MIM"
    if status:
        return status.replace("_", " ").title()
    return "Unknown"


def _load_recent_copilot_handoffs(
    limit: int = 6,
    current_objective_id: str = "",
    current_request_id: str = "",
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not DIALOG_ROOT.exists():
        return items
    current_objective_token = _normalize_objective_token(current_objective_id)
    current_request = str(current_request_id or "").strip()
    for session_state_path in sorted(DIALOG_ROOT.glob("MIM_TOD_DIALOG.session-tod-ui-copilot-*.latest.json"), reverse=True):
        session_state = _load_json(session_state_path)
        if not session_state:
            continue
        session_id = str(session_state.get("session_id") or "").strip()
        if not session_id:
            continue
        handoff_paths = _build_copilot_handoff_paths(session_id)
        handoff_artifact = _load_json(handoff_paths["session"])
        handoff_payload = handoff_artifact.get("handoff") if isinstance(handoff_artifact.get("handoff"), dict) else {}
        issue = handoff_payload.get("issue") if isinstance(handoff_payload.get("issue"), dict) else {}
        ids = handoff_payload.get("ids") if isinstance(handoff_payload.get("ids"), dict) else {}
        last_message = session_state.get("last_message") if isinstance(session_state.get("last_message"), dict) else {}
        request_id = str(ids.get("request_id") or "").strip()
        objective_id = str(ids.get("objective_id") or "").strip()
        objective_token = _normalize_objective_token(objective_id)
        if current_objective_token and objective_token and objective_token != current_objective_token:
            continue
        if current_objective_token and not objective_token and current_request and request_id and request_id != current_request:
            continue
        items.append(
            {
                "session_id": session_id,
                "status": str(session_state.get("status") or "unknown").strip(),
                "status_label": _handoff_status_label(session_state),
                "updated_at": str(session_state.get("updated_at") or "").strip(),
                "updated_age": _format_age(session_state.get("updated_at")),
                "message_count": _safe_int(session_state.get("message_count"), 0),
                "session_path": str(session_state.get("session_path") or session_state_path).strip(),
                "dialog_index_path": str(handoff_artifact.get("dialog_index_path") or _dialog_session_paths(session_id)["index"]).strip(),
                "copilot_artifact_path": str(handoff_paths["session"]),
                "request_id": request_id,
                "task_id": str(ids.get("task_id") or "").strip(),
                "objective_id": objective_id,
                "issue_summary": _pick_first_text(issue.get("summary"), last_message.get("summary")),
                "bounded_repair_request": _pick_first_text(issue.get("bounded_repair_request")),
                "next_validation": _pick_first_text(issue.get("next_validation")),
                "last_message_from": str(last_message.get("from") or "").strip(),
                "last_message_type": str(last_message.get("message_type") or "").strip(),
            }
        )
        if len(items) >= limit:
            break
    return items


def _create_copilot_handoff(
    message: str,
    state: dict[str, Any],
    session_key: str,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    status = state.get("status") if isinstance(state.get("status"), dict) else {}
    quick_facts = state.get("quick_facts") if isinstance(state.get("quick_facts"), dict) else {}
    alignment = state.get("objective_alignment") if isinstance(state.get("objective_alignment"), dict) else {}
    evidence = state.get("bridge_canonical_evidence") if isinstance(state.get("bridge_canonical_evidence"), dict) else {}
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    listener = state.get("listener_decision") if isinstance(state.get("listener_decision"), dict) else {}
    publish = state.get("publish") if isinstance(state.get("publish"), dict) else {}
    training = state.get("training_status") if isinstance(state.get("training_status"), dict) else {}
    authority = state.get("authority_reset") if isinstance(state.get("authority_reset"), dict) else {}

    request_id = str(live_task.get("request_id") or "").strip()
    task_id = str(live_task.get("task_id") or "").strip()
    objective_id = str(live_task.get("objective_id") or live_task.get("normalized_objective_id") or "").strip()
    correlation_id = str(live_task.get("correlation_id") or "").strip()
    seed = "|".join(
        [
            request_id or "no-request",
            task_id or "no-task",
            objective_id or "no-objective",
            session_key,
            _compact_text(message, 240),
            _utc_now_iso(),
        ]
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8]
    session_id = _sanitize_session_key(f"tod-ui-copilot-{timestamp}-{digest}")
    paths = _dialog_session_paths(session_id)
    turn_id = _next_dialog_turn_id(paths["session"])
    issue_summary = _pick_first_text(status.get("headline"), status.get("summary")) or "TOD needs review."
    repair_request = _next_bounded_repair_request(state)
    validation = _next_validation_check(state)
    strongest_evidence = _strongest_evidence(state)

    handoff_payload = {
        "source": "tod-ui-copilot-handoff-v1",
        "request_kind": "tod_ui_copilot_handoff",
        "operator_request": _compact_text(message, 1200),
        "attachments": attachments or [],
        "issue": {
            "status_code": str(status.get("code") or "unknown").strip(),
            "status_label": str(status.get("label") or "unknown").strip(),
            "summary": issue_summary,
            "strongest_evidence": strongest_evidence,
            "current_repair_step": _current_repair_step(state),
            "bounded_repair_request": repair_request,
            "next_validation": validation,
        },
        "ids": {
            "request_id": request_id,
            "task_id": task_id,
            "objective_id": objective_id,
            "correlation_id": correlation_id,
        },
        "quick_facts": quick_facts,
        "objective_alignment": alignment,
        "bridge_canonical_evidence": evidence,
        "listener_decision": listener,
        "publish": publish,
        "training_status": {
            "state": training.get("state"),
            "state_label": training.get("state_label"),
            "summary": training.get("summary"),
            "current_step": training.get("current_step"),
            "latest_error": training.get("latest_error"),
            "latest_resolution": training.get("latest_resolution"),
            "percent_complete": training.get("percent_complete"),
        },
        "authority_reset": authority,
        "conversation": {
            "session_key": _sanitize_session_key(session_key),
            "surface": "tod-ui-public-console",
        },
        "requested_reply": {
            "actor": "MIM",
            "message_type": "handoff_response",
            "fields": ["summary", "repair_step", "validation", "missing_artifacts", "next_update"],
        },
    }

    handoff_paths = _build_copilot_handoff_paths(session_id)
    artifact = {
        "generated_at": _utc_now_iso(),
        "source": "tod-ui-copilot-handoff-v1",
        "session_id": session_id,
        "dialog_session_path": str(paths["session"]),
        "dialog_index_path": str(paths["index"]),
        "handoff": handoff_payload,
    }
    handoff_paths["session"].parent.mkdir(parents=True, exist_ok=True)
    handoff_paths["session"].write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    handoff_paths["latest"].write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    summary = f"TOD UI requests Copilot handoff for {request_id or task_id or 'current TOD issue'}."
    dialog_message = {
        "session_id": session_id,
        "turn_id": turn_id,
        "timestamp": _utc_now_iso(),
        "from": "TOD",
        "to": "MIM",
        "message_type": "handoff_request",
        "intent": "tod_ui_copilot_handoff",
        "correlation_id": correlation_id,
        "task_id": task_id,
        "summary": summary,
        "payload": {
            **handoff_payload,
            "artifact_path": str(handoff_paths["session"]),
        },
        "requires_reply": True,
        "schema_version": DIALOG_SCHEMA_VERSION,
    }
    _append_jsonl_record(paths["session"], dialog_message)
    _append_jsonl_record(paths["log"], dialog_message)

    session_state = {
        "session_id": session_id,
        "status": "open",
        "timed_out": False,
        "message_count": turn_id,
        "updated_at": dialog_message["timestamp"],
        "session_path": str(paths["session"]),
        "open_reply": {
            "turn_id": dialog_message["turn_id"],
            "from": dialog_message["from"],
            "to": dialog_message["to"],
            "message_type": dialog_message["message_type"],
            "summary": dialog_message["summary"],
            "timestamp": dialog_message["timestamp"],
        },
        "last_message": _session_preview(dialog_message),
        "awaiting_reply_to": "MIM",
        "reply_to": "",
    }
    paths["latest"].write_text(json.dumps(session_state, indent=2), encoding="utf-8")
    _upsert_dialog_session_index(session_state)

    return {
        "ok": True,
        "session_id": session_id,
        "turn_id": turn_id,
        "summary": summary,
        "artifact_path": str(handoff_paths["session"]),
        "latest_artifact_path": str(handoff_paths["latest"]),
        "dialog_session_path": str(paths["session"]),
        "dialog_session_latest_path": str(paths["latest"]),
        "dialog_index_path": str(paths["index"]),
        "reply_contract": "MIM should answer this session with handoff_response.",
        "request_id": request_id,
        "task_id": task_id,
        "objective_id": objective_id,
    }


def _pick_first_text(*values: Any) -> str:
    for value in values:
        text = _compact_text(value, 320)
        if text and text.lower() not in {"unknown", "none", "inactive", "no publish summary", "no listener decision summary"}:
            return text
    return ""


def _next_bounded_repair_request(state: dict[str, Any]) -> str:
    guidance = state.get("operator_guidance") if isinstance(state.get("operator_guidance"), list) else []
    for item in guidance:
        if isinstance(item, dict):
            text = _pick_first_text(item.get("recommended_action"), item.get("summary"))
            if text:
                return text
    listener = state.get("listener_decision") if isinstance(state.get("listener_decision"), dict) else {}
    training = state.get("training_status") if isinstance(state.get("training_status"), dict) else {}
    publish = state.get("publish") if isinstance(state.get("publish"), dict) else {}
    return _pick_first_text(
        listener.get("next_step_recommendation"),
        training.get("latest_resolution"),
        training.get("current_step"),
        publish.get("summary"),
        "Re-run the bounded bridge diagnosis and validate listener, publish, and canonical objective alignment before changing authority again.",
    )


def _current_repair_step(state: dict[str, Any]) -> str:
    training = state.get("training_status") if isinstance(state.get("training_status"), dict) else {}
    publish = state.get("publish") if isinstance(state.get("publish"), dict) else {}
    listener = state.get("listener_decision") if isinstance(state.get("listener_decision"), dict) else {}
    return _pick_first_text(
        training.get("latest_resolution"),
        training.get("phase_detail"),
        listener.get("next_step_recommendation"),
        publish.get("summary"),
        "No confirmed automated repair step is currently published on this surface.",
    )


def _strongest_evidence(state: dict[str, Any]) -> str:
    evidence = state.get("bridge_canonical_evidence") if isinstance(state.get("bridge_canonical_evidence"), dict) else {}
    listener = state.get("listener_decision") if isinstance(state.get("listener_decision"), dict) else {}
    publish = state.get("publish") if isinstance(state.get("publish"), dict) else {}
    authority = state.get("authority_reset") if isinstance(state.get("authority_reset"), dict) else {}
    training = state.get("training_status") if isinstance(state.get("training_status"), dict) else {}
    signals = evidence.get("failure_signals") if isinstance(evidence.get("failure_signals"), list) else []
    return _pick_first_text(
        "; ".join([_compact_text(item, 140) for item in signals if _compact_text(item, 140)]),
        listener.get("summary"),
        publish.get("summary"),
        authority.get("reason"),
        training.get("latest_error"),
        training.get("summary"),
        state.get("status", {}).get("summary") if isinstance(state.get("status"), dict) else "",
    ) or "No single dominant evidence item is currently published."


def _next_validation_check(state: dict[str, Any]) -> str:
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    listener = state.get("listener_decision") if isinstance(state.get("listener_decision"), dict) else {}
    alignment = state.get("objective_alignment") if isinstance(state.get("objective_alignment"), dict) else {}
    publish = state.get("publish") if isinstance(state.get("publish"), dict) else {}
    request_id = str(live_task.get("request_id") or "").strip() or "unknown request"
    task_id = str(live_task.get("task_id") or "").strip() or "unknown task"
    objective_id = str(live_task.get("objective_id") or live_task.get("normalized_objective_id") or "").strip() or "unknown objective"
    return _pick_first_text(
        listener.get("next_step_recommendation"),
        f"Re-check alignment={str(alignment.get('status') or 'unknown').strip()} and publish={str(publish.get('status') or 'unknown').strip()} for request_id={request_id}, task_id={task_id}, objective_id={objective_id}.",
    )


def _classify_prompt(message: str) -> str:
    text = message.lower()
    normalized = re.sub(r"\s+", " ", text)
    if "copilot-style" in text or "package the current issue" in text:
        return "handoff"
    if any(
        re.search(pattern, normalized)
        for pattern in (
            r"\bstart(?:\s+(?:your|the|a|next))*\s+(?:bounded\s+)?(?:6h\s+|six[- ]hour\s+)?training(?:\s+(?:cycle|run|runbook|loop))?\b",
            r"\blaunch(?:\s+(?:the|a|your|next))*\s+(?:bounded\s+)?(?:6h\s+|six[- ]hour\s+)?training(?:\s+(?:cycle|run|runbook|loop))?\b",
            r"\bbegin(?:\s+(?:the|a|your|next))*\s+(?:bounded\s+)?(?:6h\s+|six[- ]hour\s+)?training(?:\s+(?:cycle|run|runbook|loop))?\b",
            r"\brun(?:\s+(?:the|a|your|next))*\s+(?:bounded\s+)?(?:6h\s+|six[- ]hour\s+)?training(?:\s+(?:cycle|run|runbook|loop))?\b",
        )
    ):
        return "training"
    if any(token in text for token in ("objective_id", "objective", "active task", "implement", "build", "execution loop contract")):
        return "task"
    if re.search(r"\bhandoff\b", normalized):
        return "handoff"
    if "resolve" in text and ("drift" in text or "mismatch" in text or "out of sync" in text or "out-of-sync" in text):
        return "drift"
    if "out of sync" in text or "out-of-sync" in text or "mismatch" in text:
        return "sync"
    if "attention" in text or "needs review" in text or "blocking" in text or "blocker" in text:
        return "blockers"
    if any(token in text for token in ("can you fix", "fix this", "please fix", "troubleshoot", "debug this", "begin to troubleshoot")):
        return "task"
    return "status"


def _compose_task_worklog(
    task_focus: str,
    strongest_evidence: str,
    current_repair: str,
    next_repair: str,
    next_validation: str,
    request_id: str,
    task_id: str,
    objective_id: str,
) -> str:
    return "\n".join(
        [
            f"Accepted. TOD opened a live troubleshooting lane for {task_focus}.",
            f"Thinking: grounding on the strongest published signal first. {strongest_evidence}",
            f"Working now: {current_repair}",
            f"Applying next: {next_repair}",
            f"Testing next: {next_validation}",
            f"Tracking: request_id={request_id}; task_id={task_id}; objective_id={objective_id}",
        ]
    )


_GENERATED_PROGRESS_PREFIXES = (
    "Accepted. TOD opened a live troubleshooting lane for",
    "Thinking:",
    "TOD is ",
    "Action:",
    "Objective now:",
    "Task now:",
    "Current slice:",
    "Working now:",
    "Applying next:",
    "Testing next:",
    "Tracking:",
    "Dispatch now:",
    "Waiting on:",
    "Execution confirmation was published",
    "Executable task request published",
    "Live execution feed:",
    "Progress detail:",
    "Status now:",
    "Execution evidence:",
    "Validation checks:",
    "Validation summary:",
    "Files changed:",
    "Matched surfaces:",
    "Updated:",
)


def _is_generated_progress_message(message: Any) -> bool:
    if not isinstance(message, dict):
        return False
    content = str(message.get("content") or "").strip()
    if not content:
        return False
    if any(content.startswith(prefix) for prefix in _GENERATED_PROGRESS_PREFIXES):
        return True
    return re.match(r"^Phase(?:\s+\d+)?\s+progress:", content, re.IGNORECASE) is not None


def _trim_trailing_generated_progress(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = list(messages)
    while trimmed and _is_generated_progress_message(trimmed[-1]):
        trimmed.pop()
    return trimmed


def _summarize_execution_slice(summary: Any) -> str:
    text = _compact_text(summary, 220)
    if not text:
        return ""
    return _compact_text(text, 140)


def _execution_live_status(activity_state: str, execution_state: str) -> str:
    combined = " ".join(part for part in (activity_state, execution_state) if part).strip().lower()
    if not combined:
        return "idle"
    if "idle" in combined and "wait" not in combined:
        return "idle"
    if any(token in combined for token in ("wait", "blocked", "stall", "stalled", "dependency", "rejected")):
        return "blocked"
    if any(token in combined for token in ("complete", "completed", "published", "passed", "done", "success")):
        return "complete"
    return "executing"


def _is_circular_local_executor_wait(wait_target: Any, wait_reason: Any, next_step: Any) -> bool:
    target_text = str(wait_target or "").strip().lower()
    reason_text = str(wait_reason or "").strip().lower()
    next_text = str(next_step or "").strip().lower()
    return bool(
        ("tod local executor" in target_text or "tod_local_executor" in target_text or "own next bounded local implementation step" in reason_text)
        and any(phrase in f"{reason_text} {next_text}" for phrase in ("bounded execution-loop slice", "bounded local implementation step", "implement the next bounded"))
    )


def _build_execution_event(
    *,
    action: str,
    target: str,
    status: str,
    created_at: str,
    reason: str = "",
    result: str = "",
    next_action: str = "",
    waiting_on: str = "",
    retry_rule: str = "",
) -> dict[str, str]:
    lines = [
        f"Action: {action}",
        f"Target: {target or 'unknown'}",
        f"Status: {status or 'unknown'}",
    ]
    if waiting_on:
        lines.append(f"Waiting on: {waiting_on}")
    if reason:
        lines.append(f"Reason: {reason}")
    if result:
        lines.append(f"Result: {result}")
    if next_action:
        lines.append(f"Next: {next_action}")
    if retry_rule:
        lines.append(f"Retry: {retry_rule}")
    return {
        "role": "system",
        "content": "\n".join(lines),
        "created_at": created_at,
    }


def _describe_execution_validation_target(execution: dict[str, Any]) -> str:
    next_validation = _compact_text(execution.get("next_validation"), 220)
    if not next_validation:
        return ""
    normalized = next_validation.strip().lower()
    if normalized in {"execute_now", "run_now"}:
        next_step = _compact_text(execution.get("next_step"), 180)
        if next_step:
            return f"Focused check after: {next_step}"
        return "Run the published focused check now"
    return _compact_text(next_validation, 140)


def _build_task_progress_messages(message: str, state: dict[str, Any], execution_ack: dict[str, Any] | None = None, dispatch_record: dict[str, Any] | None = None) -> list[dict[str, str]]:
    status = state.get("status") if isinstance(state.get("status"), dict) else {}
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    strongest_evidence = _strongest_evidence(state)
    current_repair = str((execution_ack or {}).get("current_action") or "").strip() or _current_repair_step(state)
    next_repair = str((execution_ack or {}).get("next_step") or "").strip() or _next_bounded_repair_request(state)
    next_validation = str((execution_ack or {}).get("next_validation") or "").strip() or _next_validation_check(state)
    request_id = str((execution_ack or {}).get("request_id") or live_task.get("request_id") or "").strip() or "Unknown"
    task_id = str((execution_ack or {}).get("task_id") or live_task.get("task_id") or "").strip() or "Unknown"
    objective_id = str((execution_ack or {}).get("objective_id") or live_task.get("objective_id") or live_task.get("normalized_objective_id") or "").strip() or "Unknown"
    task_focus = _pick_first_text(
        _summarize_requested_task(message, 180),
        str((execution_ack or {}).get("summary") or "").strip(),
        str(live_task.get("issue_summary") or "").strip(),
        str(live_task.get("title") or "").strip(),
        str(status.get("summary") or "").strip(),
        "the requested repair",
    )
    created_at = _utc_now_iso()
    progress_messages = [
        {
            "role": "tod",
            "content": f"Accepted. TOD opened a live troubleshooting lane for {task_focus}.",
            "created_at": created_at,
        },
        {
            "role": "system",
            "content": f"Thinking: grounding on the strongest published signal first. {strongest_evidence}",
            "created_at": created_at,
        },
        {
            "role": "system",
            "content": f"Working now: {current_repair}",
            "created_at": created_at,
        },
        {
            "role": "system",
            "content": f"Applying next: {next_repair}",
            "created_at": created_at,
        },
        {
            "role": "system",
            "content": f"Testing next: {next_validation}",
            "created_at": created_at,
        },
        {
            "role": "system",
            "content": f"Tracking: request_id={request_id}; task_id={task_id}; objective_id={objective_id}",
            "created_at": created_at,
        },
    ]
    if dispatch_record and dispatch_record.get("ok"):
        progress_messages.append(
            {
                "role": "tod",
                "content": "Executable task request published to the shared TOD bridge surface. The local TOD listener can now create, package, and run the bounded task instead of stopping at inspection.",
                "created_at": created_at,
            }
        )
        request_path = str(dispatch_record.get("request_path") or "").strip()
        if request_path:
            progress_messages.append(
                {
                    "role": "system",
                    "content": f"Dispatch now: wrote execute-chat-task request to {request_path}.",
                    "created_at": created_at,
                }
            )
        progress_messages.append(
            {
                "role": "system",
                "content": f"Waiting on: TOD local listener to consume request {task_id} and transition the bounded task into package and run-task execution.",
                "created_at": created_at,
            }
        )
    elif execution_ack and execution_ack.get("ok"):
        progress_messages.append(
            {
                "role": "tod",
                "content": "Execution confirmation was published to the shared TOD truth surface, but no executable task request was emitted for the local listener.",
                "created_at": created_at,
            }
        )
    return progress_messages


def _build_execution_feed_messages(state: dict[str, Any]) -> list[dict[str, str]]:
    execution = state.get("execution") if isinstance(state.get("execution"), dict) else {}
    if not execution or not execution.get("available"):
        return []
    created_at = str(execution.get("updated_at") or state.get("generated_at") or _utc_now_iso()).strip() or _utc_now_iso()
    title = (_pick_first_text(execution.get("title"), execution.get("task_focus"), execution.get("task_id")) or "the active TOD execution").rstrip(". ")
    activity_label = _pick_first_text(execution.get("activity_label"), execution.get("execution_state"), execution.get("status")) or "Working"
    activity_summary = _compact_text(execution.get("activity_summary") or execution.get("summary"), 220)
    objective_id = _compact_text(execution.get("objective_id"), 140)
    task_id = _compact_text(execution.get("task_id"), 140)
    summary = _summarize_execution_slice(execution.get("summary"))
    phase_progress = execution.get("phase_progress") if isinstance(execution.get("phase_progress"), dict) else {}
    stall_signal = execution.get("stall_signal") if isinstance(execution.get("stall_signal"), dict) else {}
    activity_state = _compact_text(execution.get("activity_state"), 80) or activity_label.lower()
    execution_state = _compact_text(execution.get("execution_state"), 120) or _compact_text(execution.get("status"), 120) or "unknown"
    updated_age = _pick_first_text(execution.get("updated_age")) or "unknown"
    live_status = _execution_live_status(activity_state, execution_state)
    files_changed = execution.get("files_changed") if isinstance(execution.get("files_changed"), list) else []
    matched_files = execution.get("matched_files") if isinstance(execution.get("matched_files"), list) else []
    file_focus = _pick_first_text(
        next((_compact_text(item, 120) for item in files_changed if _compact_text(item, 120)), ""),
        next((_compact_text(item, 120) for item in matched_files if _compact_text(item, 120)), ""),
    )
    current_action = _compact_text(execution.get("current_action"), 220)
    wait_reason = _compact_text(execution.get("wait_reason"), 220)
    wait_target = _pick_first_text(execution.get("wait_target_label"), execution.get("wait_target"))
    binding_status = _compact_text(execution.get("executor_binding_status"), 40)
    binding_target = _compact_text(execution.get("executor_binding_target"), 220)
    binding_command = _compact_text(execution.get("executor_binding_command"), 120)
    circular_block_converted = bool(execution.get("circular_block_converted")) or _is_circular_local_executor_wait(wait_target, wait_reason, execution.get("next_step"))
    wait_owner = ""
    wait_context = " ".join(str(item or "") for item in (wait_target, wait_reason, execution_state, current_action, execution.get("next_step"))).lower()
    if any(term in wait_context for term in ("codex", "copilot")):
        wait_owner = "Codex"
    elif any(term in wait_context for term in ("operator", "console", "user", "dave")):
        wait_owner = "operator"
    elif "mim" in wait_context:
        wait_owner = "MIM"
    elif any(term in wait_context for term in ("validator", "validation", "test")):
        wait_owner = "validation runner"
    elif any(term in wait_context for term in ("listener", "executor", "run-task", "local execution", "local listener")):
        wait_owner = "TOD local executor"
    elif any(term in wait_context for term in ("lock", "lease")):
        wait_owner = "execution lock"
    elif activity_state == "waiting":
        wait_owner = "TOD"
    next_step = _compact_text(execution.get("next_step"), 220)
    if not next_step and live_status == "blocked":
        next_step = "Implement the next bounded execution-loop slice in the inspected surfaces and rerun the focused validation path."
    next_validation = _describe_execution_validation_target(execution)
    command_output = _compact_text(execution.get("command_output"), 220)
    validation_summary = _compact_text(execution.get("validation_summary"), 220)
    checks = execution.get("validation_checks") if isinstance(execution.get("validation_checks"), list) else []
    check_summary = ", ".join(
        f"{_compact_text(item.get('name'), 80)}={'passed' if bool(item.get('passed')) else 'failed'}"
        for item in checks
        if isinstance(item, dict)
    )
    wait_target_text = wait_target or wait_owner or "next bounded step"
    retry_rule = ""
    if live_status == "blocked":
        retry_rule = "Retry on the next execution heartbeat and escalate if the stall threshold is crossed without newer execution evidence."
    elif live_status == "idle":
        retry_rule = "Check the next eligible source on the next activity sweep."

    if circular_block_converted and not binding_target:
        binding_status = binding_status or "missing"
        binding_target = "scripts/engines/LocalExecutionEngine.ps1::Invoke-LocalExecutionEngine"
        binding_command = binding_command or "execute-chat-task"
        wait_target_text = binding_target
        wait_reason = (
            wait_reason
            or "Circular self-wait removed. The next bounded implementation slice has not been dispatched through execute-chat-task into scripts/engines/LocalExecutionEngine.ps1::Invoke-LocalExecutionEngine."
        )

    if circular_block_converted and binding_status == "missing":
        live_line = f"TOD is blocked: missing local executor binding {binding_target}."
    elif live_status == "idle":
        live_line = "TOD is idle: no eligible work selected."
    elif live_status == "blocked":
        live_line = f"TOD is blocked: waiting on {wait_target_text}."
    elif live_status == "complete":
        live_line = f"TOD is complete: {title}."
    else:
        live_line = f"TOD is executing: {file_focus or current_action or title}."
    if updated_age:
        live_line = f"{live_line}\nLast heartbeat: {updated_age}."

    messages: list[dict[str, str]] = [
        {
            "role": "tod",
            "content": live_line,
            "created_at": created_at,
        }
    ]

    selected_target = title if title and title != "the active TOD execution" else task_id or "current TOD task"
    if objective_id:
        selected_target = f"{objective_id} -> {selected_target}"
    messages.append(
        _build_execution_event(
            action="selected_task",
            target=selected_target,
            status=live_status,
            created_at=created_at,
            reason=summary or activity_summary or "TOD selected the current execution slice.",
            next_action=next_step,
        )
    )
    if task_id:
        messages.append(
            _build_execution_event(
                action="claimed_task",
                target=task_id,
                status=live_status,
                created_at=created_at,
                reason="TOD has an active execution claim for this bounded task.",
                result=_pick_first_text(objective_id, title),
            )
        )

    if circular_block_converted:
        messages.append(
            _build_execution_event(
                action="task_created_from_circular_block",
                target=next_step or selected_target,
                status="created",
                created_at=created_at,
                reason="TOD converted a circular self-block into a concrete local implementation task.",
                result=summary or activity_summary,
            )
        )
        messages.append(
            _build_execution_event(
                action="task_claimed",
                target=task_id or selected_target,
                status="claimed",
                created_at=created_at,
                reason="TOD claimed the converted bounded implementation task locally.",
                result=_pick_first_text(objective_id, title),
            )
        )
        messages.append(
            _build_execution_event(
                action="executor_binding_checked",
                target=binding_target or wait_target_text,
                status=binding_status or "checked",
                created_at=created_at,
                reason="TOD checked the local executor binding needed to start the next bounded implementation slice.",
                result=f"Command: {binding_command}" if binding_command else "No executor command published.",
            )
        )
        if binding_status == "missing":
            messages.append(
                _build_execution_event(
                    action="blocked_missing_local_executor_binding",
                    target=binding_target,
                    status="blocked",
                    created_at=created_at,
                    reason=wait_reason,
                    result=activity_summary or summary,
                    next_action=next_step,
                    retry_rule="Do not wait for another heartbeat. Dispatch the slice through execute-chat-task or publish the missing binding repair.",
                )
            )

    inspection_context = " ".join(part for part in (current_action, summary, str(execution.get("phase") or "")) if part).lower()
    if file_focus and (not files_changed or any(token in inspection_context for token in ("inspect", "inspection", "review", "scan", "workspace_inspection"))):
        messages.append(
            _build_execution_event(
                action="inspecting_file",
                target=file_focus,
                status="observing" if live_status == "executing" else live_status,
                created_at=created_at,
                reason=current_action or summary or "TOD is reading the active execution surface.",
                result=_compact_text(", ".join(_compact_text(item, 120) for item in matched_files if _compact_text(item, 120)), 220),
            )
        )
    if files_changed or any(token in inspection_context for token in ("patch", "edit", "implement", "write", "apply")):
        messages.append(
            _build_execution_event(
                action="editing_file",
                target=file_focus or title,
                status="executing" if live_status == "executing" else live_status,
                created_at=created_at,
                reason=current_action or "TOD is applying the current implementation slice.",
                result=_compact_text(", ".join(_compact_text(item, 120) for item in files_changed if _compact_text(item, 120)), 220),
            )
        )
    if command_output or any(token in inspection_context for token in ("command", "run", "execute", "restart")):
        messages.append(
            _build_execution_event(
                action="running_command",
                target=_pick_first_text(file_focus, title, task_id) or "current command lane",
                status="executing" if live_status == "executing" else live_status,
                created_at=created_at,
                reason=current_action or "TOD is running the current command slice.",
                result=command_output,
            )
        )
    if next_validation or checks:
        messages.append(
            _build_execution_event(
                action="running_test",
                target=next_validation or check_summary or "focused validation",
                status="running" if live_status == "executing" else live_status,
                created_at=created_at,
                reason="TOD is validating the current bounded slice.",
                result=check_summary,
            )
        )
    if validation_summary or checks:
        passed_checks = any(isinstance(item, dict) and bool(item.get("passed")) for item in checks)
        failed_checks = any(isinstance(item, dict) and not bool(item.get("passed")) for item in checks)
        messages.append(
            _build_execution_event(
                action="validation_failed" if failed_checks else "validation_passed",
                target=next_validation or title,
                status="failed" if failed_checks else ("passed" if passed_checks or validation_summary else live_status),
                created_at=created_at,
                reason=validation_summary or "TOD published the latest validation outcome.",
                result=check_summary,
                next_action=next_step if failed_checks else "",
            )
        )
    if live_status == "blocked" and not (circular_block_converted and binding_status == "missing"):
        messages.append(
            _build_execution_event(
                action="blocked_with_reason",
                target=wait_target_text,
                status="blocked",
                created_at=created_at,
                waiting_on=wait_target_text,
                reason=wait_reason or _compact_text(stall_signal.get("summary"), 220) or "TOD published a wait state without a reason.",
                result=_compact_text(stall_signal.get("summary"), 220),
                next_action=next_step,
                retry_rule=retry_rule,
            )
        )
    elif live_status == "idle":
        messages.append(
            _build_execution_event(
                action="idle_no_eligible_work",
                target=selected_target,
                status="idle",
                created_at=created_at,
                reason="No eligible work is currently selected for execution.",
                result=_pick_first_text(validation_summary, activity_summary, summary, "No active execution evidence is published."),
                next_action=next_step or "Check the next eligible source.",
                retry_rule=retry_rule,
            )
        )
    if live_status == "complete" or execution_state.lower() in {"completed", "complete", "published"}:
        messages.append(
            _build_execution_event(
                action="result_published",
                target=selected_target,
                status="completed",
                created_at=created_at,
                reason=activity_summary or summary or "TOD published the final result for the active slice.",
                result=validation_summary or command_output or check_summary,
                next_action=next_step or "Check the next eligible objective.",
            )
        )
    return messages


def _messages_include_execution_feed(messages: list[dict[str, Any]], execution_updated_at: str) -> bool:
    if not execution_updated_at:
        return False
    for item in messages:
        if not isinstance(item, dict):
            continue
        if str(item.get("created_at") or "").strip() != execution_updated_at:
            continue
        content = str(item.get("content") or "").strip()
        if content.startswith("Live execution feed:") or content.startswith("TOD is ") or content.startswith("Action:"):
            return True
    return False


def _compose_tod_reply(message: str, state: dict[str, Any]) -> str:
    status = state.get("status") if isinstance(state.get("status"), dict) else {}
    alignment = state.get("objective_alignment") if isinstance(state.get("objective_alignment"), dict) else {}
    live_task = state.get("live_task_request") if isinstance(state.get("live_task_request"), dict) else {}
    training = state.get("training_status") if isinstance(state.get("training_status"), dict) else {}
    canonical_objective = _pick_first_text(
        state.get("quick_facts", {}).get("canonical_objective") if isinstance(state.get("quick_facts"), dict) else "",
        alignment.get("mim_objective_active"),
    ) or "Unknown"
    live_objective = _pick_first_text(
        state.get("quick_facts", {}).get("live_request_objective") if isinstance(state.get("quick_facts"), dict) else "",
        live_task.get("objective_id"),
        alignment.get("tod_current_objective"),
    ) or "Unknown"
    issue_summary = _pick_first_text(status.get("headline"), status.get("summary")) or "TOD has no published issue summary."
    strongest_evidence = _strongest_evidence(state)
    current_repair = _current_repair_step(state)
    next_repair = _next_bounded_repair_request(state)
    next_validation = _next_validation_check(state)
    request_id = str(live_task.get("request_id") or "").strip() or "Unknown"
    task_id = str(live_task.get("task_id") or "").strip() or "Unknown"
    objective_id = str(live_task.get("objective_id") or live_task.get("normalized_objective_id") or "").strip() or "Unknown"

    intent = _classify_prompt(message)
    if intent == "training":
        training_state = _pick_first_text(training.get("state_label"), training.get("state")) or "Unknown"
        current_gate = _pick_first_text(training.get("current_step"), training.get("phase_detail"), next_repair)
        first_blocker = _pick_first_text(training.get("latest_error"), status.get("summary"), strongest_evidence)
        return "\n".join(
            [
                "Training execution cannot be started from this public /tod surface.",
                f"Runbook status: {training_state}",
                f"Current gate: {current_gate}",
                f"First blocker: {first_blocker}",
                f"Next bounded action: {next_repair}",
                f"Next validation: {next_validation}",
            ]
        )
    if intent == "drift":
        return "\n".join(
            [
                f"Drift summary: canonical objective={canonical_objective}; live objective={live_objective}; status={str(status.get('label') or 'Unknown').strip() or 'Unknown'}.",
                f"Mismatch detail: {_pick_first_text(alignment.get('summary'), issue_summary)}",
                f"Strongest evidence: {strongest_evidence}",
                f"Current repair step: {current_repair}",
                f"Next validation: {next_validation}",
            ]
        )
    if intent == "handoff":
        return "\n".join(
            [
                "Copilot handoff summary:",
                f"Issue: {issue_summary}",
                f"Evidence: {strongest_evidence}",
                f"Bounded repair request: {next_repair}",
                f"Validation after repair: {next_validation}",
                f"Active IDs: request_id={request_id}; task_id={task_id}; objective_id={objective_id}",
            ]
        )
    if intent == "task":
        task_focus = _pick_first_text(
            _summarize_requested_task(message, 180),
            str(live_task.get("issue_summary") or "").strip(),
            str(live_task.get("title") or "").strip(),
            str(status.get("summary") or "").strip(),
            "the requested repair",
        )
        return _compose_task_worklog(
            task_focus=task_focus,
            strongest_evidence=strongest_evidence,
            current_repair=current_repair,
            next_repair=next_repair,
            next_validation=next_validation,
            request_id=request_id,
            task_id=task_id,
            objective_id=objective_id,
        )
    if intent == "sync":
        return "\n".join(
            [
                f"Current sync gap: canonical objective={canonical_objective}; live objective={live_objective}; listener state={str(state.get('quick_facts', {}).get('listener_state') or 'unknown').strip()}.",
                f"Issue summary: {issue_summary}",
                f"Strongest evidence: {strongest_evidence}",
                f"Next bounded repair: {next_repair}",
                f"Next validation: {next_validation}",
            ]
        )
    if intent == "blockers":
        return "\n".join(
            [
                f"Current blocker posture: {issue_summary}",
                f"Strongest evidence: {strongest_evidence}",
                f"Current repair step: {current_repair}",
                f"Next bounded repair: {next_repair}",
                f"Next validation: {next_validation}",
            ]
        )
    return "\n".join(
        [
            f"TOD status: {issue_summary}",
            f"Strongest evidence: {strongest_evidence}",
            f"Next bounded repair: {next_repair}",
            f"Next validation: {next_validation}",
        ]
    )


def _compose_operator_reply(message: str, state: dict[str, Any], surface_label: str = "/chat") -> str:
    if _classify_prompt(message) == "training":
        return _format_training_start_reply(_start_training_runbook(state))

    base_reply = _compose_tod_reply(message, state)
    if any(token in message.lower() for token in ("execute", "run", "start", "launch")):
        return "\n".join(
            [
                base_reply,
                f"Operator execution is enabled on {surface_label} for bounded actions.",
                "Direct actions available here: Start 6h Training and Send To Codex.",
            ]
        )
    return base_reply


def _build_chat_payload(session_key: str, messages: list[dict[str, Any]], state: dict[str, Any], surface: str = "tod") -> dict[str, Any]:
    status = state.get("status") if isinstance(state.get("status"), dict) else {}
    quick_facts = state.get("quick_facts") if isinstance(state.get("quick_facts"), dict) else {}
    normalized_surface = "chat" if str(surface or "tod").strip().lower() == "chat" else "tod"
    direct_chat_surface = normalized_surface == "chat"
    execution_enabled = True
    training_launcher = _resolve_training_request()
    visitor_name = "Operator" if direct_chat_surface else _resolve_public_visitor_name()
    session_payload = _load_chat_session_payload(session_key, state)
    pending_progress = list(session_payload.get("pending_progress") or [])
    pending_count = len(pending_progress)
    execution = state.get("execution") if isinstance(state.get("execution"), dict) else {}
    execution_feed_messages = _build_execution_feed_messages(state) if not direct_chat_surface else []
    execution_updated_at = str(execution.get("updated_at") or "").strip()
    display_messages = list(messages)
    if not display_messages:
        display_messages = execution_feed_messages
    elif execution_feed_messages and pending_count == 0:
        trimmed_messages = _trim_trailing_generated_progress(display_messages)
        execution_age_seconds = _age_seconds(execution_updated_at)
        last_message_at = str(trimmed_messages[-1].get("created_at") or "").strip() if trimmed_messages else ""
        last_message_age_seconds = _age_seconds(last_message_at)
        last_trimmed_role = str(trimmed_messages[-1].get("role") or "").strip().lower() if trimmed_messages else ""
        should_append_execution_feed = False
        replaced_prior_progress = len(trimmed_messages) != len(display_messages)
        if replaced_prior_progress:
            should_append_execution_feed = True
        if execution_age_seconds is not None and last_message_age_seconds is not None:
            should_append_execution_feed = execution_age_seconds <= last_message_age_seconds
        elif execution_updated_at and not replaced_prior_progress:
            should_append_execution_feed = True
        if last_trimmed_role in {"tod", "assistant", "copilot"}:
            should_append_execution_feed = False
        if should_append_execution_feed:
            display_messages = [*trimmed_messages, *execution_feed_messages]
    last_message = display_messages[-1] if display_messages and isinstance(display_messages[-1], dict) else {}
    last_activity_at = str(last_message.get("created_at") or state.get("generated_at") or _utc_now_iso()).strip() or _utc_now_iso()
    last_activity_age_seconds = _age_seconds(last_activity_at)
    last_role = str(last_message.get("role") or "").strip().lower()
    activity_state = "idle"
    activity_label = "Idle"
    activity_summary = "No queued TOD activity is pending in this session."
    activity_pulse = False
    if pending_count > 0:
        if last_activity_age_seconds is not None and last_activity_age_seconds > 45:
            activity_state = "stalled"
            activity_label = "Stalled"
            activity_summary = f"TOD still has {pending_count} queued update(s), but the last activity was {_format_age(last_activity_at)}."
            activity_pulse = True
        else:
            activity_state = "working"
            activity_label = "Working"
            activity_summary = f"TOD is progressing and has {pending_count} queued update(s) left to publish into this thread."
            activity_pulse = True
    elif last_role == "system":
        if last_activity_age_seconds is not None and last_activity_age_seconds > 300:
            activity_state = "stalled"
            activity_label = "Stalled"
            activity_summary = f"TOD was waiting on the next validation or result, but this session has not updated since {_format_age(last_activity_at)}."
            activity_pulse = True
        else:
            activity_state = "waiting"
            activity_label = "Waiting"
            activity_summary = "TOD published the latest working step and is waiting on the next validation or result."
    elif last_role in {"tod", "copilot", "assistant"}:
        activity_state = "complete"
        activity_label = "Replied"
        activity_summary = "TOD has posted the latest reply for this session."
    if not messages and execution_feed_messages and isinstance(execution, dict) and execution.get("available"):
        activity_state = str(execution.get("activity_state") or activity_state).strip() or activity_state
        activity_label = str(execution.get("activity_label") or activity_label).strip() or activity_label
        activity_summary = str(execution.get("activity_summary") or execution.get("wait_reason") or activity_summary).strip() or activity_summary
    return {
        "session": {
            "session_key": _sanitize_session_key(session_key),
            "mode": normalized_surface,
            "message_count": len(display_messages),
            "updated_at": display_messages[-1].get("created_at") if display_messages else state.get("generated_at", _utc_now_iso()),
            "activity": {
                "state": activity_state,
                "label": activity_label,
                "summary": activity_summary,
                "pulse": activity_pulse,
                "pending_progress_count": pending_count,
                "last_activity_at": last_activity_at,
                "last_activity_age_seconds": last_activity_age_seconds,
            },
        },
        "messages": display_messages,
        "state_marker": _chat_state_marker(state),
        "status": status,
        "quick_facts": quick_facts,
        "visitor": {
            "name": visitor_name,
            "memory_summary": _pick_first_text(
                "TOD execution is enabled on this surface.",
                status.get("summary"),
                _next_bounded_repair_request(state),
                "TOD console chat is ready.",
            ),
        },
        "guardrails": {
            "commands_blocked": not execution_enabled,
            "live_execution_blocked": not execution_enabled,
            "execution_enabled": execution_enabled,
        },
        "capabilities": {
            "training_start": training_launcher,
            "codex_handoff": True,
            "image_upload": normalized_surface == "tod",
        },
        "links": [
            {"label": "Open Direct Chat", "href": "/chat"},
            {"label": "Open TOD Console", "href": "/tod"},
            {"label": "Open MIM Codex Chat", "href": "/mim"},
            {"label": "Logout", "href": "/mim/logout"},
        ],
        "actions": {
            "message_url": "/chat/ui/message" if direct_chat_surface else "/tod/ui/chat/message",
            "handoff_url": "/chat/ui/handoff" if direct_chat_surface else "/tod/ui/chat/handoff",
            "upload_url": "" if direct_chat_surface else "/tod/ui/chat/upload-image",
            "training_url": "/chat/ui/action/training",
        },
    }


def _build_tod_console_state() -> dict[str, Any]:
    integration_payload, integration_path = _first_existing_payload(
        SHARED_RUNTIME_ROOT / "TOD_INTEGRATION_STATUS.latest.json",
        SHARED_RUNTIME_ROOT / "TOD_integration_status.latest.json",
        SHARED_STATE_ROOT / "integration_status.json",
    )
    training_payload, training_path = _first_existing_payload(
        SHARED_RUNTIME_ROOT / "TOD_TRAINING_STATUS.latest.json",
        SHARED_RUNTIME_ROOT / "TOD_training_status.latest.json",
        SHARED_STATE_ROOT / "tod_training_status.latest.json",
        SHARED_STATE_ROOT / "TOD_TRAINING_STATUS.latest.json",
    )
    autonomy_payload, autonomy_path = _first_existing_payload(
        SHARED_RUNTIME_ROOT / "TOD_AUTONOMY_STATUS.latest.json",
        SHARED_RUNTIME_ROOT / "TOD_autonomy_status.latest.json",
        SHARED_RUNTIME_ROOT / "tod_autonomy_status.latest.json",
        SHARED_STATE_ROOT / "tod_autonomy_status.latest.json",
        SHARED_STATE_ROOT / "TOD_AUTONOMY_STATUS.latest.json",
    )
    runtime_task_request_payload, runtime_task_request_path = _first_existing_payload(
        SHARED_RUNTIME_ROOT / "MIM_TOD_TASK_REQUEST.latest.json",
    )
    if not training_payload and isinstance(integration_payload.get("training_status"), dict):
        training_payload = integration_payload.get("training_status") or {}
        training_path = integration_path
    if not autonomy_payload and isinstance(integration_payload.get("autonomy_status"), dict):
        autonomy_payload = integration_payload.get("autonomy_status") or {}
        autonomy_path = integration_path
    decision_payload = _load_json(SHARED_RUNTIME_ROOT / "TOD_MIM_EXECUTION_DECISION.latest.json")
    active_objective_payload, active_objective_path = _first_existing_payload(
        SHARED_RUNTIME_ROOT / "TOD_ACTIVE_OBJECTIVE.latest.json",
    )
    active_task_payload, active_task_path = _first_existing_payload(
        SHARED_RUNTIME_ROOT / "TOD_ACTIVE_TASK.latest.json",
    )
    activity_payload, activity_path = _first_existing_payload(
        SHARED_RUNTIME_ROOT / "TOD_ACTIVITY_STREAM.latest.json",
    )
    validation_payload, validation_path = _first_existing_payload(
        SHARED_RUNTIME_ROOT / "TOD_VALIDATION_RESULT.latest.json",
    )
    execution_result_payload, execution_result_path = _first_existing_payload(
        SHARED_RUNTIME_ROOT / "TOD_EXECUTION_RESULT.latest.json",
    )
    truth_payload, truth_path = _first_existing_payload(
        SHARED_RUNTIME_ROOT / "TOD_EXECUTION_TRUTH.latest.json",
        SHARED_RUNTIME_ROOT / "TOD_execution_truth.latest.json",
    )
    probe_payload = _load_json(SHARED_RUNTIME_ROOT / "TOD_CONSOLE_PROBE.latest.json")
    recovery_payload, recovery_path = _load_remote_recovery_payload()
    shared_truth_payload, shared_truth_path = _load_shared_truth_payload()

    alignment = (
        integration_payload.get("objective_alignment")
        if isinstance(integration_payload.get("objective_alignment"), dict)
        else {}
    )
    evidence = (
        integration_payload.get("bridge_canonical_evidence")
        if isinstance(integration_payload.get("bridge_canonical_evidence"), dict)
        else {}
    )
    publish = (
        integration_payload.get("tod_status_publish")
        if isinstance(integration_payload.get("tod_status_publish"), dict)
        else {}
    )
    integration_live_task_request = (
        integration_payload.get("live_task_request")
        if isinstance(integration_payload.get("live_task_request"), dict)
        else {}
    )
    live_task_request_source = integration_live_task_request
    if runtime_task_request_payload:
        integration_live_dt = _parse_timestamp(integration_live_task_request.get("generated_at"))
        runtime_live_dt = _parse_timestamp(runtime_task_request_payload.get("generated_at"))
        if not integration_live_task_request or runtime_live_dt is None or integration_live_dt is None or runtime_live_dt >= integration_live_dt:
            live_task_request_source = {**integration_live_task_request, **runtime_task_request_payload}
    live_task_request = _select_runtime_live_task_request(live_task_request_source, active_task_payload)
    integration_listener_decision = (
        integration_payload.get("listener_decision")
        if isinstance(integration_payload.get("listener_decision"), dict)
        else {}
    )
    listener_decision = integration_listener_decision
    if decision_payload:
        integration_decision_dt = _parse_timestamp(integration_listener_decision.get("generated_at"))
        runtime_decision_dt = _parse_timestamp(decision_payload.get("generated_at"))
        if not integration_listener_decision or runtime_decision_dt is None or integration_decision_dt is None or runtime_decision_dt >= integration_decision_dt:
            listener_decision = {**integration_listener_decision, **decision_payload}
    mim_status = (
        integration_payload.get("mim_status")
        if isinstance(integration_payload.get("mim_status"), dict)
        else {}
    )
    authority_reset = (
        integration_payload.get("objective_authority_reset")
        if isinstance(integration_payload.get("objective_authority_reset"), dict)
        else {}
    )
    handshake = (
        integration_payload.get("mim_handshake")
        if isinstance(integration_payload.get("mim_handshake"), dict)
        else {}
    )
    training_source = integration_payload.get("training_status")
    if not isinstance(training_source, dict):
        training_source = training_payload
    training_status = _normalize_training_status(training_source)
    training_status["idle_policy"] = _normalize_idle_training_policy(autonomy_payload, training_status)
    execution_status = _normalize_execution_status(
        active_objective_payload,
        active_task_payload,
        activity_payload,
        validation_payload,
        execution_result_payload,
        truth_payload,
    )
    execution_objective_id = str(execution_status.get("objective_id") or "").strip()
    execution_updated_at = str(execution_status.get("updated_at") or "").strip()
    shared_truth_state = str(shared_truth_payload.get("state") or "").strip().upper()
    shared_truth_reason = _compact_text(shared_truth_payload.get("state_reason") or "", 220)
    shared_truth_next_action = _compact_text(shared_truth_payload.get("authoritative_next_action") or "", 180)
    shared_truth_objective_id = str(shared_truth_payload.get("objective_id") or "").strip()
    shared_truth_superseded_by_execution = bool(
        shared_truth_payload
        and execution_status.get("available")
        and execution_objective_id
        and shared_truth_objective_id
        and not _same_objective(execution_objective_id, shared_truth_objective_id)
        and execution_updated_at
        and (
            _parse_timestamp(execution_updated_at) is not None
            and _parse_timestamp(_pick_first_text(shared_truth_payload.get("generated_at"))) is not None
            and _parse_timestamp(execution_updated_at) >= _parse_timestamp(_pick_first_text(shared_truth_payload.get("generated_at")))
            or _parse_timestamp(execution_updated_at) is not None
            and _parse_timestamp(_pick_first_text(shared_truth_payload.get("generated_at"))) is None
        )
    )
    if shared_truth_payload:
        shared_truth_generated_at = _pick_first_text(shared_truth_payload.get("generated_at"))
        shared_truth_is_newer = bool(
            shared_truth_generated_at
            and (
                not execution_status.get("updated_at")
                or _parse_timestamp(shared_truth_generated_at) is not None
                and _parse_timestamp(execution_status.get("updated_at")) is not None
                and _parse_timestamp(shared_truth_generated_at) >= _parse_timestamp(execution_status.get("updated_at"))
                or _parse_timestamp(shared_truth_generated_at) is not None
                and _parse_timestamp(execution_status.get("updated_at")) is None
            )
        )
        execution_status["shared_truth"] = shared_truth_payload
        execution_status["authoritative_next_action"] = shared_truth_next_action
        if shared_truth_reason and not shared_truth_superseded_by_execution:
            execution_status["summary"] = shared_truth_reason
            execution_status["activity_summary"] = shared_truth_reason
        if shared_truth_is_newer and not shared_truth_superseded_by_execution:
            execution_status["updated_at"] = shared_truth_generated_at
            execution_status["updated_age"] = _format_age(shared_truth_generated_at)
            execution_status["last_update_age_seconds"] = _age_seconds(shared_truth_generated_at)
        if shared_truth_superseded_by_execution:
            execution_status["shared_truth_superseded"] = True
        elif shared_truth_state == "ACTIVE":
            execution_status["activity_state"] = "working"
            execution_status["activity_label"] = "Active"
            execution_status["active"] = True
        elif shared_truth_state == "BLOCKED_WITH_REASON":
            execution_status["activity_state"] = "stalled"
            execution_status["activity_label"] = "Blocked"
            execution_status["active"] = False
            if shared_truth_is_newer:
                execution_status["phase_progress"] = {
                    "available": False,
                    "label": "Phase progress",
                    "percent_complete": 0,
                    "completed_milestones": 0,
                    "total_milestones": 0,
                    "next_gate": "",
                    "summary": "",
                    "milestones": [],
                }
                execution_status["stall_signal"] = {
                    "flagged": False,
                    "level": "shared_truth_blocked",
                    "threshold_seconds": None,
                    "age_seconds": execution_status.get("last_update_age_seconds"),
                    "summary": shared_truth_reason,
                }
        elif shared_truth_state in {"ACCEPTED_COMPLETE", "ACCEPTED_COMPLETE_PENDING_MIM_REFRESH"}:
            execution_status["activity_state"] = "complete"
            execution_status["activity_label"] = "Complete"
            execution_status["active"] = False
            execution_status["phase_progress"] = {
                "available": False,
                "label": "Phase progress",
                "percent_complete": 0,
                "completed_milestones": 0,
                "total_milestones": 0,
                "next_gate": "",
                "summary": "",
                "milestones": [],
            }
            if shared_truth_is_newer:
                execution_status["stall_signal"] = {
                    "flagged": False,
                    "level": "ok",
                    "threshold_seconds": None,
                    "age_seconds": execution_status.get("last_update_age_seconds"),
                    "summary": "",
                }
        elif shared_truth_state == "REPLAY_OR_REPLAN_REQUIRED":
            execution_status["activity_state"] = "waiting"
            execution_status["activity_label"] = "Replay required"
            execution_status["active"] = False
            if shared_truth_is_newer:
                execution_status["phase_progress"] = {
                    "available": False,
                    "label": "Phase progress",
                    "percent_complete": 0,
                    "completed_milestones": 0,
                    "total_milestones": 0,
                    "next_gate": "",
                    "summary": "",
                    "milestones": [],
                }
                execution_status["stall_signal"] = {
                    "flagged": False,
                    "level": "shared_truth_replay_required",
                    "threshold_seconds": None,
                    "age_seconds": execution_status.get("last_update_age_seconds"),
                    "summary": shared_truth_reason,
                }
        elif shared_truth_state in {"DISAGREEMENT", "STALE"}:
            execution_status["activity_state"] = "stalled"
            execution_status["activity_label"] = "Stale" if shared_truth_state == "STALE" else "Disagreement"
            execution_status["active"] = False
            if shared_truth_is_newer:
                execution_status["phase_progress"] = {
                    "available": False,
                    "label": "Phase progress",
                    "percent_complete": 0,
                    "completed_milestones": 0,
                    "total_milestones": 0,
                    "next_gate": "",
                    "summary": "",
                    "milestones": [],
                }
                execution_status["stall_signal"] = {
                    "flagged": False,
                    "level": "shared_truth_override",
                    "threshold_seconds": None,
                    "age_seconds": execution_status.get("last_update_age_seconds"),
                    "summary": shared_truth_reason,
                }
    guidance = _normalize_guidance_items(integration_payload.get("bridge_operator_guidance"))

    canonical_objective = str(
        handshake.get("current_next_objective")
        or mim_status.get("objective_active")
        or alignment.get("mim_objective_active")
        or ""
    ).strip()
    live_objective = str(
        live_task_request.get("objective_id")
        or live_task_request.get("normalized_objective_id")
        or execution_objective_id
        or alignment.get("tod_current_objective")
        or ""
    ).strip()
    alignment_status = str(alignment.get("status") or "unknown").strip().lower() or "unknown"

    task_identity_contention = _derive_task_identity_contention(
        canonical_objective=canonical_objective,
        live_task_request=live_task_request,
        active_task_payload=active_task_payload,
        listener_decision=listener_decision,
        decision_payload=decision_payload,
    )
    task_identity_repair = {"attempted": False, "reason": "not_needed"}
    if bool(task_identity_contention.get("safe_self_repair") is True):
        task_identity_repair = _attempt_task_identity_self_repair(task_identity_contention)
        if bool(task_identity_repair.get("published")):
            # Preserve the selected canonical task identity in this rendered state.
            live_task_request = {
                **live_task_request,
                "request_id": str(task_identity_contention.get("active_task_id") or live_task_request.get("request_id") or "").strip(),
                "task_id": str(task_identity_contention.get("active_task_id") or live_task_request.get("task_id") or "").strip(),
                "objective_id": str(task_identity_contention.get("authoritative_objective_id") or live_task_request.get("objective_id") or "").strip(),
                "source_service": "tod-ui-task-identity-self-repair",
                "generated_at": _utc_now_iso(),
            }
    evidence_status = str(evidence.get("status") or "unknown").strip().lower() or "unknown"
    publish_status = str(publish.get("status") or "unknown").strip().lower() or "unknown"
    publish_consumer_status = str(publish.get("consumer_status") or "").strip().lower()
    decision_state = str(
        listener_decision.get("execution_state")
        or decision_payload.get("execution_state")
        or "unknown"
    ).strip().lower() or "unknown"
    decision_outcome = str(
        listener_decision.get("decision_outcome")
        or decision_payload.get("decision_outcome")
        or "unknown"
    ).strip().lower() or "unknown"
    decision_reason = str(
        listener_decision.get("reason_code")
        or decision_payload.get("reason_code")
        or "unknown"
    ).strip().lower() or "unknown"
    decision_summary = _compact_text(
        listener_decision.get("summary") or decision_payload.get("summary") or "",
        220,
    ).lower()
    if bool(task_identity_contention.get("detected") is True):
        decision_reason = str(task_identity_contention.get("reason_code") or "task_id_mismatch_same_objective").strip().lower()
        decision_summary = str(task_identity_contention.get("summary") or "").strip().lower()
    probe_status = str(probe_payload.get("status") or "unknown").strip().lower() or "unknown"
    authority_reset_active = bool(authority_reset.get("active") is True)
    canonical_token = _normalize_objective_token(canonical_objective)
    live_token = _normalize_objective_token(live_objective)
    if canonical_token and live_token and canonical_token != live_token:
        alignment_status = "mismatch"
    current_objective_token = canonical_token or live_token
    live_request_token = _normalize_objective_token(
        live_task_request.get("normalized_objective_id") or live_task_request.get("objective_id")
    )
    listener_objective_token = _normalize_objective_token(
        listener_decision.get("normalized_objective_id")
        or listener_decision.get("objective_id")
        or decision_payload.get("normalized_objective_id")
        or decision_payload.get("objective_id")
    )
    failure_signals = [
        str(item).strip()
        for item in evidence.get("failure_signals", [])
        if str(item).strip()
    ] if isinstance(evidence.get("failure_signals"), list) else []
    stale_residue_codes = {
        "live_task_request_objective_mismatch",
        "live_task_request_not_promoted",
    }
    alignment_is_current = alignment_status in {"match", "aligned", "in_sync", "ok"} and _same_objective(canonical_objective, live_objective)
    listener_residue_stale = bool(
        alignment_is_current
        and current_objective_token
        and listener_objective_token
        and listener_objective_token != current_objective_token
    )
    live_request_residue_stale = bool(
        alignment_is_current
        and current_objective_token
        and live_request_token
        and live_request_token != current_objective_token
    )
    promoted_live_request_current = bool(
        alignment_is_current
        and bool(live_task_request.get("promotion_applied") is True)
        and current_objective_token
        and live_token == current_objective_token
    )
    listener_alignment_wait_residue = bool(
        alignment_is_current
        and promoted_live_request_current
        and decision_reason in {"external_coordination_blocker", "objective_mismatch"}
        and decision_outcome in {"acknowledge_and_wait_on_dependency", "reject_with_specific_policy_reason"}
        and (
            decision_state == "waiting_on_dependency"
            or decision_state == "rejected"
            or "alignment" in decision_summary
            or "authoritative objective" in decision_summary
        )
    )
    residue_signals_only = bool(failure_signals) and all(signal in stale_residue_codes for signal in failure_signals)
    recovery_validation = recovery_payload.get("validation") if isinstance(recovery_payload.get("validation"), dict) else {}
    recovery_target_matches = _same_objective(recovery_payload.get("objective_id"), current_objective_token)
    recovery_confirms_alignment = bool(
        recovery_payload
        and recovery_target_matches
        and recovery_validation.get("passed") is True
        and recovery_validation.get("mismatch_cleared") is True
        and recovery_validation.get("remote_publish_verified") is True
    )
    remote_publish_verified = bool(
        evidence.get("remote_publish_verified") is True
        or recovery_confirms_alignment
        or promoted_live_request_current
        or str(publish.get("mim_mirror_status") or "").strip().lower() in {"mirrored", "uploaded"}
    )
    local_publish_ready = bool(
        alignment_is_current
        and publish_status == "local_rebuilt"
        and publish_consumer_status == "local_rebuild"
        and decision_outcome == "execute"
        and decision_state in {"ready_to_execute", "ready", "execute_now"}
        and not authority_reset_active
    )
    stale_residue_suppressed = bool(
        alignment_is_current
        and remote_publish_verified
        and (listener_residue_stale or listener_alignment_wait_residue or live_request_residue_stale or residue_signals_only)
    )

    effective_guidance = guidance
    if stale_residue_suppressed:
        stale_summary = (
            f"Canonical objective {canonical_objective or live_objective or 'unknown'} is current, but older listener or publish residue still references objective "
            f"{listener_objective_token or live_request_token or 'unknown'}."
        )
        effective_guidance = [
            {
                "code": "stale_objective_residue_suppressed",
                "severity": "info",
                "summary": _compact_text(stale_summary, 180),
                "recommended_action": _compact_text(
                    "Treat the stale listener or publication artifact as superseded on this console. Only regenerate it if a downstream consumer still requires the older file.",
                    220,
                ),
            }
        ]

    effective_listener_decision = {
        "decision_outcome": str(
            listener_decision.get("decision_outcome")
            or decision_payload.get("decision_outcome")
            or ""
        ).strip(),
        "reason_code": str(
            listener_decision.get("reason_code")
            or decision_payload.get("reason_code")
            or ""
        ).strip(),
        "execution_state": str(
            listener_decision.get("execution_state")
            or decision_payload.get("execution_state")
            or ""
        ).strip(),
        "next_step_recommendation": str(
            listener_decision.get("next_step_recommendation")
            or decision_payload.get("next_step_recommendation")
            or ""
        ).strip(),
        "generated_at": str(
            listener_decision.get("generated_at")
            or decision_payload.get("generated_at")
            or ""
        ).strip(),
        "summary": _compact_text(
            listener_decision.get("summary") or decision_payload.get("summary") or "No listener decision summary is available.",
            220,
        ),
    }
    if bool(task_identity_contention.get("detected") is True):
        effective_listener_decision = {
            **effective_listener_decision,
            "decision_outcome": "reject_with_specific_policy_reason",
            "reason_code": str(task_identity_contention.get("reason_code") or "task_id_mismatch_same_objective").strip(),
            "execution_state": "rejected",
            "next_step_recommendation": _compact_text(task_identity_contention.get("recommended_repair") or "", 220),
            "summary": _compact_text(task_identity_contention.get("summary") or "", 220),
            "authoritative_objective_id": str(task_identity_contention.get("authoritative_objective_id") or "").strip(),
            "request_objective_id": str(task_identity_contention.get("request_objective_id") or "").strip(),
            "active_task_id": str(task_identity_contention.get("active_task_id") or "").strip(),
            "request_task_id": str(task_identity_contention.get("request_task_id") or "").strip(),
            "source_service": str(task_identity_contention.get("source_service") or "").strip(),
            "last_writer": str(task_identity_contention.get("last_writer") or "").strip(),
            "blocker_type": "task_identity_contention",
            "mismatch_type": str(task_identity_contention.get("mismatch_type") or "").strip(),
            "can_self_repair": bool(task_identity_contention.get("safe_self_repair") is True),
            "self_repair_attempted": bool(task_identity_repair.get("attempted") is True),
            "self_repair_result": str(task_identity_repair.get("reason") or "").strip(),
        }
    if stale_residue_suppressed:
        effective_listener_decision = {
            "decision_outcome": "superseded_stale_listener_residue",
            "reason_code": "stale_listener_objective_residue",
            "execution_state": "aligned_after_recovery",
            "next_step_recommendation": "continue_current_objective_execution",
            "generated_at": str(recovery_payload.get("generated_at") or effective_listener_decision.get("generated_at") or integration_payload.get("generated_at") or "").strip(),
            "summary": _compact_text(
                f"Canonical objective {canonical_objective or live_objective or 'unknown'} is aligned and the promoted live request is current. Listener coordination residue for objective {listener_objective_token or live_request_token or current_objective_token or 'unknown'} is not authoritative on this console.",
                220,
            ),
        }

    effective_publish = {
        "status": str(publish.get("status") or "unknown").strip(),
        "remote_access_status": str(publish.get("remote_access_status") or "").strip(),
        "mim_mirror_status": str(publish.get("mim_mirror_status") or "").strip(),
        "consumer_status": str(publish.get("consumer_status") or "").strip(),
        "uploaded_at": str(publish.get("uploaded_at") or "").strip(),
        "error": str(publish.get("error") or "").strip(),
        "summary": _compact_text(
            publish.get("error")
            or f"status={publish.get('status') or 'unknown'}; mirror={publish.get('mim_mirror_status') or 'unknown'}; consumer={publish.get('consumer_status') or 'unknown'}",
            220,
        ),
    }
    if stale_residue_suppressed:
        effective_publish = {
            **effective_publish,
            "status": "uploaded" if promoted_live_request_current else "remote_verified",
            "uploaded_at": str(recovery_payload.get("generated_at") or effective_publish.get("uploaded_at") or integration_payload.get("generated_at") or "").strip(),
            "error": "",
            "summary": _compact_text(
                f"The live task request is promoted to canonical objective {canonical_objective or live_objective or 'unknown'}. Older local publication residue for objective {live_request_token or current_objective_token or 'unknown'} is suppressed on this console.",
                220,
            ),
        }

    status_code = "attention"
    status_label = "ATTENTION"
    headline = "ATTENTION - TOD needs review"
    summary = "TOD bridge state is available, but it needs operator review."

    if not integration_payload:
        status_code = "unknown"
        status_label = "UNKNOWN"
        headline = "UNKNOWN - TOD integration status missing"
        summary = "The shared TOD integration artifact is missing or unreadable."
    elif stale_residue_suppressed:
        status_code = "aligned"
        status_label = "ALIGNED"
        headline = "ALIGNED - canonical objective is current"
        summary = _compact_text(
            effective_listener_decision.get("summary")
            or effective_publish.get("summary")
            or "Canonical and live TOD state agree; older residue has been downgraded on this console.",
            220,
        )
    elif alignment_is_current and local_publish_ready and evidence_status != "fail":
        status_code = "aligned"
        status_label = "ALIGNED"
        headline = "ALIGNED - canonical and live TOD state agree"
        summary = "TOD and MIM objectives are in sync, and the listener is ready to execute from the locally rebuilt publish surface."
    elif alignment_status in {"match", "aligned", "in_sync", "ok"} and evidence_status == "pass" and publish_status in {"uploaded", "mirrored", "ok"}:
        status_code = "aligned"
        status_label = "ALIGNED"
        headline = "ALIGNED - canonical and live TOD state agree"
        summary = "TOD publication, objective alignment, and canonical bridge evidence are all in sync."
    elif alignment_status in {"mismatch", "drift", "out_of_sync"} or evidence_status == "fail":
        status_code = "drifted"
        status_label = "DRIFTED"
        headline = "DRIFTED - canonical and live objective disagree"
        summary = _compact_text(
            guidance[0].get("summary") if guidance else evidence.get("failure_signals") or alignment,
            220,
        ) or "The canonical objective and the live request surface do not agree."
    elif publish_status in {"failed", "error", "blocked"} or decision_state in {"blocked", "failed", "stale"} or probe_status in {"unreachable", "failed"}:
        status_code = "blocked"
        status_label = "BLOCKED"
        headline = "BLOCKED - TOD can see the work but cannot advance it cleanly"
        summary = _compact_text(
            listener_decision.get("summary")
            or decision_payload.get("summary")
            or publish.get("error")
            or "One or more TOD bridge stages are blocked.",
            220,
        )
    elif authority_reset_active:
        status_code = "authority_reset"
        status_label = "AUTHORITY RESET"
        headline = "AUTHORITY RESET - TOD is holding a stricter canonical baseline"
        summary = _compact_text(authority_reset.get("reason") or "Objective authority reset is active.", 220)

    if shared_truth_superseded_by_execution:
        execution_summary = _compact_text(
            execution_status.get("activity_summary") or execution_status.get("summary"),
            220,
        )
        status_code = "drifted"
        status_label = "DRIFTED"
        headline = "DRIFTED - active TOD execution differs from older shared truth"
        summary = execution_summary or "TOD is actively working a newer execution slice than the currently published shared truth lane."
    elif shared_truth_state == "ACTIVE":
        status_code = "active"
        status_label = "ACTIVE"
        headline = "ACTIVE - TOD and MIM shared truth shows live work"
        summary = shared_truth_reason or summary
    elif shared_truth_state == "BLOCKED_WITH_REASON":
        status_code = "blocked_with_reason"
        status_label = "BLOCKED_WITH_REASON"
        headline = "BLOCKED_WITH_REASON - shared truth reports an explicit blocker"
        summary = shared_truth_reason or summary
    elif shared_truth_state == "ACCEPTED_COMPLETE_PENDING_MIM_REFRESH":
        status_code = "accepted_complete_pending_mim_refresh"
        status_label = "ACCEPTED_COMPLETE_PENDING_MIM_REFRESH"
        headline = "ACCEPTED_COMPLETE_PENDING_MIM_REFRESH - TOD completed and MIM refresh is pending"
        summary = shared_truth_reason or summary
    elif shared_truth_state == "ACCEPTED_COMPLETE":
        status_code = "accepted_complete"
        status_label = "ACCEPTED_COMPLETE"
        headline = "ACCEPTED_COMPLETE - shared truth confirms completion"
        summary = shared_truth_reason or summary
    elif shared_truth_state == "REPLAY_OR_REPLAN_REQUIRED":
        status_code = "replay_or_replan_required"
        status_label = "REPLAY_OR_REPLAN_REQUIRED"
        headline = "REPLAY_OR_REPLAN_REQUIRED - shared truth requires a forced replay or replan"
        summary = shared_truth_reason or summary
    elif shared_truth_state == "DISAGREEMENT":
        status_code = "disagreement"
        status_label = "DISAGREEMENT"
        headline = "DISAGREEMENT - TOD and MIM truth surfaces disagree"
        summary = shared_truth_reason or summary
    elif shared_truth_state == "STALE":
        status_code = "stale"
        status_label = "STALE"
        headline = "STALE - shared truth has no recent specific execution evidence"
        summary = shared_truth_reason or summary

    if authority_reset_active and status_code == "aligned":
        status_code = "authority_reset"
        status_label = "AUTHORITY RESET"
        headline = "AUTHORITY RESET - alignment is constrained by active reset policy"
        summary = _compact_text(authority_reset.get("reason") or summary, 220)

    quick_facts = {
        "canonical_objective": canonical_objective or "Unknown",
        "live_request_objective": live_objective or "Unknown",
        "listener_state": str(
            effective_listener_decision.get("execution_state")
            or "unknown"
        ).strip().replace("_", " "),
        "publish_status": str(effective_publish.get("status") or "unknown").strip().replace("_", " "),
        "decision_outcome": str(
            effective_listener_decision.get("decision_outcome")
            or "unknown"
        ).strip().replace("_", " "),
        "authority_reset": "Active" if authority_reset_active else "Inactive",
        "training_state": training_status.get("state_label") or "Unknown",
        "training_progress": f"{training_status.get('percent_complete', 0)}%" if training_status.get("available") else "Unknown",
        "shared_truth_state": shared_truth_state or "Unknown",
        "blocker_type": str(effective_listener_decision.get("blocker_type") or "").strip(),
        "mismatch_type": str(effective_listener_decision.get("mismatch_type") or "").strip(),
        "active_task_id": str(effective_listener_decision.get("active_task_id") or "").strip(),
        "request_task_id": str(effective_listener_decision.get("request_task_id") or "").strip(),
        "source_service": str(effective_listener_decision.get("source_service") or "").strip(),
        "last_writer": str(effective_listener_decision.get("last_writer") or "").strip(),
        "self_repair_attempted": bool(effective_listener_decision.get("self_repair_attempted") is True),
        "self_repair_result": str(effective_listener_decision.get("self_repair_result") or "").strip(),
    }

    latest_operator_action = _latest_operator_action_payload()
    planner_state = _derive_planner_state(
        live_task_request,
        effective_listener_decision,
        execution_status,
        latest_operator_action,
    )
    binding_materialization = _attempt_executor_binding_materialization(live_task_request, execution_status, planner_state)
    if isinstance(binding_materialization.get("updated_live_task_request"), dict):
        live_task_request = binding_materialization.get("updated_live_task_request") or live_task_request
    if binding_materialization.get("materialized"):
        planner_state["assigned_executor"] = str(binding_materialization.get("selected_executor") or "local").strip()

    binding_status = "ready" if bool(binding_materialization.get("materialized")) else "missing" if str(binding_materialization.get("reason_code") or "").strip() else "unknown"
    execution_status["executor_binding"] = {
        "status": binding_status,
        "reason_code": str(binding_materialization.get("reason_code") or "").strip(),
        "message": str(binding_materialization.get("message") or "").strip(),
        "task_id": str(binding_materialization.get("task_id") or "").strip(),
        "objective_id": str(binding_materialization.get("objective_id") or "").strip(),
        "assigned_executor": str(binding_materialization.get("assigned_executor") or "").strip(),
        "selected_executor": str(binding_materialization.get("selected_executor") or "").strip(),
        "expected_executor": str(binding_materialization.get("expected_executor") or "").strip(),
        "active_engine": str(binding_materialization.get("active_engine") or "").strip(),
        "executor_binding": str(binding_materialization.get("executor_binding") or "").strip(),
        "task_category": str(binding_materialization.get("task_category") or "").strip(),
        "bounded_edit_mode": str(binding_materialization.get("bounded_edit_mode") or "").strip(),
        "target_artifact_path": str(binding_materialization.get("target_artifact_path") or "").strip(),
        "missing_field_or_function": str(binding_materialization.get("missing_field_or_function") or "").strip(),
        "next_executable_repair": str(binding_materialization.get("next_executable_repair") or "").strip(),
    }
    if binding_status == "missing":
        missing_summary = "Task identity is repaired, but no executor binding was produced for the queued next step."
        execution_status["activity_state"] = "blocked"
        execution_status["activity_label"] = "Binding Required"
        execution_status["activity_summary"] = missing_summary
        execution_status["wait_reason"] = missing_summary
        execution_status["wait_target"] = LOCAL_EXECUTOR_BINDING
        execution_status["wait_target_label"] = LOCAL_EXECUTOR_BINDING
        execution_status["executor_binding_status"] = "missing"
        execution_status["executor_binding_target"] = LOCAL_EXECUTOR_BINDING
        execution_status["executor_binding_command"] = LOCAL_EXECUTOR_BINDING_COMMAND
        execution_status["next_step"] = str(binding_materialization.get("next_executable_repair") or execution_status.get("next_step") or "").strip()
        planner_state["status"] = "blocked"
        planner_state["status_label"] = "Binding Required"
        planner_state["summary"] = missing_summary
        planner_state["current_step"] = "Executor binding missing"
        planner_state["next_step"] = str(binding_materialization.get("next_executable_repair") or planner_state.get("next_step") or "").strip()

    execution_status["planner_state"] = planner_state

    operator_state = {
        "execution": execution_status,
        "shared_truth": shared_truth_payload,
        "source_paths": {
            "active_objective": active_objective_path,
            "active_task": active_task_path,
            "execution_truth": truth_path,
            "shared_truth": shared_truth_path,
            "execution_result": execution_result_path,
            "validation_result": validation_path,
            "remote_recovery": recovery_path,
        },
        "objective_alignment": alignment,
        "live_task_request": live_task_request,
    }
    operator_actions = _operator_action_specs(operator_state)
    execution_live_state = _derive_execution_live_state(execution_status, planner_state, operator_actions)
    execution_status["live_state"] = execution_live_state
    if bool(execution_live_state.get("prefer_execution_surface")):
        planner_state["is_newer_than_executor"] = False
    if bool(execution_live_state.get("is_stuck")):
        barrier_summary = " | ".join(str(item) for item in execution_live_state.get("barriers", []) if str(item).strip())
        if barrier_summary:
            execution_status["wait_reason"] = _compact_text(barrier_summary, 220)
            execution_status["activity_summary"] = _compact_text(
                f"{execution_status.get('activity_summary') or ''} Escalation: {execution_live_state.get('next_to_progress') or ''}",
                220,
            )
            if not str(execution_status.get("next_step") or "").strip():
                execution_status["next_step"] = str(execution_live_state.get("next_to_progress") or "").strip()
    objective_cards = _build_objective_cards(
        {
            "execution": execution_status,
            "shared_truth": shared_truth_payload,
            "status": {"summary": summary, "label": status_label},
            "objective_alignment": {
                "tod_current_objective": str(live_task_request.get("normalized_objective_id") or live_task_request.get("objective_id") or alignment.get("tod_current_objective") or "").strip(),
                "mim_objective_active": str(alignment.get("mim_objective_active") or "").strip(),
            },
            "live_task_request": live_task_request,
            "planner_state": planner_state,
            "conversation": {
                "quick_actions": [
                    {
                        "id": "send-to-copilot",
                        "label": "Send To Codex",
                        "prompt": "TOD, package the current issue, evidence, and next bounded repair request for Copilot-style troubleshooting and report the handoff summary in this thread.",
                    }
                ]
            },
            "operator_actions": operator_actions,
        }
    )

    return {
        "generated_at": _utc_now_iso(),
        "runtime_build": UI_BUILD_ID,
        "source_paths": {
            "integration_status": integration_path,
            "training_status": training_path,
            "autonomy_status": autonomy_path,
            "active_objective": active_objective_path,
            "active_task": active_task_path,
            "activity_stream": activity_path,
            "validation_result": validation_path,
            "execution_result": execution_result_path,
            "execution_truth": truth_path,
            "shared_truth": shared_truth_path,
            "task_request": runtime_task_request_path,
            "operator_action": str(TOD_OPERATOR_ACTION_LATEST_PATH),
            "operator_evidence": str(TOD_OPERATOR_EVIDENCE_PATH),
            "execution_decision": str(SHARED_RUNTIME_ROOT / "TOD_MIM_EXECUTION_DECISION.latest.json"),
            "console_probe": str(SHARED_RUNTIME_ROOT / "TOD_CONSOLE_PROBE.latest.json"),
            "remote_recovery": recovery_path,
        },
        "conversation": {
          "enabled": True,
          "mode": "tod",
                    "state_url": "/tod/ui/chat/state",
                    "message_url": "/tod/ui/chat/message",
                                        "handoff_url": "/tod/ui/chat/handoff",
          "upload_url": "/tod/ui/chat/upload-image",
          "default_session_key": "tod-console-public",
                    "summary": "TOD operator chat is evidence-backed from live bridge, listener, publish, and training artifacts, with direct execution, uploads, training launch, and Codex handoffs available on this surface.",
                    "auto_trigger": {
                            "enabled": True,
                            "status_codes": ["attention"],
                            "once_per_session": True,
                            "prompt": "TOD, the console status is ATTENTION and requires review. Diagnose the current issue from live bridge, listener, maintenance, watchdog, and canonical objective evidence. Then report: 1. the issue summary, 2. the strongest evidence, 3. the next bounded repair request for Codex-style resolution, and 4. whether operator intervention is still required.",
                            "success_text": "TOD auto-resolution request sent.",
                    },
          "quick_actions": [
              {
                  "id": "start-training",
                  "label": "TOD Training",
                  "description": "Send the bounded six-hour training request through TOD chat and capture TOD's training reply in the thread.",
                  "prompt": "TOD, start your next bounded 6-hour training cycle and report the exact runbook status, current gate, and first blocker if it cannot proceed.",
              },
              {
                  "id": "resolve-drift",
                  "label": "Resolve Drift",
                  "description": "Send a bounded prompt asking TOD to explain and resolve current objective drift.",
                  "prompt": "TOD, resolve the current drift between canonical and live objective state. Report the mismatch, the repair step already underway, and the next validation check.",
              },
              {
                  "id": "send-to-copilot",
                  "label": "Send To Codex",
                  "description": "Create a real handoff artifact and publish it into the TOD/MIM dialog lane for Codex-style troubleshooting.",
                  "action_type": "handoff",
                  "prompt": "TOD, package the current issue, evidence, and next bounded repair request for Copilot-style troubleshooting and report the handoff summary in this thread.",
              },
          ],
        },
        "status": {
            "code": status_code,
            "label": status_label,
            "headline": headline,
            "summary": summary,
        },
        "quick_facts": quick_facts,
        "training_status": training_status,
        "execution": execution_status,
        "shared_truth": shared_truth_payload,
        "objective_cards": objective_cards,
        "operator_actions": operator_actions,
        "operator_activity_timeline": _load_recent_operator_actions(limit=10),
        "operator_evidence": _build_operator_evidence(
            {
                "status": {"summary": summary},
                "execution": execution_status,
                "shared_truth": shared_truth_payload,
                "source_paths": {
                    "active_objective": active_objective_path,
                    "active_task": active_task_path,
                    "validation_result": validation_path,
                    "execution_result": execution_result_path,
                    "execution_truth": truth_path,
                    "shared_truth": shared_truth_path,
                    "remote_recovery": recovery_path,
                },
                "objective_alignment": alignment,
                "live_task_request": live_task_request,
            }
        ),
        "mim_status": {
            "available": bool(mim_status.get("available")),
            "objective_active": str(mim_status.get("objective_active") or "").strip(),
            "phase": str(mim_status.get("phase") or "").strip(),
            "generated_at": str(mim_status.get("generated_at") or "").strip(),
            "generated_age": _format_age(mim_status.get("generated_at")),
            "blockers": str(mim_status.get("blockers") or "").strip(),
        },
        "objective_alignment": {
            "status": "mismatch" if alignment_status in {"mismatch", "drift", "out_of_sync"} else str(alignment.get("status") or "unknown").strip(),
            "aligned": bool(alignment_status in {"match", "aligned", "in_sync", "ok"} and canonical_token and live_token and canonical_token == live_token),
            "tod_current_objective": str(live_task_request.get("normalized_objective_id") or live_task_request.get("objective_id") or alignment.get("tod_current_objective") or "").strip(),
            "mim_objective_active": str(alignment.get("mim_objective_active") or "").strip(),
            "delta": alignment.get("delta"),
            "summary": (
                "TOD and MIM objectives are in sync."
                if alignment_status in {"match", "aligned", "in_sync", "ok"} and canonical_token and live_token and canonical_token == live_token
                else f"TOD sees {live_objective or 'unknown'}, while MIM canonical state points at {canonical_objective or 'unknown'}."
            ),
        },
        "bridge_canonical_evidence": {
            "status": "pass" if stale_residue_suppressed else str(evidence.get("status") or "unknown").strip(),
            "canonical_refresh_satisfied": bool(evidence.get("canonical_refresh_satisfied") is True),
            "live_bridge_publish_satisfied": bool(evidence.get("live_bridge_publish_satisfied") is True or promoted_live_request_current),
            "remote_publish_verified": remote_publish_verified,
            "failure_signals": [] if stale_residue_suppressed else failure_signals,
            "summary": _compact_text(
                effective_publish.get("summary") if stale_residue_suppressed else (
                    "; ".join(failure_signals) if failure_signals else evidence.get("status")
                ),
                220,
            ) or "No canonical bridge evidence summary is available.",
        },
        "live_task_request": {
            "request_id": str(live_task_request.get("request_id") or "").strip(),
            "task_id": str(live_task_request.get("task_id") or "").strip(),
            "objective_id": str(live_task_request.get("objective_id") or "").strip(),
            "normalized_objective_id": str(live_task_request.get("normalized_objective_id") or "").strip(),
            "generated_at": str(live_task_request.get("generated_at") or "").strip(),
            "generated_age": _format_age(live_task_request.get("generated_at")),
            "promotion_applied": bool(live_task_request.get("promotion_applied") is True),
            "promotion_reason": str(live_task_request.get("promotion_reason") or "").strip(),
        },
        "listener_decision": {
            "decision_outcome": effective_listener_decision.get("decision_outcome") or "",
            "reason_code": effective_listener_decision.get("reason_code") or "",
            "execution_state": effective_listener_decision.get("execution_state") or "",
            "next_step_recommendation": effective_listener_decision.get("next_step_recommendation") or "",
            "generated_at": effective_listener_decision.get("generated_at") or "",
            "generated_age": _format_age(effective_listener_decision.get("generated_at")),
            "summary": effective_listener_decision.get("summary") or "No listener decision summary is available.",
            "authoritative_objective_id": effective_listener_decision.get("authoritative_objective_id") or "",
            "request_objective_id": effective_listener_decision.get("request_objective_id") or "",
            "active_task_id": effective_listener_decision.get("active_task_id") or "",
            "request_task_id": effective_listener_decision.get("request_task_id") or "",
            "source_service": effective_listener_decision.get("source_service") or "",
            "last_writer": effective_listener_decision.get("last_writer") or "",
            "blocker_type": effective_listener_decision.get("blocker_type") or "",
            "mismatch_type": effective_listener_decision.get("mismatch_type") or "",
            "can_self_repair": bool(effective_listener_decision.get("can_self_repair") is True),
            "self_repair_attempted": bool(effective_listener_decision.get("self_repair_attempted") is True),
            "self_repair_result": effective_listener_decision.get("self_repair_result") or "",
        },
        "task_identity_contention": {
            "detected": bool(task_identity_contention.get("detected") is True),
            "blocker_type": str(task_identity_contention.get("blocker_type") or "").strip(),
            "mismatch_type": str(task_identity_contention.get("mismatch_type") or "").strip(),
            "authoritative_objective_id": str(task_identity_contention.get("authoritative_objective_id") or "").strip(),
            "request_objective_id": str(task_identity_contention.get("request_objective_id") or "").strip(),
            "active_task_id": str(task_identity_contention.get("active_task_id") or "").strip(),
            "request_task_id": str(task_identity_contention.get("request_task_id") or "").strip(),
            "source_service": str(task_identity_contention.get("source_service") or "").strip(),
            "last_writer": str(task_identity_contention.get("last_writer") or "").strip(),
            "recommended_repair": str(task_identity_contention.get("recommended_repair") or "").strip(),
            "can_self_repair": bool(task_identity_contention.get("safe_self_repair") is True),
            "self_repair_attempted": bool(task_identity_repair.get("attempted") is True),
            "self_repair_result": str(task_identity_repair.get("reason") or "").strip(),
        },
        "operator_guidance": effective_guidance,
        "publish": {
            "status": effective_publish.get("status") or "unknown",
            "remote_access_status": effective_publish.get("remote_access_status") or "",
            "mim_mirror_status": effective_publish.get("mim_mirror_status") or "",
            "consumer_status": effective_publish.get("consumer_status") or "",
            "uploaded_at": effective_publish.get("uploaded_at") or "",
            "uploaded_age": _format_age(effective_publish.get("uploaded_at")),
            "error": effective_publish.get("error") or "",
            "summary": effective_publish.get("summary") or "No publish summary",
        },
        "authority_reset": {
            "active": authority_reset_active,
            "authoritative_current_objective": str(authority_reset.get("authoritative_current_objective") or "").strip() if authority_reset_active else "",
            "max_valid_objective": str(authority_reset.get("max_valid_objective") or "").strip() if authority_reset_active else "",
            "effective_at": str(authority_reset.get("effective_at") or "").strip() if authority_reset_active else "",
            "effective_age": _format_age(authority_reset.get("effective_at")) if authority_reset_active else "Inactive",
            "reason": _compact_text(authority_reset.get("reason"), 260) if authority_reset_active else "",
            "invalidated_objectives": [str(item).strip() for item in authority_reset.get("invalidated_objectives", []) if str(item).strip()] if authority_reset_active and isinstance(authority_reset.get("invalidated_objectives"), list) else [],
        },
        "console_probe": {
            "available": bool(probe_payload),
            "status": str(probe_payload.get("status") or "unknown").strip(),
            "http_status": probe_payload.get("http_status"),
            "generated_at": str(probe_payload.get("generated_at") or "").strip(),
            "generated_age": _format_age(probe_payload.get("generated_at")),
            "authority_role": str(
                (probe_payload.get("authority") if isinstance(probe_payload.get("authority"), dict) else {}).get("role") or ""
            ).strip(),
        },
        "execution_truth": {
            "available": bool(truth_payload),
            "generated_at": str(truth_payload.get("generated_at") or "").strip(),
            "generated_age": _format_age(truth_payload.get("generated_at")),
            "status": str(truth_payload.get("status") or truth_payload.get("truth_status") or "").strip(),
            "summary": _compact_text(
                truth_payload.get("summary") or truth_payload.get("truth_summary") or "",
                220,
            ),
        },
        "recent_handoffs": _load_recent_copilot_handoffs(
            limit=6,
            current_objective_id=canonical_objective or live_objective,
            current_request_id=str(live_task_request.get("request_id") or "").strip(),
        ),
    }


@router.get("/tod/ui/chat/state")
async def tod_ui_chat_state(
    session_key: str = Query("tod-console-public"),
    mode: str = Query("tod"),
) -> dict[str, Any]:
    del mode
    state = _build_tod_console_state()
    messages = _advance_pending_chat_progress(session_key, state)
    return _build_chat_payload(session_key, messages, state)


@router.post("/tod/ui/chat/message")
async def tod_ui_chat_message(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    session_key = str(payload.get("session_key") or "tod-console-public").strip() or "tod-console-public"
    message = _trim_message_text(payload.get("message"), 2000)
    state = _build_tod_console_state()
    session_payload = _load_chat_session_payload(session_key, state)
    messages = list(session_payload.get("messages") or [])
    visitor_name = _resolve_public_visitor_name()
    if message:
        messages.append({"role": "visitor", "author_name": visitor_name, "content": message, "created_at": _utc_now_iso()})
        if _classify_prompt(message) == "task":
            execution_ack = _publish_local_execution_ack(message, state, surface="tod", session_key=session_key)
            dispatch_record = _publish_task_execution_request(message, state, surface="tod", session_key=session_key)
            state = _build_tod_console_state()
            progress_messages = _build_task_progress_messages(message, state, execution_ack=execution_ack, dispatch_record=dispatch_record)
            messages.extend(progress_messages[:2])
            session_payload["pending_progress"] = progress_messages[2:]
        else:
            messages.append({"role": "tod", "content": _compose_operator_reply(message, state, surface_label="/tod"), "created_at": _utc_now_iso()})
            session_payload["pending_progress"] = []
        session_payload["messages"] = messages[-40:]
        _save_chat_session_payload(session_key, session_payload, state)
    return _build_chat_payload(session_key, messages, state)


@router.get("/tod/ui/chat/media/{asset_name}")
async def tod_ui_chat_media(asset_name: str) -> FileResponse:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "", str(asset_name or "")).strip()
    if not safe_name or safe_name != asset_name:
        raise HTTPException(status_code=404, detail="media_not_found")
    asset_path = TOD_CONSOLE_CHAT_MEDIA_ROOT / safe_name
    if not asset_path.exists() or not asset_path.is_file():
        raise HTTPException(status_code=404, detail="media_not_found")
    return FileResponse(asset_path)


@router.post("/tod/ui/chat/upload-image")
async def tod_ui_chat_upload_image(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    session_key = str(payload.get("session_key") or "tod-console-public").strip() or "tod-console-public"
    prompt = _compact_text(payload.get("prompt"), 2000)
    state = _build_tod_console_state()
    messages = _load_chat_messages(session_key, state)
    attachment = _persist_public_chat_image(payload.get("attachment"))
    visitor_name = _resolve_public_visitor_name()
    user_text = prompt or f"Shared image: {attachment['filename']}"
    messages.append(
        {
            "role": "visitor",
            "author_name": visitor_name,
            "content": user_text,
            "created_at": _utc_now_iso(),
            "attachment": attachment,
        }
    )
    issue_focus = _summarize_requested_task(prompt or f"review {attachment['filename']}", 180)
    messages.append(
        {
            "role": "tod",
            "content": "\n".join(
                [
                    f"Accepted. TOD attached the screenshot for {issue_focus}.",
                    f"Image: {attachment['filename']} Ã‚Â· {max(1, round(attachment['size_bytes'] / 1024))} KB Ã‚Â· {attachment['mime_type']}",
                    "Send To Codex packages the current request, strongest evidence, next bounded repair, next validation, and the latest screenshot from this thread into a real handoff artifact.",
                    "Add a short note about what you want reviewed, or press Send To Codex now for deeper troubleshooting." if not prompt else "Ask a bounded follow-up or press Send To Codex to publish this screenshot into the TOD/MIM dialog lane.",
                ]
            ),
            "created_at": _utc_now_iso(),
        }
    )
    _save_chat_messages(session_key, messages, state)
    chat_payload = _build_chat_payload(session_key, messages, state)
    chat_payload["image_upload"] = {"ok": True, "attachment": _normalize_chat_attachment(attachment)}
    return chat_payload


@router.post("/tod/ui/chat/handoff")
async def tod_ui_chat_handoff(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    session_key = str(payload.get("session_key") or "tod-console-public").strip() or "tod-console-public"
    message = _compact_text(
        payload.get("message")
        or "TOD, package the current issue, evidence, and next bounded repair request for Copilot-style troubleshooting and report the handoff summary in this thread.",
        2000,
    )
    state = _build_tod_console_state()
    messages = _load_chat_messages(session_key, state)
    messages.append({"role": "visitor", "author_name": _resolve_public_visitor_name(), "content": message, "created_at": _utc_now_iso()})
    attachments = _recent_chat_attachments(messages)
    handoff = _create_copilot_handoff(message, state, session_key, attachments=attachments)
    status = state.get("status") if isinstance(state.get("status"), dict) else {}
    reply = "\n".join(
        [
            "Copilot handoff created:",
            f"Issue: {_pick_first_text(status.get('headline'), status.get('summary'))}",
            f"Evidence: {_strongest_evidence(state)}",
            f"Bounded repair request: {_next_bounded_repair_request(state)}",
            f"Validation after repair: {_next_validation_check(state)}",
            f"Latest screenshot attached: {'yes' if attachments else 'no'}",
            f"Artifact: {handoff['artifact_path']}",
            f"Dialog session: {handoff['session_id']}",
            f"Dialog inbox: {handoff['dialog_index_path']}",
            f"Next expected reply: {handoff['reply_contract']}",
        ]
    )
    messages.append({"role": "tod", "content": reply, "created_at": _utc_now_iso()})
    _save_chat_messages(session_key, messages, state)
    chat_payload = _build_chat_payload(session_key, messages, state)
    chat_payload["handoff"] = handoff
    return chat_payload


@router.get("/chat/ui/state")
async def chat_ui_state(
    session_key: str = Query("copilot-operator-chat"),
    mode: str = Query("chat"),
) -> dict[str, Any]:
    del mode
    state = _build_tod_console_state()
    messages = _advance_pending_chat_progress(session_key, state)
    return _build_chat_payload(session_key, messages, state, surface="chat")


@router.post("/chat/ui/message")
async def chat_ui_message(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    session_key = str(payload.get("session_key") or "copilot-operator-chat").strip() or "copilot-operator-chat"
    message = _trim_message_text(payload.get("message"), 2000)
    state = _build_tod_console_state()
    session_payload = _load_chat_session_payload(session_key, state)
    messages = list(session_payload.get("messages") or [])
    if message:
        messages.append({"role": "operator", "content": message, "created_at": _utc_now_iso()})
        if _classify_prompt(message) == "task":
            execution_ack = _publish_local_execution_ack(message, state, surface="chat", session_key=session_key)
            dispatch_record = _publish_task_execution_request(message, state, surface="chat", session_key=session_key)
            state = _build_tod_console_state()
            progress_messages = _build_task_progress_messages(message, state, execution_ack=execution_ack, dispatch_record=dispatch_record)
            immediate_progress_count = 6 if len(progress_messages) >= 6 else len(progress_messages)
            immediate_messages = [dict(item) for item in progress_messages[:immediate_progress_count]] if progress_messages else [{"role": "copilot", "content": _compose_operator_reply(message, state), "created_at": _utc_now_iso()}]
            for item in immediate_messages:
                if str(item.get("role") or "").strip().lower() == "tod":
                    item["role"] = "copilot"
            messages.extend(immediate_messages)
            session_payload["pending_progress"] = progress_messages[immediate_progress_count:]
        else:
            messages.append({"role": "copilot", "content": _compose_operator_reply(message, state), "created_at": _utc_now_iso()})
            session_payload["pending_progress"] = []
        session_payload["messages"] = messages[-40:]
        _save_chat_session_payload(session_key, session_payload, state)
    return _build_chat_payload(session_key, messages, state, surface="chat")


@router.post("/chat/ui/handoff")
async def chat_ui_handoff(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    session_key = str(payload.get("session_key") or "copilot-operator-chat").strip() or "copilot-operator-chat"
    message = _compact_text(
        payload.get("message")
        or "Package the current issue, evidence, and next bounded repair request for Codex-style troubleshooting.",
        2000,
    )
    state = _build_tod_console_state()
    messages = _load_chat_messages(session_key, state)
    messages.append({"role": "operator", "content": message, "created_at": _utc_now_iso()})
    handoff = _create_copilot_handoff(message, state, session_key)
    messages.append(
        {
            "role": "copilot",
            "content": "\n".join(
                [
                    "Codex handoff created.",
                    f"Issue: {_pick_first_text(state.get('status', {}).get('headline') if isinstance(state.get('status'), dict) else '', state.get('status', {}).get('summary') if isinstance(state.get('status'), dict) else '')}",
                    f"Artifact: {handoff['artifact_path']}",
                    f"Dialog session: {handoff['session_id']}",
                    f"Next expected reply: {handoff['reply_contract']}",
                ]
            ),
            "created_at": _utc_now_iso(),
        }
    )
    _save_chat_messages(session_key, messages, state)
    chat_payload = _build_chat_payload(session_key, messages, state, surface="chat")
    chat_payload["handoff"] = handoff
    return chat_payload


@router.post("/chat/ui/action/training")
async def chat_ui_start_training(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    session_key = str(payload.get("session_key") or "copilot-operator-chat").strip() or "copilot-operator-chat"
    state = _build_tod_console_state()
    messages = _load_chat_messages(session_key, state)
    messages.append({"role": "operator", "content": "Start 6h Training", "created_at": _utc_now_iso()})
    result = _start_training_runbook(state)
    messages.append({"role": "copilot", "content": _format_training_start_reply(result), "created_at": _utc_now_iso()})
    _save_chat_messages(session_key, messages, state)
    chat_payload = _build_chat_payload(session_key, messages, state, surface="chat")
    chat_payload["training_action"] = result
    return chat_payload


@router.post("/operator/actions")
async def operator_actions(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    action = str(payload.get("action") or "").strip()
    if not action:
        raise HTTPException(status_code=400, detail={"error": "missing_action", "message": "Operator action is required."})
    spec = OPERATOR_ACTION_SPECS.get(action)
    if not spec:
        raise HTTPException(status_code=400, detail={"error": "unknown_action", "message": f"Unknown operator action: {action}"})
    if bool(spec.get("requires_confirmation")) and not bool(payload.get("confirm")):
        raise HTTPException(status_code=400, detail={"error": "confirmation_required", "message": str(spec.get("confirmation_text") or "Confirmation is required for this action.")})
    pre_state = _build_tod_console_state()
    objective_id, task_id = _resolve_operator_action_ids(pre_state)
    _record_operator_action(
        {
            "generated_at": _utc_now_iso(),
            "action": action,
            "label": str(spec.get("label") or action),
            "status": "started",
            "ok": True,
            "objective_id": objective_id,
            "task_id": task_id,
        }
    )
    result = _execute_operator_action(action, payload, pre_state)
    post_refresh = {}
    if action != "run_shared_truth_reconciliation":
        post_refresh = _run_reconcile_shared_truth_action()
    post_state = _build_tod_console_state()
    evidence = _write_operator_evidence_snapshot(post_state)
    action_record = {
        "generated_at": _utc_now_iso(),
        "action": action,
        "label": str(spec.get("label") or action),
        "status": str(result.get("status") or ("completed" if result.get("ok") else "failed")),
        "ok": bool(result.get("ok")),
        "message": _compact_text(result.get("message"), 220),
        "objective_id": objective_id,
        "task_id": task_id,
        "command": result.get("command") if isinstance(result.get("command"), list) else [],
        "artifact_paths": result.get("artifact_paths") if isinstance(result.get("artifact_paths"), list) else [],
        "stdout_excerpt": _trim_message_text(result.get("stdout_excerpt"), 1600),
        "stderr_excerpt": _trim_message_text(result.get("stderr_excerpt"), 1600),
        "post_refresh": post_refresh,
    }
    _record_operator_action(action_record)
    return {
        "accepted": True,
        "ok": bool(result.get("ok")),
        "action": action,
        "result": action_record,
        "shared_truth": post_state.get("shared_truth") if isinstance(post_state.get("shared_truth"), dict) else {},
        "operator_evidence": evidence,
        "operator_activity_timeline": _load_recent_operator_actions(limit=10),
    }


@router.get("/tod/ui/state")
async def tod_ui_state() -> dict[str, Any]:
    return _build_tod_console_state()


@router.get("/tod", response_class=HTMLResponse)
async def tod_console() -> HTMLResponse:
    title = f"TOD Console | {settings.app_name}"
    return HTMLResponse(
        f"""
<!doctype html>
<html lang=\"en\">
<head>
            <style>
                :root {{
                    --bg-0: #030709;
                    --bg-1: #071014;
                    --bg: #071014;
                    --ink: #d7ffe8;
                    --muted: #7dbfa1;
                    --panel: rgba(8,18,22,0.86);
                    --line: rgba(102,255,188,0.28);
                    --line-strong: rgba(102,255,188,0.70);
                    --accent: #2dff9d;
                    --accent-strong: #bfffdc;
                    --good: #2dff9d;
                    --warn: #ffd166;
                    --bad: #ff5c7a;
                    --shadow: 0 0 30px rgba(0,255,160,0.12);
                    --font: "Space Mono", "Consolas", "Cascadia Mono", monospace;
                }}
                * {{ box-sizing: border-box; }}
                body {{
                    margin: 0;
                    min-height: 100vh;
                    color: var(--ink);
                    font-family: var(--font);
                    background:
                        radial-gradient(circle at 15% 10%, rgba(40,160,90,0.22), transparent 42%),
                        radial-gradient(circle at 92% 80%, rgba(0,200,255,0.14), transparent 40%),
                        linear-gradient(160deg, var(--bg-0), var(--bg-1));
                    overflow-x: hidden;
                }}
                body::before {{
                    content: "";
                    position: fixed;
                    inset: 0;
                    pointer-events: none;
                    background-image: repeating-linear-gradient(to bottom, rgba(130,255,180,0.045), rgba(130,255,180,0.045) 1px, transparent 1px, transparent 5px);
                    opacity: 0.3;
                    animation: scan 9s linear infinite;
                }}
                @keyframes scan {{
                    from {{ transform: translateY(0); }}
                    to {{ transform: translateY(5px); }}
                }}
                .page {{ max-width: 1440px; margin: 0 auto; padding: 24px 16px 40px; }}
                .shell {{
                    border: 1px solid var(--line);
                    background: var(--panel);
                    backdrop-filter: blur(2px);
                    border-radius: 14px;
                    box-shadow: var(--shadow);
                    overflow: hidden;
                }}
                .hero {{
                    padding: 24px;
                    border-bottom: 1px solid var(--line);
                    background: linear-gradient(120deg, rgba(45,255,157,0.15), rgba(0,120,90,0.05));
                }}
                .console-nav {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }}
                .console-link {{
                    display: inline-flex;
                    align-items: center;
                    gap: 8px;
                    padding: 8px 12px;
                    border-radius: 999px;
                    border: 1px solid var(--line);
                    background: rgba(4,18,16,0.75);
                    color: #ffffff;
                    text-decoration: none;
                    font-size: 12px;
                    font-weight: 800;
                    letter-spacing: 0.08em;
                    text-transform: uppercase;
                    transition: transform 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
                }}
                .console-link span {{ color: #ffffff; }}
                .console-link:hover {{ border-color: var(--line-strong); box-shadow: 0 0 12px rgba(45,255,157,0.18); transform: translateY(-1px); }}
                .console-link.active {{ border-color: var(--line-strong); box-shadow: inset 0 0 0 1px rgba(45,255,157,0.14), 0 0 12px rgba(45,255,157,0.12); }}
                .console-link.utility {{ background: rgba(4,18,16,0.62); }}
                .console-link-light {{ width: 9px; height: 9px; border-radius: 999px; background: #4b6f62; box-shadow: 0 0 0 rgba(45,255,157,0); }}
                .console-link-light.ok {{ background: var(--good); box-shadow: 0 0 14px rgba(45,255,157,0.40); }}
                .console-link-light.err {{ background: var(--bad); box-shadow: 0 0 14px rgba(255,92,122,0.28); }}
                .settings-backdrop {{ position: fixed; inset: 0; z-index: 19; background: rgba(2,12,10,0.62); opacity: 0; pointer-events: none; transition: opacity 140ms ease; }}
                .settings-backdrop.open {{ opacity: 1; pointer-events: auto; }}
                .settings-panel {{ position: fixed; top: 56px; right: 16px; z-index: 20; width: min(340px, 92vw); border: 1px solid rgba(97,219,191,0.28); border-radius: 14px; background: rgba(3,15,13,0.97); box-shadow: 0 18px 34px rgba(0,0,0,0.38); padding: 12px; display: none; }}
                .settings-panel.open {{ display: block; }}
                .settings-header {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }}
                .settings-title {{ font-size: 13px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent-strong); }}
                .settings-close {{ width: 30px; height: 30px; border-radius: 999px; border: 1px solid rgba(97,219,191,0.28); background: rgba(4,18,16,0.78); color: var(--ink); cursor: pointer; font: inherit; font-size: 16px; line-height: 1; }}
                .settings-tabs {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 10px; }}
                .settings-tab {{ appearance: none; border: 1px solid rgba(97,219,191,0.22); border-radius: 10px; background: rgba(4,18,16,0.78); color: var(--muted); padding: 9px 10px; font: inherit; font-size: 12px; font-weight: 700; cursor: pointer; }}
                .settings-tab.active {{ border-color: rgba(45,255,157,0.55); color: var(--ink); background: rgba(8,34,30,0.88); }}
                .settings-view {{ display: none; }}
                .settings-view.active {{ display: grid; gap: 10px; }}
                .settings-row {{ display: grid; gap: 6px; }}
                .settings-row label {{ font-size: 12px; color: var(--muted); }}
                .settings-row select, .settings-row input[type="text"], .settings-row input[type="range"] {{ width: 100%; }}
                .settings-row select, .settings-row input[type="text"] {{ border-radius: 10px; border: 1px solid rgba(97,219,191,0.20); background: rgba(4,18,16,0.84); padding: 10px 12px; color: var(--ink); font: inherit; }}
                .settings-note {{ font-size: 12px; color: var(--muted); line-height: 1.45; }}
                .toggle-row {{ display: grid; grid-template-columns: auto 1fr; gap: 10px; align-items: center; }}
                .camera-preview {{ width: 100%; min-height: 180px; border-radius: 10px; border: 1px solid rgba(97,219,191,0.18); background: rgba(4,18,16,0.88); object-fit: cover; }}
                .camera-preview.inactive {{ opacity: 0.55; filter: saturate(0.6); }}
                .eyebrow {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.16em; color: var(--accent); font-weight: 700; }}
                .hero-row {{ display: flex; gap: 18px; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; }}
                h1 {{ margin: 8px 0 6px; font-size: clamp(28px, 5vw, 48px); line-height: 0.98; text-transform: uppercase; text-shadow: 0 0 12px rgba(45,255,157,0.40); }}
                .hero-copy {{ max-width: 860px; color: var(--muted); font-size: 15px; line-height: 1.5; }}
                .status-chip {{ border-radius: 999px; padding: 10px 14px; font-size: 13px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase; background: rgba(4,18,16,0.78); border: 1px solid var(--line); }}
                .status-chip[data-tone="aligned"], .status-chip[data-tone="working"], .status-chip[data-tone="complete"] {{ background: rgba(7,42,24,0.60); color: var(--good); border-color: rgba(45,255,157,0.55); }}
                .status-chip[data-tone="drifted"], .status-chip[data-tone="blocked"] {{ background: rgba(56,14,24,0.55); color: var(--bad); border-color: rgba(255,92,122,0.55); }}
                .status-chip[data-tone="authority_reset"], .status-chip[data-tone="attention"], .status-chip[data-tone="waiting"] {{ background: rgba(58,43,10,0.52); color: var(--warn); border-color: rgba(255,209,102,0.55); }}
                .status-chip[data-tone="stalled"] {{ background: rgba(56,14,24,0.55); color: var(--bad); border-color: rgba(255,92,122,0.55); }}
                .status-chip[data-tone="unknown"] {{ background: rgba(4,18,16,0.72); color: var(--muted); }}
                .headline {{ margin-top: 14px; font-size: 22px; font-weight: 800; }}
                .summary {{ margin-top: 6px; color: var(--muted); font-size: 14px; line-height: 1.5; }}
                .primary-chat-panel {{ padding: 0 24px 22px; }}
                .facts {{ display: grid; grid-template-columns: repeat(8, minmax(0, 1fr)); gap: 12px; padding: 22px 24px; border-bottom: 1px solid var(--line); }}
                .fact {{ border: 1px solid rgba(97,219,191,0.16); border-radius: 14px; background: rgba(2,12,10,0.75); padding: 14px; min-height: 108px; box-shadow: inset 0 0 0 1px rgba(120,255,190,0.06); }}
                .fact-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); }}
                .fact-value {{ margin-top: 8px; font-size: 20px; font-weight: 800; line-height: 1.15; }}
                .fact-meta {{ margin-top: 8px; color: var(--muted); font-size: 13px; line-height: 1.45; }}
                .grid {{ display: grid; grid-template-columns: minmax(0, 1.22fr) minmax(0, 0.98fr); gap: 18px; padding: 22px 24px 24px; }}
                .stack {{ display: grid; gap: 18px; }}
                .panel {{ border: 1px solid rgba(97,219,191,0.22); border-radius: 14px; background: rgba(3,15,13,0.86); padding: 18px; box-shadow: 0 0 24px rgba(45,255,157,0.08); }}
                .panel h2 {{ margin: 0 0 12px; font-size: 16px; }}
                .panel-copy {{ color: var(--muted); font-size: 14px; line-height: 1.5; }}
                .kv {{ display: grid; grid-template-columns: 180px 1fr; gap: 8px 14px; margin-top: 14px; }}
                .kv-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.10em; }}
                .kv-value {{ font-size: 14px; line-height: 1.45; word-break: break-word; }}
                .guidance-list {{ display: grid; gap: 12px; margin-top: 14px; }}
                .guidance-item {{ border: 1px solid rgba(97,219,191,0.16); border-radius: 10px; padding: 14px; background: rgba(2,12,10,0.75); }}
                .guidance-code {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--accent); font-weight: 700; }}
                .guidance-summary {{ margin-top: 6px; font-weight: 700; line-height: 1.4; }}
                .guidance-action {{ margin-top: 6px; color: var(--muted); font-size: 14px; line-height: 1.45; }}
                .training-band {{ display: grid; grid-template-columns: auto 1fr auto; gap: 12px; align-items: center; }}
                .training-pill {{ display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 999px; background: rgba(4,18,16,0.78); border: 1px solid var(--line); font-size: 12px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase; }}
                .training-pill[data-tone="running"], .training-pill[data-tone="completed"] {{ background: rgba(7,42,24,0.60); color: var(--good); border-color: rgba(45,255,157,0.55); }}
                .training-pill[data-tone="failed"], .training-pill[data-tone="error"] {{ background: rgba(56,14,24,0.55); color: var(--bad); border-color: rgba(255,92,122,0.55); }}
                .training-pill[data-tone="paused"], .training-pill[data-tone="pending"] {{ background: rgba(58,43,10,0.52); color: var(--warn); border-color: rgba(255,209,102,0.55); }}
                .training-stats {{ text-align: right; font-size: 13px; color: var(--muted); }}
                .progress-track {{ margin-top: 14px; height: 12px; border-radius: 999px; background: rgba(4,18,16,0.88); border: 1px solid rgba(97,219,191,0.18); overflow: hidden; }}
                .progress-bar {{ height: 100%; width: 0%; border-radius: 999px; background: linear-gradient(90deg, rgba(84,255,168,0.82), rgba(39,216,139,0.95)); box-shadow: 0 0 10px rgba(45,255,157,0.25); transition: width 220ms ease; }}
                .collection-list {{ display: grid; gap: 10px; margin-top: 14px; }}
                .collection-item {{ border: 1px solid rgba(97,219,191,0.16); border-radius: 10px; background: rgba(2,12,10,0.75); padding: 12px 14px; }}
                .collection-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: baseline; flex-wrap: wrap; }}
                .collection-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--accent); font-weight: 700; }}
                .collection-meta {{ font-size: 12px; color: var(--muted); }}
                .collection-text {{ margin-top: 6px; font-size: 14px; line-height: 1.45; }}
                .pill-row {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }}
                .mini-pill {{ display: inline-flex; align-items: center; gap: 6px; padding: 7px 10px; border-radius: 999px; border: 1px solid rgba(97,219,191,0.24); background: rgba(4,20,17,0.78); font-size: 12px; color: var(--muted); }}
                .chat-shell {{ display: grid; gap: 12px; }}
                .primary-chat-panel .panel {{ padding: 20px; }}
                .primary-chat-panel .chat-thread {{ min-height: 320px; max-height: 560px; }}
                .chat-meta {{ display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; font-size: 12px; color: var(--muted); }}
                .chat-thread {{ min-height: 260px; max-height: 440px; overflow-y: auto; border: 1px solid rgba(97,219,191,0.22); border-radius: 10px; padding: 14px; background: rgba(3,15,13,0.86); display: grid; gap: 12px; }}
                .chat-bubble {{ max-width: 90%; border-radius: 12px; padding: 12px 14px; border: 1px solid rgba(97,219,191,0.18); background: rgba(4,18,16,0.85); }}
                .chat-bubble.user {{ margin-left: auto; background: linear-gradient(145deg, rgba(8,34,30,0.9), rgba(4,16,14,0.95)); border-color: rgba(45,255,157,0.30); }}
                .chat-bubble.assistant {{ margin-right: auto; }}
                .chat-bubble.system {{ max-width: 100%; background: rgba(2,12,10,0.75); }}
                .chat-role {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--accent); font-weight: 700; }}
                .chat-time {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
                .chat-message {{ margin-top: 8px; font-size: 14px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }}
                .chat-form {{ display: grid; gap: 10px; position: sticky; bottom: 0; z-index: 3; padding-top: 12px; background: linear-gradient(180deg, rgba(3,15,13,0) 0%, rgba(3,15,13,0.94) 24%, rgba(3,15,13,0.98) 100%); }}
                .chat-dropzone {{ border: 1px dashed rgba(97,219,191,0.24); border-radius: 10px; padding: 10px 12px; color: var(--muted); font-size: 12px; background: rgba(3,15,13,0.72); }}
                .chat-dropzone.active {{ border-color: var(--line-strong); color: var(--ink); box-shadow: 0 0 0 1px rgba(45,255,157,0.18); }}
                .chat-preview {{ display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 12px; align-items: start; border: 1px solid rgba(97,219,191,0.18); border-radius: 10px; padding: 10px; background: rgba(3,15,13,0.76); }}
                .chat-preview[hidden] {{ display: none; }}
                .chat-preview img {{ width: 120px; max-width: 100%; border-radius: 8px; border: 1px solid rgba(97,219,191,0.16); background: rgba(4,18,16,0.88); }}
                .chat-preview-meta {{ display: grid; gap: 6px; }}
                .chat-preview-name {{ font-size: 13px; font-weight: 700; color: var(--accent-strong); }}
                .chat-preview-copy {{ color: var(--muted); font-size: 12px; line-height: 1.5; }}
                .chat-input {{ width: 100%; min-height: 108px; resize: vertical; border-radius: 10px; border: 1px solid rgba(97,219,191,0.24); background: rgba(3,14,12,0.92); padding: 14px; font: inherit; color: var(--ink); outline: none; }}
                .chat-input:focus-visible {{ border-color: var(--line-strong); box-shadow: 0 0 0 2px rgba(45,255,157,0.14); }}
                .chat-actions {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; flex-wrap: wrap; }}
                .chat-action-buttons {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
                .panel-actions {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 14px; }}
                .panel-actions[hidden] {{ display: none; }}
                .chat-activity {{ display: inline-flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 999px; border: 1px solid rgba(97,219,191,0.20); background: rgba(4,20,17,0.76); font-size: 12px; color: var(--muted); }}
                .chat-activity-dot {{ width: 10px; height: 10px; border-radius: 999px; background: #4b6f62; box-shadow: 0 0 0 rgba(45,255,157,0); }}
                .chat-activity[data-pulse="true"] {{ animation: todActivityGlow 1.55s ease-in-out infinite; border-color: rgba(97,219,191,0.38); }}
                .chat-activity[data-state="working"] .chat-activity-dot {{ background: var(--good); box-shadow: 0 0 12px rgba(45,255,157,0.42); animation: todPulse 1.1s ease-in-out infinite; }}
                .chat-activity[data-state="waiting"] .chat-activity-dot {{ background: var(--warn); box-shadow: 0 0 10px rgba(255,209,102,0.32); animation: todPulse 1.8s ease-in-out infinite; }}
                .chat-activity[data-state="stalled"] .chat-activity-dot {{ background: var(--bad); box-shadow: 0 0 12px rgba(255,92,122,0.34); animation: todPulse 0.9s ease-in-out infinite; }}
                .chat-activity[data-state="blocked"] .chat-activity-dot {{ background: var(--warn); box-shadow: 0 0 12px rgba(255,209,102,0.34); animation: todPulse 1.15s ease-in-out infinite; }}
                .chat-activity[data-state="complete"] .chat-activity-dot {{ background: var(--good); box-shadow: 0 0 10px rgba(45,255,157,0.24); }}
                .chat-activity-text {{ font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ink); }}
                @keyframes todPulse {{ 0% {{ transform: scale(0.88); opacity: 0.78; }} 50% {{ transform: scale(1.08); opacity: 1; }} 100% {{ transform: scale(0.88); opacity: 0.78; }} }}
                @keyframes todActivityGlow {{ 0% {{ box-shadow: 0 0 0 rgba(45,255,157,0.00); opacity: 0.82; }} 50% {{ box-shadow: 0 0 18px rgba(45,255,157,0.22); opacity: 1; }} 100% {{ box-shadow: 0 0 0 rgba(45,255,157,0.00); opacity: 0.82; }} }}
                .status-chip[data-active="true"] {{ animation: todBadgePulse 1.35s ease-in-out infinite; }}
                @keyframes todBadgePulse {{ 0% {{ opacity: 0.80; transform: scale(0.98); }} 50% {{ opacity: 1; transform: scale(1.02); }} 100% {{ opacity: 0.80; transform: scale(0.98); }} }}
                .chat-button {{ appearance: none; border: 1px solid var(--line); border-radius: 10px; padding: 11px 16px; background: linear-gradient(120deg, rgba(11,110,79,0.9), rgba(45,255,157,0.33)); color: #e8fff2; font: inherit; font-size: 13px; font-weight: 700; cursor: pointer; box-shadow: 0 0 14px rgba(45,255,157,0.2); transition: transform 120ms ease, background 120ms ease, box-shadow 120ms ease; }}
                .chat-button:hover {{ background: linear-gradient(120deg, rgba(0,96,81,0.65), rgba(0,140,120,0.24)); transform: translateY(-1px); }}
                .chat-button:disabled {{ cursor: wait; opacity: 0.65; transform: none; }}
                .chat-button.secondary {{ background: rgba(4,20,17,0.78); color: var(--ink); box-shadow: none; }}
                .chat-button.secondary:hover {{ background: rgba(7,28,23,0.92); }}
                .chat-quick-actions {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }}
                .chat-quick-btn {{ appearance: none; border: 1px solid rgba(97,219,191,0.28); border-radius: 999px; padding: 9px 12px; background: rgba(4,20,17,0.78); color: #d5ffea; font: inherit; font-size: 12px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; cursor: pointer; transition: transform 120ms ease, background 120ms ease, border-color 120ms ease, box-shadow 120ms ease; }}
                .chat-quick-btn:hover {{ border-color: var(--line-strong); box-shadow: 0 0 10px rgba(45,255,157,0.14); transform: translateY(-1px); }}
                .chat-quick-btn:disabled {{ cursor: wait; opacity: 0.60; transform: none; }}
                .chat-quick-copy {{ font-size: 12px; color: var(--muted); line-height: 1.45; margin-top: 2px; }}
                .status-inline {{ font-size: 12px; color: var(--muted); }}
                .muted {{ color: var(--muted); }}
                .tod-activity-strip {{ display: grid; gap: 14px; margin: 0 24px 24px; padding: 18px; border: 1px solid rgba(97,219,191,0.18); border-radius: 14px; background: rgba(3,15,13,0.78); }}
                .tod-activity-head {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; flex-wrap: wrap; }}
                .tod-activity-copy {{ display: grid; gap: 6px; min-width: 0; }}
                .tod-activity-copy strong {{ font-size: 18px; color: var(--ink); line-height: 1.35; }}
                .tod-activity-summary {{ color: var(--muted); font-size: 13px; line-height: 1.5; }}
                .tod-activity-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
                .tod-activity-card {{ border: 1px solid rgba(97,219,191,0.16); border-radius: 12px; background: rgba(2,12,10,0.72); padding: 12px; display: grid; gap: 6px; min-width: 0; }}
                .tod-activity-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.10em; color: var(--muted); }}
                .tod-activity-value {{ font-size: 15px; font-weight: 700; color: var(--ink); overflow-wrap: anywhere; word-break: break-word; }}
                .tod-activity-meta {{ font-size: 12px; color: var(--muted); line-height: 1.45; overflow-wrap: anywhere; word-break: break-word; }}
                .system-details {{ margin: 0 24px 24px; border: 1px solid rgba(97,219,191,0.18); border-radius: 14px; background: rgba(3,15,13,0.72); overflow: hidden; }}
                .system-details > summary {{ list-style: none; cursor: pointer; padding: 16px 18px; display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 13px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent-strong); background: rgba(4,20,17,0.82); }}
                .system-details > summary::-webkit-details-marker {{ display: none; }}
                .system-details > summary::after {{ content: '+'; color: var(--accent); font-size: 18px; line-height: 1; }}
                .system-details[open] > summary::after {{ content: '-'; }}
                .system-details-copy {{ color: var(--muted); font-size: 12px; font-weight: 500; letter-spacing: 0; text-transform: none; margin-left: auto; }}
                .system-details-body {{ border-top: 1px solid rgba(97,219,191,0.14); }}
                .footer {{ padding: 0 24px 24px; color: var(--muted); font-size: 12px; display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }}
                @media (max-width: 1100px) {{
                    .facts {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
                    .grid {{ grid-template-columns: 1fr; }}
                    .tod-activity-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
                }}
                @media (max-width: 720px) {{
                    .facts {{ grid-template-columns: 1fr 1fr; }}
                    .kv {{ grid-template-columns: 1fr; }}
                    .training-band {{ grid-template-columns: 1fr; }}
                    .tod-activity-grid {{ grid-template-columns: 1fr; }}
                }}
                        </style>
</head>
<body>
    <div id="todSettingsBackdrop" class="settings-backdrop" hidden></div>
    <div id="todSettingsPanel" class="settings-panel" role="dialog" aria-modal="true" aria-label="TOD settings" hidden>
        <div class="settings-header">
            <div class="settings-title">TOD Settings</div>
            <button id="todSettingsCloseBtn" class="settings-close" type="button" aria-label="Close TOD settings">×</button>
        </div>
        <div class="settings-tabs">
            <button id="todSettingsTabVoice" class="settings-tab active" type="button">Voice</button>
            <button id="todSettingsTabCamera" class="settings-tab" type="button">Camera</button>
        </div>
        <div id="todSettingsViewVoice" class="settings-view active">
            <div class="settings-row">
                <label for="todVoiceSelect">Fixed Voice</label>
                <select id="todVoiceSelect"></select>
                <div class="settings-note">This stays fixed until you change it.</div>
            </div>
            <div class="settings-row toggle-row">
                <input id="todServerTtsToggle" type="checkbox" checked />
                <label for="todServerTtsToggle">Use Neural Server TTS (recommended)</label>
            </div>
            <div class="settings-row">
                <label for="todServerTtsVoiceSelect">Neural Server Voice</label>
                <select id="todServerTtsVoiceSelect"></select>
                <div class="settings-note">Higher quality voice rendered by backend TTS.</div>
            </div>
            <div class="settings-row">
                <label for="todDefaultLang">Default Listen Language</label>
                <input id="todDefaultLang" type="text" value="en-US" placeholder="en-US" />
            </div>
            <div class="settings-row">
                <label for="todMicSelect">Microphone Input</label>
                <select id="todMicSelect"></select>
                <div class="settings-note">If you have multiple mics, choose the one TOD should use.</div>
            </div>
            <div class="settings-row toggle-row">
                <input id="todAutoLangToggle" type="checkbox" checked />
                <label for="todAutoLangToggle">Speak in detected input language</label>
            </div>
            <div class="settings-row toggle-row">
                <input id="todNaturalVoiceToggle" type="checkbox" checked />
                <label for="todNaturalVoiceToggle">Natural Voice preset (smoother)</label>
            </div>
            <div class="settings-row">
                <label for="todVoiceRate">Voice Speed (<span id="todVoiceRateValue">1.00</span>)</label>
                <input id="todVoiceRate" type="range" min="0.70" max="1.35" step="0.05" value="1.00" />
            </div>
            <div class="settings-row">
                <label for="todVoicePitch">Voice Tone (<span id="todVoicePitchValue">1.00</span>)</label>
                <input id="todVoicePitch" type="range" min="0.70" max="1.35" step="0.05" value="1.00" />
            </div>
            <div class="settings-row">
                <label for="todVoiceDepth">Voice Depth (<span id="todVoiceDepthValue">0</span>)</label>
                <input id="todVoiceDepth" type="range" min="0" max="100" step="5" value="0" />
                <div class="settings-note">Higher depth lowers perceived pitch.</div>
            </div>
            <div class="settings-row">
                <label for="todVoiceVolume">Voice Volume (<span id="todVoiceVolumeValue">1.00</span>)</label>
                <input id="todVoiceVolume" type="range" min="0.40" max="1.00" step="0.05" value="1.00" />
            </div>
        </div>
        <div id="todSettingsViewCamera" class="settings-view">
            <div class="settings-row">
                <label for="todCameraSelect">Camera Device</label>
                <select id="todCameraSelect"></select>
            </div>
            <div class="settings-row">
                <video id="todCameraPreview" class="camera-preview inactive" autoplay muted playsinline></video>
                <div id="todCameraSettingsStatus" class="settings-note">Camera preview is idle.</div>
            </div>
            <div class="settings-row">
                <button id="todCameraRefreshBtn" class="chat-button secondary" type="button">Refresh Camera List</button>
            </div>
            <div class="settings-row">
                <button id="todCameraToggleBtn" class="chat-button secondary" type="button">Start Camera Preview</button>
            </div>
            <div class="settings-note">Use this panel to verify framing and permissions for TOD camera sensing.</div>
        </div>
    </div>
  <main class=\"page\">
    <section class=\"shell\">
      <header class=\"hero\">
        <div class=\"console-nav\">
          <a class=\"console-link utility\" href=\"/\"><span>Public Home</span></a>
          <a class=\"console-link\" href=\"/mim\"><span id=\"mimConsoleLight\" class=\"console-link-light\"></span><span>MIM Coordination Console</span></a>
          <a class=\"console-link active\" href=\"/tod\"><span id=\"todConsoleLight\" class=\"console-link-light\"></span><span>TOD Execution Console</span></a>
                    <a class=\"console-link utility\" href=\"/chat\"><span>Direct Chat</span></a>
                    <button id=\"todSettingsBtn\" class=\"console-link utility\" type=\"button\"><span>Settings</span></button>
          <a class=\"console-link utility\" href=\"/mim/logout\"><span>Logout</span></a>
        </div>
                <div class=\"eyebrow\">TOD Execution Console</div>
                <div class=\"hero-row\">
                    <div id=\"buildTag\" class=\"status-chip\" data-tone=\"unknown\">UI_BUILD_ID = unified-console-recovery-v1</div>
                    <div id=\"todStatusChip\" class=\"status-chip\" data-tone=\"unknown\">Loading</div>
                </div>
                <div id=\"todStatusHeadline\" class=\"headline\">Connecting TOD teammate...</div>
                <div id=\"todStatusSummary\" class=\"summary\">Checking live teammate signals and handoff lane status.</div>
                <div id=\"chatActivityIndicator\" class=\"chat-activity\" data-state=\"idle\"><span class=\"chat-activity-dot\" aria-hidden=\"true\"></span><span id=\"chatActivityText\" class=\"chat-activity-text\">Idle</span><span id=\"chatActivitySummary\">Waiting for teammate updates.</span></div>
      </header>
            <section class=\"primary-chat-panel\">
                <section class=\"panel\">
                    <div class=\"chat-shell\">
                        <div class=\"chat-meta\"><div id=\"chatSessionMeta\">Session: loading</div></div>
                        <div id=\"chatThread\" class=\"chat-thread\"></div>
                        <form id=\"chatForm\" class=\"chat-form\">
                            <div id=\"chatDropzone\" class=\"chat-dropzone\">Paste or drop a screenshot here, or use Image to attach png, jpg, or webp before sending.</div>
                            <div id=\"chatImagePreview\" class=\"chat-preview\" hidden>
                                <img id=\"chatImagePreviewImg\" alt=\"Selected TOD screenshot preview\" />
                                <div class=\"chat-preview-meta\">
                                    <div id=\"chatImagePreviewName\" class=\"chat-preview-name\">Selected image</div>
                                    <div id=\"chatImagePreviewMeta\" class=\"chat-preview-copy\">Send adds the screenshot to this TOD thread. Send To Codex then packages the latest screenshot into the handoff.</div>
                                </div>
                            </div>
                            <textarea id=\"chatInput\" class=\"chat-input\" placeholder=\"Ask TOD about training status, progress, blockers, or next steps.\"></textarea>
                            <input id=\"chatImageUploadInput\" type=\"file\" accept=\"image/png,image/jpeg,image/webp\" hidden />
                            <div class=\"chat-actions\"><div id=\"chatStatus\" class=\"status-inline\">Waiting for TOD chat state.</div><div class=\"chat-action-buttons\"><button id=\"chatImageUploadButton\" class=\"chat-button secondary\" type=\"button\">Image</button><button id=\"chatImageRemoveButton\" class=\"chat-button secondary\" type=\"button\">Remove Image</button><button id=\"copyLastTodResponseButton\" class=\"chat-button secondary\" type=\"button\">Copy Last TOD Reply</button><button id=\"chatSendButton\" class=\"chat-button\" type=\"submit\">Send To TOD</button></div></div>
                        </form>
                    </div>
                </section>
            </section>
                <section class=\"tod-activity-strip\">
                    <div class=\"tod-activity-head\">
                        <div class=\"tod-activity-copy\">
                            <div class=\"eyebrow\">Agent Communication Status</div>
                            <strong id=\"todActivityHeadline\">Loading communication timeline...</strong>
                            <div id=\"todActivitySummary\" class=\"tod-activity-summary\">Checking request, acknowledgement, execution, and result handoff status.</div>
                        </div>
                        <div id=\"todActivityBadge\" class=\"status-chip\" data-tone=\"unknown\">Syncing</div>
                    </div>
                    <div class=\"tod-activity-grid\">
                        <article class=\"tod-activity-card\"><div class=\"tod-activity-label\">Request</div><div id=\"todActivityCurrentState\" class=\"tod-activity-value\">-</div><div id=\"todActivityCurrentMeta\" class=\"tod-activity-meta\">Waiting for the latest task request from the shared lane.</div></article>
                        <article class=\"tod-activity-card\"><div class=\"tod-activity-label\">Acknowledgement</div><div id=\"todActivityFocus\" class=\"tod-activity-value\">-</div><div id=\"todActivityFocusMeta\" class=\"tod-activity-meta\">Waiting for TOD acknowledgement and task claim details.</div></article>
                        <article class=\"tod-activity-card\"><div class=\"tod-activity-label\">Execution</div><div id=\"todActivityPhase\" class=\"tod-activity-value\">-</div><div id=\"todActivityPhaseMeta\" class=\"tod-activity-meta\">Waiting for running, blocked, or completed execution updates.</div></article>
                        <article class=\"tod-activity-card\"><div class=\"tod-activity-label\">Result Handoff</div><div id=\"todActivityStall\" class=\"tod-activity-value\">-</div><div id=\"todActivityStallMeta\" class=\"tod-activity-meta\">Waiting for result publication and consumption evidence.</div></article>
                    </div>
                </section>
                <details class=\"system-details\">
          <summary><span>Debug Details</span><span class=\"system-details-copy\">Deep diagnostics stay collapsed until needed.</span></summary>
          <div class=\"system-details-body\">
        <section class=\"facts\">
        <article class=\"fact\"><div class=\"fact-label\">Canonical Objective</div><div id=\"factCanonicalObjective\" class=\"fact-value\">-</div><div id=\"factCanonicalMeta\" class=\"fact-meta\">Waiting for MIM handshake truth.</div></article>
        <article class=\"fact\"><div class=\"fact-label\">Live Request</div><div id=\"factLiveObjective\" class=\"fact-value\">-</div><div id=\"factLiveMeta\" class=\"fact-meta\">Waiting for listener request state.</div></article>
        <article class=\"fact\"><div class=\"fact-label\">Alignment</div><div id=\"factAlignment\" class=\"fact-value\">-</div><div id=\"factAlignmentMeta\" class=\"fact-meta\">Waiting for objective comparison.</div></article>
        <article class=\"fact\"><div class=\"fact-label\">Listener State</div><div id=\"factListenerState\" class=\"fact-value\">-</div><div id=\"factListenerMeta\" class=\"fact-meta\">Waiting for execution decision.</div></article>
        <article class=\"fact\"><div id=\"factPhaseProgressLabel\" class=\"fact-label\">Phase Progress</div><div id=\"factPhaseProgress\" class=\"fact-value\">-</div><div id=\"factPhaseProgressMeta\" class=\"fact-meta\">Waiting for bounded execution progress.</div></article>
        <article class=\"fact\"><div class=\"fact-label\">Stall Watch</div><div id=\"factStallWatch\" class=\"fact-value\">-</div><div id=\"factStallWatchMeta\" class=\"fact-meta\">Waiting for execution freshness evidence.</div></article>
        <article class=\"fact\"><div class=\"fact-label\">Publish Status</div><div id=\"factPublishStatus\" class=\"fact-value\">-</div><div id=\"factPublishMeta\" class=\"fact-meta\">Waiting for mirror and upload state.</div></article>
        <article class=\"fact\"><div class=\"fact-label\">Authority Reset</div><div id=\"factAuthorityReset\" class=\"fact-value\">-</div><div id=\"factAuthorityMeta\" class=\"fact-meta\">Waiting for reset policy state.</div></article>
        <article class=\"fact\"><div class=\"fact-label\">Training State</div><div id=\"factTrainingState\" class=\"fact-value\">-</div><div id=\"factTrainingMeta\" class=\"fact-meta\">Waiting for training telemetry.</div></article>
        <article class=\"fact\"><div class=\"fact-label\">Training Progress</div><div id=\"factTrainingProgress\" class=\"fact-value\">-</div><div id=\"factTrainingProgressMeta\" class=\"fact-meta\">Waiting for runtime and ETA.</div></article>
            </section>
            <section class=\"grid\">
                <div class=\"stack\">
                    <section class=\"panel\"><h2>Objective Cards</h2><div class=\"panel-copy\">Track the active objective as a control surface with explicit plan, evidence, validation, handoff, and recovery controls.</div><div id=\"objectiveCardsList\" class=\"collection-list\"></div></section>
                    <section class=\"panel\"><h2>Operator Actions</h2><div class=\"panel-copy\">Run bounded TOD/MIM action wrappers from the browser, then inspect the refreshed evidence below.</div><div id=\"operatorActionButtons\" class=\"chat-quick-actions\"></div><div id=\"operatorActionStatus\" class=\"panel-copy\">Waiting for operator action state.</div></section>
                    <section class=\"panel\"><h2>Operator Evidence</h2><div id=\"operatorEvidenceList\" class=\"collection-list\"></div></section>
                </div>
                <div class=\"stack\">
                    <section class=\"panel\"><h2>Agent Communication Timeline</h2><div id=\"operatorTimelineList\" class=\"collection-list\"></div></section>
                </div>
            </section>
            <section class=\"grid\">
        <div class=\"stack\">
          <section class=\"panel\">
            <h2>Training Status</h2>
            <div class=\"training-band\">
              <div id=\"trainingStateBadge\" class=\"training-pill\" data-tone=\"pending\">Unknown</div>
              <div>
                <div id=\"trainingSummary\" class=\"panel-copy\">Waiting for training status.</div>
                <div id=\"trainingPhaseDetail\" class=\"summary\">No phase detail is available yet.</div>
                                <div id="trainingPolicySummary" class="summary">Waiting for idle training policy.</div>
              </div>
              <div id=\"trainingStats\" class=\"training-stats\">Runtime: -<br />ETA: -</div>
            </div>
                        <div class=\"panel-actions\"><button id=\"trainingQuickActionButton\" class=\"chat-button\" type=\"button\">Start Training</button></div>
            <div class=\"progress-track\"><div id=\"trainingProgressBar\" class=\"progress-bar\"></div></div>
            <div id=\"trainingStagePills\" class=\"pill-row\"></div>
            <div class=\"kv\">
              <div class=\"kv-label\">Phase</div><div id=\"trainingPhase\" class=\"kv-value\">-</div>
              <div class=\"kv-label\">Current Step</div><div id=\"trainingCurrentStep\" class=\"kv-value\">-</div>
              <div class=\"kv-label\">Started</div><div id=\"trainingStarted\" class=\"kv-value\">-</div>
              <div class=\"kv-label\">Updated</div><div id=\"trainingUpdated\" class=\"kv-value\">-</div>
              <div class=\"kv-label\">Expected Complete</div><div id=\"trainingExpectedCompletion\" class=\"kv-value\">-</div>
                            <div class="kv-label">Idle Policy</div><div id="trainingIdlePolicy" class="kv-value">-</div>
                            <div class="kv-label">Idle Profiles</div><div id="trainingIdleProfiles" class="kv-value">-</div>
                            <div class="kv-label">Autonomy State</div><div id="trainingAutonomyState" class="kv-value">-</div>
              <div class=\"kv-label\">Warnings</div><div id=\"trainingWarnings\" class=\"kv-value\">-</div>
              <div class=\"kv-label\">Latest Error</div><div id=\"trainingLatestError\" class=\"kv-value\">-</div>
              <div class=\"kv-label\">Latest Resolution</div><div id=\"trainingLatestResolution\" class=\"kv-value\">-</div>
              <div class=\"kv-label\">Output Dir</div><div id=\"trainingOutputDir\" class=\"kv-value\">-</div>
              <div class=\"kv-label\">Trace Path</div><div id=\"trainingTracePath\" class=\"kv-value\">-</div>
            </div>
            <div id=\"trainingEvents\" class=\"collection-list\"></div>
          </section>
          <section class=\"panel\"><h2>Operator Guidance</h2><div class=\"panel-copy\">These are the bridge-level actions TOD should trust right now, ranked by severity in the shared status artifact.</div><div id=\"guidanceList\" class=\"guidance-list\"></div></section>
          <section class=\"panel\"><h2>Publish Pipeline</h2><div id=\"publishSummary\" class=\"panel-copy\">Waiting for publish details.</div><div class=\"kv\"><div class=\"kv-label\">Mirror</div><div id=\"publishMirror\" class=\"kv-value\">-</div><div class=\"kv-label\">Remote Access</div><div id=\"publishAccess\" class=\"kv-value\">-</div><div class=\"kv-label\">Consumer</div><div id=\"publishConsumer\" class=\"kv-value\">-</div><div class=\"kv-label\">Uploaded</div><div id=\"publishTime\" class=\"kv-value\">-</div><div class=\"kv-label\">Error</div><div id=\"publishError\" class=\"kv-value\">-</div></div></section>
        </div>
        <div class=\"stack\">
                    <section class=\"panel\"><h2>Execution Lane</h2><div id=\"executionSummary\" class=\"panel-copy\">Waiting for TOD execution status.</div><div class=\"kv\"><div class=\"kv-label\">Objective</div><div id=\"executionObjective\" class=\"kv-value\">-</div><div class=\"kv-label\">Task</div><div id=\"executionTask\" class=\"kv-value\">-</div><div class=\"kv-label\">Execution State</div><div id=\"executionState\" class=\"kv-value\">-</div><div class=\"kv-label\">Waiting On</div><div id=\"executionWaitTarget\" class=\"kv-value\">-</div><div class=\"kv-label\">Wait Reason</div><div id=\"executionWaitReason\" class=\"kv-value\">-</div><div class=\"kv-label\">Current Action</div><div id=\"executionAction\" class=\"kv-value\">-</div><div class=\"kv-label\">Next Step</div><div id=\"executionNextStep\" class=\"kv-value\">-</div><div class=\"kv-label\">Next Validation</div><div id=\"executionValidation\" class=\"kv-value\">-</div><div class=\"kv-label\">Command Output</div><div id=\"executionCommandOutput\" class=\"kv-value\">-</div><div class=\"kv-label\">Files Changed</div><div id=\"executionFilesChanged\" class=\"kv-value\">-</div><div class=\"kv-label\">Matched Files</div><div id=\"executionMatchedFiles\" class=\"kv-value\">-</div><div class=\"kv-label\">Rollback</div><div id=\"executionRollback\" class=\"kv-value\">-</div><div class=\"kv-label\">Recovery</div><div id=\"executionRecovery\" class=\"kv-value\">-</div><div class=\"kv-label\">Validation Checks</div><div id=\"executionChecks\" class=\"kv-value\">-</div><div class=\"kv-label\">Updated</div><div id=\"executionUpdated\" class=\"kv-value\">-</div></div></section>
          <section class=\"panel\"><h2>Alignment Detail</h2><div id=\"alignmentSummary\" class=\"panel-copy\">Waiting for alignment evidence.</div><div id=\"alignmentQuickActionPanel\" class=\"panel-actions\" hidden><button id=\"alignmentQuickActionButton\" class=\"chat-button secondary\" type=\"button\">Resolve Drift</button></div><div class=\"kv\"><div class=\"kv-label\">TOD Objective</div><div id=\"alignmentTodObjective\" class=\"kv-value\">-</div><div class=\"kv-label\">MIM Objective</div><div id=\"alignmentMimObjective\" class=\"kv-value\">-</div><div class=\"kv-label\">Bridge Evidence</div><div id=\"alignmentEvidence\" class=\"kv-value\">-</div><div class=\"kv-label\">Failure Signals</div><div id=\"alignmentSignals\" class=\"kv-value\">-</div></div></section>
          <section class=\"panel\"><h2>Listener Decision</h2><div id=\"decisionSummary\" class=\"panel-copy\">Waiting for listener decision state.</div><div class=\"kv\"><div class=\"kv-label\">Outcome</div><div id=\"decisionOutcome\" class=\"kv-value\">-</div><div class=\"kv-label\">Reason Code</div><div id=\"decisionReason\" class=\"kv-value\">-</div><div class=\"kv-label\">Execution State</div><div id=\"decisionState\" class=\"kv-value\">-</div><div class=\"kv-label\">Next Step</div><div id=\"decisionNextStep\" class=\"kv-value\">-</div><div class=\"kv-label\">Decision Age</div><div id=\"decisionAge\" class=\"kv-value\">-</div></div></section>
                    <section class=\"panel\"><h2>Authority Reset</h2><div id=\"authoritySummary\" class=\"panel-copy\">Waiting for authority reset state.</div><div class=\"kv\"><div class=\"kv-label\">Current Objective</div><div id=\"authorityCurrent\" class=\"kv-value\">-</div><div class=\"kv-label\">Max Valid</div><div id=\"authorityMaxValid\" class=\"kv-value\">-</div><div class=\"kv-label\">Effective</div><div id=\"authorityEffective\" class=\"kv-value\">-</div><div class=\"kv-label\">Invalidated</div><div id=\"authorityInvalidated\" class=\"kv-value\">-</div></div></section>
                    <section class=\"panel\"><h2>Codex Handoffs</h2><div class=\"panel-copy\">Recent real handoffs created from this TOD console and published into the shared TOD/MIM dialog lane.</div><div class=\"panel-actions\"><button id=\"handoffQuickActionButton\" class=\"chat-button secondary\" type=\"button\">Send To Codex</button></div><div id=\"handoffList\" class=\"collection-list\"></div></section>
        </div>
            </section>
                </div>
            </details>
      <div class=\"footer\"><div id=\"footerGenerated\">Loading state timestamp...</div><div>/tod/ui/state</div></div>
    </section>
  </main>
  <script>
    const statusChip = document.getElementById('todStatusChip');
    const statusHeadline = document.getElementById('todStatusHeadline');
    const statusSummary = document.getElementById('todStatusSummary');
    const mimConsoleLight = document.getElementById('mimConsoleLight');
    const todConsoleLight = document.getElementById('todConsoleLight');
    const guidanceList = document.getElementById('guidanceList');
    const footerGenerated = document.getElementById('footerGenerated');
    const factCanonicalObjective = document.getElementById('factCanonicalObjective');
    const factCanonicalMeta = document.getElementById('factCanonicalMeta');
    const factLiveObjective = document.getElementById('factLiveObjective');
    const factLiveMeta = document.getElementById('factLiveMeta');
    const factAlignment = document.getElementById('factAlignment');
    const factAlignmentMeta = document.getElementById('factAlignmentMeta');
    const factListenerState = document.getElementById('factListenerState');
    const factListenerMeta = document.getElementById('factListenerMeta');
    const factPhaseProgressLabel = document.getElementById('factPhaseProgressLabel');
    const factPhaseProgress = document.getElementById('factPhaseProgress');
    const factPhaseProgressMeta = document.getElementById('factPhaseProgressMeta');
    const factStallWatch = document.getElementById('factStallWatch');
    const factStallWatchMeta = document.getElementById('factStallWatchMeta');
    const factPublishStatus = document.getElementById('factPublishStatus');
    const factPublishMeta = document.getElementById('factPublishMeta');
    const factAuthorityReset = document.getElementById('factAuthorityReset');
    const factAuthorityMeta = document.getElementById('factAuthorityMeta');
    const factTrainingState = document.getElementById('factTrainingState');
    const factTrainingMeta = document.getElementById('factTrainingMeta');
    const factTrainingProgress = document.getElementById('factTrainingProgress');
    const factTrainingProgressMeta = document.getElementById('factTrainingProgressMeta');
    const trainingStateBadge = document.getElementById('trainingStateBadge');
    const trainingSummary = document.getElementById('trainingSummary');
    const trainingPhaseDetail = document.getElementById('trainingPhaseDetail');
    const trainingPolicySummary = document.getElementById('trainingPolicySummary');
    const trainingStats = document.getElementById('trainingStats');
    const trainingProgressBar = document.getElementById('trainingProgressBar');
    const trainingStagePills = document.getElementById('trainingStagePills');
    const trainingPhase = document.getElementById('trainingPhase');
    const trainingCurrentStep = document.getElementById('trainingCurrentStep');
    const trainingStarted = document.getElementById('trainingStarted');
    const trainingUpdated = document.getElementById('trainingUpdated');
    const trainingExpectedCompletion = document.getElementById('trainingExpectedCompletion');
    const trainingIdlePolicy = document.getElementById('trainingIdlePolicy');
    const trainingIdleProfiles = document.getElementById('trainingIdleProfiles');
    const trainingAutonomyState = document.getElementById('trainingAutonomyState');
    const trainingWarnings = document.getElementById('trainingWarnings');
    const trainingLatestError = document.getElementById('trainingLatestError');
    const trainingLatestResolution = document.getElementById('trainingLatestResolution');
    const trainingOutputDir = document.getElementById('trainingOutputDir');
    const trainingTracePath = document.getElementById('trainingTracePath');
    const trainingEvents = document.getElementById('trainingEvents');
    const trainingQuickActionButton = document.getElementById('trainingQuickActionButton');
    const buildTagEl = document.getElementById('buildTag');
    const chatSessionMeta = document.getElementById('chatSessionMeta');
    const chatActivityIndicator = document.getElementById('chatActivityIndicator');
    const chatActivityText = document.getElementById('chatActivityText');
    const chatActivitySummary = document.getElementById('chatActivitySummary');
    const chatThread = document.getElementById('chatThread');
    const chatForm = document.getElementById('chatForm');
    const chatInput = document.getElementById('chatInput');
    const chatStatus = document.getElementById('chatStatus');
    const chatSendButton = document.getElementById('chatSendButton');
    const chatImageUploadInput = document.getElementById('chatImageUploadInput');
    const chatImageUploadButton = document.getElementById('chatImageUploadButton');
    const chatImageRemoveButton = document.getElementById('chatImageRemoveButton');
    const chatImagePreview = document.getElementById('chatImagePreview');
    const chatImagePreviewImg = document.getElementById('chatImagePreviewImg');
    const chatImagePreviewName = document.getElementById('chatImagePreviewName');
    const chatImagePreviewMeta = document.getElementById('chatImagePreviewMeta');
    const chatDropzone = document.getElementById('chatDropzone');
    const copyLastTodResponseButton = document.getElementById('copyLastTodResponseButton');
    const todSettingsBtn = document.getElementById('todSettingsBtn');
    const todSettingsBackdrop = document.getElementById('todSettingsBackdrop');
    const todSettingsPanel = document.getElementById('todSettingsPanel');
    const todSettingsCloseBtn = document.getElementById('todSettingsCloseBtn');
    const todSettingsTabVoice = document.getElementById('todSettingsTabVoice');
    const todSettingsTabCamera = document.getElementById('todSettingsTabCamera');
    const todSettingsViewVoice = document.getElementById('todSettingsViewVoice');
    const todSettingsViewCamera = document.getElementById('todSettingsViewCamera');
    const todVoiceSelect = document.getElementById('todVoiceSelect');
    const todServerTtsToggle = document.getElementById('todServerTtsToggle');
    const todServerTtsVoiceSelect = document.getElementById('todServerTtsVoiceSelect');
    const todDefaultLang = document.getElementById('todDefaultLang');
    const todMicSelect = document.getElementById('todMicSelect');
    const todAutoLangToggle = document.getElementById('todAutoLangToggle');
    const todNaturalVoiceToggle = document.getElementById('todNaturalVoiceToggle');
    const todVoiceRate = document.getElementById('todVoiceRate');
    const todVoicePitch = document.getElementById('todVoicePitch');
    const todVoiceDepth = document.getElementById('todVoiceDepth');
    const todVoiceVolume = document.getElementById('todVoiceVolume');
    const todVoiceRateValue = document.getElementById('todVoiceRateValue');
    const todVoicePitchValue = document.getElementById('todVoicePitchValue');
    const todVoiceDepthValue = document.getElementById('todVoiceDepthValue');
    const todVoiceVolumeValue = document.getElementById('todVoiceVolumeValue');
    const todCameraSelect = document.getElementById('todCameraSelect');
    const todCameraPreview = document.getElementById('todCameraPreview');
    const todCameraSettingsStatus = document.getElementById('todCameraSettingsStatus');
    const todCameraRefreshBtn = document.getElementById('todCameraRefreshBtn');
    const todCameraToggleBtn = document.getElementById('todCameraToggleBtn');
    const todActivityHeadline = document.getElementById('todActivityHeadline');
    const todActivitySummary = document.getElementById('todActivitySummary');
    const todActivityBadge = document.getElementById('todActivityBadge');
    const todActivityCurrentState = document.getElementById('todActivityCurrentState');
    const todActivityCurrentMeta = document.getElementById('todActivityCurrentMeta');
    const todActivityFocus = document.getElementById('todActivityFocus');
    const todActivityFocusMeta = document.getElementById('todActivityFocusMeta');
    const todActivityPhase = document.getElementById('todActivityPhase');
    const todActivityPhaseMeta = document.getElementById('todActivityPhaseMeta');
    const todActivityStall = document.getElementById('todActivityStall');
    const todActivityStallMeta = document.getElementById('todActivityStallMeta');
    const publishSummary = document.getElementById('publishSummary');
    const publishMirror = document.getElementById('publishMirror');
    const publishAccess = document.getElementById('publishAccess');
    const publishConsumer = document.getElementById('publishConsumer');
    const publishTime = document.getElementById('publishTime');
    const publishError = document.getElementById('publishError');
    const executionSummary = document.getElementById('executionSummary');
    const executionObjective = document.getElementById('executionObjective');
    const executionTask = document.getElementById('executionTask');
    const executionState = document.getElementById('executionState');
    const executionWaitTarget = document.getElementById('executionWaitTarget');
    const executionWaitReason = document.getElementById('executionWaitReason');
    const executionAction = document.getElementById('executionAction');
    const executionNextStep = document.getElementById('executionNextStep');
    const executionValidation = document.getElementById('executionValidation');
    const executionCommandOutput = document.getElementById('executionCommandOutput');
    const executionFilesChanged = document.getElementById('executionFilesChanged');
    const executionMatchedFiles = document.getElementById('executionMatchedFiles');
    const executionRollback = document.getElementById('executionRollback');
    const executionRecovery = document.getElementById('executionRecovery');
    const executionChecks = document.getElementById('executionChecks');
    const executionUpdated = document.getElementById('executionUpdated');
    const alignmentSummary = document.getElementById('alignmentSummary');
    const alignmentTodObjective = document.getElementById('alignmentTodObjective');
    const alignmentMimObjective = document.getElementById('alignmentMimObjective');
    const alignmentEvidence = document.getElementById('alignmentEvidence');
    const alignmentSignals = document.getElementById('alignmentSignals');
    const alignmentQuickActionPanel = document.getElementById('alignmentQuickActionPanel');
    const alignmentQuickActionButton = document.getElementById('alignmentQuickActionButton');
    const decisionSummary = document.getElementById('decisionSummary');
    const decisionOutcome = document.getElementById('decisionOutcome');
    const decisionReason = document.getElementById('decisionReason');
    const decisionState = document.getElementById('decisionState');
    const decisionNextStep = document.getElementById('decisionNextStep');
    const decisionAge = document.getElementById('decisionAge');
    const authoritySummary = document.getElementById('authoritySummary');
    const authorityCurrent = document.getElementById('authorityCurrent');
    const authorityMaxValid = document.getElementById('authorityMaxValid');
    const authorityEffective = document.getElementById('authorityEffective');
    const authorityInvalidated = document.getElementById('authorityInvalidated');
    const handoffList = document.getElementById('handoffList');
    const handoffQuickActionButton = document.getElementById('handoffQuickActionButton');
    const objectiveCardsList = document.getElementById('objectiveCardsList');
    const operatorActionButtons = document.getElementById('operatorActionButtons');
    const operatorActionStatus = document.getElementById('operatorActionStatus');
    const operatorEvidenceList = document.getElementById('operatorEvidenceList');
    const operatorTimelineList = document.getElementById('operatorTimelineList');
    const CHAT_STORAGE_KEY = 'todPublicChatSessionKeyV1';
    let latestConversation = null;
    let latestChatMessages = [];
    let latestVisitor = {{ name: 'Dave' }};
    let latestExecution = {{}};
    let latestTraining = {{}};
    let latestSessionActivity = {{}};
    let latestOperatorActions = [];
    let chatQuickActionMap = new Map();
    let currentStatusCode = 'unknown';
    let selectedComposerImage = null;
    let operatorActionInFlight = false;
    let todAvailableVoices = [];
    let todAvailableMics = [];
    let todAvailableCameras = [];
    let todCameraStream = null;
    const autoTriggeredSessions = new Set();
    function safeText(value, fallback = '-') {{ const text = String(value || '').trim(); return text || fallback; }}
    function safeJoin(values, fallback = 'None') {{ return Array.isArray(values) && values.length ? values.map((item) => safeText(item, '')).filter(Boolean).join(', ') : fallback; }}
    function plannerIsPrimary(planner) {{ return Boolean(planner && typeof planner === 'object' && planner.available && planner.is_newer_than_executor); }}
    function formatSeconds(value) {{ const numeric = Number(value); if (!Number.isFinite(numeric) || numeric < 0) return 'Unknown'; const total = Math.round(numeric); const days = Math.floor(total / 86400); const hours = Math.floor((total % 86400) / 3600); const minutes = Math.floor((total % 3600) / 60); const seconds = total % 60; const parts = []; if (days) parts.push(`${{days}}d`); if (hours || parts.length) parts.push(`${{hours}}h`); if (minutes || parts.length) parts.push(`${{minutes}}m`); if (!parts.length) parts.push(`${{seconds}}s`); return parts.join(' '); }}
    function trainingTone(training) {{ const state = safeText(training && (training.state || training.state_label), 'unknown').toLowerCase(); if (state.includes('complete')) return 'completed'; if (state.includes('run') || state.includes('active') || training && training.active) return 'running'; if (state.includes('fail') || state.includes('error')) return 'failed'; if (state.includes('pause')) return 'paused'; return 'pending'; }}
    function createChatSessionKey(defaultKey) {{ const seed = Math.random().toString(36).slice(2, 10); return `${{safeText(defaultKey, 'tod-console-public')}}-${{seed}}`; }}
    function setChatSessionKey(sessionKey) {{ try {{ if (sessionKey) window.localStorage.setItem(CHAT_STORAGE_KEY, sessionKey); }} catch (_error) {{ }} return sessionKey; }}
    function getChatSessionKey(defaultKey) {{ try {{ const existing = window.localStorage.getItem(CHAT_STORAGE_KEY); if (existing) return existing; const created = createChatSessionKey(defaultKey); window.localStorage.setItem(CHAT_STORAGE_KEY, created); return created; }} catch (_error) {{ return createChatSessionKey(defaultKey); }} }}
    function shouldRotateStaleChatSession(session, messages) {{ const activity = session && typeof session.activity === 'object' ? session.activity : {{}}; const activityState = safeText(activity.state, 'idle').toLowerCase(); const ageSeconds = Number(activity.last_activity_age_seconds); if (!Array.isArray(messages) || !messages.length) return false; if (!Number.isFinite(ageSeconds) || ageSeconds < 0) return false; if (selectedComposerImage instanceof File) return false; if (chatInput && String(chatInput.value || '').trim()) return false; if (activityState === 'stalled' && ageSeconds >= 600) return true; if (activityState === 'complete' && ageSeconds >= 1800) return true; return false; }}
    function rotateChatSession(defaultKey) {{ const sessionKey = setChatSessionKey(createChatSessionKey(defaultKey)); latestChatMessages = []; return sessionKey; }}
    function getAutoTriggerStorageKey(sessionKey, statusCode, prompt) {{ return `todAutoTrigger:${{safeText(sessionKey, 'unknown')}}:${{safeText(statusCode, 'unknown')}}:${{safeText(prompt, '').slice(0, 96)}}`; }}
    function hasAutoTriggered(storageKey) {{ try {{ return window.sessionStorage.getItem(storageKey) === '1'; }} catch (_error) {{ return autoTriggeredSessions.has(storageKey); }} }}
    function markAutoTriggered(storageKey) {{ try {{ window.sessionStorage.setItem(storageKey, '1'); }} catch (_error) {{ }} autoTriggeredSessions.add(storageKey); }}
    function clearAutoTriggered(storageKey) {{ try {{ window.sessionStorage.removeItem(storageKey); }} catch (_error) {{ }} autoTriggeredSessions.delete(storageKey); }}
    function clearNode(node) {{ while (node && node.firstChild) node.removeChild(node.firstChild); }}
    function setConsoleLight(node, ok) {{ if (!node) return; node.classList.remove('ok', 'err'); node.classList.add(ok ? 'ok' : 'err'); }}
    function appendCollectionItem(node, label, meta, text) {{ const item = document.createElement('article'); item.className = 'collection-item'; const top = document.createElement('div'); top.className = 'collection-top'; const labelNode = document.createElement('div'); labelNode.className = 'collection-label'; labelNode.textContent = safeText(label, 'Item'); const metaNode = document.createElement('div'); metaNode.className = 'collection-meta'; metaNode.textContent = safeText(meta, ''); const textNode = document.createElement('div'); textNode.className = 'collection-text'; textNode.textContent = safeText(text, 'No detail published.'); top.appendChild(labelNode); top.appendChild(metaNode); item.appendChild(top); item.appendChild(textNode); node.appendChild(item); }}
    function renderOperatorTimeline(items) {{ clearNode(operatorTimelineList); if (!Array.isArray(items) || !items.length) {{ appendCollectionItem(operatorTimelineList, 'No communication events yet', '', 'Events will appear here as request, acknowledgement, execution, and result handoff updates land.'); return; }} items.forEach((item) => {{ appendCollectionItem(operatorTimelineList, `${{safeText(item.label || item.action, 'Action')}} Ã‚Â· ${{safeText(item.status, 'unknown')}}`, safeText(item.generated_at, ''), safeText(item.message || item.stdout_excerpt, 'No action detail published.')); }}); }}
    function handleObjectiveCardAction(action) {{ if (!action || !action.id) return; const mode = safeText(action.mode, 'operator_action'); if (mode === 'operator_action') {{ runOperatorAction(action); return; }} if (mode === 'chat_handoff') {{ handleQuickAction('send-to-copilot'); return; }} if (mode === 'local_view') {{ const summary = safeText(action.plan_summary, 'No plan summary is published.'); const milestones = Array.isArray(action.milestones) && action.milestones.length ? action.milestones.map((item) => `${{safeText(item.label, 'Step')}}=${{safeText(item.status, 'unknown')}}`).join(' | ') : 'No milestones are published.'; if (operatorActionStatus) operatorActionStatus.textContent = `${{summary}} ${{milestones}}`; }} }}
    function renderObjectiveCards(cards) {{ clearNode(objectiveCardsList); const payload = Array.isArray(cards) ? cards : []; if (!payload.length) {{ appendCollectionItem(objectiveCardsList, 'No objective card', '', 'The TOD console has not published an active objective card yet.'); return; }} payload.forEach((card) => {{ const wrapper = document.createElement('article'); wrapper.className = 'collection-item'; const top = document.createElement('div'); top.className = 'collection-top'; const labelNode = document.createElement('div'); labelNode.className = 'collection-label'; labelNode.textContent = `${{safeText(card.title, 'Objective')}} Ã‚Â· ${{safeText(card.status, 'unknown')}}`; const metaNode = document.createElement('div'); metaNode.className = 'collection-meta'; metaNode.textContent = `${{safeText(card.objective_id, 'no-objective')}} Ã‚Â· task=${{safeText(card.task_id, 'n/a')}}`; top.appendChild(labelNode); top.appendChild(metaNode); wrapper.appendChild(top); const summaryNode = document.createElement('div'); summaryNode.className = 'collection-text'; summaryNode.textContent = safeText(card.summary, 'No objective summary published.'); wrapper.appendChild(summaryNode); const planner = card.planner_state && typeof card.planner_state === 'object' ? card.planner_state : {{}}; const plannerNode = document.createElement('div'); plannerNode.className = 'collection-text muted'; plannerNode.textContent = planner.available ? `Planner Ã‚Â· ${{safeText(planner.status_label, 'unknown')}} Ã‚Â· ${{safeText(planner.current_step, 'No planner step')}} Ã‚Â· next=${{safeText(planner.next_step, 'unknown')}}` : 'Planner state is not currently published.'; wrapper.appendChild(plannerNode); const executor = card.executor_state && typeof card.executor_state === 'object' ? card.executor_state : {{}}; const executorNode = document.createElement('div'); executorNode.className = 'collection-text muted'; executorNode.textContent = `Executor Ã‚Â· ${{safeText(executor.status_label || executor.status, 'unknown')}} Ã‚Â· ${{safeText(executor.current_action, safeText(executor.summary, 'No executor state published.'))}}`; wrapper.appendChild(executorNode); const progress = card.phase_progress && typeof card.phase_progress === 'object' ? card.phase_progress : {{}}; const progressNode = document.createElement('div'); progressNode.className = 'collection-text muted'; progressNode.textContent = progress.available ? `${{safeText(progress.label, 'Phase progress')}} Ã‚Â· ${{safeText(progress.percent_complete, '0')}}% Ã‚Â· next=${{safeText(progress.next_gate, 'unknown')}}` : 'No phase progress plan published.'; wrapper.appendChild(progressNode); const actionsNode = document.createElement('div'); actionsNode.className = 'chat-quick-actions'; const actions = Array.isArray(card.actions) ? card.actions : []; actions.forEach((action) => {{ const button = document.createElement('button'); button.type = 'button'; button.className = 'chat-quick-btn'; button.textContent = safeText(action.label, 'Action'); button.title = action.enabled ? safeText(action.disabled_reason || action.plan_summary, '') : safeText(action.disabled_reason, 'Unavailable'); button.disabled = operatorActionInFlight || !action.enabled; button.addEventListener('click', () => handleObjectiveCardAction(action)); actionsNode.appendChild(button); }}); wrapper.appendChild(actionsNode); const artifacts = card.artifacts && typeof card.artifacts === 'object' ? card.artifacts : {{}}; const artifactNode = document.createElement('div'); artifactNode.className = 'collection-text muted'; artifactNode.textContent = `Updated: ${{safeText(artifacts.updated_at, 'unknown')}} Ã‚Â· Files: ${{Array.isArray(artifacts.files_changed) ? artifacts.files_changed.length : 0}} Ã‚Â· Checks: ${{Array.isArray(artifacts.validation_checks) ? artifacts.validation_checks.length : 0}} Ã‚Â· Rollback: ${{safeText(artifacts.rollback_state, 'not_needed')}}`; wrapper.appendChild(artifactNode); objectiveCardsList.appendChild(wrapper); }}); }}
    function renderOperatorEvidence(evidence) {{ clearNode(operatorEvidenceList); const payload = evidence && typeof evidence === 'object' ? evidence : {{}}; const activeObjective = payload.active_objective && typeof payload.active_objective === 'object' ? payload.active_objective : {{}}; const activeTask = payload.active_task && typeof payload.active_task === 'object' ? payload.active_task : {{}}; appendCollectionItem(operatorEvidenceList, `Objective Ã‚Â· ${{safeText(activeObjective.id, 'unknown')}}`, '', safeText(activeObjective.title, 'No active objective title published.')); appendCollectionItem(operatorEvidenceList, `Task Ã‚Â· ${{safeText(activeTask.id, 'unknown')}}`, '', safeText(activeTask.title, 'No active task title published.')); appendCollectionItem(operatorEvidenceList, 'Validation', safeText(payload.validation_status, 'unknown'), safeText(payload.next_validation, 'No validation command published.')); appendCollectionItem(operatorEvidenceList, `Changed Files Ã‚Â· ${{Array.isArray(payload.changed_files) ? payload.changed_files.length : 0}}`, '', Array.isArray(payload.changed_files) && payload.changed_files.length ? payload.changed_files.join(', ') : 'No file changes were published.'); appendCollectionItem(operatorEvidenceList, `Commands Run Ã‚Â· ${{Array.isArray(payload.commands_run) ? payload.commands_run.length : 0}}`, '', Array.isArray(payload.commands_run) && payload.commands_run.length ? payload.commands_run.join(' | ') : 'No command history was published.'); appendCollectionItem(operatorEvidenceList, `Blocker Ã‚Â· ${{safeText(payload.blocker_code, 'none')}}`, '', safeText(payload.blocker_detail, 'No blocker detail published.')); const timestamps = payload.artifact_timestamps && typeof payload.artifact_timestamps === 'object' ? payload.artifact_timestamps : {{}}; appendCollectionItem(operatorEvidenceList, 'Artifact Timestamps', '', Object.keys(timestamps).length ? Object.entries(timestamps).map(([key, value]) => `${{key}}=${{safeText(value, 'unknown')}}`).join(' | ') : 'No artifact timestamps published.'); }}
    async function runOperatorAction(action) {{ if (!action || operatorActionInFlight) return; if (action.requires_confirmation && !window.confirm(safeText(action.confirmation_text, `Run ${{safeText(action.label, 'this action')}}?`))) return; operatorActionInFlight = true; if (operatorActionStatus) operatorActionStatus.textContent = `Running ${{safeText(action.label, 'operator action')}}...`; renderOperatorActions(latestOperatorActions); try {{ const response = await fetch('/operator/actions', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ action: action.id, confirm: Boolean(action.requires_confirmation) }}) }}); const payload = await response.json().catch(() => ({{}})); if (!response.ok) {{ const detail = payload && payload.detail && typeof payload.detail === 'object' ? payload.detail : {{}}; throw new Error(safeText(detail.message || payload.message, `Action failed with status ${{response.status}}`)); }} if (operatorActionStatus) operatorActionStatus.textContent = safeText(payload && payload.result && payload.result.message, `${{safeText(action.label, 'Action')}} completed.`); }} catch (error) {{ if (operatorActionStatus) operatorActionStatus.textContent = safeText(error && error.message, 'Operator action failed.'); }} finally {{ operatorActionInFlight = false; await refresh(); }} }}
    function renderOperatorActions(actions) {{ clearNode(operatorActionButtons); const payload = Array.isArray(actions) ? actions : []; latestOperatorActions = payload; if (!payload.length) {{ appendCollectionItem(operatorActionButtons, 'Operator actions unavailable', '', 'No bounded operator actions were published on this surface.'); return; }} payload.forEach((action) => {{ const button = document.createElement('button'); button.type = 'button'; button.className = 'chat-quick-btn'; button.textContent = safeText(action.label, 'Action'); button.title = action.enabled ? safeText(action.description, '') : safeText(action.disabled_reason || action.description, ''); button.disabled = operatorActionInFlight || !action.enabled; button.addEventListener('click', () => runOperatorAction(action)); operatorActionButtons.appendChild(button); }}); }}
    function setChatButtonsDisabled(disabled) {{ chatSendButton.disabled = disabled; if (chatImageUploadButton) chatImageUploadButton.disabled = disabled; if (chatImageRemoveButton) chatImageRemoveButton.disabled = disabled; if (copyLastTodResponseButton) copyLastTodResponseButton.disabled = disabled; if (trainingQuickActionButton) trainingQuickActionButton.disabled = disabled; if (alignmentQuickActionButton) alignmentQuickActionButton.disabled = disabled; if (handoffQuickActionButton) handoffQuickActionButton.disabled = disabled; }}
    function updateCopyButtonState() {{ if (!copyLastTodResponseButton) return; const hasTodReply = latestChatMessages.some((message) => messageRole(message) === 'assistant' && messageBody(message)); copyLastTodResponseButton.disabled = !hasTodReply; }}
    function messageAttachment(message) {{ return message && typeof message.attachment === 'object' ? message.attachment : null; }}
    function resetComposerImage() {{ selectedComposerImage = null; if (chatImageUploadInput) chatImageUploadInput.value = ''; if (chatImagePreviewImg) chatImagePreviewImg.removeAttribute('src'); if (chatImagePreviewName) chatImagePreviewName.textContent = 'Selected image'; if (chatImagePreviewMeta) chatImagePreviewMeta.textContent = 'Send adds the screenshot to this TOD thread. Send To Codex then packages the latest screenshot into the handoff.'; if (chatImagePreview) chatImagePreview.hidden = true; if (chatDropzone) chatDropzone.classList.remove('active'); }}
    function setComposerImage(file) {{ if (!(file instanceof File)) return false; const allowed = ['image/png', 'image/jpeg', 'image/webp']; if (!allowed.includes(String(file.type || '').toLowerCase())) {{ chatStatus.textContent = 'Only png, jpg, jpeg, and webp screenshots are supported here.'; return false; }} if (Number(file.size || 0) > 2 * 1024 * 1024) {{ chatStatus.textContent = 'Screenshots on /tod must be 2 MB or smaller.'; return false; }} selectedComposerImage = file; if (chatImagePreviewName) chatImagePreviewName.textContent = file.name || 'Selected image'; if (chatImagePreviewMeta) chatImagePreviewMeta.textContent = `${{Math.max(1, Math.round((Number(file.size || 0)) / 1024))}} KB Ã‚Â· ${{safeText(file.type, 'image file')}}`; if (chatImagePreviewImg) {{ const previewUrl = URL.createObjectURL(file); chatImagePreviewImg.src = previewUrl; }} if (chatImagePreview) chatImagePreview.hidden = false; chatStatus.textContent = 'Screenshot attached. Add an optional note and send, or use Send To Codex after it lands in the thread.'; return true; }}
    function fileToDataUrl(file) {{ return new Promise((resolve, reject) => {{ const reader = new FileReader(); reader.onload = () => resolve(String(reader.result || '')); reader.onerror = () => reject(reader.error || new Error('file_read_failed')); reader.readAsDataURL(file); }}); }}
    function todSettingKey(name) {{ return `todConsoleSetting:${{name}}`; }}
    function loadTodSetting(name, fallback) {{ try {{ const value = window.localStorage.getItem(todSettingKey(name)); return value == null ? fallback : value; }} catch (_error) {{ return fallback; }} }}
    function saveTodSetting(name, value) {{ try {{ window.localStorage.setItem(todSettingKey(name), String(value)); }} catch (_error) {{ }} }}
    function openTodSettingsPanel() {{ if (!todSettingsPanel) return; todSettingsPanel.hidden = false; todSettingsPanel.classList.add('open'); if (todSettingsBackdrop) {{ todSettingsBackdrop.hidden = false; todSettingsBackdrop.classList.add('open'); }} }}
    function closeTodSettingsPanel() {{ if (!todSettingsPanel) return; todSettingsPanel.classList.remove('open'); todSettingsPanel.hidden = true; if (todSettingsBackdrop) {{ todSettingsBackdrop.classList.remove('open'); todSettingsBackdrop.hidden = true; }} }}
    function toggleTodSettingsPanel() {{ if (!todSettingsPanel) return; if (todSettingsPanel.classList.contains('open')) {{ closeTodSettingsPanel(); return; }} openTodSettingsPanel(); }}
    function setTodSettingsTab(tabName) {{ const isCamera = String(tabName || '').toLowerCase() === 'camera'; if (todSettingsTabVoice) todSettingsTabVoice.classList.toggle('active', !isCamera); if (todSettingsTabCamera) todSettingsTabCamera.classList.toggle('active', isCamera); if (todSettingsViewVoice) todSettingsViewVoice.classList.toggle('active', !isCamera); if (todSettingsViewCamera) todSettingsViewCamera.classList.toggle('active', isCamera); }}
    function updateTodVoiceUi() {{ if (todVoiceRateValue && todVoiceRate) todVoiceRateValue.textContent = Number(todVoiceRate.value || 1).toFixed(2); if (todVoicePitchValue && todVoicePitch) todVoicePitchValue.textContent = Number(todVoicePitch.value || 1).toFixed(2); if (todVoiceDepthValue && todVoiceDepth) todVoiceDepthValue.textContent = `${{Math.round(Number(todVoiceDepth.value || 0))}}`; if (todVoiceVolumeValue && todVoiceVolume) todVoiceVolumeValue.textContent = Number(todVoiceVolume.value || 1).toFixed(2); }}
    function applyTodVoiceSettings() {{ updateTodVoiceUi(); if (todVoiceSelect) saveTodSetting('voice_uri', todVoiceSelect.value || ''); if (todServerTtsToggle) saveTodSetting('server_tts_enabled', todServerTtsToggle.checked ? '1' : '0'); if (todServerTtsVoiceSelect) saveTodSetting('server_tts_voice', todServerTtsVoiceSelect.value || ''); if (todDefaultLang) saveTodSetting('default_lang', todDefaultLang.value || 'en-US'); if (todMicSelect) saveTodSetting('mic_device_id', todMicSelect.value || ''); if (todAutoLangToggle) saveTodSetting('auto_lang', todAutoLangToggle.checked ? '1' : '0'); if (todNaturalVoiceToggle) saveTodSetting('natural_voice', todNaturalVoiceToggle.checked ? '1' : '0'); if (todVoiceRate) saveTodSetting('voice_rate', todVoiceRate.value || '1.00'); if (todVoicePitch) saveTodSetting('voice_pitch', todVoicePitch.value || '1.00'); if (todVoiceDepth) saveTodSetting('voice_depth', todVoiceDepth.value || '0'); if (todVoiceVolume) saveTodSetting('voice_volume', todVoiceVolume.value || '1.00'); }}
    function populateTodServerTtsVoices() {{ if (!todServerTtsVoiceSelect) return; const voices = [['alloy', 'Alloy'], ['ash', 'Ash'], ['sage', 'Sage'], ['verse', 'Verse']]; todServerTtsVoiceSelect.innerHTML = ''; voices.forEach((entry) => {{ const option = document.createElement('option'); option.value = entry[0]; option.textContent = entry[1]; todServerTtsVoiceSelect.appendChild(option); }}); todServerTtsVoiceSelect.value = loadTodSetting('server_tts_voice', 'alloy'); }}
    function populateTodVoices() {{ if (!todVoiceSelect) return; const synth = window.speechSynthesis; todAvailableVoices = synth && typeof synth.getVoices === 'function' ? (synth.getVoices() || []) : []; todVoiceSelect.innerHTML = ''; if (!todAvailableVoices.length) {{ const option = document.createElement('option'); option.value = ''; option.textContent = 'Browser voices unavailable'; todVoiceSelect.appendChild(option); return; }} todAvailableVoices.forEach((voice) => {{ const option = document.createElement('option'); option.value = safeText(voice.voiceURI || voice.name, ''); option.textContent = `${{safeText(voice.name, 'Voice')}}${{voice.lang ? ` · ${{voice.lang}}` : ''}}`; todVoiceSelect.appendChild(option); }}); const stored = loadTodSetting('voice_uri', todAvailableVoices[0] && (todAvailableVoices[0].voiceURI || todAvailableVoices[0].name) || ''); todVoiceSelect.value = stored; if (!todVoiceSelect.value && todAvailableVoices[0]) todVoiceSelect.value = safeText(todAvailableVoices[0].voiceURI || todAvailableVoices[0].name, ''); }}
    async function enumerateTodMicDevices() {{ if (!todMicSelect) return; if (!(navigator.mediaDevices && navigator.mediaDevices.enumerateDevices)) {{ todMicSelect.innerHTML = '<option value="">Media devices unavailable</option>'; return; }} const devices = await navigator.mediaDevices.enumerateDevices(); todAvailableMics = devices.filter((device) => device.kind === 'audioinput'); todMicSelect.innerHTML = ''; if (!todAvailableMics.length) {{ todMicSelect.innerHTML = '<option value="">No microphone detected</option>'; return; }} todAvailableMics.forEach((device, index) => {{ const option = document.createElement('option'); option.value = safeText(device.deviceId, ''); option.textContent = safeText(device.label, `Microphone ${{index + 1}}`); todMicSelect.appendChild(option); }}); const stored = loadTodSetting('mic_device_id', todAvailableMics[0].deviceId || ''); todMicSelect.value = stored; if (!todMicSelect.value && todAvailableMics[0]) todMicSelect.value = todAvailableMics[0].deviceId || ''; applyTodVoiceSettings(); }}
    async function enumerateTodCameraDevices() {{ if (!todCameraSelect) return; if (!(navigator.mediaDevices && navigator.mediaDevices.enumerateDevices)) {{ todCameraSelect.innerHTML = '<option value="">Media devices unavailable</option>'; if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = 'Camera controls are unavailable in this browser.'; return; }} const devices = await navigator.mediaDevices.enumerateDevices(); todAvailableCameras = devices.filter((device) => device.kind === 'videoinput'); todCameraSelect.innerHTML = ''; if (!todAvailableCameras.length) {{ todCameraSelect.innerHTML = '<option value="">No camera detected</option>'; if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = 'No camera detected.'; return; }} todAvailableCameras.forEach((device, index) => {{ const option = document.createElement('option'); option.value = safeText(device.deviceId, ''); option.textContent = safeText(device.label, `Camera ${{index + 1}}`); todCameraSelect.appendChild(option); }}); const stored = loadTodSetting('camera_device_id', todAvailableCameras[0].deviceId || ''); todCameraSelect.value = stored; if (!todCameraSelect.value && todAvailableCameras[0]) todCameraSelect.value = todAvailableCameras[0].deviceId || ''; saveTodSetting('camera_device_id', todCameraSelect.value || ''); }}
    function stopTodCameraPreview() {{ if (todCameraStream && typeof todCameraStream.getTracks === 'function') {{ todCameraStream.getTracks().forEach((track) => track.stop()); }} todCameraStream = null; if (todCameraPreview) {{ todCameraPreview.srcObject = null; todCameraPreview.classList.add('inactive'); }} if (todCameraToggleBtn) todCameraToggleBtn.textContent = 'Start Camera Preview'; }}
    async function startTodCameraPreview() {{ if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {{ if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = 'Camera preview is unavailable in this browser.'; return; }} stopTodCameraPreview(); const constraints = {{ video: todCameraSelect && todCameraSelect.value ? {{ deviceId: {{ exact: todCameraSelect.value }} }} : true, audio: false }}; todCameraStream = await navigator.mediaDevices.getUserMedia(constraints); if (todCameraPreview) {{ todCameraPreview.srcObject = todCameraStream; todCameraPreview.classList.remove('inactive'); }} if (todCameraToggleBtn) todCameraToggleBtn.textContent = 'Stop Camera Preview'; if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = 'Camera preview is live.'; if (todCameraSelect) saveTodSetting('camera_device_id', todCameraSelect.value || ''); }}
    function initializeTodSettings() {{ if (todServerTtsToggle) todServerTtsToggle.checked = loadTodSetting('server_tts_enabled', '1') === '1'; if (todDefaultLang) todDefaultLang.value = loadTodSetting('default_lang', 'en-US'); if (todAutoLangToggle) todAutoLangToggle.checked = loadTodSetting('auto_lang', '1') === '1'; if (todNaturalVoiceToggle) todNaturalVoiceToggle.checked = loadTodSetting('natural_voice', '1') === '1'; if (todVoiceRate) todVoiceRate.value = loadTodSetting('voice_rate', '1.00'); if (todVoicePitch) todVoicePitch.value = loadTodSetting('voice_pitch', '1.00'); if (todVoiceDepth) todVoiceDepth.value = loadTodSetting('voice_depth', '0'); if (todVoiceVolume) todVoiceVolume.value = loadTodSetting('voice_volume', '1.00'); populateTodServerTtsVoices(); populateTodVoices(); updateTodVoiceUi(); enumerateTodMicDevices().catch((_error) => {{ if (todMicSelect) todMicSelect.innerHTML = '<option value="">Unable to enumerate microphones</option>'; }}); enumerateTodCameraDevices().catch((_error) => {{ if (todCameraSelect) todCameraSelect.innerHTML = '<option value="">Unable to enumerate cameras</option>'; }}); }}
    function renderGuidance(items) {{ clearNode(guidanceList); if (!Array.isArray(items) || !items.length) {{ appendCollectionItem(guidanceList, 'Guidance', '', 'No operator guidance is currently published.'); return; }} items.forEach((item) => {{ const card = document.createElement('article'); card.className = 'guidance-item'; const code = document.createElement('div'); code.className = 'guidance-code'; code.textContent = `${{safeText(item.severity, 'info')}} Ã‚Â· ${{safeText(item.code, 'guidance')}}`; const summary = document.createElement('div'); summary.className = 'guidance-summary'; summary.textContent = safeText(item.summary, 'No summary'); const action = document.createElement('div'); action.className = 'guidance-action'; action.textContent = safeText(item.recommended_action, 'No action published'); card.appendChild(code); card.appendChild(summary); card.appendChild(action); guidanceList.appendChild(card); }}); }}
    function renderHandoffs(items) {{ clearNode(handoffList); if (!Array.isArray(items) || !items.length) {{ appendCollectionItem(handoffList, 'No recent handoffs', '', 'Send To Codex will create a dialog session and it will appear here after publication.'); return; }} items.forEach((item) => {{ const card = document.createElement('article'); card.className = 'collection-item'; const top = document.createElement('div'); top.className = 'collection-top'; const labelNode = document.createElement('div'); labelNode.className = 'collection-label'; labelNode.textContent = `${{safeText(item.status_label, 'Unknown')}} Ã‚Â· ${{safeText(item.session_id, 'unknown session')}}`; const metaNode = document.createElement('div'); metaNode.className = 'collection-meta'; metaNode.textContent = `${{safeText(item.updated_age, 'Unknown')}} Ã‚Â· messages=${{safeText(item.message_count, '0')}}`; const summaryNode = document.createElement('div'); summaryNode.className = 'collection-text'; summaryNode.textContent = safeText(item.issue_summary, 'No issue summary published.'); const idsNode = document.createElement('div'); idsNode.className = 'collection-text muted'; idsNode.textContent = `request=${{safeText(item.request_id, 'n/a')}} Ã‚Â· task=${{safeText(item.task_id, 'n/a')}} Ã‚Â· objective=${{safeText(item.objective_id, 'n/a')}}`; const detailNode = document.createElement('div'); detailNode.className = 'collection-text muted'; detailNode.textContent = `Last: ${{safeText(item.last_message_from, 'unknown')}}/${{safeText(item.last_message_type, 'unknown')}} Ã‚Â· Artifact: ${{safeText(item.copilot_artifact_path, 'not published')}}`; top.appendChild(labelNode); top.appendChild(metaNode); card.appendChild(top); card.appendChild(summaryNode); card.appendChild(idsNode); card.appendChild(detailNode); if (item.bounded_repair_request) {{ const repairNode = document.createElement('div'); repairNode.className = 'collection-text'; repairNode.textContent = `Repair: ${{safeText(item.bounded_repair_request)}}`; card.appendChild(repairNode); }} if (item.next_validation) {{ const validationNode = document.createElement('div'); validationNode.className = 'collection-text'; validationNode.textContent = `Validation: ${{safeText(item.next_validation)}}`; card.appendChild(validationNode); }} handoffList.appendChild(card); }}); }}
    function renderTraining(training) {{ const payload = training && typeof training === 'object' ? training : {{}}; latestTraining = payload; const available = Boolean(payload.available); const percent = Math.max(0, Math.min(100, Number(payload.percent_complete || 0))); const runtimeText = formatSeconds(payload.runtime_seconds); const etaText = payload.eta_seconds == null ? 'Unknown' : formatSeconds(payload.eta_seconds); const idlePolicy = payload.idle_policy && typeof payload.idle_policy === 'object' ? payload.idle_policy : {{}}; factTrainingState.textContent = safeText(payload.state_label || payload.state, 'Unknown'); factTrainingMeta.textContent = available ? `${{safeText(payload.summary, 'No training summary')}} Ã‚Â· ${{safeText(idlePolicy.policy_summary, 'Idle training policy not published.')}}` : 'No training telemetry is published.'; factTrainingProgress.textContent = available ? `${{Math.round(percent)}}%` : 'Unknown'; factTrainingProgressMeta.textContent = available ? `Runtime: ${{runtimeText}} Ã‚Â· ETA: ${{etaText}}` : 'No runtime estimate is available.'; trainingStateBadge.textContent = safeText(payload.state_label || payload.state, 'Unknown'); trainingStateBadge.dataset.tone = trainingTone(payload); trainingSummary.textContent = safeText(payload.summary, 'No training summary is available.'); trainingPhaseDetail.textContent = safeText(payload.phase_detail, 'No phase detail is available yet.'); trainingPolicySummary.textContent = safeText(idlePolicy.policy_summary, 'Idle training policy not published.'); trainingStats.innerHTML = `Runtime: ${{runtimeText}}<br />ETA: ${{etaText}}`; trainingProgressBar.style.width = `${{percent}}%`; trainingPhase.textContent = safeText(payload.phase_label || payload.phase, 'Unknown'); trainingCurrentStep.textContent = safeText(payload.current_step, 'Not published'); trainingStarted.textContent = payload.started_at ? `${{safeText(payload.started_at)}} Ã‚Â· ${{safeText(payload.started_age, 'Unknown')}}` : 'Unknown'; trainingUpdated.textContent = payload.updated_at ? `${{safeText(payload.updated_at)}} Ã‚Â· ${{safeText(payload.updated_age, 'Unknown')}}` : 'Unknown'; trainingExpectedCompletion.textContent = payload.expected_completion_utc ? safeText(payload.expected_completion_utc) : 'Unknown'; trainingIdlePolicy.textContent = idlePolicy.continuous_idle_enabled ? `Always train when idle Ã‚Â· threshold ${{safeText(idlePolicy.idle_threshold_minutes, '0')}}m` : 'Disabled'; trainingIdleProfiles.textContent = `Short < ${{safeText(idlePolicy.long_idle_profile_threshold_minutes, '30')}}m: ${{safeText(idlePolicy.short_idle_profile_label, 'Runtime-safe validation subset')}} Ã‚Â· Long >= ${{safeText(idlePolicy.long_idle_profile_threshold_minutes, '30')}}m: ${{safeText(idlePolicy.long_idle_profile_label, 'Repo edit / test / recover pack')}}`; trainingAutonomyState.textContent = `${{safeText(idlePolicy.current_tod_state, 'unknown')}} Ã‚Â· ${{safeText(idlePolicy.activity_summary, 'No autonomy activity is published.')}}`; trainingWarnings.textContent = safeJoin(payload.warnings, 'None'); trainingLatestError.textContent = payload.latest_error ? `${{safeText(payload.latest_error)}}${{payload.latest_error_at ? ` Ã‚Â· ${{safeText(payload.latest_error_at)}}` : ''}}` : 'None'; trainingLatestResolution.textContent = payload.latest_resolution ? `${{safeText(payload.latest_resolution)}}${{payload.latest_resolution_at ? ` Ã‚Â· ${{safeText(payload.latest_resolution_at)}}` : ''}}` : 'None'; trainingOutputDir.textContent = safeText(payload.artifacts && payload.artifacts.output_dir, 'Not published'); trainingTracePath.textContent = safeText(payload.artifacts && payload.artifacts.trace_path, 'Not published'); clearNode(trainingStagePills); if (Array.isArray(payload.stages) && payload.stages.length) {{ payload.stages.forEach((stage) => {{ const pill = document.createElement('div'); pill.className = 'mini-pill'; pill.textContent = `${{safeText(stage.label, 'Stage')}}: ${{safeText(stage.status, 'unknown')}}`; trainingStagePills.appendChild(pill); }}); }} else {{ const pill = document.createElement('div'); pill.className = 'mini-pill'; pill.textContent = 'No stage telemetry'; trainingStagePills.appendChild(pill); }} clearNode(trainingEvents); if (Array.isArray(payload.recent_events) && payload.recent_events.length) {{ payload.recent_events.forEach((item) => appendCollectionItem(trainingEvents, safeText(item.type, 'event'), safeText(item.generated_age || item.generated_at, ''), safeText(item.summary, 'No event summary'))); }} else if (Array.isArray(payload.resolutions) && payload.resolutions.length) {{ payload.resolutions.forEach((item) => appendCollectionItem(trainingEvents, 'Resolution', '', item)); }} else {{ appendCollectionItem(trainingEvents, 'Training Feed', '', 'No training events are currently published.'); }} }}
    function messageRole(message) {{ const role = safeText(message && (message.role || message.actor || message.source || message.type), 'message').toLowerCase(); if (role.includes('visitor') || role.includes('user')) return 'user'; if (role.includes('tod') || role.includes('assistant') || role.includes('reply')) return 'assistant'; return 'system'; }}
    function messageLabel(message, role) {{ if (role === 'user') return safeText(message && message.author_name, safeText(latestVisitor && latestVisitor.name, 'Dave')); if (role === 'assistant') return 'TOD'; return 'TOD Activity'; }}
    function messageBody(message) {{ return safeText(message && (message.content || message.message || message.text || message.body || message.summary), ''); }}
    function getLastTodExchange(messages) {{ if (!Array.isArray(messages) || !messages.length) return null; for (let todIndex = messages.length - 1; todIndex >= 0; todIndex -= 1) {{ const todMessage = messages[todIndex]; if (messageRole(todMessage) !== 'assistant' || !messageBody(todMessage)) continue; let userMessage = null; for (let userIndex = todIndex - 1; userIndex >= 0; userIndex -= 1) {{ const candidate = messages[userIndex]; if (messageRole(candidate) === 'user' && messageBody(candidate)) {{ userMessage = candidate; break; }} }} return {{ user: userMessage, tod: todMessage }}; }} return null; }}
    function buildLastTodExchangeCopy(messages) {{ const exchange = getLastTodExchange(messages); if (!exchange || !exchange.tod) return ''; const lines = []; if (exchange.user && messageBody(exchange.user)) {{ lines.push('User action:'); lines.push(messageBody(exchange.user)); lines.push(''); }} lines.push('TOD response:'); lines.push(messageBody(exchange.tod)); return lines.join('\\n'); }}
    async function copyTextToClipboard(value) {{ const text = String(value || ''); if (!text.trim()) return false; if (navigator.clipboard && navigator.clipboard.writeText) {{ await navigator.clipboard.writeText(text); return true; }} const textArea = document.createElement('textarea'); textArea.value = text; textArea.setAttribute('readonly', 'readonly'); textArea.style.position = 'fixed'; textArea.style.opacity = '0'; textArea.style.pointerEvents = 'none'; document.body.appendChild(textArea); textArea.focus(); textArea.select(); const copied = document.execCommand('copy'); document.body.removeChild(textArea); return copied; }}
    async function handleCopyLastTodResponse() {{ const transcript = buildLastTodExchangeCopy(latestChatMessages); if (!transcript) {{ chatStatus.textContent = 'No TOD reply is available to copy yet.'; return; }} try {{ copyLastTodResponseButton.disabled = true; await copyTextToClipboard(transcript); chatStatus.textContent = 'Copied the last user action and TOD reply.'; }} catch (error) {{ chatStatus.textContent = `Copy failed: ${{safeText(error && error.message, 'clipboard unavailable')}}`; }} finally {{ updateCopyButtonState(); }} }}
    function renderQuickActions(conversation) {{ chatQuickActionMap = new Map(); const actions = conversation && Array.isArray(conversation.quick_actions) ? conversation.quick_actions : []; actions.forEach((action) => {{ const prompt = safeText(action && action.prompt, ''); const id = safeText(action && action.id, 'quick-action'); const label = safeText(action && action.label, 'Quick Action'); const description = safeText(action && action.description, ''); const actionType = safeText(action && action.action_type, 'prompt'); chatQuickActionMap.set(id, {{ prompt, label, description, actionType }}); }}); const trainingAction = chatQuickActionMap.get('start-training'); if (trainingQuickActionButton) {{ trainingQuickActionButton.textContent = safeText(trainingAction && trainingAction.label, 'Start Training'); trainingQuickActionButton.title = safeText(trainingAction && trainingAction.description, 'Start the bounded training request.'); trainingQuickActionButton.hidden = !trainingAction; }} const driftAction = chatQuickActionMap.get('resolve-drift'); if (alignmentQuickActionButton) {{ alignmentQuickActionButton.textContent = safeText(driftAction && driftAction.label, 'Resolve Drift'); alignmentQuickActionButton.title = safeText(driftAction && driftAction.description, 'Send a bounded drift resolution request.'); alignmentQuickActionButton.hidden = !driftAction; }} const handoffAction = chatQuickActionMap.get('send-to-copilot'); if (handoffQuickActionButton) {{ handoffQuickActionButton.textContent = safeText(handoffAction && handoffAction.label, 'Send To Codex'); handoffQuickActionButton.title = safeText(handoffAction && handoffAction.description, 'Create a Codex handoff from the current TOD thread.'); handoffQuickActionButton.hidden = !handoffAction; }} }}
    function renderExecution(execution) {{ const payload = execution && typeof execution === 'object' ? execution : {{}}; latestExecution = payload; const planner = payload.planner_state && typeof payload.planner_state === 'object' ? payload.planner_state : {{}}; const usePlanner = plannerIsPrimary(planner); executionSummary.textContent = usePlanner ? safeText(planner.summary, 'No planner summary published.') : safeText(payload.summary, 'No TOD execution activity is currently published.'); executionObjective.textContent = usePlanner ? safeText(planner.objective_id || payload.objective_id, 'Unknown') : safeText(payload.objective_id, 'Unknown'); executionTask.textContent = usePlanner ? safeText(planner.title || payload.title || payload.task_focus || planner.task_id, 'No active task') : safeText(payload.title || payload.task_focus || payload.task_id, 'No active task'); executionState.textContent = usePlanner ? safeText(planner.status_label || planner.status, 'Idle') : safeText(payload.activity_label || payload.execution_state || payload.status, 'Idle'); executionWaitTarget.textContent = usePlanner ? safeText(planner.assigned_executor || payload.wait_target_label, 'Not waiting on an external dependency.') : safeText(payload.wait_target_label, 'Not waiting on an external dependency.'); executionWaitReason.textContent = usePlanner ? safeText(planner.summary, 'No specific wait reason published.') : safeText(payload.wait_reason, 'No specific wait reason published.'); executionAction.textContent = usePlanner ? safeText(planner.current_step, 'No current action published.') : safeText(payload.current_action, 'No current action published.'); executionNextStep.textContent = usePlanner ? safeText(planner.next_step, 'No next step published.') : safeText(payload.next_step, 'No next step published.'); executionValidation.textContent = safeText(payload.next_validation || planner.requested_outcome || payload.validation_summary, 'No validation target published.'); executionCommandOutput.textContent = safeText(payload.command_output, 'No command output published.'); executionFilesChanged.textContent = safeJoin(payload.files_changed, 'None'); executionMatchedFiles.textContent = safeJoin(payload.matched_files, 'None'); executionRollback.textContent = safeText(payload.rollback_state, 'not_needed'); executionRecovery.textContent = safeText(payload.recovery_state, 'not_needed'); const checks = Array.isArray(payload.validation_checks) ? payload.validation_checks.map((item) => item && typeof item === 'object' ? `${{safeText(item.name, 'check')}}=${{item.passed ? 'passed' : 'failed'}}` : '').filter(Boolean) : []; executionChecks.textContent = checks.length ? checks.join(', ') : 'None'; executionUpdated.textContent = usePlanner ? `${{safeText(planner.updated_at, 'Unknown')}} Ã‚Â· ${{safeText(planner.updated_age, 'Unknown')}}` : payload.updated_at ? `${{safeText(payload.updated_at)}} Ã‚Â· ${{safeText(payload.updated_age, 'Unknown')}}` : 'Unknown'; }}
    function renderPrimaryStatus(status, execution) {{ const statusPayload = status && typeof status === 'object' ? status : {{}}; const executionPayload = execution && typeof execution === 'object' ? execution : {{}}; const trainingPayload = latestTraining && typeof latestTraining === 'object' ? latestTraining : {{}}; const planner = executionPayload.planner_state && typeof executionPayload.planner_state === 'object' ? executionPayload.planner_state : {{}}; const usePlanner = plannerIsPrimary(planner); const trainingActive = Boolean(trainingPayload.available) && Boolean(trainingPayload.active); if (trainingActive) {{ const trainingState = safeText(trainingPayload.state_label || trainingPayload.state, 'Training Active'); const trainingSummary = safeText(trainingPayload.summary, 'TOD training is active.'); const trainingStep = safeText(trainingPayload.current_step, 'Current step not published.'); const executionSlice = Boolean(executionPayload.available) ? safeText(executionPayload.summary, '') : ''; statusChip.textContent = trainingState.toUpperCase(); statusChip.dataset.tone = safeText(trainingTone(trainingPayload), 'pending').toLowerCase(); statusChip.dataset.active = 'true'; statusHeadline.textContent = 'TOD training is active'; statusSummary.textContent = safeText([trainingSummary, `Current step: ${{trainingStep}}.`, executionSlice ? `Latest execution slice: ${{executionSlice}}` : ''].filter(Boolean).join(' '), 'TOD training is active.'); return; }} if (usePlanner) {{ statusChip.textContent = safeText(planner.status_label, 'QUEUED').toUpperCase(); statusChip.dataset.tone = safeText(planner.status, 'waiting').toLowerCase(); statusChip.dataset.active = 'true'; statusHeadline.textContent = `TOD execution teammate is ${{safeText(planner.status_label, 'queued').toLowerCase()}}`; statusSummary.textContent = safeText([safeText(planner.summary, ''), `Current step: ${{safeText(planner.current_step, 'Not published')}}.`, `Next: ${{safeText(planner.next_step, 'Wait for fresh execution evidence.')}}`].filter(Boolean).join(' '), 'A fresher TOD request is waiting for execution evidence.'); return; }} const statusCode = safeText(statusPayload.code, 'unknown').toLowerCase(); const sharedTruthPrimary = ['blocked_with_reason', 'accepted_complete', 'accepted_complete_pending_mim_refresh', 'replay_or_replan_required', 'disagreement', 'stale'].includes(statusCode); const executionState = safeText(executionPayload.activity_state, 'idle').toLowerCase(); const useExecution = Boolean(executionPayload.available) && ['working', 'waiting', 'complete', 'stalled', 'paused', 'blocked'].includes(executionState) && (!sharedTruthPrimary || Boolean(executionPayload.shared_truth_superseded) || executionState !== 'idle'); if (useExecution) {{ const phaseProgress = executionPayload.phase_progress && typeof executionPayload.phase_progress === 'object' ? executionPayload.phase_progress : {{}}; const stallSignal = executionPayload.stall_signal && typeof executionPayload.stall_signal === 'object' ? executionPayload.stall_signal : {{}}; const stallLevel = safeText(stallSignal.level, 'ok').toLowerCase(); const phaseLabel = safeText(phaseProgress.label, 'Phase progress'); const phaseSummary = Boolean(phaseProgress.available) ? `${{phaseLabel}} ${{Math.max(0, Math.min(100, Number(phaseProgress.percent_complete || 0)))}}% complete; next gate ${{safeText(phaseProgress.next_gate, 'Unknown')}}.` : ''; const stallSummary = safeText(stallSignal.summary, '') || (executionPayload.available ? 'Stall watch clear.' : ''); const disagreementSummary = sharedTruthPrimary ? safeText(statusPayload.summary, '') : ''; const activityState = safeText(executionPayload.activity_state, 'unknown').toLowerCase(); const activityLabel = safeText(executionPayload.activity_label, 'UNKNOWN'); statusChip.textContent = activityLabel.toUpperCase(); statusChip.dataset.tone = activityState; statusChip.dataset.active = !['idle', 'complete'].includes(activityState) ? 'true' : 'false'; statusHeadline.textContent = activityState === 'complete' ? 'Latest TOD execution handoff is complete' : `TOD execution teammate is ${{activityLabel.toLowerCase()}}`; statusSummary.textContent = safeText([safeText(executionPayload.activity_summary || executionPayload.summary, ''), phaseSummary, stallSummary, disagreementSummary].filter(Boolean).join(' '), 'No shared TOD execution summary is available.'); return; }} statusChip.textContent = safeText(statusPayload.label, 'UNKNOWN'); statusChip.dataset.tone = safeText(statusPayload.code, 'unknown').toLowerCase(); statusChip.dataset.active = 'false'; statusHeadline.textContent = safeText(statusPayload.headline, 'TOD state unavailable'); statusSummary.textContent = safeText(statusPayload.summary, 'No shared TOD summary is available.'); }}
    function renderTopActivity() {{ const execution = latestExecution && typeof latestExecution === 'object' ? latestExecution : {{}}; const planner = execution.planner_state && typeof execution.planner_state === 'object' ? execution.planner_state : {{}}; const trainingPayload = latestTraining && typeof latestTraining === 'object' ? latestTraining : {{}}; const sessionActivity = latestSessionActivity && typeof latestSessionActivity === 'object' ? latestSessionActivity : {{}}; const trainingActive = Boolean(trainingPayload.available) && Boolean(trainingPayload.active); const usePlanner = !trainingActive && plannerIsPrimary(planner); const executionState = safeText(execution.activity_state, 'idle').toLowerCase(); const useExecution = !trainingActive && !usePlanner && Boolean(execution.available) && ['working', 'waiting', 'stalled', 'blocked', 'complete', 'paused'].includes(executionState); const state = trainingActive ? 'working' : usePlanner ? safeText(planner.status, 'waiting').toLowerCase() : useExecution ? executionState : safeText(sessionActivity.state, 'idle').toLowerCase(); const pulse = Boolean(trainingActive || usePlanner || (useExecution && !['idle', 'complete'].includes(executionState))); const label = pulse ? 'TOD Activity' : safeText(sessionActivity.label, 'Idle'); const summary = trainingActive ? safeText([safeText(trainingPayload.summary, ''), safeText(trainingPayload.current_step, '')].filter(Boolean).join(' Ã‚Â· '), 'TOD training is active.') : usePlanner ? safeText([safeText(planner.summary, ''), safeText(planner.current_step, '')].filter(Boolean).join(' Ã‚Â· '), 'Waiting for TOD activity.') : useExecution ? safeText(`${{safeText(execution.activity_label, 'Active')}}: ${{safeText(execution.activity_summary || execution.summary, 'Waiting for TOD activity.')}}`, 'Waiting for TOD activity.') : safeText(sessionActivity.summary, 'Waiting for TOD activity.'); const ageText = trainingActive ? (trainingPayload.updated_at ? ` Ã‚Â· updated ${{safeText(trainingPayload.updated_age, 'Unknown')}}` : '') : usePlanner ? (planner.updated_at ? ` Ã‚Â· updated ${{safeText(planner.updated_age, 'Unknown')}}` : '') : (() => {{ const ageSeconds = useExecution ? Number(execution.last_update_age_seconds) : Number(sessionActivity.last_activity_age_seconds); return Number.isFinite(ageSeconds) && ageSeconds >= 0 ? ` Ã‚Â· last update ${{formatSeconds(ageSeconds)}} ago` : ''; }})(); if (chatActivityIndicator) {{ chatActivityIndicator.dataset.state = state; chatActivityIndicator.dataset.pulse = pulse ? 'true' : 'false'; }} if (chatActivityText) chatActivityText.textContent = label; if (chatActivitySummary) chatActivitySummary.textContent = `${{summary}}${{ageText}}`; }}
    function renderTodActivityStrip(status, execution) {{ const statusPayload = status && typeof status === 'object' ? status : {{}}; const executionPayload = execution && typeof execution === 'object' ? execution : {{}}; const trainingPayload = latestTraining && typeof latestTraining === 'object' ? latestTraining : {{}}; const planner = executionPayload.planner_state && typeof executionPayload.planner_state === 'object' ? executionPayload.planner_state : {{}}; const usePlanner = plannerIsPrimary(planner); const trainingActive = Boolean(trainingPayload.available) && Boolean(trainingPayload.active); const statusCode = safeText(statusPayload.code, 'unknown').toLowerCase(); const sharedTruthPrimary = ['blocked_with_reason', 'accepted_complete', 'accepted_complete_pending_mim_refresh', 'replay_or_replan_required', 'disagreement', 'stale'].includes(statusCode); const executionState = safeText(executionPayload.activity_state, 'idle').toLowerCase(); const useExecution = Boolean(executionPayload.available) && ['working', 'waiting', 'complete', 'stalled', 'paused', 'blocked'].includes(executionState) && (!sharedTruthPrimary || Boolean(executionPayload.shared_truth_superseded) || executionState !== 'idle'); const phaseProgress = executionPayload.phase_progress && typeof executionPayload.phase_progress === 'object' ? executionPayload.phase_progress : {{}}; const stallSignal = executionPayload.stall_signal && typeof executionPayload.stall_signal === 'object' ? executionPayload.stall_signal : {{}}; const badgeTone = trainingActive ? safeText(trainingTone(trainingPayload), 'pending').toLowerCase() : usePlanner ? safeText(planner.status, 'waiting').toLowerCase() : useExecution ? executionState : safeText(statusPayload.code, 'unknown').toLowerCase(); const headline = trainingActive ? 'TOD training is active' : usePlanner ? `TOD planner is ${{safeText(planner.status_label, 'queued').toLowerCase()}}` : useExecution ? safeText(executionPayload.activity_summary || executionPayload.summary, 'TOD execution activity is published.') : safeText(statusPayload.headline, 'TOD state unavailable'); const summary = trainingActive ? safeText(trainingPayload.summary, 'TOD training is active.') : usePlanner ? safeText([safeText(planner.summary, ''), `Current step: ${{safeText(planner.current_step, 'Not published')}}.`].filter(Boolean).join(' '), 'Waiting for fresh execution evidence.') : useExecution ? safeText([safeText(executionPayload.activity_summary || executionPayload.summary, ''), Boolean(phaseProgress.available) ? `${{safeText(phaseProgress.label, 'Phase progress')}} ${{Math.max(0, Math.min(100, Number(phaseProgress.percent_complete || 0)))}}%.` : '', stallSignal.summary ? safeText(stallSignal.summary, '') : '', sharedTruthPrimary ? safeText(statusPayload.summary, '') : ''].filter(Boolean).join(' '), 'Execution state is available.') : safeText(statusPayload.summary, 'No shared TOD summary is available.'); if (todActivityHeadline) todActivityHeadline.textContent = headline; if (todActivitySummary) todActivitySummary.textContent = summary; if (todActivityBadge) {{ todActivityBadge.textContent = trainingActive ? safeText(trainingPayload.state_label || trainingPayload.state, 'Training') : usePlanner ? safeText(planner.status_label, 'Queued') : useExecution ? 'TOD Activity' : safeText(statusPayload.label, 'Unknown'); todActivityBadge.dataset.tone = badgeTone; todActivityBadge.dataset.active = (trainingActive || usePlanner || (useExecution && !['idle', 'complete'].includes(executionState))) ? 'true' : 'false'; }} if (todActivityCurrentState) todActivityCurrentState.textContent = trainingActive ? safeText(trainingPayload.state_label || trainingPayload.state, 'Training') : usePlanner ? safeText(planner.status_label, 'Queued') : useExecution ? safeText(executionPayload.activity_label, 'Idle') : safeText(statusPayload.label, 'Unknown'); if (todActivityCurrentMeta) todActivityCurrentMeta.textContent = trainingActive ? safeText(trainingPayload.current_step, 'Current training step not published.') : usePlanner ? safeText(planner.current_step, 'Planner step not published.') : useExecution ? safeText(executionPayload.current_action || executionPayload.wait_reason, 'No current action published.') : safeText(statusPayload.summary, 'No status detail published.'); if (todActivityFocus) todActivityFocus.textContent = trainingActive ? safeText(executionPayload.title || executionPayload.task_focus || executionPayload.task_id, 'Training') : usePlanner ? safeText(planner.title || planner.task_id, 'No active task') : safeText(executionPayload.title || executionPayload.task_focus || executionPayload.task_id || executionPayload.objective_id, 'No active focus'); if (todActivityFocusMeta) todActivityFocusMeta.textContent = trainingActive ? safeText(trainingPayload.phase_detail, 'Training detail not published.') : usePlanner ? `Objective: ${{safeText(planner.objective_id || executionPayload.objective_id, 'Unknown')}}` : `Objective: ${{safeText(executionPayload.objective_id, 'Unknown')}}`; if (todActivityPhase) todActivityPhase.textContent = Boolean(phaseProgress.available) ? `${{Math.max(0, Math.min(100, Number(phaseProgress.percent_complete || 0)))}}%` : trainingActive ? safeText(trainingPayload.phase_label || trainingPayload.phase, 'Training') : 'Unknown'; if (todActivityPhaseMeta) todActivityPhaseMeta.textContent = Boolean(phaseProgress.available) ? safeText(phaseProgress.summary || `Next gate: ${{safeText(phaseProgress.next_gate, 'Unknown')}}`, 'No phase progress summary.') : trainingActive ? safeText(trainingPayload.summary, 'Training phase detail unavailable.') : 'Waiting for bounded execution progress.'; if (todActivityStall) todActivityStall.textContent = stallSignal.flagged ? 'Probable stall' : safeText(stallSignal.level, 'clear').replaceAll('_', ' '); if (todActivityStallMeta) todActivityStallMeta.textContent = stallSignal.summary ? safeText(stallSignal.summary, '') : useExecution ? `Last update: ${{formatSeconds(executionPayload.last_update_age_seconds)}} ago.` : safeText(statusPayload.summary, 'Waiting for execution freshness evidence.'); }}
    function snapshotChatScroll() {{ if (!chatThread) return {{ hadMessages: false, atBottom: true, top: 0 }}; const maxTop = Math.max(0, chatThread.scrollHeight - chatThread.clientHeight); const top = Number(chatThread.scrollTop || 0); return {{ hadMessages: chatThread.childElementCount > 0, atBottom: maxTop - top <= 48, top }}; }}
    function isSyntheticExecutionOnlyThread(messages) {{ if (!Array.isArray(messages) || !messages.length) return false; const hasUserMessages = messages.some((message) => messageRole(message) === 'user'); const firstBody = messageBody(messages[0]); return !hasUserMessages && (firstBody.startsWith('Live execution feed:') || firstBody.startsWith('TOD is ')); }}
    function restoreChatScroll(snapshot, messages) {{ if (!chatThread) return; if (!snapshot || !snapshot.hadMessages) {{ chatThread.scrollTop = isSyntheticExecutionOnlyThread(messages) ? 0 : chatThread.scrollHeight; return; }} if (snapshot.atBottom) {{ chatThread.scrollTop = chatThread.scrollHeight; return; }} chatThread.scrollTop = snapshot.top; }}
    function renderChatState(data) {{ const session = data && typeof data.session === 'object' ? data.session : {{}}; const messages = Array.isArray(data && data.messages) ? data.messages : []; latestChatMessages = messages; const visitor = data && typeof data.visitor === 'object' ? data.visitor : {{}}; latestVisitor = visitor; const sessionKey = safeText(session.session_key || getChatSessionKey(latestConversation && latestConversation.default_session_key), 'unknown'); if (shouldRotateStaleChatSession(session, messages)) {{ rotateChatSession(latestConversation && latestConversation.default_session_key); chatStatus.textContent = 'Started a fresh TOD chat session because the previous thread was stale.'; clearNode(chatThread); appendCollectionItem(chatThread, 'TOD Chat', '', 'Started a fresh TOD session because the previous thread was stale. Ask for current status, next steps, or send a new request.'); renderChatActivity({{ activity: {{ state: 'idle', label: 'Fresh Session', summary: 'Started a fresh TOD session after the previous thread went stale.' }} }}); updateCopyButtonState(); setTimeout(() => {{ refreshChatState().catch((error) => {{ chatStatus.textContent = safeText(error && error.message, 'Unable to refresh fresh TOD session.'); }}); }}, 0); return; }} chatSessionMeta.textContent = `Session: ${{sessionKey}} Ã‚Â· User: ${{safeText(visitor.name, 'Dave')}}`; clearNode(chatThread); if (!messages.length) {{ appendCollectionItem(chatThread, 'TOD Chat', '', 'No TOD messages are in this session yet. Ask for status, blockers, training progress, execution work, or attach a screenshot.'); }} else {{ messages.forEach((message) => {{ const bubble = document.createElement('article'); const role = messageRole(message); bubble.className = `chat-bubble ${{role}}`; const roleNode = document.createElement('div'); roleNode.className = 'chat-role'; roleNode.textContent = messageLabel(message, role); const timeNode = document.createElement('div'); timeNode.className = 'chat-time'; timeNode.textContent = safeText(message.created_at || message.generated_at || message.timestamp, ''); const contentNode = document.createElement('div'); contentNode.className = 'chat-message'; contentNode.textContent = messageBody(message) || 'No message content'; bubble.appendChild(roleNode); if (timeNode.textContent) bubble.appendChild(timeNode); bubble.appendChild(contentNode); const attachment = messageAttachment(message); if (attachment && attachment.url) {{ const previewImg = document.createElement('img'); previewImg.src = safeText(attachment.thumbnail_url || attachment.url, ''); previewImg.alt = safeText(attachment.filename || 'Attached screenshot', 'Attached screenshot'); previewImg.style.marginTop = '10px'; previewImg.style.maxWidth = '320px'; previewImg.style.width = '100%'; previewImg.style.borderRadius = '10px'; previewImg.style.border = '1px solid rgba(97,219,191,0.18)'; previewImg.style.background = 'rgba(3,15,13,0.82)'; const attachmentMeta = document.createElement('div'); attachmentMeta.className = 'chat-time'; attachmentMeta.textContent = `${{safeText(attachment.filename, 'image')}} Ã‚Â· ${{Math.max(1, Math.round(Number(attachment.size_bytes || 0) / 1024))}} KB`; bubble.appendChild(previewImg); bubble.appendChild(attachmentMeta); }} chatThread.appendChild(bubble); }}); }} chatThread.scrollTop = chatThread.scrollHeight; updateCopyButtonState(); chatStatus.textContent = visitor.memory_summary ? safeText(visitor.memory_summary) : 'TOD operator chat is ready.'; renderChatActivity(session); const autoTrigger = latestConversation && typeof latestConversation.auto_trigger === 'object' ? latestConversation.auto_trigger : null; const enabled = Boolean(autoTrigger && autoTrigger.enabled); const statusCodes = autoTrigger && Array.isArray(autoTrigger.status_codes) ? autoTrigger.status_codes.map((value) => safeText(value, '').toLowerCase()).filter(Boolean) : []; const autoPrompt = autoTrigger ? safeText(autoTrigger.prompt, '') : ''; const shouldAutoTrigger = enabled && autoPrompt && statusCodes.includes(safeText(currentStatusCode, 'unknown').toLowerCase()) && messages.length === 0; if (shouldAutoTrigger) {{ const storageKey = getAutoTriggerStorageKey(sessionKey, currentStatusCode, autoPrompt); if (!hasAutoTriggered(storageKey)) {{ markAutoTriggered(storageKey); sendChatPrompt(autoPrompt, safeText(autoTrigger && autoTrigger.success_text, 'TOD auto-resolution request sent.')).then((ok) => {{ if (!ok) clearAutoTriggered(storageKey); }}); }} }} }}
    function renderChatState(data) {{ const session = data && typeof data.session === 'object' ? data.session : {{}}; const messages = Array.isArray(data && data.messages) ? data.messages : []; latestChatMessages = messages; const visitor = data && typeof data.visitor === 'object' ? data.visitor : {{}}; latestVisitor = visitor; latestSessionActivity = session && typeof session.activity === 'object' ? session.activity : {{}}; const sessionKey = safeText(session.session_key || getChatSessionKey(latestConversation && latestConversation.default_session_key), 'unknown'); if (shouldRotateStaleChatSession(session, messages)) {{ rotateChatSession(latestConversation && latestConversation.default_session_key); chatStatus.textContent = 'Started a fresh TOD chat session because the previous thread was stale.'; clearNode(chatThread); appendCollectionItem(chatThread, 'TOD Chat', '', 'Started a fresh TOD session because the previous thread was stale. Ask for current status, next steps, or send a new request.'); latestSessionActivity = {{ state: 'idle', label: 'Fresh Session', summary: 'Started a fresh TOD session after the previous thread went stale.' }}; renderTopActivity(); updateCopyButtonState(); setTimeout(() => {{ refreshChatState().catch((error) => {{ chatStatus.textContent = safeText(error && error.message, 'Unable to refresh fresh TOD session.'); }}); }}, 0); return; }} chatSessionMeta.textContent = `Session: ${{sessionKey}} Ã‚Â· User: ${{safeText(visitor.name, 'Dave')}}`; const scrollSnapshot = snapshotChatScroll(); clearNode(chatThread); if (!messages.length) {{ appendCollectionItem(chatThread, 'TOD Chat', '', 'No TOD messages are in this session yet. Ask for status, blockers, training progress, execution work, or attach a screenshot.'); }} else {{ messages.forEach((message) => {{ const bubble = document.createElement('article'); const role = messageRole(message); bubble.className = `chat-bubble ${{role}}`; const roleNode = document.createElement('div'); roleNode.className = 'chat-role'; roleNode.textContent = messageLabel(message, role); const timeNode = document.createElement('div'); timeNode.className = 'chat-time'; timeNode.textContent = safeText(message.created_at || message.generated_at || message.timestamp, ''); const contentNode = document.createElement('div'); contentNode.className = 'chat-message'; contentNode.textContent = messageBody(message) || 'No message content'; bubble.appendChild(roleNode); if (timeNode.textContent) bubble.appendChild(timeNode); bubble.appendChild(contentNode); const attachment = messageAttachment(message); if (attachment && attachment.url) {{ const previewImg = document.createElement('img'); previewImg.src = safeText(attachment.thumbnail_url || attachment.url, ''); previewImg.alt = safeText(attachment.filename || 'Attached screenshot', 'Attached screenshot'); previewImg.style.marginTop = '10px'; previewImg.style.maxWidth = '320px'; previewImg.style.width = '100%'; previewImg.style.borderRadius = '10px'; previewImg.style.border = '1px solid rgba(97,219,191,0.18)'; previewImg.style.background = 'rgba(3,15,13,0.82)'; const attachmentMeta = document.createElement('div'); attachmentMeta.className = 'chat-time'; attachmentMeta.textContent = `${{safeText(attachment.filename, 'image')}} Ã‚Â· ${{Math.max(1, Math.round(Number(attachment.size_bytes || 0) / 1024))}} KB`; bubble.appendChild(previewImg); bubble.appendChild(attachmentMeta); }} chatThread.appendChild(bubble); }}); }} restoreChatScroll(scrollSnapshot, messages); updateCopyButtonState(); chatStatus.textContent = visitor.memory_summary ? safeText(visitor.memory_summary) : 'TOD operator chat is ready.'; renderTopActivity(); const autoTrigger = latestConversation && typeof latestConversation.auto_trigger === 'object' ? latestConversation.auto_trigger : null; const enabled = Boolean(autoTrigger && autoTrigger.enabled); const statusCodes = autoTrigger && Array.isArray(autoTrigger.status_codes) ? autoTrigger.status_codes.map((value) => safeText(value, '').toLowerCase()).filter(Boolean) : []; const autoPrompt = autoTrigger ? safeText(autoTrigger.prompt, '') : ''; const shouldAutoTrigger = enabled && autoPrompt && statusCodes.includes(safeText(currentStatusCode, 'unknown').toLowerCase()) && messages.length === 0; if (shouldAutoTrigger) {{ const storageKey = getAutoTriggerStorageKey(sessionKey, currentStatusCode, autoPrompt); if (!hasAutoTriggered(storageKey)) {{ markAutoTriggered(storageKey); sendChatPrompt(autoPrompt, safeText(autoTrigger && autoTrigger.success_text, 'TOD auto-resolution request sent.')).then((ok) => {{ if (!ok) clearAutoTriggered(storageKey); }}); }} }} }}
    async function refreshChatState() {{ if (!latestConversation || !latestConversation.enabled) {{ chatStatus.textContent = 'TOD operator chat is disabled on this surface.'; return; }} const sessionKey = getChatSessionKey(latestConversation.default_session_key); const url = `${{safeText(latestConversation.state_url, '/tod/ui/chat/state')}}?session_key=${{encodeURIComponent(sessionKey)}}&mode=${{encodeURIComponent(safeText(latestConversation.mode, 'tod'))}}`; const response = await fetch(url, {{ cache: 'no-store' }}); if (!response.ok) throw new Error(`chat-state-${{response.status}}`); const data = await response.json(); renderChatState(data); }}
    async function sendChatPrompt(message, successText) {{ if (!latestConversation || !latestConversation.enabled) {{ chatStatus.textContent = 'TOD chat is unavailable.'; return false; }} const trimmedMessage = String(message || '').trim(); if (!trimmedMessage) {{ chatStatus.textContent = 'Enter a message for TOD first.'; return false; }} setChatButtonsDisabled(true); chatStatus.textContent = 'Sending to TOD...'; try {{ const response = await fetch(safeText(latestConversation.message_url, '/tod/ui/chat/message'), {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ message: trimmedMessage, mode: safeText(latestConversation.mode, 'tod'), session_key: getChatSessionKey(latestConversation.default_session_key) }}) }}); if (!response.ok) throw new Error(`chat-send-${{response.status}}`); chatInput.value = ''; await refreshChatState(); chatStatus.textContent = safeText(successText, 'TOD replied on this operator channel.'); return true; }} catch (error) {{ chatStatus.textContent = `TOD chat failed: ${{safeText(error && error.message, 'unknown error')}}`; return false; }} finally {{ setChatButtonsDisabled(false); }} }}
    async function uploadComposerImage() {{ if (!(selectedComposerImage instanceof File)) return false; if (!latestConversation || !latestConversation.enabled) {{ chatStatus.textContent = 'TOD image upload is unavailable.'; return false; }} const uploadUrl = safeText((latestConversation.actions && latestConversation.actions.upload_url) || latestConversation.upload_url, '/tod/ui/chat/upload-image'); setChatButtonsDisabled(true); chatStatus.textContent = 'Uploading screenshot to TOD...'; try {{ const dataUrl = await fileToDataUrl(selectedComposerImage); const response = await fetch(uploadUrl, {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ session_key: getChatSessionKey(latestConversation.default_session_key), mode: safeText(latestConversation.mode, 'tod'), prompt: String(chatInput && chatInput.value || '').trim(), attachment: {{ filename: selectedComposerImage.name || 'shared-image', mime_type: selectedComposerImage.type || 'image/png', size_bytes: Number(selectedComposerImage.size || 0), data_url: dataUrl }} }}) }}); if (!response.ok) throw new Error(`chat-upload-${{response.status}}`); const data = await response.json(); renderChatState(data); if (chatInput) chatInput.value = ''; resetComposerImage(); chatStatus.textContent = 'Screenshot attached to the TOD thread. Use Send To Codex to package it for deeper review.'; return true; }} catch (error) {{ chatStatus.textContent = `TOD image upload failed: ${{safeText(error && error.message, 'unknown error')}}`; return false; }} finally {{ setChatButtonsDisabled(false); }} }}
    async function createCopilotHandoff(message, successText) {{ if (!latestConversation || !latestConversation.enabled) {{ chatStatus.textContent = 'TOD handoff is unavailable.'; return false; }} const trimmedMessage = String(message || '').trim(); if (!trimmedMessage) {{ chatStatus.textContent = 'Enter a handoff request first.'; return false; }} setChatButtonsDisabled(true); chatStatus.textContent = 'Creating Codex handoff...'; try {{ const response = await fetch(safeText(latestConversation.handoff_url, '/tod/ui/chat/handoff'), {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ message: trimmedMessage, mode: safeText(latestConversation.mode, 'tod'), session_key: getChatSessionKey(latestConversation.default_session_key) }}) }}); if (!response.ok) throw new Error(`handoff-send-${{response.status}}`); const data = await response.json(); renderChatState(data); chatInput.value = ''; const handoff = data && typeof data.handoff === 'object' ? data.handoff : null; chatStatus.textContent = handoff && handoff.session_id ? `${{safeText(successText, 'Codex handoff created.')}} Session: ${{safeText(handoff.session_id)}} Ã‚Â· Codex receives the current request, strongest evidence, next validation target, and the latest screenshot from this thread when present.` : safeText(successText, 'Codex handoff created.'); return true; }} catch (error) {{ chatStatus.textContent = `TOD handoff failed: ${{safeText(error && error.message, 'unknown error')}}`; return false; }} finally {{ setChatButtonsDisabled(false); }} }}
    async function sendChatMessage(event) {{ event.preventDefault(); if (selectedComposerImage instanceof File) {{ await uploadComposerImage(); return; }} await sendChatPrompt(chatInput.value, 'TOD replied on this operator channel.'); }}
    async function handleQuickAction(actionId) {{ const action = chatQuickActionMap.get(String(actionId || '')); if (!action || !action.prompt) {{ chatStatus.textContent = 'That TOD quick action is not available right now.'; return; }} if (chatInput) chatInput.value = action.prompt; if (safeText(action.actionType, 'prompt') === 'handoff') {{ await createCopilotHandoff(action.prompt, `${{safeText(action.label, 'Quick action')}} created a Codex handoff.`); return; }} await sendChatPrompt(action.prompt, `${{safeText(action.label, 'Quick action')}} sent to TOD.`); }}
    function renderState(data) {{ const status = data && typeof data.status === 'object' ? data.status : {{}}; const quickFacts = data && typeof data.quick_facts === 'object' ? data.quick_facts : {{}}; const execution = data && typeof data.execution === 'object' ? data.execution : {{}}; const training = data && typeof data.training_status === 'object' ? data.training_status : {{}}; const objectiveCards = Array.isArray(data && data.objective_cards) ? data.objective_cards : []; const phaseProgress = execution.phase_progress && typeof execution.phase_progress === 'object' ? execution.phase_progress : {{}}; const stallSignal = execution.stall_signal && typeof execution.stall_signal === 'object' ? execution.stall_signal : {{}}; const stallLevel = safeText(stallSignal.level, 'ok').toLowerCase(); const alignment = data && typeof data.objective_alignment === 'object' ? data.objective_alignment : {{}}; const evidence = data && typeof data.bridge_canonical_evidence === 'object' ? data.bridge_canonical_evidence : {{}}; const liveTask = data && typeof data.live_task_request === 'object' ? data.live_task_request : {{}}; const decision = data && typeof data.listener_decision === 'object' ? data.listener_decision : {{}}; const publish = data && typeof data.publish === 'object' ? data.publish : {{}}; const authority = data && typeof data.authority_reset === 'object' ? data.authority_reset : {{}}; latestConversation = data && typeof data.conversation === 'object' ? data.conversation : null; currentStatusCode = safeText(status.code, 'unknown').toLowerCase(); if (buildTagEl) buildTagEl.textContent = `UI_BUILD_ID = ${{safeText(data.runtime_build, 'unified-console-recovery-v1')}}`; const sharedTruthPrimary = ['blocked_with_reason', 'accepted_complete', 'accepted_complete_pending_mim_refresh', 'replay_or_replan_required', 'disagreement', 'stale'].includes(currentStatusCode); renderQuickActions(latestConversation); renderExecution(execution); renderTraining(training); renderTopActivity(); renderPrimaryStatus(status, execution); renderTodActivityStrip(status, execution); renderObjectiveCards(objectiveCards); renderOperatorActions(data.operator_actions || []); renderOperatorTimeline(data.operator_activity_timeline || []); renderOperatorEvidence(data.operator_evidence || {{}}); if (alignmentQuickActionPanel) alignmentQuickActionPanel.hidden = ['aligned'].includes(safeText(status.code, 'unknown').toLowerCase()); setConsoleLight(todConsoleLight, ['aligned'].includes(safeText(status.code, 'unknown').toLowerCase())); setConsoleLight(mimConsoleLight, Boolean(data.mim_status && data.mim_status.available)); factCanonicalObjective.textContent = safeText(quickFacts.canonical_objective, 'Unknown'); factCanonicalMeta.textContent = safeText(data.mim_status && data.mim_status.generated_age, 'Unknown'); factLiveObjective.textContent = safeText(quickFacts.live_request_objective, 'Unknown'); factLiveMeta.textContent = `Request age: ${{safeText(liveTask.generated_age, 'Unknown')}}`; factAlignment.textContent = safeText(alignment.status, 'unknown').replaceAll('_', ' '); factAlignmentMeta.textContent = safeText(alignment.summary, 'No alignment summary'); factListenerState.textContent = safeText(quickFacts.listener_state, 'unknown'); factListenerMeta.textContent = safeText(decision.summary, 'No listener decision summary'); if (factPhaseProgressLabel) factPhaseProgressLabel.textContent = Boolean(phaseProgress.available) && !sharedTruthPrimary ? safeText(phaseProgress.label, 'Phase Progress') : 'Phase Progress'; factPhaseProgress.textContent = Boolean(phaseProgress.available) && !sharedTruthPrimary ? `${{Math.max(0, Math.min(100, Number(phaseProgress.percent_complete || 0)))}}%` : 'Unknown'; factPhaseProgressMeta.textContent = Boolean(phaseProgress.available) && !sharedTruthPrimary ? safeText(phaseProgress.summary, 'No phase progress summary.') : safeText(status.summary, 'Waiting for bounded execution progress.'); factStallWatch.textContent = stallSignal.flagged ? 'Probable stall' : stallLevel === 'implementation_pending' ? 'Held at gate' : sharedTruthPrimary ? 'Not a stall' : execution.available ? 'Clear' : 'Unknown'; factStallWatchMeta.textContent = sharedTruthPrimary ? safeText(status.summary, 'Shared truth superseded the older stall view.') : stallLevel !== 'ok' ? safeText(stallSignal.summary, stallSignal.flagged ? 'Probable stall detected.' : 'Implementation is pending.') : execution.available ? `Last update: ${{formatSeconds(execution.last_update_age_seconds)}} ago.` : 'Waiting for execution freshness evidence.'; factPublishStatus.textContent = safeText(quickFacts.publish_status, 'unknown'); factPublishMeta.textContent = safeText(publish.summary, 'No publish summary'); factAuthorityReset.textContent = safeText(quickFacts.authority_reset, 'Inactive'); factAuthorityMeta.textContent = authority.active ? safeText(authority.reason, 'Authority reset active') : 'No authority reset is active.'; renderGuidance(data.operator_guidance || []); renderHandoffs(data.recent_handoffs || []); publishSummary.textContent = safeText(publish.summary, 'No publish summary'); publishMirror.textContent = safeText(publish.mim_mirror_status, 'Unknown'); publishAccess.textContent = safeText(publish.remote_access_status, 'Unknown'); publishConsumer.textContent = safeText(publish.consumer_status, 'Unknown'); publishTime.textContent = `${{safeText(publish.uploaded_at, 'Unknown')}} Ã‚Â· ${{safeText(publish.uploaded_age, 'Unknown')}}`; publishError.textContent = safeText(publish.error, 'None'); alignmentSummary.textContent = safeText(alignment.summary, 'No alignment summary'); alignmentTodObjective.textContent = safeText(alignment.tod_current_objective, 'Unknown'); alignmentMimObjective.textContent = safeText(alignment.mim_objective_active, 'Unknown'); alignmentEvidence.textContent = safeText(evidence.status, 'Unknown'); alignmentSignals.textContent = Array.isArray(evidence.failure_signals) && evidence.failure_signals.length ? evidence.failure_signals.join(', ') : 'None'; decisionSummary.textContent = safeText(decision.summary, 'No listener decision summary'); decisionOutcome.textContent = safeText(decision.decision_outcome, 'Unknown'); decisionReason.textContent = safeText(decision.reason_code, 'Unknown'); decisionState.textContent = safeText(decision.execution_state, 'Unknown'); decisionNextStep.textContent = safeText(decision.next_step_recommendation, 'Unknown'); decisionAge.textContent = safeText(decision.generated_age, 'Unknown'); authoritySummary.textContent = authority.active ? safeText(authority.reason, 'Authority reset is active.') : 'Authority reset is inactive.'; authorityCurrent.textContent = safeText(authority.authoritative_current_objective, 'Unknown'); authorityMaxValid.textContent = safeText(authority.max_valid_objective, 'Unknown'); authorityEffective.textContent = authority.active ? `${{safeText(authority.effective_at, 'Unknown')}} Ã‚Â· ${{safeText(authority.effective_age, 'Unknown')}}` : 'Inactive'; authorityInvalidated.textContent = Array.isArray(authority.invalidated_objectives) && authority.invalidated_objectives.length ? authority.invalidated_objectives.join(', ') : 'None'; footerGenerated.textContent = `Generated: ${{safeText(data.generated_at, 'Unknown')}}`; }}
    async function refresh() {{ const res = await fetch('/tod/ui/state', {{ cache: 'no-store' }}); if (!res.ok) throw new Error(`tod-ui-state-${{res.status}}`); const data = await res.json(); renderState(data); await refreshChatState(); }}
    chatForm.addEventListener('submit', sendChatMessage);
    if (chatImageUploadButton && chatImageUploadInput) {{ chatImageUploadButton.addEventListener('click', () => chatImageUploadInput.click()); chatImageUploadInput.addEventListener('change', () => {{ const file = chatImageUploadInput.files && chatImageUploadInput.files[0] ? chatImageUploadInput.files[0] : null; if (file) setComposerImage(file); }}); }}
    if (chatImageRemoveButton) chatImageRemoveButton.addEventListener('click', resetComposerImage);
    if (chatInput) {{ chatInput.addEventListener('paste', (event) => {{ const items = event.clipboardData && event.clipboardData.items ? Array.from(event.clipboardData.items) : []; for (const item of items) {{ if (String(item.type || '').toLowerCase().startsWith('image/')) {{ const file = item.getAsFile(); if (file) {{ event.preventDefault(); setComposerImage(file); return; }} }} }} }}); }}
    if (chatDropzone) {{ ['dragenter', 'dragover'].forEach((eventName) => {{ chatDropzone.addEventListener(eventName, (event) => {{ event.preventDefault(); chatDropzone.classList.add('active'); }}); }}); ['dragleave', 'drop'].forEach((eventName) => {{ chatDropzone.addEventListener(eventName, (event) => {{ event.preventDefault(); if (eventName !== 'drop') chatDropzone.classList.remove('active'); }}); }}); chatDropzone.addEventListener('drop', (event) => {{ chatDropzone.classList.remove('active'); const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0] ? event.dataTransfer.files[0] : null; if (file) setComposerImage(file); }}); }}
    copyLastTodResponseButton.addEventListener('click', handleCopyLastTodResponse);
    if (todSettingsBtn) todSettingsBtn.addEventListener('click', toggleTodSettingsPanel);
    if (todSettingsCloseBtn) todSettingsCloseBtn.addEventListener('click', closeTodSettingsPanel);
    if (todSettingsBackdrop) todSettingsBackdrop.addEventListener('click', closeTodSettingsPanel);
    if (todSettingsTabVoice) todSettingsTabVoice.addEventListener('click', () => setTodSettingsTab('voice'));
    if (todSettingsTabCamera) todSettingsTabCamera.addEventListener('click', () => setTodSettingsTab('camera'));
    [todVoiceSelect, todServerTtsToggle, todServerTtsVoiceSelect, todDefaultLang, todMicSelect, todAutoLangToggle, todNaturalVoiceToggle].forEach((node) => {{ if (node) node.addEventListener('change', applyTodVoiceSettings); }});
    [todVoiceRate, todVoicePitch, todVoiceDepth, todVoiceVolume].forEach((node) => {{ if (node) node.addEventListener('input', applyTodVoiceSettings); }});
    if (todCameraSelect) todCameraSelect.addEventListener('change', async () => {{ saveTodSetting('camera_device_id', todCameraSelect.value || ''); if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = 'Camera selection updated.'; if (todCameraStream) await startTodCameraPreview().catch((error) => {{ if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = safeText(error && error.message, 'Unable to restart camera preview.'); }}); }});
    if (todCameraRefreshBtn) todCameraRefreshBtn.addEventListener('click', () => {{ if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = 'Refreshing camera list...'; enumerateTodCameraDevices().catch((error) => {{ if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = safeText(error && error.message, 'Unable to enumerate cameras.'); }}); }});
    if (todCameraToggleBtn) todCameraToggleBtn.addEventListener('click', async () => {{ if (todCameraStream) {{ stopTodCameraPreview(); if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = 'Camera preview stopped.'; return; }} if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = 'Starting camera preview...'; await startTodCameraPreview().catch((error) => {{ stopTodCameraPreview(); if (todCameraSettingsStatus) todCameraSettingsStatus.textContent = safeText(error && error.message, 'Unable to start camera preview.'); }}); }});
    document.addEventListener('keydown', (event) => {{ if (event.key === 'Escape' && todSettingsPanel && todSettingsPanel.classList.contains('open')) closeTodSettingsPanel(); }});
    if (window.speechSynthesis && typeof window.speechSynthesis.addEventListener === 'function') {{ window.speechSynthesis.addEventListener('voiceschanged', populateTodVoices); }}
    if (trainingQuickActionButton) trainingQuickActionButton.addEventListener('click', () => handleQuickAction('start-training'));
    if (alignmentQuickActionButton) alignmentQuickActionButton.addEventListener('click', () => handleQuickAction('resolve-drift'));
    if (handoffQuickActionButton) handoffQuickActionButton.addEventListener('click', () => handleQuickAction('send-to-copilot'));
    async function refreshLoop() {{ try {{ await refresh(); }} catch (error) {{ statusChip.textContent = 'ERROR'; statusChip.dataset.tone = 'blocked'; statusHeadline.textContent = 'TOD console refresh failed'; statusSummary.textContent = safeText(error && error.message, 'Unknown refresh failure'); chatStatus.textContent = safeText(error && error.message, 'Unknown refresh failure'); }} }}
    refreshLoop();
    initializeTodSettings();
    setInterval(refreshLoop, 5000);
  </script>
</body>
</html>
        """
    )


@router.get("/chat", response_class=HTMLResponse)
async def chat_console() -> HTMLResponse:
        title = f"Direct Chat | {settings.app_name}"
        return HTMLResponse(
                f"""
<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>{title}</title>
    <style>
        :root {{ --bg-0:#04070a; --bg-1:#09131a; --panel:rgba(7,18,22,0.88); --line:rgba(102,255,188,0.28); --line-strong:rgba(102,255,188,0.65); --ink:#ddfff0; --muted:#88c9af; --accent:#2dff9d; --warn:#ffd166; --good:#2dff9d; --bad:#ff5c7a; --font:"Space Mono","Consolas","Cascadia Mono",monospace; }}
        * {{ box-sizing:border-box; }}
        body {{ margin:0; min-height:100vh; color:var(--ink); font-family:var(--font); background:radial-gradient(circle at 15% 12%, rgba(45,255,157,0.18), transparent 34%), radial-gradient(circle at 88% 10%, rgba(0,174,255,0.14), transparent 32%), linear-gradient(160deg, var(--bg-0), var(--bg-1)); }}
        .page {{ max-width:1320px; margin:0 auto; padding:24px 16px 40px; }}
        .shell {{ border:1px solid var(--line); border-radius:16px; background:var(--panel); overflow:hidden; box-shadow:0 0 28px rgba(45,255,157,0.10); }}
        .hero {{ padding:24px; border-bottom:1px solid var(--line); background:linear-gradient(120deg, rgba(45,255,157,0.14), rgba(0,120,90,0.05)); }}
        .console-nav {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:14px; }}
                .console-link {{ display:inline-flex; align-items:center; gap:8px; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(4,18,16,0.78); color:#ffffff; text-decoration:none; font-size:12px; font-weight:800; letter-spacing:0.08em; text-transform:uppercase; }}
                .console-link span {{ color:#ffffff; }}
        .console-link.active {{ border-color:var(--line-strong); box-shadow:inset 0 0 0 1px rgba(45,255,157,0.14), 0 0 12px rgba(45,255,157,0.12); }}
        .console-link.utility {{ background:rgba(4,18,16,0.64); }}
        .eyebrow {{ font-size:12px; text-transform:uppercase; letter-spacing:0.16em; color:var(--accent); font-weight:700; }}
        h1 {{ margin:10px 0 8px; font-size:clamp(28px, 4vw, 44px); line-height:1; text-transform:uppercase; text-shadow:0 0 10px rgba(45,255,157,0.32); }}
        .hero-copy {{ max-width:880px; color:var(--muted); font-size:15px; line-height:1.5; }}
        .hero-meta {{ margin-top:16px; display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:12px; }}
        .fact {{ border:1px solid rgba(97,219,191,0.16); border-radius:12px; background:rgba(2,12,10,0.72); padding:14px; }}
        .fact-label {{ font-size:12px; text-transform:uppercase; letter-spacing:0.12em; color:var(--muted); }}
        .fact-value {{ margin-top:8px; font-size:18px; font-weight:800; line-height:1.2; }}
        .fact-meta {{ margin-top:8px; color:var(--muted); font-size:13px; line-height:1.45; }}
        .grid {{ display:grid; grid-template-columns:minmax(0, 0.9fr) minmax(0, 1.1fr); gap:18px; padding:22px 24px 24px; }}
        .panel {{ border:1px solid rgba(97,219,191,0.20); border-radius:14px; background:rgba(3,15,13,0.86); padding:18px; }}
        .panel h2 {{ margin:0 0 12px; font-size:16px; }}
        .panel-copy {{ color:var(--muted); font-size:14px; line-height:1.5; }}
        .launch-grid {{ display:grid; gap:12px; grid-template-columns:repeat(2, minmax(0, 1fr)); margin-top:14px; }}
        .launch-card {{ border:1px solid rgba(97,219,191,0.18); border-radius:12px; background:rgba(2,12,10,0.76); padding:14px; }}
        .launch-card strong {{ display:block; font-size:13px; text-transform:uppercase; letter-spacing:0.10em; color:var(--accent); }}
        .launch-card p {{ margin:10px 0 12px; color:var(--muted); font-size:13px; line-height:1.5; }}
        .button-row {{ display:flex; gap:10px; flex-wrap:wrap; }}
        .btn {{ appearance:none; border:1px solid var(--line); border-radius:10px; padding:11px 15px; background:linear-gradient(120deg, rgba(11,110,79,0.9), rgba(45,255,157,0.33)); color:#ebfff4; font:inherit; font-size:13px; font-weight:700; cursor:pointer; text-decoration:none; }}
        .btn.secondary {{ background:rgba(4,20,17,0.78); color:var(--ink); }}
        .btn:disabled {{ opacity:0.65; cursor:wait; }}
        .chat-meta {{ display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; font-size:12px; color:var(--muted); margin-top:12px; }}
        .chat-thread {{ min-height:320px; max-height:520px; overflow-y:auto; border:1px solid rgba(97,219,191,0.22); border-radius:10px; padding:14px; background:rgba(3,15,13,0.86); display:grid; gap:12px; margin-top:12px; }}
        .chat-bubble {{ max-width:92%; border-radius:12px; padding:12px 14px; border:1px solid rgba(97,219,191,0.18); background:rgba(4,18,16,0.85); }}
        .chat-bubble.user {{ margin-left:auto; background:linear-gradient(145deg, rgba(8,34,30,0.9), rgba(4,16,14,0.95)); border-color:rgba(45,255,157,0.30); }}
        .chat-bubble.assistant {{ margin-right:auto; }}
        .chat-role {{ font-size:12px; text-transform:uppercase; letter-spacing:0.12em; color:var(--accent); font-weight:700; }}
        .chat-time {{ font-size:12px; color:var(--muted); margin-top:4px; }}
        .chat-message {{ margin-top:8px; font-size:14px; line-height:1.55; white-space:pre-wrap; word-break:break-word; }}
        .chat-form {{ display:grid; gap:10px; margin-top:12px; }}
        .chat-input {{ width:100%; min-height:128px; resize:vertical; border-radius:10px; border:1px solid rgba(97,219,191,0.24); background:rgba(3,14,12,0.92); padding:14px; font:inherit; color:var(--ink); outline:none; }}
        .status-inline {{ font-size:12px; color:var(--muted); }}
        .chat-actions {{ display:flex; justify-content:space-between; gap:10px; align-items:center; flex-wrap:wrap; }}
        .chat-action-buttons {{ display:flex; gap:10px; flex-wrap:wrap; }}
        @media (max-width: 980px) {{ .grid {{ grid-template-columns:1fr; }} .hero-meta {{ grid-template-columns:repeat(2, minmax(0, 1fr)); }} .launch-grid {{ grid-template-columns:1fr; }} }}
        @media (max-width: 640px) {{ .hero-meta {{ grid-template-columns:1fr; }} }}
    </style>
</head>
<body>
    <main class=\"page\">
        <section class=\"shell\">
            <header class=\"hero\">
                <div class=\"console-nav\">
                    <a class=\"console-link utility\" href=\"/\">Public Home</a>
                    <a class=\"console-link utility\" href=\"/mim\">MIM Codex Chat</a>
                    <a class=\"console-link utility\" href=\"/tod\">TOD Console</a>
                    <a class=\"console-link active\" href=\"/chat\">Direct Chat</a>
                    <a class=\"console-link utility\" href=\"/mim/logout\">Logout</a>
                </div>
                <div class=\"eyebrow\">Operator Surface</div>
                <h1>Direct Copilot And Codex Bridge</h1>
                <div id=\"heroCopy\" class=\"hero-copy\">Use this page to stay on mimtod.com, send bounded operator chat messages, launch the 6-hour training runbook, or publish a direct Codex handoff without remote-login friction.</div>
                <div class=\"hero-meta\">
                    <article class=\"fact\"><div class=\"fact-label\">Status</div><div id=\"factStatus\" class=\"fact-value\">Loading</div><div id=\"factStatusMeta\" class=\"fact-meta\">Waiting for live status.</div></article>
                    <article class=\"fact\"><div class=\"fact-label\">Canonical Objective</div><div id=\"factObjective\" class=\"fact-value\">-</div><div id=\"factObjectiveMeta\" class=\"fact-meta\">Waiting for current objective.</div></article>
                    <article class=\"fact\"><div class=\"fact-label\">Listener</div><div id=\"factListener\" class=\"fact-value\">-</div><div id=\"factListenerMeta\" class=\"fact-meta\">Waiting for execution posture.</div></article>
                    <article class=\"fact\"><div class=\"fact-label\">Training</div><div id=\"factTraining\" class=\"fact-value\">-</div><div id=\"factTrainingMeta\" class=\"fact-meta\">Waiting for training telemetry.</div></article>
                </div>
            </header>
            <section class=\"grid\">
                <section class=\"panel\">
                    <h2>Launch Pads</h2>
                    <div class=\"panel-copy\">This tab is the shared bridge. Use it for direct operator actions, or jump into the dedicated TOD and MIM surfaces when you want the full console context.</div>
                    <div class=\"launch-grid\">
                        <article class=\"launch-card\"><strong>Direct Copilot Bridge</strong><p>Stay on this page to send bounded messages, launch training, or publish a Codex handoff from one place.</p><div class=\"button-row\"><button id=\"startTrainingButton\" class=\"btn\" type=\"button\">Start 6h Training</button><button id=\"sendToCodexButton\" class=\"btn secondary\" type=\"button\">Send To Codex</button></div></article>
                        <article class=\"launch-card\"><strong>Other Surfaces</strong><p>Jump straight into the TOD console or the MIM Codex chat when you want their dedicated layouts.</p><div class=\"button-row\"><a class=\"btn secondary\" href=\"/tod\">Open TOD Console</a><a class=\"btn secondary\" href=\"/mim\">Open MIM Codex Chat</a></div></article>
                    </div>
                </section>
                <section class=\"panel\">
                    <h2>Direct Chat</h2>
                    <div id=\"chatSummary\" class=\"panel-copy\">Loading direct operator chat.</div>
                    <div class=\"chat-meta\"><div id=\"chatSessionMeta\">Session: loading</div><div id=\"chatGuardrails\">Guardrails: loading</div></div>
                    <div id=\"chatThread\" class=\"chat-thread\"></div>
                    <form id=\"chatForm\" class=\"chat-form\">
                        <textarea id=\"chatInput\" class=\"chat-input\" placeholder=\"Ask for status, request a bounded repair, or tell Copilot to start the next training runbook.\"></textarea>
                        <div class=\"chat-actions\"><div id=\"chatStatus\" class=\"status-inline\">Waiting for direct chat state.</div><div class=\"chat-action-buttons\"><button id=\"copyLastReplyButton\" class=\"btn secondary\" type=\"button\">Copy Last Reply</button><button id=\"chatSendButton\" class=\"btn\" type=\"submit\">Send Message</button></div></div>
                    </form>
                </section>
            </section>
        </section>
    </main>
    <script>
        const factStatus = document.getElementById('factStatus');
        const factStatusMeta = document.getElementById('factStatusMeta');
        const factObjective = document.getElementById('factObjective');
        const factObjectiveMeta = document.getElementById('factObjectiveMeta');
        const factListener = document.getElementById('factListener');
        const factListenerMeta = document.getElementById('factListenerMeta');
        const factTraining = document.getElementById('factTraining');
        const factTrainingMeta = document.getElementById('factTrainingMeta');
        const chatSummary = document.getElementById('chatSummary');
        const chatSessionMeta = document.getElementById('chatSessionMeta');
        const chatGuardrails = document.getElementById('chatGuardrails');
        const chatThread = document.getElementById('chatThread');
        const chatForm = document.getElementById('chatForm');
        const chatInput = document.getElementById('chatInput');
        const chatStatus = document.getElementById('chatStatus');
        const chatSendButton = document.getElementById('chatSendButton');
        const copyLastReplyButton = document.getElementById('copyLastReplyButton');
        const startTrainingButton = document.getElementById('startTrainingButton');
        const sendToCodexButton = document.getElementById('sendToCodexButton');
        const CHAT_STORAGE_KEY = 'todDirectChatSessionKeyV1';
        let latestPayload = null;
        function safeText(value, fallback = '-') {{ const text = String(value || '').trim(); return text || fallback; }}
        function clearNode(node) {{ while (node && node.firstChild) node.removeChild(node.firstChild); }}
        function getSessionKey() {{ try {{ const existing = window.localStorage.getItem(CHAT_STORAGE_KEY); if (existing) return existing; const created = `copilot-operator-chat-${{Math.random().toString(36).slice(2, 10)}}`; window.localStorage.setItem(CHAT_STORAGE_KEY, created); return created; }} catch (_error) {{ return `copilot-operator-chat-${{Math.random().toString(36).slice(2, 10)}}`; }} }}
        function messageRole(message) {{ const role = safeText(message && (message.role || message.actor || message.source || message.type), 'message').toLowerCase(); if (role.includes('operator') || role.includes('visitor') || role.includes('user')) return 'user'; return 'assistant'; }}
        function messageBody(message) {{ return safeText(message && (message.content || message.message || message.text || message.body || message.summary), ''); }}
        function setButtonsDisabled(disabled) {{ chatSendButton.disabled = disabled; copyLastReplyButton.disabled = disabled; startTrainingButton.disabled = disabled; sendToCodexButton.disabled = disabled; }}
        function renderThread(messages) {{ clearNode(chatThread); if (!Array.isArray(messages) || !messages.length) {{ const empty = document.createElement('article'); empty.className = 'chat-bubble assistant'; empty.innerHTML = '<div class="chat-role">Copilot</div><div class="chat-message">No direct messages yet. Send a message, launch training, or create a Codex handoff.</div>'; chatThread.appendChild(empty); return; }} messages.forEach((message) => {{ const bubble = document.createElement('article'); const role = messageRole(message); bubble.className = `chat-bubble ${{role}}`; const roleNode = document.createElement('div'); roleNode.className = 'chat-role'; roleNode.textContent = role === 'user' ? 'Operator' : 'Copilot'; const timeNode = document.createElement('div'); timeNode.className = 'chat-time'; timeNode.textContent = safeText(message.created_at, ''); const contentNode = document.createElement('div'); contentNode.className = 'chat-message'; contentNode.textContent = messageBody(message); bubble.appendChild(roleNode); if (timeNode.textContent) bubble.appendChild(timeNode); bubble.appendChild(contentNode); chatThread.appendChild(bubble); }}); chatThread.scrollTop = chatThread.scrollHeight; }}
        function getLastExchangeText() {{ const messages = latestPayload && Array.isArray(latestPayload.messages) ? latestPayload.messages : []; if (!messages.length) return ''; let lastAssistant = null; for (let index = messages.length - 1; index >= 0; index -= 1) {{ const candidate = messages[index]; if (messageRole(candidate) !== 'assistant' || !messageBody(candidate)) continue; lastAssistant = candidate; let lastUser = null; for (let userIndex = index - 1; userIndex >= 0; userIndex -= 1) {{ const prior = messages[userIndex]; if (messageRole(prior) === 'user' && messageBody(prior)) {{ lastUser = prior; break; }} }} const lines = []; if (lastUser) {{ lines.push('User action:'); lines.push(messageBody(lastUser)); lines.push(''); }} lines.push('Assistant response:'); lines.push(messageBody(lastAssistant)); return lines.join('\\n'); }} return ''; }}
        async function copyLastExchange() {{ const transcript = getLastExchangeText(); if (!transcript) {{ chatStatus.textContent = 'No assistant reply is available to copy yet.'; return; }} if (navigator.clipboard && navigator.clipboard.writeText) {{ await navigator.clipboard.writeText(transcript); chatStatus.textContent = 'Copied the last user action and assistant reply.'; return; }} const area = document.createElement('textarea'); area.value = transcript; area.style.position = 'fixed'; area.style.opacity = '0'; document.body.appendChild(area); area.focus(); area.select(); document.execCommand('copy'); document.body.removeChild(area); chatStatus.textContent = 'Copied the last user action and assistant reply.'; }}
        function renderPayload(payload) {{ latestPayload = payload || {{}}; const status = payload && typeof payload.status === 'object' ? payload.status : {{}}; const quickFacts = payload && typeof payload.quick_facts === 'object' ? payload.quick_facts : {{}}; const guardrails = payload && typeof payload.guardrails === 'object' ? payload.guardrails : {{}}; const session = payload && typeof payload.session === 'object' ? payload.session : {{}}; const capabilities = payload && typeof payload.capabilities === 'object' ? payload.capabilities : {{}}; factStatus.textContent = safeText(status.label, 'UNKNOWN'); factStatusMeta.textContent = safeText(status.summary, 'No status summary is available.'); factObjective.textContent = safeText(quickFacts.canonical_objective, 'Unknown'); factObjectiveMeta.textContent = `Live request: ${{safeText(quickFacts.live_request_objective, 'Unknown')}}`; factListener.textContent = safeText(quickFacts.listener_state, 'Unknown'); factListenerMeta.textContent = `Decision: ${{safeText(quickFacts.decision_outcome, 'Unknown')}}`; factTraining.textContent = safeText(quickFacts.training_state, 'Unknown'); factTrainingMeta.textContent = `Progress: ${{safeText(quickFacts.training_progress, 'Unknown')}} Ã‚Â· Training start ${{capabilities.training_start && capabilities.training_start.available ? 'ready' : 'unavailable'}}`; chatSummary.textContent = safeText(payload && payload.visitor && payload.visitor.memory_summary, 'Direct operator chat is ready.'); chatSessionMeta.textContent = `Session: ${{safeText(session.session_key, getSessionKey())}} Ã‚Â· Messages: ${{safeText(session.message_count, '0')}}`; chatGuardrails.textContent = `Guardrails: commands blocked = ${{guardrails.commands_blocked ? 'yes' : 'no'}}, live execution blocked = ${{guardrails.live_execution_blocked ? 'yes' : 'no'}}`; renderThread(payload && payload.messages); copyLastReplyButton.disabled = !(payload && Array.isArray(payload.messages) && payload.messages.length); }}
        async function fetchState() {{ const response = await fetch(`/chat/ui/state?session_key=${{encodeURIComponent(getSessionKey())}}&mode=chat`, {{ cache: 'no-store' }}); if (!response.ok) throw new Error(`chat-state-${{response.status}}`); renderPayload(await response.json()); }}
        async function postJson(path, body, successText) {{ setButtonsDisabled(true); chatStatus.textContent = 'Sending request...'; try {{ const response = await fetch(path, {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(body) }}); if (!response.ok) throw new Error(`${{path}}-${{response.status}}`); const payload = await response.json(); renderPayload(payload); chatStatus.textContent = successText; return true; }} catch (error) {{ chatStatus.textContent = `Request failed: ${{safeText(error && error.message, 'unknown error')}}`; return false; }} finally {{ setButtonsDisabled(false); }} }}
        async function sendMessage(event) {{ event.preventDefault(); const message = String(chatInput.value || '').trim(); if (!message) {{ chatStatus.textContent = 'Enter a message first.'; return; }} const sent = await postJson('/chat/ui/message', {{ session_key: getSessionKey(), mode: 'chat', message }}, 'Direct operator message delivered.'); if (sent) chatInput.value = ''; }}
        async function startTraining() {{ await postJson('/chat/ui/action/training', {{ session_key: getSessionKey(), mode: 'chat' }}, 'Training action submitted.'); }}
        async function sendToCodex() {{ const message = String(chatInput.value || '').trim() || 'Package the current issue, evidence, and next bounded repair request for Codex-style troubleshooting.'; const sent = await postJson('/chat/ui/handoff', {{ session_key: getSessionKey(), mode: 'chat', message }}, 'Codex handoff created.'); if (sent) chatInput.value = ''; }}
        chatForm.addEventListener('submit', sendMessage);
        copyLastReplyButton.addEventListener('click', copyLastExchange);
        startTrainingButton.addEventListener('click', startTraining);
        sendToCodexButton.addEventListener('click', sendToCodex);
        fetchState().catch((error) => {{ chatStatus.textContent = safeText(error && error.message, 'Unable to load direct chat state.'); }});
        setInterval(() => fetchState().catch(() => {{}}), 5000);
    </script>
</body>
</html>
                """
        )