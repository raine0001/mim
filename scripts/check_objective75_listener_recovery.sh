#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-$ROOT_DIR/runtime/shared}"
LOG_FILE="${LOG_FILE:-$ROOT_DIR/runtime/logs/objective75_overnight.log}"
MAX_AGE_SECONDS="${MAX_AGE_SECONDS:-1800}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/runtime/logs}"
JSON_OUT="${JSON_OUT:-$OUTPUT_DIR/objective75_listener_recovery.latest.json}"
MD_OUT="${MD_OUT:-$OUTPUT_DIR/objective75_listener_recovery.latest.md}"
JSONL_OUT="${JSONL_OUT:-$OUTPUT_DIR/objective75_listener_recovery.jsonl}"
APPEND_JSONL="${APPEND_JSONL:-1}"

mkdir -p "$OUTPUT_DIR"

python3 - <<'PY' "$SHARED_DIR" "$LOG_FILE" "$MAX_AGE_SECONDS" "$JSON_OUT" "$MD_OUT" "$JSONL_OUT" "$APPEND_JSONL"
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

shared_dir = Path(sys.argv[1])
log_file = Path(sys.argv[2])
max_age_seconds = int(sys.argv[3])
json_out = Path(sys.argv[4])
md_out = Path(sys.argv[5])
jsonl_out = Path(sys.argv[6])
append_jsonl = str(sys.argv[7]).strip().lower() in {"1", "true", "yes"}

request_path = shared_dir / "MIM_TOD_TASK_REQUEST.latest.json"
trigger_ack_path = shared_dir / "TOD_TO_MIM_TRIGGER_ACK.latest.json"
task_ack_path = shared_dir / "TOD_MIM_TASK_ACK.latest.json"
result_path = shared_dir / "TOD_MIM_TASK_RESULT.latest.json"


def read_json(path: Path):
    if not path.exists():
        return None, f"missing file: {path}"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), ""
    except Exception as exc:
        return None, f"read error {path}: {exc}"


def parse_ts(ts_value: str | None):
    if not ts_value:
        return None
    value = str(ts_value).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        return None


checks: list[tuple[str, bool, str]] = []
latest_cycle = ""

request, request_err = read_json(request_path)
trigger_ack, trigger_ack_err = read_json(trigger_ack_path)
task_ack, task_ack_err = read_json(task_ack_path)
result, result_err = read_json(result_path)

if request_err:
    checks.append(("current_task_present", False, request_err))
    current_task = ""
else:
    current_task = str(request.get("task_id") or "").strip()
    checks.append(("current_task_present", bool(current_task), f"task_id={current_task or '<empty>'}"))

if trigger_ack_err:
    checks.append(("trigger_ack_file", False, trigger_ack_err))
else:
    trigger_status = str(trigger_ack.get("status") or "").strip().lower()
    trigger_task = str(trigger_ack.get("acknowledges") or "").strip()
    checks.append((
        "trigger_ack_status_runtime",
        trigger_status not in {"", "ready_template"},
        f"status={trigger_status or '<empty>'}",
    ))
    checks.append((
        "trigger_ack_matches_task",
        bool(current_task) and trigger_task == current_task,
        f"acknowledges={trigger_task or '<empty>'} expected={current_task or '<empty>'}",
    ))
    trigger_dt = parse_ts(trigger_ack.get("generated_at"))
    if trigger_dt is None:
        checks.append(("trigger_ack_fresh", False, f"generated_at={trigger_ack.get('generated_at')!r}"))
    else:
        age = int((datetime.now(timezone.utc) - trigger_dt).total_seconds())
        checks.append(("trigger_ack_fresh", age <= max_age_seconds, f"age_seconds={age}"))

if task_ack_err:
    checks.append(("task_ack_file", False, task_ack_err))
else:
    task_ack_status = str(task_ack.get("status") or "").strip().lower()
    task_ack_request_id = str(task_ack.get("request_id") or "").strip()
    checks.append((
        "task_ack_status_accepted",
        task_ack_status == "accepted",
        f"status={task_ack_status or '<empty>'}",
    ))
    checks.append((
        "task_ack_matches_task",
        bool(current_task) and task_ack_request_id == current_task,
        f"request_id={task_ack_request_id or '<empty>'} expected={current_task or '<empty>'}",
    ))
    task_ack_dt = parse_ts(task_ack.get("generated_at"))
    if task_ack_dt is None:
        checks.append(("task_ack_fresh", False, f"generated_at={task_ack.get('generated_at')!r}"))
    else:
        age = int((datetime.now(timezone.utc) - task_ack_dt).total_seconds())
        checks.append(("task_ack_fresh", age <= max_age_seconds, f"age_seconds={age}"))

if result_err:
    checks.append(("task_result_file", False, result_err))
else:
    result_status = str(result.get("status") or "").strip().lower()
    result_request_id = str(result.get("request_id") or "").strip()
    checks.append((
        "task_result_status_completed",
        result_status == "completed",
        f"status={result_status or '<empty>'}",
    ))
    checks.append((
        "task_result_matches_task",
        bool(current_task) and result_request_id == current_task,
        f"request_id={result_request_id or '<empty>'} expected={current_task or '<empty>'}",
    ))

if log_file.exists():
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    cycle_lines = [line for line in lines if "Cycle PASS; next TASK_NUM=" in line or "Cycle FAIL;" in line or "GUARDRAIL STOP" in line]
    latest_cycle = cycle_lines[-1] if cycle_lines else "(no cycle lines)"
    checks.append((
        "latest_cycle_not_guardrail_stop",
        "GUARDRAIL STOP" not in latest_cycle,
        latest_cycle,
    ))
else:
    checks.append(("overnight_log_present", False, f"missing file: {log_file}"))

passed = 0
check_payload = []
for name, ok, detail in checks:
    state = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    check_payload.append({"name": name, "passed": ok, "detail": detail})
    print(f"{state}: {name} :: {detail}")

total = len(checks)
summary_status = "PASS" if passed == total else "FAIL"
summary_line = f"SUMMARY: {summary_status} ({passed}/{total})"
print(summary_line)

report = {
    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "status": summary_status.lower(),
    "checks_passed": passed,
    "checks_total": total,
    "max_age_seconds": max_age_seconds,
    "current_task_id": current_task,
    "latest_cycle_line": latest_cycle,
    "paths": {
        "request": str(request_path),
        "trigger_ack": str(trigger_ack_path),
        "task_ack": str(task_ack_path),
        "task_result": str(result_path),
        "overnight_log": str(log_file),
    },
    "checks": check_payload,
}

json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

md_lines = [
    "# Objective 75 Listener Recovery Status",
    "",
    f"- generated_at: {report['generated_at']}",
    f"- status: {report['status']}",
    f"- checks: {passed}/{total}",
    f"- current_task_id: {current_task or '<empty>'}",
    f"- latest_cycle: {latest_cycle or '(none)'}",
    "",
    "## Checks",
    "",
]
for item in check_payload:
    mark = "PASS" if item["passed"] else "FAIL"
    md_lines.append(f"- {mark}: {item['name']} — {item['detail']}")
md_out.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

if append_jsonl:
    with jsonl_out.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, separators=(",", ":")) + "\n")

print(f"ARTIFACT_JSON: {json_out}")
print(f"ARTIFACT_MD: {md_out}")
if append_jsonl:
    print(f"ARTIFACT_JSONL: {jsonl_out}")

raise SystemExit(0 if summary_status == "PASS" else 1)
PY
