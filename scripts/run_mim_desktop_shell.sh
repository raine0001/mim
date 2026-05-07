#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${ROOT_DIR}/desktop/mim-shell"
UI_URL="${MIM_UI_URL:-http://127.0.0.1:18001/mim}"
USER_DATA_DIR="${MIM_ELECTRON_USER_DATA_DIR:-${ROOT_DIR}/runtime/mim-shell-profile}"
NPM_BIN="${NPM_BIN:-/usr/bin/npm}"

if [[ ! -x "${NPM_BIN}" ]]; then
  NPM_BIN="$(command -v npm || true)"
fi

if [[ -z "${NPM_BIN}" ]]; then
  echo "[mim-desktop-shell] npm not found. Install Node.js/npm first." >&2
  exit 127
fi

cd "${APP_DIR}"

if [[ ! -d node_modules ]]; then
  "${NPM_BIN}" install
fi

mkdir -p "${USER_DATA_DIR}"

START_SCRIPT="start"
if [[ "${MIM_ELECTRON_DISABLE_SANDBOX:-0}" == "1" ]]; then
  START_SCRIPT="start:service"
fi

MIM_UI_URL="${UI_URL}" MIM_ELECTRON_USER_DATA_DIR="${USER_DATA_DIR}" "${NPM_BIN}" run "${START_SCRIPT}"
