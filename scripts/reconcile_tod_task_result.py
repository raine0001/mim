#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def first_text(payload: dict, *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def nested_text(payload: dict, *path: str) -> str:
    current: object = payload
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return str(current or "").strip()


def normalize_objective(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return text
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits if digits else text


def build_payload(shared_dir: Path) -> dict:
    result_path = shared_dir / "TOD_MIM_TASK_RESULT.latest.json"
    request_path = shared_dir / "MIM_TOD_TASK_REQUEST.latest.json"
    task_ack_path = shared_dir / "TOD_MIM_TASK_ACK.latest.json"
    review_decision_path = shared_dir / "MIM_TOD_REVIEW_DECISION.latest.json"
    review_path = shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json"

    result = read_json(result_path)
    request = read_json(request_path)
    task_ack = read_json(task_ack_path)
    review_decision = read_json(review_decision_path)
    review = read_json(review_path)
    review_task = review.get("task") if isinstance(review.get("task"), dict) else {}
    existing_review_gate = result.get("review_gate") if isinstance(result.get("review_gate"), dict) else {}
    ack_bridge_processing = task_ack.get("bridge_runtime") if isinstance(task_ack.get("bridge_runtime"), dict) else {}
    ack_current_processing = ack_bridge_processing.get("current_processing") if isinstance(ack_bridge_processing.get("current_processing"), dict) else {}

    active_task_id = (
        first_text(request, "task_id", "request_id")
        or first_text(review_task, "active_task_id", "request_task_id")
        or first_text(task_ack, "request_id", "task_id")
        or first_text(ack_current_processing, "task_id", "request_id")
    )
    objective_id = (
        normalize_objective(request.get("objective_id"))
        or normalize_objective(review_task.get("objective_id"))
        or normalize_objective(review_decision.get("objective_id"))
        or normalize_objective(result.get("objective_id"))
    )
    correlation_id = (
        first_text(request, "correlation_id")
        or first_text(ack_current_processing, "correlation_id")
        or first_text(review_decision, "correlation_id")
        or first_text(result, "correlation_id")
        or (f"obj{objective_id}-{active_task_id}" if objective_id and active_task_id else "")
    )

    decision_task_id = first_text(review_decision, "task_id")
    decision_value = first_text(review_decision, "decision").lower()
    existing_request_id = first_text(result, "request_id", "task_id")
    stale_result_rebound = bool(active_task_id and existing_request_id and existing_request_id != active_task_id)
    existing_review_passed = existing_review_gate.get("passed") is True and existing_request_id == active_task_id
    accepted_review = bool(active_task_id and decision_task_id == active_task_id and decision_value == "accepted")
    review_passed = existing_review_passed or accepted_review

    request_id = active_task_id or existing_request_id

    result_status = first_text(result, "result_status", "status")
    status = first_text(result, "status", "result_status")
    if stale_result_rebound and not review_passed:
        # Keep stale rebound packets contract-valid for downstream listeners.
        # An empty top-level status with a populated result_status is now treated
        # as a runtime contract violation on the TOD side.
        status = result_status or status
    if not status and review_passed:
        status = "completed"

    review_gate_reason = "current_review_decision_accepted" if accepted_review else "review_gate_unresolved"
    review_gate = {
        "passed": review_passed,
        "reason": review_gate_reason,
        "decision": first_text(review_decision, "decision") or first_text(existing_review_gate, "decision"),
        "task_id": decision_task_id or active_task_id,
        "request_id": request_id,
        "generated_at": utc_now(),
        "source": "mim_tod_review_decision" if accepted_review else str(existing_review_gate.get("source") or "reconciled_shared_truth"),
        "decision_rationale": first_text(review_decision, "decision_rationale") or str(existing_review_gate.get("decision_rationale") or ""),
    }

    payload = dict(result)
    payload.update(
        {
            "generated_at": utc_now(),
            "packet_type": str(result.get("packet_type") or "tod-mim-task-result-v1"),
            "handshake_version": str(result.get("handshake_version") or "mim-tod-shared-export-v1"),
            "source": str(result.get("source") or "tod-mim-task-result-v1"),
            "request_id": request_id,
            "task_id": request_id or active_task_id,
            "task": request_id or active_task_id,
            "objective_id": objective_id,
            "objective": str(result.get("objective") or (f"objective-{objective_id}" if objective_id else "")),
            "correlation_id": correlation_id,
            "status": status,
            "result_status": result_status,
            "review_gate": review_gate,
            "reconciliation": {
                "generated_at": utc_now(),
                "type": "tod_task_result_reconciliation_v1",
                "active_task_id": active_task_id,
                "review_decision_task_id": decision_task_id,
                "review_decision_current": accepted_review,
                "existing_request_id": existing_request_id,
                "stale_result_rebound": stale_result_rebound,
                "review_passed": review_passed,
            },
        }
    )
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconcile TOD task-result fields from current shared truth.")
    parser.add_argument("--shared-dir", default=str(Path(__file__).resolve().parents[1] / "runtime" / "shared"))
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shared_dir = Path(args.shared_dir).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve() if args.output else shared_dir / "TOD_MIM_TASK_RESULT.latest.json"
    payload = build_payload(shared_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, payload)
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())