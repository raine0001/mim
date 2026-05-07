#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.handoff_intake_service import DEFAULT_HANDOFF_ROOT, ensure_handoff_directories  # noqa: E402
from scripts.check_handoff_watcher_status import (  # noqa: E402
    STALE_WATCHER_RECOVERY_HINT,
    evaluate_watcher_status,
)


WATCHER_RECOVERY_ARTIFACT = "HANDOFF_WATCHER_RECOVERY.latest.json"


def _env_float(name: str, default: float) -> float:
    raw_value = str(os.environ.get(name, str(default))).strip()
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_int(name: str, default: int) -> int:
    raw_value = str(os.environ.get(name, str(default))).strip()
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw_value = str(os.environ.get(name, str(default))).strip().lower()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    return default


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


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _status_dir(handoff_root: Path) -> Path:
    return ensure_handoff_directories(handoff_root=handoff_root)["status"]


def _recovery_path(handoff_root: Path) -> Path:
    return _status_dir(handoff_root) / WATCHER_RECOVERY_ARTIFACT


def _systemctl_base(*, systemctl_bin: str, scope: str) -> list[str]:
    base = [systemctl_bin]
    if scope == "user":
        base.append("--user")
    return base


def _run_systemctl(*, systemctl_bin: str, scope: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    command = _systemctl_base(systemctl_bin=systemctl_bin, scope=scope) + args
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _service_active(*, systemctl_bin: str, scope: str, service_name: str) -> bool:
    result = _run_systemctl(
        systemctl_bin=systemctl_bin,
        scope=scope,
        args=["is-active", "--quiet", service_name],
    )
    return result.returncode == 0


def _trim_output(value: str, limit: int = 300) -> str:
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _restart_or_start_service(
    *,
    systemctl_bin: str,
    scope: str,
    service_name: str,
) -> dict[str, object]:
    was_active = _service_active(systemctl_bin=systemctl_bin, scope=scope, service_name=service_name)
    action = "restart" if was_active else "start"
    command = _systemctl_base(systemctl_bin=systemctl_bin, scope=scope) + [action, service_name]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    is_active = _service_active(systemctl_bin=systemctl_bin, scope=scope, service_name=service_name)
    return {
        "action": action,
        "before_active": was_active,
        "after_active": is_active,
        "command": " ".join(shlex.quote(part) for part in command),
        "returncode": result.returncode,
        "stdout": _trim_output(result.stdout),
        "stderr": _trim_output(result.stderr),
        "succeeded": result.returncode == 0 and is_active,
    }


def _cooldown_remaining_seconds(*, previous: dict[str, object], cooldown_seconds: int, now: datetime) -> int:
    if cooldown_seconds <= 0:
        return 0
    last_attempt_at = _parse_timestamp(str(previous.get("last_recovery_started_at") or previous.get("last_recovery_at") or ""))
    if last_attempt_at is None:
        return 0
    age_seconds = max(int((now - last_attempt_at).total_seconds()), 0)
    if age_seconds >= cooldown_seconds:
        return 0
    return cooldown_seconds - age_seconds


def run_supervisor_cycle(*, handoff_root: Path) -> dict[str, object]:
    systemctl_bin = str(os.environ.get("MIM_HANDOFF_SYSTEMCTL_BIN", "systemctl")).strip() or "systemctl"
    service_scope = str(os.environ.get("MIM_HANDOFF_WATCHER_SERVICE_SCOPE", "user")).strip().lower() or "user"
    service_name = str(os.environ.get("MIM_HANDOFF_WATCHER_SERVICE_NAME", "mim-handoff-watcher.service")).strip() or "mim-handoff-watcher.service"
    cooldown_seconds = _env_int("MIM_HANDOFF_RECOVERY_COOLDOWN_SECONDS", 60)
    startup_grace_seconds = _env_float("MIM_HANDOFF_RECOVERY_STARTUP_GRACE_SECONDS", 3.0)

    if service_scope not in {"system", "user"}:
        service_scope = "user"

    now = datetime.now(timezone.utc)
    recovery_path = _recovery_path(handoff_root)
    previous = _load_json(recovery_path)
    guard_result = evaluate_watcher_status(handoff_root=handoff_root, now=now)
    recovery_payload: dict[str, object] = {
        "artifact_type": "mim-handoff-watcher-recovery-v1",
        "updated_at": _utc_now(),
        "handoff_root": str(handoff_root),
        "watcher_service_name": service_name,
        "watcher_service_scope": service_scope,
        "cooldown_seconds": cooldown_seconds,
        "guard_result": guard_result,
        "restart_attempt_count": int(previous.get("restart_attempt_count") or 0),
        "last_recovery_started_at": str(previous.get("last_recovery_started_at") or ""),
        "last_recovery_status": str(previous.get("last_recovery_status") or ""),
    }

    if str(guard_result.get("status") or "") == "ok":
        recovery_payload.update(
            {
                "status": "healthy",
                "reason": str(guard_result.get("reason") or "watcher_status_fresh"),
                "recommended_next_action": "none",
            }
        )
        _write_json(recovery_path, recovery_payload)
        return recovery_payload

    recommended_next_action = str(guard_result.get("recommended_next_action") or "").strip()
    if recommended_next_action != STALE_WATCHER_RECOVERY_HINT:
        recovery_payload.update(
            {
                "status": "blocked",
                "reason": "unsupported_recovery_action",
                "recommended_next_action": recommended_next_action or "none",
            }
        )
        _write_json(recovery_path, recovery_payload)
        return recovery_payload

    cooldown_remaining = _cooldown_remaining_seconds(
        previous=previous,
        cooldown_seconds=cooldown_seconds,
        now=now,
    )
    if cooldown_remaining > 0:
        recovery_payload.update(
            {
                "status": "cooldown_active",
                "reason": str(guard_result.get("reason") or "recovery_cooldown_active"),
                "recommended_next_action": STALE_WATCHER_RECOVERY_HINT,
                "cooldown_remaining_seconds": cooldown_remaining,
            }
        )
        _write_json(recovery_path, recovery_payload)
        return recovery_payload

    service_result = _restart_or_start_service(
        systemctl_bin=systemctl_bin,
        scope=service_scope,
        service_name=service_name,
    )
    restart_attempt_count = int(previous.get("restart_attempt_count") or 0) + 1
    recovery_payload.update(
        {
            "restart_attempt_count": restart_attempt_count,
            "last_recovery_started_at": _utc_now(),
            "last_recovery_status": "restart_requested",
            "recommended_next_action": STALE_WATCHER_RECOVERY_HINT,
            "service_action": service_result,
        }
    )

    if startup_grace_seconds > 0:
        time.sleep(startup_grace_seconds)

    post_recovery_guard = evaluate_watcher_status(handoff_root=handoff_root)
    recovered = bool(service_result.get("succeeded")) and str(post_recovery_guard.get("status") or "") == "ok"

    recovery_payload.update(
        {
            "post_recovery_guard": post_recovery_guard,
            "status": "recovered" if recovered else "recovery_failed",
            "reason": "watcher_recovered" if recovered else str(post_recovery_guard.get("reason") or "recovery_verification_failed"),
            "last_recovery_status": "recovered" if recovered else "recovery_failed",
            "last_recovery_at": _utc_now(),
        }
    )
    _write_json(recovery_path, recovery_payload)
    return recovery_payload


def _run() -> dict[str, object]:
    handoff_root = Path(os.environ.get("MIM_HANDOFF_ROOT", str(DEFAULT_HANDOFF_ROOT))).expanduser().resolve()
    poll_seconds = _env_float("MIM_HANDOFF_RECOVERY_POLL_SECONDS", 10.0)
    run_once = _env_bool("MIM_HANDOFF_RECOVERY_RUN_ONCE", False)
    max_cycles = _env_int("MIM_HANDOFF_RECOVERY_MAX_CYCLES", 0)

    cycle_count = 0
    last_result: dict[str, object] = {}
    while True:
        cycle_count += 1
        last_result = run_supervisor_cycle(handoff_root=handoff_root)
        if run_once:
            break
        if max_cycles and cycle_count >= max_cycles:
            break
        time.sleep(max(poll_seconds, 0.1))

    return {
        "status": "completed",
        "cycle_count": cycle_count,
        "last_result": last_result,
        "recovery_status_path": str(_recovery_path(handoff_root)),
        "handoff_root": str(handoff_root),
    }


def main() -> int:
    result = _run()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())