#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"

ACK_FILE="${ACK_FILE:-${SHARED_DIR}/MIM_TOD_COORDINATION_ACK.latest.json}"
REQ_FILE="${REQ_FILE:-${SHARED_DIR}/TOD_MIM_COORDINATION_REQUEST.latest.json}"
STATUS_FILE="${STATUS_FILE:-${LOG_DIR}/final_mim_ack_watch.latest.json}"
EVENT_LOG="${EVENT_LOG:-${LOG_DIR}/final_mim_ack_watch.jsonl}"

POLL_SECONDS="${POLL_SECONDS:-5}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-0}"

mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG}"

start_epoch="$(date +%s)"

while true; do
  now_iso="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  now_epoch="$(date +%s)"

  if (( TIMEOUT_SECONDS > 0 )) && (( now_epoch - start_epoch >= TIMEOUT_SECONDS )); then
    cat > "${STATUS_FILE}" <<EOF
{
  "generated_at": "${now_iso}",
  "status": "timeout",
  "ack_file": "${ACK_FILE}",
  "request_file": "${REQ_FILE}",
  "message": "Timed out waiting for final MIM ACK."
}
EOF
    printf '{"generated_at":"%s","event":"timeout"}\n' "${now_iso}" >> "${EVENT_LOG}"
    exit 1
  fi

  python3 - "${ACK_FILE}" "${REQ_FILE}" "${STATUS_FILE}" "${EVENT_LOG}" "${now_iso}" <<'PY'
import json
import sys
from pathlib import Path

ack_path = Path(sys.argv[1])
req_path = Path(sys.argv[2])
status_path = Path(sys.argv[3])
event_log = Path(sys.argv[4])
now_iso = sys.argv[5]

required_keys = {
    "acknowledged",
    "acknowledged_at",
    "request_id",
    "decision",
    "reason",
    "target_dispatch_task_id",
}
allowed_decisions = {"dispatch_approved", "dispatch_deferred", "dispatch_rejected"}

status = {
    "generated_at": now_iso,
    "status": "waiting",
    "valid": False,
    "pending_request_id": "",
    "ack_request_id": "",
    "decision": "",
    "errors": [],
}

if req_path.exists():
    try:
        req = json.loads(req_path.read_text(encoding="utf-8-sig"))
        status["pending_request_id"] = str(req.get("request_id", "")).strip()
    except Exception as exc:
        status["errors"].append(f"request_read_error: {exc}")
else:
    status["errors"].append("request_file_missing")

if not ack_path.exists():
    status["errors"].append("ack_file_missing")
else:
    try:
        ack = json.loads(ack_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        status["errors"].append(f"ack_read_error: {exc}")
        ack = {}

    if set(ack.keys()) != required_keys:
        status["errors"].append("ack_schema_mismatch")

    status["ack_request_id"] = str(ack.get("request_id", "")).strip()
    status["decision"] = str(ack.get("decision", "")).strip()

    if ack.get("acknowledged") is not True:
        status["errors"].append("acknowledged_not_true")
    if not isinstance(ack.get("acknowledged_at"), str) or not ack.get("acknowledged_at", "").strip():
        status["errors"].append("acknowledged_at_missing")
    if not isinstance(ack.get("reason"), str) or not ack.get("reason", "").strip():
        status["errors"].append("reason_missing")
    if not isinstance(ack.get("target_dispatch_task_id"), str) or not ack.get("target_dispatch_task_id", "").strip():
        status["errors"].append("target_dispatch_task_id_missing")
    if status["decision"] not in allowed_decisions:
        status["errors"].append("decision_invalid")
    if status["pending_request_id"] and status["ack_request_id"] != status["pending_request_id"]:
        status["errors"].append("request_id_mismatch")

if not status["errors"]:
    status["valid"] = True
    status["status"] = "final_ack_valid"

status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
with event_log.open("a", encoding="utf-8") as f:
    f.write(json.dumps(status, separators=(",", ":")) + "\n")

print("1" if status["valid"] else "0")
PY

  if [[ "$(tail -n 1 "${STATUS_FILE}" | tr -d '\r')" == "}" ]]; then
    current_status="$(python3 - "${STATUS_FILE}" <<'PY'
import json,sys
from pathlib import Path
d=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
print(d.get('status','waiting'))
PY
)"
    if [[ "${current_status}" == "final_ack_valid" ]]; then
      exit 0
    fi
  fi

  sleep "${POLL_SECONDS}"
done
