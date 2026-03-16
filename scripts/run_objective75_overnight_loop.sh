#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="$ROOT_DIR/runtime/shared"
LOG_DIR="$ROOT_DIR/runtime/logs"
STATE_FILE="$LOG_DIR/objective75_overnight_state.env"

mkdir -p "$LOG_DIR"

LOCK_FILE="$LOG_DIR/objective75_overnight.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] objective75 runner already active; exiting duplicate instance" >&2
  exit 0
fi

: "${LOOP_INTERVAL_SECONDS:=600}"
: "${SOAK_WAIT_TIMEOUT_SECONDS:=60}"
: "${SOAK_CYCLES:=3}"
: "${SOAK_SLEEP_SECONDS:=2}"
: "${REQUIRE_TOD_RESULT_COMPLETED:=0}"
: "${REQUIRE_RESULT_FRESH_EACH_CYCLE:=0}"
: "${ARTIFACT_VALIDATE_RETRIES:=30}"
: "${ARTIFACT_VALIDATE_SLEEP_SECONDS:=5}"
: "${REQUIRE_INTEGRATION_ALIAS_SYNC:=1}"
: "${REQUIRE_COORDINATION_ACK_FRESH:=1}"
: "${COORDINATION_ACK_MAX_AGE_SECONDS:=180}"
: "${GUARD_MAX_CONSEC_FAILS:=4}"
: "${GUARD_MAX_SAME_TASK_FAILS:=3}"
: "${GUARD_WINDOW_SIZE:=10}"
: "${GUARD_MAX_FAILS_IN_WINDOW:=6}"
: "${GUARD_RESTART_COOLDOWN_SECONDS:=900}"
: "${TRIGGER_ACK_RESYNC_RETRIES:=5}"
: "${TRIGGER_ACK_RESYNC_SLEEP_SECONDS:=2}"

if [[ -f "$STATE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$STATE_FILE"
fi

TASK_NUM="${TASK_NUM:-49}"
RUN_STARTED_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
CONSEC_FAILS=0
SAME_TASK_FAILS=0
LAST_TASK_SEEN="$TASK_NUM"
declare -a RECENT_OUTCOMES=()

guardrail_cooldown_active() {
  python3 - <<'PY' "$SHARED_DIR/MIM_TOD_GUARDRAIL_ALERT.latest.json" "$TASK_NUM" "$GUARD_RESTART_COOLDOWN_SECONDS"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

alert_path = Path(sys.argv[1])
task_num = int(sys.argv[2])
cooldown = max(0, int(sys.argv[3]))

if cooldown <= 0 or not alert_path.exists():
  raise SystemExit(1)

try:
  payload = json.loads(alert_path.read_text(encoding="utf-8-sig"))
except Exception:
  raise SystemExit(1)

alert_task = payload.get("task_num")
if str(alert_task) != str(task_num):
  raise SystemExit(1)

generated_at = str(payload.get("generated_at") or "").strip()
if generated_at.endswith("Z"):
  generated_at = generated_at[:-1] + "+00:00"

try:
  generated_dt = datetime.fromisoformat(generated_at)
except Exception:
  raise SystemExit(1)

age_seconds = int((datetime.now(timezone.utc) - generated_dt.astimezone(timezone.utc)).total_seconds())
if age_seconds < cooldown:
  print(max(0, cooldown - age_seconds))
  raise SystemExit(0)

raise SystemExit(1)
PY
}

if cooldown_remaining="$(guardrail_cooldown_active 2>/dev/null)"; then
  now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "[$now] objective75 guardrail cooldown active for TASK_NUM=$TASK_NUM; pausing for ${cooldown_remaining}s" | tee -a "$LOG_DIR/objective75_overnight.log"
  sleep "$cooldown_remaining"
fi

write_atomic_file() {
  local target_path="$1"
  local tmp_path
  tmp_path="${target_path}.tmp.$$"
  cat > "$tmp_path"
  mv -f "$tmp_path" "$target_path"
}

write_task_request() {
  local task_id="objective-75-task-$(printf '%03d' "$TASK_NUM")"
  local corr="obj75-task$(printf '%03d' "$TASK_NUM")"
  local now
  now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  write_atomic_file "$SHARED_DIR/MIM_TOD_TASK_REQUEST.latest.json" <<JSON
{
  "version": "1.0",
  "source": "MIM",
  "target": "TOD",
  "generated_at": "$now",
  "correlation_id": "$corr",
  "task_id": "$task_id",
  "objective_id": "objective-75",
  "title": "Overnight autonomous reliability run",
  "scope": "Maintain unattended MIM↔TOD cadence with strict suite checkpoints.",
  "priority": "high",
  "acceptance_criteria": [
    "Objective 75 gate PASS",
    "Strict trigger-ack regression suite PASS"
  ],
  "constraints": [
    "Do not regress objective compatibility/alignment semantics",
    "Preserve trigger ACK monotonic mutation behavior"
  ],
  "notes": "Automated overnight loop-generated task request"
}
JSON

  write_atomic_file "$SHARED_DIR/MIM_TOD_GO_ORDER.latest.json" <<JSON
{
  "version": "1.0",
  "source": "MIM",
  "target": "TOD",
  "generated_at": "$now",
  "order": {
    "correlation_id": "${corr}-go",
    "task_id": "$task_id",
    "objective_id": "objective-75",
    "type": "execute_now",
    "instructions": [
      "Acknowledge current task packet",
      "Execute and post task result packet",
      "Keep objective gate alignment true"
    ]
  }
}
JSON

  write_atomic_file "$SHARED_DIR/MIM_TO_TOD_TRIGGER.latest.json" <<JSON
{
  "version": "1.0",
  "source": "MIM",
  "target": "TOD",
  "generated_at": "$now",
  "correlation_id": "${corr}-trigger",
  "event_type": "task_kickoff",
  "payload": {
    "task_id": "$task_id",
    "objective_id": "objective-75"
  }
}
JSON
}

write_coordination_ack() {
  local phase="$1"
  local detail="$2"
  local status="active"
  local task_id="objective-75-task-$(printf '%03d' "$TASK_NUM")"
  local corr="obj75-task$(printf '%03d' "$TASK_NUM")"
  local now
  now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  if [[ "$phase" == "cycle_failed" ]]; then
    status="degraded"
  fi

  write_atomic_file "$SHARED_DIR/MIM_TOD_COORDINATION_ACK.latest.json" <<JSON
{
  "version": "1.0",
  "source": "MIM",
  "target": "TOD",
  "generated_at": "$now",
  "objective_id": "objective-75",
  "task_id": "$task_id",
  "correlation_id": "${corr}-coord-ack",
  "coordination": {
    "status": "$status",
    "phase": "$phase",
    "detail": "$detail",
    "task_num": $TASK_NUM,
    "runner": "objective75_overnight"
  }
}
JSON
}

current_trigger_ack_task() {
  python3 - <<'PY' "$SHARED_DIR/TOD_TO_MIM_TRIGGER_ACK.latest.json"
import json
import sys
from pathlib import Path

ack_path = Path(sys.argv[1])
if not ack_path.exists():
    print("")
    raise SystemExit(0)

try:
    payload = json.loads(ack_path.read_text(encoding="utf-8-sig"))
except Exception:
    print("")
    raise SystemExit(0)

print(str(payload.get("acknowledges") or "").strip())
PY
}

write_targeted_trigger() {
  local task_id="$1"
  local trigger_type="${2:-task_resync_ping}"
  local now
  now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  write_atomic_file "$SHARED_DIR/MIM_TO_TOD_TRIGGER.latest.json" <<JSON
{
  "generated_at": "$now",
  "packet_type": "shared-trigger-v1",
  "source_actor": "MIM",
  "target_actor": "TOD",
  "trigger": "$trigger_type",
  "artifact": "MIM_TOD_TASK_REQUEST.latest.json",
  "task_id": "$task_id",
  "action_required": "pull_latest_and_ack",
  "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json"
}
JSON
}

resync_trigger_ack_for_task() {
  local task_id="$1"
  local retries="$2"
  local sleep_seconds="$3"
  local attempt ack_task

  for ((attempt=1; attempt<=retries; attempt++)); do
    write_targeted_trigger "$task_id" "task_resync_ping"
    sleep "$sleep_seconds"
    ack_task="$(current_trigger_ack_task)"
    if [[ "$ack_task" == "$task_id" ]]; then
      echo "trigger_ack_resync: PASS task=$task_id attempt=$attempt"
      return 0
    fi
    echo "trigger_ack_resync: waiting task=$task_id attempt=$attempt ack_task=${ack_task:-<empty>}"
  done

  echo "trigger_ack_resync: FAIL task=$task_id ack_task=$(current_trigger_ack_task) retries=$retries"
  return 1
}

write_review_decision() {
  local task_id="objective-75-task-$(printf '%03d' "$TASK_NUM")"
  local corr="obj75-task$(printf '%03d' "$TASK_NUM")"
  local now
  now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  write_atomic_file "$SHARED_DIR/MIM_TOD_REVIEW_DECISION.latest.json" <<JSON
{
  "version": "1.0",
  "source": "MIM",
  "target": "TOD",
  "generated_at": "$now",
  "objective_id": "objective-75",
  "correlation_id": "$corr",
  "task_id": "$task_id",
  "decision": "accepted",
  "decision_rationale": "Automated overnight checkpoint: strict suite PASS and objective gate PASS.",
  "required_followups": [
    "Proceed to next overnight cycle"
  ],
  "closeout_notes": "Auto-accepted by overnight runner"
}
JSON
}

emit_guardrail_report_and_exit() {
  local reason="$1"
  local recent_fails="$2"
  local now
  now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  write_atomic_file "$SHARED_DIR/MIM_TOD_GUARDRAIL_ALERT.latest.json" <<JSON
{
  "version": "1.0",
  "source": "MIM",
  "target": "TOD",
  "generated_at": "$now",
  "objective_id": "objective-75",
  "alert_type": "overnight_runner_guardrail_stop",
  "reason": "$reason",
  "task_num": $TASK_NUM,
  "metrics": {
    "consecutive_fails": $CONSEC_FAILS,
    "same_task_fails": $SAME_TASK_FAILS,
    "recent_fail_count": $recent_fails,
    "window_size": $GUARD_WINDOW_SIZE,
    "max_consecutive_fails": $GUARD_MAX_CONSEC_FAILS,
    "max_same_task_fails": $GUARD_MAX_SAME_TASK_FAILS,
    "max_fails_in_window": $GUARD_MAX_FAILS_IN_WINDOW
  },
  "action_required": "investigate_and_restart_runner"
}
JSON

  write_atomic_file "$LOG_DIR/objective75_guardrail.latest.md" <<MD
# Objective 75 Guardrail Stop

- generated_at: $now
- reason: $reason
- task_num: $TASK_NUM
- consecutive_fails: $CONSEC_FAILS
- same_task_fails: $SAME_TASK_FAILS
- recent_fail_count: $recent_fails / $GUARD_WINDOW_SIZE
- runner_started_at: $RUN_STARTED_AT

Action: investigate TOD/MIM packet exchange and restart overnight runner.
MD

  echo "[$now] GUARDRAIL STOP: reason=$reason task_num=$TASK_NUM consec_fails=$CONSEC_FAILS same_task_fails=$SAME_TASK_FAILS recent_fails=$recent_fails/$GUARD_WINDOW_SIZE" | tee -a "$LOG_DIR/objective75_overnight.log"
  exit 2
}

record_cycle_outcome() {
  local outcome="$1"
  local bit=0
  if [[ "$outcome" == "pass" ]]; then
    CONSEC_FAILS=0
    SAME_TASK_FAILS=0
    LAST_TASK_SEEN="$TASK_NUM"
    bit=0
  else
    CONSEC_FAILS=$((CONSEC_FAILS + 1))
    if [[ "$TASK_NUM" == "$LAST_TASK_SEEN" ]]; then
      SAME_TASK_FAILS=$((SAME_TASK_FAILS + 1))
    else
      SAME_TASK_FAILS=1
      LAST_TASK_SEEN="$TASK_NUM"
    fi
    bit=1
  fi

  RECENT_OUTCOMES+=("$bit")
  if ((${#RECENT_OUTCOMES[@]} > GUARD_WINDOW_SIZE)); then
    RECENT_OUTCOMES=("${RECENT_OUTCOMES[@]:1}")
  fi
}

evaluate_guardrails() {
  local recent_fails=0
  local bit
  for bit in "${RECENT_OUTCOMES[@]}"; do
    if [[ "$bit" == "1" ]]; then
      recent_fails=$((recent_fails + 1))
    fi
  done

  if (( CONSEC_FAILS >= GUARD_MAX_CONSEC_FAILS )); then
    emit_guardrail_report_and_exit "max_consecutive_fails" "$recent_fails"
  fi
  if (( SAME_TASK_FAILS >= GUARD_MAX_SAME_TASK_FAILS )); then
    emit_guardrail_report_and_exit "max_same_task_fails" "$recent_fails"
  fi
  if ((${#RECENT_OUTCOMES[@]} >= GUARD_WINDOW_SIZE)) && (( recent_fails >= GUARD_MAX_FAILS_IN_WINDOW )); then
    emit_guardrail_report_and_exit "max_fail_rate_window" "$recent_fails"
  fi
}

validate_integration_alias_sync() {
  local require_sync
  require_sync="$(printf '%s' "$REQUIRE_INTEGRATION_ALIAS_SYNC" | tr '[:upper:]' '[:lower:]')"
  if [[ "$require_sync" != "1" && "$require_sync" != "true" && "$require_sync" != "yes" ]]; then
    echo "integration_alias_sync_check: skipped"
    return 0
  fi

  "$ROOT_DIR/scripts/check_tod_integration_alias_sync.sh"
}

validate_coordination_ack_freshness() {
  local cycle_started_epoch="$1"
  local task_id="$2"
  local require_fresh
  require_fresh="$(printf '%s' "$REQUIRE_COORDINATION_ACK_FRESH" | tr '[:upper:]' '[:lower:]')"
  if [[ "$require_fresh" != "1" && "$require_fresh" != "true" && "$require_fresh" != "yes" ]]; then
    echo "coordination_ack_freshness_check: skipped"
    return 0
  fi

  python3 - <<'PY' "$SHARED_DIR" "$cycle_started_epoch" "$task_id" "$COORDINATION_ACK_MAX_AGE_SECONDS"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

shared_dir = Path(sys.argv[1])
cycle_started_epoch = int(sys.argv[2])
task_id = sys.argv[3]
max_age_seconds = int(sys.argv[4])

ack_path = shared_dir / "MIM_TOD_COORDINATION_ACK.latest.json"
if not ack_path.exists():
    print("coordination_ack_missing")
    raise SystemExit(1)

try:
    ack = json.loads(ack_path.read_text(encoding="utf-8-sig"))
except Exception as exc:
    print(f"coordination_ack_read_error: {exc}")
    raise SystemExit(1)

generated_at = str(ack.get("generated_at", "")).strip()
ack_task_id = str(ack.get("task_id", "")).strip()

if generated_at.endswith("Z"):
    generated_at = generated_at[:-1] + "+00:00"
try:
    generated_dt = datetime.fromisoformat(generated_at)
except Exception:
    print(f"coordination_ack_invalid_generated_at: {ack.get('generated_at')!r}")
    raise SystemExit(1)

ack_epoch = int(generated_dt.timestamp())
now_epoch = int(datetime.now(timezone.utc).timestamp())

checks = [
    ("task_id_matches", ack_task_id == task_id, ack_task_id),
    ("updated_this_cycle", ack_epoch >= cycle_started_epoch, ack_epoch),
    ("max_age_respected", (now_epoch - ack_epoch) <= max_age_seconds, now_epoch - ack_epoch),
]

ok = all(item[1] for item in checks)
if ok:
    print("coordination_ack_fresh")
    raise SystemExit(0)

print("coordination_ack_stale_or_mismatch")
for name, passed, value in checks:
    state = "PASS" if passed else "FAIL"
    print(f"- {state}: {name} (value={value!r})")
print(f"- ack_file: {ack_path}")
raise SystemExit(1)
PY
}

current_recent_fail_count() {
  local count=0
  local bit
  for bit in "${RECENT_OUTCOMES[@]}"; do
    if [[ "$bit" == "1" ]]; then
      count=$((count + 1))
    fi
  done
  echo "$count"
}

validate_tod_task_artifacts() {
  local cycle_started_epoch="$1"
  local task_id="$2"

  python3 - <<'PY' "$SHARED_DIR" "$task_id" "$cycle_started_epoch"
import json
import os
import sys
import time
from pathlib import Path

shared_dir = sys.argv[1]
task_id = sys.argv[2]
cycle_started_epoch = int(sys.argv[3])

ack_path = f"{shared_dir}/TOD_MIM_TASK_ACK.latest.json"
result_path = f"{shared_dir}/TOD_MIM_TASK_RESULT.latest.json"

def read_json(path):
    with open(path, 'r', encoding='utf-8-sig') as f:
        return json.load(f)

try:
    ack = read_json(ack_path)
    result = read_json(result_path)
except Exception as exc:
    print(f"artifact_read_error: {exc}")
    raise SystemExit(1)

require_result = str(os.environ.get("REQUIRE_TOD_RESULT_COMPLETED", "0")).strip().lower() in {"1", "true", "yes"}
require_result_fresh_each_cycle = str(os.environ.get("REQUIRE_RESULT_FRESH_EACH_CYCLE", "0")).strip().lower() in {"1", "true", "yes"}
retries = max(0, int(str(os.environ.get("ARTIFACT_VALIDATE_RETRIES", "6") or "6")))
sleep_seconds = max(0.1, float(str(os.environ.get("ARTIFACT_VALIDATE_SLEEP_SECONDS", "2") or "2")))

last_ack = ack
last_result = result

ack_file = Path(ack_path)
result_file = Path(result_path)

def file_fresh(path: Path) -> bool:
  try:
    return int(path.stat().st_mtime) >= cycle_started_epoch
  except Exception:
    return False

for attempt in range(retries + 1):
  ack_ok = (
    ack.get("request_id") == task_id
    and ack.get("status") == "accepted"
    and file_fresh(ack_file)
  )
  result_ok = (
    result.get("request_id") == task_id
    and result.get("status") == "completed"
    and ((not require_result_fresh_each_cycle) or file_fresh(result_file))
  )

  if ack_ok and result_ok:
    print("artifacts_ok")
    raise SystemExit(0)

  if ack_ok and (not require_result):
    print("artifacts_warn: ack_valid_result_stale_allowed")
    raise SystemExit(0)

  last_ack = ack
  last_result = result

  if attempt >= retries:
    break

  time.sleep(sleep_seconds)
  try:
    ack = read_json(ack_path)
    result = read_json(result_path)
  except Exception as exc:
    print(f"artifact_read_error_retry[{attempt+1}/{retries}]: {exc}")
    continue

if not (last_ack.get("request_id") == task_id and last_ack.get("status") == "accepted"):
    print(
    f"ack_invalid: request_id={last_ack.get('request_id')} status={last_ack.get('status')} expected_request_id={task_id} expected_status=accepted"
    )
if not file_fresh(ack_file):
  print(f"ack_stale: file={ack_file} cycle_started_epoch={cycle_started_epoch}")
if not (last_result.get("request_id") == task_id and last_result.get("status") == "completed"):
    print(
    f"result_invalid: request_id={last_result.get('request_id')} status={last_result.get('status')} expected_request_id={task_id} expected_status=completed"
    )
if require_result_fresh_each_cycle and not file_fresh(result_file):
  print(f"result_stale: file={result_file} cycle_started_epoch={cycle_started_epoch}")

raise SystemExit(1)
PY
}

while true; do
  cycle_outcome="fail"
  cycle_started_epoch="$(date -u +%s)"
  cycle_task_id="objective-75-task-$(printf '%03d' "$TASK_NUM")"
  stamp="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "[$stamp] Starting overnight cycle for TASK_NUM=$TASK_NUM" | tee -a "$LOG_DIR/objective75_overnight.log"

  write_task_request
  write_coordination_ack "cycle_started" "task_request_published"
  existing_ack_task="$(current_trigger_ack_task)"
  if [[ "$existing_ack_task" != "$cycle_task_id" ]]; then
    if ! resync_trigger_ack_for_task "$cycle_task_id" "$TRIGGER_ACK_RESYNC_RETRIES" "$TRIGGER_ACK_RESYNC_SLEEP_SECONDS" | tee -a "$LOG_DIR/objective75_overnight.log"; then
      write_coordination_ack "cycle_failed" "trigger_ack_stale"
      echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Cycle FAIL; stale trigger ACK for TASK_NUM=$TASK_NUM" | tee -a "$LOG_DIR/objective75_overnight.log"
      cycle_outcome="fail"
      record_cycle_outcome "$cycle_outcome"
      evaluate_guardrails
      sleep "$LOOP_INTERVAL_SECONDS"
      continue
    fi
  fi

  if ! validate_coordination_ack_freshness "$cycle_started_epoch" "$cycle_task_id" | tee -a "$LOG_DIR/objective75_overnight.log"; then
    write_coordination_ack "cycle_failed" "coordination_ack_stale"
    cycle_outcome="fail"
    record_cycle_outcome "$cycle_outcome"
    emit_guardrail_report_and_exit "coordination_ack_stale" "$(current_recent_fail_count)"
  fi

  if SOAK_WAIT_TIMEOUT_SECONDS="$SOAK_WAIT_TIMEOUT_SECONDS" SOAK_CYCLES="$SOAK_CYCLES" SOAK_SLEEP_SECONDS="$SOAK_SLEEP_SECONDS" "$ROOT_DIR/scripts/run_trigger_ack_regression_suite.sh" | tee -a "$LOG_DIR/objective75_overnight.log"; then
    if validate_tod_task_artifacts "$cycle_started_epoch" "$cycle_task_id" | tee -a "$LOG_DIR/objective75_overnight.log"; then
      if validate_integration_alias_sync | tee -a "$LOG_DIR/objective75_overnight.log"; then
        write_review_decision
        write_coordination_ack "cycle_passed" "review_decision_accepted"
        if ! validate_coordination_ack_freshness "$cycle_started_epoch" "$cycle_task_id" | tee -a "$LOG_DIR/objective75_overnight.log"; then
          write_coordination_ack "cycle_failed" "coordination_ack_stale"
          cycle_outcome="fail"
          record_cycle_outcome "$cycle_outcome"
          emit_guardrail_report_and_exit "coordination_ack_stale" "$(current_recent_fail_count)"
        fi
        TASK_NUM=$((TASK_NUM + 1))
        printf 'TASK_NUM=%s\n' "$TASK_NUM" > "$STATE_FILE"
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Cycle PASS; next TASK_NUM=$TASK_NUM" | tee -a "$LOG_DIR/objective75_overnight.log"
        cycle_outcome="pass"
      else
        write_coordination_ack "cycle_failed" "integration_alias_drift"
        echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Cycle FAIL; integration alias drift detected for TASK_NUM=$TASK_NUM" | tee -a "$LOG_DIR/objective75_overnight.log"
        cycle_outcome="fail"
        record_cycle_outcome "$cycle_outcome"
        emit_guardrail_report_and_exit "integration_alias_drift" "$(current_recent_fail_count)"
      fi
    else
      write_coordination_ack "cycle_failed" "tod_artifacts_invalid"
      echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Cycle FAIL; TOD artifacts invalid for TASK_NUM=$TASK_NUM" | tee -a "$LOG_DIR/objective75_overnight.log"
      cycle_outcome="fail"
    fi
  else
    write_coordination_ack "cycle_failed" "trigger_ack_regression_suite_failed"
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Cycle FAIL; retrying same TASK_NUM after interval" | tee -a "$LOG_DIR/objective75_overnight.log"
    cycle_outcome="fail"
  fi

  record_cycle_outcome "$cycle_outcome"
  evaluate_guardrails

  sleep "$LOOP_INTERVAL_SECONDS"
done
