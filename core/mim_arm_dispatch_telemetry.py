from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


LATEST_ARTIFACT_NAME = "MIM_ARM_DISPATCH_TELEMETRY.latest.json"
RECORD_DIRECTORY_NAME = "mim_arm_dispatch_telemetry"
TASK_ACK_ARTIFACT_NAME = "TOD_MIM_TASK_ACK.latest.json"
TASK_RESULT_ARTIFACT_NAME = "TOD_MIM_TASK_RESULT.latest.json"
PUBLICATION_BOUNDARY_ARTIFACT_NAME = "MIM_TOD_PUBLICATION_BOUNDARY.latest.json"


def _json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return _json_dict(json.loads(path.read_text(encoding="utf-8-sig")))
    except Exception:
        return {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def latest_dispatch_telemetry_path(shared_root: Path) -> Path:
    return shared_root / LATEST_ARTIFACT_NAME


def dispatch_telemetry_record_dir(shared_root: Path) -> Path:
    return shared_root / RECORD_DIRECTORY_NAME


def dispatch_telemetry_record_path(shared_root: Path, request_id: str) -> Path:
    normalized = str(request_id or "").strip()
    if not normalized:
        raise ValueError("request_id is required for dispatch telemetry records")
    return dispatch_telemetry_record_dir(shared_root) / f"{normalized}.json"


def _extract_identity(record: dict[str, Any]) -> tuple[str, str, str]:
    request_id = str(record.get("request_id") or "").strip()
    task_id = str(record.get("task_id") or request_id).strip()
    correlation_id = str(record.get("correlation_id") or "").strip()
    return request_id, task_id, correlation_id


def _match_payload(payload: dict[str, Any], *, request_id: str, task_id: str, correlation_id: str) -> tuple[bool, list[str]]:
    ids = {value for value in (request_id, task_id, correlation_id) if value}
    if not ids:
        return False, []

    matched_fields: list[str] = []
    for field_name in ("request_id", "task_id", "correlation_id", "task"):
        value = str(payload.get(field_name) or "").strip()
        if value and value in ids:
            matched_fields.append(field_name)

    bridge_runtime = _json_dict(payload.get("bridge_runtime"))
    current_processing = _json_dict(bridge_runtime.get("current_processing"))
    for field_name in ("request_id", "task_id", "correlation_id"):
        value = str(current_processing.get(field_name) or "").strip()
        if value and value in ids:
            matched_fields.append(f"bridge_runtime.current_processing.{field_name}")

    for field_name in ("last_request_id", "last_task_id", "last_correlation_id"):
        value = str(payload.get(field_name) or "").strip()
        if value and value in ids:
            matched_fields.append(field_name)

    return bool(matched_fields), matched_fields


def _extract_timestamp(payload: dict[str, Any], candidates: list[str]) -> str:
    for field_name in candidates:
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    bridge_runtime = _json_dict(payload.get("bridge_runtime"))
    current_processing = _json_dict(bridge_runtime.get("current_processing"))
    for field_name in candidates:
        value = current_processing.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _nested_timestamp(payload: dict[str, Any], *path: str) -> str:
    current: Any = payload
    for part in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(part)
    return current.strip() if isinstance(current, str) and current.strip() else ""


def _feedback_timestamp(payload: dict[str, Any], primary_field: str) -> str:
    return (
        _nested_timestamp(payload, "executor_timestamps", primary_field)
        or _extract_timestamp(payload, [primary_field, "timestamp"])
    )


def _upsert_evidence_source(record: dict[str, Any], source: dict[str, Any]) -> None:
    key = str(source.get("kind") or "").strip()
    if not key:
        return
    sources = record.get("evidence_sources")
    evidence_sources = [item for item in sources if isinstance(item, dict)] if isinstance(sources, list) else []
    replaced = False
    for index, item in enumerate(evidence_sources):
        if str(item.get("kind") or "").strip() == key:
            evidence_sources[index] = {**item, **_json_safe(source)}
            replaced = True
            break
    if not replaced:
        evidence_sources.append(_json_safe(source))
    record["evidence_sources"] = evidence_sources


def _load_boundary_evidence(shared_root: Path, *, request_id: str, task_id: str) -> dict[str, Any]:
    payload = _read_json(shared_root / PUBLICATION_BOUNDARY_ARTIFACT_NAME)
    if not payload:
        return {}
    remote_request = _json_dict(payload.get("remote_request"))
    remote_trigger = _json_dict(payload.get("remote_trigger"))
    request_match = str(remote_request.get("request_id") or remote_request.get("task_id") or "").strip() in {request_id, task_id}
    trigger_match = str(remote_trigger.get("request_id") or remote_trigger.get("task_id") or "").strip() in {request_id, task_id}
    if not request_match and not trigger_match:
        return {}
    return {
        "kind": "publication_boundary",
        "role": "remote_dispatch_authority",
        "path": str((shared_root / PUBLICATION_BOUNDARY_ARTIFACT_NAME).resolve()),
        "matched": bool(request_match and trigger_match),
        "observed_at": str(remote_request.get("generated_at") or remote_trigger.get("generated_at") or "").strip(),
        "request_alignment": _json_dict(payload.get("request_alignment")),
        "trigger_alignment": _json_dict(payload.get("trigger_alignment")),
    }


def _result_reason(payload: dict[str, Any]) -> str:
    for field_name in ("result_reason", "reason", "runtime_outcome", "outcome", "status"):
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    last_result = _json_dict(_json_dict(payload.get("bridge_runtime")).get("last_result"))
    for field_name in ("reason", "runtime_outcome", "status"):
        value = last_result.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _derive_completion_status(payload: dict[str, Any], fallback_status: str = "") -> str:
    raw = " ".join(
        str(payload.get(field_name) or "").strip().lower()
        for field_name in ("completion_status", "status", "runtime_outcome", "outcome", "result_status")
    ).strip()
    raw = raw or str(fallback_status or "").strip().lower()
    if any(token in raw for token in ("success", "succeeded", "completed", "complete", "ok")):
        return "completed"
    if any(token in raw for token in ("fail", "error", "abort", "cancel", "timeout")):
        return "failed"
    if any(token in raw for token in ("progress", "running", "received", "acked", "accepted")):
        return "in_progress"
    return "pending"


def load_dispatch_telemetry_record(shared_root: Path, request_id: str) -> dict[str, Any]:
    if not str(request_id or "").strip():
        return {}
    return _read_json(dispatch_telemetry_record_path(shared_root, request_id))


def load_latest_dispatch_telemetry(shared_root: Path) -> dict[str, Any]:
    return _read_json(latest_dispatch_telemetry_path(shared_root))


def record_dispatch_telemetry_from_publish(
    *,
    shared_root: Path,
    execution_id: int,
    capability_name: str,
    execution_lane: str,
    request_payload: dict[str, Any],
    trigger_payload: dict[str, Any],
    request_path: Path,
    trigger_path: Path,
    remote_publish: dict[str, Any],
) -> dict[str, Any]:
    request_id = str(request_payload.get("request_id") or "").strip()
    task_id = str(trigger_payload.get("task_id") or request_id).strip()
    correlation_id = str(request_payload.get("correlation_id") or trigger_payload.get("correlation_id") or "").strip()
    dispatch_timestamp = str(request_payload.get("generated_at") or request_payload.get("emitted_at") or "").strip()

    dispatch_status = "published_local"
    if bool(remote_publish.get("succeeded")):
        dispatch_status = "published_remote"
    elif bool(remote_publish.get("attempted")):
        dispatch_status = "published_local_remote_sync_failed"

    record = {
        "contract": "mim_arm_dispatch_telemetry_v1",
        "surface": "dispatch_authority",
        "recorded_at": dispatch_timestamp,
        "request_id": request_id,
        "task_id": task_id,
        "correlation_id": correlation_id,
        "execution_id": execution_id,
        "capability_name": str(capability_name or "").strip(),
        "execution_lane": str(execution_lane or "tod").strip() or "tod",
        "command_name": str(request_payload.get("action") or request_payload.get("command_name") or "").strip(),
        "dispatch_timestamp": dispatch_timestamp,
        "host_received_timestamp": "",
        "host_completed_timestamp": "",
        "dispatch_status": dispatch_status,
        "completion_status": "pending",
        "result_reason": "",
        "evidence_sources": [],
    }
    _upsert_evidence_source(
        record,
        {
            "kind": "request_artifact",
            "role": "dispatch_request",
            "path": str(request_path.resolve()),
            "observed_at": dispatch_timestamp,
        },
    )
    _upsert_evidence_source(
        record,
        {
            "kind": "trigger_artifact",
            "role": "dispatch_trigger",
            "path": str(trigger_path.resolve()),
            "observed_at": str(trigger_payload.get("generated_at") or trigger_payload.get("emitted_at") or dispatch_timestamp).strip(),
        },
    )
    _upsert_evidence_source(
        record,
        {
            "kind": "feedback_endpoint",
            "role": "executor_feedback",
            "endpoint": str(request_payload.get("feedback_endpoint") or "").strip(),
            "observed_at": dispatch_timestamp,
        },
    )
    _upsert_evidence_source(
        record,
        {
            "kind": "handoff_endpoint",
            "role": "executor_handoff",
            "endpoint": str(request_payload.get("handoff_endpoint") or "").strip(),
            "observed_at": dispatch_timestamp,
        },
    )
    if bool(remote_publish.get("attempted")):
        _upsert_evidence_source(
            record,
            {
                "kind": "remote_publish",
                "role": "remote_dispatch_sync",
                "matched": bool(remote_publish.get("succeeded")),
                "returncode": remote_publish.get("returncode"),
                "stdout": str(remote_publish.get("stdout") or "").strip(),
                "stderr": str(remote_publish.get("stderr") or "").strip(),
                "observed_at": dispatch_timestamp,
            },
        )

    boundary_evidence = _load_boundary_evidence(shared_root, request_id=request_id, task_id=task_id)
    if boundary_evidence:
        _upsert_evidence_source(record, boundary_evidence)

    record_path = dispatch_telemetry_record_path(shared_root, request_id)
    _write_json(record_path, record)
    latest_payload = {**record, "record_path": str(record_path.resolve())}
    _write_json(latest_dispatch_telemetry_path(shared_root), latest_payload)
    return latest_payload


def refresh_dispatch_telemetry_record(
    shared_root: Path,
    *,
    request_id: str = "",
) -> dict[str, Any]:
    record = load_dispatch_telemetry_record(shared_root, request_id) if str(request_id or "").strip() else load_latest_dispatch_telemetry(shared_root)
    if not record:
        return {}

    request_id_value, task_id_value, correlation_id_value = _extract_identity(record)
    if not request_id_value:
        return record

    changed = False
    ack_payload = _read_json(shared_root / TASK_ACK_ARTIFACT_NAME)
    ack_matched, ack_fields = _match_payload(
        ack_payload,
        request_id=request_id_value,
        task_id=task_id_value,
        correlation_id=correlation_id_value,
    )
    if ack_matched:
        ack_timestamp = _extract_timestamp(
            ack_payload,
            ["host_received_timestamp", "acknowledged_at", "accepted_at", "received_at", "generated_at", "emitted_at"],
        )
        if ack_timestamp and str(record.get("host_received_timestamp") or "").strip() != ack_timestamp:
            record["host_received_timestamp"] = ack_timestamp
            changed = True
        if str(record.get("dispatch_status") or "") != "host_received":
            record["dispatch_status"] = "host_received"
            changed = True
        _upsert_evidence_source(
            record,
            {
                "kind": "task_ack_artifact",
                "role": "host_received",
                "path": str((shared_root / TASK_ACK_ARTIFACT_NAME).resolve()),
                "matched": True,
                "matched_fields": ack_fields,
                "observed_at": ack_timestamp,
            },
        )

    result_payload = _read_json(shared_root / TASK_RESULT_ARTIFACT_NAME)
    result_matched, result_fields = _match_payload(
        result_payload,
        request_id=request_id_value,
        task_id=task_id_value,
        correlation_id=correlation_id_value,
    )
    if result_matched:
        completed_timestamp = _extract_timestamp(
            result_payload,
            ["host_completed_timestamp", "completed_at", "finished_at", "resolved_at", "generated_at", "emitted_at"],
        )
        if completed_timestamp and str(record.get("host_completed_timestamp") or "").strip() != completed_timestamp:
            record["host_completed_timestamp"] = completed_timestamp
            changed = True
        completion_status = _derive_completion_status(result_payload)
        if str(record.get("completion_status") or "").strip() != completion_status:
            record["completion_status"] = completion_status
            changed = True
        dispatch_status = "completed" if completion_status == "completed" else "failed" if completion_status == "failed" else "host_received"
        if str(record.get("dispatch_status") or "").strip() != dispatch_status:
            record["dispatch_status"] = dispatch_status
            changed = True
        reason = _result_reason(result_payload)
        if reason and str(record.get("result_reason") or "").strip() != reason:
            record["result_reason"] = reason
            changed = True
        _upsert_evidence_source(
            record,
            {
                "kind": "task_result_artifact",
                "role": "host_completed",
                "path": str((shared_root / TASK_RESULT_ARTIFACT_NAME).resolve()),
                "matched": True,
                "matched_fields": result_fields,
                "observed_at": completed_timestamp,
            },
        )

    boundary_evidence = _load_boundary_evidence(shared_root, request_id=request_id_value, task_id=task_id_value)
    if boundary_evidence:
        _upsert_evidence_source(record, boundary_evidence)

    record_path = dispatch_telemetry_record_path(shared_root, request_id_value)
    if changed or not str(record.get("record_path") or "").strip():
        _write_json(record_path, record)
        latest_payload = {**record, "record_path": str(record_path.resolve())}
        _write_json(latest_dispatch_telemetry_path(shared_root), latest_payload)
        return latest_payload
    return {**record, "record_path": str(record_path.resolve())}


def update_dispatch_telemetry_from_feedback(
    *,
    shared_root: Path,
    execution: Any,
    feedback_status: str,
    resolved_reason: str,
    runtime_outcome: str,
    correlation_json: dict[str, Any],
    feedback_json: dict[str, Any],
    execution_truth: dict[str, Any],
) -> dict[str, Any]:
    existing_feedback = execution.feedback_json if isinstance(getattr(execution, "feedback_json", {}), dict) else {}
    bridge_publication = _json_dict(existing_feedback.get("tod_bridge_publication"))
    request_id = str(
        bridge_publication.get("request_id")
        or correlation_json.get("request_id")
        or feedback_json.get("request_id")
        or ""
    ).strip()
    if not request_id:
        return {}

    record = refresh_dispatch_telemetry_record(shared_root, request_id=request_id)
    if not record:
        return {}

    feedback_received_timestamp = _feedback_timestamp(feedback_json, "host_received_timestamp")
    feedback_completed_timestamp = _feedback_timestamp(feedback_json, "host_completed_timestamp")
    correlation_received_timestamp = _feedback_timestamp(correlation_json, "host_received_timestamp")
    correlation_completed_timestamp = _feedback_timestamp(correlation_json, "host_completed_timestamp")
    event_timestamp = str(
        feedback_completed_timestamp
        or feedback_received_timestamp
        or correlation_completed_timestamp
        or correlation_received_timestamp
        or execution_truth.get("published_at")
        or ""
    ).strip()
    feedback_status_value = str(feedback_status or "").strip().lower()
    completion_status = _derive_completion_status(
        {
            "status": feedback_status,
            "runtime_outcome": runtime_outcome,
            **_json_dict(feedback_json),
        },
        fallback_status=feedback_status,
    )

    if completion_status == "in_progress" and not str(record.get("host_received_timestamp") or "").strip():
        record["host_received_timestamp"] = str(
            feedback_received_timestamp
            or correlation_received_timestamp
            or event_timestamp
        ).strip()
        record["dispatch_status"] = "host_received"
    elif completion_status in {"completed", "failed"}:
        if feedback_completed_timestamp or correlation_completed_timestamp or event_timestamp:
            record["host_completed_timestamp"] = str(
                feedback_completed_timestamp
                or correlation_completed_timestamp
                or event_timestamp
            ).strip()
        if not str(record.get("host_received_timestamp") or "").strip():
            record["host_received_timestamp"] = str(
                feedback_received_timestamp
                or correlation_received_timestamp
                or event_timestamp
            ).strip()
        record["dispatch_status"] = "completed" if completion_status == "completed" else "failed"
        record["completion_status"] = completion_status

    if completion_status == "pending" and feedback_status_value in {"dispatched", "dispatching", "accepted", "acknowledged", "received", "running", "in_progress"}:
        record["completion_status"] = "in_progress"
        if event_timestamp and not str(record.get("host_received_timestamp") or "").strip():
            record["host_received_timestamp"] = str(
                feedback_received_timestamp
                or correlation_received_timestamp
                or event_timestamp
            ).strip()
        record["dispatch_status"] = "host_received"

    if resolved_reason:
        record["result_reason"] = str(resolved_reason).strip()
    elif runtime_outcome:
        record["result_reason"] = str(runtime_outcome).strip()

    _upsert_evidence_source(
        record,
        {
            "kind": "feedback_api_update",
            "role": "executor_feedback",
            "matched": True,
            "status": str(feedback_status or "").strip(),
            "runtime_outcome": str(runtime_outcome or "").strip(),
            "reason": str(resolved_reason or "").strip(),
            "observed_at": event_timestamp,
        },
    )
    record_path = dispatch_telemetry_record_path(shared_root, request_id)
    _write_json(record_path, record)
    latest_payload = {**record, "record_path": str(record_path.resolve())}
    _write_json(latest_dispatch_telemetry_path(shared_root), latest_payload)
    return latest_payload