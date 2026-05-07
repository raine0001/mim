from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.local_broker_boundary import BROKER_RESULT_SCHEMA_VERSION, LATEST_BROKER_RESULT_ARTIFACT
from core.local_broker_result_interpreter import interpret_broker_result_artifact


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_persisted_interpretation(interpretation: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "status": "completed",
        "interpreted_at": _utc_now(),
        "classification": str(interpretation.get("classification") or "").strip(),
        "handoff_id": str(interpretation.get("handoff_id") or "").strip(),
        "task_id": str(interpretation.get("task_id") or "").strip(),
        "linked_request_artifact": str(interpretation.get("linked_request_artifact") or "").strip(),
        "task_linkage": dict(interpretation.get("task_linkage") or {}),
    }
    if "tool_name" in interpretation:
        payload["tool_name"] = str(interpretation.get("tool_name") or "").strip()
    if "arguments" in interpretation:
        payload["arguments"] = dict(interpretation.get("arguments") or {})
    return payload


async def persist_broker_result_artifact_interpretation_async(*, result_artifact_path: Path) -> dict[str, Any]:
    resolved_result_path = result_artifact_path.expanduser().resolve()
    result_payload = _read_json(resolved_result_path)
    if not result_payload:
        raise ValueError("broker result artifact must be valid JSON")
    if str(result_payload.get("schema_version") or "").strip() != BROKER_RESULT_SCHEMA_VERSION:
        raise ValueError("broker result artifact must use the local broker result schema")

    interpretation = await asyncio.to_thread(
        interpret_broker_result_artifact,
        result_artifact_path=resolved_result_path,
    )
    result_payload["interpretation"] = _build_persisted_interpretation(interpretation)

    latest_result_path = resolved_result_path.parent / LATEST_BROKER_RESULT_ARTIFACT
    _write_json(resolved_result_path, result_payload)
    _write_json(latest_result_path, result_payload)
    return {
        "status": "completed",
        "handoff_id": str(result_payload.get("handoff_id") or "").strip(),
        "task_id": str(result_payload.get("task_id") or "").strip(),
        "classification": str(interpretation.get("classification") or "").strip(),
        "result_artifact": str(resolved_result_path),
        "latest_result_artifact": str(latest_result_path),
    }


def persist_broker_result_artifact_interpretation(*, result_artifact_path: Path) -> dict[str, Any]:
    return asyncio.run(
        persist_broker_result_artifact_interpretation_async(
            result_artifact_path=result_artifact_path,
        )
    )