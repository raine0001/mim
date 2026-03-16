#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-$ROOT_DIR/runtime/shared}"
EXPECTED_OBJECTIVE="${EXPECTED_OBJECTIVE:-74}"

STATUS_FILE="$SHARED_DIR/TOD_INTEGRATION_STATUS.latest.json"

if [[ ! -f "$STATUS_FILE" ]]; then
  echo "GATE: FAIL"
  echo "- missing file: $STATUS_FILE"
  exit 1
fi

python3 - "$STATUS_FILE" "$EXPECTED_OBJECTIVE" <<'PY'
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
expected_objective = int(sys.argv[2])

status = json.loads(status_path.read_text())

def get_path(d, *path):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur

def as_int(v):
    try:
        return int(v)
    except Exception:
        return None

compatible = get_path(status, "compatible")
alignment_status = (
    get_path(status, "objective_alignment", "status")
    or get_path(status, "alignment", "status")
    or get_path(status, "status")
)

mim_objective = (
    get_path(status, "objective_alignment", "mim_objective")
    or get_path(status, "objective_alignment", "mim_objective_active")
    or get_path(status, "mim_objective")
    or get_path(status, "mim", "objective")
    or get_path(status, "mim", "current_objective")
    or get_path(status, "mim_status", "objective_active")
    or get_path(status, "mim_handshake", "objective_active")
)

tod_objective = (
    get_path(status, "objective_alignment", "tod_objective")
    or get_path(status, "objective_alignment", "tod_current_objective")
    or get_path(status, "tod_objective")
    or get_path(status, "tod", "objective")
    or get_path(status, "tod", "current_objective")
)

refresh_failure = (
    get_path(status, "refresh", "failure_reason")
    or get_path(status, "refresh", "last_failure_reason")
    or get_path(status, "refresh_failure_reason")
    or get_path(status, "last_refresh_failure")
    or get_path(status, "mim_refresh", "failure_reason")
)

checks = []
checks.append(("compatible == true", compatible is True, compatible))
checks.append((
    "objective_alignment.status in {aligned, in_sync}",
    str(alignment_status).lower() in {"aligned", "in_sync"},
    alignment_status,
))
checks.append((
    f"tod objective == {expected_objective}",
    as_int(tod_objective) == expected_objective,
    tod_objective,
))
checks.append((
    f"mim objective == {expected_objective}",
    as_int(mim_objective) == expected_objective,
    mim_objective,
))
checks.append((
    "refresh failure empty",
    refresh_failure in (None, ""),
    refresh_failure,
))

all_ok = all(ok for _, ok, _ in checks)
print(f"GATE: {'PASS' if all_ok else 'FAIL'}")
for name, ok, value in checks:
    state = "PASS" if ok else "FAIL"
    print(f"- {state}: {name} (value={value!r})")

sys.exit(0 if all_ok else 1)
PY
