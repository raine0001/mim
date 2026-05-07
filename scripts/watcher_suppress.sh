#!/usr/bin/env bash
# Usage:
#   watcher_suppress.sh on   — create suppress lock (watcher will not restart service)
#   watcher_suppress.sh off  — remove suppress lock (watcher resumes normal operation)
#   watcher_suppress.sh status — print current state

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"
SUPPRESS_FILE="${LOG_DIR}/.watcher_suppress_restart"

cmd="${1:-status}"

case "${cmd}" in
  on)
    mkdir -p "${LOG_DIR}"
    touch "${SUPPRESS_FILE}"
    echo "[watcher_suppress] suppression ON — restart actions blocked"
    ;;
  off)
    rm -f "${SUPPRESS_FILE}"
    echo "[watcher_suppress] suppression OFF — watcher resuming normal operation"
    ;;
  status)
    if [[ -f "${SUPPRESS_FILE}" ]]; then
      echo "[watcher_suppress] status: ON (restart suppressed)"
    else
      echo "[watcher_suppress] status: OFF (normal operation)"
    fi
    ;;
  *)
    echo "Usage: $0 on|off|status" >&2
    exit 1
    ;;
esac
