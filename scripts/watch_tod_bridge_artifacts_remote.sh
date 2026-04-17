#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/env/.env}"
POLL_SECONDS="${POLL_SECONDS:-30}"
RUN_ONCE="${RUN_ONCE:-0}"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
SUMMARY_FILE="${SUMMARY_FILE:-${LOG_DIR}/tod_bridge_artifacts_remote_sync.latest.json}"
BACKUP_ROOT="${BACKUP_ROOT:-}"
ENV_TOOLS="${ENV_TOOLS:-${ROOT_DIR}/scripts/env_file_tools.py}"

mkdir -p "${SHARED_DIR}" "${LOG_DIR}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "[tod-bridge-sync] WARN no python interpreter available" >&2
  exit 0
fi

if [[ -f "${ENV_FILE}" ]]; then
  eval "$("${PYTHON_BIN}" "${ENV_TOOLS}" export --file "${ENV_FILE}" --keys MIM_TOD_SSH_HOST MIM_TOD_SSH_USER MIM_TOD_SSH_HOST_USER MIM_TOD_SSH_PORT MIM_TOD_SSH_PASS MIM_TOD_SSH_PASSWORD MIM_TOD_SSH_REMOTE_ROOT REMOTE_ROOT)"
fi

write_summary() {
  local status="$1"
  local detail="$2"
  local copied="$3"
  python3 - <<'PY' "${SUMMARY_FILE}" "${status}" "${detail}" "${copied}"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

summary_path = Path(sys.argv[1])
status = sys.argv[2]
detail = sys.argv[3]
copied = sys.argv[4].lower() == "true"
summary_path.write_text(
    json.dumps(
        {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "type": "tod_bridge_artifacts_remote_sync_v1",
            "status": status,
            "detail": detail,
            "copied": copied,
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
}

run_sync_once() {
  local args=()
  local summary_file
  summary_file="$(mktemp)"

  if [[ -n "${BACKUP_ROOT}" ]]; then
    local backup_dir
    backup_dir="${BACKUP_ROOT}/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "${backup_dir}"
    args+=(--backup-dir "${backup_dir}")
  fi

  if ! "${PYTHON_BIN}" "${ROOT_DIR}/scripts/pull_tod_bridge_artifacts_remote.py" \
      --local-shared-dir "${SHARED_DIR}" \
      "${args[@]}" >"${summary_file}" 2>&1; then
    local output
    output="$(cat "${summary_file}")"
    write_summary "error" "${output}" false
    echo "[tod-bridge-sync] WARN pull failed: ${output}" >&2
    rm -f "${summary_file}"
    return 0
  fi

  local copied
  copied="$(python3 - <<'PY' "${summary_file}"
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print("true" if payload.get("copied") else "false")
PY
)"
  mv "${summary_file}" "${SUMMARY_FILE}"
  echo "[tod-bridge-sync] completed copied=${copied}"
}

while true; do
  run_sync_once
  if [[ "${RUN_ONCE}" == "1" ]]; then
    break
  fi
  sleep "${POLL_SECONDS}"
done