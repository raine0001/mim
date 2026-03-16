#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
POLL_SECONDS="${POLL_SECONDS:-2}"
STATE_FILE="${SHARED_DIR}/.shared_trigger_state"
EVENT_LOG="${SHARED_DIR}/SHARED_TRIGGER_EVENTS.latest.jsonl"

mkdir -p "${SHARED_DIR}"
touch "${EVENT_LOG}"

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

write_alert() {
  local source_actor="$1"
  local target_actor="$2"
  local file_name="$3"
  local ts="$4"

  local alert_file="${SHARED_DIR}/${source_actor}_TO_${target_actor}_TRIGGER.latest.json"

  cat > "${alert_file}" <<EOF
{
  "generated_at": "${ts}",
  "packet_type": "shared-trigger-v1",
  "source_actor": "${source_actor}",
  "target_actor": "${target_actor}",
  "trigger": "artifact_updated",
  "artifact": "${file_name}",
  "action_required": "pull_latest_and_ack",
  "ack_file_expected": "${target_actor}_TO_${source_actor}_TRIGGER_ACK.latest.json"
}
EOF
}

append_event() {
  local source_actor="$1"
  local target_actor="$2"
  local file_name="$3"
  local ts="$4"

  cat >> "${EVENT_LOG}" <<EOF
{"generated_at":"${ts}","event":"artifact_updated","source_actor":"${source_actor}","target_actor":"${target_actor}","artifact":"${file_name}"}
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

    local ts
    ts="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    write_alert "${source_actor}" "${target_actor}" "${file_name}" "${ts}"
    append_event "${source_actor}" "${target_actor}" "${file_name}" "${ts}"
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
