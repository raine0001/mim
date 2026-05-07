#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
BASE_URL="${MIM_TEST_BASE_URL:-http://127.0.0.1:18001}"
MODE="${1:-pr}"
REPORT_DIR="${REPORT_DIR:-$ROOT_DIR/runtime/reports}"
BASELINE_PATH="${BASELINE_PATH:-$REPORT_DIR/conversation_baseline.json}"

mkdir -p "$REPORT_DIR"

if [[ "$MODE" == "pr" ]]; then
  echo "[conversation-gate] mode=pr (targeted A/B)"
  MIM_TEST_BASE_URL="$BASE_URL" "$ROOT_DIR/scripts/run_conversation_targeted_ab.sh"

  A_REPORT="$REPORT_DIR/conversation_targeted_ab_A.json"
  B_REPORT="$REPORT_DIR/conversation_targeted_ab_B.json"
  DIFF_REPORT="$REPORT_DIR/conversation_targeted_ab_diff.json"
  GATE_REPORT="$REPORT_DIR/conversation_targeted_gate.json"

  "$PYTHON_BIN" "$ROOT_DIR/scripts/compare_conversation_reports.py" --a "$A_REPORT" --b "$B_REPORT" > "$DIFF_REPORT"
  "$PYTHON_BIN" "$ROOT_DIR/scripts/enforce_conversation_regression_gate.py" \
    --mode pr \
    --a "$A_REPORT" \
    --b "$B_REPORT" \
    --output "$GATE_REPORT"

  echo "[conversation-gate] PR gate complete"
  echo "  diff: $DIFF_REPORT"
  echo "  gate: $GATE_REPORT"
  exit 0
fi

if [[ "$MODE" == "nightly" ]]; then
  echo "[conversation-gate] mode=nightly (full regression)"
  MIM_TEST_BASE_URL="$BASE_URL" BASELINE_PATH="$BASELINE_PATH" "$ROOT_DIR/scripts/run_conversation_eval_regression.sh"

  A_REPORT="$BASELINE_PATH"
  B_REPORT="$REPORT_DIR/conversation_score_report.regression.json"
  DIFF_REPORT="$REPORT_DIR/conversation_regression_diff.json"
  GATE_REPORT="$REPORT_DIR/conversation_regression_gate.json"

  "$PYTHON_BIN" "$ROOT_DIR/scripts/compare_conversation_reports.py" --a "$A_REPORT" --b "$B_REPORT" > "$DIFF_REPORT"
  "$PYTHON_BIN" "$ROOT_DIR/scripts/enforce_conversation_regression_gate.py" \
    --mode nightly \
    --a "$A_REPORT" \
    --b "$B_REPORT" \
    --output "$GATE_REPORT"

  echo "[conversation-gate] nightly gate complete"
  echo "  diff: $DIFF_REPORT"
  echo "  gate: $GATE_REPORT"
  exit 0
fi

echo "Usage: scripts/run_conversation_quality_gate.sh [pr|nightly]"
exit 64
