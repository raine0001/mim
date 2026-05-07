#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="${LOG_FILE:-${ROOT_DIR}/runtime/logs/objective75_overnight.log}"
ALERT_FILE="${ALERT_FILE:-${ROOT_DIR}/runtime/logs/objective75_overnight_alert.latest.json}"
EVENT_LOG="${EVENT_LOG:-${ROOT_DIR}/runtime/logs/objective75_overnight_alerts.jsonl}"
POLL_SECONDS="${POLL_SECONDS:-30}"
MAX_IDLE_SECONDS="${MAX_IDLE_SECONDS:-1200}"
COOLDOWN_SECONDS="${COOLDOWN_SECONDS:-300}"

mkdir -p "$(dirname "${ALERT_FILE}")"
mkdir -p "$(dirname "${EVENT_LOG}")"
touch "${EVENT_LOG}"

last_alert_epoch=0

last_cycle_pass_epoch() {
  [[ -f "${LOG_FILE}" ]] || { echo 0; return 0; }

  python3 - <<'PY' "${LOG_FILE}"
import re
import sys
from datetime import datetime, timezone

path = sys.argv[1]
pattern = re.compile(r'^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\].*Cycle PASS')
latest = None

try:
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            m = pattern.match(line)
            if m:
                latest = m.group(1)
except FileNotFoundError:
    print(0)
    raise SystemExit(0)

if not latest:
    print(0)
    raise SystemExit(0)

dt = datetime.strptime(latest, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
print(int(dt.timestamp()))
PY
}

emit_alert() {
  local idle_seconds="$1"
  local now_iso
  now_iso="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  cat > "${ALERT_FILE}" <<EOF
{
  "generated_at": "${now_iso}",
  "type": "objective75_overnight_cycle_pass_stale",
  "status": "alert",
  "max_idle_seconds": ${MAX_IDLE_SECONDS},
  "observed_idle_seconds": ${idle_seconds},
  "log_file": "${LOG_FILE}",
  "message": "No new Cycle PASS observed within threshold"
}
EOF

  cat >> "${EVENT_LOG}" <<EOF
{"generated_at":"${now_iso}","event":"cycle_pass_stale_alert","observed_idle_seconds":${idle_seconds},"max_idle_seconds":${MAX_IDLE_SECONDS}}
EOF

  echo "[objective75-watch] ALERT idle=${idle_seconds}s threshold=${MAX_IDLE_SECONDS}s"
  last_alert_epoch="$(date +%s)"
}

echo "[objective75-watch] watching ${LOG_FILE} every ${POLL_SECONDS}s (idle>${MAX_IDLE_SECONDS}s)"

while true; do
  now_epoch="$(date +%s)"
  pass_epoch="$(last_cycle_pass_epoch)"

  if (( pass_epoch == 0 )); then
    idle_seconds=999999
  else
    idle_seconds=$((now_epoch - pass_epoch))
  fi

  cooldown_elapsed=$((now_epoch - last_alert_epoch))

  if (( idle_seconds > MAX_IDLE_SECONDS )) && (( cooldown_elapsed >= COOLDOWN_SECONDS )); then
    emit_alert "${idle_seconds}"
  fi

  sleep "${POLL_SECONDS}"
done
