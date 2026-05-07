#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"
REQUEST_FILE="${REQUEST_FILE:-${SHARED_DIR}/TOD_MIM_COORDINATION_REQUEST.latest.json}"
ACK_FILE="${ACK_FILE:-${SHARED_DIR}/MIM_TOD_COORDINATION_ACK.latest.json}"
STATUS_FILE="${STATUS_FILE:-${LOG_DIR}/mim_coordination_responder.latest.json}"
EVENT_LOG_FILE="${EVENT_LOG_FILE:-${LOG_DIR}/mim_coordination_responder.jsonl}"
SERVICE_NAME="${SERVICE_NAME:-mim_coordination_responder}"
POLL_SECONDS="${POLL_SECONDS:-3}"
RUN_ONCE="${RUN_ONCE:-0}"
ALLOW_RESOLVED_REQUESTS="${ALLOW_RESOLVED_REQUESTS:-0}"
LOCK_FILE="${LOCK_FILE:-${LOG_DIR}/mim_coordination_responder.lock}"

mkdir -p "${SHARED_DIR}"
mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG_FILE}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[mim-coordination-responder] another instance is already active; exiting"
    exit 0
fi

next_bridge_meta() {
  eval "$(python3 "${ROOT_DIR}/scripts/bridge_packet_sequence.py" --shared-dir "${SHARED_DIR}" --service "${SERVICE_NAME}" --instance-id "${SERVICE_NAME}:$$")"
}

emit_ack_cycle() {
  local emitted_at sequence source_host source_service source_instance
  next_bridge_meta
  emitted_at="${EMITTED_AT}"
  sequence="${SEQUENCE}"
  source_host="${SOURCE_HOST}"
  source_service="${SOURCE_SERVICE}"
  source_instance="${SOURCE_INSTANCE_ID}"

  python3 - <<'PY' \
    "${REQUEST_FILE}" \
    "${ACK_FILE}" \
    "${STATUS_FILE}" \
    "${EVENT_LOG_FILE}" \
    "${emitted_at}" \
    "${sequence}" \
    "${source_host}" \
    "${source_service}" \
    "${source_instance}" \
    "${ALLOW_RESOLVED_REQUESTS}"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

request_path = Path(sys.argv[1])
ack_path = Path(sys.argv[2])
status_path = Path(sys.argv[3])
event_log_path = Path(sys.argv[4])
emitted_at = str(sys.argv[5])
sequence = int(sys.argv[6])
source_host = str(sys.argv[7])
source_service = str(sys.argv[8])
source_instance = str(sys.argv[9])
allow_resolved = str(sys.argv[10]).strip().lower() in {"1", "true", "yes"}


def read_json(path: Path):
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def to_text(value):
    return str(value or "").strip()


def lower_text(value):
    return to_text(value).lower()


def parse_timestamp(value):
    text = to_text(value)
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def nested_text(payload, *path):
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return to_text(current)


request = read_json(request_path)
ack = read_json(ack_path)
status = {
    "generated_at": emitted_at,
    "type": "mim_coordination_responder_status_v1",
    "service": source_service,
    "request_file": str(request_path),
    "ack_file": str(ack_path),
    "state": "waiting_for_request",
    "pending_request_id": "",
    "issue_code": "",
    "request_status": "",
    "last_ack_status": "none",
    "ack_written": False,
    "message": "No coordination request detected.",
}

if not request:
    status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    with event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(status, separators=(",", ":")) + "\n")
    print("waiting_for_request|none|")
    raise SystemExit(0)

request_id = to_text(request.get("request_id") or request.get("task_id"))
objective_id = to_text(request.get("objective_id"))
request_status = lower_text(request.get("status"))
issue_code = to_text(request.get("issue_code"))
issue_summary = to_text(request.get("issue_summary"))
requested_action = to_text(request.get("requested_action"))
correlation_id = to_text(request.get("correlation_id")) or f"{request_id}-coord-ack"
target_dispatch_task_id = to_text(
    request.get("target_dispatch_task_id")
    or nested_text(request, "bridge_runtime", "current_processing", "task_id")
    or request.get("task_id")
    or request_id
)
auto_dispatch_requested = requested_action == "dispatch_remediation_task" and bool(target_dispatch_task_id)
final_ack_keys = {
    "acknowledged",
    "acknowledged_at",
    "request_id",
    "decision",
    "reason",
    "target_dispatch_task_id",
}

status.update(
    {
        "pending_request_id": request_id,
        "issue_code": issue_code,
        "request_status": request_status,
    }
)

if not request_id:
    status["state"] = "request_missing_request_id"
    status["message"] = "Coordination request is present but missing request_id/task_id."
    status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    with event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(status, separators=(",", ":")) + "\n")
    print("request_missing_request_id|none|")
    raise SystemExit(0)

ack_request_id = to_text((ack or {}).get("request_id"))
ack_status = lower_text(
    (ack or {}).get("ack_status")
    or ((ack or {}).get("coordination") or {}).get("status")
    or (ack or {}).get("decision")
)
request_generated_at = parse_timestamp(request.get("generated_at") or request.get("emitted_at"))
ack_generated_at = parse_timestamp((ack or {}).get("generated_at") or (ack or {}).get("emitted_at"))
pending_ack_needs_refresh = bool(
    request_generated_at is not None
    and ack_generated_at is not None
    and request_generated_at > ack_generated_at
)

if not allow_resolved and request_status in {"resolved", "closed", "none"}:
    if ack_request_id == request_id and ack_status in {"resolved", "closed", "done", "complete"}:
        status["state"] = "ack_current_resolved"
        status["last_ack_status"] = ack_status
        status["message"] = "Resolved coordination ACK already current for request."
        status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        with event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(status, separators=(",", ":")) + "\n")
        print("ack_current_resolved|" + ack_status + "|" + request_id)
        raise SystemExit(0)

    ack_payload = {
        "version": "1.0",
        "source": "MIM",
        "target": "TOD",
        "generated_at": emitted_at,
        "emitted_at": emitted_at,
        "sequence": sequence,
        "source_host": source_host,
        "source_service": source_service,
        "source_instance_id": source_instance,
        "objective_id": objective_id,
        "task_id": request_id,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "acknowledged": True,
        "acknowledged_at": emitted_at,
        "ack_status": "resolved",
        "status": "resolved",
        "decision": "resolved",
        "reason": issue_summary or issue_code or "coordination_request_resolved",
        "target_dispatch_task_id": target_dispatch_task_id,
        "detail": "MIM coordination responder observed resolved coordination request and refreshed closure ACK.",
        "coordination": {
            "status": "resolved",
            "phase": "request_resolved",
            "detail": "coordination_request_resolved",
            "request_issue_code": issue_code,
            "requested_action": requested_action,
            "pending_request_id": request_id,
        },
    }
    ack_path.write_text(json.dumps(ack_payload, indent=2) + "\n", encoding="utf-8")

    status["state"] = "ack_emitted_resolved"
    status["ack_written"] = True
    status["last_ack_status"] = "resolved"
    status["message"] = "Resolved coordination ACK emitted to refresh closure state."
    status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    with event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(status, separators=(",", ":")) + "\n")
    print("ack_emitted_resolved|resolved|" + request_id)
    raise SystemExit(0)

if auto_dispatch_requested and ack_request_id == request_id and ack_status in {"pending", "acknowledged", "accepted", "active", "in_progress"}:
    ack_payload = {
        "acknowledged": True,
        "acknowledged_at": emitted_at,
        "request_id": request_id,
        "decision": "dispatch_approved",
        "reason": issue_summary or issue_code or "coordination_dispatch_approved",
        "target_dispatch_task_id": target_dispatch_task_id,
    }
    ack_path.write_text(json.dumps(ack_payload, indent=2) + "\n", encoding="utf-8")

    status["state"] = "ack_upgraded_dispatch_approved"
    status["ack_written"] = True
    status["last_ack_status"] = "dispatch_approved"
    status["message"] = "MIM coordination ACK upgraded from pending to automatic remediation dispatch approval."
    status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    with event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(status, separators=(",", ":")) + "\n")
    print("ack_upgraded_dispatch_approved|dispatch_approved|" + request_id)
    raise SystemExit(0)

if ack_request_id == request_id and ack_status in {"pending", "acknowledged", "accepted", "active", "in_progress"}:
    if pending_ack_needs_refresh:
        ack_payload = dict(ack or {})
        coordination = ack_payload.get("coordination") if isinstance(ack_payload.get("coordination"), dict) else {}
        coordination.update(
            {
                "status": ack_status or "pending",
                "phase": "request_refresh",
                "detail": "coordination_request_refresh_observed",
                "request_issue_code": issue_code,
                "requested_action": requested_action,
                "pending_request_id": request_id,
            }
        )
        ack_payload.update(
            {
                "generated_at": emitted_at,
                "emitted_at": emitted_at,
                "sequence": sequence,
                "source_host": source_host,
                "source_service": source_service,
                "source_instance_id": source_instance,
                "objective_id": objective_id,
                "task_id": target_dispatch_task_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "acknowledged": True,
                "acknowledged_at": emitted_at,
                "ack_status": ack_status or "pending",
                "status": ack_status or "pending",
                "target_dispatch_task_id": target_dispatch_task_id,
                "coordination": coordination,
            }
        )
        ack_path.write_text(json.dumps(ack_payload, indent=2) + "\n", encoding="utf-8")

        status["state"] = "ack_refreshed"
        status["ack_written"] = True
        status["last_ack_status"] = ack_status or "pending"
        status["message"] = "Pending coordination ACK refreshed because the request payload advanced."
        status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        with event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(status, separators=(",", ":")) + "\n")
        print("ack_refreshed|" + (ack_status or "pending") + "|" + request_id)
        raise SystemExit(0)

    status["state"] = "ack_current"
    status["last_ack_status"] = ack_status
    status["message"] = "Coordination ACK already current for pending request."
    status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    with event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(status, separators=(",", ":")) + "\n")
    print("ack_current|" + ack_status + "|" + request_id)
    raise SystemExit(0)

if ack_request_id == request_id and ack_status == "dispatch_approved":
    if set((ack or {}).keys()) != final_ack_keys:
        ack_payload = {
            "acknowledged": True,
            "acknowledged_at": to_text((ack or {}).get("acknowledged_at")) or emitted_at,
            "request_id": request_id,
            "decision": "dispatch_approved",
            "reason": to_text((ack or {}).get("reason")) or issue_summary or issue_code or "coordination_dispatch_approved",
            "target_dispatch_task_id": to_text((ack or {}).get("target_dispatch_task_id")) or target_dispatch_task_id,
        }
        ack_path.write_text(json.dumps(ack_payload, indent=2) + "\n", encoding="utf-8")

        status["state"] = "ack_normalized_dispatch_approved"
        status["ack_written"] = True
        status["last_ack_status"] = ack_status
        status["message"] = "Dispatch-approved coordination ACK normalized to the final contract schema."
        status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        with event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(status, separators=(",", ":")) + "\n")
        print("ack_normalized_dispatch_approved|" + ack_status + "|" + request_id)
        raise SystemExit(0)

    status["state"] = "ack_current_dispatch_approved"
    status["last_ack_status"] = ack_status
    status["message"] = "Dispatch-approved coordination ACK already current for remediation request."
    status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    with event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(status, separators=(",", ":")) + "\n")
    print("ack_current_dispatch_approved|" + ack_status + "|" + request_id)
    raise SystemExit(0)

if auto_dispatch_requested:
    ack_payload = {
        "acknowledged": True,
        "acknowledged_at": emitted_at,
        "request_id": request_id,
        "decision": "dispatch_approved",
        "reason": issue_summary or issue_code or "coordination_dispatch_approved",
        "target_dispatch_task_id": target_dispatch_task_id,
    }
    ack_path.write_text(json.dumps(ack_payload, indent=2) + "\n", encoding="utf-8")

    status["state"] = "ack_emitted_dispatch_approved"
    status["ack_written"] = True
    status["last_ack_status"] = "dispatch_approved"
    status["message"] = "MIM coordination ACK emitted with automatic remediation dispatch approval."
    status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    with event_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(status, separators=(",", ":")) + "\n")
    print("ack_emitted_dispatch_approved|dispatch_approved|" + request_id)
    raise SystemExit(0)

ack_payload = {
    "version": "1.0",
    "source": "MIM",
    "target": "TOD",
    "generated_at": emitted_at,
    "emitted_at": emitted_at,
    "sequence": sequence,
    "source_host": source_host,
    "source_service": source_service,
    "source_instance_id": source_instance,
    "objective_id": objective_id,
    "task_id": request_id,
    "request_id": request_id,
    "correlation_id": correlation_id,
    "acknowledged": True,
    "acknowledged_at": emitted_at,
    "ack_status": "pending",
    "status": "pending",
    "decision": "pending_review",
    "reason": issue_summary or issue_code or "coordination_request_received",
    "target_dispatch_task_id": target_dispatch_task_id,
    "detail": "MIM coordination responder observed request and posted pending ACK.",
    "coordination": {
        "status": "pending",
        "phase": "request_received",
        "detail": "coordination_request_observed",
        "request_issue_code": issue_code,
        "requested_action": requested_action,
        "pending_request_id": request_id,
    },
}
ack_path.write_text(json.dumps(ack_payload, indent=2) + "\n", encoding="utf-8")

status["state"] = "ack_emitted_pending"
status["ack_written"] = True
status["last_ack_status"] = "pending"
status["message"] = "MIM coordination ACK emitted for pending coordination request."
status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
with event_log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(status, separators=(",", ":")) + "\n")

print("ack_emitted_pending|pending|" + request_id)
PY
}

echo "[mim-coordination-responder] watching ${REQUEST_FILE} every ${POLL_SECONDS}s"

while true; do
  cycle_out="$(emit_ack_cycle)"
  cycle_state="$(echo "${cycle_out}" | cut -d'|' -f1)"
  cycle_ack_status="$(echo "${cycle_out}" | cut -d'|' -f2)"
  cycle_request_id="$(echo "${cycle_out}" | cut -d'|' -f3)"
  echo "[mim-coordination-responder] state=${cycle_state} ack_status=${cycle_ack_status} request_id=${cycle_request_id}"

  if [[ "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "1" || "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "true" || "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "yes" ]]; then
    break
  fi

  sleep "${POLL_SECONDS}"
done
