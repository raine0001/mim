#!/usr/bin/env python3
"""Publish bridge request/trigger artifacts into the remote shared root TOD actually polls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.tod_mim_contract import normalize_and_validate_file  # noqa: E402

try:
    import paramiko
except Exception:  # pragma: no cover - optional dependency fallback
    paramiko = None


DEFAULT_SHARED_DIR = PROJECT_ROOT / "runtime" / "shared"
BOUNDARY_STATUS_PATH = DEFAULT_SHARED_DIR / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json"


def _append_publish_audit(*, caller: str, host: str, remote_root: str, request_file: Path, trigger_file: Path, objective_id: str, request_task_id: str, trigger_task_id: str, request_generated_at: object, trigger_generated_at: object, success: bool, returncode: int, stdout: str, stderr: str, remote_request_meta: dict[str, object], remote_trigger_meta: dict[str, object]) -> None:
    from tod_bridge_audit import write_event  # type: ignore

    event = {
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "tod_bridge_write_audit_v1",
        "event": "remote_publish_transport",
        "caller": caller,
        "service_name": "publish_tod_bridge_artifacts_remote.py",
        "hostname": __import__("socket").gethostname(),
        "pid": os.getpid(),
        "cwd": os.getcwd(),
        "task_id": request_task_id,
        "objective_id": objective_id,
        "publish_target": f"ssh://{host}{remote_root}",
        "remote_host": host,
        "remote_root": remote_root,
        "publish_attempted": True,
        "publish_succeeded": success,
        "publish_returncode": returncode,
        "publish_output": stdout if success else stderr or stdout,
        "artifacts": [
            {
                "path": str(request_file),
                "absolute_path": str(request_file),
                "realpath": str(request_file.resolve()),
                "exists": request_file.exists(),
                "sha256": hashlib.sha256(request_file.read_bytes()).hexdigest(),
                "size": request_file.stat().st_size,
                "inode": request_file.stat().st_ino,
                "remote_path": str(remote_request_meta.get("path") or f"{remote_root}/MIM_TOD_TASK_REQUEST.latest.json"),
                "remote_absolute_path": str(remote_request_meta.get("absolute_path") or remote_request_meta.get("path") or f"{remote_root}/MIM_TOD_TASK_REQUEST.latest.json"),
                "remote_realpath": str(remote_request_meta.get("realpath") or remote_request_meta.get("path") or f"{remote_root}/MIM_TOD_TASK_REQUEST.latest.json"),
                "remote_sha256": str(remote_request_meta.get("sha256") or ""),
                "remote_size": remote_request_meta.get("size"),
                "remote_inode": remote_request_meta.get("inode"),
                "remote_task_id": str(remote_request_meta.get("task_id") or request_task_id),
                "remote_generated_at": remote_request_meta.get("generated_at") or request_generated_at,
            },
            {
                "path": str(trigger_file),
                "absolute_path": str(trigger_file),
                "realpath": str(trigger_file.resolve()),
                "exists": trigger_file.exists(),
                "sha256": hashlib.sha256(trigger_file.read_bytes()).hexdigest(),
                "size": trigger_file.stat().st_size,
                "inode": trigger_file.stat().st_ino,
                "remote_path": str(remote_trigger_meta.get("path") or f"{remote_root}/MIM_TO_TOD_TRIGGER.latest.json"),
                "remote_absolute_path": str(remote_trigger_meta.get("absolute_path") or remote_trigger_meta.get("path") or f"{remote_root}/MIM_TO_TOD_TRIGGER.latest.json"),
                "remote_realpath": str(remote_trigger_meta.get("realpath") or remote_trigger_meta.get("path") or f"{remote_root}/MIM_TO_TOD_TRIGGER.latest.json"),
                "remote_sha256": str(remote_trigger_meta.get("sha256") or ""),
                "remote_size": remote_trigger_meta.get("size"),
                "remote_inode": remote_trigger_meta.get("inode"),
                "remote_task_id": str(remote_trigger_meta.get("task_id") or trigger_task_id),
                "remote_generated_at": remote_trigger_meta.get("generated_at") or trigger_generated_at,
            },
        ],
    }
    write_event(event)


def _extract_remote_artifact_metadata(*, remote_path: str, raw_bytes: bytes, payload: dict[str, object], realpath: str, inode: object, size: object) -> dict[str, object]:
    return {
        "path": remote_path,
        "absolute_path": remote_path,
        "realpath": realpath,
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "size": size,
        "inode": inode,
        "task_id": str(payload.get("task_id") or ""),
        "request_id": str(payload.get("request_id") or payload.get("task_id") or ""),
        "objective_id": str(payload.get("objective_id") or ""),
        "generated_at": payload.get("generated_at"),
        "source_service": str(payload.get("source_service") or ""),
        "source_instance_id": str(payload.get("source_instance_id") or ""),
    }


def _write_boundary_status(
    *,
    host: str,
    remote_root: str,
    request_file: Path,
    trigger_file: Path,
    remote_request_meta: dict[str, object],
    remote_trigger_meta: dict[str, object],
) -> None:
    request_payload = _verify_payload(request_file)
    trigger_payload = _verify_payload(trigger_file)
    local_request_sha = hashlib.sha256(request_file.read_bytes()).hexdigest()
    local_trigger_sha = hashlib.sha256(trigger_file.read_bytes()).hexdigest()
    remote_request_path = str(remote_request_meta.get("path") or f"{remote_root}/MIM_TOD_TASK_REQUEST.latest.json")
    remote_request_realpath = str(remote_request_meta.get("realpath") or remote_request_meta.get("path") or f"{remote_root}/MIM_TOD_TASK_REQUEST.latest.json")
    remote_trigger_path = str(remote_trigger_meta.get("path") or f"{remote_root}/MIM_TO_TOD_TRIGGER.latest.json")
    remote_trigger_realpath = str(remote_trigger_meta.get("realpath") or remote_trigger_meta.get("path") or f"{remote_root}/MIM_TO_TOD_TRIGGER.latest.json")
    remote_request_source_service = str(remote_request_meta.get("source_service") or request_payload.get("source_service") or "")
    remote_request_source_instance_id = str(remote_request_meta.get("source_instance_id") or request_payload.get("source_instance_id") or "")
    local_request_task_id = str(request_payload.get("task_id") or "")
    local_request_id = str(request_payload.get("request_id") or local_request_task_id)
    remote_request_task_id = str(remote_request_meta.get("task_id") or "")
    remote_request_id = str(remote_request_meta.get("request_id") or remote_request_task_id)
    local_trigger_task_id = str(trigger_payload.get("task_id") or "")
    local_trigger_request_id = str(trigger_payload.get("request_id") or local_trigger_task_id)
    remote_trigger_task_id = str(remote_trigger_meta.get("task_id") or "")
    remote_trigger_request_id = str(remote_trigger_meta.get("request_id") or remote_trigger_task_id)
    payload = {
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "type": "mim_tod_publication_boundary_status_v1",
        "authoritative_surface": "remote_raspberry_pi",
        "authoritative_host": host,
        "authoritative_root": remote_root,
        "local_surface": str(request_file.parent),
        "request_alignment": {
            "request_id_match": remote_request_id == local_request_id,
            "task_id_match": remote_request_task_id == local_request_task_id,
            "sha256_match": str(remote_request_meta.get("sha256") or "") == local_request_sha,
        },
        "trigger_alignment": {
            "request_id_match": remote_trigger_request_id == local_trigger_request_id,
            "task_id_match": remote_trigger_task_id == local_trigger_task_id,
            "sha256_match": str(remote_trigger_meta.get("sha256") or "") == local_trigger_sha,
        },
        "remote_request": {
            "path": remote_request_path,
            "realpath": remote_request_realpath,
            "sha256": str(remote_request_meta.get("sha256") or ""),
            "inode": remote_request_meta.get("inode"),
            "size": remote_request_meta.get("size"),
            "task_id": remote_request_task_id,
            "request_id": remote_request_id,
            "objective_id": str(remote_request_meta.get("objective_id") or ""),
            "generated_at": remote_request_meta.get("generated_at"),
            "source_service": remote_request_source_service,
            "source_instance_id": remote_request_source_instance_id,
        },
        "local_request": {
            "path": str(request_file),
            "realpath": str(request_file.resolve()),
            "sha256": local_request_sha,
            "inode": request_file.stat().st_ino,
            "size": request_file.stat().st_size,
            "task_id": local_request_task_id,
            "request_id": local_request_id,
            "objective_id": str(request_payload.get("objective_id") or ""),
            "generated_at": request_payload.get("generated_at"),
            "source_service": str(request_payload.get("source_service") or ""),
            "source_instance_id": str(request_payload.get("source_instance_id") or ""),
        },
        "remote_trigger": {
            "path": remote_trigger_path,
            "realpath": remote_trigger_realpath,
            "sha256": str(remote_trigger_meta.get("sha256") or ""),
            "inode": remote_trigger_meta.get("inode"),
            "size": remote_trigger_meta.get("size"),
            "task_id": remote_trigger_task_id,
            "request_id": remote_trigger_request_id,
            "generated_at": remote_trigger_meta.get("generated_at"),
        },
        "local_trigger": {
            "path": str(trigger_file),
            "realpath": str(trigger_file.resolve()),
            "sha256": local_trigger_sha,
            "inode": trigger_file.stat().st_ino,
            "size": trigger_file.stat().st_size,
            "task_id": local_trigger_task_id,
            "request_id": local_trigger_request_id,
            "generated_at": trigger_payload.get("generated_at"),
        },
    }
    BOUNDARY_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOUNDARY_STATUS_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _collect_remote_metadata_via_ssh(*, host: str, ssh_user: str, ssh_port: int, password_env: str, remote_paths: dict[str, str]) -> dict[str, dict[str, object]]:
    remote_script = "".join([
        "python3 - <<'PY'\n",
        "import hashlib, json, os\n",
        f"paths = {json.dumps(remote_paths, sort_keys=True)}\n",
        "result = {}\n",
        "for label, path in paths.items():\n",
        "    with open(path, 'rb') as handle:\n",
        "        raw = handle.read()\n",
        "    payload = json.loads(raw.decode('utf-8-sig'))\n",
        "    stat_result = os.stat(path)\n",
        "    result[label] = {\n",
        "        'path': path,\n",
        "        'absolute_path': path,\n",
        "        'realpath': os.path.realpath(path),\n",
        "        'sha256': hashlib.sha256(raw).hexdigest(),\n",
        "        'size': stat_result.st_size,\n",
        "        'inode': getattr(stat_result, 'st_ino', None),\n",
        "        'task_id': str(payload.get('task_id') or payload.get('request_id') or ''),\n",
        "        'objective_id': str(payload.get('objective_id') or ''),\n",
        "        'generated_at': payload.get('generated_at'),\n",
        "    }\n",
        "print(json.dumps(result))\n",
        "PY",
    ])
    completed = subprocess.run(
        [
            *(_ssh_base_command(host, ssh_user, ssh_port, password_env)),
            remote_script,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    return payload if isinstance(payload, dict) else {}


def _collect_remote_metadata_via_paramiko(*, client, remote_paths):
    remote_script = "".join([
        "python3 - <<'PY'\n",
        "import hashlib, json, os\n",
        f"paths = {json.dumps(remote_paths, sort_keys=True)}\n",
        "result = {}\n",
        "for label, path in paths.items():\n",
        "    with open(path, 'rb') as handle:\n",
        "        raw = handle.read()\n",
        "    payload = json.loads(raw.decode('utf-8-sig'))\n",
        "    stat_result = os.stat(path)\n",
        "    result[label] = {\n",
        "        'path': path,\n",
        "        'absolute_path': path,\n",
        "        'realpath': os.path.realpath(path),\n",
        "        'sha256': hashlib.sha256(raw).hexdigest(),\n",
        "        'size': stat_result.st_size,\n",
        "        'inode': getattr(stat_result, 'st_ino', None),\n",
        "        'task_id': str(payload.get('task_id') or payload.get('request_id') or ''),\n",
        "        'objective_id': str(payload.get('objective_id') or ''),\n",
        "        'generated_at': payload.get('generated_at'),\n",
        "    }\n",
        "print(json.dumps(result))\n",
        "PY",
    ])
    stdin, stdout, stderr = client.exec_command(remote_script)
    _ = stdin
    exit_status = stdout.channel.recv_exit_status()
    if exit_status != 0:
        raise RuntimeError(stderr.read().decode("utf-8", errors="replace").strip() or "remote metadata collection failed")
    payload = json.loads(stdout.read().decode("utf-8", errors="replace"))
    return payload if isinstance(payload, dict) else {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("MIM_ARM_SSH_HOST", "192.168.1.90"))
    parser.add_argument(
        "--ssh-user",
        default=os.getenv("MIM_ARM_SSH_HOST_USER", os.getenv("MIM_ARM_SSH_USER", "testpilot")),
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=int(os.getenv("MIM_ARM_SSH_HOST_PORT", "22") or "22"),
    )
    parser.add_argument("--password-env", default="MIM_ARM_SSH_HOST_PASS")
    parser.add_argument(
        "--remote-root",
        default="/home/testpilot/mim/runtime/shared",
        help="Remote shared root that TOD polls.",
    )
    parser.add_argument(
        "--request-file",
        default=str(DEFAULT_SHARED_DIR / "MIM_TOD_TASK_REQUEST.latest.json"),
    )
    parser.add_argument(
        "--trigger-file",
        default=str(DEFAULT_SHARED_DIR / "MIM_TO_TOD_TRIGGER.latest.json"),
    )
    parser.add_argument(
        "--verify-task-id",
        default="",
        help="Optional task id that must appear in both remote files after publish.",
    )
    parser.add_argument(
        "--caller",
        default="",
        help="Optional caller identifier for audit output.",
    )
    return parser.parse_args()


def _resolve_password(password_env: str) -> str:
    return (
        os.getenv(password_env, "")
        or os.getenv("MIM_ARM_SSH_HOST_PASS", "")
        or os.getenv("MIM_ARM_SSH_PASSWORD", "")
    )


def _ssh_base_command(host: str, ssh_user: str, ssh_port: int, password_env: str) -> list[str]:
    password = _resolve_password(password_env)
    base: list[str] = []
    if password:
        sshpass = shutil.which("sshpass")
        if sshpass:
            base.extend([sshpass, "-p", password])
    base.extend([
        "ssh",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=5",
        "-p",
        str(ssh_port),
    ])
    if not password:
        base.extend(["-o", "BatchMode=yes"])
    base.append(f"{ssh_user}@{host}")
    return base


def _scp_base_command(host: str, ssh_user: str, ssh_port: int, password_env: str) -> list[str]:
    password = _resolve_password(password_env)
    base: list[str] = []
    if password:
        sshpass = shutil.which("sshpass")
        if sshpass:
            base.extend([sshpass, "-p", password])
    base.extend([
        "scp",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "ConnectTimeout=5",
        "-P",
        str(ssh_port),
    ])
    if not password:
        base.extend(["-o", "BatchMode=yes"])
    return base


def _verify_payload(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return payload


def _publish_with_paramiko(
    *,
    host: str,
    ssh_user: str,
    ssh_port: int,
    password: str,
    remote_root: str,
    request_file: Path,
    trigger_file: Path,
    verify_task_id: str,
    caller: str,
) -> int:
    if paramiko is None:
        raise RuntimeError(
            "Password-based SSH without sshpass requires paramiko. Install with '.venv/bin/pip install paramiko'."
        )

    request_payload = _verify_payload(request_file)
    _verify_payload(trigger_file)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=ssh_user, password=password, port=ssh_port, timeout=8)
    try:
        client.exec_command(f"mkdir -p {remote_root}")[1].channel.recv_exit_status()
        sftp = client.open_sftp()
        try:
            request_remote_path = f"{remote_root}/MIM_TOD_TASK_REQUEST.latest.json"
            trigger_remote_path = f"{remote_root}/MIM_TO_TOD_TRIGGER.latest.json"
            sftp.put(str(request_file), request_remote_path)
            sftp.put(str(trigger_file), trigger_remote_path)
            with sftp.open(request_remote_path, "rb") as handle:
                remote_request_bytes = handle.read()
            with sftp.open(trigger_remote_path, "rb") as handle:
                remote_trigger_bytes = handle.read()
            remote_request = json.loads(remote_request_bytes.decode("utf-8-sig"))
            remote_trigger = json.loads(remote_trigger_bytes.decode("utf-8-sig"))
            remote_meta = _collect_remote_metadata_via_paramiko(
                client=client,
                remote_paths={
                    "request": request_remote_path,
                    "trigger": trigger_remote_path,
                },
            )
            remote_request_meta = (remote_meta.get("request") if isinstance(remote_meta, dict) else {}) or _extract_remote_artifact_metadata(
                remote_path=request_remote_path,
                raw_bytes=remote_request_bytes,
                payload=remote_request,
                realpath=sftp.normalize(request_remote_path),
                inode=None,
                size=len(remote_request_bytes),
            )
            remote_trigger_meta = (remote_meta.get("trigger") if isinstance(remote_meta, dict) else {}) or _extract_remote_artifact_metadata(
                remote_path=trigger_remote_path,
                raw_bytes=remote_trigger_bytes,
                payload=remote_trigger,
                realpath=sftp.normalize(trigger_remote_path),
                inode=None,
                size=len(remote_trigger_bytes),
            )
        finally:
            sftp.close()
    finally:
        client.close()

    request_task_id = str(remote_request.get("task_id") or "")
    request_request_id = str(remote_request.get("request_id") or request_task_id)
    trigger_task_id = str(remote_trigger.get("task_id") or "")
    trigger_request_id = str(remote_trigger.get("request_id") or trigger_task_id)
    if verify_task_id and (request_request_id != verify_task_id or trigger_request_id != verify_task_id):
        raise RuntimeError(
            f"Remote verification failed: request request_id={request_request_id!r}, trigger request_id={trigger_request_id!r}, expected {verify_task_id!r}"
        )

    payload = {
        "remote_root": remote_root,
        "request_task_id": request_task_id,
        "request_request_id": request_request_id,
        "trigger_task_id": trigger_task_id,
        "trigger_request_id": trigger_request_id,
        "request_generated_at": remote_request.get("generated_at"),
        "trigger_generated_at": remote_trigger.get("generated_at"),
        "request_sha256": str(remote_request_meta.get("sha256") or ""),
        "trigger_sha256": str(remote_trigger_meta.get("sha256") or ""),
    }
    _append_publish_audit(
        caller=caller,
        host=host,
        remote_root=remote_root,
        request_file=request_file,
        trigger_file=trigger_file,
        objective_id=str(remote_request.get("objective_id") or request_payload.get("objective_id") or ""),
        request_task_id=request_task_id,
        trigger_task_id=trigger_task_id,
        request_generated_at=remote_request.get("generated_at"),
        trigger_generated_at=remote_trigger.get("generated_at"),
        success=True,
        returncode=0,
        stdout=json.dumps(payload, indent=2),
        stderr="",
        remote_request_meta=remote_request_meta,
        remote_trigger_meta=remote_trigger_meta,
    )
    _write_boundary_status(
        host=host,
        remote_root=remote_root,
        request_file=request_file,
        trigger_file=trigger_file,
        remote_request_meta=remote_request_meta,
        remote_trigger_meta=remote_trigger_meta,
    )
    print(json.dumps(payload, indent=2))
    return 0


def main() -> int:
    args = parse_args()
    request_file = Path(args.request_file).expanduser().resolve()
    trigger_file = Path(args.trigger_file).expanduser().resolve()
    if not request_file.exists():
        raise SystemExit(f"Request file missing: {request_file}")
    if not trigger_file.exists():
        raise SystemExit(f"Trigger file missing: {trigger_file}")

    _, request_errors = normalize_and_validate_file(
        request_file,
        message_kind="request",
        service_name="publish_tod_bridge_artifacts_remote.py",
        transport_surface=f"{args.host}:{args.remote_root}",
    )
    if request_errors:
        raise SystemExit(f"Contract validation failed for request artifact: {request_errors}")
    _, trigger_errors = normalize_and_validate_file(
        trigger_file,
        message_kind="trigger",
        service_name="publish_tod_bridge_artifacts_remote.py",
        transport_surface=f"{args.host}:{args.remote_root}",
    )
    if trigger_errors:
        raise SystemExit(f"Contract validation failed for trigger artifact: {trigger_errors}")

    password = _resolve_password(args.password_env)
    if password and shutil.which("sshpass") is None:
        return _publish_with_paramiko(
            host=args.host,
            ssh_user=args.ssh_user,
            ssh_port=args.ssh_port,
            password=password,
            remote_root=args.remote_root,
            request_file=request_file,
            trigger_file=trigger_file,
            verify_task_id=args.verify_task_id.strip(),
            caller=args.caller.strip(),
        )

    request_payload = _verify_payload(request_file)
    trigger_payload = _verify_payload(trigger_file)

    subprocess.run([
        *(_ssh_base_command(args.host, args.ssh_user, args.ssh_port, args.password_env)),
        f"mkdir -p {args.remote_root}",
    ], check=True)

    subprocess.run([
        *(_scp_base_command(args.host, args.ssh_user, args.ssh_port, args.password_env)),
        str(request_file),
        f"{args.ssh_user}@{args.host}:{args.remote_root}/MIM_TOD_TASK_REQUEST.latest.json",
    ], check=True)
    subprocess.run([
        *(_scp_base_command(args.host, args.ssh_user, args.ssh_port, args.password_env)),
        str(trigger_file),
        f"{args.ssh_user}@{args.host}:{args.remote_root}/MIM_TO_TOD_TRIGGER.latest.json",
    ], check=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_request = Path(tmpdir) / "MIM_TOD_TASK_REQUEST.latest.json"
        temp_trigger = Path(tmpdir) / "MIM_TO_TOD_TRIGGER.latest.json"
        request_remote_path = f"{args.remote_root}/MIM_TOD_TASK_REQUEST.latest.json"
        trigger_remote_path = f"{args.remote_root}/MIM_TO_TOD_TRIGGER.latest.json"
        subprocess.run([
            *(_scp_base_command(args.host, args.ssh_user, args.ssh_port, args.password_env)),
            f"{args.ssh_user}@{args.host}:{request_remote_path}",
            str(temp_request),
        ], check=True)
        subprocess.run([
            *(_scp_base_command(args.host, args.ssh_user, args.ssh_port, args.password_env)),
            f"{args.ssh_user}@{args.host}:{trigger_remote_path}",
            str(temp_trigger),
        ], check=True)
        remote_request = json.loads(temp_request.read_text(encoding="utf-8-sig"))
        remote_trigger = json.loads(temp_trigger.read_text(encoding="utf-8-sig"))
        remote_meta = _collect_remote_metadata_via_ssh(
            host=args.host,
            ssh_user=args.ssh_user,
            ssh_port=args.ssh_port,
            password_env=args.password_env,
            remote_paths={
                "request": request_remote_path,
                "trigger": trigger_remote_path,
            },
        )

    request_task_id = str(remote_request.get("task_id") or "")
    request_request_id = str(remote_request.get("request_id") or request_task_id)
    trigger_task_id = str(remote_trigger.get("task_id") or "")
    trigger_request_id = str(remote_trigger.get("request_id") or trigger_task_id)
    verify_task_id = args.verify_task_id.strip()
    if verify_task_id and (request_request_id != verify_task_id or trigger_request_id != verify_task_id):
        raise RuntimeError(
            f"Remote verification failed: request request_id={request_request_id!r}, trigger request_id={trigger_request_id!r}, expected {verify_task_id!r}"
        )

    payload = {
        "remote_root": args.remote_root,
        "request_task_id": request_task_id,
        "request_request_id": request_request_id,
        "trigger_task_id": trigger_task_id,
        "trigger_request_id": trigger_request_id,
        "request_generated_at": remote_request.get("generated_at"),
        "trigger_generated_at": remote_trigger.get("generated_at"),
        "local_request_generated_at": request_payload.get("generated_at"),
        "local_trigger_generated_at": trigger_payload.get("generated_at"),
        "request_sha256": str((remote_meta.get("request") or {}).get("sha256") or hashlib.sha256(temp_request.read_bytes()).hexdigest()),
        "trigger_sha256": str((remote_meta.get("trigger") or {}).get("sha256") or hashlib.sha256(temp_trigger.read_bytes()).hexdigest()),
    }
    _append_publish_audit(
        caller=args.caller.strip(),
        host=args.host,
        remote_root=args.remote_root,
        request_file=request_file,
        trigger_file=trigger_file,
        objective_id=str(remote_request.get("objective_id") or request_payload.get("objective_id") or ""),
        request_task_id=request_task_id,
        trigger_task_id=trigger_task_id,
        request_generated_at=remote_request.get("generated_at"),
        trigger_generated_at=remote_trigger.get("generated_at"),
        success=True,
        returncode=0,
        stdout=json.dumps(payload, indent=2),
        stderr="",
        remote_request_meta=(remote_meta.get("request") if isinstance(remote_meta, dict) else {}) or {},
        remote_trigger_meta=(remote_meta.get("trigger") if isinstance(remote_meta, dict) else {}) or {},
    )
    _write_boundary_status(
        host=args.host,
        remote_root=args.remote_root,
        request_file=request_file,
        trigger_file=trigger_file,
        remote_request_meta=(remote_meta.get("request") if isinstance(remote_meta, dict) else {}) or {},
        remote_trigger_meta=(remote_meta.get("trigger") if isinstance(remote_meta, dict) else {}) or {},
    )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
