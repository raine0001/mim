#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"
STATE_FILE="${STATE_FILE:-${LOG_DIR}/objective75_overnight_state.env}"
RESULT_FILE="${RESULT_FILE:-${ROOT_DIR}/runtime/shared/TOD_MIM_TASK_RESULT.latest.json}"
ACK_WATCH_FILE="${ACK_WATCH_FILE:-${LOG_DIR}/final_mim_ack_watch.latest.json}"

LATEST_JSON="${LATEST_JSON:-${LOG_DIR}/objective75_heartbeat.latest.json}"
LATEST_MD="${LATEST_MD:-${LOG_DIR}/objective75_heartbeat.latest.md}"
EVENT_LOG="${EVENT_LOG:-${LOG_DIR}/objective75_heartbeat.jsonl}"

POLL_SECONDS="${POLL_SECONDS:-5}"
HEARTBEAT_CYCLE_STEP="${HEARTBEAT_CYCLE_STEP:-3}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-0}"

mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG}"

if ! [[ "${HEARTBEAT_CYCLE_STEP}" =~ ^[0-9]+$ ]] || (( HEARTBEAT_CYCLE_STEP < 1 )); then
  HEARTBEAT_CYCLE_STEP=3
fi

start_epoch="$(date +%s)"
last_reported_task_num=""

while true; do
  now_epoch="$(date +%s)"
  now_iso="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  if (( TIMEOUT_SECONDS > 0 )) && (( now_epoch - start_epoch >= TIMEOUT_SECONDS )); then
    python3 - "${LATEST_JSON}" "${LATEST_MD}" "${EVENT_LOG}" "${now_iso}" <<'PY'
import json
import sys
from pathlib import Path

latest_json = Path(sys.argv[1])
latest_md = Path(sys.argv[2])
event_log = Path(sys.argv[3])
now_iso = sys.argv[4]

payload = {
    "generated_at": now_iso,
    "status": "timeout",
    "message": "Heartbeat watcher timed out.",
}

latest_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
latest_md.write_text(
    "# Objective 75 Heartbeat\n\n"
    f"- generated_at: {payload['generated_at']}\n"
    f"- status: {payload['status']}\n"
    f"- message: {payload['message']}\n",
    encoding="utf-8",
)
with event_log.open("a", encoding="utf-8") as f:
    f.write(json.dumps(payload, separators=(",", ":")) + "\n")
PY
    exit 1
  fi

    output="$(python3 - \
    "${STATE_FILE}" \
    "${RESULT_FILE}" \
    "${ACK_WATCH_FILE}" \
    "${LATEST_JSON}" \
    "${LATEST_MD}" \
    "${EVENT_LOG}" \
    "${now_iso}" \
    "${HEARTBEAT_CYCLE_STEP}" \
    "${last_reported_task_num}" <<'PY'
import json
import re
import sys
from pathlib import Path

state_file = Path(sys.argv[1])
result_file = Path(sys.argv[2])
ack_watch_file = Path(sys.argv[3])
latest_json = Path(sys.argv[4])
latest_md = Path(sys.argv[5])
event_log = Path(sys.argv[6])
now_iso = sys.argv[7]
cycle_step = int(sys.argv[8])
last_reported_raw = sys.argv[9].strip()

def read_json(path: Path):
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), "ok"
    except Exception as exc:
        return None, f"parse_error:{exc}"

def file_meta(path: Path):
    if not path.exists():
        return {"exists": False, "mtime_epoch": None, "age_seconds": None}
    stat = path.stat()
    return {
        "exists": True,
        "mtime_epoch": int(stat.st_mtime),
        "age_seconds": max(0, int(__import__("time").time()) - int(stat.st_mtime)),
    }

def read_task_num(path: Path):
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    match = re.search(r"^TASK_NUM=(\d+)\s*$", text, flags=re.MULTILINE)
    if not match:
        return None
    return int(match.group(1))

task_num = read_task_num(state_file)
result, result_state = read_json(result_file)
ack_watch, ack_state = read_json(ack_watch_file)
result = result or {}
ack_watch = ack_watch or {}

state_meta = file_meta(state_file)
result_meta = file_meta(result_file)
ack_watch_meta = file_meta(ack_watch_file)

reg = result.get("regression_snapshot") if isinstance(result, dict) else {}
if not isinstance(reg, dict):
    reg = {}

ack_errors = ack_watch.get("errors", []) if isinstance(ack_watch, dict) else []
if not isinstance(ack_errors, list):
    ack_errors = [str(ack_errors)]

payload = {
    "generated_at": now_iso,
    "heartbeat_cycle_step": cycle_step,
    "task_num": task_num,
    "last_reported_task_num": int(last_reported_raw) if last_reported_raw.isdigit() else None,
    "request_id": str(result.get("request_id", "")).strip(),
    "result_generated_at": str(result.get("generated_at", "")).strip(),
    "result_completed_at": str(result.get("completed_at", "")).strip(),
    "regression_signature": str(reg.get("signature", "")).strip(),
    "regression_passed": reg.get("passed"),
    "regression_failed": reg.get("failed"),
    "regression_total": reg.get("total"),
    "ack_watch_status": str(ack_watch.get("status", "unknown")).strip() or "unknown",
    "ack_watch_valid": bool(ack_watch.get("valid", False)),
    "ack_watch_error_count": len([e for e in ack_errors if str(e).strip()]),
    "ack_watch_pending_request_id": str(ack_watch.get("pending_request_id", "")).strip(),
    "ack_watch_errors": [str(e) for e in ack_errors if str(e).strip()][:10],
    "read_state": {
        "result_file": result_state,
        "ack_watch_file": ack_state,
    },
    "file_meta": {
        "state_file": state_meta,
        "result_file": result_meta,
        "ack_watch_file": ack_watch_meta,
    },
}

latest_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
latest_md.write_text(
    "# Objective 75 Heartbeat\n\n"
    f"- generated_at: {payload['generated_at']}\n"
    f"- task_num: {payload['task_num']}\n"
    f"- request_id: {payload['request_id']}\n"
    f"- regression_signature: {payload['regression_signature']}\n"
    f"- regression_counts: {payload['regression_passed']}/{payload['regression_failed']}/{payload['regression_total']}\n"
    f"- ack_watch: {payload['ack_watch_status']} (valid={payload['ack_watch_valid']}, errors={payload['ack_watch_error_count']})\n",
    encoding="utf-8",
)

should_emit = False
emit_reason = "task_unavailable"
if task_num is not None:
    if not last_reported_raw:
        should_emit = True
        emit_reason = "first_observation"
    else:
        try:
            last_reported = int(last_reported_raw)
            if task_num >= last_reported + cycle_step:
                should_emit = True
                emit_reason = "cycle_step_reached"
            else:
                emit_reason = "awaiting_cycle_step"
        except ValueError:
            should_emit = True
            emit_reason = "invalid_last_reported_task_num"

payload["emit_reason"] = emit_reason
payload["emit"] = should_emit

if should_emit:
    with event_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":")) + "\n")
    summary = (
        "[objective75-heartbeat] "
        f"task_num={payload['task_num']} "
        f"request_id={payload['request_id']} "
        f"reg_sig={payload['regression_signature']} "
        f"ack={payload['ack_watch_status']} "
        f"ack_valid={payload['ack_watch_valid']} "
        f"reason={payload['emit_reason']}"
    )
    print("EMIT|" + str(task_num) + "|" + summary)
else:
    print("SKIP||")
PY
)"

  if [[ "${output}" == EMIT\|* ]]; then
    new_task_num="${output#EMIT|}"
    new_task_num="${new_task_num%%|*}"
    summary_line="${output#EMIT|${new_task_num}|}"
    if [[ -n "${summary_line}" ]]; then
      echo "${summary_line}"
    fi
    if [[ -n "${new_task_num}" ]]; then
      last_reported_task_num="${new_task_num}"
    fi
  fi

  sleep "${POLL_SECONDS}"
done
