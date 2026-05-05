from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core.primitive_request_recovery_service import load_authoritative_request_status
from core.routers import mim_ui
from tod_status_signal_lib import build_task_status_review


SUCCESS_RESULT_STATUSES = {"completed", "succeeded", "approved", "done"}
REPORT_BASENAME = "mim_tod_coordination_simulation"
DEFAULT_OUTPUT_DIR = ROOT / "runtime" / "reports"


@dataclass(frozen=True)
class LaneIds:
    objective_id: str
    objective_token: str
    task_id: str
    request_id: str
    correlation_id: str
    wrong_task_id: str
    stale_task_id: str
    wrapper_request_id: str


@dataclass(frozen=True)
class ScenarioDefinition:
    name: str
    description: str
    builder: Callable[[LaneIds, datetime], dict[str, Any]]


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _task_request(ids: LaneIds, generated_at: datetime) -> dict[str, Any]:
    return {
        "generated_at": _utc(generated_at),
        "request_id": ids.request_id,
        "task_id": ids.task_id,
        "objective_id": ids.objective_id,
        "correlation_id": ids.correlation_id,
        "request_status": "queued",
        "action_name": "simulate_coordination_lane",
    }


def _trigger(ids: LaneIds, generated_at: datetime, *, task_id: str | None = None) -> dict[str, Any]:
    return {
        "generated_at": _utc(generated_at),
        "trigger": "task_request_posted",
        "task_id": task_id or ids.task_id,
        "request_id": ids.request_id,
        "objective_id": ids.objective_id,
    }


def _trigger_ack(generated_at: datetime, *, task_id: str) -> dict[str, Any]:
    return {
        "generated_at": _utc(generated_at),
        "task_id": task_id,
    }


def _task_ack(ids: LaneIds, generated_at: datetime, *, task_id: str | None = None, request_id: str | None = None, status: str = "accepted") -> dict[str, Any]:
    effective_task_id = task_id or ids.task_id
    effective_request_id = request_id or ids.request_id
    return {
        "generated_at": _utc(generated_at),
        "request_id": effective_request_id,
        "task_id": effective_task_id,
        "objective_id": ids.objective_id,
        "correlation_id": ids.correlation_id,
        "status": status,
        "bridge_runtime": {
            "current_processing": {
                "task_id": effective_task_id,
                "request_id": effective_request_id,
                "correlation_id": ids.correlation_id,
            }
        },
    }


def _task_result(
    ids: LaneIds,
    generated_at: datetime,
    *,
    task_id: str | None = None,
    request_id: str | None = None,
    status: str = "succeeded",
    result_status: str | None = None,
    current_processing_task_id: str | None = None,
    include_reconciliation: bool = True,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective_task_id = task_id or ids.task_id
    effective_request_id = request_id or ids.request_id
    payload: dict[str, Any] = {
        "generated_at": _utc(generated_at),
        "request_id": effective_request_id,
        "task_id": effective_task_id,
        "objective_id": ids.objective_id,
        "correlation_id": ids.correlation_id,
        "status": status,
        "result_status": result_status or status,
        "result_reason": f"{status}_simulation",
        "bridge_runtime": {
            "current_processing": {
                "task_id": current_processing_task_id or effective_task_id,
                "request_id": effective_request_id,
                "correlation_id": ids.correlation_id,
            }
        },
    }
    if include_reconciliation:
        payload["reconciliation"] = {
            "review_passed": True,
            "review_decision_current": True,
            "existing_task_id": effective_task_id,
        }
    if extra:
        payload.update(extra)
    return payload


def _integration(ids: LaneIds, *, live_task_id: str | None = None, live_objective_id: str | None = None, normalized_objective_id: str | None = None, promotion_applied: bool = True) -> dict[str, Any]:
    return {
        "mim_status": {"objective_active": ids.objective_token},
        "mim_handshake": {"current_next_objective": ids.objective_token},
        "objective_alignment": {
            "mim_objective_active": ids.objective_token,
            "tod_current_objective": normalized_objective_id or ids.objective_token,
            "status": "in_sync",
        },
        "live_task_request": {
            "task_id": live_task_id or ids.task_id,
            "request_id": ids.request_id,
            "objective_id": live_objective_id or ids.objective_id,
            "normalized_objective_id": normalized_objective_id or ids.objective_token,
            "promotion_applied": promotion_applied,
        },
    }


def _decision(generated_at: datetime, *, execution_state: str = "ready_to_execute", decision_outcome: str = "execute", summary: str = "Execution is ready.") -> dict[str, Any]:
    return {
        "generated_at": _utc(generated_at),
        "execution_state": execution_state,
        "decision_outcome": decision_outcome,
        "summary": summary,
    }


def _catchup_gate(generated_at: datetime, *, gate_pass: bool = True) -> dict[str, Any]:
    return {
        "generated_at": _utc(generated_at),
        "promotion_ready": gate_pass,
        "gate_pass": gate_pass,
    }


def _troubleshooting_authority() -> dict[str, Any]:
    return {
        "authority": {
            "mim": {"permissions": ["read", "write"]},
            "tod": {"permissions": ["read", "write"]},
        },
        "enforcement": {
            "access_failure_action": "allow",
            "reason_code": "",
        },
    }


def _persistent_task(ids: LaneIds, *, status: str = "queued") -> dict[str, Any]:
    return {
        "task_id": ids.task_id,
        "request_id": ids.request_id,
        "objective_id": ids.objective_id,
        "status": status,
        "title": ids.task_id,
    }


def _fallback(ids: LaneIds, generated_at: datetime, *, task_id: str | None = None, request_id: str | None = None, correlation_id: str | None = None) -> dict[str, Any]:
    return {
        "generated_at": _utc(generated_at),
        "objective_id": ids.objective_id,
        "task_id": task_id or ids.task_id,
        "request_id": request_id or ids.request_id,
        "correlation_id": correlation_id or ids.correlation_id,
        "execution_state": "running",
        "decision_outcome": "mim_direct_execution_takeover",
        "summary": "MIM claimed bounded fallback authority and is executing the active task locally.",
    }


def _wrong_task_review(ids: LaneIds, generated_at: datetime) -> dict[str, Any]:
    return {
        "generated_at": _utc(generated_at),
        "state": "completed",
        "state_reason": "task_result_current",
        "task": {
            "active_task_id": ids.wrong_task_id,
            "request_task_id": ids.wrong_task_id,
            "request_request_id": f"{ids.wrong_task_id}-request",
            "result_task_id": ids.wrong_task_id,
            "result_request_id": f"{ids.wrong_task_id}-request",
            "result_status": "succeeded",
            "objective_id": ids.objective_token,
        },
    }


def _scenario_normal(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=3)),
        "trigger": _trigger(ids, now - timedelta(minutes=3)),
        "trigger_ack": _trigger_ack(now - timedelta(minutes=2), task_id=ids.task_id),
        "task_ack": _task_ack(ids, now - timedelta(minutes=2)),
        "task_result": _task_result(ids, now - timedelta(minutes=1), status="succeeded"),
        "integration": _integration(ids),
        "decision": _decision(now - timedelta(seconds=45)),
        "expectations": {
            "accepted": True,
            "execution_confirmed": True,
            "forbid_idle_blocked": True,
            "fallback_mutation_forbidden": True,
        },
    }


def _scenario_delayed_ack(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=7)),
        "trigger": _trigger(ids, now - timedelta(minutes=7)),
        "trigger_ack": _trigger_ack(now - timedelta(minutes=6), task_id=ids.task_id),
        "task_ack": _task_ack(ids, now - timedelta(seconds=30)),
        "task_result": _task_result(ids, now - timedelta(seconds=10), status="succeeded"),
        "integration": _integration(ids),
        "decision": _decision(now - timedelta(seconds=9)),
        "expectations": {
            "accepted": True,
            "execution_confirmed": True,
            "forbid_idle_blocked": True,
        },
    }


def _scenario_missing_ack(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=4)),
        "trigger": _trigger(ids, now - timedelta(minutes=4)),
        "trigger_ack": _trigger_ack(now - timedelta(minutes=3), task_id=ids.task_id),
        "task_result": _task_result(ids, now - timedelta(seconds=15), status="succeeded"),
        "integration": _integration(ids),
        "decision": _decision(now - timedelta(seconds=14)),
        "expectations": {
            "accepted": True,
            "execution_confirmed": True,
            "forbid_idle_blocked": True,
        },
    }


def _scenario_wrong_task_ack(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=5)),
        "trigger": _trigger(ids, now - timedelta(minutes=5)),
        "trigger_ack": _trigger_ack(now - timedelta(minutes=4), task_id=ids.wrong_task_id),
        "task_ack": _task_ack(ids, now - timedelta(minutes=4), task_id=ids.wrong_task_id, request_id=f"{ids.wrong_task_id}-request"),
        "integration": _integration(ids, live_task_id=ids.wrong_task_id),
        "decision": _decision(now - timedelta(minutes=3), execution_state="waiting_on_dependency", decision_outcome="acknowledge_and_wait_on_dependency", summary="ACK does not match the active task."),
        "expectations": {
            "accepted": False,
            "execution_confirmed": False,
            "wrong_task_rejected": True,
            "forbid_idle_blocked": True,
        },
    }


def _scenario_wrong_task_result(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=6)),
        "trigger": _trigger(ids, now - timedelta(minutes=6)),
        "task_result": _task_result(ids, now - timedelta(seconds=25), task_id=ids.wrong_task_id, request_id=f"{ids.wrong_task_id}-request", current_processing_task_id=ids.wrong_task_id, status="succeeded", extra={"reconciliation": {"review_passed": False, "review_decision_current": False, "existing_task_id": ids.task_id}}),
        "integration": _integration(ids, live_task_id=ids.wrong_task_id),
        "decision": _decision(now - timedelta(seconds=20), execution_state="waiting_on_dependency", decision_outcome="acknowledge_and_wait_on_dependency", summary="Result task does not match the active task."),
        "expectations": {
            "accepted": False,
            "execution_confirmed": False,
            "wrong_task_rejected": True,
            "forbid_idle_blocked": True,
            "expect_lineage_mismatch": True,
        },
    }


def _scenario_stale_guard(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=3)),
        "trigger": _trigger(ids, now - timedelta(minutes=3)),
        "task_ack": _task_ack(ids, now - timedelta(minutes=2)),
        "task_result": _task_result(ids, now - timedelta(seconds=20), status="succeeded"),
        "command_status": {
            "status": "contract_violation_rejected",
            "request_id": ids.request_id,
            "task_id": ids.task_id,
            "decision": {
                "reason_code": "stale_guard_high_watermark",
                "requires_human": False,
                "summary": "Stale guard detected a higher watermark but did not override the active task.",
            },
            "stale_guard": {
                "detected": True,
                "status": "execution_blocked_by_stale_guard",
                "reason": "higher_authoritative_task_ordinal_active",
                "current_request": {"request_id": ids.request_id, "task_id": ids.task_id},
                "high_watermark": {"request_id": f"{ids.objective_id}-task-999999", "ordinal": 999999},
            },
        },
        "integration": _integration(ids),
        "decision": _decision(now - timedelta(seconds=18)),
        "expectations": {
            "accepted": True,
            "execution_confirmed": True,
            "stale_guard_warning_only": True,
            "forbid_idle_blocked": True,
        },
    }


def _scenario_stale_ack_result(ids: LaneIds, now: datetime) -> dict[str, Any]:
    stale_request_id = f"{ids.stale_task_id}-request"
    return {
        "request": _task_request(ids, now - timedelta(minutes=1)),
        "trigger": _trigger(ids, now - timedelta(minutes=1)),
        "trigger_ack": _trigger_ack(now - timedelta(hours=2), task_id=ids.stale_task_id),
        "task_ack": _task_ack(ids, now - timedelta(hours=2), task_id=ids.stale_task_id, request_id=stale_request_id),
        "task_result": _task_result(ids, now - timedelta(hours=2), task_id=ids.stale_task_id, request_id=stale_request_id, current_processing_task_id=ids.stale_task_id, status="succeeded"),
        "decision": _decision(now - timedelta(seconds=15), execution_state="waiting_on_dependency", decision_outcome="acknowledge_and_wait_on_dependency", summary="Only stale ACK/result artifacts are present."),
        "integration": _integration(ids, live_task_id=ids.stale_task_id),
        "expectations": {
            "accepted": False,
            "execution_confirmed": False,
            "stale_lineage_rejected": True,
            "forbid_idle_blocked": True,
            "expect_lineage_mismatch": True,
        },
    }


def _scenario_objective_match_task_mismatch(ids: LaneIds, now: datetime) -> dict[str, Any]:
    wrong_request_id = f"{ids.wrong_task_id}-request"
    return {
        "request": _task_request(ids, now - timedelta(minutes=8)),
        "trigger": _trigger(ids, now - timedelta(minutes=8), task_id=ids.wrong_task_id),
        "trigger_ack": _trigger_ack(now - timedelta(minutes=7), task_id=ids.wrong_task_id),
        "task_ack": _task_ack(ids, now - timedelta(minutes=6), task_id=ids.wrong_task_id, request_id=wrong_request_id),
        "task_result": _task_result(ids, now - timedelta(minutes=5), task_id=ids.wrong_task_id, request_id=wrong_request_id, current_processing_task_id=ids.wrong_task_id, status="succeeded"),
        "integration": _integration(ids, live_task_id=ids.wrong_task_id),
        "decision": _decision(now - timedelta(minutes=4), execution_state="waiting_on_dependency", decision_outcome="acknowledge_and_wait_on_dependency", summary="Objective matches, but task lineage does not."),
        "expectations": {
            "accepted": False,
            "execution_confirmed": False,
            "objective_only_mismatch": True,
            "forbid_idle_blocked": True,
            "expect_lineage_mismatch": True,
        },
    }


def _scenario_wrapper_only_result(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=6)),
        "trigger": _trigger(ids, now - timedelta(minutes=6)),
        "task_result": {
            "generated_at": _utc(now - timedelta(minutes=5)),
            "request_id": ids.wrapper_request_id,
            "status": "succeeded",
            "result_status": "succeeded",
            "summary": "Wrapper script reported completion without active task lineage.",
            "bridge_runtime": {
                "current_processing": {
                    "task_id": "",
                    "request_id": ids.wrapper_request_id,
                }
            },
        },
        "integration": _integration(ids),
        "decision": _decision(now - timedelta(minutes=4), execution_state="waiting_on_dependency", decision_outcome="acknowledge_and_wait_on_dependency", summary="Wrapper-only result cannot be trusted."),
        "expectations": {
            "accepted": False,
            "execution_confirmed": False,
            "wrapper_only_rejected": True,
            "forbid_idle_blocked": True,
            "expect_lineage_mismatch": True,
        },
    }


def _scenario_stuck_current_processing(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=14)),
        "trigger": _trigger(ids, now - timedelta(minutes=14)),
        "task_ack": _task_ack(ids, now - timedelta(minutes=13)),
        "task_result": _task_result(ids, now - timedelta(minutes=12), current_processing_task_id=ids.stale_task_id, status="failed", extra={"reconciliation": {"review_passed": False, "review_decision_current": False, "existing_task_id": ids.task_id}}),
        "decision": _decision(now - timedelta(minutes=10), execution_state="waiting_on_dependency", decision_outcome="acknowledge_and_wait_on_dependency", summary="Current processing is stuck on a stale task."),
        "integration": _integration(ids),
        "expectations": {
            "accepted": False,
            "execution_confirmed": False,
            "stale_lineage_rejected": True,
        },
    }


def _scenario_tod_silence(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=16)),
        "trigger": _trigger(ids, now - timedelta(minutes=16)),
        "decision": _decision(now - timedelta(minutes=15), execution_state="waiting_on_dependency", decision_outcome="acknowledge_and_wait_on_dependency", summary="TOD has gone silent on the active lane."),
        "integration": _integration(ids),
        "expectations": {
            "accepted": False,
            "execution_confirmed": False,
            "expect_direct_execution_ready": True,
        },
    }


def _scenario_replay_same_task(ids: LaneIds, now: datetime) -> dict[str, Any]:
    result = _task_result(ids, now - timedelta(seconds=20), status="succeeded", extra={"replay": {"requested": True, "replay_source_request_id": ids.request_id}})
    return {
        "request": _task_request(ids, now - timedelta(minutes=2)),
        "trigger": _trigger(ids, now - timedelta(minutes=2)),
        "trigger_ack": _trigger_ack(now - timedelta(minutes=1), task_id=ids.task_id),
        "task_ack": _task_ack(ids, now - timedelta(minutes=1)),
        "task_result": result,
        "integration": _integration(ids),
        "decision": _decision(now - timedelta(seconds=18)),
        "expectations": {
            "accepted": True,
            "execution_confirmed": True,
            "replay_same_task": True,
            "forbid_idle_blocked": True,
        },
    }


def _scenario_mim_fallback_same_task(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=11)),
        "trigger": _trigger(ids, now - timedelta(minutes=11)),
        "fallback": _fallback(ids, now - timedelta(seconds=15)),
        "integration": _integration(ids),
        "decision": _decision(now - timedelta(seconds=15), execution_state="blocked", decision_outcome="acknowledge_and_wait_on_dependency", summary="MIM is executing the active task locally after TOD silence."),
        "expectations": {
            "accepted": True,
            "execution_confirmed": True,
            "expect_fallback_active": True,
            "fallback_mutation_forbidden": True,
        },
    }


def _scenario_competing_publisher(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=9)),
        "trigger": _trigger(ids, now - timedelta(minutes=9), task_id=ids.wrong_task_id),
        "trigger_ack": _trigger_ack(now - timedelta(minutes=8), task_id=ids.wrong_task_id),
        "decision": _decision(now - timedelta(minutes=7), execution_state="waiting_on_dependency", decision_outcome="acknowledge_and_wait_on_dependency", summary="A competing publisher replaced the active task on the trigger lane."),
        "integration": _integration(ids, live_task_id=ids.wrong_task_id),
        "expectations": {
            "accepted": False,
            "execution_confirmed": False,
            "wrong_task_rejected": True,
            "forbid_idle_blocked": True,
        },
    }


def _scenario_stale_ui_mirror(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=4)),
        "trigger": _trigger(ids, now - timedelta(minutes=4)),
        "task_ack": _task_ack(ids, now - timedelta(minutes=3)),
        "task_result": _task_result(ids, now - timedelta(seconds=25), status="succeeded"),
        "integration": _integration(ids, live_task_id=ids.stale_task_id, live_objective_id=f"objective-{int(ids.objective_token) - 1}", normalized_objective_id=ids.objective_token),
        "decision": _decision(now - timedelta(seconds=24), summary="Mirror artifacts are stale, but the normalized active objective is current."),
        "expectations": {
            "accepted": True,
            "execution_confirmed": True,
            "forbid_idle_blocked": True,
        },
    }


def _scenario_wrong_task_review(ids: LaneIds, now: datetime) -> dict[str, Any]:
    return {
        "request": _task_request(ids, now - timedelta(minutes=5)),
        "trigger": _trigger(ids, now - timedelta(minutes=5)),
        "task_ack": _task_ack(ids, now - timedelta(minutes=4)),
        "task_result": _task_result(ids, now - timedelta(minutes=3), status="succeeded"),
        "review": _wrong_task_review(ids, now - timedelta(minutes=2)),
        "integration": _integration(ids),
        "decision": _decision(now - timedelta(minutes=2), execution_state="waiting_on_dependency", decision_outcome="acknowledge_and_wait_on_dependency", summary="A stale review artifact referenced a different task."),
        "expectations": {
            "accepted": True,
            "execution_confirmed": False,
            "review_task_mismatch_advisory": True,
            "forbid_idle_blocked": True,
            "expect_lineage_mismatch": True,
        },
    }


SCENARIOS = [
    ScenarioDefinition("normal_tod_ack_result", "Normal same-task ACK and terminal result.", _scenario_normal),
    ScenarioDefinition("delayed_ack", "ACK arrives late but stays on the same task lineage.", _scenario_delayed_ack),
    ScenarioDefinition("missing_ack", "TOD never publishes ACK, but terminal result stays on the same task.", _scenario_missing_ack),
    ScenarioDefinition("wrong_task_ack", "TOD ACK targets a different task and must be rejected.", _scenario_wrong_task_ack),
    ScenarioDefinition("wrong_task_result", "TOD result targets a different task and must be rejected.", _scenario_wrong_task_result),
    ScenarioDefinition("stale_guard_high_watermark", "Stale guard metadata is warning-only and cannot override active lineage.", _scenario_stale_guard),
    ScenarioDefinition("stale_ack_result", "Only stale ACK/result artifacts are present for an older task.", _scenario_stale_ack_result),
    ScenarioDefinition("objective_match_task_mismatch", "Objective matches, but task lineage mismatches and cannot be accepted.", _scenario_objective_match_task_mismatch),
    ScenarioDefinition("wrapper_only_execution_result", "Wrapper-only result without active task lineage cannot be accepted.", _scenario_wrapper_only_result),
    ScenarioDefinition("stuck_current_processing", "Current processing stays pinned to a stale task and must not confirm completion.", _scenario_stuck_current_processing),
    ScenarioDefinition("tod_silence", "TOD goes silent long enough to arm bounded fallback.", _scenario_tod_silence),
    ScenarioDefinition("replay_same_task", "Replay stays on the same task lineage and remains acceptable.", _scenario_replay_same_task),
    ScenarioDefinition("mim_fallback_same_task", "MIM fallback preserves the same objective, task, request, and correlation ids.", _scenario_mim_fallback_same_task),
    ScenarioDefinition("competing_publisher", "A competing publisher mutates the trigger lane to a different task.", _scenario_competing_publisher),
    ScenarioDefinition("stale_ui_mirror_artifacts", "Stale UI or mirror artifacts must not defeat normalized active lineage.", _scenario_stale_ui_mirror),
    ScenarioDefinition("review_task_mismatch", "MIM_TASK_STATUS_REVIEW remains advisory when its task lineage does not match.", _scenario_wrong_task_review),
]


ARTIFACT_NAMES = {
    "request": "MIM_TOD_TASK_REQUEST.latest.json",
    "task_ack": "TOD_MIM_TASK_ACK.latest.json",
    "task_result": "TOD_MIM_TASK_RESULT.latest.json",
    "review": "MIM_TASK_STATUS_REVIEW.latest.json",
    "command_status": "TOD_MIM_COMMAND_STATUS.latest.json",
    "integration": "TOD_INTEGRATION_STATUS.latest.json",
    "truth": "TOD_EXECUTION_TRUTH.latest.json",
    "decision": "TOD_MIM_EXECUTION_DECISION.latest.json",
    "coordination_request": "TOD_MIM_COORDINATION_REQUEST.latest.json",
    "coordination_ack": "MIM_TOD_COORDINATION_ACK.latest.json",
    "fallback": "MIM_TOD_FALLBACK_ACTIVATION.latest.json",
}


def _lane_ids(index: int) -> LaneIds:
    objective_number = 4100 + (index % 275)
    objective_id = f"objective-{objective_number}"
    task_id = f"{objective_id}-task-{100000 + index}"
    request_id = f"{task_id}-request"
    correlation_id = f"corr-{objective_number}-{100000 + index}"
    wrong_task_id = f"{objective_id}-task-{200000 + index}"
    stale_task_id = f"{objective_id}-task-{300000 + index}"
    return LaneIds(
        objective_id=objective_id,
        objective_token=str(objective_number),
        task_id=task_id,
        request_id=request_id,
        correlation_id=correlation_id,
        wrong_task_id=wrong_task_id,
        stale_task_id=stale_task_id,
        wrapper_request_id=f"wrapper-{100000 + index}",
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _clear_shared_root(shared_root: Path) -> None:
    for artifact_name in ARTIFACT_NAMES.values():
        artifact_path = shared_root / artifact_name
        if artifact_path.exists():
            artifact_path.unlink()


def _write_lane(shared_root: Path, lane: dict[str, Any]) -> None:
    _clear_shared_root(shared_root)
    for key, artifact_name in ARTIFACT_NAMES.items():
        payload = lane.get(key)
        if isinstance(payload, dict) and payload:
            _write_json(shared_root / artifact_name, payload)


def _evaluate_lane(*, scenario: ScenarioDefinition, ids: LaneIds, lane: dict[str, Any], authoritative_request: dict[str, Any], snapshot: dict[str, Any], review: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    expectations = lane.get("expectations") if isinstance(lane.get("expectations"), dict) else {}
    failures: list[dict[str, Any]] = []
    counters = {
        "stale_lineage_accepted": 0,
        "wrong_task_completions_accepted": 0,
        "false_idle_blocked_from_task_mismatch": 0,
        "fallback_task_mutations": 0,
    }

    review_state = str(review.get("state") or "").strip().lower()
    accepted_complete = review_state == "completed"
    execution_confirmed = bool(snapshot.get("execution_confirmed"))
    confirmed = accepted_complete or execution_confirmed
    authoritative_result_status = str(authoritative_request.get("result_status") or "").strip().lower()

    if expectations.get("accepted") is True and not confirmed:
        failures.append({
            "scenario": scenario.name,
            "code": "expected_acceptance_missing",
            "detail": f"Expected acceptance for {ids.task_id}, but review.state={review_state!r} and execution_confirmed={execution_confirmed!r}.",
        })
    if expectations.get("accepted") is False and confirmed:
        failures.append({
            "scenario": scenario.name,
            "code": "unexpected_acceptance",
            "detail": f"Unexpected acceptance for {ids.task_id}; review.state={review_state!r}, execution_confirmed={execution_confirmed!r}.",
        })

    expected_execution_confirmed = expectations.get("execution_confirmed")
    if isinstance(expected_execution_confirmed, bool) and execution_confirmed is not expected_execution_confirmed:
        failures.append({
            "scenario": scenario.name,
            "code": "execution_confirmation_mismatch",
            "detail": f"Expected execution_confirmed={expected_execution_confirmed!r} but got {execution_confirmed!r}.",
        })

    if expectations.get("forbid_idle_blocked") and review_state == "idle_blocked":
        counters["false_idle_blocked_from_task_mismatch"] += 1
        failures.append({
            "scenario": scenario.name,
            "code": "false_idle_blocked_from_task_mismatch",
            "detail": f"Task-mismatch scenario produced idle_blocked for {ids.task_id}.",
        })

    if expectations.get("expect_lineage_mismatch") and not bool(authoritative_request.get("lineage_mismatch")):
        failures.append({
            "scenario": scenario.name,
            "code": "expected_lineage_mismatch_missing",
            "detail": "Expected authoritative request recovery to surface lineage_mismatch.",
        })

    if expectations.get("stale_guard_warning_only"):
        if snapshot.get("requires_human"):
            failures.append({
                "scenario": scenario.name,
                "code": "stale_guard_escalated_human_boundary",
                "detail": "Stale guard incorrectly required human intervention.",
            })
        if not execution_confirmed:
            failures.append({
                "scenario": scenario.name,
                "code": "stale_guard_blocked_confirmation",
                "detail": "Stale guard prevented confirmation for a same-task successful result.",
            })

    if expectations.get("wrong_task_rejected") and confirmed:
        counters["wrong_task_completions_accepted"] += 1
        failures.append({
            "scenario": scenario.name,
            "code": "wrong_task_completion_accepted",
            "detail": "A wrong-task ACK or result was accepted as completion.",
        })

    if expectations.get("stale_lineage_rejected") and confirmed:
        counters["stale_lineage_accepted"] += 1
        failures.append({
            "scenario": scenario.name,
            "code": "stale_lineage_accepted",
            "detail": "A stale ACK or result was accepted for the active lane.",
        })

    if expectations.get("objective_only_mismatch"):
        if review_state == "completed" or authoritative_result_status in SUCCESS_RESULT_STATUSES or execution_confirmed:
            counters["wrong_task_completions_accepted"] += 1
            failures.append({
                "scenario": scenario.name,
                "code": "objective_only_match_accepted_complete",
                "detail": "Objective-only match produced accepted completion.",
            })
        if review_state == "idle_blocked":
            counters["false_idle_blocked_from_task_mismatch"] += 1
            failures.append({
                "scenario": scenario.name,
                "code": "objective_only_match_idle_blocked",
                "detail": "Objective-only task mismatch produced idle_blocked.",
            })

    if expectations.get("wrapper_only_rejected") and (review_state == "completed" or execution_confirmed):
        failures.append({
            "scenario": scenario.name,
            "code": "wrapper_only_result_accepted",
            "detail": "Wrapper-only result without active task lineage was accepted.",
        })

    if expectations.get("review_task_mismatch_advisory"):
        recovered_task_id = str(authoritative_request.get("task_id") or "").strip()
        if recovered_task_id and recovered_task_id != ids.task_id:
            failures.append({
                "scenario": scenario.name,
                "code": "review_task_mismatch_overrode_active_task",
                "detail": f"Review artifact overrode the active task with {recovered_task_id}.",
            })

    if expectations.get("expect_direct_execution_ready"):
        idle = review.get("idle") if isinstance(review.get("idle"), dict) else {}
        if not bool(idle.get("direct_execution_ready")):
            failures.append({
                "scenario": scenario.name,
                "code": "direct_execution_not_armed",
                "detail": "TOD silence did not arm direct execution fallback.",
            })
        pending_actions = review.get("pending_actions") if isinstance(review.get("pending_actions"), list) else []
        pending_codes = {str(item.get("code") or "").strip() for item in pending_actions if isinstance(item, dict)}
        if "fallback_to_codex_direct_execution" not in pending_codes:
            failures.append({
                "scenario": scenario.name,
                "code": "direct_execution_action_missing",
                "detail": "TOD silence did not surface fallback_to_codex_direct_execution.",
            })

    if expectations.get("expect_fallback_active"):
        fallback_active = bool(snapshot.get("fallback_active"))
        fallback_task_id = str(snapshot.get("fallback_task_id") or "").strip()
        if not fallback_active:
            failures.append({
                "scenario": scenario.name,
                "code": "fallback_not_active",
                "detail": "Expected MIM fallback to be active for the same task.",
            })
        if fallback_task_id != ids.task_id:
            counters["fallback_task_mutations"] += 1
            failures.append({
                "scenario": scenario.name,
                "code": "fallback_task_mutation",
                "detail": f"Fallback task mutated from {ids.task_id} to {fallback_task_id or '<missing>'}.",
            })

    if expectations.get("fallback_mutation_forbidden"):
        fallback_payload = lane.get("fallback") if isinstance(lane.get("fallback"), dict) else {}
        if fallback_payload:
            for key, expected in {
                "objective_id": ids.objective_id,
                "task_id": ids.task_id,
                "request_id": ids.request_id,
                "correlation_id": ids.correlation_id,
            }.items():
                actual = str(fallback_payload.get(key) or "").strip()
                if actual and actual != expected:
                    counters["fallback_task_mutations"] += 1
                    failures.append({
                        "scenario": scenario.name,
                        "code": "fallback_lineage_mutation",
                        "detail": f"Fallback field {key} mutated from {expected} to {actual}.",
                    })

    return failures, counters


def _simulate_lane(*, scenario: ScenarioDefinition, ids: LaneIds, now: datetime, shared_root: Path) -> dict[str, Any]:
    lane = scenario.builder(ids, now)
    _write_lane(shared_root, lane)
    request = lane.get("request") if isinstance(lane.get("request"), dict) else {}
    authoritative_request = load_authoritative_request_status(shared_root=shared_root)
    snapshot = mim_ui._build_tod_truth_reconciliation_snapshot(
        initiative_driver={"active_objective": {"objective_id": ids.objective_token}},
        authoritative_request=authoritative_request or request,
        shared_root=shared_root,
    )
    review = build_task_status_review(
        task_request=request,
        trigger=lane.get("trigger") if isinstance(lane.get("trigger"), dict) else {},
        trigger_ack=lane.get("trigger_ack") if isinstance(lane.get("trigger_ack"), dict) else {},
        task_ack=lane.get("task_ack") if isinstance(lane.get("task_ack"), dict) else {},
        task_result=lane.get("task_result") if isinstance(lane.get("task_result"), dict) else {},
        catchup_gate=lane.get("catchup_gate") if isinstance(lane.get("catchup_gate"), dict) else _catchup_gate(now, gate_pass=True),
        troubleshooting_authority=lane.get("troubleshooting_authority") if isinstance(lane.get("troubleshooting_authority"), dict) else _troubleshooting_authority(),
        persistent_task=lane.get("persistent_task") if isinstance(lane.get("persistent_task"), dict) else _persistent_task(ids),
        system_alert_summary=lane.get("system_alert_summary") if isinstance(lane.get("system_alert_summary"), dict) else {"active": False, "highest_severity": "none", "primary_alert": {}},
        idle_seconds=120,
        emergency_timeout_seconds=180,
        direct_execution_timeout_seconds=240,
        now=now,
    )
    failures, counters = _evaluate_lane(
        scenario=scenario,
        ids=ids,
        lane=lane,
        authoritative_request=authoritative_request,
        snapshot=snapshot,
        review=review,
    )
    return {
        "lane": lane,
        "authoritative_request": authoritative_request,
        "snapshot": snapshot,
        "review": review,
        "failures": failures,
        "counters": counters,
    }


def _scenario_catalog() -> list[dict[str, str]]:
    return [
        {
            "name": scenario.name,
            "description": scenario.description,
        }
        for scenario in SCENARIOS
    ]


def _invariant_contract() -> list[dict[str, str]]:
    return [
        {"code": "task_id_stable", "statement": "task_id never changes inside one MIM-owned execution lane."},
        {"code": "no_objective_only_acceptance", "statement": "objective-only matches cannot produce accepted completion."},
        {"code": "no_objective_only_idle_blocked", "statement": "objective-only task mismatches cannot produce idle_blocked."},
        {"code": "stale_guard_warning_only", "statement": "stale_guard is diagnostic only and cannot become authoritative execution lineage."},
        {"code": "review_advisory_unless_task_matches", "statement": "MIM_TASK_STATUS_REVIEW remains advisory unless its task_id matches the active lane."},
        {"code": "fallback_same_lineage", "statement": "MIM fallback preserves objective_id, task_id, request_id, and correlation_id."},
        {"code": "task_exact_acceptance", "statement": "ACK, result, and current_processing must all match the active task before acceptance."},
        {"code": "wrapper_only_result_rejected", "statement": "Wrapper-only execution results cannot be accepted as task completion."},
    ]


def run_simulation(*, total_runs: int = 5000, output_dir: Path | None = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    scenario_counter: Counter[str] = Counter()
    representative_examples: dict[str, dict[str, Any]] = {}
    failure_examples: list[dict[str, Any]] = []
    counters = {
        "lineage_safe_runs": 0,
        "stale_lineage_accepted": 0,
        "wrong_task_completions_accepted": 0,
        "false_idle_blocked_from_task_mismatch": 0,
        "fallback_task_mutations": 0,
    }

    with tempfile.TemporaryDirectory(prefix="mim-tod-coordination-sim-") as tmpdir:
        shared_root = Path(tmpdir)
        for index in range(total_runs):
            scenario = SCENARIOS[index % len(SCENARIOS)]
            ids = _lane_ids(index)
            now = started_at + timedelta(seconds=index)
            outcome = _simulate_lane(scenario=scenario, ids=ids, now=now, shared_root=shared_root)
            scenario_counter[scenario.name] += 1

            for key in counters:
                counters[key] += int(outcome["counters"].get(key, 0))

            if not outcome["failures"]:
                counters["lineage_safe_runs"] += 1
            else:
                if len(failure_examples) < 25:
                    failure_examples.append(
                        {
                            "scenario": scenario.name,
                            "task_id": ids.task_id,
                            "request_id": ids.request_id,
                            "failures": outcome["failures"],
                            "review_state": outcome["review"].get("state"),
                            "snapshot_state": outcome["snapshot"].get("state"),
                        }
                    )

            if scenario.name not in representative_examples:
                representative_examples[scenario.name] = {
                    "task_id": ids.task_id,
                    "request_id": ids.request_id,
                    "review_state": outcome["review"].get("state"),
                    "execution_confirmed": bool(outcome["snapshot"].get("execution_confirmed")),
                    "authoritative_result_status": outcome["authoritative_request"].get("result_status"),
                    "lineage_mismatch": bool(outcome["authoritative_request"].get("lineage_mismatch")),
                    "verdict": "safe" if not outcome["failures"] else "failed",
                }

    completed_at = datetime.now(timezone.utc)
    failure_count = len(failure_examples)
    report = {
        "generated_at": _utc(completed_at),
        "started_at": _utc(started_at),
        "completed_at": _utc(completed_at),
        "duration_seconds": max(0.0, round((completed_at - started_at).total_seconds(), 3)),
        "summary": {
            "total_runs": total_runs,
            "lineage_safe_runs": counters["lineage_safe_runs"],
            "stale_lineage_accepted": counters["stale_lineage_accepted"],
            "wrong_task_completions_accepted": counters["wrong_task_completions_accepted"],
            "false_idle_blocked_from_task_mismatch": counters["false_idle_blocked_from_task_mismatch"],
            "fallback_task_mutations": counters["fallback_task_mutations"],
            "failure_count": failure_count,
            "pass": counters["lineage_safe_runs"] == total_runs
            and counters["stale_lineage_accepted"] == 0
            and counters["wrong_task_completions_accepted"] == 0
            and counters["false_idle_blocked_from_task_mismatch"] == 0
            and counters["fallback_task_mutations"] == 0,
        },
        "scenario_counts": dict(sorted(scenario_counter.items())),
        "scenario_catalog": _scenario_catalog(),
        "invariant_contract": _invariant_contract(),
        "failure_examples": failure_examples,
        "representative_examples": representative_examples,
        "tod_correction_instructions": [
            "Preserve same-task ACK, result, and current_processing identity across the full lane.",
            "Treat stale_guard as warning-only metadata; do not promote it into authoritative lineage.",
            "Do not publish wrapper-only or objective-only completions without exact active task lineage.",
            "When MIM falls back, preserve objective_id, task_id, request_id, and correlation_id exactly.",
        ],
    }

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / f"{REPORT_BASENAME}_report.latest.json"
        failure_path = output_dir / f"{REPORT_BASENAME}_failure_examples.latest.json"
        scenario_path = output_dir / f"{REPORT_BASENAME}_scenario_catalog.latest.json"
        invariant_path = output_dir / f"{REPORT_BASENAME}_invariant_contract.latest.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        failure_path.write_text(json.dumps(report["failure_examples"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        scenario_path.write_text(json.dumps(report["scenario_catalog"], indent=2, sort_keys=True) + "\n", encoding="utf-8")
        invariant_path.write_text(json.dumps(report["invariant_contract"], indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the MIM-side TOD coordination simulation harness.")
    parser.add_argument("--runs", type=int, default=5000, help="Number of synthetic lanes to execute.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for generated simulation artifacts.",
    )
    args = parser.parse_args()

    report = run_simulation(total_runs=max(1, args.runs), output_dir=args.output_dir)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    return 0 if bool(report["summary"].get("pass")) else 1


if __name__ == "__main__":
    raise SystemExit(main())