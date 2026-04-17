#!/usr/bin/env python3
"""Pull TOD-owned response artifacts from the canonical communication shared root into local runtime/shared."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import paramiko
except Exception:  # pragma: no cover - optional dependency fallback
    paramiko = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.tod_mim_contract import (
    CONTRACT_RECEIPT_ARTIFACT,
    PRIMARY_TRANSPORT_ID,
    PRIMARY_TRANSPORT_SURFACE,
    build_activation_report,
    emit_validation_failure,
    normalize_message,
    validate_message,
)

DEFAULT_SHARED_DIR = PROJECT_ROOT / "runtime" / "shared"
DEFAULT_ARTIFACTS = [
    "TOD_TO_MIM_TRIGGER_ACK.latest.json",
    "TOD_MIM_TASK_ACK.latest.json",
    "TOD_MIM_TASK_RESULT.latest.json",
    CONTRACT_RECEIPT_ARTIFACT,
]
OPTIONAL_ARTIFACTS = {CONTRACT_RECEIPT_ARTIFACT}
DEFAULT_CANONICAL_SSH_USER = os.getenv("MIM_TOD_SSH_USER", os.getenv("MIM_TOD_SSH_HOST_USER", "testpilot"))
DEFAULT_CANONICAL_SSH_PORT = int(os.getenv("MIM_TOD_SSH_PORT", "22") or "22")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("MIM_TOD_SSH_HOST", "192.168.1.120"))
    parser.add_argument(
        "--ssh-user",
        default=DEFAULT_CANONICAL_SSH_USER,
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=DEFAULT_CANONICAL_SSH_PORT,
    )
    parser.add_argument("--password-env", default="MIM_TOD_SSH_PASS")
    parser.add_argument(
        "--remote-root",
        default="/home/testpilot/mim/runtime/shared",
        help="Remote shared root that TOD publishes back into.",
    )
    parser.add_argument(
        "--local-shared-dir",
        default=str(DEFAULT_SHARED_DIR),
    )
    parser.add_argument(
        "--artifact",
        action="append",
        dest="artifacts",
        help="Artifact filename to pull. Defaults to the three TOD response artifacts.",
    )
    parser.add_argument(
        "--backup-dir",
        default="",
        help="Optional directory to store pre-sync local copies for overwritten files.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite local files even when the remote payload is not newer.",
    )
    return parser.parse_args()


def _resolve_password(password_env: str) -> str:
    return (
        os.getenv(password_env, "")
        or os.getenv("MIM_TOD_SSH_PASSWORD", "")
        or os.getenv("MIM_TOD_SSH_PASS", "")
    )


def _validate_contract_receipt(payload: dict, remote_root: str) -> tuple[dict, list[str]]:
    normalized = normalize_message(
        payload,
        message_kind="contract_receipt",
        service_name="pull_tod_bridge_artifacts_remote",
        actor="TOD",
        transport_id=PRIMARY_TRANSPORT_ID,
        transport_surface=PRIMARY_TRANSPORT_SURFACE,
    )
    errors = validate_message(normalized, "contract_receipt")
    if errors:
        emit_validation_failure(
            artifact_path=f"{remote_root.rstrip('/')}/{CONTRACT_RECEIPT_ARTIFACT}",
            message_kind="contract_receipt",
            errors=errors,
            payload=normalized,
        )
    return normalized, errors


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


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return payload


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _best_timestamp(payload: dict) -> datetime | None:
    for key in ("generated_at", "observed_at", "emitted_at", "updated_at", "created_at"):
        parsed = _parse_timestamp(payload.get(key))
        if parsed is not None:
            return parsed
    return None


def _summarize_payload(payload: dict) -> dict:
    return {
        "task_id": str(payload.get("task_id") or payload.get("request_id") or ""),
        "generated_at": str(payload.get("generated_at") or ""),
        "status": str(payload.get("status") or ""),
    }


def _should_copy(*, remote_payload: dict, local_payload: dict | None, force: bool) -> tuple[bool, str]:
    if force:
        return True, "force"
    if local_payload is None:
        return True, "local_missing"

    remote_ts = _best_timestamp(remote_payload)
    local_ts = _best_timestamp(local_payload)
    if remote_ts and local_ts:
        if remote_ts > local_ts:
            return True, "remote_newer"
        if remote_ts < local_ts:
            return False, "local_newer"

    if remote_payload != local_payload:
        remote_summary = _summarize_payload(remote_payload)
        local_summary = _summarize_payload(local_payload)
        if remote_summary != local_summary:
            return False, "payload_diff_without_newer_timestamp"
        return False, "same_summary"

    return False, "identical"


def _pull_with_paramiko(
    *,
    host: str,
    ssh_user: str,
    ssh_port: int,
    password: str,
    remote_root: str,
    artifacts: list[str],
) -> dict[str, dict | None]:
    if paramiko is None:
        raise RuntimeError(
            "Password-based SSH without sshpass requires paramiko. Install with '.venv/bin/pip install paramiko'."
        )

    pulled: dict[str, dict | None] = {}
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=ssh_user, password=password, port=ssh_port, timeout=8)
    try:
        sftp = client.open_sftp()
        try:
            for name in artifacts:
                remote_path = f"{remote_root}/{name}"
                try:
                    with sftp.open(remote_path, "r") as handle:
                        payload = json.loads(handle.read().decode("utf-8"))
                except FileNotFoundError:
                    if name in OPTIONAL_ARTIFACTS:
                        pulled[name] = None
                        continue
                    raise
                if not isinstance(payload, dict):
                    raise RuntimeError(f"Expected JSON object in remote artifact {remote_path}")
                pulled[name] = payload
        finally:
            sftp.close()
    finally:
        client.close()
    return pulled


def _pull_with_scp(
    *,
    host: str,
    ssh_user: str,
    ssh_port: int,
    password_env: str,
    remote_root: str,
    artifacts: list[str],
) -> dict[str, dict | None]:
    pulled: dict[str, dict | None] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        temp_root = Path(tmpdir)
        for name in artifacts:
            temp_path = temp_root / name
            completed = subprocess.run(
                [
                    *(_scp_base_command(host, ssh_user, ssh_port, password_env)),
                    f"{ssh_user}@{host}:{remote_root}/{name}",
                    str(temp_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                if name in OPTIONAL_ARTIFACTS:
                    pulled[name] = None
                    continue
                raise subprocess.CalledProcessError(
                    completed.returncode,
                    completed.args,
                    output=completed.stdout,
                    stderr=completed.stderr,
                )
            pulled[name] = _read_json(temp_path)
    return pulled


def main() -> int:
    args = parse_args()
    local_shared_dir = Path(args.local_shared_dir).expanduser().resolve()
    local_shared_dir.mkdir(parents=True, exist_ok=True)
    artifacts = args.artifacts or list(DEFAULT_ARTIFACTS)
    backup_dir = Path(args.backup_dir).expanduser().resolve() if args.backup_dir else None
    if backup_dir is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)

    password = _resolve_password(args.password_env)
    if password and shutil.which("sshpass") is None:
        remote_payloads = _pull_with_paramiko(
            host=args.host,
            ssh_user=args.ssh_user,
            ssh_port=args.ssh_port,
            password=password,
            remote_root=args.remote_root,
            artifacts=artifacts,
        )
    else:
        remote_payloads = _pull_with_scp(
            host=args.host,
            ssh_user=args.ssh_user,
            ssh_port=args.ssh_port,
            password_env=args.password_env,
            remote_root=args.remote_root,
            artifacts=artifacts,
        )

    results: list[dict] = []
    copied_any = False
    validation_failed = False
    for name in artifacts:
        remote_payload = remote_payloads[name]
        local_path = local_shared_dir / name
        local_payload = _read_json(local_path) if local_path.exists() else None
        if remote_payload is None:
            results.append(
                {
                    "artifact": name,
                    "action": "skipped",
                    "reason": "remote_missing_optional",
                    "remote": {},
                    "local_before": _summarize_payload(local_payload or {}),
                }
            )
            continue
        if name == CONTRACT_RECEIPT_ARTIFACT:
            remote_payload, errors = _validate_contract_receipt(remote_payload, args.remote_root)
            if errors:
                validation_failed = True
                results.append(
                    {
                        "artifact": name,
                        "action": "validation_failed",
                        "reason": ",".join(errors),
                        "remote": _summarize_payload(remote_payload),
                        "local_before": _summarize_payload(local_payload or {}),
                    }
                )
                continue
        should_copy, reason = _should_copy(
            remote_payload=remote_payload,
            local_payload=local_payload,
            force=bool(args.force),
        )
        if should_copy:
            if backup_dir is not None and local_path.exists():
                shutil.copy2(local_path, backup_dir / name)
            local_path.write_text(json.dumps(remote_payload, indent=2) + "\n", encoding="utf-8")
            copied_any = True
        results.append(
            {
                "artifact": name,
                "action": "copied" if should_copy else "skipped",
                "reason": reason,
                "remote": _summarize_payload(remote_payload),
                "local_before": _summarize_payload(local_payload or {}),
            }
        )

    print(
        json.dumps(
            {
                "remote_root": args.remote_root,
                "local_shared_dir": str(local_shared_dir),
                "copied": copied_any,
                "results": results,
                "validation_failed": validation_failed,
            },
            indent=2,
        )
    )
    build_activation_report()
    return 1 if validation_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())