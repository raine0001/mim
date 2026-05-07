#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"
POLL_SECONDS="${POLL_SECONDS:-30}"
MAX_EXPORT_AGE_SECONDS="${MAX_EXPORT_AGE_SECONDS:-300}"
MIN_RUN_INTERVAL_SECONDS="${MIN_RUN_INTERVAL_SECONDS:-60}"
RUN_ONCE="${RUN_ONCE:-0}"
SERVICE_NAME="${SERVICE_NAME:-mim-watch-mim-context-export}"
LOCK_FILE="${LOCK_FILE:-${SHARED_DIR}/.watch_mim_context_export.lock}"
STATE_FILE="${STATE_FILE:-${LOG_DIR}/mim_context_export_watch.latest.json}"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"

mkdir -p "${SHARED_DIR}" "${LOG_DIR}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[mim-context-export] another instance is already active; exiting"
  exit 0
fi

last_run_epoch=0
last_trigger_reason="startup"

load_state() {
  if [[ ! -f "${STATE_FILE}" ]]; then
    return
  fi
  local loaded
  loaded="$(${PYTHON_BIN} - <<'PY' "${STATE_FILE}"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("0|startup")
    raise SystemExit(0)

print(f"{int(payload.get('last_run_epoch', 0) or 0)}|{str(payload.get('last_trigger_reason') or 'startup').strip() or 'startup'}")
PY
)"
  last_run_epoch="${loaded%%|*}"
  last_trigger_reason="${loaded#*|}"
}

save_state() {
  local now_epoch="$1"
  local export_epoch="$2"
  local trigger_reason="$3"
  local run_status="$4"
  cat > "${STATE_FILE}" <<EOF
{
  "generated_at": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "type": "mim_context_export_watch_status_v1",
  "service": "${SERVICE_NAME}",
  "last_run_epoch": ${now_epoch},
  "last_export_epoch": ${export_epoch},
  "last_trigger_reason": "${trigger_reason}",
  "last_run_status": "${run_status}",
  "poll_seconds": ${POLL_SECONDS},
  "max_export_age_seconds": ${MAX_EXPORT_AGE_SECONDS},
  "min_run_interval_seconds": ${MIN_RUN_INTERVAL_SECONDS}
}
EOF
}

mtime_or_zero() {
  local file_path="$1"
  if [[ -f "${file_path}" ]]; then
    stat -c %Y "${file_path}" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

latest_objective_doc_mtime() {
  find "${ROOT_DIR}/docs" -maxdepth 1 -type f -name 'objective-*.md' -printf '%T@\n' 2>/dev/null | awk 'BEGIN{max=0} { if ($1 > max) max=$1 } END { printf "%d\n", max }'
}

trigger_reason() {
  local export_file="${SHARED_DIR}/MIM_CONTEXT_EXPORT.latest.json"
  local handshake_file="${SHARED_DIR}/MIM_TOD_HANDSHAKE_PACKET.latest.json"
  local manifest_file="${SHARED_DIR}/MIM_MANIFEST.latest.json"
  local task_request_file="${SHARED_DIR}/MIM_TOD_TASK_REQUEST.latest.json"
  local task_review_file="${SHARED_DIR}/MIM_TASK_STATUS_REVIEW.latest.json"
  local export_epoch
  export_epoch="$(mtime_or_zero "${export_file}")"
  local handshake_epoch
  handshake_epoch="$(mtime_or_zero "${handshake_file}")"
  local manifest_epoch
  manifest_epoch="$(mtime_or_zero "${manifest_file}")"
  local task_request_epoch
  task_request_epoch="$(mtime_or_zero "${task_request_file}")"
  local task_review_epoch
  task_review_epoch="$(mtime_or_zero "${task_review_file}")"
  local export_script_epoch
  export_script_epoch="$(mtime_or_zero "${ROOT_DIR}/scripts/export_mim_context.py")"
  local rebuild_script_epoch
  rebuild_script_epoch="$(mtime_or_zero "${ROOT_DIR}/scripts/rebuild_tod_integration_status.py")"
  local objective_doc_epoch
  objective_doc_epoch="$(latest_objective_doc_mtime)"
  local now_epoch
  now_epoch="$(date +%s)"

  if [[ "${export_epoch}" -eq 0 || "${handshake_epoch}" -eq 0 || "${manifest_epoch}" -eq 0 ]]; then
    echo "missing_export_artifact"
    return 0
  fi
  if (( now_epoch - export_epoch > MAX_EXPORT_AGE_SECONDS )); then
    echo "export_stale"
    return 0
  fi
  if (( task_request_epoch > export_epoch )); then
    echo "task_request_newer_than_export"
    return 0
  fi
  if (( task_review_epoch > export_epoch )); then
    echo "task_review_newer_than_export"
    return 0
  fi
  if (( export_script_epoch > export_epoch || rebuild_script_epoch > export_epoch )); then
    echo "export_script_newer_than_export"
    return 0
  fi
  if (( objective_doc_epoch > export_epoch )); then
    echo "objective_docs_newer_than_export"
    return 0
  fi
  echo ""
}

run_refresh() {
  local reason="$1"
  local now_epoch
  now_epoch="$(date +%s)"
  if (( last_run_epoch > 0 && now_epoch - last_run_epoch < MIN_RUN_INTERVAL_SECONDS )); then
    return 0
  fi

  if "${PYTHON_BIN}" "${ROOT_DIR}/scripts/export_mim_context.py" >/dev/null && \
     "${PYTHON_BIN}" "${ROOT_DIR}/scripts/rebuild_tod_integration_status.py" --shared-dir "${SHARED_DIR}" --mirror-legacy-alias >/dev/null; then
    last_run_epoch="${now_epoch}"
    last_trigger_reason="${reason}"
    save_state "${now_epoch}" "$(mtime_or_zero "${SHARED_DIR}/MIM_CONTEXT_EXPORT.latest.json")" "${reason}" "success"
    echo "[mim-context-export] refreshed (${reason})"
  else
    save_state "${now_epoch}" "$(mtime_or_zero "${SHARED_DIR}/MIM_CONTEXT_EXPORT.latest.json")" "${reason}" "failed"
    echo "[mim-context-export] WARN refresh failed (${reason})" >&2
  fi
}

load_state

while true; do
  reason="$(trigger_reason)"
  if [[ -n "${reason}" ]]; then
    run_refresh "${reason}"
  fi

  if [[ "${RUN_ONCE}" == "1" ]]; then
    exit 0
  fi
  sleep "${POLL_SECONDS}"
done