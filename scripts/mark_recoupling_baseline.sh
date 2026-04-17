#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/runtime/logs}"
SHARED_DIR="${SHARED_DIR:-$ROOT_DIR/runtime/shared}"
OUT_FILE="${OUT_FILE:-$LOG_DIR/tod_recoupling_baseline.latest.json}"
MODE="${MODE:-local}"                # local | external
EXPECTED_OBJECTIVE="${EXPECTED_OBJECTIVE:-75}"
REQUIRED_CONSECUTIVE="${REQUIRED_CONSECUTIVE:-3}"
SOURCE="${SOURCE:-operator}"
NOTES="${NOTES:-}"
EXTERNAL_CAN_RECOUPLE="${EXTERNAL_CAN_RECOUPLE:-}"

mkdir -p "$LOG_DIR"

if [[ "$MODE" != "local" && "$MODE" != "external" ]]; then
  echo "ERROR: MODE must be 'local' or 'external'"
  exit 2
fi

if [[ "$MODE" == "external" ]]; then
  if [[ -z "$EXTERNAL_CAN_RECOUPLE" ]]; then
    echo "ERROR: EXTERNAL_CAN_RECOUPLE must be set in external mode (true|false)"
    exit 2
  fi
fi

GATE_OUTPUT=""
GATE_STATUS="unknown"
GATE_EXIT=99

if [[ "$MODE" == "local" ]]; then
  set +e
  GATE_OUTPUT="$(EXPECTED_OBJECTIVE="$EXPECTED_OBJECTIVE" REQUIRED_CONSECUTIVE="$REQUIRED_CONSECUTIVE" "$ROOT_DIR/scripts/check_tod_recoupling_gate.sh" 2>&1)"
  GATE_EXIT=$?
  set -e
  if [[ $GATE_EXIT -eq 0 ]]; then
    GATE_STATUS="pass"
  else
    GATE_STATUS="fail"
  fi
else
  case "${EXTERNAL_CAN_RECOUPLE,,}" in
    true|1|yes)
      GATE_STATUS="pass"
      GATE_EXIT=0
      ;;
    false|0|no)
      GATE_STATUS="fail"
      GATE_EXIT=1
      ;;
    *)
      echo "ERROR: EXTERNAL_CAN_RECOUPLE must be true/false"
      exit 2
      ;;
  esac
  GATE_OUTPUT="external attestation"
fi

python3 - <<'PY' "$OUT_FILE" "$MODE" "$SOURCE" "$NOTES" "$EXPECTED_OBJECTIVE" "$REQUIRED_CONSECUTIVE" "$GATE_STATUS" "$GATE_EXIT" "$GATE_OUTPUT" "$SHARED_DIR"
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

out_file = Path(sys.argv[1])
mode = sys.argv[2]
source = sys.argv[3]
notes = sys.argv[4]
expected_objective = int(sys.argv[5])
required_consecutive = int(sys.argv[6])
gate_status = sys.argv[7]
gate_exit = int(sys.argv[8])
gate_output = sys.argv[9]
shared_dir = Path(sys.argv[10])


def read_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def git_rev_parse(*args: str):
  try:
    return subprocess.check_output(["git", "rev-parse", *args], text=True).strip()
  except Exception:
    return "unknown"

state_file = out_file.parent / "tod_recoupling_gate_state.latest.json"
status_file = shared_dir / "TOD_INTEGRATION_STATUS.latest.json"
result_file = shared_dir / "TOD_MIM_TASK_RESULT.latest.json"
catchup_file = shared_dir / "TOD_CATCHUP_GATE.latest.json"
ack_file = shared_dir / "TOD_TO_MIM_TRIGGER_ACK.latest.json"

state = read_json(state_file) or {}
status = read_json(status_file) or {}
result = read_json(result_file) or {}
catchup = read_json(catchup_file) or {}
ack = read_json(ack_file) or {}

alignment = status.get("objective_alignment") or {}
review_gate = result.get("review_gate") or {}

payload = {
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "type": "tod_recoupling_baseline_v1",
    "mode": mode,
    "source": source,
    "notes": notes,
    "expected_objective": expected_objective,
    "required_consecutive": required_consecutive,
    "can_recouple": gate_exit == 0,
    "gate": {
        "status": gate_status,
        "exit_code": gate_exit,
        "output": gate_output,
    },
    "workspace": {
        "git_branch": git_rev_parse("--abbrev-ref", "HEAD"),
        "git_commit": git_rev_parse("HEAD"),
    },
    "snapshot": {
        "pass_streak": state.get("pass_streak"),
        "last_sample_pass": state.get("last_sample_pass"),
        "last_ack_generated_at": ack.get("generated_at"),
        "objective_alignment": {
            "status": alignment.get("status"),
            "aligned": alignment.get("aligned"),
            "tod_current_objective": alignment.get("tod_current_objective"),
            "mim_objective_active": alignment.get("mim_objective_active"),
        },
        "review_gate": {
            "passed": review_gate.get("passed"),
            "request_id": result.get("request_id"),
        },
        "catchup_gate": {
            "gate_pass": catchup.get("gate_pass"),
            "objective": catchup.get("objective"),
        },
    },
}

out_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

print(f"BASELINE: {'PASS' if payload['can_recouple'] else 'FAIL'}")
print(f"- mode: {mode}")
print(f"- source: {source}")
print(f"- out_file: {out_file}")
PY
