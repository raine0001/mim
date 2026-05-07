#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-test}"

if [[ "$TARGET" == "prod" ]]; then
  BASE_URL="http://127.0.0.1:8000"
else
  BASE_URL="http://127.0.0.1:18001"
fi

echo "Running smoke tests against $TARGET ($BASE_URL)"
curl -fsS "$BASE_URL/health" >/dev/null
curl -fsS "$BASE_URL/status" >/dev/null
curl -fsS "$BASE_URL/manifest" >/dev/null
echo "Smoke test passed for $TARGET"
