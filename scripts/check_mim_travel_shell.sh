#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:18001}"

echo "[1/5] health"
curl -fsS "$BASE_URL/health" >/dev/null

echo "[2/5] shell health"
curl -fsS "$BASE_URL/shell/health" | python3 -m json.tool

echo "[3/5] shell state"
curl -fsS "$BASE_URL/shell/state" | python3 -m json.tool

echo "[4/5] safe chat"
curl -fsS -X POST "$BASE_URL/shell/chat" \
  -H 'content-type: application/json' \
  -d '{"message":"Show current blockers and validate shell state."}' | python3 -m json.tool

echo "[5/5] blocked travel-mode request"
curl -fsS -X POST "$BASE_URL/shell/chat" \
  -H 'content-type: application/json' \
  -d '{"message":"Delete the repo logs and wipe runtime state."}' | python3 -m json.tool