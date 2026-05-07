#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/runtime/logs}"
FILE_GLOB="${FILE_GLOB:-objective75_*.jsonl}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
DRY_RUN="${DRY_RUN:-0}"
OUTPUT_JSON="${OUTPUT_JSON:-$LOG_DIR/objective75_jsonl_retention.latest.json}"
OUTPUT_MD="${OUTPUT_MD:-$LOG_DIR/objective75_jsonl_retention.latest.md}"
RUNS_JSONL="${RUNS_JSONL:-$LOG_DIR/objective75_jsonl_retention_runs.jsonl}"
APPEND_RUN_JSONL="${APPEND_RUN_JSONL:-1}"

mkdir -p "$LOG_DIR"

python3 - <<'PY' "$LOG_DIR" "$FILE_GLOB" "$RETENTION_DAYS" "$DRY_RUN" "$OUTPUT_JSON" "$OUTPUT_MD" "$RUNS_JSONL" "$APPEND_RUN_JSONL"
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

log_dir = Path(sys.argv[1])
file_glob = sys.argv[2]
retention_days = max(1, int(sys.argv[3]))
dry_run = str(sys.argv[4]).strip().lower() in {"1", "true", "yes"}
output_json = Path(sys.argv[5])
output_md = Path(sys.argv[6])
runs_jsonl = Path(sys.argv[7])
append_runs_jsonl = str(sys.argv[8]).strip().lower() in {"1", "true", "yes"}

now = datetime.now(timezone.utc)
cutoff = now - timedelta(days=retention_days)


def parse_ts(value):
    if not value:
        return None
    if not isinstance(value, str):
        return None
    txt = value.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(txt).astimezone(timezone.utc)
    except Exception:
        return None


def extract_ts(record):
    if not isinstance(record, dict):
        return None
    for key in ("generated_at", "timestamp", "generatedAt"):
        ts = parse_ts(record.get(key))
        if ts is not None:
            return ts
    return None


files = [Path(p) for p in glob.glob(str(log_dir / file_glob)) if Path(p).is_file()]
files.sort()

total_before = 0
total_after = 0
total_pruned = 0
per_file = []

for path in files:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    kept = []
    before = len(lines)
    pruned = 0
    invalid_json = 0
    no_timestamp = 0

    for raw in lines:
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            invalid_json += 1
            kept.append(raw)
            continue

        ts = extract_ts(payload)
        if ts is None:
            no_timestamp += 1
            kept.append(raw)
            continue

        if ts < cutoff:
            pruned += 1
        else:
            kept.append(raw)

    after = len(kept)
    total_before += before
    total_after += after
    total_pruned += pruned

    if not dry_run and before != after:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
        tmp.replace(path)

    per_file.append({
        "file": str(path),
        "before": before,
        "after": after,
        "pruned": pruned,
        "invalid_json_kept": invalid_json,
        "no_timestamp_kept": no_timestamp,
    })

report = {
    "generated_at": now.isoformat().replace("+00:00", "Z"),
    "status": "dry_run" if dry_run else "applied",
    "retention_days": retention_days,
    "cutoff_utc": cutoff.isoformat().replace("+00:00", "Z"),
    "file_glob": file_glob,
    "files_matched": len(files),
    "totals": {
        "before": total_before,
        "after": total_after,
        "pruned": total_pruned,
    },
    "files": per_file,
}

output_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

md_lines = [
    "# Objective 75 JSONL Retention",
    "",
    f"- generated_at: {report['generated_at']}",
    f"- status: {report['status']}",
    f"- retention_days: {retention_days}",
    f"- cutoff_utc: {report['cutoff_utc']}",
    f"- files_matched: {report['files_matched']}",
    f"- totals_before: {total_before}",
    f"- totals_after: {total_after}",
    f"- totals_pruned: {total_pruned}",
    "",
    "## Per File",
    "",
]
for item in per_file:
    md_lines.append(
        f"- {item['file']}: before={item['before']} after={item['after']} pruned={item['pruned']} invalid_json_kept={item['invalid_json_kept']} no_timestamp_kept={item['no_timestamp_kept']}"
    )
output_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

if append_runs_jsonl:
    with runs_jsonl.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, separators=(",", ":")) + "\n")

print(f"RETENTION_JSON: {output_json}")
print(f"RETENTION_MD: {output_md}")
if append_runs_jsonl:
    print(f"RETENTION_RUNS_JSONL: {runs_jsonl}")
print(f"FILES_MATCHED: {len(files)}")
print(f"PRUNED: {total_pruned}")
PY
