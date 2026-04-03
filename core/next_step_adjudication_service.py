from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SHARED_ROOT = PROJECT_ROOT / "runtime" / "shared"

SAFE_INTERFACE_INTENTS = {
    "direct_inquiry",
    "next_step_direct_inquiry",
    "next_step_request",
    "next_tod_tasks_inquiry",
    "path_validation_request",
    "status_validation_request",
}
READ_ONLY_ACTION_TYPES = {
    "diagnose",
    "inquire",
    "observe",
    "read",
    "rebuild",
    "refresh",
    "status",
    "validate",
}
EXECUTION_KEYWORDS = {
    "ack",
    "arm",
    "bridge",
    "dispatch",
    "execute",
    "execution",
    "go order",
    "live arm",
    "listener",
    "publish",
    "reissue",
    "restart",
    "result handling",
    "shared ingress",
    "shared egress",
    "task execution",
    "trigger",
}
INQUIRY_KEYWORDS = {
    "canonical pass",
    "direct inquiry",
    "next step",
    "next tod tasks",
    "status rebuild",
    "validate",
    "validation",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_preferred_review(shared_root: Path) -> dict[str, Any]:
    incident = _read_json(shared_root / "MIM_OPERATOR_INCIDENT.latest.json")
    if incident.get("active") is True:
        review_path = str(incident.get("review_path") or "").strip()
        precedence = str(incident.get("precedence") or "").strip()
        if review_path and precedence == "prefer_incident_over_latest":
            incident_review = _read_json(Path(review_path))
            if incident_review:
                return incident_review
    return _read_json(shared_root / "MIM_TASK_STATUS_REVIEW.latest.json")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _normalize_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _contains_any(text: str, needles: set[str]) -> bool:
    lowered = _normalize_text(text)
    return any(needle in lowered for needle in needles)


def load_local_posture(shared_root: Path = DEFAULT_SHARED_ROOT) -> dict[str, Any]:
    review = _read_preferred_review(shared_root)
    alerts = _read_json(shared_root / "MIM_SYSTEM_ALERTS.latest.json")
    gate = _read_json(shared_root / "TOD_CATCHUP_GATE.latest.json")
    arm = _read_json(shared_root / "mim_arm_control_readiness.latest.json")
    task = review.get("task", {}) if isinstance(review.get("task", {}), dict) else {}
    tod_readiness = arm.get("tod_readiness", {}) if isinstance(arm.get("tod_readiness", {}), dict) else {}
    control = arm.get("control", {}) if isinstance(arm.get("control", {}), dict) else {}
    return {
        "evaluated_at": _utc_now(),
        "active_task_id": str(task.get("active_task_id") or "").strip(),
        "objective_id": str(task.get("objective_id") or "").strip(),
        "review_state": str(review.get("state") or "unknown").strip(),
        "review_reason": str(review.get("state_reason") or "").strip(),
        "gate_pass": bool((review.get("gate", {}) or {}).get("pass") or gate.get("gate_pass")),
        "promotion_ready": bool((review.get("gate", {}) or {}).get("promotion_ready") or gate.get("promotion_ready")),
        "system_alerts_active": bool(alerts.get("active", False)),
        "highest_severity": str(alerts.get("highest_severity") or "none").strip(),
        "blocking_reason_codes": list(review.get("blocking_reason_codes") or []),
        "arm_operator_approval_required": bool(
            arm.get("operator_approval_required")
            or control.get("operator_approval_required")
        ),
        "tod_execution_allowed": bool(
            arm.get("tod_execution_allowed", tod_readiness.get("allowed", True))
        ),
        "tod_execution_block_reason": str(
            arm.get("tod_execution_block_reason")
            or tod_readiness.get("block_reason")
            or ""
        ).strip(),
    }


def classify_next_step_item(item: dict[str, Any]) -> dict[str, Any]:
    description = str(item.get("description") or "").strip()
    owner_workspace = _normalize_text(item.get("owner_workspace") or "mim")
    action_type = _normalize_text(item.get("action_type") or "")
    risk = _normalize_text(item.get("risk") or "medium")
    metadata = item.get("metadata_json", {}) if isinstance(item.get("metadata_json", {}), dict) else {}
    cross_system = bool(item.get("cross_system", False) or metadata.get("cross_system", False))
    approval_required = bool(item.get("approval_required", False) or metadata.get("approval_required", False))
    touches_execution = (
        bool(metadata.get("execution_authority_involved", False))
        or bool(metadata.get("live_execution", False))
        or _contains_any(description, EXECUTION_KEYWORDS)
        or action_type not in READ_ONLY_ACTION_TYPES
    )
    touches_live_arm = "arm" in _normalize_text(description) or bool(metadata.get("live_arm_execution", False))
    inquiry_only = (
        action_type in READ_ONLY_ACTION_TYPES
        and not touches_execution
    ) or _contains_any(description, INQUIRY_KEYWORDS)
    local_only = owner_workspace in {"", "mim"} and not cross_system
    requires_tod_input = (
        owner_workspace == "tod"
        or cross_system
        or bool(metadata.get("bridge_state_involved", False))
        or bool(metadata.get("execution_authority_involved", False))
        or touches_live_arm
        or _contains_any(description, {"ack", "bridge", "listener", "result", "trigger"})
    )
    return {
        "owner_workspace": owner_workspace or "mim",
        "action_type": action_type or "unknown",
        "risk": risk or "medium",
        "cross_system": cross_system,
        "approval_required": approval_required,
        "local_only": local_only,
        "requires_tod_input": requires_tod_input,
        "touches_execution": touches_execution,
        "touches_live_arm": touches_live_arm,
        "inquiry_only": inquiry_only,
    }


def adjudicate_next_step_item(
    item: dict[str, Any],
    posture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    posture = posture if isinstance(posture, dict) else load_local_posture()
    classification = classify_next_step_item(item)
    blockers: list[str] = []
    if classification["touches_execution"] and posture.get("system_alerts_active"):
        blockers.append("system_alerts_active")
    if classification["touches_execution"] and not posture.get("gate_pass", False):
        blockers.append("tod_catchup_gate_false")
    if classification["touches_live_arm"] and posture.get("arm_operator_approval_required"):
        blockers.append("operator_approval_required")
    if classification["touches_execution"] and not posture.get("tod_execution_allowed", True):
        blockers.append(str(posture.get("tod_execution_block_reason") or "tod_execution_not_allowed"))

    mim_decision = "approve"
    item_posture = "auto_execute_candidate"
    reason = "Low-risk local step is eligible for automatic progression from MIM."

    if blockers:
        if "operator_approval_required" in blockers:
            item_posture = "approval_required"
            reason = "Execution-affecting step remains gated by operator approval on the MIM side."
        else:
            mim_decision = "block"
            item_posture = "blocked"
            reason = "Local runtime posture blocks this execution-affecting step until health or gate conditions recover."
    elif classification["approval_required"] or classification["touches_live_arm"]:
        item_posture = "approval_required"
        reason = "This step touches governed execution and must remain approval-gated."
    elif classification["requires_tod_input"]:
        item_posture = "proposal_only"
        reason = "MIM can approve the inquiry posture, but TOD input is required before any cross-workspace action proceeds."
    elif classification["risk"] not in {"", "low"}:
        item_posture = "proposal_only"
        reason = "The step is not low-risk enough for auto-execution even though it is local-only."

    return {
        "step_id": str(item.get("step_id") or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "owner_workspace": classification["owner_workspace"],
        "mim_decision": mim_decision,
        "reason": reason,
        "local_blockers": blockers,
        "requires_tod_input": classification["requires_tod_input"],
        "posture": item_posture,
        "auto_execute_candidate": item_posture == "auto_execute_candidate",
        "classification": classification,
    }


def build_mim_adjudication(
    next_steps_payload: dict[str, Any],
    posture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    posture = posture if isinstance(posture, dict) else load_local_posture()
    items = next_steps_payload.get("items", []) if isinstance(next_steps_payload.get("items", []), list) else []
    adjudicated_items = [adjudicate_next_step_item(item, posture) for item in items if isinstance(item, dict)]
    return {
        "source_workspace": "MIM",
        "objective_id": str(next_steps_payload.get("objective_id") or posture.get("objective_id") or "").strip(),
        "evaluated_at": _utc_now(),
        "source_run": str(next_steps_payload.get("run_id") or "").strip(),
        "local_posture": posture,
        "items": adjudicated_items,
    }


def build_next_step_consensus(
    next_steps_payload: dict[str, Any],
    mim_adjudication: dict[str, Any],
    tod_adjudication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tod_items = {}
    if isinstance(tod_adjudication, dict):
        for item in tod_adjudication.get("items", []):
            if isinstance(item, dict):
                step_id = str(item.get("step_id") or "").strip()
                if step_id:
                    tod_items[step_id] = item

    consensus_items: list[dict[str, Any]] = []
    for item in mim_adjudication.get("items", []):
        if not isinstance(item, dict):
            continue
        step_id = str(item.get("step_id") or "").strip()
        tod_item = tod_items.get(step_id, {})
        posture = str(item.get("posture") or "proposal_only").strip()
        owner_workspace = str(item.get("owner_workspace") or "mim").strip()
        tod_decision = str(tod_item.get("tod_decision") or tod_item.get("decision") or "").strip()
        consensus_action = "proposal_only"
        if posture == "blocked":
            consensus_action = "blocked"
        elif posture == "approval_required":
            consensus_action = "approval_required"
        elif owner_workspace == "mim" and not item.get("requires_tod_input"):
            consensus_action = "auto_execute" if item.get("auto_execute_candidate") else "proposal_only"
        elif not tod_decision:
            consensus_action = "await_tod"
        elif tod_decision == "approve" and item.get("auto_execute_candidate"):
            consensus_action = "auto_execute"
        elif tod_decision == "approve":
            consensus_action = "proposal_only"
        else:
            consensus_action = "blocked"

        consensus_items.append(
            {
                "step_id": step_id,
                "mim_decision": str(item.get("mim_decision") or "").strip(),
                "tod_decision": tod_decision,
                "consensus_action": consensus_action,
                "owner_workspace": owner_workspace,
                "approval_required": posture == "approval_required",
            }
        )

    return {
        "objective_id": str(next_steps_payload.get("objective_id") or "").strip(),
        "source_run": str(next_steps_payload.get("run_id") or "").strip(),
        "generated_at": _utc_now(),
        "items": consensus_items,
    }


def build_interface_auto_approval_decision(
    *,
    parsed_intent: str,
    content: str,
    metadata_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    classification = classify_next_step_item(
        {
            "description": content,
            "owner_workspace": metadata.get("owner_workspace", "tod"),
            "action_type": metadata.get("action_type", "inquire"),
            "risk": metadata.get("risk", "low"),
            "cross_system": metadata.get("cross_system", True),
            "approval_required": metadata.get("approval_required", False),
            "metadata_json": metadata,
        }
    )
    normalized_intent = _normalize_text(parsed_intent)
    safe_intent = normalized_intent in SAFE_INTERFACE_INTENTS or normalized_intent.endswith("_inquiry")
    inquiry_only = classification["inquiry_only"] or _contains_any(content, INQUIRY_KEYWORDS)

    if classification["approval_required"] or classification["touches_execution"] or classification["touches_live_arm"]:
        return {
            "auto_approve": False,
            "decision": "manual_review",
            "reason": "The request affects execution or governed control and must remain human-reviewed.",
        }
    if safe_intent and inquiry_only:
        return {
            "auto_approve": True,
            "decision": "approved",
            "reason": "Low-risk next-step inquiry is safe for prompt MIM acknowledgment because it does not request execution.",
            "metadata_json": {
                "objective_id": "98A",
                "auto_generated": True,
                "adjudication_posture": "auto_execute_candidate",
            },
        }
    return {
        "auto_approve": False,
        "decision": "manual_review",
        "reason": "The request does not match the low-risk next-step inquiry profile.",
    }


def publish_next_step_artifacts(
    *,
    next_steps_payload: dict[str, Any],
    shared_root: Path = DEFAULT_SHARED_ROOT,
    tod_adjudication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    posture = load_local_posture(shared_root=shared_root)
    mim_adjudication = build_mim_adjudication(next_steps_payload, posture=posture)
    consensus = build_next_step_consensus(
        next_steps_payload,
        mim_adjudication,
        tod_adjudication=tod_adjudication,
    )
    next_steps_path = shared_root / "mim_codex_next_steps.latest.json"
    mim_path = shared_root / "mim_next_step_adjudication.latest.json"
    consensus_path = shared_root / "NEXT_STEP_CONSENSUS.latest.json"
    _write_json(next_steps_path, next_steps_payload)
    _write_json(mim_path, mim_adjudication)
    _write_json(consensus_path, consensus)
    return {
        "next_steps_path": str(next_steps_path),
        "mim_adjudication_path": str(mim_path),
        "consensus_path": str(consensus_path),
        "mim_adjudication": mim_adjudication,
        "consensus": consensus,
    }