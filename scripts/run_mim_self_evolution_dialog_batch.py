#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request


OPENING_PROMPT = "what would you like to work on next MIM?"


THEMES = [
    "cross-session context recall for remembered people and preferences",
    "follow-up continuity so short replies stay attached to the active topic",
    "approval and confirmation interpretation without repetitive clarification",
    "revision handling when the operator changes scope mid-thread",
    "conversion of self-improvement suggestions into bounded implementation tasks",
    "implementation-plan clarity for communication-focused work",
    "task-handoff summaries between MIM dialog and TOD execution",
    "direct-answer quality for status, priority, and next-step questions",
    "conversation memory that carries preferences and identity across sessions",
    "clarification reduction for high-signal communication requests",
]


PLAN_TEMPLATES = [
    "Keep it focused on {theme}. Create a bounded implementation plan for that and continue. This task must improve MIM communication capabilities in context and understanding.",
    "Choose one concrete task within {theme}, turn it into an implementation plan, and keep it communication-focused. This task must improve MIM communication capabilities in context and understanding.",
    "Stay within {theme}. Draft the implementation plan you would follow next and keep it bounded. This task must improve MIM communication capabilities in context and understanding.",
    "Use {theme} as the target. Turn that into a real implementation plan with the first bounded step. This task must improve MIM communication capabilities in context and understanding.",
    "Within {theme}, decide the next bounded task, explain it briefly, and create the implementation plan. This task must improve MIM communication capabilities in context and understanding.",
]


APPROVAL_TEMPLATES = [
    "yes, continue and implement",
    "yes please, execute that plan",
    "do that and move into implementation now",
    "yes, start with the first bounded implementation step",
    "proceed with that plan and execute it",
]


def _post_json(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    timeout: int = 30,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return int(response.status), parsed if isinstance(parsed, dict) else {"data": parsed}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        parsed = json.loads(raw) if raw else {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}


def _request_json(
    base_url: str,
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> tuple[int, dict[str, Any]]:
    body = None
    headers: dict[str, str] = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return int(response.status), parsed if isinstance(parsed, dict) else {"data": parsed}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        parsed = json.loads(raw) if raw else {}
        return int(exc.code), parsed if isinstance(parsed, dict) else {"data": parsed}


def _turn_payload(text: str, *, session_id: str, parsed_intent: str) -> dict[str, Any]:
    return {
        "text": text,
        "parsed_intent": parsed_intent,
        "confidence": 0.95,
        "metadata_json": {
            "route_preference": "conversation_layer",
            "conversation_session_id": session_id,
            "user_id": "david",
        },
    }


def _reply_text(payload: dict[str, Any]) -> str:
    resolution = payload.get("resolution", {}) if isinstance(payload.get("resolution", {}), dict) else {}
    mim_interface = payload.get("mim_interface", {}) if isinstance(payload.get("mim_interface", {}), dict) else {}
    return str(
        resolution.get("clarification_prompt")
        or mim_interface.get("result")
        or mim_interface.get("reply_text")
        or ""
    ).strip()


def _resolution_reason(payload: dict[str, Any]) -> str:
    resolution = payload.get("resolution", {}) if isinstance(payload.get("resolution", {}), dict) else {}
    return str(resolution.get("reason") or "").strip()


def _opening_ok(text: str) -> bool:
    lowered = text.lower()
    if not lowered:
        return False
    bad_markers = {
        "you can ask me about the current status",
        "ask for status, tod focus, priorities, or top news",
    }
    if any(marker in lowered for marker in bad_markers):
        return False
    good_markers = {
        "next i would work on",
        "operator command:",
        "self-evolution",
        "bounded implementation plan",
        "review the open recommendation",
        "reviewing open recommendation",
        "detailed implementation plan",
        "backlog",
    }
    return any(marker in lowered for marker in good_markers)


def _plan_ok(reason: str, text: str) -> bool:
    lowered = text.lower()
    if reason == "conversation_bounded_implementation_dispatch":
        return True
    return any(
        marker in lowered
        for marker in {
            "implementation plan",
            "create goal",
            "say confirm",
            "steps:",
            "next action:",
        }
    )


def _theme_ok(text: str, theme: str) -> bool:
    lowered = text.lower()
    semantic_markers = {
        "cross-session": ["cross-session", "remembered", "preferences", "identity", "context recall"],
        "follow-up continuity": ["follow-up", "continuity", "active topic", "attached replies"],
        "approval and confirmation": ["approval", "confirmation", "clarification", "operator confirmation"],
        "revision handling": ["revision", "scope change", "mid-thread", "operator changes scope"],
        "self-improvement suggestions": ["self-improvement", "bounded implementation", "suggestions into", "implementation tasks"],
        "implementation-plan clarity": ["implementation plan", "bounded task", "communication-focused", "first bounded step"],
        "task-handoff summaries": ["task-handoff", "handoff", "tod execution", "dialog and tod"],
        "direct-answer quality": ["direct-answer", "status", "priority", "next-step questions"],
        "conversation memory": ["conversation memory", "preferences", "identity", "across sessions"],
        "clarification reduction": ["clarification", "high-signal", "communication requests", "precision"],
    }
    for theme_key, markers in semantic_markers.items():
        if theme_key in theme.lower():
            if any(marker in lowered for marker in markers):
                return True

    theme_tokens = [
        token for token in theme.lower().replace("-", " ").split() if len(token) > 4
    ]
    overlap = sum(1 for token in theme_tokens if token in lowered)
    return overlap >= 1 or "communication" in lowered or "context" in lowered


def _execute_operator_action(
    base_url: str,
    *,
    actor: str,
    source: str,
) -> tuple[bool, dict[str, Any]]:
    status, next_action = _request_json(
        base_url,
        (
            "/improvement/self-evolution/next-action"
            f"?refresh=true&actor={actor}&source={source}"
            "&lookback_hours=168&min_occurrence_count=2&auto_experiment_limit=3&limit=5"
        ),
    )
    if status != 200:
        return False, {
            "stage": "next_action_fetch",
            "status": status,
            "payload": next_action,
        }

    decision = next_action.get("decision", {}) if isinstance(next_action.get("decision", {}), dict) else {}
    action = decision.get("action", {}) if isinstance(decision.get("action", {}), dict) else {}
    method = str(action.get("method") or "").strip().upper()
    path = str(action.get("path") or "").strip()
    payload = action.get("payload", {}) if isinstance(action.get("payload", {}), dict) else {}
    if not method or not path:
        return False, {"stage": "missing_action", "decision": decision}

    execute_status, execute_payload = _request_json(
        base_url,
        path,
        method=method,
        payload=payload if method != "GET" else None,
    )
    return execute_status < 400, {
        "stage": "execute_action",
        "decision": decision,
        "status": execute_status,
        "payload": execute_payload,
    }


def _run_task(
    base_url: str,
    index: int,
    theme: str,
    plan_template: str,
    approval_prompt: str,
) -> dict[str, Any]:
    session_id = f"mim-self-evolution-{index:02d}-{uuid.uuid4().hex[:8]}"

    opening_status, opening_payload = _post_json(
        base_url,
        "/gateway/intake/text",
        _turn_payload(OPENING_PROMPT, session_id=session_id, parsed_intent="question"),
    )
    opening_text = _reply_text(opening_payload)

    steering_prompt = plan_template.format(theme=theme)
    plan_status, plan_payload = _post_json(
        base_url,
        "/gateway/intake/text",
        _turn_payload(steering_prompt, session_id=session_id, parsed_intent="discussion"),
    )
    plan_text = _reply_text(plan_payload)

    approval_status, approval_payload = _post_json(
        base_url,
        "/gateway/intake/text",
        _turn_payload(approval_prompt, session_id=session_id, parsed_intent="discussion"),
    )
    approval_text = _reply_text(approval_payload)

    plan_reason = _resolution_reason(plan_payload)
    approval_reason = _resolution_reason(approval_payload)
    dialog_execute_ok = (
        plan_reason == "conversation_bounded_implementation_dispatch"
        or approval_reason == "conversation_bounded_implementation_dispatch"
    )
    operator_execute_ok, operator_execute = _execute_operator_action(
        base_url,
        actor=f"dialog-batch-{index:02d}",
        source="self_evolution_dialog_batch",
    )

    opening_ok = opening_status == 200 and _opening_ok(opening_text)
    plan_ok = plan_status == 200 and _plan_ok(plan_reason, plan_text)
    theme_ok = _theme_ok(plan_text, theme) or _theme_ok(approval_text, theme)
    passed = opening_ok and plan_ok and theme_ok and (dialog_execute_ok or operator_execute_ok)

    return {
        "task_index": index,
        "session_id": session_id,
        "theme": theme,
        "opening_prompt": OPENING_PROMPT,
        "plan_prompt": steering_prompt,
        "approval_prompt": approval_prompt,
        "opening": {
            "status": opening_status,
            "reason": _resolution_reason(opening_payload),
            "text": opening_text,
            "ok": opening_ok,
        },
        "plan": {
            "status": plan_status,
            "reason": plan_reason,
            "text": plan_text,
            "ok": plan_ok,
            "theme_ok": theme_ok,
        },
        "approval": {
            "status": approval_status,
            "reason": approval_reason,
            "text": approval_text,
            "dialog_execute_ok": dialog_execute_ok,
        },
        "operator_execute": {
            "ok": operator_execute_ok,
            "details": operator_execute,
        },
        "passed": passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run repeated self-evolution dialog tasks against MIM."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--count", type=int, default=50)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument(
        "--output",
        default="runtime/reports/mim_self_evolution_dialog_batch.json",
    )
    args = parser.parse_args()

    results: list[dict[str, Any]] = []
    counts = Counter()
    total = max(1, int(args.count))

    for index in range(total):
        theme = THEMES[index % len(THEMES)]
        plan_template = PLAN_TEMPLATES[index % len(PLAN_TEMPLATES)]
        approval_prompt = APPROVAL_TEMPLATES[index % len(APPROVAL_TEMPLATES)]
        result = _run_task(
            args.base_url,
            index + 1,
            theme,
            plan_template,
            approval_prompt,
        )
        results.append(result)
        counts["passed" if result.get("passed") else "failed"] += 1
        if result.get("opening", {}).get("ok"):
            counts["opening_ok"] += 1
        if result.get("plan", {}).get("ok"):
            counts["plan_ok"] += 1
        if result.get("approval", {}).get("dialog_execute_ok"):
            counts["dialog_execute_ok"] += 1
        if result.get("operator_execute", {}).get("ok"):
            counts["operator_execute_ok"] += 1
        if result.get("plan", {}).get("theme_ok"):
            counts["theme_ok"] += 1

        print(
            "TASK "
            f"{index + 1}/{total} "
            f"passed={bool(result.get('passed'))} "
            f"opening_ok={bool(result.get('opening', {}).get('ok'))} "
            f"plan_ok={bool(result.get('plan', {}).get('ok'))} "
            f"theme_ok={bool(result.get('plan', {}).get('theme_ok'))} "
            f"dialog_execute_ok={bool(result.get('approval', {}).get('dialog_execute_ok'))} "
            f"operator_execute_ok={bool(result.get('operator_execute', {}).get('ok'))}"
        )

        if args.delay_seconds > 0 and index + 1 < total:
            time.sleep(max(0.0, float(args.delay_seconds)))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "count": total,
        "summary": dict(counts),
        "results": results,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"REPORT {output_path}")
    print(f"PASSED {counts.get('passed', 0)}")
    print(f"FAILED {counts.get('failed', 0)}")
    print(f"OPENING_OK {counts.get('opening_ok', 0)}")
    print(f"PLAN_OK {counts.get('plan_ok', 0)}")
    print(f"THEME_OK {counts.get('theme_ok', 0)}")
    print(f"DIALOG_EXECUTE_OK {counts.get('dialog_execute_ok', 0)}")
    print(f"OPERATOR_EXECUTE_OK {counts.get('operator_execute_ok', 0)}")

    return 0 if counts.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())