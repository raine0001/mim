from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from core.config import PROJECT_ROOT, settings


_DEFAULT_COMMAND = f"{sys.executable} {PROJECT_ROOT / 'scripts' / 'mim_privileged_action.py'}"
_ALLOWED_ACTIONS = {
    "disable-system-tod-liveness-watcher",
    "enable-system-tod-liveness-watcher",
    "status-system-tod-liveness-watcher",
}


def privileged_actions_enabled() -> bool:
    return bool(getattr(settings, "mim_privileged_actions_enabled", False))


def privileged_action_command() -> str:
    configured = str(getattr(settings, "mim_privileged_action_command", "") or "").strip()
    return configured or _DEFAULT_COMMAND


def run_privileged_action(action: str, *, timeout_seconds: int = 20) -> dict[str, Any]:
    if action not in _ALLOWED_ACTIONS:
        raise ValueError(f"Unsupported privileged action: {action}")
    if not privileged_actions_enabled():
        return {
            "status": "disabled",
            "action": action,
        }

    command_text = privileged_action_command()
    command = shlex.split(command_text)
    if not command:
        raise ValueError("Privileged action command is not configured")

    executable = Path(command[0])
    if executable.is_absolute() and not executable.exists():
        raise ValueError(f"Privileged action runner not found: {command[0]}")

    completed = subprocess.run(
        [*command, action],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
        cwd=str(PROJECT_ROOT),
    )
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    payload: dict[str, Any] = {}
    if stdout.startswith("{"):
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}
    payload.setdefault("action", action)
    payload.setdefault("status", "completed" if completed.returncode == 0 else "failed")
    payload.setdefault("returncode", completed.returncode)
    if stdout:
        payload.setdefault("stdout", stdout)
    if stderr:
        payload.setdefault("stderr", stderr)
    if completed.returncode != 0:
        raise ValueError(stderr or stdout or f"Privileged action failed: {action}")
    return payload