#!/usr/bin/env python3
"""Capture filesystem and namespace facts for the canonical task request path."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import shlex
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import paramiko
except Exception:  # pragma: no cover - optional dependency fallback
    paramiko = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = PROJECT_ROOT / "runtime" / "shared" / "MIM_TOD_TASK_REQUEST.latest.json"
DEFAULT_CANONICAL_SSH_USER = os.getenv("MIM_TOD_SSH_USER", os.getenv("MIM_TOD_SSH_HOST_USER", "testpilot"))
DEFAULT_CANONICAL_SSH_PORT = int(os.getenv("MIM_TOD_SSH_PORT", "22") or "22")
DEFAULT_CANONICAL_PASSWORD_ENV = "MIM_TOD_SSH_PASS"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--path",
        default=str(DEFAULT_PATH),
        help="Path to inspect.",
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
        default=DEFAULT_CANONICAL_SSH_USER,
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
        default=DEFAULT_CANONICAL_SSH_PORT,
        help="SSH port for remote probing.",
    )
    parser.add_argument(
        "--ssh-key",
        default="",
        help="Optional SSH private key path for remote probing.",
    )
    parser.add_argument(
        "--password-env",
        default=DEFAULT_CANONICAL_PASSWORD_ENV,
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
        or os.getenv("MIM_TOD_SSH_PASSWORD", "")
        or os.getenv("MIM_TOD_SSH_PASS", "")
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
    base.extend([
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=5",
    ])
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


def _script_source() -> str:
    return Path(__file__).read_text(encoding="utf-8")


def _read_mountinfo() -> list[dict[str, Any]]:
    mountinfo_path = Path("/proc/self/mountinfo")
    entries: list[dict[str, Any]] = []
    if not mountinfo_path.exists():
        return entries
    for raw_line in mountinfo_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if " - " not in raw_line:
            continue
        pre, post = raw_line.split(" - ", 1)
        pre_parts = pre.split()
        post_parts = post.split()
        if len(pre_parts) < 6 or len(post_parts) < 3:
            continue
        entries.append(
            {
                "mount_id": pre_parts[0],
                "parent_id": pre_parts[1],
                "device": pre_parts[2],
                "root": pre_parts[3],
                "mount_point": pre_parts[4],
                "mount_options": pre_parts[5],
                "optional_fields": pre_parts[6:],
                "filesystem_type": post_parts[0],
                "mount_source": post_parts[1],
                "super_options": post_parts[2:],
            }
        )
    return entries


def _matching_mount(path_str: str, mountinfo: list[dict[str, Any]]) -> dict[str, Any]:
    best: dict[str, Any] | None = None
    best_len = -1
    for entry in mountinfo:
        mount_point = str(entry.get("mount_point") or "")
        if not mount_point:
            continue
        if path_str == mount_point or path_str.startswith(mount_point.rstrip("/") + "/"):
            if len(mount_point) > best_len:
                best = entry
                best_len = len(mount_point)
    return best or {}


def _stat_summary(path: Path) -> dict[str, Any]:
    absolute_path = str(path.absolute())
    real_path = os.path.realpath(absolute_path)
    stat_result = path.stat()
    return {
        "path": str(path),
        "absolute_path": absolute_path,
        "realpath": real_path,
        "exists": True,
        "device_id": stat_result.st_dev,
        "inode": stat_result.st_ino,
        "mode": stat_result.st_mode,
        "size": stat_result.st_size,
    }


def _namespace_hint(path: str) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def build_report(path_str: str) -> dict[str, Any]:
    target_input = Path(path_str).expanduser()
    absolute_path = str(target_input.absolute())
    real_path = os.path.realpath(absolute_path)
    target_path = Path(real_path)
    cwd = Path.cwd()
    cwd_absolute = str(cwd.absolute())
    cwd_realpath = os.path.realpath(cwd_absolute)
    parent_chain = [Path("/")]
    current = target_path.parent
    discovered: list[Path] = []
    while True:
        discovered.append(current)
        if current == current.parent:
            break
        current = current.parent
    parent_chain = list(reversed(discovered))
    mountinfo = _read_mountinfo()
    target_mount = _matching_mount(real_path, mountinfo)

    parent_entries = []
    for candidate in parent_chain:
        if candidate.exists():
            candidate_real = os.path.realpath(str(candidate.absolute()))
            candidate_stat = candidate.stat()
            candidate_mount = _matching_mount(candidate_real, mountinfo)
            parent_entries.append(
                {
                    "path": str(candidate),
                    "absolute_path": str(candidate.absolute()),
                    "realpath": candidate_real,
                    "device_id": candidate_stat.st_dev,
                    "inode": candidate_stat.st_ino,
                    "filesystem_type": candidate_mount.get("filesystem_type", ""),
                    "mount_point": candidate_mount.get("mount_point", ""),
                    "mount_source": candidate_mount.get("mount_source", ""),
                }
            )

    namespace = {
        "mount_namespace": _namespace_hint("/proc/self/ns/mnt"),
        "pid_namespace": _namespace_hint("/proc/self/ns/pid"),
        "uts_namespace": _namespace_hint("/proc/self/ns/uts"),
    }
    container_markers = {
        "dockerenv": Path("/.dockerenv").exists(),
        "containerenv": Path("/run/.containerenv").exists(),
        "cgroup_preview": Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace").splitlines()[:10]
        if Path("/proc/1/cgroup").exists()
        else [],
    }

    report: dict[str, Any] = {
        "type": "canonical_task_request_filesystem_probe_v1",
        "hostname": socket.gethostname(),
        "whoami": getpass.getuser(),
        "cwd": str(cwd),
        "cwd_absolute": cwd_absolute,
        "cwd_realpath": cwd_realpath,
        "requested_path": path_str,
        "absolute_path": absolute_path,
        "realpath": real_path,
        "target": {
            "exists": target_path.exists(),
            "stats": _stat_summary(target_path) if target_path.exists() else {},
            "filesystem_type": target_mount.get("filesystem_type", ""),
            "mount_point": target_mount.get("mount_point", ""),
            "mount_source": target_mount.get("mount_source", ""),
            "mount_device": target_mount.get("device", ""),
        },
        "parent_chain": parent_entries,
        "relevant_mounts": [
            entry
            for entry in mountinfo
            if any(
                str(entry.get("mount_point") or "") == item["realpath"]
                or real_path.startswith(str(entry.get("mount_point") or "").rstrip("/") + "/")
                for item in parent_entries
            )
        ],
        "namespace": namespace,
        "container_markers": container_markers,
    }
    return report


def _remote_command(path: str, pretty: bool) -> str:
    remote_python = str(PROJECT_ROOT / ".venv" / "bin" / "python")
    arguments = [shlex.quote(remote_python), "-", "--path", shlex.quote(path)]
    if pretty:
        arguments.append("--pretty")
    python_invocation = " ".join(arguments)
    fallback_invocation = " ".join(["python3", "-", "--path", shlex.quote(path), *(["--pretty"] if pretty else [])])
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
    pretty: bool,
) -> dict[str, Any]:
    if paramiko is None:
        raise RuntimeError("paramiko is required for password-based remote probing when sshpass is unavailable")
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
        command = _remote_command(path=path, pretty=pretty)
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


def _sample_remote(args: argparse.Namespace) -> dict[str, Any]:
    password = _resolve_password(args.password_env)
    if password and shutil.which("sshpass") is None:
        return _run_remote_via_paramiko(
            host=args.host,
            ssh_user=args.ssh_user,
            ssh_port=args.ssh_port,
            password_env=args.password_env,
            ssh_key=args.ssh_key,
            ssh_config_host=args.ssh_config_host,
            path=args.path,
            pretty=args.pretty,
        )
    command = [
        *_ssh_base_command(
            host=args.host,
            ssh_config_host=args.ssh_config_host,
            ssh_user=args.ssh_user,
            ssh_port=args.ssh_port,
            password_env=args.password_env,
            ssh_key=args.ssh_key,
        ),
        "bash",
        "-lc",
        _remote_command(path=args.path, pretty=args.pretty),
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


def main() -> int:
    args = parse_args()
    report = _sample_remote(args) if (args.host or args.ssh_config_host) else build_report(args.path)
    json.dump(report, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())