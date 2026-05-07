#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${MIM_BIND_HOST:-0.0.0.0}"
PORT="${MIM_PORT:-18001}"
PYTHON_BIN="${MIM_PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "[mim-mobile-web] python not found. Configure MIM_PYTHON_BIN or install python3." >&2
  exit 127
fi

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
PUBLIC_UI_BASE="${MIM_REMOTE_SHELL_DOMAIN:-}"
PUBLIC_UI_BASE="${PUBLIC_UI_BASE%/}"

echo "[mim-mobile-web] Starting current-source MIM on ${HOST}:${PORT}"
if [[ -n "${LAN_IP}" ]]; then
  echo "[mim-mobile-web] Phone URL: http://${LAN_IP}:${PORT}/mim"
else
  echo "[mim-mobile-web] Phone URL: http://<host-ip>:${PORT}/mim"
fi
if [[ -n "${PUBLIC_UI_BASE}" ]]; then
  echo "[mim-mobile-web] Secure URL: ${PUBLIC_UI_BASE}/mim"
fi

exec "${PYTHON_BIN}" -m uvicorn core.app:app --lifespan off --host "${HOST}" --port "${PORT}"