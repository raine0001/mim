#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8001/mim/ui/health}"
TARGET_SERVICE="${TARGET_SERVICE:-mim-desktop-shell.service}"

POLL_SECONDS="${POLL_SECONDS:-8}"
DEGRADED_THRESHOLD="${DEGRADED_THRESHOLD:-4}"
RESTART_COOLDOWN_SECONDS="${RESTART_COOLDOWN_SECONDS:-180}"
STARTUP_GRACE_SECONDS="${STARTUP_GRACE_SECONDS:-45}"

EVENT_LOG="${LOG_DIR}/mim_ui_health_watch_events.jsonl"
STATE_FILE="${LOG_DIR}/mim_ui_health_watch_state.env"

mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG}"

watch_started_epoch="$(date +%s)"
last_restart_epoch=0
consecutive_actionable_degraded=0

if [[ -f "${STATE_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${STATE_FILE}" || true
fi

save_state() {
  cat > "${STATE_FILE}" <<EOF
last_restart_epoch=${last_restart_epoch}
consecutive_actionable_degraded=${consecutive_actionable_degraded}
watch_started_epoch=${watch_started_epoch}
EOF
}

log_event() {
  local event_type="$1"
  local reason_raw="$2"
  local streak="$3"
  local cooldown_elapsed="$4"
  local now_iso
  now_iso="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  local reason
  reason="${reason_raw//\"/\'}"

  printf '{"generated_at":"%s","event":"%s","reason":"%s","streak":%s,"cooldown_elapsed":%s}\n' \
    "${now_iso}" "${event_type}" "${reason}" "${streak}" "${cooldown_elapsed}" >> "${EVENT_LOG}"
}

service_active() {
  if systemctl --user is-active --quiet "${TARGET_SERVICE}"; then
    echo "1"
  else
    echo "0"
  fi
}

evaluate_health() {
  local payload
  payload="$(curl -fsS --max-time 6 "${HEALTH_URL}" 2>/dev/null || true)"
  if [[ -z "${payload}" ]]; then
    echo "1|endpoint_unreachable"
    return 0
  fi

  python3 - <<'PY' "${payload}"
import json
import sys

raw = sys.argv[1]

try:
    data = json.loads(raw)
except Exception:
    print("1|invalid_health_payload")
    raise SystemExit(0)

checks = data.get("checks", {}) if isinstance(data, dict) else {}

db_ok = bool((checks.get("database") or {}).get("ok", False))
camera = checks.get("camera") or {}
microphone = checks.get("microphone") or {}

camera_source_health = str(camera.get("source_health", "") or "")
camera_source_status = str(camera.get("source_status", "") or "")
mic_source_health = str(microphone.get("source_health", "") or "")
mic_source_status = str(microphone.get("source_status", "") or "")

camera_age = camera.get("age_seconds")
mic_age = microphone.get("age_seconds")

actionable = False
reasons: list[str] = []

if not db_ok:
    actionable = True
    reasons.append("database_unhealthy")

if camera_source_status and camera_source_status != "active":
    actionable = True
    reasons.append(f"camera_status_{camera_source_status}")
if mic_source_status and mic_source_status != "active":
    actionable = True
    reasons.append(f"microphone_status_{mic_source_status}")

if camera_source_health and camera_source_health != "healthy":
    actionable = True
    reasons.append(f"camera_health_{camera_source_health}")
if mic_source_health and mic_source_health != "healthy":
    actionable = True
    reasons.append(f"microphone_health_{mic_source_health}")

# Missing source rows is actionable; stale ages alone are not, to avoid false restart loops when idle.
if camera_age is None:
    actionable = True
    reasons.append("camera_missing")
if mic_age is None:
    actionable = True
    reasons.append("microphone_missing")

if actionable:
    print(f"1|{'|'.join(reasons)}")
else:
    print("0|healthy_or_idle")
PY
}

restart_target() {
  if systemctl --user restart "${TARGET_SERVICE}"; then
    return 0
  fi
  return 1
}

echo "[mim-ui-health-watch] watching ${HEALTH_URL} every ${POLL_SECONDS}s; threshold=${DEGRADED_THRESHOLD}, cooldown=${RESTART_COOLDOWN_SECONDS}s"

while true; do
  now_epoch="$(date +%s)"
  uptime_seconds=$((now_epoch - watch_started_epoch))

  active_now="$(service_active)"
  eval_result="$(evaluate_health)"
  actionable_flag="${eval_result%%|*}"
  reason="${eval_result#*|}"

  if [[ "${active_now}" != "1" ]]; then
    actionable_flag="1"
    reason="service_inactive|${reason}"
  fi

  if (( uptime_seconds < STARTUP_GRACE_SECONDS )); then
    if [[ "${actionable_flag}" == "1" ]]; then
      log_event "degraded_in_grace" "${reason}|uptime=${uptime_seconds}" "${consecutive_actionable_degraded}" "0"
    fi
    sleep "${POLL_SECONDS}"
    continue
  fi

  if [[ "${actionable_flag}" == "1" ]]; then
    consecutive_actionable_degraded=$((consecutive_actionable_degraded + 1))
  else
    consecutive_actionable_degraded=0
  fi

  cooldown_elapsed=$((now_epoch - last_restart_epoch))
  if (( last_restart_epoch == 0 )); then
    cooldown_elapsed=999999
  fi
  should_restart=0
  if (( consecutive_actionable_degraded >= DEGRADED_THRESHOLD )) && (( cooldown_elapsed >= RESTART_COOLDOWN_SECONDS )); then
    should_restart=1
  fi

  if (( should_restart == 1 )); then
    if restart_target; then
      last_restart_epoch="$(date +%s)"
      log_event "restart_applied" "${reason}" "${consecutive_actionable_degraded}" "${cooldown_elapsed}"
      consecutive_actionable_degraded=0
    else
      log_event "restart_failed" "${reason}" "${consecutive_actionable_degraded}" "${cooldown_elapsed}"
    fi
  else
    event_name="healthy"
    if [[ "${actionable_flag}" == "1" ]]; then
      event_name="degraded_no_action"
    fi
    log_event "${event_name}" "${reason}" "${consecutive_actionable_degraded}" "${cooldown_elapsed}"
  fi

  save_state
  sleep "${POLL_SECONDS}"
done
