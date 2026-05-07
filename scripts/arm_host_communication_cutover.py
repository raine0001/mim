#!/usr/bin/env python3
"""Safely disable the legacy ARM-host communication surface and install a trap directory."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_ARM_HOST = os.getenv("MIM_ARM_SSH_HOST", "192.168.1.90")
DEFAULT_ARM_SSH_USER = os.getenv("MIM_ARM_SSH_HOST_USER", os.getenv("MIM_ARM_SSH_USER", "testpilot"))
DEFAULT_ARM_SSH_PORT = int(os.getenv("MIM_ARM_SSH_HOST_PORT", "22") or "22")
DEFAULT_ARM_PASSWORD_ENV = "MIM_ARM_SSH_HOST_PASS"
DEFAULT_SURFACE_DIR = "/home/testpilot/mim/runtime/shared"
DEFAULT_DISABLED_SUFFIX = "_DISABLED"
DEFAULT_TRAP_FILE = "ERROR_COMMUNICATION_SURFACE_BLOCKED.txt"
DEFAULT_TRAP_MESSAGE = "ERROR: Communication surface not allowed on ARM host (.90). Canonical TOD/MIM communication authority is 192.168.1.120:/home/testpilot/mim/runtime/shared."


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("check", "disable", "purge"), default="check")
    parser.add_argument("--host", default=DEFAULT_ARM_HOST, help="ARM host to modify over SSH.")
    parser.add_argument("--ssh-user", default=DEFAULT_ARM_SSH_USER)
    parser.add_argument("--ssh-port", type=int, default=DEFAULT_ARM_SSH_PORT)
    parser.add_argument("--password-env", default=DEFAULT_ARM_PASSWORD_ENV)
    parser.add_argument(
        "--surface-dir",
        default=DEFAULT_SURFACE_DIR,
        help="Legacy communication surface on the ARM host.",
    )
    parser.add_argument(
        "--disabled-dir",
        default="",
        help="Optional explicit disabled directory path. Defaults to <surface-dir>_DISABLED.",
    )
    parser.add_argument(
        "--trap-file",
        default=DEFAULT_TRAP_FILE,
        help="Trap file written into the recreated surface directory after disable.",
    )
    parser.add_argument(
        "--trap-message",
        default=DEFAULT_TRAP_MESSAGE,
        help="Trap message written into the trap file.",
    )
    parser.add_argument(
        "--no-trap",
        action="store_true",
        help="Disable trap directory recreation after renaming the legacy surface.",
    )
    parser.add_argument(
        "--writable-trap",
        action="store_true",
        help="Leave the trap directory writable instead of setting read-only permissions.",
    )
    parser.add_argument(
        "--local-surface-root",
        default="",
        help="Operate on a local path instead of a remote host. Intended for dry-runs and tests.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow purge even when the trap directory is missing.",
    )
    return parser.parse_args()


def _resolve_password(password_env: str) -> str:
    return str(
        os.getenv(password_env, "")
        or os.getenv("MIM_ARM_SSH_PASSWORD", "")
        or os.getenv("MIM_ARM_SSH_HOST_PASS", "")
    ).strip()


def _ssh_base_command(*, host: str, ssh_user: str, ssh_port: int, password_env: str) -> list[str]:
    password = _resolve_password(password_env)
    base: list[str] = []
    if password:
        sshpass = shutil.which("sshpass")
        if sshpass:
            base.extend([sshpass, "-p", password])
    base.extend(
        [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=5",
            "-p",
            str(ssh_port),
        ]
    )
    if not password:
        base.extend(["-o", "BatchMode=yes"])
    base.append(f"{ssh_user}@{host}")
    return base


def _disabled_dir(surface_dir: Path, override: str) -> Path:
    if override:
        return Path(override).expanduser()
    return surface_dir.parent / f"{surface_dir.name}{DEFAULT_DISABLED_SUFFIX}"


def _set_read_only(path: Path) -> None:
    path.chmod(0o555)
    for child in path.iterdir():
        if child.is_file():
            child.chmod(0o444)


def _check_local(surface_dir: Path, disabled_dir: Path, trap_file: str) -> dict[str, object]:
    trap_path = surface_dir / trap_file
    return {
        "surface_dir": str(surface_dir),
        "disabled_dir": str(disabled_dir),
        "surface_exists": surface_dir.exists(),
        "surface_is_dir": surface_dir.is_dir(),
        "disabled_exists": disabled_dir.exists(),
        "trap_exists": trap_path.exists(),
        "trap_path": str(trap_path),
        "trap_read_only": trap_path.exists() and not os.access(trap_path, os.W_OK),
    }


def _disable_local(surface_dir: Path, disabled_dir: Path, trap_file: str, trap_message: str, create_trap: bool, writable_trap: bool) -> dict[str, object]:
    renamed = False
    if surface_dir.exists() and not disabled_dir.exists():
        surface_dir.rename(disabled_dir)
        renamed = True
    if create_trap:
        surface_dir.mkdir(parents=True, exist_ok=True)
        trap_path = surface_dir / trap_file
        trap_path.write_text(trap_message + "\n", encoding="utf-8")
        if not writable_trap:
            _set_read_only(surface_dir)
    result = _check_local(surface_dir, disabled_dir, trap_file)
    result["renamed"] = renamed
    result["mode"] = "disable"
    return result


def _purge_local(surface_dir: Path, disabled_dir: Path, trap_file: str, force: bool) -> dict[str, object]:
    trap_path = surface_dir / trap_file
    if disabled_dir.exists() and (force or trap_path.exists()):
        shutil.rmtree(disabled_dir)
    result = _check_local(surface_dir, disabled_dir, trap_file)
    result["mode"] = "purge"
    return result


def _remote_inline_script(mode: str, surface_dir: str, disabled_dir: str, trap_file: str, trap_message: str, create_trap: bool, writable_trap: bool, force: bool) -> str:
    return "\n".join(
        [
            "python3 - <<'PY'",
            "import json",
            "import os",
            "import shutil",
            "from pathlib import Path",
            f"mode = {mode!r}",
            f"surface_dir = Path({surface_dir!r}).expanduser()",
            f"disabled_dir = Path({disabled_dir!r}).expanduser()",
            f"trap_file = {trap_file!r}",
            f"trap_message = {trap_message!r}",
            f"create_trap = {create_trap!r}",
            f"writable_trap = {writable_trap!r}",
            f"force = {force!r}",
            "trap_path = surface_dir / trap_file",
            "def set_read_only(path):",
            "    path.chmod(0o555)",
            "    for child in path.iterdir():",
            "        if child.is_file():",
            "            child.chmod(0o444)",
            "def check():",
            "    return {",
            "        'surface_dir': str(surface_dir),",
            "        'disabled_dir': str(disabled_dir),",
            "        'surface_exists': surface_dir.exists(),",
            "        'surface_is_dir': surface_dir.is_dir(),",
            "        'disabled_exists': disabled_dir.exists(),",
            "        'trap_exists': trap_path.exists(),",
            "        'trap_path': str(trap_path),",
            "        'trap_read_only': trap_path.exists() and not os.access(trap_path, os.W_OK),",
            "    }",
            "result = {}",
            "if mode == 'disable':",
            "    renamed = False",
            "    if surface_dir.exists() and not disabled_dir.exists():",
            "        surface_dir.rename(disabled_dir)",
            "        renamed = True",
            "    if create_trap:",
            "        surface_dir.mkdir(parents=True, exist_ok=True)",
            "        trap_path.write_text(trap_message + '\\n', encoding='utf-8')",
            "        if not writable_trap:",
            "            set_read_only(surface_dir)",
            "    result = check()",
            "    result['renamed'] = renamed",
            "    result['mode'] = 'disable'",
            "elif mode == 'purge':",
            "    if disabled_dir.exists() and (force or trap_path.exists()):",
            "        shutil.rmtree(disabled_dir)",
            "    result = check()",
            "    result['mode'] = 'purge'",
            "else:",
            "    result = check()",
            "    result['mode'] = 'check'",
            "print(json.dumps(result))",
            "PY",
        ]
    )


def _run_remote(args: argparse.Namespace) -> dict[str, object]:
    surface_dir = Path(args.surface_dir).expanduser()
    disabled_dir = _disabled_dir(surface_dir, args.disabled_dir)
    command = _remote_inline_script(
        mode=args.mode,
        surface_dir=str(surface_dir),
        disabled_dir=str(disabled_dir),
        trap_file=args.trap_file,
        trap_message=args.trap_message,
        create_trap=not args.no_trap,
        writable_trap=bool(args.writable_trap),
        force=bool(args.force),
    )
    completed = subprocess.run(
        [*_ssh_base_command(host=args.host, ssh_user=args.ssh_user, ssh_port=args.ssh_port, password_env=args.password_env), command],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"remote cutover failed with exit {completed.returncode}")
    payload = json.loads(completed.stdout or "{}")
    if not isinstance(payload, dict):
        raise RuntimeError("remote cutover returned a non-object payload")
    payload["transport"] = "ssh"
    payload["host"] = args.host
    return payload


def _run_local(args: argparse.Namespace) -> dict[str, object]:
    surface_dir = Path(args.local_surface_root).expanduser()
    disabled_dir = _disabled_dir(surface_dir, args.disabled_dir)
    if args.mode == "disable":
        payload = _disable_local(
            surface_dir=surface_dir,
            disabled_dir=disabled_dir,
            trap_file=args.trap_file,
            trap_message=args.trap_message,
            create_trap=not args.no_trap,
            writable_trap=bool(args.writable_trap),
        )
    elif args.mode == "purge":
        payload = _purge_local(
            surface_dir=surface_dir,
            disabled_dir=disabled_dir,
            trap_file=args.trap_file,
            force=bool(args.force),
        )
    else:
        payload = _check_local(surface_dir, disabled_dir, args.trap_file)
        payload["mode"] = "check"
    payload["transport"] = "local"
    return payload


def main() -> int:
    args = parse_args()
    if args.local_surface_root:
        result = _run_local(args)
    else:
        result = _run_remote(args)
    output = {
        "generated_at": _utcnow(),
        "type": "arm_host_communication_cutover_v1",
        "result": result,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())