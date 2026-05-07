#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"
TOD_CONSOLE_URL="${TOD_CONSOLE_URL:-http://192.168.1.161:8844}"
PROBE_FILE="${PROBE_FILE:-${SHARED_DIR}/TOD_CONSOLE_PROBE.latest.json}"
EVENT_LOG_FILE="${EVENT_LOG_FILE:-${LOG_DIR}/tod_console_probe.jsonl}"
POLL_SECONDS="${POLL_SECONDS:-60}"
RUN_ONCE="${RUN_ONCE:-0}"

mkdir -p "${SHARED_DIR}"
mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG_FILE}"

emit_probe() {
  python3 - <<'PY' \
    "${TOD_CONSOLE_URL}" \
    "${PROBE_FILE}" \
    "${EVENT_LOG_FILE}"
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


console_url = str(sys.argv[1]).strip()
probe_file = Path(sys.argv[2])
event_log_file = Path(sys.argv[3])

status = 'unreachable'
http_status = None
detail = 'Console probe has not completed yet.'

request = urllib.request.Request(console_url, method='GET')
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        http_status = int(response.status)
        response.read(256)
        status = 'reachable'
        detail = f'Read-only probe reached TOD console with HTTP {http_status}.'
except urllib.error.HTTPError as exc:
    http_status = int(exc.code)
    status = 'http_error'
    detail = f'Read-only probe reached TOD console but received HTTP {http_status}.'
except Exception as exc:
    detail = f'Read-only probe could not reach TOD console: {exc}'

payload = {
    'generated_at': now_iso(),
    'type': 'tod_console_probe_v1',
    'console_url': console_url,
    'status': status,
    'http_status': http_status,
    'detail': detail,
    'authority': {
        'authoritative': False,
        'role': 'supplemental_liveness_evidence_only',
    },
}

probe_file.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
with event_log_file.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps(payload, separators=(',', ':')) + '\n')
PY
}

while true; do
  emit_probe
  if [[ "${RUN_ONCE}" == "1" ]]; then
    break
  fi
  sleep "${POLL_SECONDS}"
done