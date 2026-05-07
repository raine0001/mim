from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.local_broker_boundary import DEFAULT_SHARED_ROOT, LocalBrokerBoundary, build_broker_result_artifact
from core.local_broker_result_interpreter import interpret_broker_result_artifact


SUPPORTED_EXECUTION_TOOLS = frozenset({"get_current_objective", "get_tod_status"})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def execute_interpreted_broker_tool_intent(
    *,
    result_artifact_path: Path,
    shared_root: Path = DEFAULT_SHARED_ROOT,
) -> dict[str, Any]:
    resolved_result_path = result_artifact_path.expanduser().resolve()
    interpretation = interpret_broker_result_artifact(result_artifact_path=resolved_result_path)
    if interpretation["classification"] != "single_bounded_tool_intent_placeholder":
        raise ValueError("broker result artifact must contain exactly one interpreted single bounded tool intent placeholder")

    result_payload = _read_json(resolved_result_path)
    if not result_payload:
        raise ValueError("broker result artifact must be valid JSON")
    response = result_payload.get("response")
    if not isinstance(response, dict):
        raise ValueError("broker result artifact must contain a response payload")

    tool_call_intent = response.get("tool_call_intent")
    if not isinstance(tool_call_intent, dict):
        raise ValueError("broker result artifact must contain one tool_call_intent")
    if str(tool_call_intent.get("execution_state") or "").strip() != "not_executed":
        raise ValueError("broker tool intent must remain not_executed before local bridge execution")

    tool_name = str(tool_call_intent.get("tool_name") or "").strip()
    if tool_name not in SUPPORTED_EXECUTION_TOOLS:
        raise ValueError("broker execution bridge currently supports only one interpreted get_current_objective or get_tod_status intent")

    request_artifact_path = Path(str(result_payload.get("linked_request_artifact") or "").strip()).expanduser().resolve()
    request_payload = _read_json(request_artifact_path)
    session_context = request_payload.get("session_context")
    if not isinstance(session_context, dict):
        raise ValueError("linked broker request artifact must preserve session_context")

    boundary = LocalBrokerBoundary(shared_root=shared_root)
    executed_result = boundary.execute_tool(
        tool_name=tool_name,
        arguments=dict(tool_call_intent.get("arguments") or {}),
        session_context=session_context,
    )

    updated_tool_call_intent = dict(tool_call_intent)
    updated_tool_call_intent["execution_state"] = "executed"
    updated_tool_call_intent["executed_tool_name"] = tool_name

    broker_response = {
        "status": "tool_executed",
        "output_text": str(response.get("output_text") or "").strip(),
        "tool_call_intent": updated_tool_call_intent,
        "available_tools": response.get("available_tools") or [],
        "model_response_placeholder": response.get("model_response_placeholder"),
        "tool_call_placeholder": response.get("tool_call_placeholder"),
        "executed_result": executed_result,
    }
    updated_result_payload = build_broker_result_artifact(
        handoff_id=str(result_payload.get("handoff_id") or "").strip(),
        task_id=str(result_payload.get("task_id") or "").strip(),
        broker_response=broker_response,
        linked_request_artifact=str(result_payload.get("linked_request_artifact") or "").strip(),
        task_linkage=result_payload.get("task_linkage") if isinstance(result_payload.get("task_linkage"), dict) else None,
    )
    _write_json(resolved_result_path, updated_result_payload)
    latest_result_path = resolved_result_path.parent / resolved_result_path.name.replace(f"{updated_result_payload['handoff_id']}.broker-result.json", "HANDOFF_BROKER_RESULT.latest.json")
    _write_json(latest_result_path, updated_result_payload)
    return {
        "status": "completed",
        "handoff_id": updated_result_payload["handoff_id"],
        "task_id": updated_result_payload["task_id"],
        "result_artifact": str(resolved_result_path),
        "latest_result_artifact": str(latest_result_path),
        "executed_tool_name": tool_name,
    }