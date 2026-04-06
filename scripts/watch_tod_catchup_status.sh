#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"

INTEGRATION_STATUS_FILE="${INTEGRATION_STATUS_FILE:-${SHARED_DIR}/TOD_INTEGRATION_STATUS.latest.json}"
LEGACY_INTEGRATION_STATUS_FILE="${LEGACY_INTEGRATION_STATUS_FILE:-${SHARED_DIR}/TOD_integration_status.latest.json}"
HANDSHAKE_FILE="${HANDSHAKE_FILE:-${SHARED_DIR}/MIM_TOD_HANDSHAKE_PACKET.latest.json}"
MANIFEST_FILE="${MANIFEST_FILE:-${SHARED_DIR}/MIM_MANIFEST.latest.json}"
CONTEXT_EXPORT_FILE="${CONTEXT_EXPORT_FILE:-${SHARED_DIR}/MIM_CONTEXT_EXPORT.latest.json}"
TASK_REQUEST_FILE="${TASK_REQUEST_FILE:-${SHARED_DIR}/MIM_TOD_TASK_REQUEST.latest.json}"
COORDINATION_ACK_FILE="${COORDINATION_ACK_FILE:-${SHARED_DIR}/MIM_TOD_COORDINATION_ACK.latest.json}"
LATEST_JSON_FILE="${LATEST_JSON_FILE:-${LOG_DIR}/tod_catchup_status.latest.json}"
LATEST_MD_FILE="${LATEST_MD_FILE:-${LOG_DIR}/tod_catchup_status.latest.md}"
EVENT_LOG_FILE="${EVENT_LOG_FILE:-${LOG_DIR}/tod_catchup_status.jsonl}"
STATE_FILE="${STATE_FILE:-${LOG_DIR}/tod_catchup_state.env}"
GATE_SIGNAL_FILE="${GATE_SIGNAL_FILE:-${SHARED_DIR}/TOD_CATCHUP_GATE.latest.json}"

POLL_SECONDS="${POLL_SECONDS:-30}"
MAX_STATUS_AGE_SECONDS="${MAX_STATUS_AGE_SECONDS:-900}"
CONSECUTIVE_PASS_TARGET="${CONSECUTIVE_PASS_TARGET:-3}"
RUN_ONCE="${RUN_ONCE:-0}"
AUTO_REBUILD_INTEGRATION_STATUS="${AUTO_REBUILD_INTEGRATION_STATUS:-0}"
AUTO_RECONCILE_TASK_RESULT="${AUTO_RECONCILE_TASK_RESULT:-0}"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"

mkdir -p "${LOG_DIR}"
mkdir -p "${SHARED_DIR}"
touch "${EVENT_LOG_FILE}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi

should_run_flag() {
    local value
    value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    [[ "${value}" == "1" || "${value}" == "true" || "${value}" == "yes" ]]
}

run_auto_repairs() {
    if should_run_flag "${AUTO_REBUILD_INTEGRATION_STATUS}"; then
        if "${PYTHON_BIN}" "${ROOT_DIR}/scripts/rebuild_tod_integration_status.py" --shared-dir "${SHARED_DIR}" --mirror-legacy-alias >/dev/null 2>&1; then
            echo "[tod-catchup] auto-rebuilt canonical TOD integration status"
        else
            echo "[tod-catchup] WARN auto-rebuild of canonical TOD integration status failed" >&2
        fi
    fi

    if should_run_flag "${AUTO_RECONCILE_TASK_RESULT}"; then
        if "${PYTHON_BIN}" "${ROOT_DIR}/scripts/reconcile_tod_task_result.py" --shared-dir "${SHARED_DIR}" >/dev/null 2>&1; then
            echo "[tod-catchup] auto-reconciled TOD task result contract"
        else
            echo "[tod-catchup] WARN auto-reconcile of TOD task result failed" >&2
        fi
    fi
}

if [[ -f "${STATE_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${STATE_FILE}" || true
fi

PASS_STREAK="${PASS_STREAK:-0}"

emit_status() {
  python3 - <<'PY' \
        "${ROOT_DIR}" \
    "${INTEGRATION_STATUS_FILE}" \
        "${HANDSHAKE_FILE}" \
        "${MANIFEST_FILE}" \
        "${CONTEXT_EXPORT_FILE}" \
        "${TASK_REQUEST_FILE}" \
        "${COORDINATION_ACK_FILE}" \
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

root_dir = Path(sys.argv[1])
sys.path.insert(0, str(root_dir / 'scripts'))

from tod_status_signal_lib import build_publisher_warning, read_json  # type: ignore

integration_path = Path(sys.argv[2])
handshake_path = Path(sys.argv[3])
manifest_path = Path(sys.argv[4])
context_export_path = Path(sys.argv[5])
task_request_path = Path(sys.argv[6])
coordination_ack_path = Path(sys.argv[7])
latest_json_path = Path(sys.argv[8])
latest_md_path = Path(sys.argv[9])
event_log_path = Path(sys.argv[10])
gate_signal_path = Path(sys.argv[11])
max_age_seconds = int(sys.argv[12])
consecutive_target = int(sys.argv[13])
prior_streak = int(sys.argv[14])

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

def non_empty(value):
    return isinstance(value, str) and bool(value.strip())

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

objective_value = tod_obj or mim_obj
if isinstance(objective_value, str) and objective_value.isdigit():
    objective_value = int(objective_value)
elif objective_value == '':
    objective_value = None

mim_refresh = integration.get('mim_refresh', {})
refresh_failure_reason = str(mim_refresh.get('failure_reason', ''))
published_handshake = integration.get('mim_handshake', {})
shared_handshake = read_json(handshake_path)
shared_manifest = read_json(manifest_path)
context_export = read_json(context_export_path)
task_request = read_json(task_request_path)
coordination_ack = read_json(coordination_ack_path)
shared_truth = (shared_handshake or {}).get('truth', {})
shared_manifest_payload = (shared_manifest or {}).get('manifest', {})
shared_artifacts_present = shared_handshake is not None and shared_manifest is not None
shared_schema = shared_truth.get('schema_version') or shared_manifest_payload.get('schema_version')
shared_release = shared_truth.get('release_tag') or shared_manifest_payload.get('release_tag')
shared_objective = shared_truth.get('objective_active')

refresh_evidence_checks = {
    'copied_manifest': mim_refresh.get('copied_manifest') is True,
    'source_manifest': non_empty(mim_refresh.get('source_manifest')),
    'source_handshake_packet': non_empty(mim_refresh.get('source_handshake_packet')),
    'handshake_available': published_handshake.get('available') is True,
    'objective_match': str(published_handshake.get('objective_active') or '') == str(shared_objective or ''),
    'schema_match': str(published_handshake.get('schema_version') or '') == str(shared_schema or ''),
    'release_match': str(published_handshake.get('release_tag') or '') == str(shared_release or ''),
    'mim_schema_match': str(integration.get('mim_schema') or '') == str(shared_schema or ''),
}
refresh_evidence_ok = (not shared_artifacts_present) or all(refresh_evidence_checks.values())
refresh_ok = refresh_failure_reason == '' and refresh_evidence_ok

publisher_warning = build_publisher_warning(
    integration=integration,
    context_export=context_export,
    handshake=shared_handshake,
    task_request=task_request,
    coordination_ack=coordination_ack,
)

gate_pass = all([compatible, aligned, refresh_ok, fresh])
streak = (prior_streak + 1) if gate_pass else 0
caught_up = gate_pass and streak >= consecutive_target

confidence = 'high' if gate_pass else ('medium' if compatible else 'low')
if publisher_warning.get('active'):
    confidence = 'medium' if confidence == 'high' else confidence

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
        'artifacts_present': shared_artifacts_present,
        'evidence_ok': refresh_evidence_ok,
        'expected_objective_active': shared_objective,
        'expected_schema_version': shared_schema,
        'expected_release_tag': shared_release,
        'checks': refresh_evidence_checks if shared_artifacts_present else {},
    },
    'warnings': [publisher_warning] if publisher_warning.get('active') else [],
    'publisher_warning': publisher_warning,
}

gate_signal = {
    'generated_at': status['generated_at'],
    'type': 'tod_catchup_gate_signal_v1',
    'objective': objective_value,
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
        'refresh_evidence_ok': refresh_evidence_ok,
        'fresh': fresh,
        'alignment_status': alignment_status,
        'tod_current_objective': tod_obj,
        'mim_objective_active': mim_obj,
        'refresh_failure_reason': refresh_failure_reason,
        'expected_objective_active': shared_objective,
        'expected_schema_version': shared_schema,
        'expected_release_tag': shared_release,
        'refresh_checks': refresh_evidence_checks if shared_artifacts_present else {},
        'publisher_warning_active': bool(publisher_warning.get('active')),
        'publisher_warning_code': str(publisher_warning.get('code') or ''),
        'publisher_warning_message': str(publisher_warning.get('message') or ''),
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
    f"- refresh_evidence_ok: {refresh_evidence_ok}",
    f"- freshness_age_seconds: {age_seconds}",
    f"- freshness_max_age_seconds: {max_age_seconds}",
    '',
]
if shared_artifacts_present:
    md_lines.extend([
        f"- expected_objective_active: {shared_objective}",
        f"- expected_schema_version: {shared_schema}",
        f"- expected_release_tag: {shared_release}",
        '- refresh_checks:',
    ])
    for key, value in refresh_evidence_checks.items():
        md_lines.append(f"  - {key}: {value}")
if publisher_warning.get('active'):
    md_lines.extend([
        '',
        'Publisher warning:',
        f"- code: {publisher_warning.get('code')}",
        f"- message: {publisher_warning.get('message')}",
        f"- canonical_objective_active: {publisher_warning.get('canonical_objective_active')}",
        f"- live_task_objective: {publisher_warning.get('live_task_objective')}",
        f"- live_task_id: {publisher_warning.get('live_task_id')}",
        f"- live_source_service: {publisher_warning.get('live_source_service') or '(unknown)'}",
        f"- hint: {publisher_warning.get('hint')}",
    ])
md_lines.extend([
    '',
    'Evidence source:',
    f"- {integration_path}",
])
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
    run_auto_repairs

    if [[ -f "${INTEGRATION_STATUS_FILE}" && "${LEGACY_INTEGRATION_STATUS_FILE}" != "${INTEGRATION_STATUS_FILE}" ]]; then
        cp -f "${INTEGRATION_STATUS_FILE}" "${LEGACY_INTEGRATION_STATUS_FILE}" || true
    fi

  out="$(emit_status)"
    if should_run_flag "${AUTO_REBUILD_INTEGRATION_STATUS}" || should_run_flag "${AUTO_RECONCILE_TASK_RESULT}"; then
        out="$(emit_status)"
    fi
  PASS_STREAK="$(echo "${out}" | sed -n '1p')"
  caught_up_flag="$(echo "${out}" | sed -n '2p')"
  stamp="$(echo "${out}" | sed -n '3p')"

printf 'PASS_STREAK=%s\n' "${PASS_STREAK}" > "${STATE_FILE}"
  if [[ "${caught_up_flag}" == "1" ]]; then
    echo "[tod-catchup] ${stamp} caught_up=true streak=${PASS_STREAK}/${CONSECUTIVE_PASS_TARGET}"
  else
    echo "[tod-catchup] ${stamp} caught_up=false streak=${PASS_STREAK}/${CONSECUTIVE_PASS_TARGET}"
  fi

    if [[ "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "1" || "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "true" || "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "yes" ]]; then
        break
    fi

  sleep "${POLL_SECONDS}"
done
