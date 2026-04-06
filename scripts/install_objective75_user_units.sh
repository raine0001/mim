#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$ROOT_DIR/deploy/systemd-user"
USER_UNIT_DIR="$HOME/.config/systemd/user"

UNITS=(
  mim-watch-shared-triggers.service
  mim-watch-tod-liveness.service
  mim-watch-tod-bridge-artifacts-remote.service
  mim-watch-tod-catchup-status.service
  mim-watch-tod-task-status-review.service
  mim-watch-tod-consume-evidence.service
  mim-watch-tod-consume-timeout-policy.service
  mim-watch-mim-context-export.service
  mim-watch-mim-coordination-responder.service
  mim-watch-objective75-cycle-pass.service
  mim-watch-objective75-stale-ack-watchdog.service
  mim-objective75-nightly-summary.service
  mim-objective75-nightly-summary.timer
  mim-objective75-jsonl-retention.service
  mim-objective75-jsonl-retention.timer
)

mkdir -p "$USER_UNIT_DIR"

for script in \
  "$ROOT_DIR/scripts/watch_shared_triggers.sh" \
  "$ROOT_DIR/scripts/watch_tod_liveness.sh" \
  "$ROOT_DIR/scripts/watch_tod_bridge_artifacts_remote.sh" \
  "$ROOT_DIR/scripts/watch_tod_catchup_status.sh" \
  "$ROOT_DIR/scripts/watch_tod_task_status_review.sh" \
  "$ROOT_DIR/scripts/watch_tod_consume_evidence.sh" \
  "$ROOT_DIR/scripts/watch_tod_consume_timeout_policy.sh" \
  "$ROOT_DIR/scripts/watch_mim_context_export.sh" \
  "$ROOT_DIR/scripts/watch_mim_coordination_responder.sh" \
  "$ROOT_DIR/scripts/watch_objective75_cycle_pass.sh" \
  "$ROOT_DIR/scripts/watch_objective75_stale_ack_watchdog.sh" \
  "$ROOT_DIR/scripts/generate_objective75_nightly_summary.sh" \
  "$ROOT_DIR/scripts/prune_objective75_jsonl_retention.sh"; do
  chmod +x "$script"
done

echo "Installing Objective 75 user systemd units..."
for unit in "${UNITS[@]}"; do
  cp "$SRC_DIR/$unit" "$USER_UNIT_DIR/$unit"
done

if [[ -f "$USER_UNIT_DIR/mim-objective75-overnight-loop.service" ]]; then
  echo "Retiring mim-objective75-overnight-loop.service from user systemd startup..."
  systemctl --user disable --now mim-objective75-overnight-loop.service 2>/dev/null || true
  rm -f "$USER_UNIT_DIR/mim-objective75-overnight-loop.service"
fi

systemctl --user daemon-reload
systemctl --user enable "${UNITS[@]}"

echo "Stopping manually launched duplicates (if any)..."
pkill -f '/home/testpilot/mim/scripts/run_objective75_overnight_loop.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_shared_triggers.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_tod_liveness.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_tod_bridge_artifacts_remote.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_tod_catchup_status.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_tod_task_status_review.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_tod_consume_evidence.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_tod_consume_timeout_policy.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_mim_context_export.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_mim_coordination_responder.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_objective75_cycle_pass.sh' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_objective75_stale_ack_watchdog.sh' 2>/dev/null || true

echo "Starting Objective 75 user units..."
systemctl --user restart "${UNITS[@]}"

echo "Objective 75 user units state:"
systemctl --user --no-pager --full status "${UNITS[@]}" | sed -n '1,160p' || true

echo "If you want these to survive logout/reboot, run once:"
echo "  sudo loginctl enable-linger $USER"
