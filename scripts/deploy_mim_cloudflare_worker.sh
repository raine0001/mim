#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${MIM_ENV_FILE:-$ROOT_DIR/.env}"

if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    line="${raw_line%$'\r'}"
    if [[ -z "$line" || "$line" == \#* || "$line" != *=* ]]; then
      continue
    fi
    key="${line%%=*}"
    value="${line#*=}"
    if [[ -n "$key" ]]; then
      export "$key=$value"
    fi
  done < "$ENV_FILE"
fi

HOSTNAME="${MIM_REMOTE_SHELL_HOSTNAME:-}"
EXPLICIT_ORIGIN="${MIM_REMOTE_SHELL_ORIGIN:-}"
ORIGIN=""

if [[ -n "$HOSTNAME" ]]; then
  ORIGIN="https://${HOSTNAME}"
elif [[ -n "$EXPLICIT_ORIGIN" ]]; then
  ORIGIN="$EXPLICIT_ORIGIN"
fi

if [[ -z "$ORIGIN" ]]; then
  echo "[mim-worker] missing remote shell origin. Set MIM_REMOTE_SHELL_HOSTNAME or MIM_REMOTE_SHELL_ORIGIN." >&2
  exit 2
fi

WRANGLER_COMMAND="wrangler deploy --var MIM_REMOTE_SHELL_ORIGIN:${ORIGIN}"
if [[ -n "${MIM_REMOTE_SHELL_PATH_PREFIX:-}" ]]; then
  WRANGLER_COMMAND+=" --var MIM_REMOTE_SHELL_PATH_PREFIX:${MIM_REMOTE_SHELL_PATH_PREFIX}"
fi

cd "$ROOT_DIR"

if command -v node >/dev/null 2>&1; then
  NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
else
  NODE_MAJOR=0
fi

if [[ "$NODE_MAJOR" =~ ^[0-9]+$ ]] && (( NODE_MAJOR >= 20 )); then
  /usr/bin/env bash -lc "$WRANGLER_COMMAND"
else
  npx -y -p node@20 -p wrangler sh -lc "$WRANGLER_COMMAND"
fi

cat <<EOF
[mim-worker] deployed_origin=$ORIGIN
[mim-worker] verify_url=https://mim.dave-477.workers.dev/healthz
EOF