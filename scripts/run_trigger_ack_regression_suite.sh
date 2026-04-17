#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

EXPECTED_OBJECTIVE="${EXPECTED_OBJECTIVE:-75}"
MAX_LAG_SECONDS="${MAX_LAG_SECONDS:-300}"
CLOCK_SKEW_SECONDS="${CLOCK_SKEW_SECONDS:-300}"
SOAK_CYCLES="${SOAK_CYCLES:-3}"
SOAK_SLEEP_SECONDS="${SOAK_SLEEP_SECONDS:-2}"
SOAK_WAIT_TIMEOUT_SECONDS="${SOAK_WAIT_TIMEOUT_SECONDS:-12}"
PREFLIGHT_SLEEP_SECONDS="${PREFLIGHT_SLEEP_SECONDS:-2}"
PREFLIGHT_TRIGGER_TYPE="${PREFLIGHT_TRIGGER_TYPE:-go_order_posted}"
PREFLIGHT_TRIGGER_ARTIFACT="${PREFLIGHT_TRIGGER_ARTIFACT:-MIM_TOD_GO_ORDER.latest.json}"
SHARED_DIR="${SHARED_DIR:-$ROOT_DIR/runtime/shared}"

resolve_current_task_id() {
  local request_file="$SHARED_DIR/MIM_TOD_TASK_REQUEST.latest.json"
  if [[ ! -f "$request_file" ]]; then
    return 0
  fi

  python3 - "$request_file" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
except Exception:
    print("")
    raise SystemExit(0)

print(str(data.get("task_id") or "").strip())
PY
}

CURRENT_TASK_ID="$(resolve_current_task_id)"

run_check() {
  local name="$1"
  shift

  echo "[suite] ${name}"
  if "$@"; then
    echo "[suite] ${name}: PASS"
    return 0
  fi
  echo "[suite] ${name}: FAIL"
  return 1
}

overall=0

echo "[suite] preflight_trigger"
now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
cat > "$SHARED_DIR/MIM_TO_TOD_TRIGGER.latest.json" <<EOF
{
  "generated_at": "$now",
  "packet_type": "shared-trigger-v1",
  "source_actor": "MIM",
  "target_actor": "TOD",
  "trigger": "$PREFLIGHT_TRIGGER_TYPE",
  "artifact": "$PREFLIGHT_TRIGGER_ARTIFACT",
  "task_id": "$CURRENT_TASK_ID",
  "action_required": "pull_latest_and_ack",
  "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json"
}
EOF
sleep "$PREFLIGHT_SLEEP_SECONDS"
echo "[suite] preflight_trigger: PASS"

if ! run_check "objective_gate" env EXPECTED_OBJECTIVE="$EXPECTED_OBJECTIVE" "$ROOT_DIR/scripts/validate_mim_tod_gate.sh"; then
  overall=1
fi

freshness_failed=0
if ! run_check "trigger_ack_freshness" env MAX_LAG_SECONDS="$MAX_LAG_SECONDS" CLOCK_SKEW_SECONDS="$CLOCK_SKEW_SECONDS" EXPECTED_TASK_ID="$CURRENT_TASK_ID" "$ROOT_DIR/scripts/assert_trigger_ack_freshness.sh"; then
  freshness_failed=1
  overall=1
fi

soak_failed=0
if ! run_check "trigger_ack_monotonic_soak" env CYCLES="$SOAK_CYCLES" SLEEP_SECONDS="$SOAK_SLEEP_SECONDS" WAIT_TIMEOUT_SECONDS="$SOAK_WAIT_TIMEOUT_SECONDS" TRIGGER_TASK_ID="$CURRENT_TASK_ID" "$ROOT_DIR/scripts/soak_trigger_ack_mutation.sh"; then
  soak_failed=1
  overall=1
fi

if (( freshness_failed == 1 && soak_failed == 0 )); then
  if run_check "trigger_ack_freshness_post_soak" env MAX_LAG_SECONDS="$MAX_LAG_SECONDS" CLOCK_SKEW_SECONDS="$CLOCK_SKEW_SECONDS" EXPECTED_TASK_ID="$CURRENT_TASK_ID" "$ROOT_DIR/scripts/assert_trigger_ack_freshness.sh"; then
    overall=$(( overall - 1 ))
  fi
fi

if (( overall == 0 )); then
  echo "SUITE: PASS"
  exit 0
fi

echo "SUITE: FAIL"
exit 1
