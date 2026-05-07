#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="${STATE_FILE:-${ROOT_DIR}/runtime/logs/objective75_overnight_state.env}"
LOG_FILE="${LOG_FILE:-${ROOT_DIR}/runtime/logs/objective75_overnight.log}"
SUMMARY_LATEST_FILE="${SUMMARY_LATEST_FILE:-${ROOT_DIR}/runtime/logs/objective75_milestone_summary.latest.json}"
SUMMARY_EVENT_LOG="${SUMMARY_EVENT_LOG:-${ROOT_DIR}/runtime/logs/objective75_milestone_summaries.jsonl}"
LAST_REPORTED_FILE="${LAST_REPORTED_FILE:-${ROOT_DIR}/runtime/logs/objective75_last_reported_milestone.env}"
POLL_SECONDS="${POLL_SECONDS:-20}"
LIFECYCLE_GENERATOR="${LIFECYCLE_GENERATOR:-${ROOT_DIR}/scripts/generate_objective75_lifecycle_status.py}"

mkdir -p "$(dirname "${SUMMARY_LATEST_FILE}")"
mkdir -p "$(dirname "${SUMMARY_EVENT_LOG}")"
touch "${SUMMARY_EVENT_LOG}"

read_task_num() {
  [[ -f "${STATE_FILE}" ]] || { echo 0; return 0; }
  awk -F= '/^TASK_NUM=/{print $2}' "${STATE_FILE}" 2>/dev/null | tr -dc '0-9' || echo 0
}

read_last_reported() {
  if [[ -f "${LAST_REPORTED_FILE}" ]]; then
    awk -F= '/^LAST_REPORTED_MILESTONE=/{print $2}' "${LAST_REPORTED_FILE}" 2>/dev/null | tr -dc '0-9' || echo 0
  else
    echo 0
  fi
}

initialize_last_reported() {
  local task_num="$1"
  local floor_milestone=$(( (task_num / 10) * 10 ))
  printf 'LAST_REPORTED_MILESTONE=%s\n' "${floor_milestone}" > "${LAST_REPORTED_FILE}"
  echo "[objective75-milestone] initialized last milestone=${floor_milestone} (task_num=${task_num})"
}

emit_summary() {
  local milestone="$1"
  local task_num="$2"
  local now_iso
  local pass_count
  local fail_count
  local last_pass

  now_iso="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  pass_count="$(grep -c 'Cycle PASS; next TASK_NUM=' "${LOG_FILE}" 2>/dev/null || echo 0)"
  fail_count="$(grep -c 'Cycle FAIL;' "${LOG_FILE}" 2>/dev/null || echo 0)"
  last_pass="$(grep 'Cycle PASS; next TASK_NUM=' "${LOG_FILE}" 2>/dev/null | tail -n 1 | sed 's/"//g' || true)"

  python3 - <<'PY' "${ROOT_DIR}" "${SUMMARY_LATEST_FILE}" "${SUMMARY_EVENT_LOG}" "${now_iso}" "${milestone}" "${task_num}" "${pass_count}" "${fail_count}" "${last_pass}"
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
latest_path = Path(sys.argv[2])
event_log_path = Path(sys.argv[3])
generated_at = sys.argv[4]
milestone = int(sys.argv[5])
task_num = int(sys.argv[6])
pass_count = int(sys.argv[7])
fail_count = int(sys.argv[8])
last_pass = sys.argv[9]

def read_json(path: Path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except Exception:
        return {}

ack = read_json(root / 'runtime/shared/TOD_MIM_TASK_ACK.latest.json')
result = read_json(root / 'runtime/shared/TOD_MIM_TASK_RESULT.latest.json')
request = read_json(root / 'runtime/shared/MIM_TOD_TASK_REQUEST.latest.json')
review = read_json(root / 'runtime/shared/MIM_TOD_REVIEW_DECISION.latest.json')

summary = {
    'generated_at': generated_at,
    'type': 'objective75_milestone_summary',
    'milestone': milestone,
    'task_num': task_num,
    'counts': {
        'cycle_pass_total': pass_count,
        'cycle_fail_total': fail_count,
    },
    'latest_cycle_pass_line': last_pass,
    'latest_packets': {
        'request': {
            'task_id': request.get('task_id'),
            'generated_at': request.get('generated_at'),
        },
        'ack': {
            'request_id': ack.get('request_id'),
            'status': ack.get('status'),
            'generated_at': ack.get('generated_at'),
        },
        'result': {
            'request_id': result.get('request_id'),
            'status': result.get('status'),
            'generated_at': result.get('generated_at'),
            'output_preview': result.get('output_preview'),
        },
        'review': {
            'task_id': review.get('task_id'),
            'decision': review.get('decision'),
            'generated_at': review.get('generated_at'),
        },
    },
}

latest_path.write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
with event_log_path.open('a', encoding='utf-8') as f:
    f.write(json.dumps(summary, separators=(',', ':')) + '\n')
PY

  printf 'LAST_REPORTED_MILESTONE=%s\n' "${milestone}" > "${LAST_REPORTED_FILE}"
  echo "[objective75-milestone] milestone=${milestone} task_num=${task_num} pass_total=${pass_count} fail_total=${fail_count}"

  if [[ -f "${LIFECYCLE_GENERATOR}" ]]; then
    if python3 "${LIFECYCLE_GENERATOR}" >/dev/null 2>&1; then
      echo "[objective75-milestone] lifecycle status refreshed"
    else
      echo "[objective75-milestone] WARN lifecycle status refresh failed"
    fi
  else
    echo "[objective75-milestone] WARN lifecycle generator missing: ${LIFECYCLE_GENERATOR}"
  fi
}

initial_task_num="$(read_task_num)"
if [[ ! -f "${LAST_REPORTED_FILE}" ]]; then
  initialize_last_reported "${initial_task_num:-0}"
fi

echo "[objective75-milestone] watching ${STATE_FILE} every ${POLL_SECONDS}s"

while true; do
  task_num="$(read_task_num)"
  [[ -n "${task_num}" ]] || task_num=0

  if [[ "${task_num}" =~ ^[0-9]+$ ]] && (( task_num >= 10 )); then
    last_reported="$(read_last_reported)"
    [[ -n "${last_reported}" ]] || last_reported=0

    current_milestone=$(( (task_num / 10) * 10 ))
    if (( current_milestone > last_reported )) && (( task_num % 10 == 0 )); then
      emit_summary "${current_milestone}" "${task_num}"
    fi
  fi

  sleep "${POLL_SECONDS}"
done
