#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import paramiko
except Exception:  # pragma: no cover - optional dependency fallback
    paramiko = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_PATH = PROJECT_ROOT / "runtime" / "shared" / "TOD_MIM_COMMAND_STATUS.latest.json"
DEFAULT_REMOTE_PATH = "/home/testpilot/mim/runtime/shared/TOD_MIM_COMMAND_STATUS.latest.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_execution_readiness_payload(
    *,
    action: str,
    detail: str,
    source: str,
    request_id: str,
    task_id: str,
    correlation_id: str,
) -> dict[str, object]:
    generated_at = _utcnow()
    readiness = {
        "status": "valid",
        "source": source,
        "detail": detail,
        "valid": True,
        "execution_allowed": True,
        "authoritative": True,
        "freshness_state": "fresh",
        "signal_name": "execution-readiness",
        "evaluated_action": action,
        "policy_outcome": "allow",
        "decision_path": [
            "signal:execution-readiness",
            "status:valid",
            f"source:{source}",
            f"action:{action}",
            "policy_outcome:allow",
        ],
        "generated_at": generated_at,
    }
    attribution = {key: value for key, value in {
        "request_id": request_id,
        "task_id": task_id,
        "correlation_id": correlation_id,
    }.items() if value}
    payload = {
        "generated_at": generated_at,
        "source": "tod-mim-command-status-v1",
        "type": "tod_mim_command_status_v1",
        "status": "ready",
        "detail": detail,
        "acted_upon": False,
        "action": action,
        "request_id": request_id or task_id,
        "task_id": task_id or request_id,
        "execution_readiness": readiness,
        "execution_trace": {
            "action": action,
            "execution_readiness": readiness,
        },
        "bridge_runtime": {
            "current_processing": attribution,
        },
        "metadata_json": {
            "refreshed_by": "refresh_execution_readiness.py",
            "attribution": attribution,
        },
    }
    return payload


def _write_local(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resolve_password(password_env: str) -> str:
    return os.getenv(password_env, "") or os.getenv("MIM_ARM_SSH_HOST_PASS", "") or os.getenv("MIM_ARM_SSH_PASSWORD", "")


def _write_remote_via_paramiko(*, host: str, ssh_user: str, ssh_port: int, password: str, remote_path: str, payload: dict[str, object]) -> None:
    if paramiko is None:
        raise RuntimeError("paramiko is required for password-based remote readiness refresh when sshpass is unavailable")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=ssh_user, password=password, port=ssh_port, timeout=8)
    try:
        sftp = client.open_sftp()
        try:
            remote_parent = str(Path(remote_path).parent)
            client.exec_command(f"mkdir -p {remote_parent}")[1].channel.recv_exit_status()
            with sftp.file(remote_path, "w") as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        finally:
            sftp.close()
    finally:
        client.close()


def _write_remote_via_ssh(*, host: str, ssh_user: str, ssh_port: int, password_env: str, remote_path: str, payload: dict[str, object]) -> None:
    password = _resolve_password(password_env)
    if password and shutil.which("sshpass") is None:
        _write_remote_via_paramiko(
            host=host,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            password=password,
            remote_path=remote_path,
            payload=payload,
        )
        return

    with subprocess.Popen(
        [
            *( [shutil.which("sshpass"), "-p", password] if password and shutil.which("sshpass") else [] ),
            "ssh",
            "-o",
            "ConnectTimeout=5",
            "-p",
            str(ssh_port),
            *( [] if password else ["-o", "BatchMode=yes"] ),
            f"{ssh_user}@{host}",
            f"cat > {remote_path}",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        stdout, stderr = process.communicate(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        if process.returncode != 0:
            raise RuntimeError(stderr.strip() or stdout.strip() or f"remote write failed with exit {process.returncode}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh execution readiness locally and optionally on the remote TOD boundary.")
    parser.add_argument("--action", default="safe_home")
    parser.add_argument("--detail", default="Fresh execution readiness refreshed before controlled publish verification.")
    parser.add_argument("--source", default="controlled_publish_refresh")
    parser.add_argument("--request-id", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--correlation-id", default="")
    parser.add_argument("--local-output", default=str(DEFAULT_LOCAL_PATH))
    parser.add_argument("--skip-local", action="store_true")
    parser.add_argument("--remote-host", default=os.getenv("MIM_ARM_SSH_HOST", "192.168.1.90"))
    parser.add_argument("--remote-user", default=os.getenv("MIM_ARM_SSH_HOST_USER", os.getenv("MIM_ARM_SSH_USER", "testpilot")))
    parser.add_argument("--remote-port", type=int, default=int(os.getenv("MIM_ARM_SSH_HOST_PORT", "22") or "22"))
    parser.add_argument("--remote-output", default=DEFAULT_REMOTE_PATH)
    parser.add_argument("--password-env", default="MIM_ARM_SSH_HOST_PASS")
    parser.add_argument("--skip-remote", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_execution_readiness_payload(
        action=str(args.action or "safe_home").strip() or "safe_home",
        detail=str(args.detail or "").strip() or "Fresh execution readiness refreshed before controlled publish verification.",
        source=str(args.source or "controlled_publish_refresh").strip() or "controlled_publish_refresh",
        request_id=str(args.request_id or "").strip(),
        task_id=str(args.task_id or "").strip(),
        correlation_id=str(args.correlation_id or "").strip(),
    )
    outputs: dict[str, str] = {}
    if not bool(args.skip_local):
        local_output = Path(str(args.local_output)).expanduser().resolve()
        _write_local(local_output, payload)
        outputs["local_output"] = str(local_output)
    if not bool(args.skip_remote):
        _write_remote_via_ssh(
            host=str(args.remote_host),
            ssh_user=str(args.remote_user),
            ssh_port=int(args.remote_port),
            password_env=str(args.password_env),
            remote_path=str(args.remote_output),
            payload=payload,
        )
        outputs["remote_output"] = str(args.remote_output)
    print(json.dumps({"payload": payload, "outputs": outputs}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())