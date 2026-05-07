#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:${PATH}"

CLOUDFLARED_BIN="${MIM_CLOUDFLARED_BIN:-}"
if [[ -z "$CLOUDFLARED_BIN" ]]; then
  if [[ -x "$HOME/.local/bin/cloudflared" ]]; then
    CLOUDFLARED_BIN="$HOME/.local/bin/cloudflared"
  else
    CLOUDFLARED_BIN="$(command -v cloudflared || true)"
  fi
fi

if [[ -z "$CLOUDFLARED_BIN" || ! -x "$CLOUDFLARED_BIN" ]]; then
  echo "[mim-cloudflared] cloudflared not found. Set MIM_CLOUDFLARED_BIN or install to ~/.local/bin/cloudflared." >&2
  exit 127
fi

CONFIG_PATH="${MIM_CLOUDFLARED_CONFIG:-${ROOT_DIR}/deploy/cloudflare/mim-shell-tunnel.yml}"
TUNNEL_NAME="${MIM_CLOUDFLARED_TUNNEL_NAME:-mim-travel-shell}"

if [[ "$#" -gt 0 ]]; then
  exec "$CLOUDFLARED_BIN" "$@"
fi

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "[mim-cloudflared] config not found at $CONFIG_PATH" >&2
  echo "[mim-cloudflared] Copy deploy/cloudflare/mim-shell-tunnel.example.yml to deploy/cloudflare/mim-shell-tunnel.yml and set your hostname first." >&2
  exit 2
fi

if grep -q 'mim.yourdomain.com' "$CONFIG_PATH"; then
  echo "[mim-cloudflared] config still contains the placeholder hostname mim.yourdomain.com" >&2
  exit 2
fi

TUNNEL_REF="$(python3 - "$CONFIG_PATH" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
for raw_line in path.read_text().splitlines():
    line = raw_line.strip()
    if line.startswith('tunnel:'):
        print(line.split(':', 1)[1].strip())
        break
PY
)"

if [[ -z "$TUNNEL_REF" ]]; then
  echo "[mim-cloudflared] config is missing a tunnel: entry" >&2
  exit 2
fi

if [[ "$TUNNEL_REF" == "YOUR_TUNNEL_UUID" ]]; then
  echo "[mim-cloudflared] config still contains the placeholder tunnel UUID" >&2
  exit 2
fi

CREDENTIALS_PATH="$(python3 - "$CONFIG_PATH" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
for raw_line in path.read_text().splitlines():
    line = raw_line.strip()
    if line.startswith('credentials-file:'):
        print(line.split(':', 1)[1].strip())
        break
PY
)"

if [[ -z "$CREDENTIALS_PATH" ]]; then
  echo "[mim-cloudflared] config is missing a credentials-file entry" >&2
  exit 2
fi

if [[ ! -f "$CREDENTIALS_PATH" ]]; then
  echo "[mim-cloudflared] credentials file not found at $CREDENTIALS_PATH" >&2
  echo "[mim-cloudflared] Run scripts/provision_mim_cloudflare_tunnel.sh to (re)hydrate the named tunnel credentials." >&2
  exit 2
fi

echo "[mim-cloudflared] Starting tunnel $TUNNEL_NAME with $CONFIG_PATH"
exec "$CLOUDFLARED_BIN" tunnel --config "$CONFIG_PATH" run "$TUNNEL_NAME"