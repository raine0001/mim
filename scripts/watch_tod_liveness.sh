#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
POLL_SECONDS="${POLL_SECONDS:-3}"
STALE_SECONDS="${STALE_SECONDS:-45}"
COOLDOWN_SECONDS="${COOLDOWN_SECONDS:-30}"
TRIGGER_PROTECT_SECONDS="${TRIGGER_PROTECT_SECONDS:-20}"

EVENT_LOG="${SHARED_DIR}/TOD_LIVENESS_EVENTS.latest.jsonl"
STATE_FILE="${SHARED_DIR}/.tod_liveness_state"

WATCH_FILES=(
  "TOD_MIM_TASK_ACK.latest.json"
  "TOD_MIM_TASK_RESULT.latest.json"
  "TOD_LOOP_JOURNAL.latest.json"
  "TOD_INTEGRATION_STATUS.latest.json"
)

mkdir -p "${SHARED_DIR}"
touch "${EVENT_LOG}"

last_ping_epoch=0

load_state() {
  if [[ -f "${STATE_FILE}" ]]; then
    last_ping_epoch="$(cat "${STATE_FILE}" 2>/dev/null || echo 0)"
  fi
}

save_state() {
  printf '%s' "${last_ping_epoch}" > "${STATE_FILE}"
}

oldest_age_seconds() {
  local now
  now="$(date +%s)"
  local max_age=0

  for file_name in "${WATCH_FILES[@]}"; do
    local file_path="${SHARED_DIR}/${file_name}"
    if [[ ! -f "${file_path}" ]]; then
      echo "999999"
      return 0
    fi
    local mtime
    mtime="$(stat -c %Y "${file_path}" 2>/dev/null || echo 0)"
    if [[ "${mtime}" -eq 0 ]]; then
      echo "999999"
      return 0
    fi
    local age=$((now - mtime))
    if (( age > max_age )); then
      max_age="${age}"
    fi
  done

  echo "${max_age}"
}

emit_liveness_ping() {
  local reason="$1"
  local age_seconds="$2"
  local now_iso
  now_iso="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  cat > "${SHARED_DIR}/MIM_TO_TOD_PING.latest.json" <<EOF
{
  "generated_at": "${now_iso}",
  "packet_type": "mim-tod-liveness-ping-v1",
  "source_actor": "MIM",
  "target_actor": "TOD",
  "reason": "${reason}",
  "suspected_stale_seconds": ${age_seconds},
  "requested_action": "respond_with_alive_status",
  "response_file_expected": "TOD_TO_MIM_PING.latest.json"
}
EOF

  local trigger_file="${SHARED_DIR}/MIM_TO_TOD_TRIGGER.latest.json"
  local existing_trigger=""
  local existing_age=999999

  if [[ -f "${trigger_file}" ]]; then
    existing_trigger="$(python3 - <<'PY' "${trigger_file}"
import json,sys
try:
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        print(json.load(f).get('trigger',''))
except Exception:
    print('')
PY
)"
    local trigger_mtime
    trigger_mtime="$(stat -c %Y "${trigger_file}" 2>/dev/null || echo 0)"
    if [[ "${trigger_mtime}" -gt 0 ]]; then
      existing_age=$(( $(date +%s) - trigger_mtime ))
    fi
  fi

  if [[ -n "${existing_trigger}" && "${existing_trigger}" != "liveness_ping" && ${existing_age} -lt ${TRIGGER_PROTECT_SECONDS} ]]; then
    cat >> "${EVENT_LOG}" <<EOF
{"generated_at":"${now_iso}","event":"liveness_trigger_deferred","reason":"active_non_liveness_trigger","existing_trigger":"${existing_trigger}","existing_trigger_age_seconds":${existing_age},"protect_seconds":${TRIGGER_PROTECT_SECONDS}}
EOF
    echo "[tod-liveness] deferred trigger overwrite (active=${existing_trigger}, age=${existing_age}s)"
  else
    cat > "${trigger_file}" <<EOF
{
  "generated_at": "${now_iso}",
  "packet_type": "shared-trigger-v1",
  "source_actor": "MIM",
  "target_actor": "TOD",
  "trigger": "liveness_ping",
  "artifact": "MIM_TO_TOD_PING.latest.json",
  "action_required": "pull_latest_and_ack",
  "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json"
}
EOF
  fi

  cat >> "${EVENT_LOG}" <<EOF
{"generated_at":"${now_iso}","event":"freeze_suspected","reason":"${reason}","stale_seconds":${age_seconds},"action":"ping_emitted"}
EOF

  last_ping_epoch="$(date +%s)"
  save_state
  echo "[tod-liveness] ping emitted (reason=${reason}, stale=${age_seconds}s)"
}

emit_alive_event_if_present() {
  local response_file="${SHARED_DIR}/TOD_TO_MIM_PING.latest.json"
  [[ -f "${response_file}" ]] || return 0

  local mtime
  mtime="$(stat -c %Y "${response_file}" 2>/dev/null || echo 0)"
  [[ "${mtime}" -eq 0 ]] && return 0

  local now
  now="$(date +%s)"
  local age=$((now - mtime))
  if (( age <= POLL_SECONDS + 2 )); then
    local now_iso
    now_iso="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
    cat >> "${EVENT_LOG}" <<EOF
{"generated_at":"${now_iso}","event":"alive_response_seen","file":"TOD_TO_MIM_PING.latest.json"}
EOF
    echo "[tod-liveness] alive response detected"
  fi
}

load_state

echo "[tod-liveness] watching ${SHARED_DIR} every ${POLL_SECONDS}s (stale>${STALE_SECONDS}s, protect_trigger<${TRIGGER_PROTECT_SECONDS}s)"

while true; do
  age="$(oldest_age_seconds)"
  now_epoch="$(date +%s)"
  cooldown_elapsed=$((now_epoch - last_ping_epoch))

  if (( age > STALE_SECONDS )); then
    if (( cooldown_elapsed >= COOLDOWN_SECONDS )); then
      emit_liveness_ping "tod_artifacts_stale" "${age}"
    fi
  fi

  emit_alive_event_if_present
  sleep "${POLL_SECONDS}"
done
