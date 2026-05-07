#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.handoff_intake_service import DEFAULT_HANDOFF_ROOT, ensure_handoff_directories  # noqa: E402
from scripts.run_handoff_intake_once import run_one_handoff_intake  # noqa: E402


WATCHER_STATUS_ARTIFACT = "HANDOFF_WATCHER.latest.json"


def _env_float(name: str, default: float) -> float:
    raw_value = str(os.environ.get(name, str(default))).strip()
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw_value = str(os.environ.get(name, str(default))).strip()
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value >= 0 else default


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_stale_after_seconds(poll_interval_seconds: float) -> int:
    return max(int(poll_interval_seconds * 3), 1)


def _write_watcher_status(
    *,
    handoff_root: Path,
    poll_interval_seconds: float,
    stale_after_seconds: int,
    poll_count: int,
    processed_count: int,
    lifecycle_state: str,
    last_result: dict[str, object],
) -> Path:
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    payload = {
        "artifact_type": "mim-handoff-watcher-status-v1",
        "updated_at": _utc_now(),
        "lifecycle_state": lifecycle_state,
        "poll_interval_seconds": poll_interval_seconds,
        "stale_after_seconds": stale_after_seconds,
        "stale": False,
        "stale_reason": "",
        "poll_count": poll_count,
        "processed_count": processed_count,
        "handoff_root": str(handoff_root),
        "last_result": {
            "status": str(last_result.get("status") or ""),
            "mode": str(last_result.get("mode") or ""),
            "handoff_id": str(last_result.get("handoff_id") or ""),
            "reason": str(last_result.get("reason") or ""),
        },
    }
    artifact_path = status_dir / WATCHER_STATUS_ARTIFACT
    artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return artifact_path


async def _run() -> dict[str, object]:
    handoff_root = Path(os.environ.get("MIM_HANDOFF_ROOT", str(DEFAULT_HANDOFF_ROOT))).expanduser().resolve()
    shared_root = Path(os.environ.get("MIM_SHARED_ROOT", str(PROJECT_ROOT / "runtime" / "shared"))).expanduser().resolve()
    poll_interval_seconds = _env_float("MIM_HANDOFF_POLL_INTERVAL_SECONDS", 2.0)
    max_polls = _env_int("MIM_HANDOFF_MAX_POLLS", 0)
    exit_after_processed = _env_int("MIM_HANDOFF_EXIT_AFTER_PROCESSED", 0)
    stale_after_seconds = _env_int(
        "MIM_HANDOFF_STALE_AFTER_SECONDS",
        _default_stale_after_seconds(poll_interval_seconds),
    )

    ensure_handoff_directories(handoff_root=handoff_root)

    poll_count = 0
    processed_count = 0
    last_result: dict[str, object] = {
        "status": "idle",
        "reason": "watcher_not_started",
        "handoff_root": str(handoff_root),
    }
    heartbeat_path = _write_watcher_status(
        handoff_root=handoff_root,
        poll_interval_seconds=poll_interval_seconds,
        stale_after_seconds=stale_after_seconds,
        poll_count=poll_count,
        processed_count=processed_count,
        lifecycle_state="starting",
        last_result=last_result,
    )

    while True:
        poll_count += 1
        last_result = await run_one_handoff_intake(
            handoff_root=handoff_root,
            shared_root=shared_root,
        )
        heartbeat_path = _write_watcher_status(
            handoff_root=handoff_root,
            poll_interval_seconds=poll_interval_seconds,
            stale_after_seconds=stale_after_seconds,
            poll_count=poll_count,
            processed_count=processed_count,
            lifecycle_state="polling",
            last_result=last_result,
        )
        if str(last_result.get("status") or "") != "idle":
            processed_count += 1
            heartbeat_path = _write_watcher_status(
                handoff_root=handoff_root,
                poll_interval_seconds=poll_interval_seconds,
                stale_after_seconds=stale_after_seconds,
                poll_count=poll_count,
                processed_count=processed_count,
                lifecycle_state="processed",
                last_result=last_result,
            )
            print(json.dumps(last_result, sort_keys=True), flush=True)
            if exit_after_processed and processed_count >= exit_after_processed:
                break

        if max_polls and poll_count >= max_polls:
            break

        await asyncio.sleep(poll_interval_seconds)

    heartbeat_path = _write_watcher_status(
        handoff_root=handoff_root,
        poll_interval_seconds=poll_interval_seconds,
        stale_after_seconds=stale_after_seconds,
        poll_count=poll_count,
        processed_count=processed_count,
        lifecycle_state="completed",
        last_result=last_result,
    )

    return {
        "status": "completed",
        "poll_count": poll_count,
        "processed_count": processed_count,
        "last_result": last_result,
        "watcher_status_path": str(heartbeat_path),
        "handoff_root": str(handoff_root),
        "shared_root": str(shared_root),
    }


def main() -> int:
    result = asyncio.run(_run())
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())