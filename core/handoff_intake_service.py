from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.bounded_action_registry import SUPPORTED_BOUNDED_ACTIONS
from core.config import PROJECT_ROOT
from core.local_broker_boundary import (
    LATEST_BROKER_REQUEST_ARTIFACT,
    LATEST_BROKER_RESULT_ARTIFACT,
    LocalBrokerBoundary,
    build_broker_request_artifact,
    build_broker_result_artifact,
    build_broker_tool_schemas,
    build_handoff_broker_session_context,
    live_openai_broker_configured,
)
from core.local_openai_broker_artifact_worker import (
    consume_broker_request_artifact_with_live_response_async,
)
from core.local_broker_result_artifact_interpretation_worker import (
    persist_broker_result_artifact_interpretation_async,
)
from core.primitive_request_recovery_service import (
    DEFAULT_SHARED_ROOT,
)


DEFAULT_HANDOFF_ROOT = PROJECT_ROOT / "handoff"
INPUT_SCHEMA_VERSION = "mim-handoff-input-v1"
STATUS_SCHEMA_VERSION = "mim-handoff-status-v1"
LATEST_STATUS_ARTIFACT = "HANDOFF_STATUS.latest.json"
LATEST_TASK_ARTIFACT = "HANDOFF_TASK.latest.json"
SUPPORTED_TOD_ACTIONS = SUPPORTED_BOUNDED_ACTIONS
DIRECT_ANSWER_HINTS = (
    "answer",
    "explain",
    "summary",
    "summarize",
    "clarify",
    "recap",
    "what",
    "why",
    "how",
)
CODEX_HINTS = (
    "implement",
    "implementation",
    "code",
    "patch",
    "fix",
    "refactor",
    "edit",
    "modify",
    "change",
    "test",
    "write",
    "build",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _compact_text(value: Any, limit: int = 220) -> str:
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _handoff_directories(handoff_root: Path) -> dict[str, Path]:
    return {
        "root": handoff_root,
        "inbox": handoff_root / "inbox",
        "processing": handoff_root / "processing",
        "done": handoff_root / "done",
        "failed": handoff_root / "failed",
        "status": handoff_root / "status",
    }


def ensure_handoff_directories(*, handoff_root: Path = DEFAULT_HANDOFF_ROOT) -> dict[str, Path]:
    directories = _handoff_directories(handoff_root.expanduser().resolve())
    for path in directories.values():
        path.mkdir(parents=True, exist_ok=True)
    return directories


def _normalize_action_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized


def _normalize_step_list(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(values, start=1):
        if isinstance(item, dict):
            step_id = str(item.get("step_id") or item.get("id") or f"step_{index:03d}").strip()
            summary = _compact_text(item.get("summary") or item.get("detail") or item.get("description") or step_id, 180)
        else:
            step_id = f"step_{index:03d}"
            summary = _compact_text(item, 180)
        normalized.append({"step_id": step_id, "summary": summary})
    return normalized


def _fallback_handoff_id(path: Path) -> str:
    stem = str(path.stem or "handoff").strip().replace(" ", "-")
    return stem or f"handoff-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


def validate_handoff_payload(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required_string_fields = (
        "schema_version",
        "handoff_id",
        "created_at",
        "source",
        "topic",
        "summary",
        "requested_outcome",
        "status",
    )
    for field_name in required_string_fields:
        value = str(payload.get(field_name) or "").strip()
        if not value:
            errors.append(f"missing_or_blank:{field_name}")

    if str(payload.get("schema_version") or "").strip() != INPUT_SCHEMA_VERSION:
        errors.append("unsupported_schema_version")

    list_fields = (
        "constraints",
        "next_bounded_steps",
        "bounded_actions_allowed",
    )
    for field_name in list_fields:
        value = payload.get(field_name)
        if not isinstance(value, list):
            errors.append(f"invalid_type:{field_name}:list_required")

    created_at = str(payload.get("created_at") or "").strip()
    if created_at:
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append("invalid_created_at")

    status_value = str(payload.get("status") or "").strip().lower()
    if status_value and status_value not in {"pending", "ready", "new"}:
        errors.append("invalid_status")

    next_steps = payload.get("next_bounded_steps")
    if isinstance(next_steps, list):
        for item in next_steps:
            if not isinstance(item, (str, dict)):
                errors.append("invalid_type:next_bounded_steps:item")
                break

    return sorted(set(errors))


def _matches_any_hint(payload: dict[str, Any], hints: tuple[str, ...]) -> bool:
    text = " ".join(
        [
            str(payload.get("topic") or ""),
            str(payload.get("summary") or ""),
            str(payload.get("requested_outcome") or ""),
            " ".join(_normalize_string_list(payload.get("constraints"))),
            " ".join(step.get("summary", "") for step in _normalize_step_list(payload.get("next_bounded_steps"))),
        ]
    ).lower()
    return any(
        re.search(rf"(^|[^a-z0-9]){re.escape(hint.lower())}([^a-z0-9]|$)", text)
        for hint in hints
    )


def select_handoff_mode(payload: dict[str, Any]) -> dict[str, Any]:
    for raw_action in _normalize_string_list(payload.get("bounded_actions_allowed")):
        normalized_action = _normalize_action_name(raw_action)
        dispatch = SUPPORTED_TOD_ACTIONS.get(normalized_action)
        if dispatch is not None:
            return {
                "mode": "bounded_tod_dispatch",
                "execution_owner": "tod",
                "assistance_mode": "bounded_tod_dispatch",
                "result_authority": "tod_dispatch_artifacts",
                "active_step_id": "dispatch_bounded_tod",
                "selected_action": normalized_action,
                "dispatch_callable": dispatch,
                "summary": f"Selected one existing bounded TOD action: {normalized_action}.",
            }

    direct_answer_match = _matches_any_hint(payload, DIRECT_ANSWER_HINTS)
    codex_match = _matches_any_hint(payload, CODEX_HINTS)

    if direct_answer_match and not codex_match:
        return {
            "mode": "direct_mim_answer",
            "execution_owner": "mim",
            "assistance_mode": "local_direct_answer",
            "result_authority": "mim_local_response",
            "active_step_id": "answer_directly",
            "selected_action": "direct_mim_answer",
            "summary": "Selected the direct MIM answer path.",
        }

    if codex_match:
        return {
            "mode": "codex_assisted_bounded_implementation",
            "execution_owner": "codex",
            "assistance_mode": "codex_assisted_bounded_implementation",
            "result_authority": "local_task_queue",
            "active_step_id": "queue_codex_bounded_implementation",
            "selected_action": "codex_assisted_bounded_implementation",
            "summary": "Selected the Codex-assisted bounded implementation path.",
        }

    if direct_answer_match or not _normalize_string_list(payload.get("bounded_actions_allowed")):
        return {
            "mode": "direct_mim_answer",
            "execution_owner": "mim",
            "assistance_mode": "local_direct_answer",
            "result_authority": "mim_local_response",
            "active_step_id": "answer_directly",
            "selected_action": "direct_mim_answer",
            "summary": "Selected the direct MIM answer path.",
        }

    return {
        "mode": "blocked",
        "execution_owner": "mim",
        "assistance_mode": "blocked",
        "result_authority": "handoff_policy_block",
        "active_step_id": "blocked",
        "selected_action": "blocked",
        "summary": "Blocked because no valid bounded handling mode matched the handoff artifact.",
    }


def _build_direct_answer(payload: dict[str, Any]) -> str:
    next_steps = _normalize_step_list(payload.get("next_bounded_steps"))
    constraints = _normalize_string_list(payload.get("constraints"))
    summary = _compact_text(payload.get("summary"), 200)
    requested_outcome = _compact_text(payload.get("requested_outcome"), 160)

    parts = [
        f"Handoff summary: {summary}",
        f"Requested outcome: {requested_outcome}",
    ]
    if next_steps:
        parts.append(f"Next bounded step: {next_steps[0].get('summary')}")
    if constraints:
        parts.append("Constraints: " + ", ".join(constraints[:4]))
    return _compact_text(" ".join(parts), 320)


def _build_codex_queue_summary(payload: dict[str, Any], *, task_id: str) -> str:
    next_steps = _normalize_step_list(payload.get("next_bounded_steps"))
    if next_steps:
        return _compact_text(
            f"Queued bounded implementation task {task_id} for Codex-assisted handling. First bounded step: {next_steps[0].get('summary')}",
            320,
        )
    return f"Queued bounded implementation task {task_id} for Codex-assisted handling."


def _build_codex_blocked_summary(
    payload: dict[str, Any],
    *,
    task_id: str,
    broker_response: dict[str, Any],
) -> str:
    reason = str(broker_response.get("reason") or "local_broker_unavailable").strip().replace("_", " ")
    requested_outcome = _compact_text(payload.get("requested_outcome") or payload.get("summary"), 140)
    return _compact_text(
        f"Blocked bounded implementation task {task_id} because the local broker is unavailable ({reason}). Requested outcome: {requested_outcome}",
        320,
    )


def _broker_preparation_requires_blocked_state(broker_preparation: dict[str, Any]) -> bool:
    broker_response = (
        broker_preparation.get("broker_response")
        if isinstance(broker_preparation.get("broker_response"), dict)
        else {}
    )
    response_status = str(broker_response.get("status") or "").strip().lower()
    return response_status in {"not_configured", "failed", "error", "blocked"}


def _build_codex_completion_summary(
    payload: dict[str, Any],
    *,
    broker_preparation: dict[str, Any],
    task_id: str,
) -> str:
    automatic_live_response = broker_preparation.get("automatic_live_response")
    if isinstance(automatic_live_response, dict):
        result_artifact = Path(str(automatic_live_response.get("result_artifact") or "").strip())
        if result_artifact.exists():
            response_payload = _read_json(result_artifact).get("response")
            if isinstance(response_payload, dict):
                output_text = str(response_payload.get("output_text") or "").strip()
                if output_text:
                    return _compact_text(output_text, 320)
    return _compact_text(
        f"Completed bounded implementation task {task_id} through automatic live broker execution for {_compact_text(payload.get('topic'), 120)}.",
        320,
    )


def _automatic_live_completion_ready(broker_preparation: dict[str, Any]) -> bool:
    automatic_live_response = broker_preparation.get("automatic_live_response")
    automatic_live_interpretation = broker_preparation.get("automatic_live_interpretation")
    if not isinstance(automatic_live_response, dict):
        return False
    if not isinstance(automatic_live_interpretation, dict):
        return False
    if str(automatic_live_response.get("status") or "").strip() != "completed":
        return False
    if str(automatic_live_interpretation.get("status") or "").strip() != "completed":
        return False
    result_artifact = Path(str(automatic_live_response.get("result_artifact") or "").strip())
    if not result_artifact.exists():
        return False
    result_payload = _read_json(result_artifact)
    response_payload = result_payload.get("response")
    if not isinstance(response_payload, dict):
        return False
    tool_call_intent = response_payload.get("tool_call_intent")
    executed_result = response_payload.get("executed_result")
    return bool(
        executed_result is not None
        and isinstance(tool_call_intent, dict)
        and str(tool_call_intent.get("execution_state") or "").strip() == "executed"
    )


def _status_paths(handoff_root: Path, handoff_id: str) -> tuple[Path, Path]:
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    return status_dir / f"{handoff_id}.json", status_dir / LATEST_STATUS_ARTIFACT


def _task_paths(handoff_root: Path, handoff_id: str) -> tuple[Path, Path]:
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    return status_dir / f"{handoff_id}.task.json", status_dir / LATEST_TASK_ARTIFACT


def _broker_request_paths(handoff_root: Path, handoff_id: str) -> tuple[Path, Path]:
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    return (
        status_dir / f"{handoff_id}.broker-request.json",
        status_dir / LATEST_BROKER_REQUEST_ARTIFACT,
    )


def _broker_result_paths(handoff_root: Path, handoff_id: str) -> tuple[Path, Path]:
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    return (
        status_dir / f"{handoff_id}.broker-result.json",
        status_dir / LATEST_BROKER_RESULT_ARTIFACT,
    )


def _build_local_task_record(
    *,
    handoff_id: str,
    task_id: str,
    payload: dict[str, Any],
    execution_owner: str,
    assistance_mode: str,
    result_authority: str,
    task_state: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "handoff_id": handoff_id,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "title": _compact_text(f"handoff:{payload.get('topic') or handoff_id}", 200),
        "source": str(payload.get("source") or "").strip(),
        "topic": str(payload.get("topic") or "").strip(),
        "requested_outcome": _compact_text(payload.get("requested_outcome"), 220),
        "summary": _compact_text(payload.get("summary"), 320),
        "execution_owner": execution_owner,
        "assistance_mode": assistance_mode,
        "result_authority": result_authority,
        "state": task_state,
        "next_bounded_steps": _normalize_step_list(payload.get("next_bounded_steps")),
        "dispatch_contract": (
            dict(payload.get("dispatch_contract"))
            if isinstance(payload.get("dispatch_contract"), dict)
            else {}
        ),
    }


def _write_task_artifacts(
    *, handoff_root: Path, handoff_id: str, payload: dict[str, Any]
) -> tuple[Path, Path]:
    task_path, latest_path = _task_paths(handoff_root, handoff_id)
    _write_json(task_path, payload)
    _write_json(latest_path, payload)
    return task_path, latest_path


def _write_broker_request_artifacts(
    *, handoff_root: Path, handoff_id: str, payload: dict[str, Any]
) -> tuple[Path, Path]:
    request_path, latest_path = _broker_request_paths(handoff_root, handoff_id)
    _write_json(request_path, payload)
    _write_json(latest_path, payload)
    return request_path, latest_path


def _write_broker_result_artifacts(
    *, handoff_root: Path, handoff_id: str, payload: dict[str, Any]
) -> tuple[Path, Path]:
    result_path, latest_path = _broker_result_paths(handoff_root, handoff_id)
    _write_json(result_path, payload)
    _write_json(latest_path, payload)
    return result_path, latest_path


def _build_status_payload(
    *,
    handoff_id: str,
    status: str,
    active_step_id: str,
    execution_owner: str,
    assistance_mode: str,
    result_authority: str,
    latest_result: dict[str, Any],
    task_id: str | None,
    source_artifact: Path,
    archived_artifact: Path | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "handoff_id": handoff_id,
        "status": status,
        "active_step_id": active_step_id,
        "updated_at": _utc_now(),
        "execution_owner": execution_owner,
        "assistance_mode": assistance_mode,
        "result_authority": result_authority,
        "latest_result": latest_result,
        "source_artifact": str(source_artifact),
    }
    if task_id is not None:
        payload["task_id"] = str(task_id)
    if archived_artifact is not None:
        payload["archived_artifact"] = str(archived_artifact)
    return payload


def _write_status_artifacts(
    *, handoff_root: Path, handoff_id: str, payload: dict[str, Any]
) -> tuple[Path, Path]:
    status_path, latest_path = _status_paths(handoff_root, handoff_id)
    _write_json(status_path, payload)
    _write_json(latest_path, payload)
    return status_path, latest_path


def normalize_handoff_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    normalized = dict(payload) if isinstance(payload, dict) else {}
    handoff_id = str(normalized.get("handoff_id") or "").strip() or f"handoff-{uuid.uuid4()}"
    topic = _compact_text(
        normalized.get("topic")
        or normalized.get("summary")
        or normalized.get("requested_outcome")
        or handoff_id,
        220,
    )
    summary = _compact_text(
        normalized.get("summary")
        or normalized.get("requested_outcome")
        or topic,
        320,
    )
    requested_outcome = _compact_text(
        normalized.get("requested_outcome") or summary,
        220,
    )
    source = str(normalized.get("source") or "conversation-gateway").strip() or "conversation-gateway"
    status = str(normalized.get("status") or "pending").strip().lower() or "pending"

    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "handoff_id": handoff_id,
        "created_at": str(normalized.get("created_at") or _utc_now()).strip(),
        "source": source,
        "topic": topic,
        "summary": summary,
        "requested_outcome": requested_outcome,
        "constraints": _normalize_string_list(normalized.get("constraints")),
        "next_bounded_steps": _normalize_step_list(normalized.get("next_bounded_steps")),
        "bounded_actions_allowed": _normalize_string_list(
            normalized.get("bounded_actions_allowed")
        ),
        "dispatch_contract": (
            dict(normalized.get("dispatch_contract"))
            if isinstance(normalized.get("dispatch_contract"), dict)
            else {}
        ),
        "status": status,
    }


async def submit_handoff_payload(
    payload: dict[str, Any] | None,
    *,
    handoff_root: Path = DEFAULT_HANDOFF_ROOT,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, Any]:
    directories = ensure_handoff_directories(handoff_root=handoff_root)
    normalized_payload = normalize_handoff_payload(payload)
    inbox_path = directories["inbox"] / f"{normalized_payload['handoff_id']}.json"
    _write_json(inbox_path, normalized_payload)
    return await ingest_one_handoff_artifact(
        handoff_root=directories["root"],
        shared_root=shared_root,
    )


async def ingest_one_handoff_artifact(
    *,
    handoff_root: Path = DEFAULT_HANDOFF_ROOT,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, Any]:
    directories = ensure_handoff_directories(handoff_root=handoff_root)
    inbox_candidates = sorted(directories["inbox"].glob("*.json"))
    if not inbox_candidates:
        return {
            "status": "idle",
            "reason": "no_handoff_artifact_found",
            "handoff_root": str(directories["root"]),
        }

    inbox_path = inbox_candidates[0]
    payload = _read_json(inbox_path)
    handoff_id = str(payload.get("handoff_id") or _fallback_handoff_id(inbox_path)).strip()

    if not payload:
        failed_path = directories["failed"] / inbox_path.name
        inbox_path.replace(failed_path)
        latest_result = {
            "summary": "Blocked because the handoff artifact was not valid JSON.",
            "validation_errors": ["invalid_json"],
        }
        status_payload = _build_status_payload(
            handoff_id=handoff_id,
            status="failed",
            active_step_id="validate_schema",
            execution_owner="mim",
            assistance_mode="blocked",
            result_authority="handoff_policy_block",
            latest_result=latest_result,
            task_id=None,
            source_artifact=inbox_path,
            archived_artifact=failed_path,
        )
        status_path, latest_path = _write_status_artifacts(
            handoff_root=directories["root"], handoff_id=handoff_id, payload=status_payload
        )
        return {
            "handoff_id": handoff_id,
            "mode": "blocked",
            "status": "failed",
            "task_path": "",
            "status_path": str(status_path),
            "latest_status_path": str(latest_path),
            "archived_artifact": str(failed_path),
        }

    validation_errors = validate_handoff_payload(payload)
    if validation_errors:
        failed_path = directories["failed"] / inbox_path.name
        inbox_path.replace(failed_path)
        latest_result = {
            "summary": "Blocked because the handoff artifact schema validation failed.",
            "validation_errors": validation_errors,
        }
        status_payload = _build_status_payload(
            handoff_id=handoff_id,
            status="failed",
            active_step_id="validate_schema",
            execution_owner="mim",
            assistance_mode="blocked",
            result_authority="handoff_policy_block",
            latest_result=latest_result,
            task_id=None,
            source_artifact=inbox_path,
            archived_artifact=failed_path,
        )
        status_path, latest_path = _write_status_artifacts(
            handoff_root=directories["root"], handoff_id=handoff_id, payload=status_payload
        )
        return {
            "handoff_id": handoff_id,
            "mode": "blocked",
            "status": "failed",
            "validation_errors": validation_errors,
            "task_path": "",
            "status_path": str(status_path),
            "latest_status_path": str(latest_path),
            "archived_artifact": str(failed_path),
        }

    processing_path = directories["processing"] / inbox_path.name
    inbox_path.replace(processing_path)

    mode = select_handoff_mode(payload)
    selected_mode = str(mode.get("mode") or "blocked").strip()
    task_id = f"handoff-task-{handoff_id}"

    latest_result: dict[str, Any]
    final_status: str
    archive_dir = directories["done"]
    task_state = "queued"
    broker_preparation: dict[str, Any] | None = None
    execution_owner = str(mode.get("execution_owner") or "mim").strip() or "mim"
    assistance_mode = str(mode.get("assistance_mode") or "blocked").strip() or "blocked"
    result_authority = (
        str(mode.get("result_authority") or "handoff_policy_block").strip()
        or "handoff_policy_block"
    )
    active_step_id = str(mode.get("active_step_id") or "completed").strip() or "completed"

    if selected_mode == "bounded_tod_dispatch":
        dispatch_callable = mode["dispatch_callable"]
        dispatch = dispatch_callable(
            request_id=handoff_id,
            session_key=f"handoff:{handoff_id}",
            content=_compact_text(payload.get("requested_outcome") or payload.get("summary"), 200),
            actor="handoff-artifact",
            shared_root=shared_root,
        )
        latest_result = {
            "summary": str(dispatch.get("result_reason") or mode.get("summary") or "").strip(),
            "selected_mode": selected_mode,
            "selected_action": str(mode.get("selected_action") or "").strip(),
            "request_id": str(dispatch.get("request_id") or "").strip(),
            "dispatch": dispatch,
        }
        task_state = "completed"
        final_status = "completed"
    elif selected_mode == "codex_assisted_bounded_implementation":
        broker_context = build_handoff_broker_session_context(
            handoff_id=handoff_id,
            payload=payload,
        )
        broker_tool_schemas = build_broker_tool_schemas()
        broker_request_artifact = build_broker_request_artifact(
            handoff_id=handoff_id,
            task_id=task_id,
            session_context=broker_context,
            tool_schemas=broker_tool_schemas,
        )
        broker_request_path, latest_broker_request_path = _write_broker_request_artifacts(
            handoff_root=directories["root"],
            handoff_id=handoff_id,
            payload=broker_request_artifact,
        )
        broker_boundary = LocalBrokerBoundary(shared_root=shared_root)
        broker_response = await broker_boundary.invoke(
            session_context=broker_context,
            tool_schemas=broker_tool_schemas,
        )
        broker_result_artifact = build_broker_result_artifact(
            handoff_id=handoff_id,
            task_id=task_id,
            broker_response=broker_response,
        )
        broker_result_path, latest_broker_result_path = _write_broker_result_artifacts(
            handoff_root=directories["root"],
            handoff_id=handoff_id,
            payload=broker_result_artifact,
        )
        broker_preparation = {
            "status": "prepared",
            "session_context": broker_context,
            "tool_schemas": broker_tool_schemas,
            "tool_names": [str(tool.get("name") or "") for tool in broker_tool_schemas],
            "broker_response": broker_response,
            "broker_request_artifact": str(broker_request_path),
            "latest_broker_request_artifact": str(latest_broker_request_path),
            "broker_result_artifact": str(broker_result_path),
            "latest_broker_result_artifact": str(latest_broker_result_path),
        }
        if live_openai_broker_configured():
            try:
                automatic_live_response = await consume_broker_request_artifact_with_live_response_async(
                    request_artifact_path=broker_request_path,
                )
                broker_preparation["automatic_live_response"] = automatic_live_response
                automatic_live_interpretation = await persist_broker_result_artifact_interpretation_async(
                    result_artifact_path=Path(automatic_live_response["result_artifact"]),
                )
                broker_preparation["automatic_live_interpretation"] = automatic_live_interpretation
            except Exception as exc:
                broker_preparation["automatic_live_response"] = {
                    "status": "failed",
                    "reason": _compact_text(exc, 240),
                }
        summary = _build_codex_queue_summary(payload, task_id=task_id)
        if _automatic_live_completion_ready(broker_preparation):
            summary = _build_codex_completion_summary(
                payload,
                broker_preparation=broker_preparation,
                task_id=task_id,
            )
            task_state = "completed"
            final_status = "completed"
            result_authority = "local_broker_result_artifact"
            active_step_id = "automatic_live_broker_completed"
        elif _broker_preparation_requires_blocked_state(broker_preparation):
            summary = _build_codex_blocked_summary(
                payload,
                task_id=task_id,
                broker_response=broker_response,
            )
            task_state = "blocked"
            final_status = "blocked"
            result_authority = "local_broker_unavailable"
            active_step_id = "blocked_local_broker_unavailable"
        else:
            task_state = "queued"
            final_status = "queued"
        latest_result = {
            "summary": summary,
            "selected_mode": selected_mode,
            "selected_action": str(mode.get("selected_action") or "").strip(),
            "next_bounded_steps": _normalize_step_list(payload.get("next_bounded_steps")),
            "broker_preparation": broker_preparation,
        }
    elif selected_mode == "direct_mim_answer":
        latest_result = {
            "summary": _build_direct_answer(payload),
            "selected_mode": selected_mode,
            "selected_action": str(mode.get("selected_action") or "").strip(),
        }
        task_state = "completed"
        final_status = "completed"
    else:
        latest_result = {
            "summary": str(mode.get("summary") or "Blocked because no valid bounded path matched.").strip(),
            "selected_mode": "blocked",
            "selected_action": "blocked",
        }
        task_state = "blocked"
        final_status = "blocked"
        archive_dir = directories["failed"]

    task_record = _build_local_task_record(
        handoff_id=handoff_id,
        task_id=task_id,
        payload=payload,
        execution_owner=execution_owner,
        assistance_mode=assistance_mode,
        result_authority=result_authority,
        task_state=task_state,
    )
    if broker_preparation is not None:
        task_record["broker_preparation"] = broker_preparation
    latest_result["task_id"] = task_record["task_id"]
    task_record["updated_at"] = _utc_now()
    task_path, latest_task_path = _write_task_artifacts(
        handoff_root=directories["root"], handoff_id=handoff_id, payload=task_record
    )

    archived_path = archive_dir / processing_path.name
    processing_path.replace(archived_path)
    status_payload = _build_status_payload(
        handoff_id=handoff_id,
        status=final_status,
        active_step_id=active_step_id,
        execution_owner=execution_owner,
        assistance_mode=assistance_mode,
        result_authority=result_authority,
        latest_result=latest_result,
        task_id=None,
        source_artifact=inbox_path,
        archived_artifact=archived_path,
    )
    status_path, latest_path = _write_status_artifacts(
        handoff_root=directories["root"], handoff_id=handoff_id, payload=status_payload
    )

    return {
        "handoff_id": handoff_id,
        "mode": selected_mode,
        "status": final_status,
        "latest_result": latest_result,
        "latest_result_summary": str(latest_result.get("summary") or "").strip(),
        "task_id": str(task_record["task_id"]),
        "task_path": str(task_path),
        "latest_task_path": str(latest_task_path),
        "status_path": str(status_path),
        "latest_status_path": str(latest_path),
        "archived_artifact": str(archived_path),
    }