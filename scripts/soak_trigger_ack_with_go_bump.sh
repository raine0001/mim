#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-$ROOT_DIR/runtime/shared}"
TRIGGER_FILE="$SHARED_DIR/MIM_TO_TOD_TRIGGER.latest.json"
GO_FILE="$SHARED_DIR/MIM_TOD_GO_ORDER.latest.json"
ACK_FILE="$SHARED_DIR/TOD_TO_MIM_TRIGGER_ACK.latest.json"

CYCLES="${CYCLES:-3}"
SLEEP_SECONDS="${SLEEP_SECONDS:-4}"

if [[ ! -f "$ACK_FILE" || ! -f "$GO_FILE" ]]; then
  echo "SOAK_GO: FAIL"
  echo "- missing required shared files"
  exit 1
fi

last_ack_ts=""

for ((i=1; i<=CYCLES; i++)); do
  now="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

  python3 - "$GO_FILE" "$now" "$i" <<'PY'
import json, sys
from pathlib import Path

path = Path(sys.argv[1])
now = sys.argv[2]
idx = sys.argv[3]
data = json.loads(path.read_text(encoding="utf-8-sig"))
data["generated_at"] = now
base = str(data.get("correlation_id", "obj75-task-go"))
data["correlation_id"] = f"{base.split('-bump-')[0]}-bump-{idx}"
path.write_text(json.dumps(data, indent=2) + "\n")
PY

  cat > "$TRIGGER_FILE" <<EOF
{
  "generated_at": "$now",
  "packet_type": "shared-trigger-v1",
  "source_actor": "MIM",
  "target_actor": "TOD",
  "trigger": "go_order_posted",
  "artifact": "MIM_TOD_GO_ORDER.latest.json",
  "action_required": "pull_latest_and_ack",
  "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json"
}
EOF

  sleep "$SLEEP_SECONDS"

  ack_ts="$(python3 - "$ACK_FILE" <<'PY'
import json,sys
from pathlib import Path
data=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
print(data.get("generated_at",""))
PY
)"

  if [[ -z "$ack_ts" ]]; then
    echo "SOAK_GO: FAIL"
    echo "- cycle $i: missing ack generated_at"
    exit 1
  fi

  if [[ -n "$last_ack_ts" && "$ack_ts" == "$last_ack_ts" ]]; then
    echo "SOAK_GO: FAIL"
    echo "- cycle $i: ack generated_at unchanged ($ack_ts)"
    exit 1
  fi

  echo "- cycle $i: ack generated_at=$ack_ts"
  last_ack_ts="$ack_ts"
done

echo "SOAK_GO: PASS"
echo "- observed $CYCLES consecutive ack timestamp mutations with go-order bumps"
