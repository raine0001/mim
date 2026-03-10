#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/env/.env.prod"
DEPLOY_LOG="$ROOT_DIR/runtime/prod/deployments.log"
TARGET_SHA="${1:-}"

if [[ ! -f "$DEPLOY_LOG" ]]; then
  echo "No deployment log found: $DEPLOY_LOG"
  exit 1
fi

if [[ -z "$TARGET_SHA" ]]; then
  TARGET_SHA="$(tail -n 2 "$DEPLOY_LOG" | head -n 1 | sed -n 's/.*git_sha=\([a-f0-9]\{40\}\).*/\1/p')"
fi

if [[ -z "$TARGET_SHA" ]]; then
  echo "Could not determine rollback SHA (provide it as first argument)."
  exit 1
fi

if ! git -C "$ROOT_DIR" cat-file -e "$TARGET_SHA^{commit}"; then
  echo "Target SHA does not exist locally: $TARGET_SHA"
  exit 1
fi

current_ref="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD || true)"
current_sha="$(git -C "$ROOT_DIR" rev-parse HEAD || true)"
if [[ -n "$(git -C "$ROOT_DIR" status --porcelain)" ]]; then
  echo "Working tree is not clean. Commit/stash changes before rollback."
  exit 1
fi

sed -i "s|^BUILD_GIT_SHA=.*|BUILD_GIT_SHA=$TARGET_SHA|" "$ENV_FILE"
sed -i "s|^BUILD_TIMESTAMP=.*|BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)|" "$ENV_FILE"
sed -i "s|^RELEASE_TAG=.*|RELEASE_TAG=rollback-${TARGET_SHA:0:12}|" "$ENV_FILE"

git -C "$ROOT_DIR" checkout "$TARGET_SHA"
sudo docker compose -f "$ROOT_DIR/docker/prod/compose.yaml" --env-file "$ENV_FILE" up -d --build
"$ROOT_DIR/scripts/smoke_test.sh" prod

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) release=rollback-${TARGET_SHA:0:12} git_sha=$TARGET_SHA" >> "$DEPLOY_LOG"

echo "Rollback successful to $TARGET_SHA"

git -C "$ROOT_DIR" checkout "$current_ref" >/dev/null 2>&1 || git -C "$ROOT_DIR" checkout "$current_sha" >/dev/null 2>&1 || true
