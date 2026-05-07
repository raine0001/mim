#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.handoff_intake_service import DEFAULT_HANDOFF_ROOT, ensure_handoff_directories  # noqa: E402
from scripts.watch_handoff_inbox import WATCHER_STATUS_ARTIFACT  # noqa: E402
from scripts.watch_handoff_watcher_supervisor import WATCHER_RECOVERY_ARTIFACT  # noqa: E402


def _load_json(path: Path) -> tuple[dict[str, object], str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return {}, "missing"
    except json.JSONDecodeError:
        return {}, "malformed"
    if not isinstance(payload, dict):
        return {}, "malformed"
    return payload, "ok"


def _watcher_state_text(payload: dict[str, object], load_status: str) -> str:
    if load_status != "ok":
        return load_status
    lifecycle_state = str(payload.get("lifecycle_state") or "").strip() or "unknown"
    if bool(payload.get("stale")):
        return f"{lifecycle_state} (stale)"
    return lifecycle_state


def _recovery_state_text(payload: dict[str, object], load_status: str) -> str:
    if load_status != "ok":
        return load_status
    return str(payload.get("status") or "unknown").strip() or "unknown"


def _manual_action_needed(*, watcher_load_status: str, recovery_load_status: str, recovery_payload: dict[str, object]) -> str:
    if watcher_load_status != "ok" or recovery_load_status != "ok":
        return "yes"
    recovery_status = str(recovery_payload.get("status") or "").strip()
    if recovery_status == "recovery_failed":
        return "yes"
    return "no"


def main() -> int:
    handoff_root = Path(os.environ.get("MIM_HANDOFF_ROOT", str(DEFAULT_HANDOFF_ROOT))).expanduser().resolve()
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    watcher_path = status_dir / WATCHER_STATUS_ARTIFACT
    recovery_path = status_dir / WATCHER_RECOVERY_ARTIFACT

    watcher_payload, watcher_load_status = _load_json(watcher_path)
    recovery_payload, recovery_load_status = _load_json(recovery_path)

    print(f"Watcher state: {_watcher_state_text(watcher_payload, watcher_load_status)}")
    print(f"Recovery state: {_recovery_state_text(recovery_payload, recovery_load_status)}")
    print(
        "Manual action needed: "
        f"{_manual_action_needed(watcher_load_status=watcher_load_status, recovery_load_status=recovery_load_status, recovery_payload=recovery_payload)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())