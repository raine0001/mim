#!/usr/bin/env python3

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
RUNTIME_SHARED = ROOT / "runtime" / "shared"
RUNTIME_LOGS = ROOT / "runtime" / "logs"

OUTPUT_MD = DOCS_DIR / "objective-75-lifecycle-status.md"

OBJECTIVE_DOC = DOCS_DIR / "objective-75-mim-tod-interface-hardening.md"
BRIDGE_DOC = DOCS_DIR / "tod-mim-bridge.md"
READINESS_REPORT = DOCS_DIR / "objective-75-promotion-readiness-report.md"
PROD_REPORT = DOCS_DIR / "objective-75-prod-promotion-report.md"

STATE_FILE = RUNTIME_LOGS / "objective75_overnight_state.env"
LOOP_LOG = RUNTIME_LOGS / "objective75_overnight.log"

INTEGRATION_STATUS = RUNTIME_SHARED / "TOD_INTEGRATION_STATUS.latest.json"
MANIFEST = RUNTIME_SHARED / "MIM_MANIFEST.latest.json"
HANDSHAKE = RUNTIME_SHARED / "MIM_TOD_HANDSHAKE_PACKET.latest.json"
ALIGNMENT_REQUEST = RUNTIME_SHARED / "MIM_TOD_ALIGNMENT_REQUEST.latest.json"
CONTEXT_JSON = RUNTIME_SHARED / "MIM_CONTEXT_EXPORT.latest.json"
CONTEXT_YAML = RUNTIME_SHARED / "MIM_CONTEXT_EXPORT.latest.yaml"


VALID_STATUSES = {"not_started", "in_progress", "completed", "blocked", "verified"}
VALID_CONFIDENCE = {"high", "medium", "low"}


@dataclass
class TaskRow:
    phase: str
    task: str
    description: str
    status: str
    evidence: str
    last_update: str
    confidence: str


def read_json(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def file_mtime_iso(path: Path) -> str:
    if not path.exists():
        return "n/a"
    dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def detect_task_num() -> int:
    if not STATE_FILE.exists():
        return 0
    m = re.search(
        r"TASK_NUM=(\d+)", STATE_FILE.read_text(encoding="utf-8", errors="ignore")
    )
    return int(m.group(1)) if m else 0


def count_log(pattern: str) -> int:
    if not LOOP_LOG.exists():
        return 0
    text = LOOP_LOG.read_text(encoding="utf-8", errors="ignore")
    return len(re.findall(pattern, text))


def latest_pass_line() -> str:
    if not LOOP_LOG.exists():
        return "n/a"
    lines = LOOP_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    for line in reversed(lines):
        if "Cycle PASS; next TASK_NUM=" in line:
            return line.strip()
    return "n/a"


def status_counts(rows: list[TaskRow]) -> Dict[str, int]:
    counts = {
        k: 0 for k in ["not_started", "in_progress", "completed", "blocked", "verified"]
    }
    for row in rows:
        counts[row.status] += 1
    return counts


def build_rows() -> list[TaskRow]:
    integration = read_json(INTEGRATION_STATUS)
    manifest = read_json(MANIFEST)
    handshake = read_json(HANDSHAKE)

    mim_refresh = integration.get("mim_refresh", {})
    mim_handshake = integration.get("mim_handshake", {})
    alignment = integration.get("objective_alignment", {})

    required_files_present = CONTEXT_JSON.exists() and CONTEXT_YAML.exists()
    key_fields_ok = bool(
        integration.get("compatible") is True and alignment.get("aligned") is True
    )
    alignment_request_present = ALIGNMENT_REQUEST.exists()
    tod_sync_published = bool(integration.get("generated_at"))
    shared_truth = handshake.get("truth", {})
    shared_manifest = manifest.get("manifest", {})
    expected_schema = shared_truth.get("schema_version") or shared_manifest.get(
        "schema_version"
    )
    expected_release = shared_truth.get("release_tag") or shared_manifest.get(
        "release_tag"
    )
    expected_objective = shared_truth.get("objective_active")
    refresh_failure_empty = str(mim_refresh.get("failure_reason") or "") == ""
    artifact_pull_verified = bool(
        mim_refresh.get("copied_manifest")
        and str(mim_refresh.get("source_manifest") or "").strip()
        and str(mim_refresh.get("source_handshake_packet") or "").strip()
        and mim_handshake.get("available") is True
        and str(mim_handshake.get("objective_active") or "")
        == str(expected_objective or "")
        and str(mim_handshake.get("schema_version") or "") == str(expected_schema or "")
        and str(mim_handshake.get("release_tag") or "") == str(expected_release or "")
        and str(integration.get("mim_schema") or "") == str(expected_schema or "")
        and refresh_failure_empty
    )
    objective_aligned = bool(alignment.get("aligned"))

    readiness_exists = READINESS_REPORT.exists()
    prod_exists = PROD_REPORT.exists()
    gate_core_ok = bool(
        integration.get("compatible") is True
        and objective_aligned
        and artifact_pull_verified
        and refresh_failure_empty
    )

    schema_version = expected_schema
    contract_frozen = bool(schema_version)

    rows: list[TaskRow] = [
        TaskRow(
            phase="Phase 1 — Contract freeze",
            task="1",
            description="publish contract addendum",
            status="verified" if BRIDGE_DOC.exists() else "not_started",
            evidence="contract doc",
            last_update=file_mtime_iso(BRIDGE_DOC),
            confidence="high" if BRIDGE_DOC.exists() else "low",
        ),
        TaskRow(
            phase="Phase 1 — Contract freeze",
            task="2",
            description="define alignment rule",
            status="verified" if key_fields_ok else "in_progress",
            evidence="integration status snapshot",
            last_update=integration.get("generated_at", "n/a"),
            confidence="high" if key_fields_ok else "medium",
        ),
        TaskRow(
            phase="Phase 1 — Contract freeze",
            task="3",
            description="freeze interface version",
            status="completed" if contract_frozen else "in_progress",
            evidence="packet exchange",
            last_update=file_mtime_iso(HANDSHAKE if HANDSHAKE.exists() else MANIFEST),
            confidence="medium" if contract_frozen else "low",
        ),
        TaskRow(
            phase="Phase 2 — MIM producer conformance",
            task="4",
            description="required file presence",
            status="verified" if required_files_present else "blocked",
            evidence="packet exchange",
            last_update=max(file_mtime_iso(CONTEXT_JSON), file_mtime_iso(CONTEXT_YAML)),
            confidence="high" if required_files_present else "high",
        ),
        TaskRow(
            phase="Phase 2 — MIM producer conformance",
            task="5",
            description="key field validation",
            status="verified" if key_fields_ok else "in_progress",
            evidence="log entry",
            last_update=file_mtime_iso(LOOP_LOG),
            confidence="high" if key_fields_ok else "medium",
        ),
        TaskRow(
            phase="Phase 2 — MIM producer conformance",
            task="6",
            description="deterministic alignment request generation",
            status="completed" if alignment_request_present else "in_progress",
            evidence="packet exchange",
            last_update=file_mtime_iso(ALIGNMENT_REQUEST),
            confidence="medium" if alignment_request_present else "low",
        ),
        TaskRow(
            phase="Phase 3 — TOD consumer conformance",
            task="7",
            description="TOD sync and status publish",
            status="verified" if tod_sync_published else "in_progress",
            evidence="integration status snapshot",
            last_update=integration.get("generated_at", "n/a"),
            confidence="high" if tod_sync_published else "medium",
        ),
        TaskRow(
            phase="Phase 3 — TOD consumer conformance",
            task="8",
            description="artifact pull verification",
            status="verified" if artifact_pull_verified else "in_progress",
            evidence="integration status snapshot",
            last_update=integration.get("generated_at", "n/a"),
            confidence="high" if artifact_pull_verified else "medium",
        ),
        TaskRow(
            phase="Phase 3 — TOD consumer conformance",
            task="9",
            description="objective alignment to MIM active objective",
            status="verified" if objective_aligned else "blocked",
            evidence="integration status snapshot",
            last_update=integration.get("generated_at", "n/a"),
            confidence="high" if objective_aligned else "high",
        ),
        TaskRow(
            phase="Phase 4 — Promotion gate",
            task="10",
            description="compatibility + alignment pre-promotion gate",
            status="verified" if gate_core_ok else "in_progress",
            evidence="log entry",
            last_update=file_mtime_iso(LOOP_LOG),
            confidence="high" if gate_core_ok else "medium",
        ),
        TaskRow(
            phase="Phase 4 — Promotion gate",
            task="11",
            description="readiness and production evidence capture",
            status="verified" if (readiness_exists and prod_exists) else "not_started",
            evidence="readiness report" if readiness_exists else "prod report",
            last_update=max(
                file_mtime_iso(READINESS_REPORT), file_mtime_iso(PROD_REPORT)
            ),
            confidence="high" if (readiness_exists and prod_exists) else "high",
        ),
    ]

    for row in rows:
        if row.status not in VALID_STATUSES:
            raise ValueError(f"invalid status: {row.status}")
        if row.confidence not in VALID_CONFIDENCE:
            raise ValueError(f"invalid confidence: {row.confidence}")

    return rows


def write_report(rows: list[TaskRow]) -> None:
    counts = status_counts(rows)
    task_num = detect_task_num()
    pass_count = count_log(r"Cycle PASS; next TASK_NUM=")
    fail_count = count_log(r"Cycle FAIL;")
    last_pass = latest_pass_line()

    lines: list[str] = []
    lines.append("# Objective 75 Lifecycle Status")
    lines.append("")
    lines.append(
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- not_started: {counts['not_started']}")
    lines.append(f"- in_progress: {counts['in_progress']}")
    lines.append(f"- completed: {counts['completed']}")
    lines.append(f"- blocked: {counts['blocked']}")
    lines.append(f"- verified: {counts['verified']}")
    lines.append("")
    lines.append(f"- overnight_task_num: {task_num}")
    lines.append(f"- promotions_recorded: {pass_count}")
    lines.append(f"- cycle_failures_recorded: {fail_count}")
    lines.append(f"- last_cycle_pass: {last_pass}")
    lines.append("")
    lines.append("## Task Table")
    lines.append("")
    lines.append(
        "| Phase | Task | Description | Status | Evidence | Last Update | Confidence |"
    )
    lines.append("|---|---:|---|---|---|---|---|")
    for row in rows:
        lines.append(
            f"| {row.phase} | {row.task} | {row.description} | {row.status} | {row.evidence} | {row.last_update} | {row.confidence} |"
        )
    lines.append("")
    lines.append("## Evidence Inputs")
    lines.append("")
    lines.append("- runtime/logs/objective75_overnight.log")
    lines.append("- runtime/logs/objective75_overnight_state.env")
    lines.append("- runtime/shared/TOD_INTEGRATION_STATUS.latest.json")
    lines.append("- runtime/shared/MIM_TOD_HANDSHAKE_PACKET.latest.json")
    lines.append("- runtime/shared/MIM_MANIFEST.latest.json")
    lines.append("- docs/objective-75-mim-tod-interface-hardening.md")
    lines.append("- docs/tod-mim-bridge.md")
    lines.append("- docs/objective-75-promotion-readiness-report.md")
    lines.append("- docs/objective-75-prod-promotion-report.md")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- Statuses are artifact-derived and intentionally conservative when readiness/prod reports are missing."
    )
    lines.append(
        "- Confidence reflects direct deterministic evidence (`high`) vs inferred progression (`medium`/`low`)."
    )

    OUTPUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    rows = build_rows()
    write_report(rows)
    print(f"wrote {OUTPUT_MD}")


if __name__ == "__main__":
    main()
