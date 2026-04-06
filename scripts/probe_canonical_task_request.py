#!/usr/bin/env python3
"""Emit a normalized fingerprint for the canonical live task request artifact."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import paramiko
except Exception:  # pragma: no cover - optional dependency fallback
    paramiko = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "runtime" / "shared" / "MIM_TOD_TASK_REQUEST.latest.json"


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        default=str(DEFAULT_PATH),
        help="Absolute path to the authoritative task-request file.",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="Number of samples to collect. Use more than one to detect rewrite races.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=0.5,
        help="Delay between samples when --samples > 1.",
    )
    parser.add_argument(
        "--host",
        default="",
        help="Optional remote host. When set, probe the path over SSH on that host.",
    )
    parser.add_argument(
        "--user",
        "--ssh-user",
        dest="ssh_user",
        default=os.getenv("MIM_ARM_SSH_HOST_USER", os.getenv("MIM_ARM_SSH_USER", "testpilot")),
        help="SSH user for remote probing.",
    )
    parser.add_argument(
        "--ssh-config-host",
        default="",
        help="Optional SSH config host alias to use instead of a raw host name.",
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=int(os.getenv("MIM_ARM_SSH_HOST_PORT", "22") or "22"),
        help="SSH port for remote probing.",
    )
    parser.add_argument(
        "--ssh-key",
        default="",
        help="Optional SSH private key path for remote probing.",
    )
    parser.add_argument(
        "--password-env",
        default="MIM_ARM_SSH_HOST_PASS",
        help="Environment variable holding the SSH password when sshpass is needed.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser.parse_args()


def _resolve_password(password_env: str) -> str:
    return (
        os.getenv(password_env, "")
        or os.getenv("MIM_ARM_SSH_HOST_PASS", "")
        or os.getenv("MIM_ARM_SSH_PASSWORD", "")
    )


def _ssh_base_command(
    *,
    host: str,
    ssh_config_host: str,
    ssh_user: str,
    ssh_port: int,
    password_env: str,
    ssh_key: str,
) -> list[str]:
    password = _resolve_password(password_env)
    base: list[str] = []
    if password:
        sshpass = shutil.which("sshpass")
        if sshpass:
            base.extend([sshpass, "-p", password])
    destination_host = ssh_config_host.strip() or host.strip()
    if not destination_host:
        raise RuntimeError("remote probing requires --host or --ssh-config-host")
    base.extend(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=5",
        ]
    )
    if ssh_key.strip():
        base.extend(["-i", ssh_key.strip()])
    if not ssh_config_host.strip():
        base.extend(["-p", str(ssh_port)])
    if not password:
        base.extend(["-o", "BatchMode=yes"])
    if ssh_user.strip():
        base.append(f"{ssh_user}@{destination_host}")
    else:
        base.append(destination_host)
    return base


def _normalize_text(value: object) -> str:
    return str(value or "").strip()


def _read_payload(path: Path) -> tuple[dict[str, Any], bytes]:
    data = path.read_bytes()
    payload = json.loads(data.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return payload, data


def _sample_local(path_str: str) -> dict[str, Any]:
    sampled_at = isoformat_z(datetime.now(timezone.utc))
    path = Path(path_str).expanduser()
    absolute_path = str(path.absolute())
    real_path = os.path.realpath(absolute_path)
    stat_result = Path(real_path).stat()
    payload, raw_bytes = _read_payload(Path(real_path))
    return {
        "hostname": socket.gethostname(),
        "whoami": getpass.getuser(),
        "absolute_path": absolute_path,
        "realpath": real_path,
        "sampled_at": sampled_at,
        "pwd": str(Path.cwd()),
        "ls_inode": stat_result.st_ino,
        "mtime": isoformat_z(datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc)),
        "size": stat_result.st_size,
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "task_id": _normalize_text(payload.get("task_id") or payload.get("request_id")),
        "request_id": _normalize_text(payload.get("request_id")),
        "objective_id": _normalize_text(payload.get("objective_id")),
        "correlation_id": _normalize_text(payload.get("correlation_id")),
        "generated_at": _normalize_text(payload.get("generated_at")),
        "emitted_at": _normalize_text(payload.get("emitted_at")),
        "sequence": payload.get("sequence"),
        "source_service": _normalize_text(payload.get("source_service")),
        "source_instance_id": _normalize_text(payload.get("source_instance_id")),
    }


def _remote_script_path() -> str:
    return str(Path(__file__).resolve())


def _script_source() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def _remote_command(*, path: str, samples: int, interval_seconds: float, pretty: bool) -> str:
    remote_python = str(PROJECT_ROOT / ".venv" / "bin" / "python")
    arguments = [
        shlex.quote(remote_python),
        "-",
        "--path",
        shlex.quote(path),
        "--samples",
        shlex.quote(str(samples)),
        "--interval-seconds",
        shlex.quote(str(interval_seconds)),
    ]
    if pretty:
        arguments.append("--pretty")
    python_invocation = " ".join(arguments)
    fallback_invocation = " ".join(
        [
            "python3",
            "-",
            "--path",
            shlex.quote(path),
            "--samples",
            shlex.quote(str(samples)),
            "--interval-seconds",
            shlex.quote(str(interval_seconds)),
            *( ["--pretty"] if pretty else [] ),
        ]
    )
    return (
        "if [ -x {remote_python} ]; then exec {python_invocation}; "
        "else exec {fallback_invocation}; fi"
    ).format(
        remote_python=shlex.quote(remote_python),
        python_invocation=python_invocation,
        fallback_invocation=fallback_invocation,
    )


def _run_remote_via_paramiko(
    *,
    host: str,
    ssh_user: str,
    ssh_port: int,
    password_env: str,
    ssh_key: str,
    ssh_config_host: str,
    path: str,
    samples: int,
    interval_seconds: float,
    pretty: bool,
) -> dict[str, Any]:
    if paramiko is None:
        raise RuntimeError(
            "paramiko is required for password-based remote probing when sshpass is unavailable"
        )
    destination_host = ssh_config_host.strip() or host.strip()
    if not destination_host:
        raise RuntimeError("remote probing requires --host or --ssh-config-host")

    password = _resolve_password(password_env)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict[str, Any] = {
        "hostname": destination_host,
        "username": ssh_user.strip() or None,
        "port": ssh_port,
        "timeout": 8,
    }
    if ssh_key.strip():
        connect_kwargs["key_filename"] = ssh_key.strip()
    elif password:
        connect_kwargs["password"] = password

    client.connect(**connect_kwargs)
    try:
        command = _remote_command(
            path=path,
            samples=samples,
            interval_seconds=interval_seconds,
            pretty=pretty,
        )
        stdin, stdout, stderr = client.exec_command(f"bash -lc {shlex.quote(command)}")
        stdin.write(_script_source())
        stdin.channel.shutdown_write()
        output = stdout.read().decode("utf-8")
        error = stderr.read().decode("utf-8")
        exit_status = stdout.channel.recv_exit_status()
    finally:
        client.close()

    if exit_status != 0:
        raise RuntimeError(error.strip() or output.strip() or "remote probe failed")
    payload = json.loads(output)
    if not isinstance(payload, dict):
        raise RuntimeError("remote probe returned non-object JSON")
    return payload


def _sample_remote(
    *,
    host: str,
    ssh_config_host: str,
    ssh_user: str,
    ssh_port: int,
    password_env: str,
    ssh_key: str,
    path: str,
    samples: int,
    interval_seconds: float,
    pretty: bool,
) -> dict[str, Any]:
    password = _resolve_password(password_env)
    if password and shutil.which("sshpass") is None:
        return _run_remote_via_paramiko(
            host=host,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            password_env=password_env,
            ssh_key=ssh_key,
            ssh_config_host=ssh_config_host,
            path=path,
            samples=samples,
            interval_seconds=interval_seconds,
            pretty=pretty,
        )
    command = [
        *_ssh_base_command(
            host=host,
            ssh_config_host=ssh_config_host,
            ssh_user=ssh_user,
            ssh_port=ssh_port,
            password_env=password_env,
            ssh_key=ssh_key,
        ),
        "bash",
        "-lc",
        _remote_command(
            path=path,
            samples=samples,
            interval_seconds=interval_seconds,
            pretty=pretty,
        ),
    ]
    completed = subprocess.run(
        command,
        input=_script_source(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "remote probe failed")
    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("remote probe returned non-object JSON")
    return payload


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    samples_requested = max(1, int(args.samples or 1))
    interval_seconds = max(0.0, float(args.interval_seconds or 0.0))
    remote_mode = bool(args.host or args.ssh_config_host)
    samples: list[dict[str, Any]] = []
    if remote_mode:
        remote_report = _sample_remote(
            host=args.host,
            ssh_config_host=args.ssh_config_host,
            ssh_user=args.ssh_user,
            ssh_port=args.ssh_port,
            password_env=args.password_env,
            ssh_key=args.ssh_key,
            path=args.path,
            samples=samples_requested,
            interval_seconds=interval_seconds,
            pretty=False,
        )
        remote_samples = remote_report.get("samples") if isinstance(remote_report.get("samples"), list) else []
        samples = [item for item in remote_samples if isinstance(item, dict)]
        for index, sample in enumerate(samples, start=1):
            sample["transport"] = "ssh"
            sample["target_host"] = args.ssh_config_host or args.host
            sample["sample_index"] = index
    else:
        for index in range(samples_requested):
            sample = _sample_local(args.path)
            sample["transport"] = "local"
            sample["target_host"] = sample.get("hostname")
            sample["sample_index"] = index + 1
            samples.append(sample)
            if index + 1 < samples_requested and interval_seconds > 0:
                time.sleep(interval_seconds)

    sha_values = sorted({str(item.get("sha256") or "") for item in samples if str(item.get("sha256") or "")})
    task_ids = sorted({str(item.get("task_id") or "") for item in samples if str(item.get("task_id") or "")})
    objective_ids = sorted({str(item.get("objective_id") or "") for item in samples if str(item.get("objective_id") or "")})
    generated_ats = sorted({str(item.get("generated_at") or "") for item in samples if str(item.get("generated_at") or "")})
    sequences = [item.get("sequence") for item in samples]
    first_sample = samples[0] if samples else {}
    return {
        "hostname": str(first_sample.get("hostname") or ""),
        "whoami": str(first_sample.get("whoami") or ""),
        "absolute_path": str(first_sample.get("absolute_path") or ""),
        "realpath": str(first_sample.get("realpath") or ""),
        "generated_at": isoformat_z(datetime.now(timezone.utc)),
        "type": "canonical_task_request_probe_v1",
        "requested_path": args.path,
        "requested_host": args.ssh_config_host or args.host or "",
        "requested_user": args.ssh_user,
        "transport": "ssh" if remote_mode else "local",
        "samples_requested": samples_requested,
        "interval_seconds": interval_seconds,
        "stable": len(sha_values) == 1,
        "unique_sha256": sha_values,
        "unique_task_ids": task_ids,
        "unique_objective_ids": objective_ids,
        "unique_generated_at": generated_ats,
        "sequence_trace": sequences,
        "samples": samples,
    }


def main() -> int:
    args = parse_args()
    report = build_report(args)
    json.dump(report, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())