#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-$ROOT_DIR/runtime/shared}"
TRUTH_FILE="${1:-$SHARED_DIR/TOD_EXECUTION_TRUTH.latest.json}"
MAX_AGE_SECONDS="${MAX_AGE_SECONDS:-900}"

if [[ ! -f "$TRUTH_FILE" ]]; then
  echo "EXECUTION_TRUTH_GATE: FAIL"
  echo "- missing file: $TRUTH_FILE"
  exit 1
fi

python3 - "$TRUTH_FILE" "$MAX_AGE_SECONDS" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

truth_path = Path(sys.argv[1])
max_age_seconds = int(sys.argv[2])

payload = json.loads(truth_path.read_text(encoding="utf-8-sig"))


def parse_iso(ts: str):
    if not ts:
        return None
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


generated_at = str(payload.get("generated_at", ""))
generated_dt = parse_iso(generated_at)
now = datetime.now(timezone.utc)
age_seconds = int((now - generated_dt).total_seconds()) if generated_dt else 10**9

summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
recent = payload.get("recent_execution_truth") if isinstance(payload.get("recent_execution_truth"), list) else []
first = recent[0] if recent else {}
first_truth = first.get("execution_truth") if isinstance(first.get("execution_truth"), dict) else {}

checks = [
    ("packet_type == tod-execution-truth-bridge-v1", str(payload.get("packet_type", "")) == "tod-execution-truth-bridge-v1", payload.get("packet_type")),
    ("contract == execution_truth_v1", str(payload.get("contract", "")) == "execution_truth_v1", payload.get("contract")),
    ("generated_at present", bool(generated_dt), generated_at),
    ("generated_at fresh", age_seconds <= max_age_seconds, age_seconds),
    ("summary.execution_count present", isinstance(summary.get("execution_count"), int), summary.get("execution_count")),
    ("summary.deviation_signal_count present", isinstance(summary.get("deviation_signal_count"), int), summary.get("deviation_signal_count")),
]

if recent:
    checks.extend([
        ("recent execution_id present", isinstance(first.get("execution_id"), int), first.get("execution_id")),
        ("recent capability_name present", bool(str(first.get("capability_name", "")).strip()), first.get("capability_name")),
        ("recent execution_truth contract present", str(first_truth.get("contract", "")) == "execution_truth_v1", first_truth.get("contract")),
        ("recent execution_truth published_at present", bool(parse_iso(str(first_truth.get("published_at", "")))), first_truth.get("published_at")),
    ])

all_ok = all(ok for _, ok, _ in checks)
print(f"EXECUTION_TRUTH_GATE: {'PASS' if all_ok else 'FAIL'}")
for name, ok, value in checks:
    state = "PASS" if ok else "FAIL"
    print(f"- {state}: {name} (value={value!r})")

sys.exit(0 if all_ok else 1)
PY