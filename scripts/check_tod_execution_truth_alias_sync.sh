#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
CANONICAL_FILE="${1:-${SHARED_DIR}/TOD_EXECUTION_TRUTH.latest.json}"
LEGACY_FILE="${2:-${SHARED_DIR}/TOD_execution_truth.latest.json}"

if [[ ! -f "${CANONICAL_FILE}" ]]; then
  echo "EXECUTION_TRUTH_ALIAS_SYNC: FAIL"
  echo "- missing canonical file: ${CANONICAL_FILE}"
  exit 1
fi

if [[ ! -f "${LEGACY_FILE}" ]]; then
  echo "EXECUTION_TRUTH_ALIAS_SYNC: FAIL"
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
    ("packet_type", canonical.get("packet_type"), legacy.get("packet_type")),
    ("contract", canonical.get("contract"), legacy.get("contract")),
    (
        "summary.execution_count",
        get_path(canonical, "summary", "execution_count"),
        get_path(legacy, "summary", "execution_count"),
    ),
    (
        "summary.deviation_signal_count",
        get_path(canonical, "summary", "deviation_signal_count"),
        get_path(legacy, "summary", "deviation_signal_count"),
    ),
    (
        "bridge_publication.projection_source",
        get_path(canonical, "bridge_publication", "projection_source"),
        get_path(legacy, "bridge_publication", "projection_source"),
    ),
    (
        "recent_execution_truth.first_execution_id",
        ((canonical.get("recent_execution_truth") or [{}])[0] if isinstance(canonical.get("recent_execution_truth"), list) and canonical.get("recent_execution_truth") else {}).get("execution_id"),
        ((legacy.get("recent_execution_truth") or [{}])[0] if isinstance(legacy.get("recent_execution_truth"), list) and legacy.get("recent_execution_truth") else {}).get("execution_id"),
    ),
]

all_ok = True
print("EXECUTION_TRUTH_ALIAS_SYNC: PASS" if all(c == l for _, c, l in checks) else "EXECUTION_TRUTH_ALIAS_SYNC: FAIL")
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