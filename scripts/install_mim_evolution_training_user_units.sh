#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$ROOT_DIR/deploy/systemd-user"
USER_UNIT_DIR="$HOME/.config/systemd/user"

UNITS=(
  mim-training-web.service
  mim-evolution-training.service
  mim-evolution-training-watchdog.service
)

mkdir -p "$USER_UNIT_DIR"

chmod +x \
  "$ROOT_DIR/scripts/mim_evolution_continuous_runner.py" \
  "$ROOT_DIR/scripts/watch_mim_evolution_training_supervisor.py"

echo "Installing MIM evolution-training user systemd units..."
for unit in "${UNITS[@]}"; do
  cp "$SRC_DIR/$unit" "$USER_UNIT_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable "${UNITS[@]}"

echo "Stopping manually launched MIM evolution-training processes (if any)..."
pkill -f '/home/testpilot/mim/scripts/mim_evolution_continuous_runner.py' 2>/dev/null || true
pkill -f '/home/testpilot/mim/scripts/watch_mim_evolution_training_supervisor.py' 2>/dev/null || true

echo "Restarting MIM evolution-training units..."
systemctl --user restart "${UNITS[@]}"

echo "MIM evolution-training unit state:"
systemctl --user --no-pager --full status "${UNITS[@]}" | sed -n '1,220p' || true

echo "If you want these to survive logout/reboot, run once:"
echo "  sudo loginctl enable-linger $USER"