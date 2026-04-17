#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
START_ID="${START_ID:-8}"
COUNT="${COUNT:-5}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-2}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-45}"
STOP_FILE="${SHARED_DIR}/.dispatch_stop"
SERVICE_NAME="${SERVICE_NAME:-continuous_task_dispatch}"
AUDIT_SCRIPT="${AUDIT_SCRIPT:-${ROOT_DIR}/scripts/tod_bridge_audit.py}"
CONTRACT_TOOL="${CONTRACT_TOOL:-${ROOT_DIR}/scripts/tod_mim_contract_tools.py}"
ALLOW_LOCAL_ONLY_CANONICAL_WRITE="${ALLOW_LOCAL_ONLY_CANONICAL_WRITE:-0}"

if [[ -z "${OBJECTIVE_ID:-}" ]]; then
  OBJECTIVE_ID="$(python3 - <<'PY' "$ROOT_DIR"
import importlib.util
import sys
from pathlib import Path

root = Path(sys.argv[1])
module_path = root / "scripts" / "export_mim_context.py"
spec = importlib.util.spec_from_file_location("export_mim_context", module_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
payload, _ = module.build_payload_bundle()
print(str(payload.get("objective_active") or payload.get("current_next_objective") or "75"))
PY
)"
fi

mkdir -p "${SHARED_DIR}"

allow_local_only="$(printf '%s' "${ALLOW_LOCAL_ONLY_CANONICAL_WRITE}" | tr '[:upper:]' '[:lower:]')"
if [[ "${allow_local_only}" != "1" && "${allow_local_only}" != "true" && "${allow_local_only}" != "yes" ]]; then
  echo "[dispatch] local-only canonical writer blocked; continuous dispatch must not overwrite the canonical TOD-facing request lane at 192.168.1.120:/home/testpilot/mim/runtime/shared. Set ALLOW_LOCAL_ONLY_CANONICAL_WRITE=1 to opt in explicitly."
  exit 0
fi

json_str() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

next_bridge_meta() {
  eval "$(python3 "${ROOT_DIR}/scripts/bridge_packet_sequence.py" --shared-dir "${SHARED_DIR}" --service "${SERVICE_NAME}" --instance-id "${SERVICE_NAME}:$$")"
}

sha256_for_file() {
  local file_path="$1"
  if [[ -f "${file_path}" ]]; then
    sha256sum "${file_path}" | awk '{print $1}'
  fi
}

record_bridge_audit() {
  local event_name="$1"
  local artifact_path="$2"
  python3 "${AUDIT_SCRIPT}" \
    --event "${event_name}" \
    --caller "scripts/continuous_task_dispatch.sh" \
    --service-name "${SERVICE_NAME}" \
    --task-id "${CURRENT_TASK_ID:-}" \
    --objective-id "${OBJECTIVE_ID:-}" \
    --publish-target "/home/testpilot/mim/runtime/shared -> ${MIM_TOD_SSH_HOST:-192.168.1.120}:${MIM_TOD_SSH_REMOTE_ROOT:-/home/testpilot/mim/runtime/shared}" \
    --remote-host "${MIM_TOD_SSH_HOST:-192.168.1.120}" \
    --remote-root "${MIM_TOD_SSH_REMOTE_ROOT:-/home/testpilot/mim/runtime/shared}" \
    --artifact-path "${artifact_path}" >/dev/null
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
  local request_seq request_at request_host request_service request_instance
  local trigger_seq trigger_at trigger_host trigger_service trigger_instance
  local request_sha256=""

  CURRENT_TASK_ID="${task_id}"

  next_bridge_meta
  request_seq="$SEQUENCE"
  request_at="$EMITTED_AT"
  request_host="$SOURCE_HOST"
  request_service="$SOURCE_SERVICE"
  request_instance="$SOURCE_INSTANCE_ID"

  cat > "${SHARED_DIR}/MIM_TOD_TASK_REQUEST.latest.json" <<EOF
{
  "generated_at": "${request_at}",
  "emitted_at": "${request_at}",
  "sequence": ${request_seq},
  "packet_type": "mim-tod-task-request-v1",
  "handshake_version": "mim-tod-shared-export-v1",
  "source_host": "${request_host}",
  "source_service": "${request_service}",
  "source_instance_id": "${request_instance}",
  "correlation_id": "${corr_id}",
  "request_id": "${task_id}",
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

  python3 "${CONTRACT_TOOL}" normalize-packet \
    --kind request \
    --file "${SHARED_DIR}/MIM_TOD_TASK_REQUEST.latest.json" \
    --source-service "${SERVICE_NAME}" >/dev/null

  request_sha256="$(sha256_for_file "${SHARED_DIR}/MIM_TOD_TASK_REQUEST.latest.json")"
  record_bridge_audit "local_request_write" "${SHARED_DIR}/MIM_TOD_TASK_REQUEST.latest.json"

  next_bridge_meta
  trigger_seq="$SEQUENCE"
  trigger_at="$EMITTED_AT"
  trigger_host="$SOURCE_HOST"
  trigger_service="$SOURCE_SERVICE"
  trigger_instance="$SOURCE_INSTANCE_ID"

  cat > "${SHARED_DIR}/MIM_TO_TOD_TRIGGER.latest.json" <<EOF
{
  "generated_at": "${trigger_at}",
  "emitted_at": "${trigger_at}",
  "sequence": ${trigger_seq},
  "packet_type": "shared-trigger-v1",
  "source_actor": "MIM",
  "target_actor": "TOD",
  "source_host": "${trigger_host}",
  "source_service": "${trigger_service}",
  "source_instance_id": "${trigger_instance}",
  "trigger": "task_request_posted",
  "artifact": "MIM_TOD_TASK_REQUEST.latest.json",
  "artifact_path": "${SHARED_DIR}/MIM_TOD_TASK_REQUEST.latest.json",
  "artifact_sha256": "${request_sha256}",
  "task_id": "${task_id}",
  "correlation_id": "${corr_id}",
  "action_required": "pull_latest_and_ack",
  "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json"
}
EOF

  python3 "${CONTRACT_TOOL}" normalize-packet \
    --kind trigger \
    --file "${SHARED_DIR}/MIM_TO_TOD_TRIGGER.latest.json" \
    --source-service "${SERVICE_NAME}" >/dev/null

  record_bridge_audit "local_trigger_write" "${SHARED_DIR}/MIM_TO_TOD_TRIGGER.latest.json"

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
