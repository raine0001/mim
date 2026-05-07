#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import sys


def _systemctl() -> str:
    return shutil.which("systemctl") or "/bin/systemctl"


def _run(args: list[str]) -> dict[str, object]:
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    return {
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "").strip(),
        "stderr": (completed.stderr or "").strip(),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps({"status": "failed", "error": "usage: mim_privileged_action.py <action>"}))
        return 2

    action = argv[1]
    unit = "mim-watch-tod-liveness.service"
    systemctl = _systemctl()
    action_map = {
        "disable-system-tod-liveness-watcher": [systemctl, "disable", "--now", unit],
        "enable-system-tod-liveness-watcher": [systemctl, "enable", "--now", unit],
        "status-system-tod-liveness-watcher": [systemctl, "status", "--no-pager", "--full", unit],
    }
    command = action_map.get(action)
    if command is None:
        print(json.dumps({"status": "failed", "action": action, "error": "unsupported action"}))
        return 2

    result = _run(command)
    payload = {
        "action": action,
        "unit": unit,
        "command": command,
        "status": "completed" if result["returncode"] == 0 else "failed",
        **result,
    }
    print(json.dumps(payload))
    return int(result["returncode"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))