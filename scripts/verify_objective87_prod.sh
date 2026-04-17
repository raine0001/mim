#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
EXPECTED_OBJECTIVE="${EXPECTED_OBJECTIVE:-87}"
EXPECTED_RELEASE_TAG="${EXPECTED_RELEASE_TAG:-objective-87}"

exec "$PYTHON_BIN" "$ROOT_DIR/scripts/probe_objective87_prod.py" \
  --base-url "$BASE_URL" \
  --expected-objective "$EXPECTED_OBJECTIVE" \
  --expected-release-tag "$EXPECTED_RELEASE_TAG" \
  "$@"