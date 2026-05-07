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
from scripts.check_handoff_watcher_status import STALE_WATCHER_RECOVERY_HINT  # noqa: E402
from scripts.watch_handoff_inbox import WATCHER_STATUS_ARTIFACT  # noqa: E402


def _load_status(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def main() -> int:
    handoff_root = Path(os.environ.get("MIM_HANDOFF_ROOT", str(DEFAULT_HANDOFF_ROOT))).expanduser().resolve()
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    status_path = status_dir / WATCHER_STATUS_ARTIFACT
    payload = _load_status(status_path)

    lifecycle_state = str(payload.get("lifecycle_state") or "").strip()
    recommended_next_action = str(payload.get("recommended_next_action") or "").strip()

    if lifecycle_state == "stale" and recommended_next_action:
        print(f"Watcher recovery instruction: {recommended_next_action}")
        return 0

    if not payload:
        print(f"Watcher recovery instruction: {STALE_WATCHER_RECOVERY_HINT}")
        return 0

    print("Watcher recovery instruction: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())