#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-test}"
CANARY_SCOPE="${2:-objective97-canary-$(date -u +%Y%m%d%H%M%S)}"

if [[ "$TARGET" == "prod" ]]; then
  BASE_URL="http://127.0.0.1:8000"
else
  BASE_URL="http://127.0.0.1:8001"
fi

post_json() {
  local path="$1"
  local payload="$2"
  curl -fsS -X POST "$BASE_URL$path" \
    -H 'Content-Type: application/json' \
    -d "$payload"
}

get_json() {
  local path="$1"
  curl -fsS "$BASE_URL$path"
}

echo "Running Objective 97 canary on $TARGET ($BASE_URL), scope=$CANARY_SCOPE"

intake_payload="$(cat <<JSON
{
  "text": "objective97 canary workspace check $CANARY_SCOPE",
  "parsed_intent": "observe_workspace",
  "confidence": 0.97,
  "requested_goal": "canary inspect $CANARY_SCOPE",
  "metadata_json": {
    "capability": "workspace_check",
    "managed_scope": "$CANARY_SCOPE"
  }
}
JSON
)"

execution_payload="$(post_json "/gateway/intake/text" "$intake_payload")"
execution_id="$(printf '%s' "$execution_payload" | python3 -c 'import json,sys; p=json.load(sys.stdin); print((p.get("execution") or {}).get("execution_id", ""))')"
trace_id="$(printf '%s' "$execution_payload" | python3 -c 'import json,sys; p=json.load(sys.stdin); print((p.get("execution") or {}).get("trace_id", ""))')"

if [[ -z "$execution_id" || -z "$trace_id" ]]; then
  echo "FAIL: could not create canary execution"
  echo "$execution_payload"
  exit 1
fi

echo "Created execution_id=$execution_id trace_id=$trace_id"

failed_feedback="$(cat <<JSON
{
  "actor": "executor",
  "status": "failed",
  "reason": "objective97 canary simulated failure",
  "feedback_json": {"objective": "97", "canary": true}
}
JSON
)"
post_json "/gateway/capabilities/executions/$execution_id/feedback" "$failed_feedback" >/dev/null

echo "Injected failed feedback"

attempt_payload="$(cat <<JSON
{
  "actor": "objective97-canary",
  "source": "objective97_canary",
  "trace_id": "$trace_id",
  "requested_decision": "retry_current_step"
}
JSON
)"
post_json "/execution/recovery/attempt" "$attempt_payload" >/dev/null

echo "Recorded recovery attempt"

failed_again_feedback="$(cat <<JSON
{
  "actor": "executor",
  "status": "failed",
  "reason": "objective97 canary failed again",
  "feedback_json": {"objective": "97", "canary": true}
}
JSON
)"
post_json "/gateway/capabilities/executions/$execution_id/feedback" "$failed_again_feedback" >/dev/null

echo "Injected failed-again feedback"

recovery_json="$(get_json "/execution/recovery/$trace_id")"
python3 - <<'PY' "$recovery_json"
import json
import sys
payload = json.loads(sys.argv[1])
recovery = payload.get("recovery", {}) if isinstance(payload, dict) else {}
if not isinstance(recovery, dict):
    raise SystemExit("FAIL: recovery payload missing")
if "recovery_learning" not in recovery:
    raise SystemExit("FAIL: recovery_learning missing from recovery payload")
print("PASS: canary recovery payload includes recovery_learning")
PY

scope_encoded="$(python3 - <<'PY' "$CANARY_SCOPE" "$trace_id"
import sys, urllib.parse
scope = sys.argv[1]
trace = sys.argv[2]
print(urllib.parse.quote(f"execution-recovery:{scope}:{trace}", safe=""))
PY
)"

snapshot_json="$(get_json "/state-bus/snapshots/$scope_encoded")"
python3 - <<'PY' "$snapshot_json"
import json
import sys
payload = json.loads(sys.argv[1])
snapshot = payload.get("snapshot", {}) if isinstance(payload, dict) else {}
state_payload = snapshot.get("state_payload_json", {}) if isinstance(snapshot, dict) else {}
if "recovery_learning" not in state_payload:
    raise SystemExit("FAIL: state bus snapshot missing recovery_learning")
print("PASS: state bus snapshot includes recovery_learning")
PY

echo "Objective 97 canary passed for scope=$CANARY_SCOPE"
