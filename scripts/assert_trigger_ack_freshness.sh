#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-$ROOT_DIR/runtime/shared}"
TRIGGER_FILE="$SHARED_DIR/MIM_TO_TOD_TRIGGER.latest.json"
TRIGGER_ACK_FILE="$SHARED_DIR/TOD_TO_MIM_TRIGGER_ACK.latest.json"
MAX_LAG_SECONDS="${MAX_LAG_SECONDS:-120}"
CLOCK_SKEW_SECONDS="${CLOCK_SKEW_SECONDS:-1800}"
EXPECTED_TASK_ID="${EXPECTED_TASK_ID:-}"

if [[ -z "$EXPECTED_TASK_ID" ]]; then
    REQUEST_FILE="$SHARED_DIR/MIM_TOD_TASK_REQUEST.latest.json"
    if [[ -f "$REQUEST_FILE" ]]; then
        EXPECTED_TASK_ID="$(python3 - "$REQUEST_FILE" <<'PY'
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
)"
    fi
fi

if [[ ! -f "$TRIGGER_FILE" ]]; then
  echo "ASSERT: FAIL"
  echo "- missing file: $TRIGGER_FILE"
  exit 1
fi

if [[ ! -f "$TRIGGER_ACK_FILE" ]]; then
  echo "ASSERT: FAIL"
  echo "- missing file: $TRIGGER_ACK_FILE"
  exit 1
fi

python3 - "$TRIGGER_FILE" "$TRIGGER_ACK_FILE" "$MAX_LAG_SECONDS" "$CLOCK_SKEW_SECONDS" "$EXPECTED_TASK_ID" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

trigger_path = Path(sys.argv[1])
ack_path = Path(sys.argv[2])
max_lag = int(sys.argv[3])
clock_skew = int(sys.argv[4])
expected_task_id = str(sys.argv[5] or "").strip()

trigger = json.loads(trigger_path.read_text(encoding="utf-8-sig"))
ack = json.loads(ack_path.read_text(encoding="utf-8-sig"))


def parse_ts(value):
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(v).astimezone(timezone.utc)
    except Exception:
        return None


trigger_ts = parse_ts(trigger.get("generated_at"))
ack_ts = parse_ts(ack.get("generated_at"))
ack_status = str(ack.get("status", "")).strip().lower()
ack_acknowledges = str(ack.get("acknowledges", "")).strip()
trigger_sequence = trigger.get("sequence")
ack_sequence = ack.get("ack_sequence")
acknowledged_trigger_sequence = ack.get("acknowledged_trigger_sequence")
observed_at = parse_ts(ack.get("observed_at"))

checks = []
checks.append(("trigger timestamp parseable", trigger_ts is not None, trigger.get("generated_at")))
checks.append(("ack timestamp parseable", ack_ts is not None, ack.get("generated_at")))
checks.append(("ack status is runtime (not ready_template)", ack_status not in {"", "ready_template"}, ack.get("status")))
if observed_at is not None:
    checks.append(("ack observed_at parseable", True, ack.get("observed_at")))
elif ack.get("observed_at") is not None:
    checks.append(("ack observed_at parseable", False, ack.get("observed_at")))
if expected_task_id:
    checks.append(("acknowledges expected task", ack_acknowledges == expected_task_id, ack_acknowledges))
if trigger_sequence is not None and acknowledged_trigger_sequence is not None:
    checks.append((
        "acknowledged trigger sequence matches trigger sequence",
        str(acknowledged_trigger_sequence) == str(trigger_sequence),
        {"trigger_sequence": trigger_sequence, "acknowledged_trigger_sequence": acknowledged_trigger_sequence},
    ))
if ack_sequence is not None:
    try:
        ack_sequence_int = int(ack_sequence)
        checks.append(("ack sequence parseable", ack_sequence_int >= 1, ack_sequence))
    except Exception:
        checks.append(("ack sequence parseable", False, ack_sequence))

lag_seconds = None
if trigger_ts and ack_ts:
    lag_seconds = (trigger_ts - ack_ts).total_seconds()
    checks.append(("ack not older than trigger beyond clock skew", lag_seconds <= clock_skew, lag_seconds))
    checks.append(("ack lag within threshold", lag_seconds <= max_lag, lag_seconds))

all_ok = all(ok for _, ok, _ in checks)
print(f"ASSERT: {'PASS' if all_ok else 'FAIL'}")
for name, ok, value in checks:
    state = "PASS" if ok else "FAIL"
    print(f"- {state}: {name} (value={value!r})")

sys.exit(0 if all_ok else 1)
PY
