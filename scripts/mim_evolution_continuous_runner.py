#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.training_routine_service import build_next_cycle_plan, load_routine_profile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = PROJECT_ROOT / "runtime" / "reports"
DEFAULT_STATUS_ARTIFACT = REPORT_DIR / "mim_evolution_continuous_training.latest.json"
DEFAULT_SUMMARY_REPORT = REPORT_DIR / "mim_evolution_training_summary.json"
DEFAULT_CONVERSATION_REPORT = REPORT_DIR / "mim_evolution_conversation_report.json"
DEFAULT_ACTION_REPORT = REPORT_DIR / "mim_action_simulation_report.json"
DEFAULT_LOG_DIR = PROJECT_ROOT / "runtime" / "logs" / "mim_evolution_training"
SIMULATION_SCRIPT = PROJECT_ROOT / "scripts" / "run_mim_evolution_simulations.sh"
NUMERIC_SCORE_KEYS = (
    "overall",
    "relevance",
    "task_completion",
    "initiative",
    "smoothness",
    "brevity",
    "non_repetition",
    "safety",
)
FAILURE_TO_SKILL_CANDIDATE = {
    "clarification_spam": "clarification_discipline",
    "repeated_clarifier_pattern": "clarification_discipline",
    "context_drift": "followup_continuity",
    "low_relevance": "direct_answer_grounding",
    "question_not_answered": "direct_answer_grounding",
    "missing_safety_boundary": "safety_boundary_directness",
    "over_explaining": "concise_status_reporting",
    "response_loop_risk": "loop_recovery_control",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env_int(name: str, default: int) -> int:
    raw_value = str(os.environ.get(name, str(default))).strip()
    try:
        return int(raw_value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw_value = str(os.environ.get(name, str(default))).strip()
    try:
        return float(raw_value)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw_value = str(os.environ.get(name, str(default))).strip().lower()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    return default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _round_metric(value: float) -> float:
    return round(float(value), 4)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _http_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 60) -> tuple[int, dict[str, Any]]:
    body = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, method=method.upper(), data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read().decode("utf-8")
            parsed = json.loads(content) if content else {}
            return response.status, parsed if isinstance(parsed, dict) else {}
    except urllib.error.HTTPError as exc:
        content = exc.read().decode("utf-8")
        parsed = json.loads(content) if content else {}
        return exc.code, parsed if isinstance(parsed, dict) else {}
    except urllib.error.URLError as exc:
        return 0, {"error": "url_error", "reason": str(getattr(exc, "reason", exc))}


def _join_url(base_url: str, path: str, query: dict[str, Any] | None = None) -> str:
    normalized_base = str(base_url or "").rstrip("/")
    url = f"{normalized_base}{path}"
    if query:
        encoded = urllib.parse.urlencode(query)
        if encoded:
            url = f"{url}?{encoded}"
    return url


def fetch_progress(*, base_url: str, actor: str, source: str) -> tuple[int, dict[str, Any]]:
    return _http_json(
        "GET",
        _join_url(
            base_url,
            "/improvement/self-evolution/natural-language/progress",
            {"actor": actor, "source": source},
        ),
        timeout=30,
    )


def reset_progress(*, base_url: str, actor: str, source: str) -> tuple[int, dict[str, Any]]:
    return _http_json(
        "POST",
        _join_url(base_url, "/improvement/self-evolution/natural-language/reset"),
        {"actor": actor, "source": source},
        timeout=60,
    )


def evaluate_progress(
    *,
    base_url: str,
    actor: str,
    source: str,
    slice_id: str,
    metrics_json: dict[str, Any],
    failure_tags: list[str],
    proof_summary: str,
    discovered_skill_candidates: list[str],
    metadata_json: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    return _http_json(
        "POST",
        _join_url(base_url, "/improvement/self-evolution/natural-language/evaluate"),
        {
            "actor": actor,
            "source": source,
            "slice_id": slice_id,
            "metrics_json": metrics_json,
            "failure_tags": failure_tags,
            "proof_summary": proof_summary,
            "discovered_skill_candidates": discovered_skill_candidates,
            "metadata_json": metadata_json,
        },
        timeout=120,
    )


def ensure_progress_ready(
    *,
    base_url: str,
    actor: str,
    source: str,
    reset_if_not_running: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    status_code, payload = fetch_progress(base_url=base_url, actor=actor, source=source)
    progress = payload.get("progress", {}) if isinstance(payload.get("progress"), dict) else {}
    progress_status = str(progress.get("status") or "").strip().lower()
    active_slice_id = str(progress.get("active_slice_id") or "").strip()
    if status_code == 200 and active_slice_id and progress_status in {"running", "repairing"}:
        return progress, {"action": "reused", "status_code": status_code}
    if not reset_if_not_running:
        return progress, {"action": "unchanged", "status_code": status_code}
    reset_status, reset_payload = reset_progress(base_url=base_url, actor=actor, source=source)
    reset_progress_payload = reset_payload.get("progress", {}) if isinstance(reset_payload.get("progress"), dict) else {}
    return reset_progress_payload, {"action": "reset", "status_code": reset_status}


def collect_evaluation_inputs(
    *,
    conversation_report_path: Path,
    training_summary_path: Path,
) -> dict[str, Any]:
    conversation_report = _read_json(conversation_report_path)
    training_summary = _read_json(training_summary_path)
    conversation_summary = (
        conversation_report.get("summary", {})
        if isinstance(conversation_report.get("summary"), dict)
        else {}
    )
    summary_conversation = (
        training_summary.get("conversation", {})
        if isinstance(training_summary.get("conversation"), dict)
        else {}
    )
    summary_actions = (
        training_summary.get("actions", {})
        if isinstance(training_summary.get("actions"), dict)
        else {}
    )
    results = conversation_report.get("results", []) if isinstance(conversation_report.get("results"), list) else []

    metric_values: dict[str, list[float]] = {key: [] for key in NUMERIC_SCORE_KEYS}
    failure_counter: Counter[str] = Counter()
    for result in results:
        if not isinstance(result, dict):
            continue
        score = result.get("score", {}) if isinstance(result.get("score"), dict) else {}
        for key in NUMERIC_SCORE_KEYS:
            raw_value = score.get(key)
            try:
                if raw_value is not None:
                    metric_values[key].append(float(raw_value))
            except (TypeError, ValueError):
                continue
        for tag in result.get("failures", []) if isinstance(result.get("failures"), list) else []:
            normalized = str(tag or "").strip().lower()
            if normalized:
                failure_counter[normalized] += 1

    top_failures = conversation_summary.get("top_failures", []) if isinstance(conversation_summary.get("top_failures"), list) else []
    for item in top_failures:
        if not isinstance(item, dict):
            continue
        normalized = str(item.get("tag") or "").strip().lower()
        count = int(item.get("count") or 0)
        if normalized and count > 0 and failure_counter[normalized] < count:
            failure_counter[normalized] = count

    metrics: dict[str, float] = {}
    for key in NUMERIC_SCORE_KEYS:
        actual = _mean(metric_values[key])
        if actual is None:
            summary_value = conversation_summary.get(key)
            if summary_value is None:
                summary_value = summary_conversation.get(key)
            try:
                actual = float(summary_value)
            except (TypeError, ValueError):
                actual = None
        if actual is not None:
            metrics[key] = _round_metric(actual)

    if "overall" not in metrics:
        try:
            metrics["overall"] = _round_metric(float(summary_conversation.get("overall") or 0.0))
        except (TypeError, ValueError):
            metrics["overall"] = 0.0

    action_pass_ratio = 0.0
    try:
        action_pass_ratio = float(summary_actions.get("pass_ratio") or 0.0)
    except (TypeError, ValueError):
        action_pass_ratio = 0.0

    stage_metrics = {
        **metrics,
        "scenario_count": int(summary_conversation.get("scenario_count") or len(results) or 0),
        "failure_count": int(summary_conversation.get("failure_count") or sum(failure_counter.values()) or 0),
        "action_pass_ratio": _round_metric(action_pass_ratio),
    }
    metrics_json = {
        **metrics,
        "smoke": dict(stage_metrics),
        "expanded": dict(stage_metrics),
        "summary": {
            **stage_metrics,
            "bucket_average": conversation_summary.get("bucket_average", {}),
        },
    }
    failure_tags = sorted(failure_counter)
    discovered_skill_candidates = [
        candidate
        for candidate in dict.fromkeys(
            FAILURE_TO_SKILL_CANDIDATE[tag]
            for tag in failure_tags
            if tag in FAILURE_TO_SKILL_CANDIDATE
        )
    ]

    return {
        "metrics_json": metrics_json,
        "failure_tags": failure_tags,
        "top_failures": [
            {"tag": tag, "count": count}
            for tag, count in failure_counter.most_common(8)
        ],
        "discovered_skill_candidates": discovered_skill_candidates,
        "conversation_overall": metrics.get("overall", 0.0),
        "action_pass_ratio": _round_metric(action_pass_ratio),
        "scenario_count": int(stage_metrics["scenario_count"]),
    }


def build_proof_summary(*, cycle: int, evaluation_inputs: dict[str, Any]) -> str:
    top_failures = evaluation_inputs.get("top_failures", []) if isinstance(evaluation_inputs.get("top_failures"), list) else []
    failure_summary = ", ".join(
        f"{item.get('tag')}={item.get('count')}"
        for item in top_failures[:4]
        if isinstance(item, dict) and str(item.get("tag") or "").strip()
    )
    if not failure_summary:
        failure_summary = "none"
    return (
        f"Continuous training cycle {cycle} recorded overall={evaluation_inputs.get('conversation_overall', 0.0):.4f}, "
        f"action_pass_ratio={evaluation_inputs.get('action_pass_ratio', 0.0):.4f}, "
        f"scenario_count={int(evaluation_inputs.get('scenario_count', 0) or 0)}, "
        f"top_failures={failure_summary}."
    )


def _build_status_payload(**payload: Any) -> dict[str, Any]:
    return {"updated_at": _utc_now(), **payload}


def _status_cycle(path: Path) -> int:
    current = _read_json(path)
    return int(current.get("cycle", 0) or 0)


def _wait_for_runtime_ready(
    *,
    base_url: str,
    status_path: Path,
    status_payload: dict[str, Any],
    heartbeat_seconds: float,
) -> None:
    health_url = _join_url(base_url, "/health")
    while True:
        status_code, payload = _http_json("GET", health_url, timeout=15)
        if status_code == 200:
            return
        _write_json(
            status_path,
            _build_status_payload(
                **status_payload,
                phase="waiting_for_runtime",
                runtime_health_status_code=status_code,
                runtime_health_payload=payload,
            ),
        )
        time.sleep(max(float(heartbeat_seconds), 1.0))


def _sleep_with_heartbeat(*, seconds: float, status_path: Path, payload: dict[str, Any], heartbeat_seconds: float) -> None:
    remaining = max(float(seconds), 0.0)
    while remaining > 0:
        _write_json(
            status_path,
            _build_status_payload(
                **payload,
                phase="sleeping",
                sleep_remaining_seconds=round(remaining, 2),
            ),
        )
        interval = min(max(float(heartbeat_seconds), 1.0), remaining)
        time.sleep(interval)
        remaining = max(remaining - interval, 0.0)


def _run_simulation_process(
    *,
    command: list[str],
    env: dict[str, str],
    log_path: Path,
    status_path: Path,
    status_payload: dict[str, Any],
    heartbeat_seconds: float,
    max_runtime_seconds: float,
) -> tuple[int, float, bool]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    start_time = time.monotonic()
    with log_path.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        while True:
            exit_code = process.poll()
            elapsed = time.monotonic() - start_time
            _write_json(
                status_path,
                _build_status_payload(
                    **status_payload,
                    phase="simulation_running",
                    process_id=process.pid,
                    log_path=str(log_path),
                    elapsed_seconds=round(elapsed, 2),
                    max_runtime_seconds=round(max_runtime_seconds, 2),
                ),
            )
            if exit_code is not None:
                return int(exit_code), elapsed, False
            if max_runtime_seconds > 0 and elapsed >= max_runtime_seconds:
                process.terminate()
                try:
                    exit_code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    exit_code = process.wait(timeout=5)
                return int(exit_code) if exit_code is not None else 124, elapsed, True
            time.sleep(max(float(heartbeat_seconds), 1.0))


def run_forever() -> None:
    profile = load_routine_profile(os.environ)
    base_url = str(profile.get("base_url") or "http://127.0.0.1:18021").strip()
    actor = str(os.environ.get("MIM_TRAINING_PROGRESS_ACTOR", "workspace")).strip() or "workspace"
    source = str(os.environ.get("MIM_TRAINING_PROGRESS_SOURCE", "objective173")).strip() or "objective173"
    seed_base = _env_int("MIM_TRAINING_SEED_BASE", 20260317)
    pause_seconds = _env_float("MIM_TRAINING_PAUSE_SECONDS", 15.0)
    heartbeat_seconds = _env_float("MIM_TRAINING_HEARTBEAT_SECONDS", 20.0)
    reset_if_not_running = _env_bool("MIM_TRAINING_RESET_PROGRESS_IF_NOT_RUNNING", True)
    status_path = Path(os.environ.get("MIM_TRAINING_STATUS_ARTIFACT", str(profile.get("status_path") or DEFAULT_STATUS_ARTIFACT))).expanduser().resolve()
    summary_report_path = Path(os.environ.get("MIM_TRAINING_SUMMARY_REPORT", str(profile.get("summary_report_path") or DEFAULT_SUMMARY_REPORT))).expanduser().resolve()
    conversation_report_path = Path(os.environ.get("MIM_TRAINING_CONVERSATION_REPORT", str(DEFAULT_CONVERSATION_REPORT))).expanduser().resolve()
    action_report_path = Path(os.environ.get("MIM_TRAINING_ACTION_REPORT", str(DEFAULT_ACTION_REPORT))).expanduser().resolve()
    log_dir = Path(os.environ.get("MIM_TRAINING_LOG_DIR", str(DEFAULT_LOG_DIR))).expanduser().resolve()

    while True:
        cycle = _status_cycle(status_path) + 1
        previous_status = _read_json(status_path)
        previous_summary = _read_json(summary_report_path)
        cycle_plan = build_next_cycle_plan(
            profile=profile,
            previous_status=previous_status,
            previous_summary=previous_summary,
        )
        cycle_target_conversations = int(cycle_plan.get("target_conversations") or profile.get("default_conversations") or 320)
        progress, progress_action = ensure_progress_ready(
            base_url=base_url,
            actor=actor,
            source=source,
            reset_if_not_running=reset_if_not_running,
        )
        active_slice_id = str(progress.get("active_slice_id") or "").strip()
        cycle_seed = seed_base + cycle
        cycle_log_path = log_dir / f"cycle_{cycle:04d}.log"
        status_base = {
            "status": "running",
            "cycle": cycle,
            "active_slice_id": active_slice_id,
            "progress_status": str(progress.get("status") or "").strip(),
            "progress_action": progress_action,
            "base_url": base_url,
            "summary_report": str(summary_report_path),
            "conversation_report": str(conversation_report_path),
            "action_report": str(action_report_path),
            "target_conversations": cycle_target_conversations,
            "seed": cycle_seed,
            "profile": profile,
            "cycle_plan": cycle_plan,
        }
        _write_json(status_path, _build_status_payload(**status_base, phase="cycle_preparing"))
        _wait_for_runtime_ready(
            base_url=base_url,
            status_path=status_path,
            status_payload=status_base,
            heartbeat_seconds=heartbeat_seconds,
        )

        env = os.environ.copy()
        env.update(
            {
                "MIM_TEST_BASE_URL": base_url,
                "TARGET_CONVERSATIONS": str(cycle_target_conversations),
                "REQUEST_TIMEOUT_SECONDS": str(int(cycle_plan.get("request_timeout_seconds") or profile.get("request_timeout_seconds") or 90)),
                "SEED": str(cycle_seed),
                "REPORT_DIR": str(summary_report_path.parent),
            }
        )
        command = ["/usr/bin/env", "bash", str(SIMULATION_SCRIPT)]
        exit_code, elapsed_seconds, timed_out = _run_simulation_process(
            command=command,
            env=env,
            log_path=cycle_log_path,
            status_path=status_path,
            status_payload=status_base,
            heartbeat_seconds=heartbeat_seconds,
            max_runtime_seconds=float(cycle_plan.get("max_cycle_seconds") or profile.get("max_cycle_seconds") or 0),
        )

        evaluation_inputs = collect_evaluation_inputs(
            conversation_report_path=conversation_report_path,
            training_summary_path=summary_report_path,
        )
        proof_summary = build_proof_summary(cycle=cycle, evaluation_inputs=evaluation_inputs)
        evaluation_metadata = {
            "continuous_training": True,
            "cycle": cycle,
            "seed": cycle_seed,
            "run_exit_code": exit_code,
            "elapsed_seconds": round(elapsed_seconds, 2),
            "timed_out": bool(timed_out),
            "summary_report": str(summary_report_path),
            "conversation_report": str(conversation_report_path),
            "action_report": str(action_report_path),
            "log_path": str(cycle_log_path),
        }
        evaluation_status = 0
        evaluation_payload: dict[str, Any] = {}
        if active_slice_id:
            evaluation_status, evaluation_payload = evaluate_progress(
                base_url=base_url,
                actor=actor,
                source=source,
                slice_id=active_slice_id,
                metrics_json=evaluation_inputs["metrics_json"],
                failure_tags=evaluation_inputs["failure_tags"],
                proof_summary=proof_summary,
                discovered_skill_candidates=evaluation_inputs["discovered_skill_candidates"],
                metadata_json=evaluation_metadata,
            )
            if evaluation_status == 422:
                refreshed_progress, _ = ensure_progress_ready(
                    base_url=base_url,
                    actor=actor,
                    source=source,
                    reset_if_not_running=False,
                )
                refreshed_slice_id = str(refreshed_progress.get("active_slice_id") or "").strip()
                if refreshed_slice_id:
                    evaluation_status, evaluation_payload = evaluate_progress(
                        base_url=base_url,
                        actor=actor,
                        source=source,
                        slice_id=refreshed_slice_id,
                        metrics_json=evaluation_inputs["metrics_json"],
                        failure_tags=evaluation_inputs["failure_tags"],
                        proof_summary=proof_summary,
                        discovered_skill_candidates=evaluation_inputs["discovered_skill_candidates"],
                        metadata_json={**evaluation_metadata, "retry_after_active_slice_refresh": True},
                    )
                    active_slice_id = refreshed_slice_id

        next_cycle_plan = build_next_cycle_plan(
            profile=profile,
            previous_status={
                **status_base,
                "elapsed_seconds": round(elapsed_seconds, 2),
                "run_exit_code": exit_code,
            },
            previous_summary=_read_json(summary_report_path),
        )

        _write_json(
            status_path,
            _build_status_payload(
                **status_base,
                active_slice_id=active_slice_id,
                phase="cycle_complete",
                run_exit_code=exit_code,
                elapsed_seconds=round(elapsed_seconds, 2),
                log_path=str(cycle_log_path),
                timed_out=bool(timed_out),
                proof_summary=proof_summary,
                evaluation_status_code=evaluation_status,
                evaluation_outcome=str(evaluation_payload.get("outcome") or "").strip(),
                evaluation=evaluation_payload,
                metrics_json=evaluation_inputs["metrics_json"],
                failure_tags=evaluation_inputs["failure_tags"],
                top_failures=evaluation_inputs["top_failures"],
                discovered_skill_candidates=evaluation_inputs["discovered_skill_candidates"],
                next_cycle_plan=next_cycle_plan,
            ),
        )
        _sleep_with_heartbeat(
            seconds=pause_seconds,
            status_path=status_path,
            payload={
                **status_base,
                "active_slice_id": active_slice_id,
                "last_run_exit_code": exit_code,
                "last_evaluation_outcome": str(evaluation_payload.get("outcome") or "").strip(),
            },
            heartbeat_seconds=heartbeat_seconds,
        )


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        sys.exit(130)