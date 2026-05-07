from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.bounded_action_registry import get_bounded_action
from core.local_broker_boundary import BROKER_RESULT_SCHEMA_VERSION, build_broker_tool_schemas


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _expected_tool_names() -> list[str]:
    return [str(tool.get("name") or "").strip() for tool in build_broker_tool_schemas()]


def _parse_live_model_tool_request(output_text: Any) -> dict[str, Any] | None:
    normalized_output = str(output_text or "").strip()
    if not normalized_output:
        return None

    candidate = normalized_output
    fenced_match = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", normalized_output, re.DOTALL)
    if fenced_match:
        candidate = fenced_match.group(1).strip()

    try:
        parsed = json.loads(candidate)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    if "tool_name" not in parsed:
        return None

    unknown_keys = sorted(set(parsed) - {"tool_name", "arguments"})
    if unknown_keys:
        raise ValueError("live model tool intent request must contain only tool_name and arguments")

    arguments = parsed.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("live model tool intent request arguments must be an object")

    return {
        "tool_name": str(parsed.get("tool_name") or "").strip(),
        "arguments": dict(arguments),
    }


def interpret_broker_result_artifact(*, result_artifact_path: Path) -> dict[str, Any]:
    resolved_result_path = result_artifact_path.expanduser().resolve()
    payload = _read_json(resolved_result_path)
    if not payload:
        raise ValueError("broker result artifact must be valid JSON")
    if str(payload.get("schema_version") or "").strip() != BROKER_RESULT_SCHEMA_VERSION:
        raise ValueError("broker result artifact must use the local broker result schema")

    handoff_id = str(payload.get("handoff_id") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    linked_request_artifact = str(payload.get("linked_request_artifact") or "").strip()
    task_linkage = payload.get("task_linkage")
    response = payload.get("response")
    if not handoff_id or not task_id:
        raise ValueError("broker result artifact must preserve handoff_id and task_id")
    if not linked_request_artifact:
        raise ValueError("broker result artifact must preserve linked_request_artifact")
    if not isinstance(task_linkage, dict):
        raise ValueError("broker result artifact must preserve task_linkage")
    if not isinstance(response, dict):
        raise ValueError("broker result artifact must contain a response payload")

    available_tools = [str(value or "").strip() for value in response.get("available_tools") or []]
    if available_tools != _expected_tool_names():
        raise ValueError("broker result artifact must preserve the fixed bounded tool list")

    result: dict[str, Any] = {
        "classification": "unknown",
        "handoff_id": handoff_id,
        "task_id": task_id,
        "task_linkage": dict(task_linkage),
        "linked_request_artifact": linked_request_artifact,
        "result_artifact": str(resolved_result_path),
    }

    response_kind = str(payload.get("response_kind") or "").strip()
    if response_kind == "model_response":
        live_tool_request = _parse_live_model_tool_request(response.get("output_text"))
        if live_tool_request is None:
            result["classification"] = "model_response_text"
            result["output_text"] = str(response.get("output_text") or "").strip()
            return result

        live_tool_name = str(live_tool_request.get("tool_name") or "").strip()
        if not live_tool_name:
            raise ValueError("live model tool intent request must include tool_name")
        if live_tool_name not in available_tools:
            raise ValueError("live model tool intent request must match the fixed bounded tool list")
        if live_tool_name == "run_bounded_action":
            action_name = str((live_tool_request.get("arguments") or {}).get("action_name") or "").strip()
            if get_bounded_action(action_name) is None:
                raise ValueError("live model tool intent request must reference one existing bounded action")

        result["classification"] = "model_response_single_bounded_tool_intent_request"
        result["tool_name"] = live_tool_name
        result["arguments"] = dict(live_tool_request.get("arguments") or {})
        result["output_text"] = str(response.get("output_text") or "").strip()
        return result

    tool_call_placeholder = response.get("tool_call_placeholder")
    tool_call_intent = response.get("tool_call_intent")
    if not isinstance(tool_call_placeholder, dict):
        raise ValueError("broker result artifact must include tool_call_placeholder")

    placeholder_tool_name = str(tool_call_placeholder.get("tool_name") or "").strip()
    if not placeholder_tool_name:
        if tool_call_intent is not None:
            raise ValueError("no-tool placeholder must not include a tool_call_intent")
        result["classification"] = "no_tool_placeholder"
        return result

    if placeholder_tool_name not in available_tools:
        raise ValueError("tool intent placeholder must match the fixed bounded tool list")
    if not isinstance(tool_call_intent, dict):
        raise ValueError("single bounded tool intent placeholder must include tool_call_intent")
    if str(tool_call_intent.get("execution_state") or "").strip() != "not_executed":
        raise ValueError("tool intent placeholder must remain not_executed")
    if placeholder_tool_name == "run_bounded_action":
        action_name = str((tool_call_intent.get("arguments") or {}).get("action_name") or "").strip()
        if get_bounded_action(action_name) is None:
            raise ValueError("tool intent placeholder must reference one existing bounded action")
    result["classification"] = "single_bounded_tool_intent_placeholder"
    return result