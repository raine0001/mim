#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="${LOG_FILE:-$ROOT_DIR/runtime/logs/objective75_overnight.log}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/runtime/logs}"
ALERT_JSON="${ALERT_JSON:-$OUTPUT_DIR/objective75_stale_ack_watchdog.latest.json}"
ALERT_MD="${ALERT_MD:-$OUTPUT_DIR/objective75_stale_ack_watchdog.latest.md}"
ALERT_JSONL="${ALERT_JSONL:-$OUTPUT_DIR/objective75_stale_ack_watchdog.jsonl}"
MAX_CONSEC_STALE_FAILS="${MAX_CONSEC_STALE_FAILS:-2}"
POLL_SECONDS="${POLL_SECONDS:-10}"
MAX_WATCH_SECONDS="${MAX_WATCH_SECONDS:-0}"
APPEND_JSONL="${APPEND_JSONL:-1}"

mkdir -p "$OUTPUT_DIR"

if [[ ! -f "$LOG_FILE" ]]; then
  echo "WATCHDOG: FAIL"
  echo "- missing log file: $LOG_FILE"
  exit 1
fi

python3 - <<'PY' "$LOG_FILE" "$ALERT_JSON" "$ALERT_MD" "$ALERT_JSONL" "$MAX_CONSEC_STALE_FAILS" "$POLL_SECONDS" "$MAX_WATCH_SECONDS" "$APPEND_JSONL"
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

log_file = Path(sys.argv[1])
alert_json = Path(sys.argv[2])
alert_md = Path(sys.argv[3])
alert_jsonl = Path(sys.argv[4])
threshold = max(1, int(sys.argv[5]))
poll_seconds = max(1, int(sys.argv[6]))
max_watch_seconds = max(0, int(sys.argv[7]))
append_jsonl = str(sys.argv[8]).strip().lower() in {"1", "true", "yes"}

stale_pattern = re.compile(r"Cycle FAIL; stale trigger ACK for TASK_NUM=(\d+)")
cycle_pattern = re.compile(r"Cycle (PASS|FAIL);")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_alert(status: str, reason: str, consecutive_stale: int, latest_line: str, task_num: str, started_at: str):
    payload = {
        "generated_at": now_iso(),
        "status": status,
        "reason": reason,
        "consecutive_stale_failures": consecutive_stale,
        "threshold": threshold,
        "latest_cycle_line": latest_line,
        "task_num": task_num,
        "started_at": started_at,
        "log_file": str(log_file),
    }
    alert_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    md_lines = [
        "# Objective 75 Stale ACK Watchdog",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- status: {status}",
        f"- reason: {reason}",
        f"- consecutive_stale_failures: {consecutive_stale}",
        f"- threshold: {threshold}",
        f"- task_num: {task_num or '<unknown>'}",
        f"- latest_cycle_line: {latest_line or '<none>'}",
        f"- log_file: {log_file}",
    ]
    alert_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    if append_jsonl:
        with alert_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")


try:
    total_lines = len(log_file.read_text(encoding="utf-8", errors="replace").splitlines())
except Exception as exc:
    print("WATCHDOG: FAIL")
    print(f"- unable to read log: {exc}")
    raise SystemExit(1)

start_time = time.time()
started_at = now_iso()
offset = total_lines
consecutive_stale = 0
last_cycle_line = ""
last_task = ""

print(f"WATCHDOG: START threshold={threshold} poll_seconds={poll_seconds} max_watch_seconds={max_watch_seconds}")
print(f"WATCHDOG: tailing {log_file}")

while True:
    if max_watch_seconds > 0 and (time.time() - start_time) >= max_watch_seconds:
        write_alert(
            status="ok",
            reason="watch_timeout_without_alert",
            consecutive_stale=consecutive_stale,
            latest_line=last_cycle_line,
            task_num=last_task,
            started_at=started_at,
        )
        print("WATCHDOG: OK timeout reached without repeated stale-ACK failures")
        print(f"ARTIFACT_JSON: {alert_json}")
        print(f"ARTIFACT_MD: {alert_md}")
        if append_jsonl:
            print(f"ARTIFACT_JSONL: {alert_jsonl}")
        raise SystemExit(0)

    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    if offset > len(lines):
        offset = len(lines)
    new_lines = lines[offset:]
    offset = len(lines)

    for line in new_lines:
        if not cycle_pattern.search(line) and "GUARDRAIL STOP" not in line:
            continue

        last_cycle_line = line

        stale_match = stale_pattern.search(line)
        if stale_match:
            last_task = stale_match.group(1)
            consecutive_stale += 1
            print(f"WATCHDOG: stale_ack_fail_consecutive={consecutive_stale} task={last_task}")
            if consecutive_stale >= threshold:
                write_alert(
                    status="alert",
                    reason="consecutive_stale_trigger_ack_failures",
                    consecutive_stale=consecutive_stale,
                    latest_line=line,
                    task_num=last_task,
                    started_at=started_at,
                )
                print("WATCHDOG: ALERT consecutive stale-ACK failure threshold reached")
                print(f"ARTIFACT_JSON: {alert_json}")
                print(f"ARTIFACT_MD: {alert_md}")
                if append_jsonl:
                    print(f"ARTIFACT_JSONL: {alert_jsonl}")
                raise SystemExit(2)
            continue

        if "Cycle PASS;" in line or "Cycle FAIL;" in line:
            if consecutive_stale > 0:
                print("WATCHDOG: stale_ack_consecutive counter reset")
            consecutive_stale = 0

    time.sleep(poll_seconds)
PY
