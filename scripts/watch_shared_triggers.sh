#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
POLL_SECONDS="${POLL_SECONDS:-2}"
STATE_FILE="${SHARED_DIR}/.shared_trigger_state"
EVENT_LOG="${SHARED_DIR}/SHARED_TRIGGER_EVENTS.latest.jsonl"
SERVICE_NAME="${SERVICE_NAME:-mim-watch-shared-triggers}"
LOCK_FILE="${LOCK_FILE:-${SHARED_DIR}/.watch_shared_triggers.lock}"

mkdir -p "${SHARED_DIR}"
touch "${EVENT_LOG}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[shared-triggers] another instance is already active; exiting"
  exit 0
fi

declare -A LAST_MTIME

load_state() {
  if [[ ! -f "${STATE_FILE}" ]]; then
    return
  fi
  while IFS='|' read -r path mtime; do
    [[ -z "${path}" ]] && continue
    LAST_MTIME["${path}"]="${mtime}"
  done < "${STATE_FILE}"
}

save_state() {
  : > "${STATE_FILE}"
  for path in "${!LAST_MTIME[@]}"; do
    printf '%s|%s\n' "${path}" "${LAST_MTIME[$path]}" >> "${STATE_FILE}"
  done
}

actor_for_file() {
  local filename="$1"
  if [[ "${filename}" == MIM_* ]]; then
    echo "MIM"
  elif [[ "${filename}" == TOD_* ]]; then
    echo "TOD"
  else
    echo "UNKNOWN"
  fi
}

target_for_actor() {
  local actor="$1"
  if [[ "${actor}" == "MIM" ]]; then
    echo "TOD"
  elif [[ "${actor}" == "TOD" ]]; then
    echo "MIM"
  else
    echo "UNKNOWN"
  fi
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

trigger_for_file() {
  local filename="$1"
  case "${filename}" in
    MIM_TOD_TASK_REQUEST.latest.json)
      echo "task_request_posted"
      ;;
    MIM_TOD_GO_ORDER.latest.json)
      echo "go_order_posted"
      ;;
    MIM_TOD_COORDINATION_ACK.latest.json)
      echo "coordination_ack_posted"
      ;;
    MIM_TOD_REVIEW_DECISION.latest.json)
      echo "review_decision_posted"
      ;;
    MIM_TO_TOD_PING.latest.json)
      echo "liveness_ping"
      ;;
    TOD_MIM_TASK_ACK.latest.json)
      echo "task_ack_posted"
      ;;
    TOD_MIM_TASK_RESULT.latest.json)
      echo "task_result_posted"
      ;;
    TOD_MIM_COORDINATION_REQUEST.latest.json)
      echo "coordination_request_posted"
      ;;
    TOD_TO_MIM_PING.latest.json)
      echo "liveness_response_posted"
      ;;
    *)
      echo "artifact_updated"
      ;;
  esac
}

extract_ids() {
  local file_path="$1"
  python3 - <<'PY' "$file_path"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
task_id = ""
correlation_id = ""

try:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
except Exception:
    print("|")
    raise SystemExit(0)

if isinstance(data, dict):
    task_id = str(data.get("task_id") or data.get("request_id") or data.get("task") or "").strip()
    correlation_id = str(data.get("correlation_id") or "").strip()

    order = data.get("order")
    if isinstance(order, dict):
        task_id = task_id or str(order.get("task_id") or "").strip()
        correlation_id = correlation_id or str(order.get("correlation_id") or "").strip()

    coordination = data.get("coordination")
    if isinstance(coordination, dict):
        correlation_id = correlation_id or str(coordination.get("correlation_id") or "").strip()

print(f"{task_id}|{correlation_id}")
PY
}

write_alert() {
  local source_actor="$1"
  local target_actor="$2"
  local file_name="$3"
  local ts="$4"
  local trigger_name="$5"
  local task_id="$6"
  local correlation_id="$7"

  local alert_file="${SHARED_DIR}/${source_actor}_TO_${target_actor}_TRIGGER.latest.json"
  local artifact_path="${SHARED_DIR}/${file_name}"
  local artifact_sha256=""

  next_bridge_meta
  artifact_sha256="$(sha256_for_file "${artifact_path}")"

  if [[ -n "${task_id}" || -n "${correlation_id}" ]]; then
    cat > "${alert_file}" <<EOF
{
  "generated_at": "${EMITTED_AT}",
  "emitted_at": "${EMITTED_AT}",
  "sequence": ${SEQUENCE},
  "packet_type": "shared-trigger-v1",
  "source_actor": "${source_actor}",
  "target_actor": "${target_actor}",
  "source_host": "${SOURCE_HOST}",
  "source_service": "${SOURCE_SERVICE}",
  "source_instance_id": "${SOURCE_INSTANCE_ID}",
  "trigger": "${trigger_name}",
  "artifact": "${file_name}",
  "artifact_path": "${artifact_path}",
  "artifact_sha256": "${artifact_sha256}",
  "action_required": "pull_latest_and_ack",
  "ack_file_expected": "${target_actor}_TO_${source_actor}_TRIGGER_ACK.latest.json",
  "task_id": "${task_id}",
  "correlation_id": "${correlation_id}"
}
EOF
  else
    cat > "${alert_file}" <<EOF
{
  "generated_at": "${EMITTED_AT}",
  "emitted_at": "${EMITTED_AT}",
  "sequence": ${SEQUENCE},
  "packet_type": "shared-trigger-v1",
  "source_actor": "${source_actor}",
  "target_actor": "${target_actor}",
  "source_host": "${SOURCE_HOST}",
  "source_service": "${SOURCE_SERVICE}",
  "source_instance_id": "${SOURCE_INSTANCE_ID}",
  "trigger": "${trigger_name}",
  "artifact": "${file_name}",
  "artifact_path": "${artifact_path}",
  "artifact_sha256": "${artifact_sha256}",
  "action_required": "pull_latest_and_ack",
  "ack_file_expected": "${target_actor}_TO_${source_actor}_TRIGGER_ACK.latest.json"
}
EOF
  fi
}

append_event() {
  local source_actor="$1"
  local target_actor="$2"
  local file_name="$3"
  local ts="$4"
  local trigger_name="$5"
  local task_id="$6"
  local correlation_id="$7"

  next_bridge_meta

  cat >> "${EVENT_LOG}" <<EOF
{"generated_at":"${EMITTED_AT}","emitted_at":"${EMITTED_AT}","sequence":${SEQUENCE},"event":"${trigger_name}","source_actor":"${source_actor}","target_actor":"${target_actor}","source_host":"${SOURCE_HOST}","source_service":"${SOURCE_SERVICE}","source_instance_id":"${SOURCE_INSTANCE_ID}","artifact":"${file_name}","artifact_path":"${SHARED_DIR}/${file_name}","task_id":"${task_id}","correlation_id":"${correlation_id}"}
EOF
}

scan_once() {
  local changed=0
  while IFS= read -r file_path; do
    local file_name
    file_name="$(basename "${file_path}")"

    if [[ "${file_name}" == SHARED_TRIGGER_EVENTS.latest.jsonl ]]; then
      continue
    fi
    if [[ "${file_name}" == *.ACK.latest.json ]]; then
      continue
    fi
    if [[ "${file_name}" == *_TO_*_TRIGGER.latest.json ]]; then
      continue
    fi

    local mtime
    mtime="$(stat -c %Y "${file_path}" 2>/dev/null || true)"
    [[ -z "${mtime}" ]] && continue

    local previous="${LAST_MTIME[${file_path}]:-}"
    LAST_MTIME["${file_path}"]="${mtime}"

    if [[ -n "${previous}" && "${previous}" == "${mtime}" ]]; then
      continue
    fi

    local source_actor
    source_actor="$(actor_for_file "${file_name}")"
    local target_actor
    target_actor="$(target_for_actor "${source_actor}")"
    if [[ "${source_actor}" == "UNKNOWN" || "${target_actor}" == "UNKNOWN" ]]; then
      continue
    fi

    local trigger_name
    trigger_name="$(trigger_for_file "${file_name}")"
    local id_pair
    id_pair="$(extract_ids "${file_path}")"
    local task_id="${id_pair%%|*}"
    local correlation_id="${id_pair#*|}"

    local ts
    ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    if [[ "${trigger_name}" != "artifact_updated" ]]; then
      write_alert "${source_actor}" "${target_actor}" "${file_name}" "${ts}" "${trigger_name}" "${task_id}" "${correlation_id}"
    fi
    append_event "${source_actor}" "${target_actor}" "${file_name}" "${ts}" "${trigger_name}" "${task_id}" "${correlation_id}"
    changed=1
  done < <(find "${SHARED_DIR}" -maxdepth 1 -type f -name "*.latest.json" | sort)

  save_state
  return ${changed}
}

load_state
scan_once >/dev/null 2>&1 || true

echo "[shared-trigger-watch] watching ${SHARED_DIR} every ${POLL_SECONDS}s"
while true; do
  if scan_once; then
    echo "[shared-trigger-watch] trigger emitted at $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  fi
  sleep "${POLL_SECONDS}"
done
