#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$ROOT_DIR/deploy/systemd-user"
USER_UNIT_DIR="$HOME/.config/systemd/user"

UNITS=(
  mim-handoff-watcher.service
  mim-handoff-watcher-supervisor.service
)

mkdir -p "$USER_UNIT_DIR"

for script in \
  "$ROOT_DIR/scripts/watch_handoff_inbox.py" \
  "$ROOT_DIR/scripts/check_handoff_watcher_status.py" \
  "$ROOT_DIR/scripts/print_handoff_watcher_recovery.py" \
  "$ROOT_DIR/scripts/watch_handoff_watcher_supervisor.py"; do
  chmod +x "$script"
done

echo "Installing handoff watcher user systemd units..."
for unit in "${UNITS[@]}"; do
  cp "$SRC_DIR/$unit" "$USER_UNIT_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable "${UNITS[@]}"

echo "Stopping manually launched handoff watcher processes (if any)..."
pkill -f '/home/testpilot/mim/scripts/watch_handoff_inbox.py' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_handoff_watcher_supervisor.py' 2>/dev/null || true

echo "Starting handoff watcher user units..."
systemctl --user restart "${UNITS[@]}"

echo "Handoff watcher user units state:"
systemctl --user --no-pager --full status "${UNITS[@]}" | sed -n '1,160p' || true