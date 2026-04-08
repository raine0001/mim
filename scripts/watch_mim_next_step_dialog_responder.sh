#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
DIALOG_ROOT="${DIALOG_ROOT:-${SHARED_DIR}/dialog}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"
STATUS_FILE="${STATUS_FILE:-${LOG_DIR}/mim_next_step_dialog_responder.latest.json}"
EVENT_LOG_FILE="${EVENT_LOG_FILE:-${LOG_DIR}/mim_next_step_dialog_responder.jsonl}"
DIALOG_GLOB="${DIALOG_GLOB:-MIM_TOD_DIALOG.session-*.jsonl}"
POLL_SECONDS="${POLL_SECONDS:-3}"
RUN_ONCE="${RUN_ONCE:-0}"
LOCK_FILE="${LOCK_FILE:-${LOG_DIR}/mim_next_step_dialog_responder.lock}"

mkdir -p "${SHARED_DIR}"
mkdir -p "${DIALOG_ROOT}"
mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG_FILE}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[mim-next-step-dialog-responder] another instance is already active; exiting"
    exit 0
fi

run_cycle() {
  python3 - <<'PY' "${ROOT_DIR}" "${SHARED_DIR}" "${DIALOG_ROOT}" "${STATUS_FILE}" "${EVENT_LOG_FILE}" "${DIALOG_GLOB}"
import json
import sys
from pathlib import Path

root_dir = Path(sys.argv[1])
shared_dir = Path(sys.argv[2])
dialog_root = Path(sys.argv[3])
status_path = Path(sys.argv[4])
event_log_path = Path(sys.argv[5])
dialog_glob = str(sys.argv[6])

if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from core.next_step_dialog_service import process_pending_dialog_sessions

result = process_pending_dialog_sessions(shared_root=shared_dir, dialog_root=dialog_root, pattern=dialog_glob)
status = {
    **result,
    "type": "mim_next_step_dialog_responder_status_v1",
    "shared_root": str(shared_dir),
  "dialog_root": str(dialog_root),
    "dialog_glob": dialog_glob,
    "state": "response_appended" if int(result.get("processed_count", 0) or 0) > 0 else "idle",
}
status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
with event_log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(status, separators=(",", ":")) + "\n")

print(f"{status['state']}|{int(result.get('processed_count', 0) or 0)}")
PY
}

echo "[mim-next-step-dialog-responder] watching ${DIALOG_ROOT}/${DIALOG_GLOB} every ${POLL_SECONDS}s"

while true; do
  cycle_out="$(run_cycle)"
  cycle_state="$(echo "${cycle_out}" | cut -d'|' -f1)"
  cycle_count="$(echo "${cycle_out}" | cut -d'|' -f2)"
  echo "[mim-next-step-dialog-responder] state=${cycle_state} processed_count=${cycle_count}"

  if [[ "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "1" || "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "true" || "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "yes" ]]; then
    break
  fi

  sleep "${POLL_SECONDS}"
done