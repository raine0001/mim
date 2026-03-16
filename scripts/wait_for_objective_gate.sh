#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VALIDATOR="${VALIDATOR:-$ROOT_DIR/scripts/validate_mim_tod_gate.sh}"
EXPECTED_OBJECTIVE="${EXPECTED_OBJECTIVE:-75}"
POLL_SECONDS="${POLL_SECONDS:-3}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-300}"

if [[ ! -x "$VALIDATOR" ]]; then
  echo "validator not executable: $VALIDATOR" >&2
  exit 2
fi

start_epoch="$(date +%s)"
attempt=0

while true; do
  attempt=$((attempt + 1))
  now_iso="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  set +e
  output="$(EXPECTED_OBJECTIVE="$EXPECTED_OBJECTIVE" "$VALIDATOR" 2>&1)"
  code=$?
  set -e

  if [[ $code -eq 0 ]]; then
    echo "[$now_iso] gate PASS on attempt $attempt"
    echo "$output"
    exit 0
  fi

  elapsed=$(( $(date +%s) - start_epoch ))
  if (( elapsed >= TIMEOUT_SECONDS )); then
    echo "[$now_iso] gate TIMEOUT after ${elapsed}s (attempts=$attempt)" >&2
    echo "$output" >&2
    exit 1
  fi

  echo "[$now_iso] gate not ready (attempt $attempt, elapsed=${elapsed}s); retrying in ${POLL_SECONDS}s"
  sleep "$POLL_SECONDS"
done
