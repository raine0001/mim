#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.handoff_intake_service import DEFAULT_HANDOFF_ROOT, ensure_handoff_directories  # noqa: E402
from scripts.watch_handoff_inbox import WATCHER_STATUS_ARTIFACT  # noqa: E402


STALE_WATCHER_RECOVERY_HINT = "restart_local_handoff_watcher"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _load_watcher_status(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_watcher_status(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def watcher_status_path(*, handoff_root: Path) -> Path:
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    return status_dir / WATCHER_STATUS_ARTIFACT


def evaluate_watcher_status(*, handoff_root: Path, now: datetime | None = None) -> dict[str, object]:
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    status_path = status_dir / WATCHER_STATUS_ARTIFACT
    payload = _load_watcher_status(status_path)

    if not payload:
        return {
            "status": "missing",
            "reason": "watcher_status_not_found",
            "recommended_next_action": STALE_WATCHER_RECOVERY_HINT,
            "watcher_status_path": str(status_path),
        }

    lifecycle_state = str(payload.get("lifecycle_state") or "").strip()
    updated_at = _parse_timestamp(str(payload.get("updated_at") or ""))
    stale_after_seconds = int(payload.get("stale_after_seconds") or 0)
    effective_now = now or datetime.now(timezone.utc)

    if lifecycle_state == "completed":
        return {
            "status": "ok",
            "reason": "watcher_completed_cleanly",
            "recommended_next_action": "none",
            "watcher_status_path": str(status_path),
            "lifecycle_state": lifecycle_state,
        }

    if updated_at is None or stale_after_seconds <= 0:
        return {
            "status": "blocked",
            "reason": "watcher_status_missing_freshness_fields",
            "recommended_next_action": STALE_WATCHER_RECOVERY_HINT,
            "watcher_status_path": str(status_path),
            "lifecycle_state": lifecycle_state,
        }

    age_seconds = max(int((effective_now - updated_at).total_seconds()), 0)
    if age_seconds <= stale_after_seconds:
        return {
            "status": "ok",
            "reason": "watcher_status_fresh",
            "recommended_next_action": "none",
            "watcher_status_path": str(status_path),
            "lifecycle_state": lifecycle_state,
            "age_seconds": age_seconds,
        }

    payload["lifecycle_state"] = "stale"
    payload["stale"] = True
    payload["stale_reason"] = "heartbeat_expired"
    payload["recommended_next_action"] = STALE_WATCHER_RECOVERY_HINT
    payload["stale_detected_at"] = _utc_now()
    payload["updated_at"] = _utc_now()
    payload["last_result"] = {
        **(payload.get("last_result") if isinstance(payload.get("last_result"), dict) else {}),
        "reason": "heartbeat_expired",
    }
    _write_watcher_status(status_path, payload)

    return {
        "status": "stale",
        "reason": "heartbeat_expired",
        "recommended_next_action": STALE_WATCHER_RECOVERY_HINT,
        "watcher_status_path": str(status_path),
        "lifecycle_state": "stale",
        "age_seconds": age_seconds,
    }


def main() -> int:
    handoff_root = Path(os.environ.get("MIM_HANDOFF_ROOT", str(DEFAULT_HANDOFF_ROOT))).expanduser().resolve()
    result = evaluate_watcher_status(handoff_root=handoff_root)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())