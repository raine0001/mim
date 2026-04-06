#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$ROOT_DIR/deploy/systemd"

UNITS=(
  mim-watch-shared-triggers.service
  mim-watch-tod-liveness.service
  mim-watch-tod-catchup-status.service
  mim-watch-tod-task-status-review.service
  mim-watch-objective75-cycle-pass.service
  mim-watch-objective75-stale-ack-watchdog.service
  mim-objective75-nightly-summary.service
  mim-objective75-nightly-summary.timer
  mim-objective75-jsonl-retention.service
  mim-objective75-jsonl-retention.timer
)

for script in \
  "$ROOT_DIR/scripts/watch_shared_triggers.sh" \
  "$ROOT_DIR/scripts/watch_tod_liveness.sh" \
  "$ROOT_DIR/scripts/watch_tod_catchup_status.sh" \
  "$ROOT_DIR/scripts/watch_tod_task_status_review.sh" \
  "$ROOT_DIR/scripts/watch_objective75_cycle_pass.sh" \
  "$ROOT_DIR/scripts/watch_objective75_stale_ack_watchdog.sh" \
  "$ROOT_DIR/scripts/generate_objective75_nightly_summary.sh" \
  "$ROOT_DIR/scripts/prune_objective75_jsonl_retention.sh"; do
  chmod +x "$script"
done

echo "Installing Objective 75 systemd units..."
for unit in "${UNITS[@]}"; do
  sudo cp "$UNIT_DIR/$unit" "/etc/systemd/system/$unit"
done

if [[ -f "/etc/systemd/system/mim-objective75-overnight-loop.service" ]]; then
  echo "Retiring mim-objective75-overnight-loop.service from systemd startup..."
  sudo systemctl disable --now mim-objective75-overnight-loop.service 2>/dev/null || true
  sudo rm -f /etc/systemd/system/mim-objective75-overnight-loop.service
fi

sudo systemctl daemon-reload
sudo systemctl enable "${UNITS[@]}"

echo "Stopping manually launched duplicates (if any)..."
pkill -f '/home/testpilot/mim/scripts/run_objective75_overnight_loop.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_shared_triggers.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_tod_liveness.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_tod_catchup_status.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_tod_task_status_review.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_objective75_cycle_pass.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_objective75_stale_ack_watchdog.sh' 2>/dev/null || true

echo "Starting Objective 75 units..."
sudo systemctl restart "${UNITS[@]}"

echo "Disabling overlapping user-level Objective 75 units (best effort)..."
systemctl --user disable --now "${UNITS[@]}" 2>/dev/null || true

echo "Objective 75 units active status:"
sudo systemctl --no-pager --full status "${UNITS[@]}" | sed -n '1,120p' || true
