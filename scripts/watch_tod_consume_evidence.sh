#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"

REVIEW_FILE="${REVIEW_FILE:-${SHARED_DIR}/MIM_TASK_STATUS_REVIEW.latest.json}"
REQUEST_FILE="${REQUEST_FILE:-${SHARED_DIR}/MIM_TOD_TASK_REQUEST.latest.json}"
TASK_ACK_FILE="${TASK_ACK_FILE:-${SHARED_DIR}/TOD_MIM_TASK_ACK.latest.json}"
TASK_RESULT_FILE="${TASK_RESULT_FILE:-${SHARED_DIR}/TOD_MIM_TASK_RESULT.latest.json}"
MANUAL_DISPATCH_LOCK_FILE="${MANUAL_DISPATCH_LOCK_FILE:-${SHARED_DIR}/MIM_TOD_MANUAL_DISPATCH_LOCK.latest.json}"
RECOVERY_ALERT_FILE="${RECOVERY_ALERT_FILE:-${SHARED_DIR}/TOD_MIM_RECOVERY_ALERT.latest.json}"

EVIDENCE_FILE="${EVIDENCE_FILE:-${SHARED_DIR}/MIM_TOD_CONSUME_EVIDENCE.latest.json}"
COLLAB_PROGRESS_FILE="${COLLAB_PROGRESS_FILE:-${SHARED_DIR}/MIM_TOD_COLLAB_PROGRESS.latest.json}"
STATUS_FILE="${STATUS_FILE:-${LOG_DIR}/mim_tod_consume_evidence.latest.json}"
STATE_FILE="${STATE_FILE:-${LOG_DIR}/mim_tod_consume_evidence.state.json}"
EVENT_LOG_FILE="${EVENT_LOG_FILE:-${LOG_DIR}/mim_tod_consume_evidence.jsonl}"

POLL_SECONDS="${POLL_SECONDS:-5}"
WATCH_WINDOW_SECONDS="${WATCH_WINDOW_SECONDS:-900}"
RUN_ONCE="${RUN_ONCE:-0}"

mkdir -p "${SHARED_DIR}"
mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG_FILE}"

emit_cycle() {
  python3 - <<'PY' \
    "${REVIEW_FILE}" \
    "${REQUEST_FILE}" \
    "${TASK_ACK_FILE}" \
    "${TASK_RESULT_FILE}" \
        "${MANUAL_DISPATCH_LOCK_FILE}" \
        "${RECOVERY_ALERT_FILE}" \
    "${EVIDENCE_FILE}" \
        "${COLLAB_PROGRESS_FILE}" \
    "${STATUS_FILE}" \
    "${STATE_FILE}" \
    "${EVENT_LOG_FILE}" \
    "${WATCH_WINDOW_SECONDS}"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

review_file = Path(sys.argv[1])
request_file = Path(sys.argv[2])
task_ack_file = Path(sys.argv[3])
task_result_file = Path(sys.argv[4])
manual_dispatch_lock_file = Path(sys.argv[5])
recovery_alert_file = Path(sys.argv[6])
evidence_file = Path(sys.argv[7])
collab_progress_file = Path(sys.argv[8])
status_file = Path(sys.argv[9])
state_file = Path(sys.argv[10])
event_log_file = Path(sys.argv[11])
watch_window_seconds = max(1, int(sys.argv[12]))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def first_text(payload: dict, *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def derive_execution_identity(review: dict, request: dict) -> tuple[str, str, str, str, str]:
    review_task = first_text(
        review.get("task") if isinstance(review.get("task"), dict) else {},
        "active_task_id",
    )
    registry_task_id = review_task or first_text(request, "task_id")
    bridge_request_id = first_text(request, "request_id")
    execution_id = bridge_request_id or registry_task_id
    id_kind = "bridge_request_id" if bridge_request_id else ("mim_task_registry_id" if registry_task_id else "")
    execution_lane = "tod_bridge_request" if id_kind == "bridge_request_id" else ("mim_task_registry" if id_kind == "mim_task_registry_id" else "")
    return execution_id, id_kind, execution_lane, registry_task_id, bridge_request_id


def format_execution_reference(execution_id: str, id_kind: str) -> str:
    if id_kind == "bridge_request_id":
        return f"bridge_request={execution_id or 'unknown'}"
    if id_kind == "mim_task_registry_id":
        return f"task={execution_id or 'unknown'}"
    return f"execution_id={execution_id or 'unknown'}"


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def describe_consume_phase(
    phase: str,
    execution_id: str,
    id_kind: str,
    first_ack_mutation: object,
    first_result_mutation: object,
    timed_out: bool,
) -> tuple[str, str, str]:
    execution_ref = format_execution_reference(execution_id, id_kind)
    if phase == "captured":
        return (
            "auto_watch_captured_consume_mutation",
            "result_published_for_target_task",
            (
                f"consume evidence captured for {execution_ref}; "
                f"ack_mutated={bool(first_ack_mutation)} result_mutated={bool(first_result_mutation)}"
            ),
        )
    if phase == "timeout":
        return (
            "auto_watch_timed_out_waiting_for_consume",
            "awaiting_target_task_consume",
            (
                f"consume watch timed out for {execution_ref}; "
                f"timed_out={timed_out} ack_mutated={bool(first_ack_mutation)} result_mutated={bool(first_result_mutation)}"
            ),
        )
    if phase == "watching":
        return (
            "auto_watch_waiting_for_consume_mutation",
            "awaiting_target_task_consume",
            f"watching for TOD ACK and RESULT mutation for {execution_ref}",
        )
    return (
        "auto_watch_waiting_for_target_task",
        "no_target_task_selected",
        "waiting for a target task before consume evidence can be tracked",
    )


def describe_manual_dispatch_lock(lock_payload: dict, reference: datetime) -> tuple[str, str, str]:
    active = bool(lock_payload.get("active"))
    reason = first_text(lock_payload, "reason") or "manual_dispatch_lock_inactive"
    expires_at = parse_iso(lock_payload.get("expires_at"))
    if expires_at and expires_at <= reference:
        active = False
    if active:
        return (
            "manual_dispatch_lock_active",
            "observe_only_while_lock_active",
            (
                f"manual dispatch lock is active; reason={reason}; "
                f"expires_at={to_iso(expires_at) if expires_at else 'unknown'}"
            ),
        )
    return (
        "manual_dispatch_lock_inactive",
        "normal_publication_lane",
        f"manual dispatch lock is inactive; last_reason={reason}",
    )


def describe_recovery_alert(
    recovery_alert: dict,
    execution_id: str,
    id_kind: str,
) -> tuple[str, str, str]:
    if not recovery_alert:
        return (
            "no_tod_recovery_signal_observed",
            "no_recovery_alert_present",
            "no TOD recovery alert is present in shared artifacts",
        )

    progress_classification = first_text(recovery_alert, "progress_classification")
    task_state = first_text(recovery_alert, "task_state") or "unknown"
    issue_code = first_text(recovery_alert, "issue_code") or "unknown_issue"
    issue_detail = first_text(recovery_alert, "issue_detail") or ""
    recovery_action = first_text(recovery_alert, "recovery_action") or "observe"
    alert_task_id = first_text(recovery_alert, "task_id", "request_id")
    if alert_task_id and execution_id and alert_task_id != execution_id:
        return (
            "stale_tod_recovery_signal_ignored",
            "no_recovery_alert_present",
            f"ignoring stale TOD recovery alert for authoritative_task={alert_task_id} while tracking {format_execution_reference(execution_id, id_kind)}",
        )
    task_note = ""
    tracked_ref = format_execution_reference(execution_id, id_kind)
    return (
        "auto_observing_tod_recovery_signal",
        progress_classification or task_state,
        (
            f"TOD recovery alert reports issue={issue_code} action={recovery_action}. "
            f"detail={issue_detail or 'none'}.{task_note}"
        ).strip(),
    )


def build_collaboration_progress(
    reference: datetime,
    execution_id: str,
    id_kind: str,
    execution_lane: str,
    registry_task_id: str,
    bridge_request_id: str,
    phase: str,
    first_ack_mutation: object,
    first_result_mutation: object,
    timed_out: bool,
    manual_dispatch_lock: dict,
    recovery_alert: dict,
) -> dict:
    consume_mim_status, consume_tod_status, consume_observation = describe_consume_phase(
        phase,
        execution_id,
        id_kind,
        first_ack_mutation,
        first_result_mutation,
        timed_out,
    )
    lock_mim_status, lock_tod_status, lock_observation = describe_manual_dispatch_lock(
        manual_dispatch_lock,
        reference,
    )
    recovery_mim_status, recovery_tod_status, recovery_observation = describe_recovery_alert(
        recovery_alert,
        execution_id,
        id_kind,
    )
    payload = {
        "generated_at": to_iso(reference),
        "type": "mim_tod_collaboration_progress_v1",
        "execution_id": execution_id,
        "id_kind": id_kind,
        "execution_lane": execution_lane,
        "owners": {
            "mim": "publish_and_decision_owner",
            "tod": "consume_and_execution_owner",
        },
        "workstreams": [
            {
                "id": 1,
                "name": "consume_mutation_tracking",
                "mim_status": consume_mim_status,
                "tod_status": consume_tod_status,
                "latest_observation": consume_observation,
            },
            {
                "id": 2,
                "name": "publisher_guard",
                "mim_status": lock_mim_status,
                "tod_status": lock_tod_status,
                "latest_observation": lock_observation,
            },
            {
                "id": 3,
                "name": "tod_recovery_progress",
                "mim_status": recovery_mim_status,
                "tod_status": recovery_tod_status,
                "latest_observation": recovery_observation,
            },
        ],
    }
    if registry_task_id:
        payload["task_id"] = registry_task_id
    if bridge_request_id:
        payload["request_id"] = bridge_request_id
    return payload


reference = now_utc()
review = read_json(review_file)
request = read_json(request_file)
task_ack = read_json(task_ack_file)
task_result = read_json(task_result_file)
manual_dispatch_lock = read_json(manual_dispatch_lock_file)
recovery_alert = read_json(recovery_alert_file)
state = read_json(state_file)

execution_id, id_kind, execution_lane, registry_task_id, bridge_request_id = derive_execution_identity(review, request)
target_task_id = registry_task_id or bridge_request_id
match_request_id = bridge_request_id or registry_task_id

request_generated_at = first_text(request, "generated_at", "emitted_at")

target_changed = (
    str(state.get("target_task_id") or "") != target_task_id
    or str(state.get("execution_id") or "") != execution_id
    or str(state.get("id_kind") or "") != id_kind
)
request_reissued = (
    bool(execution_id)
    and str(state.get("execution_id") or "") == execution_id
    and bool(request_generated_at)
    and request_generated_at != str(state.get("request_generated_at") or "")
)
if target_changed or request_reissued:
    state = {
        "execution_id": execution_id,
        "id_kind": id_kind,
        "execution_lane": execution_lane,
        "target_task_id": target_task_id,
        "bridge_request_id": bridge_request_id,
        "watch_started_at": to_iso(reference),
        "request_generated_at": request_generated_at,
        "watch_window_seconds": watch_window_seconds,
        "ack_baseline_generated_at": first_text(task_ack, "generated_at"),
        "result_baseline_generated_at": first_text(task_result, "generated_at"),
        "first_ack_mutation": None,
        "first_result_mutation": None,
        "timed_out": False,
        "phase": "watching" if target_task_id else "idle",
        "last_event": (
            "request_reissued_watch_reset"
            if request_reissued
            else ("target_initialized" if target_task_id else "waiting_for_target_task")
        ),
    }

watch_started_at = parse_iso(state.get("watch_started_at")) or reference
elapsed_seconds = max(0, int((reference - watch_started_at).total_seconds()))

ack_generated_at = first_text(task_ack, "generated_at")
ack_request_id = first_text(task_ack, "request_id", "task_id")
ack_matches = bool(match_request_id and ack_request_id == match_request_id)
ack_mutated = (
    ack_matches
    and bool(ack_generated_at)
    and ack_generated_at != str(state.get("ack_baseline_generated_at") or "")
)

result_generated_at = first_text(task_result, "generated_at")
result_request_id = first_text(task_result, "request_id", "task_id")
result_matches = bool(match_request_id and result_request_id == match_request_id)
result_mutated = (
    result_matches
    and bool(result_generated_at)
    and result_generated_at != str(state.get("result_baseline_generated_at") or "")
)

first_ack_mutation = state.get("first_ack_mutation")
if first_ack_mutation is None and (target_changed or request_reissued) and ack_matches and ack_generated_at:
    first_ack_mutation = {
        "observed_at": to_iso(reference),
        "generated_at": ack_generated_at,
        "request_id": ack_request_id,
        "status": str(task_ack.get("status") or ""),
    }
if first_ack_mutation is None and ack_mutated:
    first_ack_mutation = {
        "observed_at": to_iso(reference),
        "generated_at": ack_generated_at,
        "request_id": ack_request_id,
        "status": str(task_ack.get("status") or ""),
    }

first_result_mutation = state.get("first_result_mutation")
if first_result_mutation is None and (target_changed or request_reissued) and result_matches and result_generated_at:
    first_result_mutation = {
        "observed_at": to_iso(reference),
        "generated_at": result_generated_at,
        "request_id": result_request_id,
        "status": str(task_result.get("status") or ""),
    }
if first_result_mutation is None and result_mutated:
    first_result_mutation = {
        "observed_at": to_iso(reference),
        "generated_at": result_generated_at,
        "request_id": result_request_id,
        "status": str(task_result.get("status") or ""),
    }

timed_out = bool(state.get("timed_out", False))
if execution_id and not timed_out and elapsed_seconds >= watch_window_seconds:
    timed_out = True

completed = bool(first_ack_mutation and first_result_mutation)
phase = "idle"
last_event = "waiting_for_target_task"
if execution_id:
    if completed:
        phase = "captured"
        last_event = "consume_mutation_captured"
    elif timed_out:
        phase = "timeout"
        last_event = "watch_window_elapsed_without_full_mutation"
    else:
        phase = "watching"
        last_event = "watching_for_consume_mutation"

state.update(
    {
        "execution_id": execution_id,
        "id_kind": id_kind,
        "execution_lane": execution_lane,
        "target_task_id": target_task_id,
        "bridge_request_id": bridge_request_id,
        "watch_started_at": to_iso(watch_started_at),
        "request_generated_at": request_generated_at,
        "watch_window_seconds": watch_window_seconds,
        "ack_baseline_generated_at": str(state.get("ack_baseline_generated_at") or ack_generated_at),
        "result_baseline_generated_at": str(state.get("result_baseline_generated_at") or result_generated_at),
        "first_ack_mutation": first_ack_mutation,
        "first_result_mutation": first_result_mutation,
        "timed_out": timed_out,
        "phase": phase,
        "last_event": last_event,
        "last_updated_at": to_iso(reference),
    }
)
write_json(state_file, state)

evidence_payload = {
    "generated_at": to_iso(reference),
    "type": "mim_tod_consume_evidence_v1",
    "execution_id": execution_id,
    "id_kind": id_kind,
    "execution_lane": execution_lane,
    "watch": {
        "started_at": state.get("watch_started_at"),
        "window_seconds": watch_window_seconds,
        "elapsed_seconds": elapsed_seconds,
        "phase": phase,
        "timed_out": timed_out,
    },
    "baseline": {
        "task_ack_generated_at": state.get("ack_baseline_generated_at"),
        "task_result_generated_at": state.get("result_baseline_generated_at"),
    },
    "first_mutations": {
        "task_ack": first_ack_mutation,
        "task_result": first_result_mutation,
    },
    "current": {
        "task_ack": {
            "generated_at": ack_generated_at,
            "request_id": ack_request_id,
            "status": str(task_ack.get("status") or ""),
            "matches_target": ack_matches,
        },
        "task_result": {
            "generated_at": result_generated_at,
            "request_id": result_request_id,
            "status": str(task_result.get("status") or ""),
            "matches_target": result_matches,
        },
    },
}
if registry_task_id:
    evidence_payload["task_id"] = registry_task_id
if bridge_request_id:
    evidence_payload["request_id"] = bridge_request_id
write_json(evidence_file, evidence_payload)
write_json(
    collab_progress_file,
    build_collaboration_progress(
        reference,
        execution_id,
        id_kind,
        execution_lane,
        registry_task_id,
        bridge_request_id,
        phase,
        first_ack_mutation,
        first_result_mutation,
        timed_out,
        manual_dispatch_lock,
        recovery_alert,
    ),
)
write_json(status_file, {
    "generated_at": to_iso(reference),
    "type": "mim_tod_consume_evidence_status_v1",
    "execution_id": execution_id,
    "id_kind": id_kind,
    "execution_lane": execution_lane,
    "phase": phase,
    "timed_out": timed_out,
    "elapsed_seconds": elapsed_seconds,
    "captured_ack": bool(first_ack_mutation),
    "captured_result": bool(first_result_mutation),
    "last_event": last_event,
})
if registry_task_id:
    write_json(status_file, {
        **read_json(status_file),
        "task_id": registry_task_id,
    })
if bridge_request_id:
    write_json(status_file, {
        **read_json(status_file),
        "request_id": bridge_request_id,
    })

event = {
    "generated_at": to_iso(reference),
    "execution_id": execution_id,
    "id_kind": id_kind,
    "execution_lane": execution_lane,
    "phase": phase,
    "timed_out": timed_out,
    "captured_ack": bool(first_ack_mutation),
    "captured_result": bool(first_result_mutation),
    "last_event": last_event,
}
if registry_task_id:
    event["task_id"] = registry_task_id
if bridge_request_id:
    event["request_id"] = bridge_request_id
with event_log_file.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(event, separators=(",", ":")) + "\n")

print(phase)
print(str(bool(first_ack_mutation)).lower())
print(str(bool(first_result_mutation)).lower())
PY
}

echo "[tod-consume-evidence] watching ACK/RESULT every ${POLL_SECONDS}s (window=${WATCH_WINDOW_SECONDS}s)"

while true; do
  out="$(emit_cycle)"
  phase="$(echo "${out}" | sed -n '1p')"
  ack_flag="$(echo "${out}" | sed -n '2p')"
  result_flag="$(echo "${out}" | sed -n '3p')"
  echo "[tod-consume-evidence] phase=${phase} captured_ack=${ack_flag} captured_result=${result_flag}"

  run_once="$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')"
  if [[ "${run_once}" == "1" || "${run_once}" == "true" || "${run_once}" == "yes" ]]; then
    break
  fi

  sleep "${POLL_SECONDS}"
done
