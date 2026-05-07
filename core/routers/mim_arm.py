from __future__ import annotations

import json
import os
import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.config import settings
from core.execution_trace_service import append_execution_trace_event
from core.execution_lane_service import TARGET_MIM_ARM, build_execution_target_profile, submit_execution_request
from core.journal import write_journal
from core.mim_arm_dispatch_telemetry import (
    record_dispatch_telemetry_from_publish,
    refresh_dispatch_telemetry_record,
)
from core.primitive_request_recovery_service import load_authoritative_request_status
from core.models import (
    ArmEnvelopeProbeAttempt,
    ArmProbeAuthorization,
    ArmServoEnvelope,
    SupervisedMicroStepExecution,
    SupervisedPhysicalMicroStepExecution,
    CapabilityExecution,
    CapabilityRegistration,
    ExecutionTaskOrchestration,
    InputEvent,
    InputEventResolution,
)
from core.arm_envelope_service import (
    approve_probe_authorization,
    begin_supervised_micro_step_execution,
    check_physical_micro_step_allowed,
    create_supervised_micro_step_authorization,
    execute_physical_micro_step,
    expire_pending_probe_authorizations,
    get_physical_execution,
    get_probe_authorization,
    get_supervised_execution,
    generate_dry_run_commands_for_servo,
    generate_dry_run_plan,
    generate_simulation_probe_plan_for_servo,
    get_envelope,
    get_envelopes,
    get_probe_attempts,
    initialize_envelopes,
    is_stale,
    DirectArmHttpAdapter,
    MockServoAdapter,
    record_supervised_probe_outcome,
    ServoHardwareAdapter,
    reject_probe_authorization,
    trigger_safe_home_fallback,
)
from core.schemas import (
    ArmDryRunCommandRequest,
    ArmDryRunCommandSequence,
    ArmEnvelopeProbeAttemptRead,
    ArmEnvelopeProbePlanPreview,
    ArmProbeAuthorizationApproveRequest,
    ArmProbeAuthorizationRead,
    ArmProbeAuthorizationRejectRequest,
    ArmProbeAuthorizationRequest,
    ArmProbeExecutionGateResult,
    ArmServoEnvelopeRead,
    ArmServoEnvelopeUpdateRequest,
    ArmSimulationProbePlan,
    ArmSimulationProbeRequest,
    PhysicalMicroStepExecutionRead,
    PhysicalMicroStepExecutionRequest,
    RecordProbeOutcomeRead,
    RecordProbeOutcomeRequest,
    SafeHomeTriggerRequest,
    SupervisedMicroStepExecutionRead,
    SupervisedMicroStepExecutionRequest,
)
from core.routers.self_awareness_router import health_monitor as _mim_health_monitor
from core.routers import gateway as gateway_router
from core.task_orchestrator import to_execution_task_orchestration_out
from core.tod_mim_contract import CONTRACT_SCHEMA_VERSION, normalize_and_validate_file

router = APIRouter(prefix="/mim/arm", tags=["mim-arm"])

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SHARED_ROOT = Path("runtime/shared")
ARM_STATUS_ARTIFACT = "mim_arm_status.latest.json"
ARM_HOST_STATE_ARTIFACT = "mim_arm_host_state.latest.json"
ARM_DIAGNOSTIC_ARTIFACT = "mim_arm_startup_diagnostic.latest.json"
ARM_CONTROL_READINESS_ARTIFACT = "mim_arm_control_readiness.latest.json"
ARM_REFRESH_STATUS_ARTIFACT = "mim_arm_refresh_status.latest.json"
TOD_COMMAND_STATUS_ARTIFACT = "TOD_MIM_COMMAND_STATUS.latest.json"
TOD_TASK_RESULT_ARTIFACT = "TOD_MIM_TASK_RESULT.latest.json"
TOD_CATCHUP_GATE_ARTIFACT = "TOD_CATCHUP_GATE.latest.json"
MIM_DECISION_TASK_ARTIFACT = "MIM_DECISION_TASK.latest.json"
MIM_ARM_DISPATCH_TELEMETRY_ARTIFACT = "MIM_ARM_DISPATCH_TELEMETRY.latest.json"
CONTEXT_EXPORT_ARTIFACT = "MIM_CONTEXT_EXPORT.latest.json"
TOD_BRIDGE_REQUEST_ARTIFACT = "MIM_TOD_BRIDGE_REQUEST.latest.json"
MIM_ARM_COMPOSED_TASK_ARTIFACT = "MIM_ARM_COMPOSED_TASK.latest.json"
MIM_ARM_COMPOSED_TASK_DIRNAME = "mim_arm_composed_tasks"
ARM_SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_mim_arm_host_state.py"
ARM_STATUS_SCRIPT = PROJECT_ROOT / "scripts" / "generate_mim_arm_status.py"
BRIDGE_SEQUENCE_SCRIPT = PROJECT_ROOT / "scripts" / "bridge_packet_sequence.py"
TOD_REMOTE_PUBLISH_SCRIPT = PROJECT_ROOT / "scripts" / "publish_tod_bridge_artifacts_remote.py"
TOD_BRIDGE_AUDIT_SCRIPT = PROJECT_ROOT / "scripts" / "tod_bridge_audit.py"
MIM_ARM_ENV_FILE = PROJECT_ROOT / "env" / ".env"
BOUNDED_LIVE_ACTIONS = ("safe_home", "scan_pose", "capture_frame")

MIM_ARM_CAPABILITY_DEFINITIONS = [
    {
        "capability_name": "mim_arm.get_control_readiness",
        "category": "management",
        "description": "Evaluate whether MIM currently has access, bounded control readiness, and management authority over MIM_ARM.",
        "requires_confirmation": False,
        "enabled": True,
        "safety_policy": {
            "stage": "management_readiness",
            "mode": "read_only",
            "executor": "mim",
            "live_motion": False,
        },
    },
    {
        "capability_name": "mim_arm.refresh_status",
        "category": "management",
        "description": "Refresh the remote arm-host truth and regenerate MIM's local bounded status surface without moving hardware.",
        "requires_confirmation": False,
        "enabled": True,
        "safety_policy": {
            "stage": "management_readiness",
            "mode": "read_only",
            "executor": "mim",
            "live_motion": False,
        },
    },
    {
        "capability_name": "mim_arm.get_status",
        "category": "diagnostic",
        "description": "Read the bounded MIM_ARM status surface before any motion proposal.",
        "requires_confirmation": False,
        "enabled": True,
        "safety_policy": {
            "stage": "read_only_awareness",
            "mode": "read_only",
            "executor": "tod",
            "live_motion": False,
        },
    },
    {
        "capability_name": "mim_arm.get_pose",
        "category": "diagnostic",
        "description": "Read the current bounded arm pose and servo state snapshot.",
        "requires_confirmation": False,
        "enabled": True,
        "safety_policy": {
            "stage": "read_only_awareness",
            "mode": "read_only",
            "executor": "tod",
            "live_motion": False,
        },
    },
    {
        "capability_name": "mim_arm.get_camera_state",
        "category": "diagnostic",
        "description": "Read the camera availability state exposed by MIM_ARM.",
        "requires_confirmation": False,
        "enabled": True,
        "safety_policy": {
            "stage": "read_only_awareness",
            "mode": "read_only",
            "executor": "tod",
            "live_motion": False,
        },
    },
    {
        "capability_name": "mim_arm.get_last_execution",
        "category": "diagnostic",
        "description": "Read the last bounded execution result and TOD readiness posture.",
        "requires_confirmation": False,
        "enabled": True,
        "safety_policy": {
            "stage": "read_only_awareness",
            "mode": "read_only",
            "executor": "tod",
            "live_motion": False,
        },
    },
    {
        "capability_name": "mim_arm.propose_safe_home",
        "category": "proposal",
        "description": "Generate a proposal-only safe-home motion request for TOD/operator review.",
        "requires_confirmation": False,
        "enabled": True,
        "safety_policy": {
            "stage": "proposal_only",
            "mode": "proposal_only",
            "executor": "tod",
            "dispatch_allowed": False,
            "operator_approval_required_for_execution": True,
        },
    },
    {
        "capability_name": "mim_arm.propose_scan_pose",
        "category": "proposal",
        "description": "Generate a proposal-only scan-pose request for TOD/operator review.",
        "requires_confirmation": False,
        "enabled": True,
        "safety_policy": {
            "stage": "proposal_only",
            "mode": "proposal_only",
            "executor": "tod",
            "dispatch_allowed": False,
            "operator_approval_required_for_execution": True,
        },
    },
    {
        "capability_name": "mim_arm.propose_capture_frame",
        "category": "proposal",
        "description": "Generate a proposal-only capture-frame request for TOD/operator review.",
        "requires_confirmation": False,
        "enabled": True,
        "safety_policy": {
            "stage": "proposal_only",
            "mode": "proposal_only",
            "executor": "tod",
            "dispatch_allowed": False,
            "operator_approval_required_for_execution": True,
        },
    },
    {
        "capability_name": "mim_arm.execute_safe_home",
        "category": "manipulation",
        "description": "Dispatch a governed bounded safe_home action to TOD.",
        "requires_confirmation": True,
        "enabled": True,
        "safety_policy": {
            "stage": "bounded_execution",
            "mode": "operator_guarded",
            "executor": "tod",
            "allowed_targets": ["safe_home"],
            "operator_approval_required_for_execution": True,
        },
    },
    {
        "capability_name": "mim_arm.execute_scan_pose",
        "category": "manipulation",
        "description": "Dispatch a governed bounded scan_pose action to TOD.",
        "requires_confirmation": True,
        "enabled": True,
        "safety_policy": {
            "stage": "bounded_execution",
            "mode": "operator_guarded",
            "executor": "tod",
            "allowed_targets": ["scan_pose"],
            "operator_approval_required_for_execution": True,
        },
    },
    {
        "capability_name": "mim_arm.execute_capture_frame",
        "category": "manipulation",
        "description": "Dispatch a governed bounded capture_frame action to TOD.",
        "requires_confirmation": True,
        "enabled": True,
        "safety_policy": {
            "stage": "bounded_execution",
            "mode": "operator_guarded",
            "executor": "tod",
            "allowed_targets": ["capture_frame"],
            "operator_approval_required_for_execution": True,
        },
    },
    {
        "capability_name": "mim_arm.execute_gripper",
        "category": "manipulation",
        "description": "Prepare a governed gripper/claw motion request for TOD/operator review.",
        "requires_confirmation": True,
        "enabled": True,
        "safety_policy": {
            "stage": "bounded_execution",
            "mode": "operator_guarded",
            "executor": "tod",
            "allowed_targets": ["open_gripper", "close_gripper", "set_gripper"],
            "operator_approval_required_for_execution": True,
            "guard_terms": ["gripper", "claw", "servo", "estop_ok", "motion_allowed"],
        },
    },
    {
        "capability_name": "mim_arm.supervised_probe",
        "category": "manipulation",
        "description": "Prepare a governed supervised servo-envelope probe goal for TOD/operator review before any motion.",
        "requires_confirmation": True,
        "enabled": True,
        "safety_policy": {
            "stage": "supervised_probe_prep",
            "mode": "operator_guarded",
            "executor": "tod",
            "allowed_targets": ["servo_envelope_probe"],
            "operator_approval_required_for_execution": True,
            "guard_terms": [
                "servo",
                "gripper",
                "arm",
                "safe_home",
                "motion_allowed",
                "estop_ok",
                "learned_bounds",
            ],
        },
    },
]


class MimArmExecuteSafeHomeRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    explicit_operator_approval: bool = False
    shared_workspace_active: bool = False
    metadata_json: dict = Field(default_factory=dict)


def _action_slug(action_name: str) -> str:
    return str(action_name or "").strip().replace("_", "-")


def _action_display_name(action_name: str) -> str:
    return str(action_name or "").strip().replace("_", " ")


def _bounded_live_actions_phrase() -> str:
    action_names = [str(action).strip() for action in BOUNDED_LIVE_ACTIONS if str(action).strip()]
    if not action_names:
        return ""
    if len(action_names) == 1:
        return action_names[0]
    return f"{', '.join(action_names[:-1])}, or {action_names[-1]}"


def _bounded_action_execution_phrase(action_name: str) -> str:
    if action_name in {"safe_home", "scan_pose"}:
        return f"Move the arm to the {action_name} pose via TOD-governed bounded execution."
    if action_name == "capture_frame":
        return "Capture one bounded frame via TOD-governed execution."
    return f"Execute bounded {action_name} via TOD-governed execution."


def _resolve_execution_action_name(execution: CapabilityExecution) -> str:
    arguments = _json_dict(getattr(execution, "arguments_json", {}))
    action_name = str(arguments.get("target_pose") or arguments.get("action") or "").strip()
    return action_name


class MimArmRefreshStatusRequest(BaseModel):
    remote_sync: bool = True
    skip_remote_run: bool = False


class MimArmExecutionLaneRequest(BaseModel):
    request_id: str
    target: str = TARGET_MIM_ARM
    sequence: int = 1
    issued_at: str = ""
    expires_at: str = ""
    supersedes_request_id: str = ""
    command: dict = Field(default_factory=dict)
    metadata_json: dict = Field(default_factory=dict)


class MimArmComposedTaskRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    explicit_operator_approval: bool = False
    shared_workspace_active: bool = False
    steps: list[str] = Field(default_factory=lambda: ["safe_home", "scan_pose", "capture_frame"])
    max_retry_per_step: int = Field(default=1, ge=0, le=3)
    metadata_json: dict = Field(default_factory=dict)


class MimArmComposedTaskAdvanceRequest(BaseModel):
    actor: str = "operator"
    reason: str = ""
    explicit_operator_approval: bool = False
    allow_retry: bool = True
    metadata_json: dict = Field(default_factory=dict)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _freshness_token(value: object) -> int:
    parsed = _parse_timestamp(value)
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    return int(parsed.strftime("%Y%m%d%H%M%S"))


def _json_dict(raw: object) -> dict:
    return raw if isinstance(raw, dict) else {}


def _read_json_artifact(path: Path) -> dict:
    try:
        return _json_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return {}


def _write_json_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _coerce_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "ok", "ready", "online", "clear", "released"}:
        return True
    if text in {"0", "false", "no", "off", "offline", "error", "pressed", "engaged", "blocked"}:
        return False
    return None


def _coerce_status(value: object, *, default: str = "unknown") -> str:
    return str(value or default).strip().lower() or default


def _coerce_pose(value: object, *, default: str = "unknown") -> object:
    if isinstance(value, (list, dict)):
        return value
    if value in {None, ""}:
        return default
    return str(value).strip() or default


def _write_json_artifact(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _audit_tod_bridge_write(
    *,
    event: str,
    caller: str,
    service_name: str,
    task_id: str,
    objective_id: str,
    artifact_path: Path,
    publish_attempted: bool = False,
    publish_succeeded: bool = False,
    publish_returncode: int = 0,
    publish_output: str = "",
) -> None:
    if not TOD_BRIDGE_AUDIT_SCRIPT.exists():
        return
    subprocess.run(
        [
            sys.executable,
            str(TOD_BRIDGE_AUDIT_SCRIPT),
            "--event",
            event,
            "--caller",
            caller,
            "--service-name",
            service_name,
            "--task-id",
            task_id,
            "--objective-id",
            objective_id,
            "--publish-target",
            f"/home/testpilot/mim/runtime/shared -> {os.getenv('MIM_TOD_SSH_HOST', '192.168.1.120')}:{os.getenv('MIM_TOD_SSH_REMOTE_ROOT', '/home/testpilot/mim/runtime/shared')}",
            "--remote-host",
            os.getenv("MIM_TOD_SSH_HOST", "192.168.1.120"),
            "--remote-root",
            os.getenv("MIM_TOD_SSH_REMOTE_ROOT", "/home/testpilot/mim/runtime/shared"),
            "--publish-attempted",
            "true" if publish_attempted else "false",
            "--publish-succeeded",
            "true" if publish_succeeded else "false",
            "--publish-returncode",
            str(publish_returncode),
            "--publish-output",
            publish_output,
            "--artifact-path",
            str(artifact_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _artifact_exists(path_value: object) -> bool:
    text = str(path_value or "").strip()
    if not text:
        return False
    return Path(text).exists()


def _env_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _seconds_since(timestamp: object) -> float | None:
    parsed = _parse_timestamp(timestamp)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _normalize_objective(raw: object) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return "", ""
    if text.startswith("objective-"):
        return text.removeprefix("objective-"), text
    return text, f"objective-{text}"


def _bridge_meta(
    *,
    shared_root: Path,
    service_name: str,
    instance_id: str,
) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            str(BRIDGE_SEQUENCE_SCRIPT),
            "--shared-dir",
            str(shared_root),
            "--service",
            service_name,
            "--instance-id",
            instance_id,
            "--host",
            "MIM",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload: dict[str, object] = {}
    for raw_line in result.stdout.splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        payload[key.strip()] = value.strip()
    return payload


def _active_objective_metadata(shared_root: Path) -> dict[str, str]:
    candidates = [
        shared_root / CONTEXT_EXPORT_ARTIFACT,
        PROJECT_ROOT / CONTEXT_EXPORT_ARTIFACT,
    ]
    for path in candidates:
        payload = _read_json_artifact(path)
        if payload:
            objective_raw = payload.get("objective_active") or payload.get("current_next_objective")
            objective_id, objective_ref = _normalize_objective(objective_raw)
            return {
                "objective_id": objective_id,
                "objective_ref": objective_ref,
                "release_tag": str(payload.get("release_tag") or "").strip(),
                "schema_version": str(payload.get("schema_version") or "").strip(),
            }
    return {
        "objective_id": "",
        "objective_ref": "",
        "release_tag": "",
        "schema_version": "",
    }


def publish_mim_arm_execution_to_tod(
    *,
    execution: CapabilityExecution,
    status: dict[str, object],
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    shared_root = shared_root.expanduser().resolve()
    shared_root.mkdir(parents=True, exist_ok=True)
    _load_mim_arm_env_defaults()

    action_name = _resolve_execution_action_name(execution)
    if not action_name:
        raise RuntimeError("Bounded MIM arm execution publish requires explicit action identity.")
    action_slug = _action_slug(action_name)
    action_display = _action_display_name(action_name)
    capability_name = str(getattr(execution, "capability_name", f"mim_arm.execute_{action_name}") or f"mim_arm.execute_{action_name}").strip()
    publication_service = f"mim_arm_{action_name}_dispatch"
    execution_id = int(getattr(execution, "id", 0) or 0)
    publication_instance = f"{publication_service}:{execution_id}"
    objective = _active_objective_metadata(shared_root)
    objective_id = objective.get("objective_id", "")
    objective_ref = objective.get("objective_ref", "") or "objective-unknown"
    tod_action = "run-bridge-request" if action_name == "capture_frame" else ""

    request_meta = _bridge_meta(
        shared_root=shared_root,
        service_name=publication_service,
        instance_id=publication_instance,
    )
    request_generated_at = str(request_meta.get("EMITTED_AT") or _utcnow()).strip()
    request_sequence = int(str(request_meta.get("SEQUENCE") or "1").strip() or "1")
    publish_freshness_token = _freshness_token(request_generated_at)
    request_id = f"{objective_ref}-task-mim-arm-{action_slug}-{publish_freshness_token}"
    correlation_id = f"obj{objective_id or 'unknown'}-mim-arm-{action_slug}-{publish_freshness_token}"
    request_path = shared_root / "MIM_TOD_TASK_REQUEST.latest.json"
    bridge_request_path = shared_root / TOD_BRIDGE_REQUEST_ARTIFACT
    bridge_request_payload = {
        "version": "1.0",
        "source": "MIM",
        "target": "TOD",
        "generated_at": request_generated_at,
        "emitted_at": request_generated_at,
        "sequence": request_sequence,
        "objective_id": objective_ref,
        "objective": objective_ref,
        "task_id": request_id,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "CorrelationId": correlation_id,
        "action": action_name,
        "Action": action_name,
        "capability_name": capability_name,
        "CapabilityName": capability_name,
        "execution_lane": "tod_bridge_request" if tod_action else "",
        "command": {
            "name": action_name,
            "args": {},
        },
    }
    if tod_action:
        bridge_request_payload["tod_action"] = action_name
    request_payload = {
        "version": "1.0",
        "source": "MIM",
        "target": "TOD",
        "generated_at": request_generated_at,
        "emitted_at": request_generated_at,
        "sequence": request_sequence,
        "source_host": str(request_meta.get("SOURCE_HOST") or "MIM").strip() or "MIM",
        "source_service": str(request_meta.get("SOURCE_SERVICE") or publication_service).strip() or publication_service,
        "source_instance_id": str(request_meta.get("SOURCE_INSTANCE_ID") or publication_instance).strip() or publication_instance,
        "correlation_id": correlation_id,
        "CorrelationId": correlation_id if tod_action else "",
        "request_id": request_id,
        "RequestId": request_id if tod_action else "",
        "freshness_token": publish_freshness_token,
        "publish_index": request_sequence,
        "objective_id": objective_ref,
        "objective": objective_ref,
        "title": f"Execute bounded {action_display} via TOD",
        "scope": _bounded_action_execution_phrase(action_name),
        "priority": "high",
        "action": action_name,
        "tod_action": tod_action,
        "bridge_request_id": request_id if tod_action else "",
        "capability_name": capability_name,
        "requested_executor": str(getattr(execution, "requested_executor", "tod") or "tod").strip() or "tod",
        "execution_id": execution_id,
        "RequestPath": str(bridge_request_path) if tod_action else "",
        "handoff_endpoint": f"/gateway/capabilities/executions/{execution_id}/handoff",
        "feedback_endpoint": f"/gateway/capabilities/executions/{execution_id}/feedback",
        "telemetry_contract": {
            "surface": "mim_arm_dispatch_telemetry_v1",
            "latest_endpoint": "/mim/arm/dispatch-telemetry/latest",
            "per_dispatch_endpoint": f"/mim/arm/dispatch-telemetry/{request_id}",
            "preferred_feedback_fields": {
                "host_received_timestamp": [
                    "feedback_json.host_received_timestamp",
                    "feedback_json.executor_timestamps.host_received_timestamp",
                    "correlation_json.host_received_timestamp",
                ],
                "host_completed_timestamp": [
                    "feedback_json.host_completed_timestamp",
                    "feedback_json.executor_timestamps.host_completed_timestamp",
                    "correlation_json.host_completed_timestamp",
                ],
            },
        },
        "release_tag": objective.get("release_tag", ""),
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "acceptance_criteria": [
            "TOD retrieves the latest handoff payload for this execution.",
            "TOD posts execution feedback, including executor-originated host_received_timestamp and host_completed_timestamp when available, back to the gateway feedback endpoint.",
            f"Arm status reflects the resulting bounded {action_display} execution outcome.",
        ],
        "constraints": [
            f"Execute only the bounded {action_display} action.",
            "Preserve operator approval and TOD governance semantics.",
            "Do not promote beyond bounded managed access.",
        ],
        "status_snapshot": {
            "arm_online": status.get("arm_online"),
            "current_pose": status.get("current_pose"),
            "mode": status.get("mode"),
            "camera_online": status.get("camera_online"),
            "serial_ready": status.get("serial_ready"),
            "estop_ok": status.get("estop_ok"),
            "tod_execution_allowed": status.get("tod_execution_allowed"),
            "motion_allowed": status.get("motion_allowed"),
        },
        "notes": f"Automatically projected from approved MIM arm {action_display} execution binding.",
    }
    if tod_action:
        request_payload["tod_action_args"] = {
            "RequestId": request_id,
            "RequestPath": str(bridge_request_path),
            "CorrelationId": correlation_id,
            "Action": action_name,
            "CapabilityName": capability_name,
        }
        request_payload["tod_bridge_request"] = {
            "action": action_name,
            "Action": action_name,
            "capability_name": capability_name,
            "CapabilityName": capability_name,
            "execution_lane": "tod_bridge_request",
            "request_id": request_id,
            "RequestId": request_id,
            "request_path": str(bridge_request_path),
            "RequestPath": str(bridge_request_path),
            "correlation_id": correlation_id,
            "CorrelationId": correlation_id,
        }
        bridge_request_path.write_text(json.dumps(bridge_request_payload, indent=2) + "\n", encoding="utf-8")
    request_path.write_text(json.dumps(request_payload, indent=2) + "\n", encoding="utf-8")
    _, request_errors = normalize_and_validate_file(
        request_path,
        message_kind="request",
        service_name=publication_service,
        instance_id=publication_instance,
    )
    if request_errors:
        raise RuntimeError(f"TOD↔MIM contract validation failed for request artifact: {request_errors}")
    _audit_tod_bridge_write(
        event="local_request_write",
        caller="core.routers.mim_arm._publish_tod_bridge_request",
        service_name=publication_service,
        task_id=request_id,
        objective_id=objective_ref,
        artifact_path=request_path,
    )
    request_sha256 = hashlib.sha256(request_path.read_bytes()).hexdigest()

    trigger_meta = _bridge_meta(
        shared_root=shared_root,
        service_name=publication_service,
        instance_id=publication_instance,
    )
    trigger_generated_at = str(trigger_meta.get("EMITTED_AT") or _utcnow()).strip()
    trigger_sequence = int(str(trigger_meta.get("SEQUENCE") or "1").strip() or "1")
    trigger_path = shared_root / "MIM_TO_TOD_TRIGGER.latest.json"
    trigger_payload = {
        "generated_at": trigger_generated_at,
        "emitted_at": trigger_generated_at,
        "packet_type": "shared-trigger-v1",
        "source_actor": "MIM",
        "target_actor": "TOD",
        "source_host": str(trigger_meta.get("SOURCE_HOST") or "MIM").strip() or "MIM",
        "source_service": str(trigger_meta.get("SOURCE_SERVICE") or publication_service).strip() or publication_service,
        "source_instance_id": str(trigger_meta.get("SOURCE_INSTANCE_ID") or publication_instance).strip() or publication_instance,
        "sequence": trigger_sequence,
        "freshness_token": publish_freshness_token,
        "trigger": "task_request_posted",
        "artifact": request_path.name,
        "artifact_path": str(request_path),
        "artifact_sha256": request_sha256,
        "task_id": request_id,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "action_required": "pull_latest_and_ack",
        "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json",
    }
    trigger_path.write_text(json.dumps(trigger_payload, indent=2) + "\n", encoding="utf-8")
    _, trigger_errors = normalize_and_validate_file(
        trigger_path,
        message_kind="trigger",
        service_name=publication_service,
        instance_id=publication_instance,
    )
    if trigger_errors:
        raise RuntimeError(f"TOD↔MIM contract validation failed for trigger artifact: {trigger_errors}")
    _audit_tod_bridge_write(
        event="local_trigger_write",
        caller="core.routers.mim_arm._publish_tod_bridge_request",
        service_name=publication_service,
        task_id=request_id,
        objective_id=objective_ref,
        artifact_path=trigger_path,
    )

    remote_publish_enabled = _env_flag("MIM_ARM_EXECUTION_REMOTE_PUBLISH", default=True)
    remote_publish = {
        "enabled": remote_publish_enabled,
        "attempted": False,
        "succeeded": False,
        "returncode": None,
        "stdout": "",
        "stderr": "",
    }
    if remote_publish_enabled and TOD_REMOTE_PUBLISH_SCRIPT.exists():
        remote_publish["attempted"] = True
        result = subprocess.run(
            [
                sys.executable,
                str(TOD_REMOTE_PUBLISH_SCRIPT),
                "--caller",
                "core.routers.mim_arm._publish_tod_bridge_request",
                "--request-file",
                str(request_path),
                "--bridge-request-file",
                str(bridge_request_path),
                "--trigger-file",
                str(trigger_path),
                "--verify-task-id",
                request_id,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        remote_publish["returncode"] = result.returncode
        remote_publish["stdout"] = result.stdout.strip()
        remote_publish["stderr"] = result.stderr.strip()
        remote_publish["succeeded"] = result.returncode == 0
        publish_output = (result.stdout or result.stderr).strip()
        _audit_tod_bridge_write(
            event="remote_publish_result",
            caller="core.routers.mim_arm._publish_tod_bridge_request",
            service_name=publication_service,
            task_id=request_id,
            objective_id=objective_ref,
            artifact_path=request_path,
            publish_attempted=True,
            publish_succeeded=result.returncode == 0,
            publish_returncode=result.returncode,
            publish_output=publish_output,
        )
        _audit_tod_bridge_write(
            event="remote_publish_result",
            caller="core.routers.mim_arm._publish_tod_bridge_request",
            service_name=publication_service,
            task_id=request_id,
            objective_id=objective_ref,
            artifact_path=trigger_path,
            publish_attempted=True,
            publish_succeeded=result.returncode == 0,
            publish_returncode=result.returncode,
            publish_output=publish_output,
        )

    return {
        "task_id": request_id,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "request_path": str(request_path),
        "trigger_path": str(trigger_path),
        "request_sequence": request_sequence,
        "trigger_sequence": trigger_sequence,
        "local_written": True,
        "remote_publish": remote_publish,
        "dispatch_telemetry": record_dispatch_telemetry_from_publish(
            shared_root=shared_root,
            execution_id=execution_id,
            capability_name=str(getattr(execution, "capability_name", "") or "").strip(),
            execution_lane=str(getattr(execution, "requested_executor", "tod") or "tod").strip() or "tod",
            request_payload=request_payload,
            trigger_payload=trigger_payload,
            request_path=request_path,
            trigger_path=trigger_path,
            remote_publish=remote_publish,
        ),
    }


def _load_mim_arm_env_defaults() -> None:
    if os.getenv("MIM_ARM_SSH_HOST") and os.getenv("MIM_ARM_SSH_HOST_USER"):
        return
    if not MIM_ARM_ENV_FILE.exists():
        return
    for raw_line in MIM_ARM_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def _health_posture() -> dict[str, object]:
    status = "healthy"
    try:
        summary = _mim_health_monitor.get_health_summary()
        if isinstance(summary, dict):
            status = str(summary.get("status") or "healthy").strip().lower() or "healthy"
    except Exception:
        status = "healthy"
    requires_confirmation = status in {"degraded", "critical"}
    if requires_confirmation:
        summary_text = f"Self-health is {status}; live arm execution remains confirmation-gated."
    elif status == "suboptimal":
        summary_text = "Self-health is suboptimal; proposals may proceed but live execution should remain operator-reviewed."
    else:
        summary_text = "Self-health is healthy; bounded arm proposals remain eligible for TOD review."
    return {
        "status": status,
        "requires_confirmation": requires_confirmation,
        "summary": summary_text,
    }


def _bounded_capability_name(action_name: str) -> str:
    return f"mim_arm.execute_{action_name}"


def _step_key(index: int, action_name: str) -> str:
    return f"step_{index + 1}_{action_name}"


def _normalize_composed_steps(raw_steps: list[str]) -> list[str]:
    normalized = [str(item or "").strip() for item in raw_steps]
    normalized = [item for item in normalized if item]
    if not normalized:
        normalized = list(BOUNDED_LIVE_ACTIONS)
    invalid = [item for item in normalized if item not in BOUNDED_LIVE_ACTIONS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_composed_task_step",
                "allowed_steps": list(BOUNDED_LIVE_ACTIONS),
                "invalid_steps": invalid,
            },
        )
    return normalized


def _memory_hygiene_snapshot(*, shared_root: Path = DEFAULT_SHARED_ROOT) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    try:
        raw = _mim_health_monitor.get_health_summary()
        if isinstance(raw, dict):
            summary = raw
    except Exception:
        summary = {}
    task_dir = shared_root / MIM_ARM_COMPOSED_TASK_DIRNAME
    task_files = []
    if task_dir.exists():
        task_files = [item for item in task_dir.glob("*.json") if item.is_file()]
    return {
        "health_status": str(summary.get("status") or "unknown").strip() or "unknown",
        "memory_mb": summary.get("memory_mb"),
        "memory_percent": summary.get("memory_percent"),
        "artifact_file_count": len(task_files),
        "retention_limit": 20,
        "compaction_state": "normal" if len(task_files) <= 20 else "trim_required",
    }


def _composed_task_dir(shared_root: Path) -> Path:
    return shared_root / MIM_ARM_COMPOSED_TASK_DIRNAME


def _composed_task_artifact_path(shared_root: Path, trace_id: str) -> Path:
    return _composed_task_dir(shared_root) / f"{trace_id}.json"


def _latest_composed_task_artifact_path(shared_root: Path) -> Path:
    return shared_root / MIM_ARM_COMPOSED_TASK_ARTIFACT


def _persist_composed_task_snapshot(task: dict[str, Any], *, shared_root: Path = DEFAULT_SHARED_ROOT) -> None:
    trace_id = str(task.get("trace_id") or "").strip()
    if not trace_id:
        return
    task_path = _composed_task_artifact_path(shared_root, trace_id)
    latest_path = _latest_composed_task_artifact_path(shared_root)
    _write_json_artifact(task_path, task)
    _write_json_artifact(latest_path, task)

    task_files = sorted(
        [item for item in _composed_task_dir(shared_root).glob("*.json") if item.is_file()],
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in task_files[20:]:
        try:
            stale.unlink()
        except OSError:
            continue


def _compact_step_attempt(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt_number": int(attempt.get("attempt_number") or 0),
        "execution_id": attempt.get("execution_id"),
        "step_trace_id": str(attempt.get("step_trace_id") or "").strip(),
        "request_id": str(attempt.get("request_id") or "").strip(),
        "task_id": str(attempt.get("task_id") or "").strip(),
        "correlation_id": str(attempt.get("correlation_id") or "").strip(),
        "dispatch_decision": str(attempt.get("dispatch_decision") or "").strip(),
        "status": str(attempt.get("status") or "").strip(),
        "reason": str(attempt.get("reason") or "").strip(),
        "handoff_endpoint": str(attempt.get("handoff_endpoint") or "").strip(),
        "dispatched_at": str(attempt.get("dispatched_at") or _utcnow()).strip(),
    }


def _step_attempt_from_dispatch_response(index: int, action_name: str, response: dict[str, Any]) -> dict[str, Any]:
    execution = _json_dict(response.get("execution"))
    feedback = _json_dict(execution.get("feedback_json"))
    bridge = _json_dict(execution.get("bridge_publication"))
    return {
        "step_key": _step_key(index, action_name),
        "attempt_number": 1,
        "execution_id": execution.get("execution_id"),
        "step_trace_id": str(feedback.get("trace_id") or "").strip(),
        "request_id": str(bridge.get("request_id") or bridge.get("task_id") or "").strip(),
        "task_id": str(bridge.get("task_id") or bridge.get("request_id") or "").strip(),
        "correlation_id": str(bridge.get("correlation_id") or "").strip(),
        "dispatch_decision": str(execution.get("dispatch_decision") or "requires_confirmation").strip(),
        "status": str(execution.get("status") or "pending_confirmation").strip(),
        "reason": str(execution.get("reason") or "").strip(),
        "handoff_endpoint": str(execution.get("handoff_endpoint") or "").strip(),
        "dispatched_at": _utcnow(),
    }


def _new_composed_step(index: int, action_name: str) -> dict[str, Any]:
    return {
        "step_index": index,
        "step_key": _step_key(index, action_name),
        "action": action_name,
        "capability_name": _bounded_capability_name(action_name),
        "status": "planned",
        "dispatch_decision": "",
        "reason": "",
        "request_id": "",
        "task_id": "",
        "correlation_id": "",
        "execution_id": None,
        "step_trace_id": "",
        "retry_count": 0,
        "proof_chain_complete": False,
        "proof_requirements": {},
        "failure_classification": "",
        "attempts": [],
    }


def _apply_attempt_to_step(step: dict[str, Any], attempt: dict[str, Any], *, increment_retry: bool = False) -> dict[str, Any]:
    attempts = [item for item in step.get("attempts", []) if isinstance(item, dict)]
    sanitized = _compact_step_attempt(attempt)
    sanitized["attempt_number"] = len(attempts) + 1
    attempts.append(sanitized)
    step.update(
        {
            "status": sanitized["status"],
            "dispatch_decision": sanitized["dispatch_decision"],
            "reason": sanitized["reason"],
            "request_id": sanitized["request_id"],
            "task_id": sanitized["task_id"],
            "correlation_id": sanitized["correlation_id"],
            "execution_id": sanitized["execution_id"],
            "step_trace_id": sanitized["step_trace_id"],
            "attempts": attempts[-4:],
        }
    )
    if increment_retry:
        step["retry_count"] = int(step.get("retry_count") or 0) + 1
    return step


def _telemetry_source(telemetry: dict[str, Any], kind: str) -> dict[str, Any]:
    for item in telemetry.get("evidence_sources", []) if isinstance(telemetry.get("evidence_sources", []), list) else []:
        payload = _json_dict(item)
        if str(payload.get("kind") or "").strip() == kind:
            return payload
    return {}


def _host_attribution_matches(*, shared_root: Path, request_id: str, task_id: str, correlation_id: str) -> bool:
    payload = _read_json_artifact(shared_root / ARM_HOST_STATE_ARTIFACT)
    if not payload:
        return False
    last_result = _json_dict(payload.get("last_command_result"))
    evidence = _json_dict(payload.get("command_evidence"))
    ids = {value for value in (request_id, task_id, correlation_id) if value}
    if not ids:
        return False
    candidates = {
        str(last_result.get("request_id") or "").strip(),
        str(last_result.get("task_id") or "").strip(),
        str(last_result.get("correlation_id") or "").strip(),
        str(evidence.get("request_id") or "").strip(),
        str(evidence.get("task_id") or "").strip(),
        str(evidence.get("correlation_id") or "").strip(),
        str(payload.get("last_request_id") or "").strip(),
        str(payload.get("last_task_id") or "").strip(),
        str(payload.get("last_correlation_id") or "").strip(),
    }
    candidates.discard("")
    return bool(candidates.intersection(ids))


def _step_proof_from_telemetry(step: dict[str, Any], *, shared_root: Path) -> tuple[dict[str, bool], bool, str]:
    request_id = str(step.get("request_id") or "").strip()
    if not request_id:
        return ({}, False, "")
    telemetry = refresh_dispatch_telemetry_record(shared_root, request_id=request_id)
    if not telemetry:
        return ({}, False, "")

    step["request_id"] = str(telemetry.get("request_id") or request_id).strip()
    step["task_id"] = str(telemetry.get("task_id") or step.get("task_id") or request_id).strip()
    step["correlation_id"] = str(telemetry.get("correlation_id") or step.get("correlation_id") or "").strip()

    ack = _telemetry_source(telemetry, "task_ack_artifact")
    result = _telemetry_source(telemetry, "task_result_artifact")
    publication_boundary = _telemetry_source(telemetry, "publication_boundary")
    host_attribution = _host_attribution_matches(
        shared_root=shared_root,
        request_id=str(step.get("request_id") or "").strip(),
        task_id=str(step.get("task_id") or "").strip(),
        correlation_id=str(step.get("correlation_id") or "").strip(),
    )
    proof_requirements = {
        "dispatch_telemetry_present": True,
        "request_task_correlation_aligned": bool(
            str(telemetry.get("request_id") or "").strip()
            and str(telemetry.get("task_id") or "").strip()
            and str(telemetry.get("correlation_id") or "").strip()
        ),
        "host_received_timestamp_present": bool(str(telemetry.get("host_received_timestamp") or "").strip()),
        "host_completed_timestamp_present": bool(str(telemetry.get("host_completed_timestamp") or "").strip()),
        "tod_ack_result_aligned": bool(result.get("matched") and (ack.get("matched") or result.get("ack_inferred"))),
        "explicit_host_attribution_present": bool(host_attribution),
    }
    proof_chain_complete = bool(publication_boundary.get("matched")) and all(proof_requirements.values())
    completion_status = str(telemetry.get("completion_status") or "pending").strip().lower() or "pending"
    reason = str(telemetry.get("result_reason") or step.get("reason") or "").strip()

    step["proof_requirements"] = proof_requirements
    step["proof_chain_complete"] = proof_chain_complete
    if proof_chain_complete:
        step["status"] = "proved"
    elif completion_status == "completed":
        step["status"] = "completed_unproved"
    elif completion_status == "failed":
        step["status"] = "failed"
    elif str(telemetry.get("dispatch_status") or "").strip() in {"host_received", "completed"}:
        step["status"] = "in_progress"
    step["reason"] = reason
    return proof_requirements, proof_chain_complete, reason


def _classify_step_failure(step: dict[str, Any]) -> str:
    reason = str(step.get("reason") or "").strip().lower()
    if not reason and str(step.get("status") or "").strip() != "failed":
        return ""
    if reason in {"execution_timeout", "transport_dispatch_failed", "failed", "succeeded"}:
        return "retryable_transport"
    if "timeout" in reason or "transport" in reason or "publish" in reason:
        return "retryable_transport"
    if "interrupted" in reason or "stop" in reason:
        return "operator_interrupted"
    if reason:
        return "non_retryable"
    return "retryable_transport"


def _build_operator_summary(task: dict[str, Any]) -> str:
    steps = [item for item in task.get("steps", []) if isinstance(item, dict)]
    if not steps:
        return "No composed arm steps are currently tracked."
    current_index = int(task.get("current_step_index") or 0)
    current_index = max(0, min(current_index, len(steps) - 1))
    current = steps[current_index]
    completed = len([item for item in steps if bool(item.get("proof_chain_complete"))])
    return (
        f"Composed task {task.get('trace_id', '')} is {task.get('status', 'active')} with "
        f"{completed}/{len(steps)} proved steps. Current step is {current.get('action', 'unknown')} "
        f"({current.get('status', 'planned')})."
    )


def _build_operator_commands(task: dict[str, Any]) -> list[dict[str, str]]:
    trace_id = str(task.get("trace_id") or "").strip()
    decision = _json_dict(task.get("decision"))
    if not trace_id:
        return []
    commands = [
        {
            "method": "GET",
            "path": f"/mim/arm/tasks/composed/{trace_id}",
            "purpose": "Review the composed task state and current step proof.",
        }
    ]
    code = str(decision.get("code") or "").strip()
    if code in {"dispatch_next_step", "retry_current_step", "await_current_step_proof", "operator_review_current_step", "await_operator_approval_for_next_step"}:
        commands.append(
            {
                "method": "POST",
                "path": f"/mim/arm/tasks/composed/{trace_id}/advance",
                "purpose": "Refresh proof, then either advance or retry the bounded step if policy allows.",
            }
        )
    return commands


def _reconcile_composed_task(
    task: dict[str, Any],
    *,
    shared_root: Path = DEFAULT_SHARED_ROOT,
    explicit_operator_approval: bool = False,
    allow_retry: bool = True,
) -> dict[str, Any]:
    steps = [item for item in task.get("steps", []) if isinstance(item, dict)]
    if not steps:
        task["status"] = "failed"
        task["decision"] = {"code": "task_missing_steps", "detail": "No composed steps were found."}
        task["operator_summary"] = _build_operator_summary(task)
        task["operator_commands"] = _build_operator_commands(task)
        return task

    current_index = int(task.get("current_step_index") or 0)
    current_index = max(0, min(current_index, len(steps) - 1))
    task["current_step_index"] = current_index
    current = steps[current_index]

    if str(current.get("request_id") or "").strip():
        _, proof_chain_complete, _ = _step_proof_from_telemetry(current, shared_root=shared_root)
        if not proof_chain_complete and str(current.get("status") or "").strip() == "failed":
            current["failure_classification"] = _classify_step_failure(current)

    proved_steps = [item for item in steps if bool(item.get("proof_chain_complete"))]
    task["memory_hygiene"] = _memory_hygiene_snapshot(shared_root=shared_root)

    if len(proved_steps) == len(steps):
        task["status"] = "completed"
        task["current_step_key"] = "completed"
        task["decision"] = {
            "code": "task_completed",
            "detail": "Every bounded step has an ACK/RESULT proof chain and explicit host attribution.",
        }
    elif str(current.get("status") or "").strip() in {"pending_confirmation", "awaiting_review"}:
        task["status"] = "awaiting_operator"
        task["current_step_key"] = str(current.get("step_key") or "").strip()
        task["decision"] = {
            "code": "operator_review_current_step",
            "detail": f"Current step {current.get('action', 'unknown')} is still waiting for explicit operator approval.",
        }
    elif str(current.get("status") or "").strip() == "blocked":
        task["status"] = "awaiting_operator"
        task["current_step_key"] = str(current.get("step_key") or "").strip()
        blocked_reason = str(current.get("reason") or current.get("dispatch_decision") or "execution_blocked").strip()
        task["decision"] = {
            "code": "operator_review_current_step",
            "detail": f"Current step {current.get('action', 'unknown')} is blocked ({blocked_reason}). Review readiness or policy before retrying bounded execution.",
        }
    elif bool(current.get("proof_chain_complete")):
        if current_index + 1 >= len(steps):
            task["status"] = "completed"
            task["current_step_key"] = "completed"
            task["decision"] = {
                "code": "task_completed",
                "detail": "Every bounded step has now been proved complete.",
            }
        elif explicit_operator_approval:
            task["status"] = "active"
            task["current_step_key"] = str(current.get("step_key") or "").strip()
            task["decision"] = {
                "code": "dispatch_next_step",
                "detail": f"Step {current.get('action', 'unknown')} is proved complete; dispatch the next bounded step.",
            }
        else:
            task["status"] = "awaiting_operator"
            task["current_step_key"] = str(current.get("step_key") or "").strip()
            task["decision"] = {
                "code": "await_operator_approval_for_next_step",
                "detail": f"Step {current.get('action', 'unknown')} is proved complete. Explicit approval is still required before the next step dispatch.",
            }
    elif str(current.get("status") or "").strip() == "failed":
        failure_classification = str(current.get("failure_classification") or _classify_step_failure(current)).strip()
        current["failure_classification"] = failure_classification
        retry_budget = int(task.get("max_retry_per_step") or 0)
        retry_count = int(current.get("retry_count") or 0)
        if allow_retry and explicit_operator_approval and failure_classification == "retryable_transport" and retry_count < retry_budget:
            task["status"] = "recovery_pending"
            task["current_step_key"] = str(current.get("step_key") or "").strip()
            task["decision"] = {
                "code": "retry_current_step",
                "detail": f"Retry step {current.get('action', 'unknown')} within the bounded retry budget.",
            }
        else:
            task["status"] = "degraded"
            task["current_step_key"] = str(current.get("step_key") or "").strip()
            task["decision"] = {
                "code": "rollback_safe_home",
                "detail": f"Stop advancing. Review step {current.get('action', 'unknown')} and consider bounded rollback to safe_home.",
            }
    else:
        task["status"] = "active"
        task["current_step_key"] = str(current.get("step_key") or "").strip()
        task["decision"] = {
            "code": "await_current_step_proof",
            "detail": f"Waiting for ACK/RESULT proof and host attribution on step {current.get('action', 'unknown')}.",
        }

    task["steps"] = steps
    task["operator_summary"] = _build_operator_summary(task)
    task["operator_commands"] = _build_operator_commands(task)
    return task


async def _load_execution_orchestration_row(db: AsyncSession, trace_id: str) -> ExecutionTaskOrchestration | None:
    return (
        (
            await db.execute(
                select(ExecutionTaskOrchestration)
                .where(ExecutionTaskOrchestration.trace_id == str(trace_id or "").strip())
                .order_by(ExecutionTaskOrchestration.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def _load_composed_task_from_orchestration(row: ExecutionTaskOrchestration, *, shared_root: Path = DEFAULT_SHARED_ROOT) -> dict[str, Any]:
    metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    composed_task = _json_dict(metadata.get("composed_task"))
    if composed_task:
        return composed_task
    payload = _read_json_artifact(_composed_task_artifact_path(shared_root, row.trace_id))
    return payload if payload else {}


def _apply_composed_task_to_orchestration(
    row: ExecutionTaskOrchestration,
    task: dict[str, Any],
    *,
    base_metadata: dict[str, Any] | None = None,
) -> None:
    steps = [item for item in task.get("steps", []) if isinstance(item, dict)]
    current_index = int(task.get("current_step_index") or 0)
    current_index = max(0, min(current_index, len(steps) - 1)) if steps else 0
    current = steps[current_index] if steps else {}
    row.execution_id = current.get("execution_id") if current else row.execution_id
    row.orchestration_status = str(task.get("status") or "active").strip() or "active"
    row.current_step_key = str(task.get("current_step_key") or current.get("step_key") or "created").strip() or "created"
    row.step_state_json = steps
    row.checkpoint_json = {
        "task_kind": "mim_arm_composed_sequence",
        "current_step_index": current_index,
        "total_steps": len(steps),
        "proved_steps": len([item for item in steps if bool(item.get("proof_chain_complete"))]),
        "latest_request_id": str(current.get("request_id") or "").strip(),
        "latest_task_id": str(current.get("task_id") or "").strip(),
        "latest_correlation_id": str(current.get("correlation_id") or "").strip(),
        "decision": _json_dict(task.get("decision")),
    }
    row.retry_count = sum(int(item.get("retry_count") or 0) for item in steps)
    row.rollback_state_json = {
        "fallback_action": "safe_home",
        "rollback_recommended": str(_json_dict(task.get("decision")).get("code") or "") == "rollback_safe_home",
        "rollback_reason": str(_json_dict(task.get("decision")).get("detail") or "").strip(),
    }
    row.metadata_json = {
        **(row.metadata_json if isinstance(row.metadata_json, dict) else {}),
        **(base_metadata if isinstance(base_metadata, dict) else {}),
        "composed_task": task,
        "operator_summary": str(task.get("operator_summary") or "").strip(),
        "decision": _json_dict(task.get("decision")),
        "memory_hygiene": _json_dict(task.get("memory_hygiene")),
    }


def _build_composed_task_snapshot(
    *,
    trace_id: str,
    request: MimArmComposedTaskRequest,
    first_response: dict[str, Any],
    steps: list[str],
) -> dict[str, Any]:
    task_steps = [_new_composed_step(index, action_name) for index, action_name in enumerate(steps)]
    first_attempt = _step_attempt_from_dispatch_response(0, steps[0], first_response)
    _apply_attempt_to_step(task_steps[0], first_attempt)
    task = {
        "trace_id": trace_id,
        "task_kind": "mim_arm_composed_sequence",
        "created_at": _utcnow(),
        "updated_at": _utcnow(),
        "status": "active",
        "reason": str(request.reason or "").strip(),
        "actor": str(request.actor or "operator").strip() or "operator",
        "explicit_operator_approval": bool(request.explicit_operator_approval),
        "shared_workspace_active": bool(request.shared_workspace_active),
        "max_retry_per_step": int(request.max_retry_per_step or 0),
        "steps": task_steps,
        "current_step_index": 0,
        "current_step_key": task_steps[0]["step_key"],
        "decision": {},
        "operator_summary": "",
        "operator_commands": [],
        "memory_hygiene": {},
        "metadata_json": _json_dict(request.metadata_json),
    }
    return task


def _latest_readiness(shared_root: Path) -> dict[str, object]:
    candidates: list[tuple[datetime, dict]] = []
    for artifact_name in (TOD_TASK_RESULT_ARTIFACT, TOD_COMMAND_STATUS_ARTIFACT):
        payload = _read_json_artifact(shared_root / artifact_name)
        if not payload:
            continue
        generated_at = _parse_timestamp(payload.get("generated_at")) or datetime.min.replace(tzinfo=timezone.utc)
        candidates.append((generated_at, {"artifact_name": artifact_name, "payload": payload}))
    if not candidates:
        return {
            "status": "unknown",
            "detail": "No TOD readiness artifact present.",
            "execution_allowed": False,
            "policy_outcome": "unknown",
            "freshness_state": "unknown",
            "authoritative": False,
            "artifact_name": "",
            "generated_at": "",
        }

    _, selected = sorted(candidates, key=lambda item: item[0], reverse=True)[0]
    payload = _json_dict(selected.get("payload"))
    readiness = _json_dict(payload.get("execution_readiness"))
    if not readiness:
        readiness = _json_dict(_json_dict(payload.get("execution_trace")).get("execution_readiness"))
    return {
        "status": _coerce_status(readiness.get("status"), default="unknown"),
        "detail": str(readiness.get("detail") or "").strip(),
        "execution_allowed": bool(readiness.get("execution_allowed", False)),
        "policy_outcome": _coerce_status(readiness.get("policy_outcome"), default="unknown"),
        "freshness_state": _coerce_status(readiness.get("freshness_state"), default="unknown"),
        "authoritative": bool(readiness.get("authoritative", False)),
        "artifact_name": str(selected.get("artifact_name") or "").strip(),
        "generated_at": str(payload.get("generated_at") or "").strip(),
        "evaluated_action": str(readiness.get("evaluated_action") or "").strip(),
    }


def _catchup_gate(shared_root: Path) -> dict[str, object]:
    payload = _read_json_artifact(shared_root / TOD_CATCHUP_GATE_ARTIFACT)
    if not payload:
        return {
            "gate_pass": False,
            "promotion_ready": False,
            "confidence": "unknown",
            "detail": "No TOD catchup gate artifact present.",
        }
    details = _json_dict(payload.get("details"))
    return {
        "gate_pass": bool(payload.get("gate_pass", False)),
        "promotion_ready": bool(payload.get("promotion_ready", False)),
        "confidence": str(payload.get("confidence") or "unknown").strip() or "unknown",
        "detail": str(details.get("refresh_failure_reason") or details.get("alignment_status") or "").strip(),
        "details": details,
        "generated_at": str(payload.get("generated_at") or "").strip(),
    }


def _build_tod_catchup_summary(catchup: dict[str, object]) -> dict[str, object]:
    details = _json_dict(catchup.get("details"))
    refresh_checks = _json_dict(details.get("refresh_checks"))
    failed_refresh_checks = [
        key for key, value in refresh_checks.items() if value is False
    ]
    return {
        "gate_pass": bool(catchup.get("gate_pass", False)),
        "promotion_ready": bool(catchup.get("promotion_ready", False)),
        "confidence": str(catchup.get("confidence") or "unknown").strip() or "unknown",
        "detail": str(catchup.get("detail") or "").strip(),
        "generated_at": str(catchup.get("generated_at") or "").strip(),
        "aligned": bool(details.get("aligned", False)),
        "refresh_ok": bool(details.get("refresh_ok", False)),
        "refresh_evidence_ok": bool(details.get("refresh_evidence_ok", False)),
        "fresh": bool(details.get("fresh", False)),
        "freshness_age_seconds": details.get("freshness_age_seconds"),
        "freshness_max_age_seconds": details.get("freshness_max_age_seconds"),
        "failed_refresh_checks": failed_refresh_checks,
    }


def _tod_execution_gate_reason(readiness: dict[str, object], catchup: dict[str, object]) -> str:
    freshness_state = _coerce_status(readiness.get("freshness_state"), default="unknown")
    policy_outcome = _coerce_status(readiness.get("policy_outcome"), default="unknown")
    readiness_status = _coerce_status(readiness.get("status"), default="unknown")

    if freshness_state not in {"fresh"}:
        return "readiness_stale"
    if readiness_status not in {"valid", "ready"}:
        return f"readiness_status_{readiness_status}"
    if not bool(readiness.get("authoritative", False)):
        return "readiness_not_authoritative"
    if policy_outcome not in {"allow", "allowed"}:
        return f"policy_outcome_{policy_outcome}"
    if not bool(readiness.get("execution_allowed", False)):
        return "readiness_execution_not_allowed"
    if not bool(catchup.get("gate_pass", False)):
        return "catchup_gate_false"
    return ""


def _persistent_communication_dispatch_gate(shared_root: Path) -> dict[str, object]:
    payload = _read_json_artifact(shared_root / MIM_DECISION_TASK_ARTIFACT)
    communication_escalation = _json_dict(payload.get("communication_escalation"))
    required = bool(communication_escalation.get("required") is True)
    required_cycle_count = int(communication_escalation.get("required_cycle_count", 0) or 0)
    threshold_cycles = int(communication_escalation.get("block_dispatch_threshold_cycles", 3) or 3)
    if required and required_cycle_count > threshold_cycles:
        return {
            "active": True,
            "reason_code": "communication_escalation_persistent",
            "required_cycle_count": required_cycle_count,
            "threshold_cycles": threshold_cycles,
            "detail": str(communication_escalation.get("detail") or "").strip(),
            "code": str(communication_escalation.get("code") or "").strip(),
        }
    return {
        "active": False,
        "reason_code": "",
        "required_cycle_count": required_cycle_count,
        "threshold_cycles": threshold_cycles,
        "detail": str(communication_escalation.get("detail") or "").strip(),
        "code": str(communication_escalation.get("code") or "").strip(),
    }


def load_mim_arm_status_surface(*, shared_root: Path = DEFAULT_SHARED_ROOT) -> dict[str, object]:
    status_payload = _read_json_artifact(shared_root / ARM_STATUS_ARTIFACT)
    host_state_payload = _read_json_artifact(shared_root / ARM_HOST_STATE_ARTIFACT)
    if host_state_payload:
        status_payload = {**status_payload, **host_state_payload}
    diagnostic = _read_json_artifact(shared_root / ARM_DIAGNOSTIC_ARTIFACT)
    readiness = _latest_readiness(shared_root)
    catchup = _catchup_gate(shared_root)
    catchup_summary = _build_tod_catchup_summary(catchup)
    communication_dispatch_gate = _persistent_communication_dispatch_gate(shared_root)
    health = _health_posture()

    process_service = _json_dict(diagnostic.get("process_service"))
    devices = _json_dict(diagnostic.get("devices"))
    connectivity = _json_dict(diagnostic.get("connectivity"))
    likely_root_cause = _json_dict(diagnostic.get("likely_root_cause"))

    direct_servo_states = _json_dict(status_payload.get("servo_states"))
    serial_status = _coerce_bool(status_payload.get("serial_ready"))
    if serial_status is None:
        serial_status = bool(_json_dict(devices.get("serial_controller_port_availability")).get("ok", False))

    app_alive = _coerce_bool(status_payload.get("app_alive"))
    if app_alive is None:
        active_processes = _json_dict(process_service.get("active_processes"))
        app_alive = bool(active_processes.get("ok", False) and str(active_processes.get("stdout") or "").strip())

    arm_online = _coerce_bool(status_payload.get("arm_online"))
    if arm_online is None:
        arm_online = bool(connectivity.get("host_reachable", False))

    camera_online = _coerce_bool(status_payload.get("camera_online"))
    if camera_online is None:
        camera_online = bool(_json_dict(devices.get("camera_device_availability")).get("ok", False))

    # E-stop must be explicit from arm host truth; never infer from status labels.
    estop_ok = _coerce_bool(status_payload.get("estop_ok"))
    estop_supported = _coerce_bool(status_payload.get("estop_supported"))
    estop_status = _coerce_status(
        status_payload.get("estop_status"),
        default="unknown",
    )

    current_pose = _coerce_pose(
        status_payload.get("current_pose")
        or status_payload.get("pose")
        or "unknown"
    )
    mode = str(
        status_payload.get("mode")
        or status_payload.get("current_mode")
        or status_payload.get("active_mode")
        or "unknown"
    ).strip() or "unknown"
    arm_status = _coerce_status(
        status_payload.get("arm_status"),
        default="online" if arm_online else "offline",
    )
    camera_status = _coerce_status(
        status_payload.get("camera_status"),
        default="online" if camera_online else "offline",
    )

    last_command_result = _json_dict(status_payload.get("last_command_result"))
    if not last_command_result:
        recent_command_result = diagnostic.get("recent_command_result")
        if isinstance(recent_command_result, dict):
            last_command_result = recent_command_result
    if not last_command_result:
        last_command_result = {
            "status": readiness.get("policy_outcome"),
            "detail": readiness.get("detail"),
            "evaluated_action": readiness.get("evaluated_action"),
        }
    last_command_status = str(
        status_payload.get("last_command_status")
        or last_command_result.get("status")
        or readiness.get("policy_outcome")
        or "unknown"
    ).strip() or "unknown"

    last_error = status_payload.get("last_error")
    if last_error in {"", None}:
        tod_error = str(readiness.get("detail") or "").strip()
        if str(readiness.get("policy_outcome") or "").strip().lower() == "block":
            last_error = tod_error
        else:
            last_error = str(likely_root_cause.get("summary") or "").strip() or None

    tod_execution_block_reason = _tod_execution_gate_reason(readiness, catchup)
    if not tod_execution_block_reason and bool(communication_dispatch_gate.get("active", False)):
        tod_execution_block_reason = str(communication_dispatch_gate.get("reason_code") or "").strip()
    tod_execution_allowed = tod_execution_block_reason == ""

    motion_block_reasons: list[str] = []
    if not bool(arm_online):
        motion_block_reasons.append("arm_offline")
    if not bool(serial_status):
        motion_block_reasons.append("controller_not_ready")
    if estop_ok is not True:
        if estop_supported is False:
            motion_block_reasons.append("estop_not_supported")
        else:
            motion_block_reasons.append("estop_not_confirmed")
    if not bool(tod_execution_allowed):
        motion_block_reasons.append("tod_execution_not_allowed")
    if bool(health.get("requires_confirmation", False)):
        motion_block_reasons.append("system_health_requires_confirmation")

    motion_allowed = bool(
        tod_execution_allowed
        and arm_online
        and serial_status
        and estop_ok is True
        and not bool(health.get("requires_confirmation", False))
    )
    authoritative_request = load_authoritative_request_status(shared_root=shared_root)

    if authoritative_request:
        last_command_result = {
            **last_command_result,
            "request_id": authoritative_request.get("request_id"),
            "task_id": authoritative_request.get("task_id"),
            "objective_id": authoritative_request.get("objective_id"),
            "decision_code": authoritative_request.get("decision_code"),
            "result_status": authoritative_request.get("result_status"),
            "result_reason": authoritative_request.get("result_reason"),
        }

    return {
        "generated_at": _utcnow(),
        "host_timestamp": str(status_payload.get("host_timestamp") or "").strip(),
        "source_host": str(status_payload.get("source_host") or "").strip(),
        "uptime": status_payload.get("uptime"),
        "arm_state_probe": _json_dict(status_payload.get("arm_state_probe")),
        "camera_probe": _json_dict(status_payload.get("camera_probe")),
        "controller_probe": _json_dict(status_payload.get("controller_probe")),
        "process_probe": _json_dict(status_payload.get("process_probe")),
        "ui_process_alive": _coerce_bool(status_payload.get("ui_process_alive")),
        "controller_connected": _coerce_bool(status_payload.get("controller_connected")),
        "arm_online": arm_online,
        "arm_status": arm_status,
        "app_alive": bool(app_alive),
        "current_pose": current_pose,
        "servo_states": direct_servo_states,
        "camera_online": camera_online,
        "camera_status": camera_status,
        "estop_ok": estop_ok,
        "estop_supported": estop_supported,
        "estop_state_explicit": (estop_ok is not None) or (estop_supported is False),
        "estop_status": estop_status,
        "mode": mode,
        "serial_ready": serial_status,
        "last_command_status": last_command_status,
        "last_command_result": last_command_result,
        "command_evidence": _json_dict(status_payload.get("command_evidence")),
        "last_error": last_error,
        "tod_execution_allowed": tod_execution_allowed,
        "tod_execution_block_reason": tod_execution_block_reason,
        "motion_allowed": motion_allowed,
        "motion_block_reasons": motion_block_reasons,
        "tod_readiness": {
            "status": readiness.get("status"),
            "detail": readiness.get("detail"),
            "policy_outcome": readiness.get("policy_outcome"),
            "freshness_state": readiness.get("freshness_state"),
            "authoritative": readiness.get("authoritative"),
            "artifact_name": readiness.get("artifact_name"),
            "generated_at": readiness.get("generated_at"),
            "gate_pass": catchup.get("gate_pass"),
            "promotion_ready": catchup.get("promotion_ready"),
            "gate_confidence": catchup.get("confidence"),
            "catchup_detail": catchup_summary,
            "communication_dispatch_gate": communication_dispatch_gate,
        },
        "self_health": health,
        "current_request": authoritative_request,
        "source_artifacts": {
            "arm_status": str(shared_root / ARM_STATUS_ARTIFACT),
            "arm_host_state": str(shared_root / ARM_HOST_STATE_ARTIFACT),
            "arm_diagnostic": str(shared_root / ARM_DIAGNOSTIC_ARTIFACT),
            "tod_command_status": str(shared_root / TOD_COMMAND_STATUS_ARTIFACT),
            "tod_task_result": str(shared_root / TOD_TASK_RESULT_ARTIFACT),
            "tod_catchup_gate": str(shared_root / TOD_CATCHUP_GATE_ARTIFACT),
            "mim_decision_task": str(shared_root / MIM_DECISION_TASK_ARTIFACT),
        },
    }


def build_mim_arm_control_readiness(
    status: dict[str, object],
    *,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    host_state_age_seconds = _seconds_since(status.get("host_timestamp"))
    host_state_fresh = host_state_age_seconds is not None and host_state_age_seconds <= 300
    source_artifacts = _json_dict(status.get("source_artifacts"))
    artifact_availability = {
        name: _artifact_exists(path_value)
        for name, path_value in source_artifacts.items()
    }

    access_blockers: list[str] = []
    if not bool(status.get("arm_online", False)):
        access_blockers.append("arm_offline")
    if not bool(status.get("app_alive", False)):
        access_blockers.append("app_not_alive")
    if not str(status.get("source_host") or "").strip():
        access_blockers.append("source_host_unknown")
    if not bool(_json_dict(status.get("arm_state_probe")).get("available", False)):
        access_blockers.append("arm_state_probe_unavailable")
    if not host_state_fresh:
        access_blockers.append("host_state_stale")

    bounded_live_control_ready = bool(
        status.get("arm_online", False)
        and status.get("serial_ready", False)
        and status.get("tod_execution_allowed", False)
        and status.get("estop_ok") is True
    )
    control_blockers = list(dict.fromkeys([str(item) for item in status.get("motion_block_reasons", []) if item]))

    management_blockers: list[str] = []
    if access_blockers:
        management_blockers.extend(access_blockers)
    if not ARM_SYNC_SCRIPT.exists():
        management_blockers.append("sync_script_missing")
    if not ARM_STATUS_SCRIPT.exists():
        management_blockers.append("status_script_missing")
    if not artifact_availability.get("arm_status", False):
        management_blockers.append("arm_status_artifact_missing")
    if not artifact_availability.get("arm_host_state", False):
        management_blockers.append("arm_host_state_artifact_missing")

    access_ready = not access_blockers
    management_ready = not management_blockers
    promotion_caveats: list[str] = []
    if status.get("estop_supported") is False:
        promotion_caveats.append("estop_not_supported_for_promotion")
    catchup_detail = _json_dict(_json_dict(status.get("tod_readiness")).get("catchup_detail"))

    if not access_ready:
        recommended_next_step = "Restore arm host access and fresh probe visibility before attempting control promotion."
    elif not bool(status.get("tod_execution_allowed", False)):
        if str(status.get("tod_execution_block_reason") or "").strip() == "communication_escalation_persistent":
            communication_gate = _json_dict(_json_dict(status.get("tod_readiness")).get("communication_dispatch_gate"))
            required_cycle_count = int(communication_gate.get("required_cycle_count", 0) or 0)
            threshold_cycles = int(communication_gate.get("threshold_cycles", 3) or 3)
            recommended_next_step = (
                "Persistent TOD communication escalation is blocking new dispatch. "
                f"Wait for TOD recovery evidence or clear escalation after more than {threshold_cycles} cycles "
                f"(current {required_cycle_count})."
            )
        elif not bool(catchup_detail.get("refresh_evidence_ok", True)):
            recommended_next_step = "Publish a fresh TOD integration status so canonical refresh evidence matches the current MIM handshake and manifest before bounded live arm dispatch."
        elif not bool(catchup_detail.get("fresh", True)):
            recommended_next_step = "Refresh the TOD catchup status artifacts so the catchup gate becomes fresh before bounded live arm dispatch."
        else:
            recommended_next_step = "Recover the TOD execution bridge and catchup gate so bounded live arm dispatch can proceed."
    elif not bounded_live_control_ready:
        if status.get("estop_supported") is False and status.get("estop_ok") is not True:
            recommended_next_step = "Integrate explicit emergency-stop support before promoting beyond bounded managed access."
        else:
            recommended_next_step = "Clear remaining controller or readiness blockers before first live bounded dispatch."
    elif status.get("estop_supported") is False:
        recommended_next_step = f"Bounded {_bounded_live_actions_phrase()} are available, but integrate explicit emergency-stop support before promoting beyond bounded managed access."
    else:
        recommended_next_step = f"MIM can request bounded {_bounded_live_actions_phrase()} execution once operator approval is supplied."

    return {
        "generated_at": _utcnow(),
        "current_authority": {
            "executor": "tod",
            "operator_approval_required": True,
            "allowed_live_actions": list(BOUNDED_LIVE_ACTIONS),
            "bounded_live_control_route_available": True,
        },
        "access": {
            "ready": access_ready,
            "status": "ready" if access_ready else "blocked",
            "blockers": access_blockers,
            "host_state_age_seconds": host_state_age_seconds,
            "host_state_fresh": host_state_fresh,
            "source_host": status.get("source_host"),
            "arm_state_probe_available": bool(_json_dict(status.get("arm_state_probe")).get("available", False)),
        },
        "control": {
            "ready": bounded_live_control_ready,
            "status": "ready" if bounded_live_control_ready else "blocked",
            "operator_guarded": True,
            "autonomous_motion_ready": bool(status.get("motion_allowed", False)),
            "blockers": control_blockers,
            "tod_execution_allowed": bool(status.get("tod_execution_allowed", False)),
            "tod_execution_block_reason": str(status.get("tod_execution_block_reason") or "").strip(),
            "tod_catchup_detail": catchup_detail,
        },
        "management": {
            "ready": management_ready,
            "status": "ready" if management_ready else "blocked",
            "refresh_supported": ARM_SYNC_SCRIPT.exists() and ARM_STATUS_SCRIPT.exists(),
            "artifact_availability": artifact_availability,
            "blockers": management_blockers,
            "promotion_caveats": promotion_caveats,
        },
        "recommended_next_step": recommended_next_step,
        "status_snapshot": status,
    }


def refresh_mim_arm_management_surface(
    *,
    shared_root: Path = DEFAULT_SHARED_ROOT,
    remote_sync: bool = True,
    skip_remote_run: bool = False,
) -> dict[str, object]:
    shared_root = shared_root.expanduser().resolve()
    _load_mim_arm_env_defaults()
    command_results: list[dict[str, object]] = []

    if remote_sync:
        arm_host = os.environ.get("MIM_ARM_SSH_HOST", "192.168.1.90")
        sync_command = [sys.executable, str(ARM_SYNC_SCRIPT), "--local-output", str(shared_root / ARM_HOST_STATE_ARTIFACT)]
        if skip_remote_run:
            sync_command.append("--skip-remote-run")
        sync_result = subprocess.run(sync_command, capture_output=True, text=True, check=False)
        command_results.append(
            {
                "name": "sync_mim_arm_host_state",
                "command": sync_command,
                "stdout": sync_result.stdout.strip(),
                "stderr": sync_result.stderr.strip(),
                "returncode": sync_result.returncode,
            }
        )
        if sync_result.returncode != 0:
            # SSH-based sync failed.  Fall back to generating host state locally by
            # probing the arm host's HTTP API.  This handles the case where SSH auth
            # is unavailable (no private key, etc.) but the arm app is reachable.
            http_fallback_command = [
                sys.executable,
                str(ARM_SYNC_SCRIPT),
                "--local-output", str(shared_root / ARM_HOST_STATE_ARTIFACT),
                "--http-fallback",
                "--host", arm_host,
            ]
            fallback_result = subprocess.run(http_fallback_command, capture_output=True, text=True, check=False)
            command_results.append(
                {
                    "name": "sync_mim_arm_host_state_http_fallback",
                    "command": http_fallback_command,
                    "stdout": fallback_result.stdout.strip(),
                    "stderr": fallback_result.stderr.strip(),
                    "returncode": fallback_result.returncode,
                }
            )

    status_command = [
        sys.executable,
        str(ARM_STATUS_SCRIPT),
        "--shared-root",
        str(shared_root),
        "--output",
        str((shared_root / ARM_STATUS_ARTIFACT).resolve()),
    ]
    status_result = subprocess.run(status_command, capture_output=True, text=True, check=True)
    command_results.append(
        {
            "name": "generate_mim_arm_status",
            "command": status_command,
            "stdout": status_result.stdout.strip(),
            "stderr": status_result.stderr.strip(),
            "returncode": status_result.returncode,
        }
    )

    status = load_mim_arm_status_surface(shared_root=shared_root)
    readiness = build_mim_arm_control_readiness(status, shared_root=shared_root)
    refresh_payload = {
        "generated_at": _utcnow(),
        "remote_sync": remote_sync,
        "skip_remote_run": skip_remote_run,
        "commands": command_results,
        "readiness": {
            "access": readiness.get("access"),
            "control": readiness.get("control"),
            "management": readiness.get("management"),
            "recommended_next_step": readiness.get("recommended_next_step"),
        },
    }
    _write_json_artifact(shared_root / ARM_CONTROL_READINESS_ARTIFACT, readiness)
    _write_json_artifact(shared_root / ARM_REFRESH_STATUS_ARTIFACT, refresh_payload)
    return {
        "status": status,
        "control_readiness": readiness,
        "refresh": refresh_payload,
    }

def _arm_hard_safety_signal(
    *,
    status: dict[str, object],
    shared_workspace_active: bool,
) -> dict[str, object]:
    if shared_workspace_active:
        return {
            "active": True,
            "code": "user_action_safety_risk",
            "precedence": "hard_safety_escalation",
            "reason": "Shared workspace is active; physical motion is blocked unless an operator explicitly approves it.",
        }
    if status.get("estop_ok") is False:
        return {
            "active": True,
            "code": "user_action_safety_risk",
            "precedence": "hard_safety_escalation",
            "reason": "Emergency stop is not clear; physical motion is blocked unless an operator explicitly approves it.",
        }
    return {
        "active": False,
        "code": "",
        "precedence": "",
        "reason": "",
    }


def _arm_execution_availability_block(status: dict[str, object]) -> dict[str, object]:
    if not bool(status.get("arm_online", False)):
        return {
            "active": True,
            "reason": "Arm host is offline; TOD dispatch remains blocked until live state is restored.",
        }
    if not bool(status.get("serial_ready", False)):
        return {
            "active": True,
            "reason": "Arm controller is not connected; TOD dispatch remains blocked until controller readiness returns.",
        }
    if status.get("estop_ok") is not True:
        if status.get("estop_supported") is False:
            return {
                "active": True,
                "reason": "Emergency-stop capability is unsupported on arm host; TOD dispatch remains blocked.",
            }
        return {
            "active": True,
            "reason": "Emergency-stop state is not explicitly confirmed clear; TOD dispatch remains blocked.",
        }
    if not bool(status.get("tod_execution_allowed", False)):
        tod_reason = str(status.get("tod_execution_block_reason") or "tod_execution_not_allowed").strip()
        return {
            "active": True,
            "reason": f"TOD readiness is blocked ({tod_reason}); bounded arm execution cannot dispatch yet.",
        }
    return {
        "active": False,
        "reason": "",
    }


def _arm_governance_summary(
    *,
    explicit_operator_approval: bool,
    hard_safety_signal: dict[str, object],
    health_posture: dict[str, object],
    action_name: str = "safe_home",
) -> dict[str, object]:
    signal_codes: list[str] = []
    signal_notes: list[str] = []
    primary_signal = "benign_healthy_auto_execution"
    applied_reason = "operator_approval_required"
    applied_outcome = "requires_confirmation"
    action_display = _action_display_name(action_name)

    if bool(hard_safety_signal.get("active", False)):
        signal_codes.append(str(hard_safety_signal.get("code") or "").strip())
        signal_notes.append(str(hard_safety_signal.get("reason") or "").strip())
        primary_signal = "hard_safety_escalation"
        applied_reason = "user_action_safety_requires_inquiry"
        applied_outcome = "blocked"

    if bool(health_posture.get("requires_confirmation", False)):
        signal_codes.append("system_health_degraded")
        signal_notes.append(str(health_posture.get("summary") or "").strip())
        if primary_signal != "hard_safety_escalation":
            primary_signal = "degraded_health_confirmation"
            applied_reason = "system_health_degraded"
            applied_outcome = "requires_confirmation"

    if explicit_operator_approval:
        primary_signal = "explicit_operator_approval"
        applied_reason = "explicit_operator_approval"
        applied_outcome = "auto_execute"

    if signal_notes:
        summary = signal_notes[0]
        if len(signal_notes) > 1:
            summary = f"{signal_notes[0]} Additionally: {'; '.join(signal_notes[1:])}."
    elif explicit_operator_approval:
        summary = f"Operator explicitly approved bounded {action_display} execution through TOD."
    else:
        summary = f"Bounded {action_display} execution requires explicit operator approval before TOD dispatch."

    return {
        "applied_reason": applied_reason,
        "applied_outcome": applied_outcome,
        "primary_signal": primary_signal,
        "signal_codes": [code for code in signal_codes if code],
        "precedence_order": list(gateway_router.GATEWAY_GOVERNANCE_PRECEDENCE),
        "system_health_status": str(health_posture.get("status") or "healthy").strip() or "healthy",
        "summary": summary,
        "explicit_operator_approval": explicit_operator_approval,
    }


def _build_bounded_pose_event_and_resolution(
    *,
    payload: MimArmExecuteSafeHomeRequest,
    status: dict[str, object],
    action_name: str,
    capability_name: str,
) -> tuple[InputEvent, InputEventResolution]:
    health_posture = _json_dict(status.get("self_health"))
    availability_block = _arm_execution_availability_block(status)
    hard_safety_signal = _arm_hard_safety_signal(
        status=status,
        shared_workspace_active=bool(payload.shared_workspace_active),
    )
    action_display = _action_display_name(action_name)
    governance = _arm_governance_summary(
        explicit_operator_approval=bool(payload.explicit_operator_approval),
        hard_safety_signal=hard_safety_signal,
        health_posture=health_posture,
        action_name=action_name,
    )

    if bool(availability_block.get("active", False)):
        outcome = "blocked"
        safety_decision = "blocked"
        reason = "execution_readiness_blocked"
        clarification_prompt = str(availability_block.get("reason") or "").strip()
    elif payload.explicit_operator_approval:
        outcome = "auto_execute"
        safety_decision = "auto_execute"
        reason = "explicit_operator_approval"
        clarification_prompt = ""
    elif bool(hard_safety_signal.get("active", False)):
        outcome = "blocked"
        safety_decision = "blocked"
        reason = "user_action_safety_requires_inquiry"
        clarification_prompt = str(hard_safety_signal.get("reason") or "").strip()
    elif bool(health_posture.get("requires_confirmation", False)):
        outcome = "requires_confirmation"
        safety_decision = "requires_confirmation"
        reason = "system_health_degraded"
        clarification_prompt = str(health_posture.get("summary") or "").strip()
    else:
        outcome = "requires_confirmation"
        safety_decision = "requires_confirmation"
        reason = "operator_approval_required"
        clarification_prompt = f"Bounded {action_display} execution requires explicit operator approval before TOD dispatch."

    escalation_reasons: list[str] = []
    if bool(hard_safety_signal.get("active", False)):
        escalation_reasons.append("user_action_safety_risk")
    if bool(health_posture.get("requires_confirmation", False)):
        escalation_reasons.append("system_health_degraded")

    event = InputEvent(
        source="mim_arm",
        raw_input=f"execute {action_name}",
        parsed_intent="execute_capability",
        confidence=0.99,
        target_system="tod",
        requested_goal=f"mim_arm:execute_{action_name}",
        safety_flags=["bounded", "physical", "tod_executor", "operator_guarded"],
        metadata_json={
            "capability": capability_name,
            "action": action_name,
            "shared_workspace_active": bool(payload.shared_workspace_active),
            "explicit_operator_approval": bool(payload.explicit_operator_approval),
            "status_snapshot": {
                "arm_online": status.get("arm_online"),
                "current_pose": status.get("current_pose"),
                "mode": status.get("mode"),
                "estop_ok": status.get("estop_ok"),
                "motion_allowed": status.get("motion_allowed"),
                "tod_execution_allowed": status.get("tod_execution_allowed"),
            },
            "metadata_json": payload.metadata_json,
        },
        normalized=True,
    )

    resolution = InputEventResolution(
        input_event_id=0,
        internal_intent="execute_capability",
        confidence_tier="high",
        outcome=outcome,
        resolution_status=outcome,
        safety_decision=safety_decision,
        reason=reason,
        clarification_prompt=clarification_prompt,
        escalation_reasons=escalation_reasons,
        capability_name=capability_name,
        capability_registered=True,
        capability_enabled=True,
        goal_id=None,
        proposed_goal_description=_bounded_action_execution_phrase(action_name),
        proposed_actions=[
            {
                "step": 1,
                "action_type": "execute_capability",
                "capability": capability_name,
                "details": _bounded_action_execution_phrase(action_name),
            }
        ],
        metadata_json={
            "source": "mim_arm",
            "governance": governance,
            "arm_execution": {
                "action": action_name,
                "shared_workspace_active": bool(payload.shared_workspace_active),
                "explicit_operator_approval": bool(payload.explicit_operator_approval),
                "availability_block": availability_block,
                "health_posture": health_posture,
                "hard_safety_signal": hard_safety_signal,
                "status_snapshot": {
                    "arm_online": status.get("arm_online"),
                    "current_pose": status.get("current_pose"),
                    "mode": status.get("mode"),
                    "camera_online": status.get("camera_online"),
                    "serial_ready": status.get("serial_ready"),
                    "estop_ok": status.get("estop_ok"),
                    "tod_execution_allowed": status.get("tod_execution_allowed"),
                    "motion_allowed": status.get("motion_allowed"),
                },
            },
            "operator_reason": str(payload.reason or "").strip(),
            "metadata_json": payload.metadata_json,
        },
    )
    return event, resolution


def _proposal_reason(action_name: str, status: dict[str, object]) -> str:
    return (
        f"Proposal for '{action_name}': review the posture below and approve the "
        f"lowest-risk first live motion before live dispatch is enabled.  "
        f"arm_online={status.get('arm_online')}, "
        f"estop_ok={status.get('estop_ok')}, "
        f"tod_execution_allowed={status.get('tod_execution_allowed')}."
    )


def build_mim_arm_proposal(
    *,
    action_name: str,
    capability_name: str,
    target_pose: str | None = None,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, object]:
    status = load_mim_arm_status_surface(shared_root=shared_root)
    health = _health_posture()
    safety_posture = {
        "arm_online": bool(status.get("arm_online", False)),
        "camera_online": bool(status.get("camera_online", False)),
        "serial_ready": bool(status.get("serial_ready", False)),
        "estop_ok": status.get("estop_ok"),
        "tod_execution_allowed": bool(status.get("tod_execution_allowed", False)),
        "motion_allowed": bool(status.get("motion_allowed", False)),
    }
    return {
        "proposal_id": f"{capability_name.replace('.', '_')}_proposal",
        "capability_name": capability_name,
        "stage": "proposal_only",
        "action": action_name,
        "proposal": {
            "target_pose": target_pose or "",
            "requested_executor": "tod",
            "dispatch_allowed": False,
        },
        "reasoning": _proposal_reason(action_name, status),
        "health_posture": health,
        "safety_posture": safety_posture,
        "operator_approval_required": True,
        "live_dispatch_allowed": False,
        "status_snapshot": status,
    }

def list_mim_arm_capability_definitions() -> list[dict[str, object]]:
    return [dict(item) for item in MIM_ARM_CAPABILITY_DEFINITIONS]


def get_mim_arm_execution_target_profile(
    *,
    shared_root: Path = DEFAULT_SHARED_ROOT,
    status: dict[str, object] | None = None,
    hardware_transport_enabled: bool | None = None,
) -> dict[str, object]:
    status_surface = status if isinstance(status, dict) else load_mim_arm_status_surface(shared_root=shared_root)
    _load_mim_arm_env_defaults()
    transport_enabled = _env_flag("MIM_ARM_EXECUTION_ENABLE", default=True) if hardware_transport_enabled is None else bool(hardware_transport_enabled)
    return build_execution_target_profile(
        target=TARGET_MIM_ARM,
        shared_root=shared_root,
        status_surface=status_surface,
        hardware_transport_enabled=transport_enabled,
    )


def submit_mim_arm_execution_request(
    *,
    request: dict[str, object],
    shared_root: Path = DEFAULT_SHARED_ROOT,
    status: dict[str, object] | None = None,
    hardware_transport_enabled: bool | None = None,
) -> dict[str, object]:
    status_surface = status if isinstance(status, dict) else load_mim_arm_status_surface(shared_root=shared_root)
    _load_mim_arm_env_defaults()
    transport_enabled = _env_flag("MIM_ARM_EXECUTION_ENABLE", default=True) if hardware_transport_enabled is None else bool(hardware_transport_enabled)
    return submit_execution_request(
        request=request,
        shared_root=shared_root,
        expected_target=TARGET_MIM_ARM,
        execution_mode="mim_arm",
        status_surface=status_surface,
        hardware_transport_enabled=transport_enabled,
    )


async def _ensure_mim_arm_capability(db: AsyncSession, definition: dict[str, object]) -> CapabilityRegistration:
    capability_name = str(definition.get("capability_name") or "").strip()
    row = (
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
    if row:
        row.category = str(definition.get("category") or row.category).strip() or row.category
        row.description = str(definition.get("description") or row.description).strip()
        row.requires_confirmation = bool(definition.get("requires_confirmation", row.requires_confirmation))
        row.enabled = bool(definition.get("enabled", row.enabled))
        row.safety_policy = _json_dict(definition.get("safety_policy"))
        return row

    row = CapabilityRegistration(
        capability_name=capability_name,
        category=str(definition.get("category") or "diagnostic").strip() or "diagnostic",
        description=str(definition.get("description") or "").strip(),
        requires_confirmation=bool(definition.get("requires_confirmation", False)),
        enabled=bool(definition.get("enabled", True)),
        safety_policy=_json_dict(definition.get("safety_policy")),
    )
    db.add(row)
    await db.flush()
    return row


@router.get("/status")
def get_mim_arm_status() -> dict[str, object]:
    return load_mim_arm_status_surface()


@router.get("/control-readiness")
def get_mim_arm_control_readiness() -> dict[str, object]:
    status = load_mim_arm_status_surface()
    return build_mim_arm_control_readiness(status)


@router.get("/pose")
def get_mim_arm_pose() -> dict[str, object]:
    status = load_mim_arm_status_surface()
    return {
        "arm_online": status.get("arm_online"),
        "current_pose": status.get("current_pose"),
        "mode": status.get("mode"),
        "servo_states": status.get("servo_states"),
        "serial_ready": status.get("serial_ready"),
    }


@router.get("/camera-state")
def get_mim_arm_camera_state() -> dict[str, object]:
    status = load_mim_arm_status_surface()
    return {
        "arm_online": status.get("arm_online"),
        "camera_online": status.get("camera_online"),
        "camera_status": status.get("camera_status"),
    }


@router.get("/last-execution")
def get_mim_arm_last_execution() -> dict[str, object]:
    status = load_mim_arm_status_surface()
    return {
        "last_command_status": status.get("last_command_status"),
        "last_command_result": status.get("last_command_result"),
        "last_error": status.get("last_error"),
        "tod_readiness": status.get("tod_readiness"),
    }


@router.get("/proposals/safe-home")
def propose_safe_home() -> dict[str, object]:
    return build_mim_arm_proposal(
        action_name="safe_home",
        capability_name="mim_arm.propose_safe_home",
        target_pose="safe_home",
    )


@router.get("/proposals/scan-pose")
def propose_scan_pose() -> dict[str, object]:
    return build_mim_arm_proposal(
        action_name="scan_pose",
        capability_name="mim_arm.propose_scan_pose",
        target_pose="scan_pose",
    )


@router.get("/proposals/capture-frame")
def propose_capture_frame() -> dict[str, object]:
    return build_mim_arm_proposal(
        action_name="capture_frame",
        capability_name="mim_arm.propose_capture_frame",
    )


@router.get("/capabilities")
def get_mim_arm_capabilities() -> dict[str, object]:
    return {
        "stage": "read_only_awareness_and_proposal_only",
        "capabilities": list_mim_arm_capability_definitions(),
    }


@router.get("/execution-target")
def get_mim_arm_execution_target() -> dict[str, object]:
    status = load_mim_arm_status_surface()
    return get_mim_arm_execution_target_profile(status=status)


@router.post("/execution-lane/requests")
def post_mim_arm_execution_request(
    payload: MimArmExecutionLaneRequest,
) -> dict[str, object]:
    status = load_mim_arm_status_surface()
    request = payload.model_dump()
    submission = submit_mim_arm_execution_request(request=request, status=status)
    return {
        "target_profile": get_mim_arm_execution_target_profile(status=status),
        "submission": submission,
    }


async def _execute_bounded_pose(
    *,
    payload: MimArmExecuteSafeHomeRequest,
    db: AsyncSession,
    action_name: str,
    capability_name: str,
) -> dict[str, object]:
    status = load_mim_arm_status_surface()
    event, resolution = _build_bounded_pose_event_and_resolution(
        payload=payload,
        status=status,
        action_name=action_name,
        capability_name=capability_name,
    )

    db.add(event)
    await db.flush()
    resolution.input_event_id = int(event.id)
    db.add(resolution)
    await db.flush()

    execution = await gateway_router._create_or_update_execution_binding(
        event=event,
        resolution=resolution,
        capability_name=capability_name,
        db=db,
        force_dispatch=bool(payload.explicit_operator_approval)
        and str(resolution.outcome or "").strip() == "auto_execute",
        arguments_json={
            "target_pose": action_name,
            "action": action_name,
            "bounded_execution": True,
            "shared_workspace_active": bool(payload.shared_workspace_active),
            "explicit_operator_approval": bool(payload.explicit_operator_approval),
            "tod_execution_allowed": bool(status.get("tod_execution_allowed", False)),
        },
        safety_mode="operator_guarded",
        requested_executor="tod",
    )

    bridge_publication = None
    if (
        execution is not None
        and str(getattr(execution, "dispatch_decision", "") or "").strip() == "auto_dispatch"
        and str(getattr(execution, "status", "") or "").strip() == "dispatched"
    ):
        bridge_publication = publish_mim_arm_execution_to_tod(
            execution=execution,
            status=status,
            shared_root=DEFAULT_SHARED_ROOT,
        )
        execution.feedback_json = {
            **(execution.feedback_json if isinstance(execution.feedback_json, dict) else {}),
            "tod_bridge_publication": bridge_publication,
        }
        if str(getattr(execution, "trace_id", "") or "").strip():
            telemetry_payload = bridge_publication.get("dispatch_telemetry") if isinstance(bridge_publication, dict) else {}
            await append_execution_trace_event(
                db=db,
                trace_id=str(getattr(execution, "trace_id", "") or "").strip(),
                execution_id=int(getattr(execution, "id", 0) or 0),
                intent_id=None,
                event_type="dispatch_telemetry_emitted",
                event_stage="dispatch_published",
                causality_role="effect",
                summary="Dispatch-authoritative telemetry emitted for bounded MIM ARM execution.",
                payload_json=telemetry_payload if isinstance(telemetry_payload, dict) else {},
            )
        await db.flush()

    await db.commit()

    handoff_endpoint = (
        f"/gateway/capabilities/executions/{execution.id}/handoff"
        if execution is not None and getattr(execution, "id", None) is not None
        else ""
    )
    return {
        "input_event_id": event.id,
        "resolution": {
            "resolution_id": resolution.id,
            "outcome": resolution.outcome,
            "reason": resolution.reason,
            "clarification_prompt": resolution.clarification_prompt,
            "escalation_reasons": resolution.escalation_reasons,
            "metadata_json": resolution.metadata_json,
        },
        "execution": {
            "execution_id": execution.id if execution is not None else None,
            "capability_name": getattr(execution, "capability_name", capability_name),
            "requested_executor": getattr(execution, "requested_executor", "tod"),
            "dispatch_decision": getattr(execution, "dispatch_decision", "requires_confirmation"),
            "status": getattr(execution, "status", "pending_confirmation"),
            "reason": getattr(execution, "reason", resolution.reason),
            "feedback_json": getattr(execution, "feedback_json", {}),
            "bridge_publication": bridge_publication,
            "handoff_endpoint": handoff_endpoint,
        },
    }


@router.post("/executions/safe-home")
async def execute_safe_home(
    payload: MimArmExecuteSafeHomeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    return await _execute_bounded_pose(
        payload=payload,
        db=db,
        action_name="safe_home",
        capability_name="mim_arm.execute_safe_home",
    )


@router.post("/executions/scan-pose")
async def execute_scan_pose(
    payload: MimArmExecuteSafeHomeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    return await _execute_bounded_pose(
        payload=payload,
        db=db,
        action_name="scan_pose",
        capability_name="mim_arm.execute_scan_pose",
    )


@router.post("/executions/capture-frame")
async def execute_capture_frame(
    payload: MimArmExecuteSafeHomeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    return await _execute_bounded_pose(
        payload=payload,
        db=db,
        action_name="capture_frame",
        capability_name="mim_arm.execute_capture_frame",
    )


@router.get("/dispatch-telemetry/latest")
def get_latest_dispatch_telemetry() -> dict[str, object]:
    payload = refresh_dispatch_telemetry_record(DEFAULT_SHARED_ROOT)
    if not payload:
        raise HTTPException(status_code=404, detail="dispatch telemetry not found")
    return payload


@router.get("/dispatch-telemetry/{request_id}")
def get_dispatch_telemetry(request_id: str) -> dict[str, object]:
    payload = refresh_dispatch_telemetry_record(DEFAULT_SHARED_ROOT, request_id=str(request_id or "").strip())
    if not payload:
        raise HTTPException(status_code=404, detail="dispatch telemetry not found")
    return payload


@router.post("/tasks/composed")
async def create_composed_task(
    payload: MimArmComposedTaskRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    steps = _normalize_composed_steps(payload.steps)
    first_action = steps[0]
    first_response = await _execute_bounded_pose(
        payload=MimArmExecuteSafeHomeRequest(
            actor=payload.actor,
            reason=payload.reason,
            explicit_operator_approval=payload.explicit_operator_approval,
            shared_workspace_active=payload.shared_workspace_active,
            metadata_json={
                **_json_dict(payload.metadata_json),
                "composed_task": {"steps": steps, "max_retry_per_step": int(payload.max_retry_per_step or 0)},
            },
        ),
        db=db,
        action_name=first_action,
        capability_name=_bounded_capability_name(first_action),
    )

    execution = _json_dict(first_response.get("execution"))
    feedback = _json_dict(execution.get("feedback_json"))
    trace_id = str(feedback.get("trace_id") or "").strip()
    if not trace_id:
        raise HTTPException(status_code=500, detail="composed_task_trace_id_missing")
    row = await _load_execution_orchestration_row(db, trace_id)
    if row is None:
        raise HTTPException(status_code=500, detail="composed_task_orchestration_missing")

    task = _build_composed_task_snapshot(
        trace_id=trace_id,
        request=payload,
        first_response=first_response,
        steps=steps,
    )
    task = _reconcile_composed_task(
        task,
        shared_root=DEFAULT_SHARED_ROOT,
        explicit_operator_approval=bool(payload.explicit_operator_approval),
        allow_retry=True,
    )
    _apply_composed_task_to_orchestration(
        row,
        task,
        base_metadata={
            "composed_task_created_by": str(payload.actor or "operator").strip() or "operator",
            "composed_task_reason": str(payload.reason or "").strip(),
        },
    )
    await append_execution_trace_event(
        db=db,
        trace_id=trace_id,
        execution_id=int(execution.get("execution_id") or 0) or None,
        intent_id=row.intent_id,
        event_type="composed_task_created",
        event_stage="orchestration",
        causality_role="effect",
        summary=f"Created composed bounded arm task with {len(steps)} steps.",
        payload_json={
            "steps": steps,
            "decision": _json_dict(task.get("decision")),
            "max_retry_per_step": int(payload.max_retry_per_step or 0),
        },
    )
    await db.commit()
    _persist_composed_task_snapshot(task, shared_root=DEFAULT_SHARED_ROOT)
    return {
        "task": task,
        "decision": _json_dict(task.get("decision")),
        "operator_summary": str(task.get("operator_summary") or "").strip(),
        "operator_commands": task.get("operator_commands", []),
        "orchestration": to_execution_task_orchestration_out(row),
    }


@router.get("/tasks/composed/{trace_id}")
async def get_composed_task(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = await _load_execution_orchestration_row(db, trace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="composed_task_not_found")
    task = _load_composed_task_from_orchestration(row, shared_root=DEFAULT_SHARED_ROOT)
    if not task:
        raise HTTPException(status_code=404, detail="composed_task_state_missing")
    task = _reconcile_composed_task(task, shared_root=DEFAULT_SHARED_ROOT, explicit_operator_approval=False, allow_retry=False)
    _apply_composed_task_to_orchestration(row, task)
    await db.commit()
    _persist_composed_task_snapshot(task, shared_root=DEFAULT_SHARED_ROOT)
    return {
        "task": task,
        "decision": _json_dict(task.get("decision")),
        "operator_summary": str(task.get("operator_summary") or "").strip(),
        "operator_commands": task.get("operator_commands", []),
        "orchestration": to_execution_task_orchestration_out(row),
    }


@router.get("/tasks/composed/{trace_id}/decision")
async def get_composed_task_decision(
    trace_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    payload = await get_composed_task(trace_id=trace_id, db=db)
    task = _json_dict(payload.get("task"))
    return {
        "trace_id": trace_id,
        "decision": _json_dict(task.get("decision")),
        "operator_summary": str(task.get("operator_summary") or "").strip(),
        "memory_hygiene": _json_dict(task.get("memory_hygiene")),
    }


@router.post("/tasks/composed/{trace_id}/advance")
async def advance_composed_task(
    trace_id: str,
    payload: MimArmComposedTaskAdvanceRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    row = await _load_execution_orchestration_row(db, trace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="composed_task_not_found")
    task = _load_composed_task_from_orchestration(row, shared_root=DEFAULT_SHARED_ROOT)
    if not task:
        raise HTTPException(status_code=404, detail="composed_task_state_missing")

    explicit_operator_approval = bool(payload.explicit_operator_approval or task.get("explicit_operator_approval"))
    task = _reconcile_composed_task(
        task,
        shared_root=DEFAULT_SHARED_ROOT,
        explicit_operator_approval=explicit_operator_approval,
        allow_retry=bool(payload.allow_retry),
    )

    decision = _json_dict(task.get("decision"))
    decision_code = str(decision.get("code") or "").strip()
    steps = [item for item in task.get("steps", []) if isinstance(item, dict)]
    current_index = int(task.get("current_step_index") or 0)
    current_index = max(0, min(current_index, len(steps) - 1)) if steps else 0

    if decision_code == "dispatch_next_step" and current_index + 1 < len(steps):
        next_index = current_index + 1
        next_step = steps[next_index]
        next_action = str(next_step.get("action") or "").strip()
        response = await _execute_bounded_pose(
            payload=MimArmExecuteSafeHomeRequest(
                actor=payload.actor,
                reason=payload.reason or f"advance composed task {trace_id}",
                explicit_operator_approval=explicit_operator_approval,
                shared_workspace_active=bool(task.get("shared_workspace_active")),
                metadata_json={
                    **_json_dict(task.get("metadata_json")),
                    **_json_dict(payload.metadata_json),
                    "composed_task": {"trace_id": trace_id, "step_index": next_index, "step_key": next_step.get("step_key")},
                },
            ),
            db=db,
            action_name=next_action,
            capability_name=_bounded_capability_name(next_action),
        )
        attempt = _step_attempt_from_dispatch_response(next_index, next_action, response)
        _apply_attempt_to_step(next_step, attempt)
        task["current_step_index"] = next_index
        task["current_step_key"] = str(next_step.get("step_key") or "").strip()
    elif decision_code == "retry_current_step" and steps:
        current_step = steps[current_index]
        current_action = str(current_step.get("action") or "").strip()
        response = await _execute_bounded_pose(
            payload=MimArmExecuteSafeHomeRequest(
                actor=payload.actor,
                reason=payload.reason or f"retry composed task {trace_id}",
                explicit_operator_approval=explicit_operator_approval,
                shared_workspace_active=bool(task.get("shared_workspace_active")),
                metadata_json={
                    **_json_dict(task.get("metadata_json")),
                    **_json_dict(payload.metadata_json),
                    "composed_task": {"trace_id": trace_id, "step_index": current_index, "step_key": current_step.get("step_key"), "retry": True},
                },
            ),
            db=db,
            action_name=current_action,
            capability_name=_bounded_capability_name(current_action),
        )
        attempt = _step_attempt_from_dispatch_response(current_index, current_action, response)
        _apply_attempt_to_step(current_step, attempt, increment_retry=True)

    task["updated_at"] = _utcnow()
    task = _reconcile_composed_task(
        task,
        shared_root=DEFAULT_SHARED_ROOT,
        explicit_operator_approval=explicit_operator_approval,
        allow_retry=bool(payload.allow_retry),
    )
    _apply_composed_task_to_orchestration(
        row,
        task,
        base_metadata={
            "composed_task_last_actor": str(payload.actor or "operator").strip() or "operator",
            "composed_task_last_reason": str(payload.reason or "").strip(),
        },
    )
    await append_execution_trace_event(
        db=db,
        trace_id=trace_id,
        execution_id=row.execution_id,
        intent_id=row.intent_id,
        event_type="composed_task_advanced",
        event_stage="orchestration",
        causality_role="effect",
        summary=str(_json_dict(task.get("decision")).get("detail") or "Composed task advanced.").strip(),
        payload_json={
            "decision": _json_dict(task.get("decision")),
            "current_step_key": str(task.get("current_step_key") or "").strip(),
            "current_step_index": int(task.get("current_step_index") or 0),
        },
    )
    await db.commit()
    _persist_composed_task_snapshot(task, shared_root=DEFAULT_SHARED_ROOT)
    return {
        "task": task,
        "decision": _json_dict(task.get("decision")),
        "operator_summary": str(task.get("operator_summary") or "").strip(),
        "operator_commands": task.get("operator_commands", []),
        "orchestration": to_execution_task_orchestration_out(row),
    }


@router.post("/management/refresh-status")
def refresh_mim_arm_status(
    payload: MimArmRefreshStatusRequest,
) -> dict[str, object]:
    return refresh_mim_arm_management_surface(
        remote_sync=bool(payload.remote_sync),
        skip_remote_run=bool(payload.skip_remote_run),
    )


@router.post("/capabilities/bootstrap")
async def bootstrap_mim_arm_capabilities(
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    rows: list[CapabilityRegistration] = []
    for definition in MIM_ARM_CAPABILITY_DEFINITIONS:
        rows.append(await _ensure_mim_arm_capability(db, definition))
    await write_journal(
        db,
        actor="mim_arm",
        action="bootstrap_capabilities",
        target_type="capability_family",
        target_id="mim_arm",
        summary="Bootstrapped bounded MIM arm read-only and proposal-only capabilities.",
        metadata_json={
            "capability_names": [str(row.capability_name) for row in rows],
            "stage": "read_only_awareness_and_proposal_only",
        },
    )
    await db.commit()
    for row in rows:
        await db.refresh(row)
    return {
        "stage": "read_only_awareness_and_proposal_only",
        "registered_capabilities": [
            {
                "capability_name": row.capability_name,
                "category": row.category,
                "description": row.description,
                "requires_confirmation": row.requires_confirmation,
                "enabled": row.enabled,
                "safety_policy": row.safety_policy,
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# Servo envelope endpoints  (Objective 173 — no hardware movement)
# ---------------------------------------------------------------------------


def _arm_id_from_status() -> str:
    """Derive arm_id from the live status surface (host IP) or fall back to 'default'."""
    try:
        status = load_mim_arm_status_surface()
        url: str = str(status.get("arm_state_probe", {}).get("url") or "")
        # url looks like "http://192.168.1.90:5000/arm_state"
        if "://" in url:
            host = url.split("://")[1].split(":")[0].split("/")[0]
            if host:
                return host
    except Exception:
        pass
    return "default"


@router.post("/envelopes/initialize")
async def initialize_arm_envelopes(
    db: AsyncSession = Depends(get_db),
    arm_id: str | None = None,
    force: bool = False,
) -> dict[str, object]:
    """
    Seed ArmServoEnvelope rows (servos 0–5) from configured servo limits.
    Safe to call multiple times — skips rows that already exist unless force=True.
    Learned values are never overwritten during initialization.
    NO hardware movement.
    """
    resolved_arm_id = arm_id or _arm_id_from_status()
    rows = await initialize_envelopes(db, arm_id=resolved_arm_id, actor="api", force=force)
    await db.commit()
    for row in rows:
        await db.refresh(row)
    return {
        "arm_id": resolved_arm_id,
        "initialized_count": len(rows),
        "force": force,
        "envelopes": [
            {
                "servo_id": r.servo_id,
                "servo_name": r.servo_name,
                "configured_min": r.configured_min,
                "configured_max": r.configured_max,
                "status": r.status,
            }
            for r in sorted(rows, key=lambda r: r.servo_id)
        ],
    }


@router.get("/envelopes")
async def list_arm_envelopes(
    db: AsyncSession = Depends(get_db),
    arm_id: str | None = None,
) -> dict[str, object]:
    """
    Return all servo envelope records for the arm.
    Read-only.  No hardware interaction.
    """
    resolved_arm_id = arm_id or _arm_id_from_status()
    rows = await get_envelopes(db, arm_id=resolved_arm_id)
    return {
        "arm_id": resolved_arm_id,
        "count": len(rows),
        "envelopes": [
            {
                "id": r.id,
                "servo_id": r.servo_id,
                "servo_name": r.servo_name,
                "configured_min": r.configured_min,
                "configured_max": r.configured_max,
                "learned_soft_min": r.learned_soft_min,
                "learned_soft_max": r.learned_soft_max,
                "preferred_min": r.preferred_min,
                "preferred_max": r.preferred_max,
                "unstable_regions": r.unstable_regions,
                "confidence": r.confidence,
                "evidence_count": r.evidence_count,
                "last_verified_at": r.last_verified_at.isoformat() if r.last_verified_at else None,
                "last_probe_phase": r.last_probe_phase,
                "status": r.status,
                "is_stale": is_stale(r),
                "stale_after_seconds": r.stale_after_seconds,
                "actor": r.actor,
                "source": r.source,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ],
    }


@router.get("/envelopes/{servo_id}")
async def get_arm_envelope(
    servo_id: int,
    db: AsyncSession = Depends(get_db),
    arm_id: str | None = None,
) -> dict[str, object]:
    """
    Return the servo envelope for a specific servo (0–5).
    Read-only.  No hardware interaction.
    """
    if servo_id < 0 or servo_id > 5:
        raise HTTPException(status_code=400, detail="servo_id must be 0–5")

    resolved_arm_id = arm_id or _arm_id_from_status()
    row = await get_envelope(db, servo_id, arm_id=resolved_arm_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No envelope found for servo_id={servo_id} arm_id={resolved_arm_id}. "
                   "Call POST /mim/arm/envelopes/initialize first.",
        )
    return {
        "id": row.id,
        "arm_id": row.arm_id,
        "servo_id": row.servo_id,
        "servo_name": row.servo_name,
        "configured_min": row.configured_min,
        "configured_max": row.configured_max,
        "learned_soft_min": row.learned_soft_min,
        "learned_soft_max": row.learned_soft_max,
        "preferred_min": row.preferred_min,
        "preferred_max": row.preferred_max,
        "unstable_regions": row.unstable_regions,
        "confidence": row.confidence,
        "evidence_count": row.evidence_count,
        "last_verified_at": row.last_verified_at.isoformat() if row.last_verified_at else None,
        "last_probe_phase": row.last_probe_phase,
        "status": row.status,
        "is_stale": is_stale(row),
        "stale_after_seconds": row.stale_after_seconds,
        "actor": row.actor,
        "source": row.source,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/envelopes/{servo_id}/probe-attempts")
async def list_probe_attempts(
    servo_id: int,
    db: AsyncSession = Depends(get_db),
    arm_id: str | None = None,
    limit: int = 50,
    phase: str | None = None,
) -> dict[str, object]:
    """
    Return the probe attempt log for a specific servo, newest first.
    Optionally filter by phase (simulation|dry_run|supervised_micro|autonomous).
    Read-only.  No hardware interaction.
    """
    if servo_id < 0 or servo_id > 5:
        raise HTTPException(status_code=400, detail="servo_id must be 0–5")
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be 1–500")

    resolved_arm_id = arm_id or _arm_id_from_status()
    attempts = await get_probe_attempts(
        db, servo_id, arm_id=resolved_arm_id, limit=limit, phase=phase
    )
    return {
        "arm_id": resolved_arm_id,
        "servo_id": servo_id,
        "phase_filter": phase,
        "count": len(attempts),
        "attempts": [
            {
                "id": a.id,
                "probe_id": a.probe_id,
                "envelope_id": a.envelope_id,
                "servo_id": a.servo_id,
                "phase": a.phase,
                "commanded_angle": a.commanded_angle,
                "prior_angle": a.prior_angle,
                "observed_angle": a.observed_angle,
                "step_degrees": a.step_degrees,
                "stop_condition": a.stop_condition,
                "stop_condition_flags": a.stop_condition_flags,
                "simulation_id": a.simulation_id,
                "execution_id": a.execution_id,
                "result": a.result,
                "confidence_delta": a.confidence_delta,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in attempts
        ],
    }


@router.get("/envelopes/probe-plan/dry-run")
async def get_envelope_dry_run_plan(
    db: AsyncSession = Depends(get_db),
    arm_id: str | None = None,
) -> dict[str, object]:
    """
    Generate a Phase-2 dry-run probe plan for all servos.
    Returns the plan as a preview — no hardware command is dispatched.
    """
    resolved_arm_id = arm_id or _arm_id_from_status()
    rows = await get_envelopes(db, arm_id=resolved_arm_id)
    if not rows:
        raise HTTPException(
            status_code=404,
            detail="No envelopes found. Call POST /mim/arm/envelopes/initialize first.",
        )
    plan = generate_dry_run_plan(rows, arm_id=resolved_arm_id)
    return plan


@router.post("/envelopes/{servo_id}/probe-plan/simulate")
async def post_simulation_probe_plan(
    servo_id: int,
    request: ArmSimulationProbeRequest,
    db: AsyncSession = Depends(get_db),
    arm_id: str | None = None,
) -> dict[str, object]:
    """
    Generate a detailed simulation-only probe plan for a single servo.

    NO hardware dispatch.  NO actuation.

    Request body: ArmSimulationProbeRequest with options for plan configuration.
    Response: Detailed ArmSimulationProbePlan with step-by-step probe sequence.

    If persist_planned_attempts=true, saves probe steps as planned_only attempts
    in the probe_attempts table (no execution_id, result='planned_only').
    """
    if servo_id < 0 or servo_id > 5:
        raise HTTPException(status_code=400, detail="servo_id must be 0–5")

    resolved_arm_id = arm_id or _arm_id_from_status()

    # Fetch the envelope for this servo
    envelope = await get_envelope(db, servo_id, arm_id=resolved_arm_id)
    if envelope is None:
        raise HTTPException(
            status_code=404,
            detail=f"Envelope for servo {servo_id} not found. Call POST /mim/arm/envelopes/initialize first.",
        )

    # Generate the detailed simulation-only plan
    plan = generate_simulation_probe_plan_for_servo(
        envelope,
        arm_id=resolved_arm_id,
        max_target_angles=request.max_target_angles,
        skip_unstable_regions=request.skip_unstable_regions,
    )

    # Optionally persist planned attempts
    if request.persist_planned_attempts and plan.get("probe_steps"):
        from uuid import uuid4
        for step in plan["probe_steps"]:
            attempt = ArmEnvelopeProbeAttempt(
                probe_id=str(uuid4()),
                envelope_id=envelope.id,
                servo_id=servo_id,
                phase="simulation_only",
                commanded_angle=step["target_angle"],
                prior_angle=step["current_angle"],
                observed_angle=None,
                step_degrees=step["step_degrees"],
                stop_condition="",
                stop_condition_flags={"applicable": step["stop_conditions_applicable"]},
                simulation_id=None,
                execution_id="",  # No execution in simulation-only
                result="planned_only",
                confidence_delta=0.0,
            )
            db.add(attempt)
        await db.commit()

    return plan


# ---------------------------------------------------------------------------
# Objective 175 — POST /envelopes/{servo_id}/probe-commands/dry-run
# ---------------------------------------------------------------------------


@router.post("/envelopes/{servo_id}/probe-commands/dry-run")
async def post_dry_run_probe_commands(
    servo_id: int,
    request: ArmDryRunCommandRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    Generate a dry-run command sequence for a single servo.

    Internally calls generate_simulation_probe_plan_for_servo() to obtain
    simulation plan steps, then converts each step into a structured
    ArmProbeCommandStep with command_id, rollback, stop_conditions, etc.

    dry_run=True and physical_execution_allowed=False always.
    NO hardware dispatch.  NO actuation.

    If persist_as_attempts=True, each command step is saved as an
    ArmEnvelopeProbeAttempt row with result='dry_run_generated'.
    """
    if servo_id < 0 or servo_id > 5:
        raise HTTPException(status_code=400, detail="servo_id must be 0–5")

    resolved_arm_id = request.arm_id or _arm_id_from_status()

    envelope = await get_envelope(db, servo_id, arm_id=resolved_arm_id)
    if envelope is None:
        raise HTTPException(
            status_code=404,
            detail=f"Envelope for servo {servo_id} not found. Call POST /mim/arm/envelopes/initialize first.",
        )

    # Step 1: generate simulation plan to get probe steps
    sim_plan = generate_simulation_probe_plan_for_servo(
        envelope,
        arm_id=resolved_arm_id,
        max_target_angles=request.max_target_angles,
        skip_unstable_regions=request.skip_unstable_regions,
    )
    probe_steps = sim_plan.get("probe_steps", [])

    # Step 2: convert simulation steps into dry-run command objects
    command_sequence = generate_dry_run_commands_for_servo(
        envelope,
        probe_steps,
        arm_id=resolved_arm_id,
    )

    # Step 3: optionally persist as dry_run_generated attempts
    if request.persist_as_attempts and command_sequence.get("commands"):
        from uuid import uuid4
        for cmd in command_sequence["commands"]:
            attempt = ArmEnvelopeProbeAttempt(
                probe_id=cmd["command_id"],
                envelope_id=envelope.id,
                servo_id=servo_id,
                phase="dry_run",
                commanded_angle=cmd["target_angle"],
                prior_angle=cmd["prior_angle"],
                observed_angle=None,
                step_degrees=cmd["step_degrees"],
                stop_condition="",
                stop_condition_flags={
                    "stop_conditions": cmd["stop_conditions"],
                    "dry_run": bool(cmd.get("dry_run", True)),
                    "physical_execution_allowed": False,
                    "safe_home_fallback": command_sequence.get("safe_home_fallback", {}),
                },
                simulation_id=None,
                execution_id="",
                result="dry_run_generated",
                confidence_delta=0.0,
            )
            db.add(attempt)
        await db.commit()

    return command_sequence


@router.post("/envelopes/{servo_id}/probe-authorizations/request", response_model=ArmProbeAuthorizationRead)
async def post_probe_authorization_request(
    servo_id: int,
    request: ArmProbeAuthorizationRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    Create a supervised micro-step authorization request from one dry-run command.

    No movement is executed. This endpoint only stages authorization state.
    """
    if servo_id < 0 or servo_id > 5:
        raise HTTPException(status_code=400, detail="servo_id must be 0–5")

    resolved_arm_id = request.arm_id or _arm_id_from_status()
    envelope = await get_envelope(db, servo_id, arm_id=resolved_arm_id)
    if envelope is None:
        raise HTTPException(
            status_code=404,
            detail=f"Envelope for servo {servo_id} not found. Call POST /mim/arm/envelopes/initialize first.",
        )

    try:
        auth = await create_supervised_micro_step_authorization(
            db,
            envelope,
            arm_id=resolved_arm_id,
            dry_run_command_id=request.dry_run_command_id,
            operator_id=request.operator_id,
            expires_in_seconds=request.expires_in_seconds,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()

    return auth


@router.post("/probe-authorizations/{authorization_id}/approve", response_model=ArmProbeAuthorizationRead)
async def post_probe_authorization_approve(
    authorization_id: str,
    request: ArmProbeAuthorizationApproveRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Approve one pending supervised probe authorization (no movement execution)."""
    await expire_pending_probe_authorizations(db)
    authorization = await get_probe_authorization(db, authorization_id)
    if authorization is None:
        raise HTTPException(status_code=404, detail="Authorization not found")

    try:
        result = await approve_probe_authorization(
            db,
            authorization,
            authorized_by=request.authorized_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()

    return result


@router.post("/probe-authorizations/{authorization_id}/reject", response_model=ArmProbeAuthorizationRead)
async def post_probe_authorization_reject(
    authorization_id: str,
    request: ArmProbeAuthorizationRejectRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Reject one pending supervised probe authorization (no movement execution)."""
    await expire_pending_probe_authorizations(db)
    authorization = await get_probe_authorization(db, authorization_id)
    if authorization is None:
        raise HTTPException(status_code=404, detail="Authorization not found")

    try:
        result = await reject_probe_authorization(db, authorization)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return result


@router.get("/probe-authorizations/{authorization_id}", response_model=ArmProbeAuthorizationRead)
async def get_probe_authorization_state(
    authorization_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Fetch one authorization state, applying expiry transition when needed."""
    await expire_pending_probe_authorizations(db)
    await db.commit()
    authorization = await get_probe_authorization(db, authorization_id)
    if authorization is None:
        raise HTTPException(status_code=404, detail="Authorization not found")

    return {
        "authorization_id": authorization.authorization_id,
        "arm_id": authorization.arm_id,
        "servo_id": authorization.servo_id,
        "dry_run_command_id": authorization.dry_run_command_id,
        "requested_angle": authorization.requested_angle,
        "prior_angle": authorization.prior_angle,
        "step_degrees": authorization.step_degrees,
        "direction": authorization.direction,
        "operator_id": authorization.operator_id,
        "authorized_by": authorization.authorized_by,
        "authorization_status": authorization.authorization_status,
        "expires_at": authorization.expires_at.isoformat() if authorization.expires_at else None,
        "stop_conditions": authorization.stop_conditions if isinstance(authorization.stop_conditions, list) else [],
        "safe_home_required": bool(authorization.safe_home_required),
        "physical_execution_allowed": bool(authorization.physical_execution_allowed),
        "created_at": authorization.created_at.isoformat() if authorization.created_at else None,
        "updated_at": authorization.updated_at.isoformat() if authorization.updated_at else None,
    }


@router.post("/probe-authorizations/{authorization_id}/gate-check", response_model=ArmProbeExecutionGateResult)
async def post_probe_authorization_gate_check(
    authorization_id: str,
    consume: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    Execution gate stub.

    This endpoint only evaluates gate status; it never dispatches servo movement.
    If consume=true and authorization is valid, status transitions to consumed.
    """
    await expire_pending_probe_authorizations(db)
    await db.commit()
    authorization = await get_probe_authorization(db, authorization_id)
    if authorization is None:
        raise HTTPException(status_code=404, detail="Authorization not found")

    result = await check_physical_micro_step_allowed(db, authorization, consume=consume)
    await db.commit()
    return result


# ---------------------------------------------------------------------------
# Objective 177 — Supervised Micro-Step Execution Stub
# ---------------------------------------------------------------------------

@router.post(
    "/probe-authorizations/{authorization_id}/execute",
    response_model=SupervisedMicroStepExecutionRead,
)
async def post_supervised_micro_step_execute(
    authorization_id: str,
    request: SupervisedMicroStepExecutionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    Begin one operator-triggered supervised micro-step execution stub.

    Requires an approved, unexpired, unconsumed authorization.
    Atomically consumes the authorization and creates an execution record.

    No hardware movement is dispatched.  physical_movement_dispatched is always False.
    """
    await expire_pending_probe_authorizations(db)
    authorization = await get_probe_authorization(db, authorization_id)
    if authorization is None:
        raise HTTPException(status_code=404, detail="Authorization not found")

    try:
        result = await begin_supervised_micro_step_execution(
            db,
            authorization,
            operator_id=request.operator_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()
    return result


@router.post(
    "/supervised-executions/{execution_id}/safe-home",
    response_model=SupervisedMicroStepExecutionRead,
)
async def post_supervised_execution_safe_home(
    execution_id: str,
    request: SafeHomeTriggerRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    Operator-triggered safe-home fallback for a running supervised execution.

    Transitions execution status to 'safe_home_triggered' and records the event
    in the execution log.  No hardware command is dispatched.
    """
    execution = await get_supervised_execution(db, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")

    try:
        result = await trigger_safe_home_fallback(
            db,
            execution,
            operator_id=request.operator_id,
            reason=request.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()
    return result


@router.get(
    "/supervised-executions/{execution_id}",
    response_model=SupervisedMicroStepExecutionRead,
)
async def get_supervised_execution_state(
    execution_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Fetch the full state of one supervised micro-step execution record."""
    execution = await get_supervised_execution(db, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Execution not found")
    from core.arm_envelope_service import _execution_to_dict
    return _execution_to_dict(execution)


# ---------------------------------------------------------------------------
# Objective 178 — Supervised Physical Micro-Step Execution endpoint
# ---------------------------------------------------------------------------

@router.post(
    "/probe-authorizations/{authorization_id}/execute-physical-micro-step",
    response_model=PhysicalMicroStepExecutionRead,
)
async def post_physical_micro_step_execute(
    authorization_id: str,
    request: PhysicalMicroStepExecutionRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    Execute exactly one supervised physical servo micro-step.

    Requires MIM_ARM_PHYSICAL_MICRO_STEP_ENABLED=true, an approved, unexpired,
    unconsumed authorization with stop conditions and a safe-home fallback.

    Uses MockServoAdapter in test environments; DirectArmHttpAdapter when
    MIM_ARM_PHYSICAL_MICRO_STEP_ENABLED=true and real hardware is available.

    Atomically consumes the authorization on execution.  No replay possible.
    """
    await expire_pending_probe_authorizations(db)
    authorization = await get_probe_authorization(db, authorization_id)
    if authorization is None:
        raise HTTPException(status_code=404, detail="Authorization not found")

    if settings.mim_arm_physical_micro_step_enabled:
        adapter: ServoHardwareAdapter = DirectArmHttpAdapter()
    else:
        adapter = MockServoAdapter()
    try:
        result = await execute_physical_micro_step(
            db,
            authorization,
            operator_id=request.operator_id,
            adapter=adapter,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()
    return result


@router.get(
    "/physical-executions/{execution_id}",
    response_model=PhysicalMicroStepExecutionRead,
)
async def get_physical_execution_state(
    execution_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """Fetch the full state of one supervised physical micro-step execution record."""
    execution = await get_physical_execution(db, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Physical execution not found")
    from core.arm_envelope_service import _physical_execution_to_dict
    return _physical_execution_to_dict(execution)


# ---------------------------------------------------------------------------
# Objective 179 — Record supervised probe outcome (envelope learning update)
# ---------------------------------------------------------------------------

@router.post(
    "/physical-executions/{execution_id}/record-probe-outcome",
    response_model=RecordProbeOutcomeRead,
)
async def post_record_probe_outcome(
    execution_id: str,
    request: RecordProbeOutcomeRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    """
    Record the envelope learning outcome from a completed supervised physical micro-step.

    Looks up the execution by execution_id (path param), validates it matches
    the request body execution_id, creates an ArmEnvelopeProbeAttempt with
    phase="supervised_micro", and updates the ArmServoEnvelope confidence,
    evidence_count, and learned bounds as appropriate.

    Safe to call only once per execution (idempotency not enforced at this layer —
    call sites should ensure single invocation).
    """
    if request.execution_id != execution_id:
        raise HTTPException(
            status_code=400,
            detail="Path execution_id does not match request body execution_id",
        )
    execution = await get_physical_execution(db, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="Physical execution not found")

    try:
        outcome = await record_supervised_probe_outcome(db, execution)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()
    return outcome


# ---------------------------------------------------------------------------
# Objective 180 — ARM envelope learning UI/operator workflow surface
# ---------------------------------------------------------------------------

def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _auth_to_ui_dict(row: ArmProbeAuthorization | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "authorization_id": row.authorization_id,
        "arm_id": row.arm_id,
        "servo_id": row.servo_id,
        "dry_run_command_id": row.dry_run_command_id,
        "requested_angle": row.requested_angle,
        "prior_angle": row.prior_angle,
        "step_degrees": row.step_degrees,
        "direction": row.direction,
        "operator_id": row.operator_id,
        "authorized_by": row.authorized_by,
        "authorization_status": row.authorization_status,
        "expires_at": _iso_or_none(row.expires_at),
        "stop_conditions": row.stop_conditions if isinstance(row.stop_conditions, list) else [],
        "safe_home_required": bool(row.safe_home_required),
        "physical_execution_allowed": bool(row.physical_execution_allowed),
        "created_at": _iso_or_none(row.created_at),
        "updated_at": _iso_or_none(row.updated_at),
    }


def _execution_feedback_to_ui_dict(row: SupervisedPhysicalMicroStepExecution | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "execution_id": row.execution_id,
        "authorization_id": row.authorization_id,
        "arm_id": row.arm_id,
        "servo_id": row.servo_id,
        "operator_id": row.operator_id,
        "execution_status": row.execution_status,
        "prior_angle": row.prior_angle,
        "commanded_angle": row.commanded_angle,
        "target_angle": row.target_angle,
        "step_degrees": row.step_degrees,
        "direction": row.direction,
        "physical_movement_dispatched": bool(row.physical_movement_dispatched),
        "dispatch_result": row.dispatch_result,
        "movement_duration_ms": row.movement_duration_ms,
        "stop_condition_triggered": row.stop_condition_triggered,
        "safe_home_triggered": bool(row.safe_home_triggered),
        "safe_home_outcome": row.safe_home_outcome,
        "error_message": row.error_message,
        "log_entries": row.log_entries if isinstance(row.log_entries, list) else [],
        "completed_at": _iso_or_none(row.completed_at),
        "created_at": _iso_or_none(row.created_at),
        "updated_at": _iso_or_none(row.updated_at),
    }


def _attempt_to_ui_dict(row: ArmEnvelopeProbeAttempt | None) -> dict[str, object] | None:
    if row is None:
        return None
    return {
        "attempt_id": row.id,
        "probe_id": row.probe_id,
        "phase": row.phase,
        "result": row.result,
        "execution_id": row.execution_id,
        "commanded_angle": row.commanded_angle,
        "prior_angle": row.prior_angle,
        "observed_angle": row.observed_angle,
        "step_degrees": row.step_degrees,
        "stop_condition": row.stop_condition,
        "confidence_delta": row.confidence_delta,
        "stop_condition_flags": row.stop_condition_flags if isinstance(row.stop_condition_flags, dict) else {},
        "created_at": _iso_or_none(row.created_at),
    }


@router.get("/operator-workflow/envelopes/{servo_id}")
async def get_envelope_learning_operator_workflow(
    servo_id: int,
    db: AsyncSession = Depends(get_db),
    arm_id: str | None = None,
    authorization_id: str | None = None,
    execution_id: str | None = None,
    max_preview_targets: int = 12,
) -> dict[str, object]:
    """
    UI/operator workflow surface for supervised envelope learning.

    This endpoint exposes the full operator flow in one response:
      1) show envelope state
      2) preview probe plan
      3) generate dry-run (action endpoint)
      4) request authorization (action endpoint)
      5) approve/reject (action endpoints)
      6) execute one micro-step (action endpoint)
      7) show execution feedback
      8) show learned envelope update
    """
    if servo_id < 0 or servo_id > 5:
        raise HTTPException(status_code=400, detail="servo_id must be 0–5")
    if max_preview_targets < 1 or max_preview_targets > 50:
        raise HTTPException(status_code=400, detail="max_preview_targets must be 1–50")

    resolved_arm_id = arm_id or _arm_id_from_status()
    envelope = await get_envelope(db, servo_id, arm_id=resolved_arm_id)
    if envelope is None:
        raise HTTPException(
            status_code=404,
            detail=f"Envelope for servo {servo_id} not found. Call POST /mim/arm/envelopes/initialize first.",
        )

    preview_plan = generate_simulation_probe_plan_for_servo(
        envelope,
        arm_id=resolved_arm_id,
        max_target_angles=max_preview_targets,
        skip_unstable_regions=True,
    )

    latest_dry_run_result = await db.execute(
        select(ArmEnvelopeProbeAttempt)
        .where(
            ArmEnvelopeProbeAttempt.envelope_id == envelope.id,
            ArmEnvelopeProbeAttempt.servo_id == servo_id,
            ArmEnvelopeProbeAttempt.phase == "dry_run",
            ArmEnvelopeProbeAttempt.result == "dry_run_generated",
        )
        .order_by(ArmEnvelopeProbeAttempt.id.desc())
        .limit(1)
    )
    latest_dry_run = latest_dry_run_result.scalar_one_or_none()

    selected_auth: ArmProbeAuthorization | None
    if authorization_id:
        auth_result = await db.execute(
            select(ArmProbeAuthorization).where(
                ArmProbeAuthorization.authorization_id == authorization_id,
                ArmProbeAuthorization.arm_id == resolved_arm_id,
                ArmProbeAuthorization.servo_id == servo_id,
            )
        )
        selected_auth = auth_result.scalar_one_or_none()
    else:
        auth_result = await db.execute(
            select(ArmProbeAuthorization)
            .where(
                ArmProbeAuthorization.arm_id == resolved_arm_id,
                ArmProbeAuthorization.servo_id == servo_id,
            )
            .order_by(ArmProbeAuthorization.updated_at.desc(), ArmProbeAuthorization.created_at.desc())
            .limit(1)
        )
        selected_auth = auth_result.scalar_one_or_none()

    selected_exec: SupervisedPhysicalMicroStepExecution | None
    if execution_id:
        exec_result = await db.execute(
            select(SupervisedPhysicalMicroStepExecution).where(
                SupervisedPhysicalMicroStepExecution.execution_id == execution_id,
                SupervisedPhysicalMicroStepExecution.arm_id == resolved_arm_id,
                SupervisedPhysicalMicroStepExecution.servo_id == servo_id,
            )
        )
        selected_exec = exec_result.scalar_one_or_none()
    else:
        exec_result = await db.execute(
            select(SupervisedPhysicalMicroStepExecution)
            .where(
                SupervisedPhysicalMicroStepExecution.arm_id == resolved_arm_id,
                SupervisedPhysicalMicroStepExecution.servo_id == servo_id,
            )
            .order_by(
                SupervisedPhysicalMicroStepExecution.updated_at.desc(),
                SupervisedPhysicalMicroStepExecution.created_at.desc(),
            )
            .limit(1)
        )
        selected_exec = exec_result.scalar_one_or_none()

    latest_learned_result = await db.execute(
        select(ArmEnvelopeProbeAttempt)
        .where(
            ArmEnvelopeProbeAttempt.envelope_id == envelope.id,
            ArmEnvelopeProbeAttempt.servo_id == servo_id,
            ArmEnvelopeProbeAttempt.phase == "supervised_micro",
        )
        .order_by(ArmEnvelopeProbeAttempt.id.desc())
        .limit(1)
    )
    latest_learning_attempt = latest_learned_result.scalar_one_or_none()

    current_auth = _auth_to_ui_dict(selected_auth)
    can_approve_reject = bool(current_auth and current_auth.get("authorization_status") == "pending")
    can_execute = bool(current_auth and current_auth.get("authorization_status") == "approved")
    can_record_outcome = bool(selected_exec is not None)

    return {
        "arm_id": resolved_arm_id,
        "servo_id": servo_id,
        "servo_name": envelope.servo_name,
        "workflow_id": "mim_arm_envelope_learning_operator_workflow",
        "workflow_steps": [
            "show_envelope_state",
            "preview_probe_plan",
            "generate_dry_run",
            "request_authorization",
            "approve_or_reject",
            "execute_one_micro_step",
            "show_feedback",
            "show_learned_envelope_update",
        ],
        "envelope_state": {
            "id": envelope.id,
            "configured_min": envelope.configured_min,
            "configured_max": envelope.configured_max,
            "learned_soft_min": envelope.learned_soft_min,
            "learned_soft_max": envelope.learned_soft_max,
            "preferred_min": envelope.preferred_min,
            "preferred_max": envelope.preferred_max,
            "unstable_regions": envelope.unstable_regions if isinstance(envelope.unstable_regions, list) else [],
            "confidence": envelope.confidence,
            "evidence_count": envelope.evidence_count,
            "last_verified_at": _iso_or_none(envelope.last_verified_at),
            "last_probe_phase": envelope.last_probe_phase,
            "status": envelope.status,
            "is_stale": is_stale(envelope),
            "stale_after_seconds": envelope.stale_after_seconds,
            "updated_at": _iso_or_none(envelope.updated_at),
        },
        "probe_plan_preview": {
            "phase": preview_plan.get("phase"),
            "estimated_total_steps": preview_plan.get("estimated_total_steps"),
            "risk_assessment": preview_plan.get("risk_assessment"),
            "start_angle": preview_plan.get("start_angle"),
            "target_angles": preview_plan.get("target_angles", []),
            "probe_steps": preview_plan.get("probe_steps", []),
        },
        "latest_dry_run": _attempt_to_ui_dict(latest_dry_run),
        "latest_authorization": current_auth,
        "latest_execution_feedback": _execution_feedback_to_ui_dict(selected_exec),
        "latest_learned_envelope_update": _attempt_to_ui_dict(latest_learning_attempt),
        "actions": {
            "generate_dry_run": {
                "method": "POST",
                "endpoint": f"/mim/arm/envelopes/{servo_id}/probe-commands/dry-run",
                "request_template": {
                    "arm_id": resolved_arm_id,
                    "skip_unstable_regions": True,
                    "max_target_angles": max_preview_targets,
                    "persist_as_attempts": True,
                },
            },
            "request_authorization": {
                "method": "POST",
                "endpoint": f"/mim/arm/envelopes/{servo_id}/probe-authorizations/request",
                "request_template": {
                    "arm_id": resolved_arm_id,
                    "dry_run_command_id": latest_dry_run.probe_id if latest_dry_run is not None else "",
                    "operator_id": "operator.test",
                    "expires_in_seconds": 300,
                },
            },
            "approve": {
                "enabled": can_approve_reject,
                "method": "POST",
                "endpoint": (
                    f"/mim/arm/probe-authorizations/{current_auth['authorization_id']}/approve"
                    if current_auth is not None
                    else ""
                ),
                "request_template": {"authorized_by": "supervisor.test"},
            },
            "reject": {
                "enabled": can_approve_reject,
                "method": "POST",
                "endpoint": (
                    f"/mim/arm/probe-authorizations/{current_auth['authorization_id']}/reject"
                    if current_auth is not None
                    else ""
                ),
                "request_template": {"rejected_by": "supervisor.test", "reason": "operator_rejected"},
            },
            "execute_one_micro_step": {
                "enabled": can_execute,
                "method": "POST",
                "endpoint": (
                    f"/mim/arm/probe-authorizations/{current_auth['authorization_id']}/execute-physical-micro-step"
                    if current_auth is not None
                    else ""
                ),
                "request_template": {"operator_id": "operator.test"},
            },
            "record_probe_outcome": {
                "enabled": can_record_outcome,
                "method": "POST",
                "endpoint": (
                    f"/mim/arm/physical-executions/{selected_exec.execution_id}/record-probe-outcome"
                    if selected_exec is not None
                    else ""
                ),
                "request_template": {
                    "execution_id": selected_exec.execution_id if selected_exec is not None else "",
                },
            },
        },
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
