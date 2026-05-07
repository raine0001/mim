#!/usr/bin/env python3
"""Generate a bounded, read-only startup diagnostic for the MIM arm host."""

from __future__ import annotations

import argparse
import json
import shlex
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "runtime" / "shared" / "mim_arm_startup_diagnostic.latest.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_local(command: list[str], timeout: int = 10) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "command": command,
            "returncode": None,
            "stdout": (exc.stdout or "").strip() if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "").strip() if isinstance(exc.stderr, str) else "",
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "error": f"timeout_after_{timeout}s",
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "error": "command_not_found",
        }


def run_remote(host: str, ssh_user: str, remote_command: str, timeout: int = 15) -> dict[str, Any]:
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        f"{ssh_user}@{host}",
        remote_command,
    ]
    result = run_local(command, timeout=timeout)
    result["remote_command"] = remote_command
    return result


def check_connectivity(host: str) -> dict[str, Any]:
    ping = run_local(["ping", "-c", "1", "-W", "2", host], timeout=5)
    tcp = {"reachable": False, "error": ""}
    started = time.monotonic()
    try:
        with socket.create_connection((host, 22), timeout=2):
            tcp = {
                "reachable": True,
                "port": 22,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            }
    except OSError as exc:
        tcp = {
            "reachable": False,
            "port": 22,
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "error": str(exc),
        }
    return {
        "host": host,
        "ping": ping,
        "ssh_tcp_probe": tcp,
        "host_reachable": bool(ping.get("ok")) or bool(tcp.get("reachable")),
    }


def remote_or_placeholder(
    host: str,
    ssh_user: str | None,
    remote_command: str,
    *,
    placeholder_reason: str,
    timeout: int = 15,
) -> dict[str, Any]:
    if not ssh_user:
        return {
            "ok": False,
            "remote_command": remote_command,
            "placeholder": True,
            "reason": placeholder_reason,
        }
    return run_remote(host, ssh_user, remote_command, timeout=timeout)


def build_likely_root_cause(summary: dict[str, Any]) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    connectivity = summary["connectivity"]
    if not connectivity.get("host_reachable"):
        candidates.append(
            {
                "category": "connectivity",
                "confidence": "high",
                "reason": "Host is unreachable by ping and/or TCP probe.",
            }
        )

    process_stdout = str(summary["process_service"]["active_processes"].get("stdout", ""))
    if not process_stdout:
        candidates.append(
            {
                "category": "process/service",
                "confidence": "medium",
                "reason": "No matching arm UI process was found from the inspected process list.",
            }
        )

    port_stdout = str(summary["ports"]["port_bindings"].get("stdout", ""))
    if summary["ports"].get("expected_ports") and not port_stdout:
        candidates.append(
            {
                "category": "port-binding",
                "confidence": "medium",
                "reason": "Expected application ports are not currently bound.",
            }
        )

    if not candidates:
        candidates.append(
            {
                "category": "unknown",
                "confidence": "low",
                "reason": "No single dominant failure mode detected from the bounded startup diagnostic.",
            }
        )

    return {
        "summary": candidates[0]["reason"],
        "candidates": candidates,
    }


def build_recovery_actions(args: argparse.Namespace, summary: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if not summary["connectivity"].get("host_reachable"):
        actions.append(
            {
                "action": "verify_host_reachability",
                "requires_approval": True,
                "details": f"Verify network reachability to {args.host} before any remote remediation.",
            }
        )
    if args.service_name:
        actions.append(
            {
                "action": "restart_service",
                "requires_approval": True,
                "details": f"Restart service '{args.service_name}' only after operator approval.",
            }
        )
    if args.startup_command:
        actions.append(
            {
                "action": "run_startup_command_manually",
                "requires_approval": True,
                "details": f"Run startup command manually for observation: {args.startup_command}",
            }
        )
    actions.extend(
        [
            {
                "action": "reinstall_missing_dependency",
                "requires_approval": True,
                "details": "Use only if logs or environment checks show a missing Python or system dependency.",
            },
            {
                "action": "correct_path_or_env_mismatch",
                "requires_approval": True,
                "details": "Use only if service/startup paths differ from deployed configuration.",
            },
            {
                "action": "release_occupied_port",
                "requires_approval": True,
                "details": "Use only if a conflicting listener is confirmed on an expected application port.",
            },
            {
                "action": "restore_config_file",
                "requires_approval": True,
                "details": "Use only if config presence or hash verification shows drift or removal.",
            },
            {
                "action": "verify_camera_permission_and_mapping",
                "requires_approval": True,
                "details": "Use after app recovery to confirm camera device path and permissions.",
            },
        ]
    )
    return actions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.1.90")
    parser.add_argument("--ssh-user", default="")
    parser.add_argument("--service-name", default="")
    parser.add_argument("--process-match", default="mim_arm|arm_ui|mim arm")
    parser.add_argument("--startup-command", default="")
    parser.add_argument("--log-command", default="journalctl -u {service} -n 120 --no-pager")
    parser.add_argument("--expected-port", action="append", default=[])
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ssh_user = args.ssh_user.strip() or None
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    connectivity = check_connectivity(args.host)
    service_name = args.service_name.strip()
    log_command = args.log_command.format(service=service_name or "mim-arm-ui")

    process_service = {
        "active_processes": remote_or_placeholder(
            args.host,
            ssh_user,
            f"ps -ef | grep -E {shlex.quote(args.process_match)} | grep -v grep",
            placeholder_reason="ssh_user_not_provided",
        ),
        "service_status": remote_or_placeholder(
            args.host,
            ssh_user,
            f"systemctl status {shlex.quote(service_name)} --no-pager" if service_name else "echo service_name_not_provided",
            placeholder_reason="ssh_user_not_provided_or_service_name_missing",
        ),
        "startup_script_or_command": {
            "declared_startup_command": args.startup_command,
            "service_name": service_name,
        },
    }

    logs = {
        "recent_app_logs": remote_or_placeholder(
            args.host,
            ssh_user,
            log_command,
            placeholder_reason="ssh_user_not_provided",
            timeout=20,
        )
    }

    devices = {
        "camera_device_availability": remote_or_placeholder(
            args.host,
            ssh_user,
            "ls -l /dev/video* 2>/dev/null || true",
            placeholder_reason="ssh_user_not_provided",
        ),
        "serial_controller_port_availability": remote_or_placeholder(
            args.host,
            ssh_user,
            "ls -l /dev/ttyUSB* /dev/ttyACM* /dev/serial/by-id/* 2>/dev/null || true",
            placeholder_reason="ssh_user_not_provided",
        ),
    }

    system_basics = {
        "disk_memory_basics": remote_or_placeholder(
            args.host,
            ssh_user,
            "printf '--- df -h ---\n'; df -h; printf '\n--- free -m ---\n'; free -m",
            placeholder_reason="ssh_user_not_provided",
        ),
        "python_env_dependency_failures": remote_or_placeholder(
            args.host,
            ssh_user,
            "python3 -m pip check 2>/dev/null || python -m pip check 2>/dev/null || true",
            placeholder_reason="ssh_user_not_provided",
            timeout=20,
        ),
    }

    ports = {
        "expected_ports": args.expected_port,
        "port_bindings": remote_or_placeholder(
            args.host,
            ssh_user,
            "ss -ltnp || netstat -ltnp",
            placeholder_reason="ssh_user_not_provided",
        ),
    }

    summary = {
        "artifact_name": output_path.name,
        "generated_at": utc_now(),
        "project": "MIM Arm Recovery and Integration Baseline",
        "task": {
            "title": "Diagnose why the MIM arm UI app is not starting on target host",
            "host": args.host,
            "constraints": [
                "no live motion commands",
                "no config changes without approval",
                "gather logs and root-cause candidates first",
                "prefer read-only inspection before remediation",
                "produce artifact summary and next-step recommendation",
            ],
        },
        "connectivity": connectivity,
        "process_service": process_service,
        "logs": logs,
        "devices": devices,
        "system_basics": system_basics,
        "ports": ports,
    }
    summary["likely_root_cause"] = build_likely_root_cause(summary)
    summary["suggested_recovery_actions"] = build_recovery_actions(args, summary)
    summary["approval_policy"] = {
        "remediation_requires_operator_approval": True,
        "allowed_without_approval": [
            "read_only_inspection",
            "artifact_generation",
            "status_ingestion_after_app_recovery",
        ],
    }
    summary["next_phase_after_recovery"] = {
        "mode": "read_only_mim_input",
        "fields": [
            "app_alive",
            "arm_status",
            "camera_status",
            "current_pose",
            "estop_status",
            "active_mode",
            "last_error",
            "recent_command_result",
        ],
    }

    output_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
