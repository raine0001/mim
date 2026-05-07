#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/env/.env.prod"
DEPLOY_LOG="$ROOT_DIR/runtime/prod/deployments.log"

ensure_clean_promotion_source() {
  local pending
  pending="$(git -C "$ROOT_DIR" status --porcelain --untracked-files=normal -- \
    . \
    ':(exclude)runtime/**' \
    ':(exclude)logs/**' \
    ':(exclude)__pycache__/**' \
    ':(exclude).pytest_cache/**' \
    ':(exclude).mypy_cache/**')"
  if [[ -n "$pending" ]]; then
    echo "Promotion aborted: source workspace has uncommitted changes outside runtime/log noise."
    echo "$pending"
    echo "Commit or discard the pending source changes before promoting."
    exit 1
  fi
}

set_kv() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

GIT_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"
SHORT_SHA="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"
BUILD_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RELEASE_TAG="${1:-sha-${SHORT_SHA}}"

ensure_clean_promotion_source

echo "[1/3] Run test smoke checks"
"$ROOT_DIR/scripts/smoke_test.sh" test

echo "[2/3] Backup production"
"$ROOT_DIR/scripts/backup_prod.sh"

echo "[3/4] Stamp runtime metadata"
set_kv BUILD_GIT_SHA "$GIT_SHA"
set_kv BUILD_TIMESTAMP "$BUILD_TS"
set_kv RELEASE_TAG "$RELEASE_TAG"

echo "[4/4] Rebuild/restart production stack"
sudo docker compose \
  -f "$ROOT_DIR/docker/prod/compose.yaml" \
  --env-file "$ENV_FILE" \
  up -d --build

echo "[5/5] Refresh shared MIM context export"
"$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/export_mim_context.py" --prefer-prod-runtime

mkdir -p "$(dirname "$DEPLOY_LOG")"
echo "${BUILD_TS} release=${RELEASE_TAG} git_sha=${GIT_SHA}" >> "$DEPLOY_LOG"

echo "Promotion complete. Run prod smoke test to confirm."
