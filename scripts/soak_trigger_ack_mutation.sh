#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-$ROOT_DIR/runtime/shared}"
TRIGGER_FILE="$SHARED_DIR/MIM_TO_TOD_TRIGGER.latest.json"
ACK_FILE="$SHARED_DIR/TOD_TO_MIM_TRIGGER_ACK.latest.json"

CYCLES="${CYCLES:-3}"
SLEEP_SECONDS="${SLEEP_SECONDS:-2}"
TRIGGER_TYPE="${TRIGGER_TYPE:-go_order_posted}"
TRIGGER_ARTIFACT="${TRIGGER_ARTIFACT:-MIM_TOD_GO_ORDER.latest.json}"
WAIT_TIMEOUT_SECONDS="${WAIT_TIMEOUT_SECONDS:-8}"
TRIGGER_TASK_ID="${TRIGGER_TASK_ID:-}"

if [[ ! -f "$ACK_FILE" ]]; then
  echo "SOAK: FAIL"
  echo "- missing file: $ACK_FILE"
  exit 1
fi

last_ack_ts=""

for ((i=1; i<=CYCLES; i++)); do
  now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  cat > "$TRIGGER_FILE" <<EOF
{
  "generated_at": "$now",
  "packet_type": "shared-trigger-v1",
  "source_actor": "MIM",
  "target_actor": "TOD",
  "trigger": "$TRIGGER_TYPE",
  "artifact": "$TRIGGER_ARTIFACT",
  "task_id": "$TRIGGER_TASK_ID",
  "action_required": "pull_latest_and_ack",
  "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json"
}
EOF

  sleep "$SLEEP_SECONDS"

  ack_ts=""
  ack_task=""
  start_epoch="$(date +%s)"
  while true; do
    ack_payload="$(python3 - "$ACK_FILE" <<'PY'
import json,sys
from pathlib import Path
data=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
print(f"{str(data.get('generated_at','')).strip()}\t{str(data.get('acknowledges','')).strip()}")
PY
)"
    ack_ts="${ack_payload%%$'\t'*}"
    ack_task="${ack_payload#*$'\t'}"

    if [[ -n "$ack_ts" && ( -z "$TRIGGER_TASK_ID" || "$ack_task" == "$TRIGGER_TASK_ID" ) && ( -z "$last_ack_ts" || "$ack_ts" != "$last_ack_ts" ) ]]; then
      break
    fi

    now_epoch="$(date +%s)"
    if (( now_epoch - start_epoch >= WAIT_TIMEOUT_SECONDS )); then
      echo "SOAK: FAIL"
      if [[ -z "$ack_ts" ]]; then
        echo "- cycle $i: missing ack generated_at"
      elif [[ -n "$TRIGGER_TASK_ID" && "$ack_task" != "$TRIGGER_TASK_ID" ]]; then
        echo "- cycle $i: ack acknowledges unexpected task ($ack_task), expected=$TRIGGER_TASK_ID"
      else
        echo "- cycle $i: ack generated_at unchanged ($ack_ts)"
      fi
      exit 1
    fi

    sleep 1
  done

  if [[ -z "$ack_ts" ]]; then
    echo "SOAK: FAIL"
    echo "- cycle $i: missing ack generated_at"
    exit 1
  fi

  echo "- cycle $i: ack generated_at=$ack_ts"
  last_ack_ts="$ack_ts"
done

echo "SOAK: PASS"
echo "- observed $CYCLES consecutive ack timestamp mutations"
