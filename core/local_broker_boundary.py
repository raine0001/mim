from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib import error as urllib_error
from urllib import request as urllib_request

from core.bounded_action_registry import (
    CANONICAL_BOUNDED_ACTION_NAMES,
    get_bounded_action,
    list_bounded_actions,
)
from core.config import settings
from core.primitive_request_recovery_service import (
    DEFAULT_SHARED_ROOT,
    dispatch_bounded_tod_bridge_warning_recommendation_request,
    dispatch_bounded_tod_bridge_warning_request,
    dispatch_bounded_tod_recent_changes_request,
    dispatch_bounded_tod_status_request,
    dispatch_bounded_tod_warnings_summary_request,
)


MIM_CONTEXT_EXPORT_ARTIFACT = "MIM_CONTEXT_EXPORT.latest.json"
BROKER_REQUEST_SCHEMA_VERSION = "mim-local-broker-request-v1"
BROKER_RESULT_SCHEMA_VERSION = "mim-local-broker-result-v1"
LATEST_BROKER_REQUEST_ARTIFACT = "HANDOFF_BROKER_REQUEST.latest.json"
LATEST_BROKER_RESULT_ARTIFACT = "HANDOFF_BROKER_RESULT.latest.json"
DEFAULT_OPENAI_BROKER_MODEL = "gpt-4.1-mini"
DEFAULT_OPENAI_BROKER_URL = "https://api.openai.com/v1/chat/completions"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _compact_text(value: Any, limit: int = 220) -> str:
    cleaned = " ".join(str(value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item or "").strip()]


def _normalize_step_list(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(values, start=1):
        if isinstance(item, dict):
            step_id = str(item.get("step_id") or item.get("id") or f"step_{index:03d}").strip()
            summary = _compact_text(
                item.get("summary") or item.get("detail") or item.get("description") or step_id,
                180,
            )
        else:
            step_id = f"step_{index:03d}"
            summary = _compact_text(item, 180)
        normalized.append({"step_id": step_id, "summary": summary})
    return normalized


def _request_id(prefix: str, session_context: dict[str, Any]) -> str:
    seed = str(
        session_context.get("handoff_id")
        or session_context.get("session_id")
        or session_context.get("topic")
        or "local"
    ).strip()
    normalized = re.sub(r"[^a-z0-9]+", "-", seed.lower()).strip("-") or "local"
    return f"broker-{prefix}-{normalized}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"


@dataclass(frozen=True)
class BrokerSessionContext:
    session_id: str
    handoff_id: str
    source: str
    topic: str
    summary: str
    requested_outcome: str
    constraints: list[str]
    next_bounded_steps: list[dict[str, Any]]
    bounded_actions_allowed: list[str]

    def to_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "handoff_id": self.handoff_id,
            "source": self.source,
            "topic": self.topic,
            "summary": self.summary,
            "requested_outcome": self.requested_outcome,
            "constraints": list(self.constraints),
            "next_bounded_steps": list(self.next_bounded_steps),
            "bounded_actions_allowed": list(self.bounded_actions_allowed),
        }


class BrokerClient(Protocol):
    async def generate(
        self,
        *,
        session_context: dict[str, Any],
        tool_schemas: list[dict[str, Any]],
    ) -> dict[str, Any]: ...


def _live_openai_api_key() -> str:
    return str(
        os.getenv("OPENAI_API_KEY")
        or os.getenv("MIM_OPENAI_API_KEY")
        or settings.openai_api_key
        or ""
    ).strip()


def live_openai_broker_configured() -> bool:
    return bool(_live_openai_api_key())


def _extract_chat_completion_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message = first_choice.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip() in {"text", "output_text", "input_text"}:
                text_value = str(item.get("text") or "").strip()
                if text_value:
                    text_parts.append(text_value)
        return "\n".join(part for part in text_parts if part).strip()
    return ""


class OpenAIBrokerClient:
    def __init__(
        self,
        *,
        model: str | None = None,
        api_url: str | None = None,
    ) -> None:
        self.model = str(
            model or os.getenv("MIM_LOCAL_BROKER_OPENAI_MODEL") or DEFAULT_OPENAI_BROKER_MODEL
        ).strip()
        self.api_url = str(
            api_url or os.getenv("MIM_LOCAL_BROKER_OPENAI_URL") or DEFAULT_OPENAI_BROKER_URL
        ).strip()

    def _build_request_payload(
        self,
        *,
        session_context: dict[str, Any],
        tool_schemas: list[dict[str, Any]],
    ) -> dict[str, Any]:
        prompt_payload = {
            "session_context": session_context,
            "tool_schemas": tool_schemas,
            "response_contract": {
                "mode": "response_only",
                "tool_execution": "forbidden",
                "tool_request_output": "forbidden",
                "required_output": "Return one concise plain-text answer only.",
            },
        }
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a bounded local broker. Respond with plain text only. "
                        "Do not request tools, do not emit tool-call JSON, and do not plan multi-step execution."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(prompt_payload, indent=2, sort_keys=True),
                },
            ],
            "temperature": 0.2,
        }

    def _generate_sync(
        self,
        *,
        session_context: dict[str, Any],
        tool_schemas: list[dict[str, Any]],
    ) -> dict[str, Any]:
        api_key = _live_openai_api_key()
        if not api_key:
            return {
                "status": "not_configured",
                "reason": "openai_api_key_missing",
                "output_text": "",
                "tool_call_intent": None,
                "available_tools": [tool.get("name") for tool in tool_schemas],
                "generated_at": _utc_now(),
            }

        request_payload = self._build_request_payload(
            session_context=session_context,
            tool_schemas=tool_schemas,
        )
        request = urllib_request.Request(
            self.api_url,
            data=json.dumps(request_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib_request.urlopen(request, timeout=30) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else str(exc)
            raise RuntimeError(f"openai_broker_http_error:{exc.code}:{_compact_text(detail, 240)}") from exc
        except OSError as exc:
            raise RuntimeError(f"openai_broker_transport_error:{exc}") from exc

        output_text = _extract_chat_completion_text(response_payload)
        if not output_text:
            raise RuntimeError("openai_broker_empty_response")

        choices = response_payload.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices else {}
        finish_reason = str(first_choice.get("finish_reason") or "").strip() if isinstance(first_choice, dict) else ""
        return {
            "status": "completed",
            "output_text": output_text,
            "tool_call_intent": None,
            "available_tools": [tool.get("name") for tool in tool_schemas],
            "generated_at": _utc_now(),
            "model_response": {
                "provider": "openai",
                "response_id": str(response_payload.get("id") or "").strip(),
                "model": str(response_payload.get("model") or self.model).strip(),
                "finish_reason": finish_reason,
                "usage": response_payload.get("usage") or {},
            },
        }

    async def generate(
        self,
        *,
        session_context: dict[str, Any],
        tool_schemas: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._generate_sync,
            session_context=session_context,
            tool_schemas=tool_schemas,
        )


def build_handoff_broker_session_context(*, handoff_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    context = BrokerSessionContext(
        session_id=f"handoff:{handoff_id}",
        handoff_id=handoff_id,
        source=str(payload.get("source") or "").strip(),
        topic=str(payload.get("topic") or "").strip(),
        summary=_compact_text(payload.get("summary"), 320),
        requested_outcome=_compact_text(payload.get("requested_outcome"), 220),
        constraints=_normalize_string_list(payload.get("constraints")),
        next_bounded_steps=_normalize_step_list(payload.get("next_bounded_steps")),
        bounded_actions_allowed=_normalize_string_list(payload.get("bounded_actions_allowed")),
    )
    return context.to_payload()


def build_broker_tool_schemas() -> list[dict[str, Any]]:
    bounded_actions = list(CANONICAL_BOUNDED_ACTION_NAMES)
    return [
        {
            "name": "get_current_objective",
            "description": "Return the current objective summary from local shared state.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "get_tod_status",
            "description": "Run the existing bounded TOD status request through the shared bridge.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "get_recent_changes",
            "description": "Run the existing bounded TOD recent-changes request through the shared bridge.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "get_current_warnings",
            "description": "Run the existing bounded TOD warnings-summary request through the shared bridge.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "get_bridge_warning_explanation",
            "description": "Run the existing bounded TOD bridge-warning explanation request through the shared bridge.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "get_bridge_warning_next_step",
            "description": "Run the existing bounded TOD bridge-warning recommendation request through the shared bridge.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "list_bounded_actions",
            "description": "List the existing bounded actions that MIM is allowed to run locally.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "run_bounded_action",
            "description": "Run one existing bounded action only; arbitrary commands are not allowed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "action_name": {"type": "string", "enum": bounded_actions},
                },
                "required": ["action_name"],
                "additionalProperties": False,
            },
        },
    ]


def build_broker_request_artifact(
    *,
    handoff_id: str,
    task_id: str,
    session_context: dict[str, Any],
    tool_schemas: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": BROKER_REQUEST_SCHEMA_VERSION,
        "artifact_type": BROKER_REQUEST_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "handoff_id": str(handoff_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "task_linkage": {
            "handoff_id": str(handoff_id or "").strip(),
            "task_id": str(task_id or "").strip(),
            "session_id": str(session_context.get("session_id") or "").strip(),
        },
        "session_context": session_context,
        "tool_schemas": tool_schemas,
        "tool_names": [str(tool.get("name") or "").strip() for tool in tool_schemas],
    }


def build_broker_result_artifact(
    *,
    handoff_id: str,
    task_id: str,
    broker_response: dict[str, Any],
    linked_request_artifact: str = "",
    task_linkage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response_status = str(broker_response.get("status") or "").strip() or "response_placeholder"
    if response_status == "not_configured":
        response_kind = "not_configured"
        response_payload: dict[str, Any] = {
            "status": "not_configured",
            "reason": str(broker_response.get("reason") or "local_broker_client_not_configured").strip(),
            "available_tools": broker_response.get("available_tools") or [],
            "output_text": str(broker_response.get("output_text") or "").strip(),
            "tool_call_intent": broker_response.get("tool_call_intent"),
        }
    else:
        response_kind = "model_response" if broker_response.get("model_response") else "bounded_model_or_tool_call_placeholder"
        response_payload = {
            "status": response_status,
            "output_text": str(broker_response.get("output_text") or "").strip(),
            "tool_call_intent": broker_response.get("tool_call_intent"),
            "available_tools": broker_response.get("available_tools") or [],
            "model_response": broker_response.get("model_response"),
            "model_response_placeholder": broker_response.get("model_response_placeholder"),
            "tool_call_placeholder": broker_response.get("tool_call_placeholder"),
            "executed_result": broker_response.get("executed_result"),
        }
    artifact = {
        "schema_version": BROKER_RESULT_SCHEMA_VERSION,
        "artifact_type": BROKER_RESULT_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "handoff_id": str(handoff_id or "").strip(),
        "task_id": str(task_id or "").strip(),
        "response_kind": response_kind,
        "response": response_payload,
    }
    if linked_request_artifact:
        artifact["linked_request_artifact"] = str(linked_request_artifact).strip()
    if task_linkage:
        artifact["task_linkage"] = dict(task_linkage)
    return artifact


def get_current_objective(*, shared_root: Path = DEFAULT_SHARED_ROOT) -> dict[str, Any]:
    payload = _read_json(shared_root / MIM_CONTEXT_EXPORT_ARTIFACT)
    objective_id = str(
        payload.get("objective_active")
        or payload.get("current_next_objective")
        or payload.get("latest_completed_objective")
        or ""
    ).strip()
    summary_parts = []
    if objective_id:
        summary_parts.append(f"Current objective is {objective_id}.")
    else:
        summary_parts.append("Current objective is not available from local shared state.")
    note = str(payload.get("self_evolution_summary") or "").strip()
    if note:
        summary_parts.append(_compact_text(note, 180))
    return {
        "objective_id": objective_id,
        "summary": _compact_text(" ".join(summary_parts), 240),
        "source": str(shared_root / MIM_CONTEXT_EXPORT_ARTIFACT),
        "generated_at": _utc_now(),
    }


class LocalBrokerBoundary:
    def __init__(
        self,
        *,
        shared_root: Path = DEFAULT_SHARED_ROOT,
        client: BrokerClient | None = None,
    ) -> None:
        self.shared_root = shared_root.expanduser().resolve()
        self.client = client if client is not None else OpenAIBrokerClient() if live_openai_broker_configured() else None

    def execute_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None,
        session_context: dict[str, Any],
    ) -> dict[str, Any]:
        tool_name = str(tool_name or "").strip()
        arguments = arguments or {}
        if tool_name == "get_current_objective":
            return get_current_objective(shared_root=self.shared_root)
        if tool_name == "get_tod_status":
            return dispatch_bounded_tod_status_request(
                request_id=_request_id("status", session_context),
                session_key=str(session_context.get("session_id") or "local-broker"),
                content=str(session_context.get("requested_outcome") or session_context.get("summary") or "status").strip(),
                actor="local-broker",
                shared_root=self.shared_root,
            )
        if tool_name == "get_recent_changes":
            return dispatch_bounded_tod_recent_changes_request(
                request_id=_request_id("recent-changes", session_context),
                session_key=str(session_context.get("session_id") or "local-broker"),
                content=str(session_context.get("requested_outcome") or session_context.get("summary") or "recent changes").strip(),
                actor="local-broker",
                shared_root=self.shared_root,
            )
        if tool_name == "get_current_warnings":
            return dispatch_bounded_tod_warnings_summary_request(
                request_id=_request_id("warnings", session_context),
                session_key=str(session_context.get("session_id") or "local-broker"),
                content=str(session_context.get("requested_outcome") or session_context.get("summary") or "warnings").strip(),
                actor="local-broker",
                shared_root=self.shared_root,
            )
        if tool_name == "get_bridge_warning_explanation":
            return dispatch_bounded_tod_bridge_warning_request(
                request_id=_request_id("bridge-warning", session_context),
                session_key=str(session_context.get("session_id") or "local-broker"),
                content=str(session_context.get("requested_outcome") or session_context.get("summary") or "bridge warning").strip(),
                actor="local-broker",
                shared_root=self.shared_root,
            )
        if tool_name == "get_bridge_warning_next_step":
            return dispatch_bounded_tod_bridge_warning_recommendation_request(
                request_id=_request_id("bridge-next-step", session_context),
                session_key=str(session_context.get("session_id") or "local-broker"),
                content=str(session_context.get("requested_outcome") or session_context.get("summary") or "bridge next step").strip(),
                actor="local-broker",
                shared_root=self.shared_root,
            )
        if tool_name == "list_bounded_actions":
            payload = list_bounded_actions()
            payload["generated_at"] = _utc_now()
            return payload
        if tool_name == "run_bounded_action":
            action_name = str(arguments.get("action_name") or "").strip()
            action = get_bounded_action(action_name)
            if action is None:
                raise ValueError("action_name must match one existing bounded action")
            return action(
                request_id=_request_id(action_name or "bounded-action", session_context),
                session_key=str(session_context.get("session_id") or "local-broker"),
                content=str(session_context.get("requested_outcome") or session_context.get("summary") or action_name).strip(),
                actor="local-broker",
                shared_root=self.shared_root,
            )
        raise ValueError("tool_name must match one local broker tool")

    async def invoke(
        self,
        *,
        session_context: dict[str, Any],
        tool_schemas: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        effective_tool_schemas = tool_schemas or build_broker_tool_schemas()
        if self.client is None:
            return {
                "status": "not_configured",
                "reason": "local_broker_client_not_configured",
                "output_text": "",
                "tool_call_intent": None,
                "available_tools": [tool.get("name") for tool in effective_tool_schemas],
                "generated_at": _utc_now(),
            }
        return await self.client.generate(
            session_context=session_context,
            tool_schemas=effective_tool_schemas,
        )