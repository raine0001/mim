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
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS_ARTIFACT = PROJECT_ROOT / "runtime" / "reports" / "mim_evolution_continuous_training.latest.json"
DEFAULT_RECOVERY_ARTIFACT = PROJECT_ROOT / "runtime" / "reports" / "mim_evolution_training_recovery.latest.json"


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


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


def evaluate_training_health(*, status_path: Path, stale_after_seconds: int, now: datetime | None = None) -> dict[str, Any]:
    current_time = now or datetime.now(timezone.utc)
    if not status_path.exists():
        return {
            "status": "missing",
            "reason": "status_artifact_missing",
            "recommended_next_action": "restart_continuous_training",
            "status_artifact": str(status_path),
        }
    payload = _load_json(status_path)
    updated_at = _parse_timestamp(payload.get("updated_at"))
    if updated_at is None:
        return {
            "status": "blocked",
            "reason": "updated_at_missing_or_invalid",
            "recommended_next_action": "restart_continuous_training",
            "status_artifact": str(status_path),
            "payload": payload,
        }
    age_seconds = max(int((current_time - updated_at).total_seconds()), 0)
    if stale_after_seconds > 0 and age_seconds > stale_after_seconds:
        return {
            "status": "stale",
            "reason": "heartbeat_expired",
            "recommended_next_action": "restart_continuous_training",
            "status_artifact": str(status_path),
            "age_seconds": age_seconds,
            "stale_after_seconds": stale_after_seconds,
            "payload": payload,
        }
    return {
        "status": "ok",
        "reason": "heartbeat_fresh",
        "recommended_next_action": "none",
        "status_artifact": str(status_path),
        "age_seconds": age_seconds,
        "stale_after_seconds": stale_after_seconds,
        "payload": payload,
    }


def _restart_or_start_service(*, systemctl_bin: str, scope: str, service_name: str) -> dict[str, Any]:
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


def _cooldown_remaining_seconds(*, previous: dict[str, Any], cooldown_seconds: int, now: datetime) -> int:
    if cooldown_seconds <= 0:
        return 0
    last_attempt_at = _parse_timestamp(
        previous.get("last_recovery_started_at") or previous.get("last_recovery_at") or ""
    )
    if last_attempt_at is None:
        return 0
    age_seconds = max(int((now - last_attempt_at).total_seconds()), 0)
    if age_seconds >= cooldown_seconds:
        return 0
    return cooldown_seconds - age_seconds


def run_supervisor_cycle() -> dict[str, Any]:
    systemctl_bin = str(os.environ.get("MIM_TRAINING_SYSTEMCTL_BIN", "systemctl")).strip() or "systemctl"
    service_scope = str(os.environ.get("MIM_TRAINING_SERVICE_SCOPE", "user")).strip().lower() or "user"
    service_name = str(os.environ.get("MIM_TRAINING_SERVICE_NAME", "mim-evolution-training.service")).strip() or "mim-evolution-training.service"
    status_path = Path(os.environ.get("MIM_TRAINING_STATUS_ARTIFACT", str(DEFAULT_STATUS_ARTIFACT))).expanduser().resolve()
    recovery_path = Path(os.environ.get("MIM_TRAINING_RECOVERY_ARTIFACT", str(DEFAULT_RECOVERY_ARTIFACT))).expanduser().resolve()
    stale_after_seconds = _env_int("MIM_TRAINING_STALE_AFTER_SECONDS", 180)
    cooldown_seconds = _env_int("MIM_TRAINING_RECOVERY_COOLDOWN_SECONDS", 180)
    startup_grace_seconds = _env_float("MIM_TRAINING_STARTUP_GRACE_SECONDS", 30.0)

    if service_scope not in {"system", "user"}:
        service_scope = "user"

    now = datetime.now(timezone.utc)
    previous = _load_json(recovery_path)
    guard_result = evaluate_training_health(status_path=status_path, stale_after_seconds=stale_after_seconds, now=now)
    service_is_active = _service_active(systemctl_bin=systemctl_bin, scope=service_scope, service_name=service_name)
    recovery_payload: dict[str, Any] = {
        "artifact_type": "mim-evolution-training-recovery-v1",
        "updated_at": _utc_now(),
        "watcher_service_name": service_name,
        "watcher_service_scope": service_scope,
        "status_artifact": str(status_path),
        "guard_result": guard_result,
        "service_active": service_is_active,
        "cooldown_seconds": cooldown_seconds,
        "restart_attempt_count": int(previous.get("restart_attempt_count") or 0),
        "last_recovery_started_at": str(previous.get("last_recovery_started_at") or ""),
        "last_recovery_status": str(previous.get("last_recovery_status") or ""),
    }

    if guard_result.get("status") == "ok" and service_is_active:
        recovery_payload.update(
            {
                "status": "healthy",
                "reason": "training_service_healthy",
                "recommended_next_action": "none",
            }
        )
        _write_json(recovery_path, recovery_payload)
        return recovery_payload

    cooldown_remaining = _cooldown_remaining_seconds(previous=previous, cooldown_seconds=cooldown_seconds, now=now)
    if cooldown_remaining > 0:
        recovery_payload.update(
            {
                "status": "cooldown_active",
                "reason": "recovery_cooldown_active",
                "recommended_next_action": "restart_continuous_training",
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
    recovery_payload.update(
        {
            "restart_attempt_count": int(previous.get("restart_attempt_count") or 0) + 1,
            "last_recovery_started_at": _utc_now(),
            "last_recovery_status": "restart_requested",
            "recommended_next_action": "restart_continuous_training",
            "service_action": service_result,
        }
    )
    if startup_grace_seconds > 0:
        time.sleep(startup_grace_seconds)

    post_guard_result = evaluate_training_health(status_path=status_path, stale_after_seconds=stale_after_seconds)
    recovered = bool(service_result.get("succeeded")) and post_guard_result.get("status") == "ok"
    recovery_payload.update(
        {
            "post_recovery_guard": post_guard_result,
            "status": "recovered" if recovered else "recovery_failed",
            "reason": "training_service_recovered" if recovered else str(post_guard_result.get("reason") or "recovery_verification_failed"),
            "last_recovery_status": "recovered" if recovered else "recovery_failed",
            "last_recovery_at": _utc_now(),
        }
    )
    _write_json(recovery_path, recovery_payload)
    return recovery_payload


def _run() -> None:
    poll_seconds = _env_float("MIM_TRAINING_WATCHDOG_POLL_SECONDS", 30.0)
    run_once = str(os.environ.get("MIM_TRAINING_WATCHDOG_RUN_ONCE", "")).strip().lower() in {"1", "true", "yes", "on"}
    max_cycles = _env_int("MIM_TRAINING_WATCHDOG_MAX_CYCLES", 0)
    cycle_count = 0
    while True:
        cycle_count += 1
        payload = run_supervisor_cycle()
        if run_once:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return
        if max_cycles > 0 and cycle_count >= max_cycles:
            return
        time.sleep(max(poll_seconds, 1.0))


if __name__ == "__main__":
    try:
        _run()
    except KeyboardInterrupt:
        sys.exit(130)