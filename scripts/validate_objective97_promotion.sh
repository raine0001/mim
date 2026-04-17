#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-prod}"
SCOPE="${2:-objective97-validation}"

if [[ "$TARGET" == "prod" ]]; then
  BASE_URL="http://127.0.0.1:8000"
else
  BASE_URL="http://127.0.0.1:8001"
fi

echo "Running Objective 97 validation against $TARGET ($BASE_URL)"

manifest_json="$(curl -fsS "$BASE_URL/manifest")"
release_tag="$(printf '%s' "$manifest_json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("release_tag",""))')"
if [[ "$release_tag" != "objective-97" ]]; then
  echo "FAIL: release_tag expected objective-97, got '$release_tag'"
  exit 1
fi
echo "PASS: manifest release_tag is objective-97"

learning_resp="$(curl -fsS "$BASE_URL/execution/recovery/learning/profiles?managed_scope=$SCOPE")"
python3 - <<'PY' "$learning_resp"
import json
import sys
payload = json.loads(sys.argv[1])
if not isinstance(payload, dict):
    raise SystemExit("FAIL: learning profiles response is not an object")
if "profiles" not in payload or "latest_profile" not in payload:
    raise SystemExit("FAIL: learning profiles contract missing keys")
print("PASS: recovery learning endpoint contract valid")
PY

ui_state="$(curl -fsS "$BASE_URL/mim/ui/state?scope=system")"
python3 - <<'PY' "$ui_state"
import json
import sys
payload = json.loads(sys.argv[1])
operator_reasoning = payload.get("operator_reasoning", {}) if isinstance(payload, dict) else {}
if "execution_recovery_learning" not in operator_reasoning:
    raise SystemExit("FAIL: operator_reasoning.execution_recovery_learning missing")
print("PASS: operator UI includes execution_recovery_learning")
PY

telemetry="$(curl -fsS "$BASE_URL/execution/recovery/learning/telemetry?managed_scope=$SCOPE")"
python3 - <<'PY' "$telemetry"
import json
import sys
payload = json.loads(sys.argv[1])
if not isinstance(payload, dict):
    raise SystemExit("FAIL: telemetry response is not an object")
for key in ("window", "metrics", "alerts"):
    if key not in payload:
        raise SystemExit(f"FAIL: telemetry response missing '{key}'")
print("PASS: telemetry endpoint contract valid")
PY

echo "Objective 97 validation passed for $TARGET"
