#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
STALE_SECONDS="${STALE_SECONDS:-45}"

WATCH_FILES=(
  "TOD_MIM_TASK_ACK.latest.json"
  "TOD_MIM_TASK_RESULT.latest.json"
  "TOD_LOOP_JOURNAL.latest.json"
  "TOD_INTEGRATION_STATUS.latest.json"
  "MIM_TOD_COORDINATION_ACK.latest.json"
)

age_for_file() {
  local file_path="$1"
  if [[ ! -f "${file_path}" ]]; then
    echo "missing"
    return
  fi
  local now
  now="$(date +%s)"
  local mtime
  mtime="$(stat -c %Y "${file_path}" 2>/dev/null || echo 0)"
  if [[ "${mtime}" -eq 0 ]]; then
    echo "unknown"
    return
  fi
  echo $((now - mtime))
}

json_field() {
  local file_path="$1"
  local field="$2"
  [[ -f "${file_path}" ]] || return 1
  grep -m1 "\"${field}\"" "${file_path}" | sed -E 's/.*: *"([^"]*)".*/\1/'
}

print_header() {
  echo "TOD Status Dashboard"
  echo "time_utc: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "shared_dir: ${SHARED_DIR}"
  echo "stale_threshold_seconds: ${STALE_SECONDS}"
  echo
}

print_watchdog_state() {
  local running_count
  running_count="$(pgrep -fc 'watch_tod_liveness.sh' || true)"
  if [[ -n "${running_count}" && "${running_count}" != "0" ]]; then
    echo "watchdog_process: running (${running_count})"
  else
    echo "watchdog_process: stopped"
  fi
}

print_file_ages() {
  echo "artifact_ages:"
  local worst=0
  local worst_label="none"

  for file_name in "${WATCH_FILES[@]}"; do
    local file_path="${SHARED_DIR}/${file_name}"
    local age
    age="$(age_for_file "${file_path}")"

    if [[ "${age}" =~ ^[0-9]+$ ]]; then
      local state="fresh"
      if (( age > STALE_SECONDS )); then
        state="stale"
      fi
      printf '  - %s: %ss (%s)\n' "${file_name}" "${age}" "${state}"
      if (( age > worst )); then
        worst="${age}"
        worst_label="${file_name}"
      fi
    else
      printf '  - %s: %s\n' "${file_name}" "${age}"
    fi
  done

  echo "worst_age_seconds: ${worst} (${worst_label})"
}

print_latest_activity() {
  local ack_file="${SHARED_DIR}/TOD_MIM_TASK_ACK.latest.json"
  local result_file="${SHARED_DIR}/TOD_MIM_TASK_RESULT.latest.json"
  local ping_file="${SHARED_DIR}/MIM_TO_TOD_PING.latest.json"

  local ack_id="unknown"
  local result_id="unknown"
  local result_status="unknown"
  local ping_time="none"

  ack_id="$(json_field "${ack_file}" "request_id" || echo unknown)"
  result_id="$(json_field "${result_file}" "request_id" || echo unknown)"
  result_status="$(json_field "${result_file}" "status" || echo unknown)"
  ping_time="$(json_field "${ping_file}" "generated_at" || echo none)"

  echo
  echo "latest_activity:"
  echo "  - tod_ack_request_id: ${ack_id}"
  echo "  - tod_result_request_id: ${result_id}"
  echo "  - tod_result_status: ${result_status}"
  echo "  - mim_last_ping_at: ${ping_time}"
}

print_health_summary() {
  local result_file="${SHARED_DIR}/TOD_MIM_TASK_RESULT.latest.json"
  local compatible
  compatible="$(grep -m1 '"compatible"' "${result_file}" 2>/dev/null | sed -E 's/.*: *(true|false).*/\1/' || echo unknown)"

  local max_age=0
  for file_name in "${WATCH_FILES[@]}"; do
    local file_path="${SHARED_DIR}/${file_name}"
    local age
    age="$(age_for_file "${file_path}")"
    if [[ "${age}" =~ ^[0-9]+$ ]] && (( age > max_age )); then
      max_age="${age}"
    fi
  done

  echo
  if (( max_age > STALE_SECONDS )); then
    echo "health: DEGRADED (freeze risk detected; ping watchdog active)"
  else
    echo "health: HEALTHY"
  fi
  echo "compatibility: ${compatible}"
}

print_header
print_watchdog_state
print_file_ages
print_latest_activity
print_health_summary
