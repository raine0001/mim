#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
OBJECTIVE_ID="${OBJECTIVE_ID:-75}"
START_ID="${START_ID:-8}"
COUNT="${COUNT:-5}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-2}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-45}"
STOP_FILE="${SHARED_DIR}/.dispatch_stop"

mkdir -p "${SHARED_DIR}"

json_str() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

extract_json_string() {
  local file_path="$1"
  local key="$2"
  [[ -f "${file_path}" ]] || return 1
  grep -m1 "\"${key}\"" "${file_path}" | sed -E 's/.*: *"([^"]*)".*/\1/'
}

extract_json_bool() {
  local file_path="$1"
  local key="$2"
  [[ -f "${file_path}" ]] || return 1
  grep -m1 "\"${key}\"" "${file_path}" | sed -E 's/.*: *(true|false).*/\1/'
}

wait_for_task_completion() {
  local task_id="$1"
  local started_at
  started_at="$(date +%s)"

  while true; do
    if [[ -f "${STOP_FILE}" ]]; then
      echo "[dispatch] stop file detected (${STOP_FILE}); exiting loop"
      return 2
    fi

    local ack_request_id=""
    local result_request_id=""
    local result_status=""

    ack_request_id="$(extract_json_string "${SHARED_DIR}/TOD_MIM_TASK_ACK.latest.json" "request_id" || true)"
    result_request_id="$(extract_json_string "${SHARED_DIR}/TOD_MIM_TASK_RESULT.latest.json" "request_id" || true)"
    result_status="$(extract_json_string "${SHARED_DIR}/TOD_MIM_TASK_RESULT.latest.json" "status" || true)"

    if [[ "${ack_request_id}" == "${task_id}" && "${result_request_id}" == "${task_id}" && "${result_status}" == "completed" ]]; then
      local compatible=""
      compatible="$(extract_json_bool "${SHARED_DIR}/TOD_MIM_TASK_RESULT.latest.json" "compatible" || true)"
      echo "[dispatch] ${task_id}: ACK+RESULT completed (compatible=${compatible:-unknown})"
      return 0
    fi

    local now
    now="$(date +%s)"
    if (( now - started_at >= TIMEOUT_SECONDS )); then
      echo "[dispatch] ${task_id}: timed out waiting for ACK/RESULT"
      return 1
    fi

    sleep 1
  done
}

send_task() {
  local numeric_id="$1"
  local task_id="objective-${OBJECTIVE_ID}-task-$(printf '%03d' "${numeric_id}")"
  local corr_id="obj${OBJECTIVE_ID}-task$(printf '%03d' "${numeric_id}")"
  local now
  now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  cat > "${SHARED_DIR}/MIM_TOD_TASK_REQUEST.latest.json" <<EOF
{
  "generated_at": "${now}",
  "packet_type": "mim-tod-task-request-v1",
  "handshake_version": "mim-tod-shared-export-v1",
  "correlation_id": "${corr_id}",
  "task_id": "${task_id}",
  "objective_id": "${OBJECTIVE_ID}",
  "title": "Continuous dispatch sample ${numeric_id}",
  "scope": "Execute one standard MIM->TOD loop cycle and publish ACK/RESULT.",
  "constraints": [
    "No schema changes",
    "No objective drift",
    "Shared path only"
  ],
  "acceptance_criteria": [
    "TOD ACK request_id matches task_id",
    "TOD RESULT request_id matches task_id",
    "TOD RESULT status is completed"
  ],
  "required_tests": [
    "ACK freshness",
    "RESULT freshness"
  ],
  "submission_requirements": [
    "Publish TOD_MIM_TASK_ACK.latest.json",
    "Publish TOD_MIM_TASK_RESULT.latest.json"
  ],
  "requested_by": "MIM",
  "priority": "high"
}
EOF

  cat > "${SHARED_DIR}/MIM_TO_TOD_TRIGGER.latest.json" <<EOF
{
  "generated_at": "${now}",
  "packet_type": "shared-trigger-v1",
  "source_actor": "MIM",
  "target_actor": "TOD",
  "trigger": "task_request_posted",
  "artifact": "MIM_TOD_TASK_REQUEST.latest.json",
  "action_required": "pull_latest_and_ack",
  "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json"
}
EOF

  echo "[dispatch] sent ${task_id}"
  wait_for_task_completion "${task_id}"
}

echo "[dispatch] starting continuous dispatch: objective=${OBJECTIVE_ID}, start=${START_ID}, count=${COUNT}, interval=${INTERVAL_SECONDS}s"
echo "[dispatch] create ${STOP_FILE} to stop early"

sent=0
ok=0
failed=0

for ((i=0; i<COUNT; i++)); do
  if [[ -f "${STOP_FILE}" ]]; then
    echo "[dispatch] stop file detected before next send; exiting"
    break
  fi

  task_num=$((START_ID + i))
  sent=$((sent + 1))

  if send_task "${task_num}"; then
    ok=$((ok + 1))
  else
    code=$?
    if [[ ${code} -eq 2 ]]; then
      break
    fi
    failed=$((failed + 1))
  fi

  sleep "${INTERVAL_SECONDS}"
done

echo "[dispatch] summary: sent=${sent} ok=${ok} failed=${failed}"
