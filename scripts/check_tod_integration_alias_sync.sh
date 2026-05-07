#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
CANONICAL_FILE="${1:-${SHARED_DIR}/TOD_INTEGRATION_STATUS.latest.json}"
LEGACY_FILE="${2:-${SHARED_DIR}/TOD_integration_status.latest.json}"

if [[ ! -f "${CANONICAL_FILE}" ]]; then
  echo "ALIAS_SYNC: FAIL"
  echo "- missing canonical file: ${CANONICAL_FILE}"
  exit 1
fi

if [[ ! -f "${LEGACY_FILE}" ]]; then
  echo "ALIAS_SYNC: FAIL"
  echo "- missing legacy file: ${LEGACY_FILE}"
  exit 1
fi

python3 - "${CANONICAL_FILE}" "${LEGACY_FILE}" <<'PY'
import json
import sys
from pathlib import Path

canonical_path = Path(sys.argv[1])
legacy_path = Path(sys.argv[2])

canonical = json.loads(canonical_path.read_text(encoding="utf-8-sig"))
legacy = json.loads(legacy_path.read_text(encoding="utf-8-sig"))


def get_path(obj, *path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur

checks = [
    (
        "alignment_status",
        get_path(canonical, "objective_alignment", "status"),
        get_path(legacy, "objective_alignment", "status"),
    ),
    (
        "tod_current_objective",
        get_path(canonical, "objective_alignment", "tod_current_objective"),
        get_path(legacy, "objective_alignment", "tod_current_objective"),
    ),
    (
        "mim_objective_active",
        get_path(canonical, "objective_alignment", "mim_objective_active"),
        get_path(legacy, "objective_alignment", "mim_objective_active"),
    ),
    (
        "compatible",
        canonical.get("compatible"),
        legacy.get("compatible"),
    ),
    (
        "refresh_failure_reason",
        get_path(canonical, "mim_refresh", "failure_reason"),
        get_path(legacy, "mim_refresh", "failure_reason"),
    ),
]

all_ok = True
print("ALIAS_SYNC: PASS" if all(c == l for _, c, l in checks) else "ALIAS_SYNC: FAIL")
for key, c_val, l_val in checks:
    ok = c_val == l_val
    all_ok = all_ok and ok
    state = "PASS" if ok else "FAIL"
    print(f"- {state}: {key} (canonical={c_val!r}, legacy={l_val!r})")

if not all_ok:
    print(f"- canonical_file: {canonical_path}")
    print(f"- legacy_file: {legacy_path}")
    sys.exit(1)
PY
