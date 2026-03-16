#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="${LOG_FILE:-$ROOT_DIR/runtime/logs/objective75_overnight.log}"
STATE_FILE="${STATE_FILE:-$ROOT_DIR/runtime/logs/objective75_overnight_state.env}"
RECOVERY_JSON="${RECOVERY_JSON:-$ROOT_DIR/runtime/logs/objective75_listener_recovery.latest.json}"
WATCHDOG_JSON="${WATCHDOG_JSON:-$ROOT_DIR/runtime/logs/objective75_stale_ack_watchdog.latest.json}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/runtime/logs}"
LATEST_JSON="${LATEST_JSON:-$OUTPUT_DIR/objective75_nightly_summary.latest.json}"
LATEST_MD="${LATEST_MD:-$OUTPUT_DIR/objective75_nightly_summary.latest.md}"
JSONL_OUT="${JSONL_OUT:-$OUTPUT_DIR/objective75_nightly_summary.jsonl}"
APPEND_JSONL="${APPEND_JSONL:-1}"
REFRESH_RECOVERY_CHECK="${REFRESH_RECOVERY_CHECK:-1}"

mkdir -p "$OUTPUT_DIR"

if [[ "$REFRESH_RECOVERY_CHECK" == "1" || "$REFRESH_RECOVERY_CHECK" == "true" || "$REFRESH_RECOVERY_CHECK" == "yes" ]]; then
  bash "$ROOT_DIR/scripts/check_objective75_listener_recovery.sh" >/dev/null 2>&1 || true
fi

python3 - <<'PY' "$LOG_FILE" "$STATE_FILE" "$RECOVERY_JSON" "$WATCHDOG_JSON" "$LATEST_JSON" "$LATEST_MD" "$JSONL_OUT" "$APPEND_JSONL"
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

log_file = Path(sys.argv[1])
state_file = Path(sys.argv[2])
recovery_json = Path(sys.argv[3])
watchdog_json = Path(sys.argv[4])
latest_json = Path(sys.argv[5])
latest_md = Path(sys.argv[6])
jsonl_out = Path(sys.argv[7])
append_jsonl = str(sys.argv[8]).strip().lower() in {"1", "true", "yes"}


def read_json(path: Path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def read_text(path: Path):
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def parse_task_num_from_state(text: str) -> int:
    m = re.search(r"^TASK_NUM=(\d+)\s*$", text, flags=re.MULTILINE)
    return int(m.group(1)) if m else 0


log_text = read_text(log_file)
log_lines = log_text.splitlines()

cycle_pass_lines = [line for line in log_lines if "Cycle PASS; next TASK_NUM=" in line]
cycle_fail_lines = [line for line in log_lines if "Cycle FAIL;" in line]
guardrail_lines = [line for line in log_lines if "GUARDRAIL STOP" in line]
stale_ack_fail_lines = [line for line in log_lines if "Cycle FAIL; stale trigger ACK" in line]

last_cycle_line = ""
for line in reversed(log_lines):
    if "Cycle PASS; next TASK_NUM=" in line or "Cycle FAIL;" in line or "GUARDRAIL STOP" in line:
        last_cycle_line = line
        break

state_text = read_text(state_file)
task_num = parse_task_num_from_state(state_text)

recovery = read_json(recovery_json)
watchdog = read_json(watchdog_json)

now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

summary = {
    "generated_at": now,
    "task_num": task_num,
    "log_file": str(log_file),
    "counts": {
        "cycle_pass_total": len(cycle_pass_lines),
        "cycle_fail_total": len(cycle_fail_lines),
        "guardrail_stop_total": len(guardrail_lines),
        "stale_ack_fail_total": len(stale_ack_fail_lines),
    },
    "latest": {
        "cycle_line": last_cycle_line,
        "last_pass_line": cycle_pass_lines[-1] if cycle_pass_lines else "",
        "last_fail_line": cycle_fail_lines[-1] if cycle_fail_lines else "",
        "last_guardrail_line": guardrail_lines[-1] if guardrail_lines else "",
        "last_stale_ack_fail_line": stale_ack_fail_lines[-1] if stale_ack_fail_lines else "",
    },
    "listener_recovery": {
        "status": recovery.get("status"),
        "checks_passed": recovery.get("checks_passed"),
        "checks_total": recovery.get("checks_total"),
        "latest_cycle_line": recovery.get("latest_cycle_line"),
        "current_task_id": recovery.get("current_task_id"),
    },
    "stale_ack_watchdog": {
        "status": watchdog.get("status"),
        "reason": watchdog.get("reason"),
        "consecutive_stale_failures": watchdog.get("consecutive_stale_failures"),
        "threshold": watchdog.get("threshold"),
        "latest_cycle_line": watchdog.get("latest_cycle_line"),
    },
}

latest_json.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

md_lines = [
    "# Objective 75 Nightly Summary",
    "",
    f"- generated_at: {summary['generated_at']}",
    f"- task_num: {summary['task_num']}",
    "",
    "## Counts",
    "",
    f"- cycle_pass_total: {summary['counts']['cycle_pass_total']}",
    f"- cycle_fail_total: {summary['counts']['cycle_fail_total']}",
    f"- guardrail_stop_total: {summary['counts']['guardrail_stop_total']}",
    f"- stale_ack_fail_total: {summary['counts']['stale_ack_fail_total']}",
    "",
    "## Latest",
    "",
    f"- cycle_line: {summary['latest']['cycle_line'] or '<none>'}",
    f"- last_pass_line: {summary['latest']['last_pass_line'] or '<none>'}",
    f"- last_fail_line: {summary['latest']['last_fail_line'] or '<none>'}",
    f"- last_guardrail_line: {summary['latest']['last_guardrail_line'] or '<none>'}",
    f"- last_stale_ack_fail_line: {summary['latest']['last_stale_ack_fail_line'] or '<none>'}",
    "",
    "## Listener Recovery",
    "",
    f"- status: {summary['listener_recovery']['status']}",
    f"- checks: {summary['listener_recovery']['checks_passed']}/{summary['listener_recovery']['checks_total']}",
    f"- current_task_id: {summary['listener_recovery']['current_task_id']}",
    "",
    "## Stale-ACK Watchdog",
    "",
    f"- status: {summary['stale_ack_watchdog']['status']}",
    f"- reason: {summary['stale_ack_watchdog']['reason']}",
    f"- consecutive_stale_failures: {summary['stale_ack_watchdog']['consecutive_stale_failures']}",
    f"- threshold: {summary['stale_ack_watchdog']['threshold']}",
]
latest_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

if append_jsonl:
    with jsonl_out.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, separators=(",", ":")) + "\n")

print(f"SUMMARY_JSON: {latest_json}")
print(f"SUMMARY_MD: {latest_md}")
if append_jsonl:
    print(f"SUMMARY_JSONL: {jsonl_out}")
print(f"TASK_NUM: {task_num}")
print(f"LATEST_CYCLE: {summary['latest']['cycle_line'] or '<none>'}")
PY
