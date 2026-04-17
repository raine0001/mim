#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
BASE_URL="${MIM_TEST_BASE_URL:-http://127.0.0.1:18001}"
REPORT_DIR="${REPORT_DIR:-$ROOT_DIR/runtime/reports}"
HISTORY_DIR="${HISTORY_DIR:-$REPORT_DIR/mim_evolution_history}"
DURATION_SECONDS="${DURATION_SECONDS:-3600}"
TARGET_CONVERSATIONS="${TARGET_CONVERSATIONS:-320}"
SEED_BASE="${SEED_BASE:-20260317}"
WINDOW_POINTS="${WINDOW_POINTS:-30}"
MAX_OVERALL_DROP="${MAX_OVERALL_DROP:-0.01}"
MIN_ACTION_PASS_RATIO="${MIN_ACTION_PASS_RATIO:-0.95}"
MAX_TAG_RATE_INCREASE="${MAX_TAG_RATE_INCREASE:-0.05}"
MAX_TAG_RATE_RATIO="${MAX_TAG_RATE_RATIO:-1.5}"
WATCH_TAGS="${WATCH_TAGS:-low_relevance,response_loop_risk,missing_safety_boundary,repeated_clarifier_pattern,context_drift,clarification_spam}"

mkdir -p "$REPORT_DIR" "$HISTORY_DIR"

start_epoch="$(date +%s)"
end_epoch="$((start_epoch + DURATION_SECONDS))"
cycle=0
run_stamp_root="$(date -u +%Y%m%dT%H%M%SZ)"
SESSION_HISTORY_DIR="${SESSION_HISTORY_DIR:-$HISTORY_DIR/session_${run_stamp_root}}"

mkdir -p "$SESSION_HISTORY_DIR"

printf '[mim-evolution-soak] start duration=%ss base_url=%s target_conversations=%s\n' "$DURATION_SECONDS" "$BASE_URL" "$TARGET_CONVERSATIONS"
printf '[mim-evolution-soak] session_history=%s\n' "$SESSION_HISTORY_DIR"

while [[ "$(date +%s)" -lt "$end_epoch" ]]; do
  cycle="$((cycle + 1))"
  now_epoch="$(date +%s)"
  run_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  seed="$((SEED_BASE + cycle))"

  echo "[mim-evolution-soak] cycle=$cycle seed=$seed stamp=$run_stamp"

  set +e
  MIM_TEST_BASE_URL="$BASE_URL" \
  TARGET_CONVERSATIONS="$TARGET_CONVERSATIONS" \
  SEED="$seed" \
  "$ROOT_DIR/scripts/run_mim_evolution_simulations.sh" > "$REPORT_DIR/mim_evolution_cycle_${cycle}.log" 2>&1
  run_exit=$?
  set -e

  if [[ -f "$REPORT_DIR/mim_evolution_training_summary.json" ]]; then
    cp "$REPORT_DIR/mim_evolution_training_summary.json" "$SESSION_HISTORY_DIR/mim_evolution_training_summary_${run_stamp}_c${cycle}.json"
    cp "$REPORT_DIR/mim_evolution_training_summary.json" "$HISTORY_DIR/mim_evolution_training_summary_${run_stamp}_c${cycle}.json"
  fi

  if [[ $run_exit -ne 0 ]]; then
    echo "[mim-evolution-soak] cycle=$cycle exit=$run_exit (continuing)"
  fi

  if [[ "$(date +%s)" -ge "$end_epoch" ]]; then
    break
  fi

done

trend_cmd=(
  "$PYTHON_BIN" "$ROOT_DIR/scripts/trend_mim_evolution_reports.py"
  --history-dir "$SESSION_HISTORY_DIR"
  --output "$REPORT_DIR/mim_evolution_trend_report.json"
  --window-points "$WINDOW_POINTS"
  --max-overall-drop "$MAX_OVERALL_DROP"
  --min-action-pass-ratio "$MIN_ACTION_PASS_RATIO"
  --max-tag-rate-increase "$MAX_TAG_RATE_INCREASE"
  --max-tag-rate-ratio "$MAX_TAG_RATE_RATIO"
  --fail-on-regression
)

IFS=',' read -r -a watch_tags_array <<< "$WATCH_TAGS"
for watch_tag in "${watch_tags_array[@]}"; do
  trimmed_tag="$(echo "$watch_tag" | xargs)"
  if [[ -n "$trimmed_tag" ]]; then
    trend_cmd+=(--watch-tag "$trimmed_tag")
  fi
done

"${trend_cmd[@]}"

elapsed="$(( $(date +%s) - start_epoch ))"

"$PYTHON_BIN" - <<'PY' "$SESSION_HISTORY_DIR" "$REPORT_DIR/mim_evolution_trend_report.json" "$elapsed"
import json
import sys
from pathlib import Path

history_dir = Path(sys.argv[1])
trend_path = Path(sys.argv[2])
elapsed = int(sys.argv[3])

items = sorted(history_dir.glob('mim_evolution_training_summary_*.json'))
scenarios = 0
for item in items:
    try:
        payload = json.loads(item.read_text(encoding='utf-8'))
        conv = payload.get('conversation', {}) if isinstance(payload.get('conversation'), dict) else {}
        scenarios += int(conv.get('scenario_count', 0) or 0)
    except Exception:
        pass

trend = {}
if trend_path.exists():
    try:
        trend = json.loads(trend_path.read_text(encoding='utf-8'))
    except Exception:
        trend = {}

summary = {
    'elapsed_seconds': elapsed,
    'runs': len(items),
    'total_scenarios': scenarios,
    'avg_scenarios_per_run': round((scenarios / len(items)), 2) if items else 0.0,
    'scenarios_per_second': round((scenarios / elapsed), 4) if elapsed > 0 else 0.0,
    'trend_report': str(trend_path),
    'session_history_dir': str(history_dir),
    'latest_overall': (trend.get('latest', {}) if isinstance(trend, dict) else {}).get('overall', 0.0),
    'alert_status': (trend.get('alerts', {}) if isinstance(trend, dict) else {}).get('status', 'unknown'),
    'alert_violations': len((trend.get('alerts', {}) if isinstance(trend, dict) else {}).get('violations', [])),
    'hour_window_comparison': (trend.get('hour_window_comparison', {}) if isinstance(trend, dict) else {}),
}
print(json.dumps(summary, indent=2))
Path(trend_path.parent / 'mim_evolution_soak_summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
PY

printf '[mim-evolution-soak] complete history=%s\n' "$HISTORY_DIR"
