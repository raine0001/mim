from __future__ import annotations

from datetime import datetime, timezone


def _safe_float(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def execution_truth_scope_refs(row: object) -> set[str]:
    refs: set[str] = set()
    arguments = getattr(row, "arguments_json", {})
    feedback = getattr(row, "feedback_json", {})
    correlation = (
        feedback.get("correlation_json", {})
        if isinstance(feedback.get("correlation_json", {}), dict)
        else {}
    ) if isinstance(feedback, dict) else {}

    def _collect(value: object) -> None:
        text = str(value or "").strip()
        if text:
            refs.add(text)

    for payload in (arguments, feedback, correlation):
        if not isinstance(payload, dict):
            continue
        for key in ("managed_scope", "target_scope", "scope", "zone", "scan_area"):
            _collect(payload.get(key))

    observations = (
        feedback.get("observations", [])
        if isinstance(feedback, dict) and isinstance(feedback.get("observations", []), list)
        else []
    )
    for item in observations:
        if not isinstance(item, dict):
            continue
        _collect(item.get("zone"))

    return refs


def execution_truth_scope_matches(*, row: object, managed_scope: str) -> bool:
    scope = str(managed_scope or "").strip() or "global"
    if scope == "global":
        return True
    return scope in execution_truth_scope_refs(row)


def execution_truth_freshness(
    summary: dict, *, decay_window_hours: int = 24
) -> dict:
    if not isinstance(summary, dict):
        return {
            "latest_published_at": None,
            "latest_age_seconds": None,
            "freshness_weight": 0.0,
            "decay_window_hours": int(max(1, decay_window_hours)),
        }

    latest: datetime | None = None
    recent_executions = (
        summary.get("recent_executions", [])
        if isinstance(summary.get("recent_executions", []), list)
        else []
    )
    for item in recent_executions:
        if not isinstance(item, dict):
            continue
        published_at = _parse_timestamp(item.get("published_at"))
        if published_at is None:
            continue
        if latest is None or published_at > latest:
            latest = published_at

    if latest is None:
        return {
            "latest_published_at": None,
            "latest_age_seconds": None,
            "freshness_weight": 0.0,
            "decay_window_hours": int(max(1, decay_window_hours)),
        }

    now = datetime.now(timezone.utc)
    age_seconds = max(0.0, (now - latest).total_seconds())
    window_seconds = float(max(1, int(decay_window_hours)) * 3600)
    freshness_weight = max(0.0, min(1.0, 1.0 - (age_seconds / window_seconds)))
    return {
        "latest_published_at": latest.isoformat(),
        "latest_age_seconds": round(age_seconds, 6),
        "freshness_weight": round(freshness_weight, 6),
        "decay_window_hours": int(max(1, decay_window_hours)),
    }


def canonicalize_execution_truth(
    *,
    execution_id: int,
    capability_name: str,
    payload: dict,
    runtime_outcome: str = "",
) -> dict:
    expected_duration_ms = payload.get("expected_duration_ms")
    actual_duration_ms = payload.get("actual_duration_ms")
    duration_delta_ratio = payload.get("duration_delta_ratio")

    if duration_delta_ratio is None and expected_duration_ms not in {None, ""}:
        expected_value = _safe_float(expected_duration_ms)
        actual_value = _safe_float(actual_duration_ms)
        if expected_value > 0:
            duration_delta_ratio = (actual_value - expected_value) / expected_value

    truth_runtime_outcome = (
        str(payload.get("runtime_outcome") or runtime_outcome or "").strip().lower()
    )
    truth = {
        "contract": "execution_truth_v1",
        "execution_id": int(payload.get("execution_id") or execution_id),
        "capability_name": str(
            payload.get("capability_name") or capability_name
        ).strip(),
        "expected_duration_ms": (
            _safe_int(expected_duration_ms)
            if expected_duration_ms not in {None, ""}
            else None
        ),
        "actual_duration_ms": (
            _safe_int(actual_duration_ms)
            if actual_duration_ms not in {None, ""}
            else None
        ),
        "duration_delta_ratio": (
            round(_safe_float(duration_delta_ratio), 6)
            if duration_delta_ratio is not None
            else None
        ),
        "retry_count": max(0, _safe_int(payload.get("retry_count"), default=0)),
        "fallback_used": bool(payload.get("fallback_used", False)),
        "runtime_outcome": truth_runtime_outcome,
        "environment_shift_detected": bool(
            payload.get("environment_shift_detected", False)
        ),
        "simulation_match_status": str(
            payload.get("simulation_match_status") or "unknown"
        )
        .strip()
        .lower()
        or "unknown",
        "truth_confidence": round(
            max(
                0.0, min(1.0, _safe_float(payload.get("truth_confidence"), default=0.0))
            ),
            6,
        ),
        "published_at": str(payload.get("published_at") or ""),
    }
    return truth


def derive_execution_truth_signals(truth: dict) -> list[dict]:
    if (
        not isinstance(truth, dict)
        or str(truth.get("contract", "")).strip() != "execution_truth_v1"
    ):
        return []

    capability_name = str(truth.get("capability_name", "unknown")).strip() or "unknown"
    published_at = str(truth.get("published_at", "")).strip()
    truth_confidence = round(
        max(0.0, min(1.0, _safe_float(truth.get("truth_confidence"), default=0.0))), 6
    )
    execution_id = _safe_int(truth.get("execution_id"), default=0)
    duration_delta_ratio = truth.get("duration_delta_ratio")
    ratio_value = (
        _safe_float(duration_delta_ratio, default=0.0)
        if duration_delta_ratio is not None
        else None
    )
    retry_count = max(0, _safe_int(truth.get("retry_count"), default=0))
    runtime_outcome = str(truth.get("runtime_outcome", "")).strip().lower()
    simulation_match_status = (
        str(truth.get("simulation_match_status", "unknown")).strip().lower()
        or "unknown"
    )

    signals: list[dict] = []

    if ratio_value is not None and ratio_value >= 0.2:
        signals.append(
            {
                "signal_type": "execution_slower_than_expected",
                "target_scope": capability_name,
                "execution_id": execution_id,
                "severity": round(min(1.0, max(0.2, ratio_value)), 6),
                "confidence": truth_confidence,
                "source": "execution_truth_v1",
                "runtime_outcome": runtime_outcome,
                "observed_at": published_at,
                "metadata_json": {
                    "duration_delta_ratio": round(ratio_value, 6),
                    "expected_duration_ms": truth.get("expected_duration_ms"),
                    "actual_duration_ms": truth.get("actual_duration_ms"),
                },
            }
        )

    if retry_count > 0:
        signals.append(
            {
                "signal_type": "retry_instability_detected",
                "target_scope": capability_name,
                "execution_id": execution_id,
                "severity": round(min(1.0, 0.25 + (retry_count * 0.15)), 6),
                "confidence": truth_confidence,
                "source": "execution_truth_v1",
                "runtime_outcome": runtime_outcome,
                "observed_at": published_at,
                "metadata_json": {
                    "retry_count": retry_count,
                },
            }
        )

    if bool(truth.get("fallback_used", False)):
        signals.append(
            {
                "signal_type": "fallback_path_used",
                "target_scope": capability_name,
                "execution_id": execution_id,
                "severity": 0.7,
                "confidence": truth_confidence,
                "source": "execution_truth_v1",
                "runtime_outcome": runtime_outcome,
                "observed_at": published_at,
                "metadata_json": {
                    "fallback_used": True,
                },
            }
        )

    if simulation_match_status in {"partial_match", "mismatch"}:
        signals.append(
            {
                "signal_type": "simulation_reality_mismatch",
                "target_scope": capability_name,
                "execution_id": execution_id,
                "severity": 0.55
                if simulation_match_status == "partial_match"
                else 0.85,
                "confidence": truth_confidence,
                "source": "execution_truth_v1",
                "runtime_outcome": runtime_outcome,
                "observed_at": published_at,
                "metadata_json": {
                    "simulation_match_status": simulation_match_status,
                },
            }
        )

    if bool(truth.get("environment_shift_detected", False)):
        signals.append(
            {
                "signal_type": "environment_shift_during_execution",
                "target_scope": capability_name,
                "execution_id": execution_id,
                "severity": 0.8,
                "confidence": truth_confidence,
                "source": "execution_truth_v1",
                "runtime_outcome": runtime_outcome,
                "observed_at": published_at,
                "metadata_json": {
                    "environment_shift_detected": True,
                },
            }
        )

    return signals


def summarize_execution_truth_signal_types(summary: dict) -> list[str]:
    if not isinstance(summary, dict):
        return []

    seen: set[str] = set()
    signal_types: list[str] = []

    deviation_signals = (
        summary.get("deviation_signals", [])
        if isinstance(summary.get("deviation_signals", []), list)
        else []
    )
    for item in deviation_signals:
        if not isinstance(item, dict):
            continue
        signal_type = str(item.get("signal_type", "")).strip()
        if signal_type and signal_type not in seen:
            seen.add(signal_type)
            signal_types.append(signal_type)

    recent_executions = (
        summary.get("recent_executions", [])
        if isinstance(summary.get("recent_executions", []), list)
        else []
    )
    for item in recent_executions:
        if not isinstance(item, dict):
            continue
        item_signal_types = (
            item.get("signal_types", [])
            if isinstance(item.get("signal_types", []), list)
            else []
        )
        for signal in item_signal_types:
            signal_type = str(signal).strip()
            if signal_type and signal_type not in seen:
                seen.add(signal_type)
                signal_types.append(signal_type)

    return signal_types


def summarize_execution_truth(
    rows: list[object], *, managed_scope: str = "", max_age_hours: int | None = None
) -> dict:
    signals: list[dict] = []
    capabilities: list[str] = []
    recent_executions: list[dict] = []
    scope = str(managed_scope or "").strip()
    now = datetime.now(timezone.utc)

    for row in rows:
        truth = getattr(row, "execution_truth_json", {})
        if (
            not isinstance(truth, dict)
            or str(truth.get("contract", "")).strip() != "execution_truth_v1"
        ):
            continue
        if scope and not execution_truth_scope_matches(row=row, managed_scope=scope):
            continue
        published_at = _parse_timestamp(truth.get("published_at"))
        if max_age_hours is not None and published_at is not None:
            age_seconds = max(0.0, (now - published_at).total_seconds())
            if age_seconds > float(max(1, int(max_age_hours)) * 3600):
                continue
        capability_name = (
            str(getattr(row, "capability_name", "unknown") or "unknown").strip()
            or "unknown"
        )
        capabilities.append(capability_name)
        row_signals = derive_execution_truth_signals(truth)
        signals.extend(row_signals)
        recent_executions.append(
            {
                "execution_id": int(getattr(row, "id", 0) or 0),
                "capability_name": capability_name,
                "runtime_outcome": str(truth.get("runtime_outcome", "")).strip(),
                "truth_confidence": round(
                    _safe_float(truth.get("truth_confidence"), default=0.0), 6
                ),
                "published_at": str(truth.get("published_at", "")).strip(),
                "scope_refs": sorted(execution_truth_scope_refs(row)),
                "signal_types": [
                    str(item.get("signal_type", "")).strip()
                    for item in row_signals
                    if isinstance(item, dict)
                    and str(item.get("signal_type", "")).strip()
                ],
            }
        )

    signal_types = summarize_execution_truth_signal_types(
        {
            "deviation_signals": signals,
            "recent_executions": recent_executions,
        }
    )
    freshness = execution_truth_freshness(
        {"recent_executions": recent_executions},
        decay_window_hours=(max_age_hours if max_age_hours is not None else 24),
    )

    return {
        "execution_count": len(recent_executions),
        "capabilities": sorted({item for item in capabilities if item}),
        "deviation_signal_count": len(signals),
        "deviation_signals": signals[:20],
        "recent_executions": recent_executions[:10],
        "signal_types": signal_types,
        "freshness": freshness,
        "managed_scope": scope or "global",
    }


def build_execution_truth_bridge_projection(
    *,
    rows: list[object],
    generated_at: str,
    source: str,
    max_recent_items: int = 10,
) -> dict:
    summary = summarize_execution_truth(rows)
    projected_rows: list[dict] = []

    for row in rows:
        truth = getattr(row, "execution_truth_json", {})
        if (
            not isinstance(truth, dict)
            or str(truth.get("contract", "")).strip() != "execution_truth_v1"
        ):
            continue
        execution_id = int(getattr(row, "id", 0) or 0)
        projected_rows.append(
            {
                "execution_id": execution_id,
                "capability_name": str(
                    getattr(row, "capability_name", "") or ""
                ).strip(),
                "status": str(getattr(row, "status", "") or "").strip(),
                "reason": str(getattr(row, "reason", "") or "").strip(),
                "execution_truth": truth,
                "deviation_signals": derive_execution_truth_signals(truth),
            }
        )
        if len(projected_rows) >= max_recent_items:
            break

    return {
        "generated_at": generated_at,
        "packet_type": "tod-execution-truth-bridge-v1",
        "contract": "execution_truth_v1",
        "source": source,
        "summary": summary,
        "recent_execution_truth": projected_rows,
    }
