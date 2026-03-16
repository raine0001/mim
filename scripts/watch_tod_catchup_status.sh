#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"

INTEGRATION_STATUS_FILE="${INTEGRATION_STATUS_FILE:-${SHARED_DIR}/TOD_INTEGRATION_STATUS.latest.json}"
LEGACY_INTEGRATION_STATUS_FILE="${LEGACY_INTEGRATION_STATUS_FILE:-${SHARED_DIR}/TOD_integration_status.latest.json}"
LATEST_JSON_FILE="${LATEST_JSON_FILE:-${LOG_DIR}/tod_catchup_status.latest.json}"
LATEST_MD_FILE="${LATEST_MD_FILE:-${LOG_DIR}/tod_catchup_status.latest.md}"
EVENT_LOG_FILE="${EVENT_LOG_FILE:-${LOG_DIR}/tod_catchup_status.jsonl}"
STATE_FILE="${STATE_FILE:-${LOG_DIR}/tod_catchup_state.env}"
GATE_SIGNAL_FILE="${GATE_SIGNAL_FILE:-${SHARED_DIR}/TOD_CATCHUP_GATE.latest.json}"

POLL_SECONDS="${POLL_SECONDS:-30}"
MAX_STATUS_AGE_SECONDS="${MAX_STATUS_AGE_SECONDS:-900}"
CONSECUTIVE_PASS_TARGET="${CONSECUTIVE_PASS_TARGET:-3}"

mkdir -p "${LOG_DIR}"
mkdir -p "${SHARED_DIR}"
touch "${EVENT_LOG_FILE}"

if [[ -f "${STATE_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${STATE_FILE}" || true
fi

PASS_STREAK="${PASS_STREAK:-0}"

emit_status() {
  python3 - <<'PY' \
    "${INTEGRATION_STATUS_FILE}" \
    "${LATEST_JSON_FILE}" \
    "${LATEST_MD_FILE}" \
    "${EVENT_LOG_FILE}" \
    "${GATE_SIGNAL_FILE}" \
    "${MAX_STATUS_AGE_SECONDS}" \
    "${CONSECUTIVE_PASS_TARGET}" \
    "${PASS_STREAK}"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

integration_path = Path(sys.argv[1])
latest_json_path = Path(sys.argv[2])
latest_md_path = Path(sys.argv[3])
event_log_path = Path(sys.argv[4])
gate_signal_path = Path(sys.argv[5])
max_age_seconds = int(sys.argv[6])
consecutive_target = int(sys.argv[7])
prior_streak = int(sys.argv[8])

now = datetime.now(timezone.utc)

def parse_iso(ts: str):
    if not ts:
        return None
    ts = ts.strip()
    if ts.endswith('Z'):
        ts = ts[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None

if integration_path.exists():
    try:
        integration = json.loads(integration_path.read_text(encoding='utf-8-sig'))
    except Exception:
        integration = {}
else:
    integration = {}

generated_at = integration.get('generated_at', '')
generated_dt = parse_iso(generated_at)
age_seconds = int((now - generated_dt).total_seconds()) if generated_dt else 10**9
fresh = age_seconds <= max_age_seconds

compatible = bool(integration.get('compatible') is True)
alignment = integration.get('objective_alignment', {})
alignment_status = str(alignment.get('status', 'unknown'))
tod_obj = str(alignment.get('tod_current_objective', ''))
mim_obj = str(alignment.get('mim_objective_active', ''))
aligned = alignment_status in {'aligned', 'in_sync'} and tod_obj != '' and tod_obj == mim_obj

mim_refresh = integration.get('mim_refresh', {})
refresh_failure_reason = str(mim_refresh.get('failure_reason', ''))
refresh_ok = refresh_failure_reason == ''

gate_pass = all([compatible, aligned, refresh_ok, fresh])
streak = (prior_streak + 1) if gate_pass else 0
caught_up = gate_pass and streak >= consecutive_target

confidence = 'high' if gate_pass else ('medium' if compatible else 'low')

status = {
    'generated_at': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
    'type': 'tod_catchup_status_v1',
    'caught_up': caught_up,
    'catchup_gate_pass': gate_pass,
    'confidence': confidence,
    'streak': {
        'pass_streak': streak,
        'target': consecutive_target,
    },
    'freshness': {
        'integration_generated_at': generated_at,
        'age_seconds': age_seconds,
        'max_age_seconds': max_age_seconds,
        'fresh': fresh,
    },
    'compatibility': {
        'compatible': compatible,
        'alignment_status': alignment_status,
        'tod_current_objective': tod_obj,
        'mim_objective_active': mim_obj,
        'aligned': aligned,
    },
    'refresh': {
        'failure_reason': refresh_failure_reason,
        'ok': refresh_ok,
    },
}

gate_signal = {
    'generated_at': status['generated_at'],
    'type': 'tod_catchup_gate_signal_v1',
    'objective': 76,
    'promotion_ready': caught_up,
    'gate_pass': gate_pass,
    'confidence': confidence,
    'streak': {
        'pass_streak': streak,
        'target': consecutive_target,
    },
    'evidence': {
        'integration_status_file': str(integration_path),
        'catchup_status_file': str(latest_json_path),
    },
    'details': {
        'compatible': compatible,
        'aligned': aligned,
        'refresh_ok': refresh_ok,
        'fresh': fresh,
        'alignment_status': alignment_status,
        'tod_current_objective': tod_obj,
        'mim_objective_active': mim_obj,
        'refresh_failure_reason': refresh_failure_reason,
        'freshness_age_seconds': age_seconds,
        'freshness_max_age_seconds': max_age_seconds,
    },
}

latest_json_path.write_text(json.dumps(status, indent=2) + '\n', encoding='utf-8')
gate_signal_path.write_text(json.dumps(gate_signal, indent=2) + '\n', encoding='utf-8')
with event_log_path.open('a', encoding='utf-8') as f:
    f.write(json.dumps(status, separators=(',', ':')) + '\n')

md_lines = [
    '# TOD Catch-up Status',
    '',
    f"Generated: {status['generated_at']}",
    '',
    f"- caught_up: {status['caught_up']}",
    f"- catchup_gate_pass: {status['catchup_gate_pass']}",
    f"- confidence: {status['confidence']}",
    f"- pass_streak: {streak}/{consecutive_target}",
    f"- compatible: {compatible}",
    f"- alignment_status: {alignment_status}",
    f"- tod_current_objective: {tod_obj}",
    f"- mim_objective_active: {mim_obj}",
    f"- refresh_ok: {refresh_ok}",
    f"- refresh_failure_reason: {refresh_failure_reason or '(empty)'}",
    f"- freshness_age_seconds: {age_seconds}",
    f"- freshness_max_age_seconds: {max_age_seconds}",
    '',
    'Evidence source:',
    f"- {integration_path}",
]
latest_md_path.write_text('\n'.join(md_lines) + '\n', encoding='utf-8')

print(streak)
print('1' if caught_up else '0')
print(status['generated_at'])
PY
}

echo "[tod-catchup] watching ${INTEGRATION_STATUS_FILE} every ${POLL_SECONDS}s"
if [[ "${LEGACY_INTEGRATION_STATUS_FILE}" != "${INTEGRATION_STATUS_FILE}" ]]; then
    echo "[tod-catchup] mirroring compatibility alias ${LEGACY_INTEGRATION_STATUS_FILE}"
fi

while true; do
    if [[ -f "${INTEGRATION_STATUS_FILE}" && "${LEGACY_INTEGRATION_STATUS_FILE}" != "${INTEGRATION_STATUS_FILE}" ]]; then
        cp -f "${INTEGRATION_STATUS_FILE}" "${LEGACY_INTEGRATION_STATUS_FILE}" || true
    fi

  out="$(emit_status)"
  PASS_STREAK="$(echo "${out}" | sed -n '1p')"
  caught_up_flag="$(echo "${out}" | sed -n '2p')"
  stamp="$(echo "${out}" | sed -n '3p')"

printf 'PASS_STREAK=%s\n' "${PASS_STREAK}" > "${STATE_FILE}"
  if [[ "${caught_up_flag}" == "1" ]]; then
    echo "[tod-catchup] ${stamp} caught_up=true streak=${PASS_STREAK}/${CONSECUTIVE_PASS_TARGET}"
  else
    echo "[tod-catchup] ${stamp} caught_up=false streak=${PASS_STREAK}/${CONSECUTIVE_PASS_TARGET}"
  fi

  sleep "${POLL_SECONDS}"
done
