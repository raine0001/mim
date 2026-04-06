#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
BASE_URL="${MIM_TEST_BASE_URL:-http://127.0.0.1:18001}"
SEED_A="${SEED_A:-20260317}"
SEED_B="${SEED_B:-20260318}"
REPORT_DIR="${REPORT_DIR:-$ROOT_DIR/runtime/reports}"
SCENARIOS="${SCENARIOS:-$ROOT_DIR/conversation_scenarios/focused_failure_tag_scenarios.json}"
PROFILES_A="${PROFILES_A:-$ROOT_DIR/conversation_profiles.json}"
PROFILES_B="${PROFILES_B:-$ROOT_DIR/conversation_profiles.json}"

mkdir -p "$REPORT_DIR"

A_REPORT="$REPORT_DIR/conversation_targeted_ab_A.json"
B_REPORT="$REPORT_DIR/conversation_targeted_ab_B.json"
DIFF_REPORT="$REPORT_DIR/conversation_targeted_ab_diff.json"

"$PYTHON_BIN" "$ROOT_DIR/conversation_eval_runner.py" \
  --base-url "$BASE_URL" \
  --scenarios "$SCENARIOS" \
  --profiles "$PROFILES_A" \
  --seed "$SEED_A" \
  --randomize \
  --turn-delay-ms 0 \
  --target-conversations 300 \
  --output "$A_REPORT"

"$PYTHON_BIN" "$ROOT_DIR/conversation_eval_runner.py" \
  --base-url "$BASE_URL" \
  --scenarios "$SCENARIOS" \
  --profiles "$PROFILES_B" \
  --seed "$SEED_B" \
  --randomize \
  --turn-delay-ms 0 \
  --target-conversations 300 \
  --output "$B_REPORT"

"$PYTHON_BIN" "$ROOT_DIR/scripts/compare_conversation_reports.py" \
  --a "$A_REPORT" \
  --b "$B_REPORT" > "$DIFF_REPORT"

echo "[conversation-targeted-ab] complete"
echo "  A report:   $A_REPORT"
echo "  B report:   $B_REPORT"
echo "  diff report:$DIFF_REPORT"
