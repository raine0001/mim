#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_SCRIPT="$ROOT_DIR/scripts/tod_status_dashboard.sh"

if [[ ! -f "$DASHBOARD_SCRIPT" ]]; then
  echo "Dashboard script not found: $DASHBOARD_SCRIPT" >&2
  exit 1
fi

if [[ "${1:-}" == "--watch" ]]; then
  INTERVAL="${2:-2}"
  watch -n "$INTERVAL" "bash '$DASHBOARD_SCRIPT'"
else
  bash "$DASHBOARD_SCRIPT"
fi
