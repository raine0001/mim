#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request as urllib_request

try:
    import paramiko
except Exception:  # pragma: no cover - optional dependency fallback
    paramiko = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_DIR = PROJECT_ROOT / "runtime" / "diagnostics"
DEFAULT_SHARED_DIR = PROJECT_ROOT / "runtime" / "shared"
DEFAULT_REMOTE_COMMAND_STATUS = "/home/testpilot/mim/runtime/shared/TOD_MIM_COMMAND_STATUS.latest.json"
DEFAULT_PUBLICATION_BOUNDARY = DEFAULT_SHARED_DIR / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json"
DEFAULT_LOCAL_TASK_ACK = DEFAULT_SHARED_DIR / "TOD_MIM_TASK_ACK.latest.json"
DEFAULT_LOCAL_TASK_RESULT = DEFAULT_SHARED_DIR / "TOD_MIM_TASK_RESULT.latest.json"
DEFAULT_LOCAL_HOST_STATE = DEFAULT_SHARED_DIR / "mim_arm_host_state.latest.json"
DEFAULT_DISPATCH_TELEMETRY_ENDPOINT_TEMPLATE = "/mim/arm/dispatch-telemetry/{request_id}"
HOST_STATE_SYNC_SCRIPT = PROJECT_ROOT / "scripts" / "sync_mim_arm_host_state.py"


def _action_slug(action: str) -> str:
    return str(action or "safe_home").strip().replace("_", "-") or "safe-home"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get_json(url: str, timeout_seconds: int = 8) -> dict[str, Any]:
    with urllib_request.urlopen(urllib_request.Request(url, method="GET"), timeout=timeout_seconds) as response:
        payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {"data": payload}


def _post_json(url: str, payload: dict[str, Any], timeout_seconds: int = 15) -> tuple[int, dict[str, Any]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8", "replace")
        parsed = json.loads(raw) if raw else {}
        return int(response.status), parsed if isinstance(parsed, dict) else {"data": parsed}


def _resolve_password(password_env: str) -> str:
    return os.getenv(password_env, "") or os.getenv("MIM_ARM_SSH_HOST_PASS", "") or os.getenv("MIM_ARM_SSH_PASSWORD", "")


def _read_remote_json_via_paramiko(*, host: str, ssh_user: str, ssh_port: int, password: str, remote_path: str) -> dict[str, Any]:
    if paramiko is None:
        raise RuntimeError("paramiko is required for password-based remote reads when sshpass is unavailable")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=ssh_user, password=password, port=ssh_port, timeout=8)
    try:
        sftp = client.open_sftp()
        try:
            with sftp.file(remote_path, "r") as handle:
                return _json_dict(json.loads(handle.read().decode("utf-8")))
        finally:
            sftp.close()
    finally:
        client.close()


def _read_remote_json(*, host: str, ssh_user: str, ssh_port: int, password_env: str, remote_path: str) -> dict[str, Any]:
    password = _resolve_password(password_env)
    if password and shutil.which("sshpass") is None:
        return _read_remote_json_via_paramiko(
            host=host,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            password=password,
            remote_path=remote_path,
        )
    command = [
        *([shutil.which("sshpass"), "-p", password] if password and shutil.which("sshpass") else []),
        "ssh",
        "-o",
        "ConnectTimeout=5",
        "-p",
        str(ssh_port),
        *( [] if password else ["-o", "BatchMode=yes"] ),
        f"{ssh_user}@{host}",
        f"cat {remote_path}",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"remote read failed with exit {completed.returncode}")
    return _json_dict(json.loads(completed.stdout or "{}"))


def _json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_local_json(path: str | Path) -> dict[str, Any]:
    payload_path = Path(path).expanduser().resolve()
    if not payload_path.exists():
        return {}
    try:
        return _json_dict(json.loads(payload_path.read_text(encoding="utf-8-sig")))
    except Exception:
        return {}


def _best_effort_remote_json(*, host: str, ssh_user: str, ssh_port: int, password_env: str, remote_path: str) -> tuple[dict[str, Any], str]:
    try:
        return (
            _read_remote_json(
                host=host,
                ssh_user=ssh_user,
                ssh_port=ssh_port,
                password_env=password_env,
                remote_path=remote_path,
            ),
            "",
        )
    except Exception as exc:
        return {}, str(exc).strip() or exc.__class__.__name__


def _refresh_local_host_state(*, local_output: str | Path, host: str, arm_api_port: int) -> tuple[dict[str, Any], str]:
    command = [
        sys.executable,
        str(HOST_STATE_SYNC_SCRIPT),
        "--http-fallback",
        "--host",
        host,
        "--arm-api-port",
        str(arm_api_port),
        "--local-output",
        str(Path(local_output).expanduser().resolve()),
    ]
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return {}, (completed.stderr.strip() or completed.stdout.strip() or f"host-state refresh failed with exit {completed.returncode}")
    return _read_local_json(local_output), ""


def _merge_host_payload(arm_state: dict[str, Any], host_state: dict[str, Any]) -> dict[str, Any]:
    merged = {**_json_dict(arm_state), **_json_dict(host_state)}
    merged["command_evidence"] = {
        **_json_dict(arm_state.get("command_evidence")),
        **_json_dict(host_state.get("command_evidence")),
    }
    merged["last_command_result"] = {
        **_json_dict(arm_state.get("last_command_result")),
        **_json_dict(host_state.get("last_command_result")),
    }
    merged["bridge_runtime"] = {
        **_json_dict(arm_state.get("bridge_runtime")),
        **_json_dict(host_state.get("bridge_runtime")),
    }
    return merged


def _find_matches(payload: Any, needle: str, path: str = "$", matches: list[str] | None = None) -> list[str]:
    collected = matches if matches is not None else []
    if isinstance(payload, dict):
        for key, value in payload.items():
            _find_matches(value, needle, f"{path}.{key}", collected)
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            _find_matches(value, needle, f"{path}[{index}]", collected)
    else:
        if str(payload).strip() == needle:
            collected.append(path)
    return collected


def _authoritative_matches(paths: list[str]) -> list[str]:
    return [path for path in paths if not path.startswith("$.trigger")]


def _classify_remote_command_status(
    payload: dict[str, Any],
    *,
    fresh_dispatch_identifier_matches: list[str],
    authoritative_fresh_dispatch_identifier_matches: list[str],
) -> dict[str, Any]:
    source = str(payload.get("source") or payload.get("packet_type") or "").strip()
    has_execution_readiness = isinstance(payload.get("execution_readiness"), dict)
    refreshed_by = str(_json_dict(payload.get("metadata_json")).get("refreshed_by") or "").strip()

    surface_kind = "unknown"
    dispatch_identifier_expected_on_surface = True
    supports_dispatch_consumption_proof = True
    interpretation = "Remote command-status semantics are not classified."

    if source == "tod-mim-command-status-v1" and (has_execution_readiness or refreshed_by == "refresh_execution_readiness.py"):
        surface_kind = "readiness_preflight"
        dispatch_identifier_expected_on_surface = False
        supports_dispatch_consumption_proof = False
        interpretation = (
            "Remote command-status is acting as the readiness preflight surface for this check; "
            "fresh bounded dispatch identifiers are not expected to appear here."
        )

    return {
        "surface_kind": surface_kind,
        "dispatch_identifier_expected_on_surface": dispatch_identifier_expected_on_surface,
        "supports_dispatch_consumption_proof": supports_dispatch_consumption_proof,
        "fresh_dispatch_identifier_visible": bool(fresh_dispatch_identifier_matches),
        "fresh_dispatch_identifier_consumed_on_surface": bool(authoritative_fresh_dispatch_identifier_matches),
        "interpretation": interpretation,
    }


def _resolve_dispatch_identifier(bridge_publication: dict[str, Any]) -> tuple[str, str]:
    task_id = str(bridge_publication.get("task_id") or "").strip()
    if task_id:
        return "bridge_task_id", task_id
    request_id = str(bridge_publication.get("request_id") or "").strip()
    if request_id:
        return "bridge_request_id", request_id
    return "", ""


def _boundary_matches_dispatch_identifier(boundary_payload: dict[str, Any], dispatch_identifier: str) -> bool:
    remote_request = _json_dict(boundary_payload.get("remote_request"))
    remote_trigger = _json_dict(boundary_payload.get("remote_trigger"))
    request_alignment = _json_dict(boundary_payload.get("request_alignment"))
    trigger_alignment = _json_dict(boundary_payload.get("trigger_alignment"))
    remote_request_id = str(remote_request.get("request_id") or remote_request.get("task_id") or "").strip()
    remote_trigger_id = str(remote_trigger.get("request_id") or remote_trigger.get("task_id") or "").strip()
    return bool(
        dispatch_identifier
        and remote_request_id == dispatch_identifier
        and remote_trigger_id == dispatch_identifier
        and bool(request_alignment.get("request_id_match"))
        and bool(trigger_alignment.get("request_id_match"))
    )


def _response_artifact_match(payload: dict[str, Any], dispatch_identifier: str) -> dict[str, Any]:
    matched_fields: list[str] = []
    for field_name in ("request_id", "task_id", "task"):
        if str(payload.get(field_name) or "").strip() == dispatch_identifier:
            matched_fields.append(field_name)
    bridge_processing = _json_dict(_json_dict(payload.get("bridge_runtime")).get("current_processing"))
    if str(bridge_processing.get("request_id") or "").strip() == dispatch_identifier:
        matched_fields.append("bridge_runtime.current_processing.request_id")
    if str(bridge_processing.get("task_id") or "").strip() == dispatch_identifier:
        matched_fields.append("bridge_runtime.current_processing.task_id")
    return {
        "matched": bool(matched_fields),
        "matched_fields": matched_fields,
    }


def _poll_local_tod_responses(*, task_id: str, task_ack_path: str | Path, task_result_path: str | Path, timeout_seconds: int, interval_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    task_ack: dict[str, Any] = {}
    task_result: dict[str, Any] = {}
    task_ack_matches: list[str] = []
    task_result_matches: list[str] = []
    while time.monotonic() < deadline:
        task_ack = _read_local_json(task_ack_path)
        task_result = _read_local_json(task_result_path)
        task_ack_matches = _find_matches(task_ack, task_id)
        task_result_matches = _find_matches(task_result, task_id)
        if task_ack_matches and task_result_matches:
            break
        time.sleep(interval_seconds)
    return {
        "task_ack": task_ack,
        "task_result": task_result,
        "task_ack_matches": task_ack_matches,
        "task_result_matches": task_result_matches,
    }


def _extract_command_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    serial = _json_dict(payload.get("serial"))
    last_command_result = _json_dict(payload.get("last_command_result"))
    command_evidence = _json_dict(payload.get("command_evidence"))
    return {
        "commands_total": command_evidence.get("commands_total") or payload.get("commands_total") or last_command_result.get("commands_total") or serial.get("serial_command_count") or serial.get("command_count"),
        "acks_total": command_evidence.get("acks_total") or payload.get("acks_total") or last_command_result.get("acks_total") or serial.get("serial_ack_count") or serial.get("ack_count"),
        "last_command_sent": command_evidence.get("last_command_sent") or payload.get("last_command_sent") or last_command_result.get("last_command_sent") or serial.get("last_command"),
        "last_command_sent_at": command_evidence.get("last_command_sent_at") or payload.get("last_command_sent_at") or last_command_result.get("last_command_sent_at") or serial.get("last_command_sent_at"),
        "last_serial_event": command_evidence.get("last_serial_event") or payload.get("last_serial_event") or serial.get("last_serial_event") or serial.get("last_event"),
        "request_id": command_evidence.get("request_id") or payload.get("last_request_id") or last_command_result.get("request_id") or serial.get("last_request_id"),
        "task_id": command_evidence.get("task_id") or payload.get("last_task_id") or last_command_result.get("task_id") or serial.get("last_task_id"),
        "correlation_id": command_evidence.get("correlation_id") or payload.get("last_correlation_id") or last_command_result.get("correlation_id") or serial.get("last_correlation_id"),
        "lane": command_evidence.get("lane") or payload.get("last_command_lane") or last_command_result.get("lane") or serial.get("last_command_lane"),
    }


def _refresh_readiness(*, action: str, task_id: str, host: str, ssh_user: str, ssh_port: int, password_env: str, remote_output: str) -> dict[str, Any]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "refresh_execution_readiness.py"),
        "--action",
        action,
        "--source",
        "controlled_dispatch_attribution_check",
        "--detail",
        f"Fresh readiness issued before controlled {action} dispatch attribution check.",
        "--request-id",
        task_id,
        "--task-id",
        task_id,
        "--correlation-id",
        task_id,
        "--remote-host",
        host,
        "--remote-user",
        ssh_user,
        "--remote-port",
        str(ssh_port),
        "--remote-output",
        remote_output,
        "--password-env",
        password_env,
    ]
    if not host:
        command.append("--skip-remote")
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "readiness refresh failed")
    return _json_dict(json.loads(completed.stdout or "{}"))


def _poll_remote_command_status(*, task_id: str, host: str, ssh_user: str, ssh_port: int, password_env: str, remote_path: str, timeout_seconds: int, interval_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = _read_remote_json(
            host=host,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            password_env=password_env,
            remote_path=remote_path,
        )
        last_payload = payload
        if _find_matches(payload, task_id):
            return payload
        time.sleep(interval_seconds)
    return last_payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh readiness, publish one bounded MIM ARM task, and capture same-source TOD/host dispatch evidence.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--arm-base-url", default=os.getenv("MIM_ARM_HTTP_BASE_URL", "http://192.168.1.90:5000"))
    parser.add_argument("--remote-host", default=os.getenv("MIM_ARM_SSH_HOST", "192.168.1.90"))
    parser.add_argument("--remote-user", default=os.getenv("MIM_ARM_SSH_HOST_USER", os.getenv("MIM_ARM_SSH_USER", "testpilot")))
    parser.add_argument("--remote-port", type=int, default=int(os.getenv("MIM_ARM_SSH_HOST_PORT", "22") or "22"))
    parser.add_argument("--password-env", default="MIM_ARM_SSH_HOST_PASS")
    parser.add_argument("--remote-command-status", default=DEFAULT_REMOTE_COMMAND_STATUS)
    parser.add_argument("--publication-boundary", default=str(DEFAULT_PUBLICATION_BOUNDARY))
    parser.add_argument("--local-task-ack", default=str(DEFAULT_LOCAL_TASK_ACK))
    parser.add_argument("--local-task-result", default=str(DEFAULT_LOCAL_TASK_RESULT))
    parser.add_argument("--local-host-state", default=str(DEFAULT_LOCAL_HOST_STATE))
    parser.add_argument("--dispatch-telemetry-endpoint-template", default=DEFAULT_DISPATCH_TELEMETRY_ENDPOINT_TEMPLATE)
    parser.add_argument("--arm-api-port", type=int, default=5000)
    parser.add_argument("--poll-timeout-seconds", type=int, default=90)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    parser.add_argument("--action", default="safe_home")
    parser.add_argument("--actor", default="operator")
    parser.add_argument("--reason", default="controlled bounded dispatch attribution verification")
    parser.add_argument("--shared-workspace-active", action="store_true")
    parser.add_argument("--skip-host-state-refresh", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = _utcnow()
    action_name = str(args.action or "safe_home").strip() or "safe_home"
    action_slug = _action_slug(action_name)

    command_status_before, command_status_before_error = _best_effort_remote_json(
        host=str(args.remote_host),
        ssh_user=str(args.remote_user),
        ssh_port=int(args.remote_port),
        password_env=str(args.password_env),
        remote_path=str(args.remote_command_status),
    )
    remote_status_available = not bool(command_status_before_error)
    arm_state_before = _get_json(f"{str(args.arm_base_url).rstrip('/')}/arm_state")
    if bool(args.skip_host_state_refresh):
        host_state_before = _read_local_json(str(args.local_host_state))
        host_state_before_error = ""
    else:
        host_state_before, host_state_before_error = _refresh_local_host_state(
            local_output=str(args.local_host_state),
            host=str(args.remote_host),
            arm_api_port=int(args.arm_api_port),
        )

    pre_refresh_tag = f"dispatch-attribution-refresh-{int(time.time())}"
    refresh_result = _refresh_readiness(
        action=action_name,
        task_id=pre_refresh_tag,
        host=str(args.remote_host) if remote_status_available else "",
        ssh_user=str(args.remote_user),
        ssh_port=int(args.remote_port),
        password_env=str(args.password_env),
        remote_output=str(args.remote_command_status),
    )

    publish_payload = {
        "actor": str(args.actor),
        "reason": str(args.reason),
        "explicit_operator_approval": True,
        "shared_workspace_active": bool(args.shared_workspace_active),
        "metadata_json": {
            "verification": "controlled_dispatch_attribution_check",
            "requested_at": _utcnow(),
        },
    }
    publish_status_code, publish_response = _post_json(
        f"{str(args.base_url).rstrip('/')}/mim/arm/executions/{action_slug}",
        publish_payload,
        timeout_seconds=30,
    )
    bridge_publication = _json_dict(_json_dict(publish_response.get("execution")).get("bridge_publication"))
    identifier_kind, dispatch_identifier = _resolve_dispatch_identifier(bridge_publication)
    if not dispatch_identifier:
        raise RuntimeError(f"{action_slug} publish did not return a bridge task_id or request_id")

    publication_boundary = _read_local_json(str(args.publication_boundary))

    command_status_after: dict[str, Any] = {}
    command_status_after_error = ""
    if remote_status_available:
        try:
            command_status_after = _poll_remote_command_status(
                task_id=dispatch_identifier,
                host=str(args.remote_host),
                ssh_user=str(args.remote_user),
                ssh_port=int(args.remote_port),
                password_env=str(args.password_env),
                remote_path=str(args.remote_command_status),
                timeout_seconds=int(args.poll_timeout_seconds),
                interval_seconds=float(args.poll_interval_seconds),
            )
        except Exception as exc:
            command_status_after_error = str(exc).strip() or exc.__class__.__name__
    tod_responses = _poll_local_tod_responses(
        task_id=dispatch_identifier,
        task_ack_path=str(args.local_task_ack),
        task_result_path=str(args.local_task_result),
        timeout_seconds=int(args.poll_timeout_seconds),
        interval_seconds=float(args.poll_interval_seconds),
    )
    dispatch_telemetry: dict[str, Any] = {}
    dispatch_telemetry_error = ""
    try:
        dispatch_telemetry = _get_json(
            f"{str(args.base_url).rstrip('/')}{str(args.dispatch_telemetry_endpoint_template).format(request_id=dispatch_identifier)}",
            timeout_seconds=15,
        )
    except Exception as exc:
        dispatch_telemetry_error = str(exc).strip() or exc.__class__.__name__
    arm_state_after = _get_json(f"{str(args.arm_base_url).rstrip('/')}/arm_state")
    if bool(args.skip_host_state_refresh):
        host_state_after = _read_local_json(str(args.local_host_state))
        host_state_after_error = ""
    else:
        host_state_after, host_state_after_error = _refresh_local_host_state(
            local_output=str(args.local_host_state),
            host=str(args.remote_host),
            arm_api_port=int(args.arm_api_port),
        )

    remote_matches = _find_matches(command_status_after, dispatch_identifier)
    remote_authoritative_matches = _authoritative_matches(remote_matches)
    remote_status_classification = _classify_remote_command_status(
        command_status_after,
        fresh_dispatch_identifier_matches=remote_matches,
        authoritative_fresh_dispatch_identifier_matches=remote_authoritative_matches,
    )
    publication_boundary_matches = _boundary_matches_dispatch_identifier(publication_boundary, dispatch_identifier)
    task_ack = _json_dict(tod_responses.get("task_ack"))
    task_result = _json_dict(tod_responses.get("task_result"))
    task_ack_occurrences = list(tod_responses.get("task_ack_matches") or [])
    task_result_occurrences = list(tod_responses.get("task_result_matches") or [])
    task_ack_match = _response_artifact_match(task_ack, dispatch_identifier)
    task_result_match = _response_artifact_match(task_result, dispatch_identifier)
    dispatch_telemetry_request_match = str(dispatch_telemetry.get("request_id") or "").strip() == dispatch_identifier
    dispatch_telemetry_task_match = str(dispatch_telemetry.get("task_id") or "").strip() == dispatch_identifier
    dispatch_telemetry_correlation_match = str(dispatch_telemetry.get("correlation_id") or "").strip() == str(bridge_publication.get("correlation_id") or "").strip()
    dispatch_telemetry_host_received = bool(str(dispatch_telemetry.get("host_received_timestamp") or "").strip())
    dispatch_telemetry_host_completed = bool(str(dispatch_telemetry.get("host_completed_timestamp") or "").strip())
    merged_host_before = _merge_host_payload(arm_state_before, host_state_before)
    merged_host_after = _merge_host_payload(arm_state_after, host_state_after)
    arm_matches = _find_matches(merged_host_after, dispatch_identifier)
    before_evidence = _extract_command_evidence(merged_host_before)
    after_evidence = _extract_command_evidence(merged_host_after)

    report = {
        "check_type": "mim_arm_dispatch_attribution_check",
        "started_at": started_at,
        "completed_at": _utcnow(),
        "inputs": {
            "base_url": str(args.base_url),
            "arm_base_url": str(args.arm_base_url),
            "remote_host": str(args.remote_host),
            "remote_command_status": str(args.remote_command_status),
            "action": action_name,
            "execution_endpoint": f"/mim/arm/executions/{action_slug}",
        },
        "readiness_refresh": refresh_result,
        "publish": {
            "status_code": publish_status_code,
            "request": publish_payload,
            "response": publish_response,
            "bridge_publication": bridge_publication,
            "dispatch_identifier": dispatch_identifier,
            "dispatch_identifier_kind": identifier_kind,
            "task_id": str(bridge_publication.get("task_id") or "").strip(),
            "request_id": str(bridge_publication.get("request_id") or "").strip(),
            "correlation_id": str(bridge_publication.get("correlation_id") or "").strip(),
        },
        "evidence": {
            "publication_boundary": {
                "path": str(Path(str(args.publication_boundary)).expanduser().resolve()),
                "payload": publication_boundary,
                "remote_publication_matches_dispatch_identifier": publication_boundary_matches,
            },
            "remote_command_status": {
                "available": remote_status_available and not bool(command_status_after_error),
                "before": command_status_before,
                "before_error": command_status_before_error,
                "after": command_status_after,
                "after_error": command_status_after_error,
                "fresh_dispatch_identifier_matches": remote_matches,
                "authoritative_fresh_dispatch_identifier_matches": remote_authoritative_matches,
                "trigger_saw_fresh_dispatch_identifier": bool(remote_matches),
                "consumed_fresh_dispatch_identifier": bool(remote_authoritative_matches),
                "surface_note": "Remote arm-host readiness surface seeded by refresh_execution_readiness.py; diagnostic only for this check.",
                "classification": remote_status_classification,
            },
            "synced_tod_response_artifacts": {
                "task_ack_path": str(Path(str(args.local_task_ack)).expanduser().resolve()),
                "task_result_path": str(Path(str(args.local_task_result)).expanduser().resolve()),
                "task_ack": task_ack,
                "task_result": task_result,
                "task_ack_occurrences": task_ack_occurrences,
                "task_result_occurrences": task_result_occurrences,
                "task_ack_match": task_ack_match,
                "task_result_match": task_result_match,
                "task_ack_matches_dispatch_identifier": bool(task_ack_match.get("matched")),
                "task_result_matches_dispatch_identifier": bool(task_result_match.get("matched")),
            },
            "dispatch_telemetry": {
                "available": bool(dispatch_telemetry) and not bool(dispatch_telemetry_error),
                "error": dispatch_telemetry_error,
                "payload": dispatch_telemetry,
                "request_id_matches_dispatch_identifier": dispatch_telemetry_request_match,
                "task_id_matches_dispatch_identifier": dispatch_telemetry_task_match,
                "correlation_id_matches_publish_response": dispatch_telemetry_correlation_match,
                "dispatch_timestamp_present": bool(str(dispatch_telemetry.get("dispatch_timestamp") or "").strip()),
                "host_received_timestamp_present": dispatch_telemetry_host_received,
                "host_completed_timestamp_present": dispatch_telemetry_host_completed,
            },
            "live_arm_state": {
                "before": arm_state_before,
                "after": arm_state_after,
                "before_command_evidence": _extract_command_evidence(arm_state_before),
                "after_command_evidence": _extract_command_evidence(arm_state_after),
            },
            "live_host_state_artifact": {
                "path": str(Path(str(args.local_host_state)).expanduser().resolve()),
                "before": host_state_before,
                "before_error": host_state_before_error,
                "after": host_state_after,
                "after_error": host_state_after_error,
                "before_command_evidence": before_evidence,
                "after_command_evidence": after_evidence,
                "fresh_dispatch_identifier_matches": arm_matches,
                "explicit_dispatch_identifier_attribution": bool(arm_matches),
            },
        },
        "summary": {
            "dispatch_identifier": dispatch_identifier,
            "dispatch_identifier_kind": identifier_kind,
            "remote_publication_boundary_matches_dispatch_identifier": publication_boundary_matches,
            "remote_command_status_available": remote_status_available and not bool(command_status_after_error),
            "remote_command_status_surface_kind": remote_status_classification.get("surface_kind"),
            "remote_command_status_dispatch_identifier_expected": remote_status_classification.get("dispatch_identifier_expected_on_surface"),
            "remote_command_status_supports_dispatch_consumption_proof": remote_status_classification.get("supports_dispatch_consumption_proof"),
            "dispatch_telemetry_available": bool(dispatch_telemetry) and not bool(dispatch_telemetry_error),
            "dispatch_telemetry_request_id_matches": dispatch_telemetry_request_match,
            "dispatch_telemetry_task_id_matches": dispatch_telemetry_task_match,
            "dispatch_telemetry_correlation_id_matches": dispatch_telemetry_correlation_match,
            "dispatch_telemetry_host_received_timestamp_present": dispatch_telemetry_host_received,
            "dispatch_telemetry_host_completed_timestamp_present": dispatch_telemetry_host_completed,
            "dispatch_telemetry_completion_status": str(dispatch_telemetry.get("completion_status") or "").strip(),
            "dispatch_telemetry_dispatch_status": str(dispatch_telemetry.get("dispatch_status") or "").strip(),
            "tod_trigger_saw_fresh_dispatch_identifier": bool(remote_matches),
            "tod_consumed_fresh_dispatch_identifier": bool(remote_authoritative_matches),
            "tod_ack_matches_dispatch_identifier": bool(task_ack_match.get("matched")),
            "tod_result_matches_dispatch_identifier": bool(task_result_match.get("matched")),
            "host_command_counters_changed": before_evidence.get("commands_total") != after_evidence.get("commands_total")
            or before_evidence.get("acks_total") != after_evidence.get("acks_total"),
            "host_last_command_changed": before_evidence.get("last_command_sent") != after_evidence.get("last_command_sent")
            or before_evidence.get("last_command_sent_at") != after_evidence.get("last_command_sent_at"),
            "host_pose_changed": arm_state_before.get("current_pose") != arm_state_after.get("current_pose"),
            "host_explicitly_attributes_fresh_dispatch_identifier": bool(arm_matches),
            "host_after_request_id": after_evidence.get("request_id"),
            "host_after_task_id": after_evidence.get("task_id"),
            "proof_chain_complete": bool(
                publication_boundary_matches
                and bool(dispatch_telemetry)
                and dispatch_telemetry_request_match
                and dispatch_telemetry_task_match
                and dispatch_telemetry_correlation_match
                and dispatch_telemetry_host_received
                and dispatch_telemetry_host_completed
                and bool(task_ack_match.get("matched"))
                and bool(task_result_match.get("matched"))
                and arm_matches
            ),
        },
    }

    report_dir = Path(str(args.report_dir)).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"mim_arm_dispatch_attribution_check.{dispatch_identifier}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report_path": str(report_path), "report": report}, indent=2))
    return 0 if bool(report["summary"]["proof_chain_complete"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())