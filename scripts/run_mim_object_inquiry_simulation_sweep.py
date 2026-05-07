#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


FEEDBACK_ACTOR = "tod"


def _post_json(
    base_url: str, path: str, payload: dict[str, Any], timeout: int = 25
) -> tuple[int, dict[str, Any]]:
    req = request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return int(response.status), parsed if isinstance(parsed, dict) else {
                "data": parsed
            }
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}


def _get_json(
    base_url: str, path: str, query: dict[str, Any] | None = None, timeout: int = 25
) -> tuple[int, dict[str, Any] | list[Any]]:
    url = f"{base_url}{path}"
    if query:
        url = f"{url}?{parse.urlencode(query)}"
    req = request.Request(url, method="GET")
    try:
        with request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return int(response.status), parsed if isinstance(
                parsed, (dict, list)
            ) else {"data": parsed}
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return int(exc.code), parsed if isinstance(parsed, (dict, list)) else {
            "data": parsed
        }


def _render(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format(**replacements)
    if isinstance(value, list):
        return [_render(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _render(item, replacements) for key, item in value.items()}
    return value


def _contains_all(text: str, markers: list[str]) -> bool:
    lowered = str(text or "").strip().lower()
    return all(
        str(marker).strip().lower() in lowered
        for marker in markers
        if str(marker).strip()
    )


def _contains_any(text: str, markers: list[str]) -> bool:
    lowered = str(text or "").strip().lower()
    cleaned = [str(marker).strip().lower() for marker in markers if str(marker).strip()]
    if not cleaned:
        return True
    return any(marker in lowered for marker in cleaned)


def _contains_none(text: str, markers: list[str]) -> bool:
    lowered = str(text or "").strip().lower()
    return all(
        str(marker).strip().lower() not in lowered
        for marker in markers
        if str(marker).strip()
    )


def _ensure_workspace_scan_capability(base_url: str, timeout: int) -> None:
    _post_json(
        base_url,
        "/gateway/capabilities",
        {
            "capability_name": "workspace_scan",
            "category": "diagnostic",
            "description": "Scan workspace and return observation set",
            "requires_confirmation": False,
            "enabled": True,
            "safety_policy": {"scope": "non-actuating", "mode": "scan-only"},
        },
        timeout=timeout,
    )


def _run_workspace_scan(
    base_url: str, scan_area: str, observations: list[dict[str, Any]], timeout: int
) -> tuple[bool, str]:
    _ensure_workspace_scan_capability(base_url, timeout)
    status, payload = _post_json(
        base_url,
        "/gateway/intake/text",
        {
            "text": f"scan workspace {scan_area}",
            "parsed_intent": "observe_workspace",
            "confidence": 0.95,
            "metadata_json": {
                "scan_mode": "full",
                "scan_area": scan_area,
                "confidence_threshold": 0.65,
            },
        },
        timeout=timeout,
    )
    if status != 200 or not isinstance(payload, dict):
        return False, f"workspace_scan_start_failed status={status}"
    execution = payload.get("execution", {}) if isinstance(payload, dict) else {}
    execution_id = int(execution.get("execution_id", 0) or 0)
    if execution_id <= 0:
        return False, "workspace_scan_missing_execution_id"
    for step in ["accepted", "running"]:
        _post_json(
            base_url,
            f"/gateway/capabilities/executions/{execution_id}/feedback",
            {
                "status": step,
                "reason": step,
                "actor": FEEDBACK_ACTOR,
                "feedback_json": {},
            },
            timeout=timeout,
        )
    status, final_payload = _post_json(
        base_url,
        f"/gateway/capabilities/executions/{execution_id}/feedback",
        {
            "status": "succeeded",
            "reason": "scan complete",
            "actor": FEEDBACK_ACTOR,
            "feedback_json": {
                "observations": observations,
                "observation_confidence": 0.92,
            },
        },
        timeout=timeout,
    )
    if status != 200:
        return False, f"workspace_scan_complete_failed status={status}"
    return True, "workspace_scan_ok"


def _post_camera_event(
    base_url: str,
    *,
    session_id: str,
    device_suffix: str,
    observations: list[dict[str, Any]],
    timeout: int,
) -> tuple[int, dict[str, Any]]:
    return _post_json(
        base_url,
        "/gateway/perception/camera/events",
        {
            "device_id": f"cam-{device_suffix}-{session_id}",
            "source_type": "camera",
            "session_id": session_id,
            "is_remote": False,
            "min_interval_seconds": 0,
            "duplicate_window_seconds": 2,
            "observation_confidence_floor": 0.2,
            "metadata_json": {"source": f"object-inquiry-sim-{device_suffix}"},
            "observations": observations,
        },
        timeout=timeout,
    )


def _post_session_turn(
    base_url: str, *, session_id: str, text: str, parsed_intent: str, timeout: int
) -> tuple[int, dict[str, Any]]:
    return _post_json(
        base_url,
        "/gateway/intake/text",
        {
            "text": text,
            "parsed_intent": parsed_intent,
            "confidence": 0.9,
            "metadata_json": {"conversation_session_id": session_id},
        },
        timeout=timeout,
    )


def _state_prompt(base_url: str, timeout: int) -> tuple[int, str, dict[str, Any]]:
    status, payload = _get_json(base_url, "/mim/ui/state", timeout=timeout)
    if not isinstance(payload, dict):
        return status, "", {}
    return status, str(payload.get("inquiry_prompt", "") or "").strip(), payload


def _object_library_entry(
    base_url: str, label: str, timeout: int
) -> tuple[int, dict[str, Any] | None, list[dict[str, Any]]]:
    status, payload = _get_json(
        base_url, "/workspace/object-library", {"label": label}, timeout=timeout
    )
    if not isinstance(payload, dict):
        return status, None, []
    objects = (
        payload.get("objects", [])
        if isinstance(payload.get("objects", []), list)
        else []
    )
    target = next(
        (
            item
            for item in objects
            if isinstance(item, dict)
            and str(item.get("canonical_name", "")).strip() == label
        ),
        None,
    )
    return status, target, objects


def _check_metadata_value(actual: str, expected: str) -> bool:
    return str(actual or "").strip().lower() == str(expected or "").strip().lower()


def _check_metadata_any(actual: str, expected_values: list[str]) -> bool:
    lowered = str(actual or "").strip().lower()
    return any(str(item or "").strip().lower() in lowered for item in expected_values)


def _execute_action(
    base_url: str, session_id: str, action: dict[str, Any], timeout: int
) -> dict[str, Any]:
    action_type = str(action.get("type") or "").strip()
    result: dict[str, Any] = {"type": action_type, "ok": True}

    if action_type == "camera_event":
        status, payload = _post_camera_event(
            base_url,
            session_id=session_id,
            device_suffix=str(action.get("device_suffix") or "camera"),
            observations=action.get("observations", []),
            timeout=timeout,
        )
        result.update({"status": status, "payload": payload, "ok": status == 200})
        return result

    if action_type == "workspace_scan":
        ok, detail = _run_workspace_scan(
            base_url,
            scan_area=str(action.get("scan_area") or "scan-area"),
            observations=action.get("observations", []),
            timeout=timeout,
        )
        result.update({"ok": ok, "detail": detail})
        return result

    if action_type == "state_expect":
        status, prompt, payload = _state_prompt(base_url, timeout)
        ok = status == 200
        ok = ok and _contains_all(prompt, action.get("required_prompt_markers", []))
        ok = ok and _contains_any(prompt, action.get("required_any_prompt_markers", []))
        ok = ok and _contains_none(prompt, action.get("forbidden_prompt_markers", []))
        result.update(
            {"status": status, "prompt": prompt, "payload": payload, "ok": ok}
        )
        return result

    if action_type == "user_turn":
        status, payload = _post_session_turn(
            base_url,
            session_id=session_id,
            text=str(action.get("text") or ""),
            parsed_intent=str(action.get("parsed_intent") or "discussion"),
            timeout=timeout,
        )
        resolution = payload.get("resolution", {}) if isinstance(payload, dict) else {}
        prompt = str(resolution.get("clarification_prompt", "") or "").strip()
        metadata = (
            resolution.get("metadata_json", {}) if isinstance(resolution, dict) else {}
        )
        topic = str(metadata.get("conversation_topic", "") or "").strip().lower()
        ok = status == 200 and bool(prompt)
        ok = ok and _contains_all(prompt, action.get("required_markers", []))
        ok = ok and _contains_any(prompt, action.get("required_any_markers", []))
        ok = ok and _contains_none(prompt, action.get("forbidden_markers", []))
        expected_topic = str(action.get("expected_topic") or "").strip().lower()
        if expected_topic:
            ok = ok and topic == expected_topic
        result.update(
            {
                "status": status,
                "prompt": prompt,
                "payload": payload,
                "topic": topic,
                "ok": ok,
            }
        )
        return result

    if action_type == "object_library_expect":
        status, target, objects = _object_library_entry(
            base_url,
            label=str(action.get("label") or "").strip(),
            timeout=timeout,
        )
        ok = status == 200 and target is not None
        metadata = target.get("metadata_json", {}) if isinstance(target, dict) else {}
        semantic_fields = (
            target.get("semantic_fields", []) if isinstance(target, dict) else []
        )
        for key, expected in (
            action.get("expected_metadata", {})
            if isinstance(action.get("expected_metadata", {}), dict)
            else {}
        ).items():
            ok = ok and _check_metadata_value(
                str(metadata.get(key, "") or ""), str(expected or "")
            )
        for key, expected_values in (
            action.get("expected_any_metadata", {})
            if isinstance(action.get("expected_any_metadata", {}), dict)
            else {}
        ).items():
            expected_list = (
                expected_values
                if isinstance(expected_values, list)
                else [expected_values]
            )
            ok = ok and _check_metadata_any(
                str(metadata.get(key, "") or ""), [str(item) for item in expected_list]
            )
        for field in action.get("required_semantic_fields", []):
            ok = ok and str(field) in [str(item) for item in semantic_fields]
        result.update(
            {"status": status, "target": target, "objects": objects, "ok": ok}
        )
        return result

    result.update({"ok": False, "detail": f"unsupported_action:{action_type}"})
    return result


def run_scenarios(
    base_url: str, scenarios: list[dict[str, Any]], timeout: int
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        scenario_id = str(scenario.get("scenario_id") or "scenario")
        run_id = uuid.uuid4().hex[:8]
        replacements = {"run_id": run_id}
        rendered = _render(scenario, replacements)
        session_id = f"object-sim-{scenario_id}-{run_id}"
        action_results: list[dict[str, Any]] = []
        scenario_ok = True
        for action in rendered.get("actions", []):
            action_result = _execute_action(base_url, session_id, action, timeout)
            action_results.append(action_result)
            if not bool(action_result.get("ok")):
                scenario_ok = False
                break
        results.append(
            {
                "scenario_id": scenario_id,
                "bucket": str(rendered.get("bucket") or "unknown"),
                "description": str(rendered.get("description") or "").strip(),
                "session_id": session_id,
                "ok": scenario_ok,
                "actions": action_results,
            }
        )
    ok_count = sum(1 for item in results if bool(item.get("ok")))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "scenario_count": len(results),
        "ok_count": ok_count,
        "ok_ratio": round(ok_count / float(max(1, len(results))), 6),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run proactive camera object-inquiry simulations against MIM"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument(
        "--scenarios",
        default="conversation_scenarios/object_inquiry_proactive_pack.json",
    )
    parser.add_argument(
        "--output",
        default="runtime/reports/object_inquiry_proactive_sweep.json",
    )
    parser.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args()

    scenarios_path = Path(args.scenarios)
    scenarios_data = json.loads(scenarios_path.read_text(encoding="utf-8"))
    scenarios = scenarios_data if isinstance(scenarios_data, list) else []
    report = run_scenarios(str(args.base_url).rstrip("/"), scenarios, int(args.timeout))

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output_path),
                "summary": {
                    "ok_count": report.get("ok_count", 0),
                    "scenario_count": report.get("scenario_count", 0),
                    "ok_ratio": report.get("ok_ratio", 0.0),
                },
            },
            indent=2,
        )
    )
    return (
        0
        if int(report.get("ok_count", 0) or 0)
        == int(report.get("scenario_count", 0) or 0)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
