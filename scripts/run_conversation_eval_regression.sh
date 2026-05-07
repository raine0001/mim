#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
BASE_URL="${MIM_TEST_BASE_URL:-http://127.0.0.1:18001}"
SEED="${SEED:-20260317}"
REPORT_DIR="${REPORT_DIR:-$ROOT_DIR/runtime/reports}"
BASELINE_PATH="${BASELINE_PATH:-$REPORT_DIR/conversation_baseline.json}"

mkdir -p "$REPORT_DIR"

run_stage() {
  local stage="$1"
  local output_path="$2"
  shift 2
  echo "[conversation-eval] stage=$stage output=$output_path"
  "$PYTHON_BIN" "$ROOT_DIR/conversation_eval_runner.py" \
    --base-url "$BASE_URL" \
    --stage "$stage" \
    --seed "$SEED" \
    --randomize \
    --turn-delay-ms 0 \
    --output "$output_path" \
    "$@"
}

run_stage smoke "$REPORT_DIR/conversation_score_report.smoke.json"
run_stage expanded "$REPORT_DIR/conversation_score_report.expanded.json"
run_stage stress "$REPORT_DIR/conversation_score_report.stress.json"

if [[ ! -f "$BASELINE_PATH" ]]; then
  echo "[conversation-eval] baseline missing; writing baseline from regression stage"
  run_stage regression "$REPORT_DIR/conversation_score_report.regression.json" --write-baseline "$BASELINE_PATH"
else
  echo "[conversation-eval] baseline exists; enforcing regression gate"
  run_stage regression "$REPORT_DIR/conversation_score_report.regression.json" --compare-baseline "$BASELINE_PATH"
fi

echo "[conversation-eval] completed"
echo "  smoke:     $REPORT_DIR/conversation_score_report.smoke.json"
echo "  expanded:  $REPORT_DIR/conversation_score_report.expanded.json"
echo "  stress:    $REPORT_DIR/conversation_score_report.stress.json"
echo "  regression:$REPORT_DIR/conversation_score_report.regression.json"
echo "  baseline:  $BASELINE_PATH"
