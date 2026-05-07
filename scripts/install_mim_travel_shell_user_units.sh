#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$ROOT_DIR/deploy/systemd-user"
USER_UNIT_DIR="$HOME/.config/systemd/user"

UNITS=(
  mim-mobile-web.service
  mim-watch-mim-coordination-responder.service
  mim-watch-ui-health.service
)

if [[ -x "$HOME/.local/bin/cloudflared" && -f "$ROOT_DIR/deploy/cloudflare/mim-shell-tunnel.yml" ]]; then
  UNITS+=(mim-cloudflared-tunnel.service)
else
  echo "[mim-travel-shell] Skipping tunnel unit install until both of these exist:" >&2
  echo "[mim-travel-shell]   $HOME/.local/bin/cloudflared" >&2
  echo "[mim-travel-shell]   $ROOT_DIR/deploy/cloudflare/mim-shell-tunnel.yml" >&2
fi

mkdir -p "$USER_UNIT_DIR"

chmod +x \
  "$ROOT_DIR/scripts/run_mim_mobile_web.sh" \
  "$ROOT_DIR/scripts/watch_mim_coordination_responder.sh" \
  "$ROOT_DIR/scripts/watch_mim_ui_health.sh" \
  "$ROOT_DIR/scripts/run_cloudflared_tunnel.sh"

echo "Installing MIM travel-shell user systemd units..."
for unit in "${UNITS[@]}"; do
  cp "$SRC_DIR/$unit" "$USER_UNIT_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable "${UNITS[@]}"

echo "Stopping manually launched MIM mobile-web processes (if any)..."
pkill -f '/home/testpilot/mim/scripts/run_mim_mobile_web.sh' 2>/dev/null || true
pkill -f 'uvicorn core.app:app.*18001' 2>/dev/null || true

echo "Restarting MIM travel-shell units..."
systemctl --user restart "${UNITS[@]}"

echo "MIM travel-shell unit state:"
systemctl --user --no-pager --full status "${UNITS[@]}" | sed -n '1,220p' || true

echo "If you want these to survive logout/reboot, run once:"
echo "  sudo loginctl enable-linger $USER"