#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/env/.env.test"
COMPOSE_FILE="$ROOT_DIR/docker/test/compose.yaml"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

set_kv() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

wait_for_http() {
  local url="$1"
  local timeout_seconds="$2"
  local start_time
  start_time="$(date +%s)"
  while true; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    if (( $(date +%s) - start_time >= timeout_seconds )); then
      return 1
    fi
    sleep 2
  done
}

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing test env file: $ENV_FILE"
  exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Missing python runtime at $PYTHON_BIN"
  exit 1
fi

GIT_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"
SHORT_SHA="$(git -C "$ROOT_DIR" rev-parse --short HEAD)"
BUILD_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RELEASE_TAG="${1:-test-${SHORT_SHA}}"
EXPECTED_SCHEMA="$(MIM_ROOT_DIR="$ROOT_DIR" $PYTHON_BIN -c 'import os, re, pathlib; root = pathlib.Path(os.environ["MIM_ROOT_DIR"]); text = (root / "core" / "manifest.py").read_text(encoding="utf-8"); m = re.search(r"SCHEMA_VERSION\s*=\s*\"([^\"]+)\"", text); print(m.group(1) if m else "unknown")')"

echo "[1/5] Stamp test runtime metadata"
set_kv BUILD_GIT_SHA "$GIT_SHA"
set_kv BUILD_TIMESTAMP "$BUILD_TS"
set_kv RELEASE_TAG "$RELEASE_TAG"

echo "[2/5] Rebuild/restart test stack"
sudo docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d --build

echo "[3/5] Wait for test health/status"
wait_for_http "http://127.0.0.1:8001/health" 120 || { echo "Test health endpoint did not become ready"; exit 1; }
wait_for_http "http://127.0.0.1:8001/status" 120 || { echo "Test status endpoint did not become ready"; exit 1; }

echo "[4/5] Validate test manifest schema + endpoints"
MIM_EXPECTED_SCHEMA="$EXPECTED_SCHEMA" \
  $PYTHON_BIN - <<'PY'
import json
import os
import urllib.request

expected_schema = os.environ.get("MIM_EXPECTED_SCHEMA", "unknown")
required_endpoints = {
    "/state-bus/events",
    "/state-bus/reactions/mim-tod/step",
    "/interface/sessions/{session_key}",
    "/interface/sessions/{session_key}/messages",
    "/interface/sessions/{session_key}/approvals",
}

with urllib.request.urlopen("http://127.0.0.1:8001/manifest", timeout=20) as response:
    payload = json.loads(response.read().decode("utf-8"))

actual_schema = str(payload.get("schema_version", "unknown"))
actual_release = str(payload.get("release_tag", "unknown"))
raw_endpoints = payload.get("endpoints") or payload.get("available_endpoints") or []
available_endpoints = set()
for endpoint in raw_endpoints:
  if isinstance(endpoint, str):
    available_endpoints.add(endpoint)
  elif isinstance(endpoint, dict):
    path = endpoint.get("path")
    if path:
      available_endpoints.add(path)

missing = sorted(required_endpoints - available_endpoints)

print(f"test_release_tag={actual_release}")
print(f"test_schema_version={actual_schema}")

if actual_schema != expected_schema:
    raise SystemExit(f"schema mismatch: expected={expected_schema} actual={actual_schema}")
if missing:
    raise SystemExit(f"manifest missing expected endpoints: {', '.join(missing)}")

print("manifest_validation=pass")
PY

echo "[5/5] Refresh shared MIM context export"
"$PYTHON_BIN" "$ROOT_DIR/scripts/export_mim_context.py"

echo "Test stack refresh complete."
