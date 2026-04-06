from __future__ import annotations

import json
import os
import hashlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db import get_db
from core.execution_trace_service import append_execution_trace_event
from core.execution_lane_service import TARGET_MIM_ARM, build_execution_target_profile, submit_execution_request
from core.journal import write_journal
from core.mim_arm_dispatch_telemetry import (
    record_dispatch_telemetry_from_publish,
    refresh_dispatch_telemetry_record,
)
from core.models import CapabilityExecution, CapabilityRegistration, InputEvent, InputEventResolution
from core.routers.self_awareness_router import health_monitor as _mim_health_monitor
from core.routers import gateway as gateway_router
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
MIM_ARM_DISPATCH_TELEMETRY_ARTIFACT = "MIM_ARM_DISPATCH_TELEMETRY.latest.json"
CONTEXT_EXPORT_ARTIFACT = "MIM_CONTEXT_EXPORT.latest.json"
ARM_SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_mim_arm_host_state.py"
ARM_STATUS_SCRIPT = PROJECT_ROOT / "scripts" / "generate_mim_arm_status.py"
BRIDGE_SEQUENCE_SCRIPT = PROJECT_ROOT / "scripts" / "bridge_packet_sequence.py"
TOD_REMOTE_PUBLISH_SCRIPT = PROJECT_ROOT / "scripts" / "publish_tod_bridge_artifacts_remote.py"
TOD_BRIDGE_AUDIT_SCRIPT = PROJECT_ROOT / "scripts" / "tod_bridge_audit.py"
MIM_ARM_ENV_FILE = PROJECT_ROOT / "env" / ".env"

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
        "description": "Dispatch the first live governed motion to TOD: safe_home only.",
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
        "description": "Dispatch the second bounded governed motion to TOD: scan_pose only.",
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


def _bounded_action_execution_phrase(action_name: str) -> str:
    if action_name in {"safe_home", "scan_pose"}:
        return f"Move the arm to the {action_name} pose via TOD-governed bounded execution."
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
            f"/home/testpilot/mim/runtime/shared -> {os.getenv('MIM_ARM_SSH_HOST', '')}:{os.getenv('MIM_ARM_SSH_REMOTE_ROOT', '/home/testpilot/mim/runtime/shared')}",
            "--remote-host",
            os.getenv("MIM_ARM_SSH_HOST", ""),
            "--remote-root",
            os.getenv("MIM_ARM_SSH_REMOTE_ROOT", "/home/testpilot/mim/runtime/shared"),
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
    action_slug = _action_slug(action_name)
    action_display = _action_display_name(action_name)
    capability_name = str(getattr(execution, "capability_name", f"mim_arm.execute_{action_name}") or f"mim_arm.execute_{action_name}").strip()
    publication_service = f"mim_arm_{action_name}_dispatch"
    execution_id = int(getattr(execution, "id", 0) or 0)
    publication_instance = f"{publication_service}:{execution_id}"
    objective = _active_objective_metadata(shared_root)
    objective_id = objective.get("objective_id", "")
    objective_ref = objective.get("objective_ref", "") or "objective-unknown"

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
        "request_id": request_id,
        "freshness_token": publish_freshness_token,
        "publish_index": request_sequence,
        "objective_id": objective_ref,
        "objective": objective_ref,
        "title": f"Execute bounded {action_display} via TOD",
        "scope": _bounded_action_execution_phrase(action_name),
        "priority": "high",
        "action": action_name,
        "capability_name": capability_name,
        "requested_executor": str(getattr(execution, "requested_executor", "tod") or "tod").strip() or "tod",
        "execution_id": execution_id,
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


def load_mim_arm_status_surface(*, shared_root: Path = DEFAULT_SHARED_ROOT) -> dict[str, object]:
    status_payload = _read_json_artifact(shared_root / ARM_STATUS_ARTIFACT)
    host_state_payload = _read_json_artifact(shared_root / ARM_HOST_STATE_ARTIFACT)
    if host_state_payload:
        status_payload = {**status_payload, **host_state_payload}
    diagnostic = _read_json_artifact(shared_root / ARM_DIAGNOSTIC_ARTIFACT)
    readiness = _latest_readiness(shared_root)
    catchup = _catchup_gate(shared_root)
    catchup_summary = _build_tod_catchup_summary(catchup)
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
        },
        "self_health": health,
        "source_artifacts": {
            "arm_status": str(shared_root / ARM_STATUS_ARTIFACT),
            "arm_host_state": str(shared_root / ARM_HOST_STATE_ARTIFACT),
            "arm_diagnostic": str(shared_root / ARM_DIAGNOSTIC_ARTIFACT),
            "tod_command_status": str(shared_root / TOD_COMMAND_STATUS_ARTIFACT),
            "tod_task_result": str(shared_root / TOD_TASK_RESULT_ARTIFACT),
            "tod_catchup_gate": str(shared_root / TOD_CATCHUP_GATE_ARTIFACT),
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
        if not bool(catchup_detail.get("refresh_evidence_ok", True)):
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
        recommended_next_step = "Bounded safe_home and scan_pose are available, but integrate explicit emergency-stop support before promoting beyond bounded managed access."
    else:
        recommended_next_step = "MIM can request bounded safe_home or scan_pose execution once operator approval is supplied."

    return {
        "generated_at": _utcnow(),
        "current_authority": {
            "executor": "tod",
            "operator_approval_required": True,
            "allowed_live_actions": ["safe_home", "scan_pose"],
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