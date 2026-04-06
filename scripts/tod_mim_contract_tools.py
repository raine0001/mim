#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.tod_mim_contract import (
    CONTRACT_RECEIPT_PATH,
    CONTRACT_TRANSMISSION_ARTIFACT,
    CONTRACT_TRANSMISSION_PATH,
    PRIMARY_TRANSPORT_ID,
    PRIMARY_TRANSPORT_SURFACE,
    build_activation_report,
    ensure_contract_signature,
    ensure_runtime_contract_lock,
    normalize_and_validate_file,
    receipt_status,
    write_contract_transmission_artifact,
)


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def command_sign(_args: argparse.Namespace) -> int:
    _print_json(ensure_contract_signature())
    return 0


def command_lock(_args: argparse.Namespace) -> int:
    _print_json(ensure_runtime_contract_lock())
    return 0


def command_normalize(args: argparse.Namespace) -> int:
    payload, errors = normalize_and_validate_file(
        Path(args.file),
        message_kind=args.kind,
        service_name=args.source_service,
        instance_id=args.source_instance or "",
        actor=args.actor,
        transport_id=args.transport_id,
        transport_surface=args.transport_surface,
    )
    if errors:
        _print_json({"ok": False, "errors": errors, "payload": payload})
        return 1
    _print_json({"ok": True, "payload": payload})
    return 0


def _transmit_with_scp(local_path: Path, remote_target: str) -> None:
    raise RuntimeError("use_parameterized_transmit")


def _resolve_password(password_env: str) -> str:
    return str(
        os.getenv(password_env, "")
        or os.getenv("MIM_TOD_SSH_PASSWORD", "")
        or os.getenv("MIM_TOD_SSH_PASS", "")
        or ""
    ).strip()


def _scp_base_command(*, ssh_port: int, password_env: str) -> list[str]:
    password = _resolve_password(password_env)
    base: list[str] = []
    if password:
        sshpass = shutil.which("sshpass")
        if not sshpass:
            raise RuntimeError("sshpass_not_available_for_password_auth")
        base.extend([sshpass, "-p", password])
    base.extend(
        [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=5",
            "-P",
            str(ssh_port),
        ]
    )
    if not password:
        base.extend(["-o", "BatchMode=yes"])
    return base


def command_transmit(args: argparse.Namespace) -> int:
    payload = write_contract_transmission_artifact(
        service_name=args.source_service,
        instance_id=args.source_instance or "",
    )
    result = {
        "local_artifact": str(CONTRACT_TRANSMISSION_PATH),
        "contract_id": payload.get("contract_id"),
        "contract_version": payload.get("contract_version"),
        "checksum_sha256": payload.get("checksum_sha256"),
        "remote_publish": "skipped",
    }
    if args.remote_host:
        remote_user = args.remote_user or "testpilot"
        remote_root = args.remote_root or "/home/testpilot/mim/runtime/shared"
        remote_target = f"{remote_user}@{args.remote_host}:{remote_root.rstrip('/')}/{CONTRACT_TRANSMISSION_ARTIFACT}"
        subprocess.run(
            [
                *_scp_base_command(ssh_port=int(args.ssh_port), password_env=args.password_env),
                str(CONTRACT_TRANSMISSION_PATH),
                remote_target,
            ],
            check=True,
        )
        result["remote_publish"] = "completed"
        result["remote_target"] = remote_target
    _print_json(result)
    return 0


def command_receipt(_args: argparse.Namespace) -> int:
    _print_json(receipt_status())
    return 0


def command_report(_args: argparse.Namespace) -> int:
    _print_json(build_activation_report())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TOD↔MIM contract activation utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sign_parser = subparsers.add_parser("sign", help="Generate or refresh the contract signature artifact")
    sign_parser.set_defaults(func=command_sign)

    lock_parser = subparsers.add_parser("lock", help="Assert runtime contract lock and write the lock artifact")
    lock_parser.set_defaults(func=command_lock)

    normalize_parser = subparsers.add_parser(
        "normalize-packet",
        help="Normalize a packet in-place and validate it against the frozen contract",
    )
    normalize_parser.add_argument("--kind", required=True)
    normalize_parser.add_argument("--file", required=True)
    normalize_parser.add_argument("--source-service", required=True)
    normalize_parser.add_argument("--source-instance", default="")
    normalize_parser.add_argument("--actor", default="MIM")
    normalize_parser.add_argument("--transport-id", default=PRIMARY_TRANSPORT_ID)
    normalize_parser.add_argument("--transport-surface", default=PRIMARY_TRANSPORT_SURFACE)
    normalize_parser.set_defaults(func=command_normalize)

    transmit_parser = subparsers.add_parser(
        "transmit-contract",
        help="Write the atomic contract transmission artifact and optionally copy it to the remote shared root",
    )
    transmit_parser.add_argument("--source-service", default="tod_mim_contract_tools")
    transmit_parser.add_argument("--source-instance", default="")
    transmit_parser.add_argument("--remote-host", default="")
    transmit_parser.add_argument("--remote-user", default="testpilot")
    transmit_parser.add_argument("--remote-root", default="/home/testpilot/mim/runtime/shared")
    transmit_parser.add_argument("--ssh-port", type=int, default=22)
    transmit_parser.add_argument("--password-env", default="MIM_TOD_SSH_PASS")
    transmit_parser.set_defaults(func=command_transmit)

    receipt_parser = subparsers.add_parser("receipt-status", help="Report TOD receipt status for the frozen contract")
    receipt_parser.set_defaults(func=command_receipt)

    report_parser = subparsers.add_parser("activation-report", help="Write and print the current activation readiness report")
    report_parser.set_defaults(func=command_report)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())