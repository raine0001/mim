from __future__ import annotations

import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_ROOT = PROJECT_ROOT / "runtime" / "reports"
DEFAULT_STATUS_ARTIFACT = REPORT_ROOT / "mim_evolution_continuous_training.latest.json"
DEFAULT_SUMMARY_REPORT = REPORT_ROOT / "mim_evolution_training_summary.json"
DEFAULT_RECOVERY_ARTIFACT = REPORT_ROOT / "mim_evolution_training_recovery.latest.json"


def _env_text(env: Mapping[str, str], name: str, default: str) -> str:
    return str(env.get(name, default)).strip() or default


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw_value = str(env.get(name, str(default))).strip()
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value >= 0 else default


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw_value = str(env.get(name, str(default))).strip()
    try:
        value = float(raw_value)
    except ValueError:
        return default
    return value if value >= 0 else default


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _compact_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(int(minimum), min(int(maximum), int(value)))


def _systemctl_base(*, systemctl_bin: str, scope: str) -> list[str]:
    parts = [systemctl_bin]
    if scope == "user":
        parts.append("--user")
    return parts


def _run_systemctl(*, systemctl_bin: str, scope: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _systemctl_base(systemctl_bin=systemctl_bin, scope=scope) + args,
        check=False,
        capture_output=True,
        text=True,
    )


def _service_active(*, systemctl_bin: str, scope: str, service_name: str) -> bool:
    result = _run_systemctl(
        systemctl_bin=systemctl_bin,
        scope=scope,
        args=["is-active", "--quiet", service_name],
    )
    return result.returncode == 0


def load_routine_profile(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    source = env or os.environ
    target_window_seconds = _env_int(source, "MIM_TRAINING_TARGET_WINDOW_SECONDS", 5400)
    max_cycle_seconds = _env_int(source, "MIM_TRAINING_MAX_CYCLE_SECONDS", 7200)
    min_conversations = _env_int(source, "MIM_TRAINING_MIN_CONVERSATIONS", 240)
    max_conversations = _env_int(source, "MIM_TRAINING_MAX_CONVERSATIONS", 720)
    default_conversations = _env_int(source, "MIM_TRAINING_TARGET_CONVERSATIONS", 320)
    step_conversations = _env_int(source, "MIM_TRAINING_STEP_CONVERSATIONS", 80)
    request_timeout_seconds = _env_int(source, "MIM_TRAINING_REQUEST_TIMEOUT_SECONDS", 90)
    throughput_utilization = _env_float(source, "MIM_TRAINING_THROUGHPUT_UTILIZATION", 0.85)
    min_conversations = max(1, min_conversations)
    max_conversations = max(min_conversations, max_conversations)
    default_conversations = _clamp(default_conversations, min_conversations, max_conversations)
    step_conversations = max(1, step_conversations)
    return {
        "profile_id": _env_text(source, "MIM_TRAINING_PROFILE_ID", "routine"),
        "label": _env_text(source, "MIM_TRAINING_PROFILE_LABEL", "Routine bounded training"),
        "target_window_seconds": max(1800, target_window_seconds),
        "max_cycle_seconds": max(max_cycle_seconds, target_window_seconds),
        "min_conversations": min_conversations,
        "max_conversations": max_conversations,
        "default_conversations": default_conversations,
        "step_conversations": step_conversations,
        "request_timeout_seconds": max(30, request_timeout_seconds),
        "throughput_utilization": min(max(throughput_utilization, 0.25), 0.95),
        "base_url": _env_text(source, "MIM_TRAINING_BASE_URL", "http://127.0.0.1:18021"),
        "service_scope": _env_text(source, "MIM_TRAINING_SERVICE_SCOPE", "user").lower(),
        "training_service_name": _env_text(source, "MIM_TRAINING_SERVICE_NAME", "mim-evolution-training.service"),
        "runtime_service_name": _env_text(source, "MIM_TRAINING_RUNTIME_SERVICE_NAME", "mim-training-web.service"),
        "systemctl_bin": _env_text(source, "MIM_TRAINING_SYSTEMCTL_BIN", "systemctl"),
        "status_path": Path(_env_text(source, "MIM_TRAINING_STATUS_ARTIFACT", str(DEFAULT_STATUS_ARTIFACT))).expanduser().resolve(),
        "summary_report_path": Path(_env_text(source, "MIM_TRAINING_SUMMARY_REPORT", str(DEFAULT_SUMMARY_REPORT))).expanduser().resolve(),
        "recovery_artifact_path": Path(_env_text(source, "MIM_TRAINING_RECOVERY_ARTIFACT", str(DEFAULT_RECOVERY_ARTIFACT))).expanduser().resolve(),
    }


def _extract_summary_metrics(previous_status: dict[str, Any], previous_summary: dict[str, Any]) -> dict[str, float | int | None]:
    summary_conversation = (
        previous_summary.get("conversation", {})
        if isinstance(previous_summary.get("conversation"), dict)
        else {}
    )
    summary_actions = (
        previous_summary.get("actions", {})
        if isinstance(previous_summary.get("actions"), dict)
        else {}
    )
    status_metrics = (
        previous_status.get("metrics_json", {}).get("summary", {})
        if isinstance(previous_status.get("metrics_json"), dict)
        and isinstance(previous_status.get("metrics_json", {}).get("summary"), dict)
        else {}
    )

    def _metric_value(source_a: dict[str, Any], source_b: dict[str, Any], key: str) -> float | None:
        for source in (source_a, source_b):
            try:
                value = source.get(key)
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _int_value(source_a: dict[str, Any], source_b: dict[str, Any], key: str) -> int | None:
        for source in (source_a, source_b):
            try:
                value = source.get(key)
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None

    return {
        "overall": _metric_value(summary_conversation, status_metrics, "overall"),
        "scenario_count": _int_value(summary_conversation, status_metrics, "scenario_count"),
        "failure_count": _int_value(summary_conversation, status_metrics, "failure_count"),
        "action_pass_ratio": _metric_value(summary_actions, status_metrics, "pass_ratio")
        or _metric_value(summary_actions, status_metrics, "action_pass_ratio"),
    }


def build_next_cycle_plan(
    *,
    profile: dict[str, Any],
    previous_status: dict[str, Any] | None = None,
    previous_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = previous_status or {}
    summary = previous_summary or {}
    default_target = int(profile.get("default_conversations") or 320)
    min_target = int(profile.get("min_conversations") or default_target)
    max_target = int(profile.get("max_conversations") or default_target)
    step = int(profile.get("step_conversations") or 80)
    target_window_seconds = int(profile.get("target_window_seconds") or 5400)
    throughput_utilization = float(profile.get("throughput_utilization") or 0.85)
    elapsed_seconds = 0.0
    try:
        elapsed_seconds = float(status.get("elapsed_seconds") or 0.0)
    except (TypeError, ValueError):
        elapsed_seconds = 0.0
    metrics = _extract_summary_metrics(status, summary)
    scenario_count = int(metrics.get("scenario_count") or 0)
    overall = float(metrics.get("overall") or 0.0)
    failure_count = int(metrics.get("failure_count") or 0)
    action_pass_ratio = float(metrics.get("action_pass_ratio") or 0.0)
    target = default_target
    reasons: list[str] = []
    estimated_duration_seconds: int | None = None
    if elapsed_seconds > 0 and scenario_count > 0:
        throughput_per_second = scenario_count / elapsed_seconds
        throughput_target = int(throughput_per_second * target_window_seconds * throughput_utilization)
        if throughput_target > 0:
            target = throughput_target
            estimated_duration_seconds = int((target / max(throughput_per_second, 0.0001)))
            reasons.append("matched prior throughput to the routine window")
    failure_rate = (failure_count / scenario_count) if scenario_count > 0 else 0.0
    quality_signal = "hold"
    if scenario_count > 0:
        if overall >= 0.84 and failure_rate <= 0.15 and action_pass_ratio >= 0.95:
            target += step
            quality_signal = "expand"
            reasons.append("previous cycle cleared the quality bar, so the next batch can expand")
        elif overall < 0.78 or failure_rate >= 0.50 or action_pass_ratio < 0.90 or int(status.get("run_exit_code") or 0) != 0:
            target -= step
            quality_signal = "reduce"
            reasons.append("previous cycle showed quality or runtime pressure, so the next batch is reduced")
        else:
            reasons.append("previous cycle was mixed, so the next batch stays near the routine baseline")
    target = _clamp(target, min_target, max_target)
    if estimated_duration_seconds is None and elapsed_seconds > 0 and scenario_count > 0 and target > 0:
        estimated_duration_seconds = int((elapsed_seconds / scenario_count) * target)
    if not reasons:
        reasons.append("no completed cycle metrics were available, so the routine baseline is used")
    return {
        "profile_id": str(profile.get("profile_id") or "routine").strip() or "routine",
        "label": str(profile.get("label") or "Routine bounded training").strip() or "Routine bounded training",
        "target_conversations": int(target),
        "min_conversations": int(min_target),
        "max_conversations": int(max_target),
        "default_conversations": int(default_target),
        "target_window_seconds": int(target_window_seconds),
        "max_cycle_seconds": int(profile.get("max_cycle_seconds") or target_window_seconds),
        "request_timeout_seconds": int(profile.get("request_timeout_seconds") or 90),
        "estimated_duration_seconds": int(estimated_duration_seconds or 0),
        "quality_signal": quality_signal,
        "overall": round(overall, 4) if scenario_count > 0 else None,
        "failure_rate": round(failure_rate, 4) if scenario_count > 0 else None,
        "action_pass_ratio": round(action_pass_ratio, 4) if scenario_count > 0 else None,
        "scenario_count": int(scenario_count),
        "failure_count": int(failure_count),
        "optimization_reason": " ".join(reasons),
    }


def _format_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "Unknown"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes = max(1, remainder // 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def build_training_routine_snapshot(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    profile = load_routine_profile(env)
    status = _read_json(profile["status_path"])
    summary = _read_json(profile["summary_report_path"])
    recovery = _read_json(profile["recovery_artifact_path"])
    training_active = _service_active(
        systemctl_bin=str(profile["systemctl_bin"]),
        scope=str(profile["service_scope"]),
        service_name=str(profile["training_service_name"]),
    )
    runtime_active = _service_active(
        systemctl_bin=str(profile["systemctl_bin"]),
        scope=str(profile["service_scope"]),
        service_name=str(profile["runtime_service_name"]),
    )
    updated_at = _parse_timestamp(status.get("updated_at"))
    age_seconds = max(int((_utc_now() - updated_at).total_seconds()), 0) if updated_at else None
    next_cycle_plan = (
        status.get("next_cycle_plan")
        if isinstance(status.get("next_cycle_plan"), dict)
        else build_next_cycle_plan(profile=profile, previous_status=status, previous_summary=summary)
    )
    summary_conversation = summary.get("conversation", {}) if isinstance(summary.get("conversation"), dict) else {}
    latest_overall = summary_conversation.get("overall")
    latest_scenarios = summary_conversation.get("scenario_count")
    latest_failures = summary_conversation.get("failure_count")
    phase = str(status.get("phase") or "idle").strip() or "idle"
    if training_active and phase == "simulation_running":
        state_label = "Running"
    elif training_active and phase == "waiting_for_runtime":
        state_label = "Waiting for runtime"
    elif training_active:
        state_label = "Armed"
    else:
        state_label = "Stopped"
    if recovery.get("status") == "recovery_failed":
        state_label = "Recovery needed"
    summary_text = (
        f"Routine profile targets {_format_duration(next_cycle_plan.get('target_window_seconds'))} windows with "
        f"{next_cycle_plan.get('target_conversations')} conversations in the next batch."
    )
    detail = str(next_cycle_plan.get("optimization_reason") or "").strip()
    if latest_overall is not None and latest_scenarios is not None:
        detail = (
            f"Last completed cycle: overall {float(latest_overall):.3f} across {int(latest_scenarios)} conversations"
            f" with {int(latest_failures or 0)} failures. {detail}"
        ).strip()
    return {
        "available": True,
        "profile": profile,
        "state_label": state_label,
        "phase": phase,
        "active": bool(training_active),
        "training_service_active": bool(training_active),
        "runtime_service_active": bool(runtime_active),
        "base_url": str(profile.get("base_url") or ""),
        "cycle": int(status.get("cycle") or 0),
        "updated_at": status.get("updated_at") or "",
        "updated_age_seconds": age_seconds,
        "summary": summary_text,
        "detail": _compact_text(detail, 320),
        "next_cycle_plan": next_cycle_plan,
        "latest_cycle": {
            "overall": float(latest_overall) if latest_overall is not None else None,
            "scenario_count": int(latest_scenarios or 0),
            "failure_count": int(latest_failures or 0),
            "proof_summary": str(status.get("proof_summary") or "").strip(),
            "elapsed_seconds": float(status.get("elapsed_seconds") or 0.0),
            "evaluation_outcome": str(status.get("evaluation_outcome") or "").strip(),
        },
        "recovery": recovery,
        "controls": {
            "can_start": not training_active,
            "can_stop": bool(training_active or runtime_active),
            "can_restart": bool(runtime_active or training_active),
        },
    }


def control_training_routine(action: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    profile = load_routine_profile(env)
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"start", "stop", "restart"}:
        raise ValueError("unsupported_training_action")
    systemctl_bin = str(profile["systemctl_bin"])
    scope = str(profile["service_scope"])
    runtime_service_name = str(profile["runtime_service_name"])
    training_service_name = str(profile["training_service_name"])

    def _action(service_name: str, verb: str) -> dict[str, Any]:
        result = _run_systemctl(systemctl_bin=systemctl_bin, scope=scope, args=[verb, service_name])
        return {
            "service_name": service_name,
            "verb": verb,
            "command": " ".join(
                shlex.quote(part) for part in (_systemctl_base(systemctl_bin=systemctl_bin, scope=scope) + [verb, service_name])
            ),
            "returncode": int(result.returncode),
            "stdout": _compact_text(result.stdout, 240),
            "stderr": _compact_text(result.stderr, 240),
            "active": _service_active(systemctl_bin=systemctl_bin, scope=scope, service_name=service_name),
        }

    actions: list[dict[str, Any]] = []
    if normalized_action == "start":
        actions.append(_action(runtime_service_name, "start"))
        actions.append(_action(training_service_name, "start"))
    elif normalized_action == "restart":
        actions.append(_action(runtime_service_name, "restart"))
        actions.append(_action(training_service_name, "restart"))
    else:
        actions.append(_action(training_service_name, "stop"))
        actions.append(_action(runtime_service_name, "stop"))
    snapshot = build_training_routine_snapshot(env)
    ok = all(int(item.get("returncode") or 0) == 0 for item in actions)
    return {
        "ok": ok,
        "action": normalized_action,
        "services": actions,
        "training": snapshot,
        "generated_at": _utc_now_iso(),
    }
