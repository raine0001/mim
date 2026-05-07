from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.bounded_action_registry import get_bounded_action
from core.local_broker_boundary import (
    BROKER_REQUEST_SCHEMA_VERSION,
    BROKER_RESULT_SCHEMA_VERSION,
    LATEST_BROKER_RESULT_ARTIFACT,
    build_broker_result_artifact,
    build_broker_tool_schemas,
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _result_paths(*, status_dir: Path, handoff_id: str) -> tuple[Path, Path]:
    return status_dir / f"{handoff_id}.broker-result.json", status_dir / LATEST_BROKER_RESULT_ARTIFACT


def _expected_tool_names() -> list[str]:
    return [str(tool.get("name") or "").strip() for tool in build_broker_tool_schemas()]


def _build_placeholder_tool_call(
    *,
    tool_name: str | None,
    arguments: dict[str, Any] | None,
    available_tools: list[str],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    normalized_tool_name = str(tool_name or "").strip()
    normalized_arguments = dict(arguments or {})
    if not normalized_tool_name:
        return (
            {
                "tool_name": None,
                "arguments": {},
                "note": "No tool call executed. Placeholder only.",
            },
            None,
        )
    if normalized_tool_name not in available_tools:
        raise ValueError("placeholder tool intent must match the fixed bounded tool list")
    if normalized_tool_name == "run_bounded_action":
        action_name = str(normalized_arguments.get("action_name") or "").strip()
        if get_bounded_action(action_name) is None:
            raise ValueError("placeholder bounded action intent must reference one existing bounded action")
    placeholder = {
        "tool_name": normalized_tool_name,
        "arguments": normalized_arguments,
        "note": "Intent placeholder only. The worker did not execute this tool.",
    }
    intent = {
        "tool_name": normalized_tool_name,
        "arguments": normalized_arguments,
        "execution_state": "not_executed",
    }
    return placeholder, intent


def consume_broker_request_artifact(
    *,
    request_artifact_path: Path,
    tool_name: str | None = None,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_request_path = request_artifact_path.expanduser().resolve()
    request_payload = _read_json(resolved_request_path)
    if not request_payload:
        raise ValueError("broker request artifact must be valid JSON")
    if str(request_payload.get("schema_version") or "").strip() != BROKER_REQUEST_SCHEMA_VERSION:
        raise ValueError("broker request artifact must use the local broker request schema")

    handoff_id = str(request_payload.get("handoff_id") or "").strip()
    task_id = str(request_payload.get("task_id") or "").strip()
    if not handoff_id or not task_id:
        raise ValueError("broker request artifact must include handoff_id and task_id")

    expected_tool_names = _expected_tool_names()
    request_tool_names = [str(value or "").strip() for value in request_payload.get("tool_names") or []]
    if request_tool_names != expected_tool_names:
        raise ValueError("broker request artifact must preserve the fixed bounded tool list")

    status_dir = resolved_request_path.parent
    result_path, latest_result_path = _result_paths(status_dir=status_dir, handoff_id=handoff_id)
    if not result_path.exists():
        raise FileNotFoundError("existing broker result artifact not found")

    existing_result_payload = _read_json(result_path)
    if str(existing_result_payload.get("schema_version") or "").strip() != BROKER_RESULT_SCHEMA_VERSION:
        raise ValueError("existing broker result artifact must use the local broker result schema")

    task_linkage = request_payload.get("task_linkage")
    if not isinstance(task_linkage, dict):
        task_linkage = {
            "handoff_id": handoff_id,
            "task_id": task_id,
            "session_id": str(request_payload.get("session_context", {}).get("session_id") or "").strip(),
        }

    tool_call_placeholder, tool_call_intent = _build_placeholder_tool_call(
        tool_name=tool_name,
        arguments=arguments,
        available_tools=request_tool_names,
    )

    placeholder_response = {
        "status": "placeholder_written",
        "available_tools": request_tool_names,
        "model_response_placeholder": {
            "summary": f"Local placeholder broker response prepared for {handoff_id}.",
            "session_id": str(task_linkage.get("session_id") or "").strip(),
            "note": "No live broker client is configured; this is a bounded local placeholder.",
        },
        "tool_call_placeholder": tool_call_placeholder,
        "output_text": "",
        "tool_call_intent": tool_call_intent,
    }

    result_payload = build_broker_result_artifact(
        handoff_id=handoff_id,
        task_id=task_id,
        broker_response=placeholder_response,
        linked_request_artifact=str(resolved_request_path),
        task_linkage=task_linkage,
    )
    _write_json(result_path, result_payload)
    _write_json(latest_result_path, result_payload)
    return {
        "status": "completed",
        "handoff_id": handoff_id,
        "task_id": task_id,
        "request_artifact": str(resolved_request_path),
        "result_artifact": str(result_path),
        "latest_result_artifact": str(latest_result_path),
    }