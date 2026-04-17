#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
BASE_URL="${MIM_TEST_BASE_URL:-http://127.0.0.1:18001}"
REPORT_DIR="${REPORT_DIR:-$ROOT_DIR/runtime/reports}"
SEED="${SEED:-20260317}"
TARGET_CONVERSATIONS="${TARGET_CONVERSATIONS:-320}"

SCENARIOS="${SCENARIOS:-$ROOT_DIR/conversation_scenarios/mim_evolution_training_set.json}"
PROFILES="${PROFILES:-$ROOT_DIR/conversation_profiles_evolution.json}"
CONVERSATION_REPORT="$REPORT_DIR/mim_evolution_conversation_report.json"
ACTION_REPORT="$REPORT_DIR/mim_action_simulation_report.json"
SUMMARY_REPORT="$REPORT_DIR/mim_evolution_training_summary.json"

mkdir -p "$REPORT_DIR"

echo "[mim-evolution] running conversation training simulations"
"$PYTHON_BIN" "$ROOT_DIR/conversation_eval_runner.py" \
  --base-url "$BASE_URL" \
  --scenarios "$SCENARIOS" \
  --profiles "$PROFILES" \
  --seed "$SEED" \
  --randomize \
  --turn-delay-ms 0 \
  --target-conversations "$TARGET_CONVERSATIONS" \
  --output "$CONVERSATION_REPORT"

echo "[mim-evolution] running action execution simulations"
set +e
"$PYTHON_BIN" "$ROOT_DIR/scripts/run_mim_action_simulations.py" \
  --base-url "$BASE_URL" \
  --output "$ACTION_REPORT"
ACTION_EXIT=$?
set -e

echo "[mim-evolution] building combined summary"
"$PYTHON_BIN" - <<'PY' "$CONVERSATION_REPORT" "$ACTION_REPORT" "$SUMMARY_REPORT" "$ACTION_EXIT"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

conversation_report = Path(sys.argv[1])
action_report = Path(sys.argv[2])
summary_report = Path(sys.argv[3])
action_exit = int(sys.argv[4])

conv = json.loads(conversation_report.read_text(encoding="utf-8")) if conversation_report.exists() else {}
act = json.loads(action_report.read_text(encoding="utf-8")) if action_report.exists() else {}

conv_summary = conv.get("summary", {}) if isinstance(conv.get("summary"), dict) else {}
act_summary = act.get("summary", {}) if isinstance(act.get("summary"), dict) else {}

top_failures = conv_summary.get("top_failures", []) if isinstance(conv_summary.get("top_failures"), list) else []

summary = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "conversation": {
        "overall": conv_summary.get("overall", 0.0),
        "scenario_count": conv_summary.get("scenario_count", 0),
        "failure_count": conv_summary.get("failure_count", 0),
        "top_failures": top_failures[:8],
        "bucket_average": conv_summary.get("bucket_average", {}),
    },
    "actions": {
        "total": act_summary.get("total", 0),
        "passed": act_summary.get("passed", 0),
        "failed": act_summary.get("failed", 0),
        "pass_ratio": act_summary.get("pass_ratio", 0.0),
        "exit_code": action_exit,
        "runtime_build": act.get("state_runtime_build", ""),
        "runtime_features": act.get("state_runtime_features", []),
    },
    "reports": {
        "conversation": str(conversation_report),
        "actions": str(action_report),
    },
}

summary_report.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"output": str(summary_report), "summary": summary}, indent=2))
PY

echo "[mim-evolution] complete"
echo "  conversation: $CONVERSATION_REPORT"
echo "  actions:      $ACTION_REPORT"
echo "  summary:      $SUMMARY_REPORT"

if [[ "$ACTION_EXIT" -ne 0 ]]; then
  echo "[mim-evolution] note: one or more action simulations failed"
fi
