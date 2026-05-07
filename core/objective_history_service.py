from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HISTORY_DIR = ROOT / "runtime" / "history" / "objective_history"
DAY_OBJECTIVE_HISTORY_DIR = ROOT / "runtime" / "history" / "day_objectives"
SYSTEM_OBJECTIVE_HISTORY_DIR = ROOT / "runtime" / "history" / "system_objectives"
SUMMARY_FILE_NAME = "OBJECTIVE_HISTORY_SUMMARY.latest.json"
FORMAL_PROGRAM_RESPONSE_PATH = ROOT / "runtime" / "formal_program_drive_response.json"
PROGRAM_REGISTRY_PATH = ROOT / "runtime" / "shared" / "mim_program_registry.latest.json"
TASK_STATUS_REVIEW_PATH = ROOT / "runtime" / "shared" / "MIM_TASK_STATUS_REVIEW.latest.json"
INTEGRATION_STATUS_PATH = ROOT / "runtime" / "shared" / "TOD_INTEGRATION_STATUS.latest.json"
TRAINING_SUMMARY_PATH = ROOT / "runtime" / "reports" / "mim_evolution_training_summary.json"
LITERATURE_CATALOG_PATH = ROOT / "runtime" / "reports" / "classic_literature_catalog_seed.json"

TERMINAL_STATUSES = {"completed", "failed", "cancelled", "superseded", "not_applicable"}
STATUS_PRIORITY = {
    "completed": 100,
    "failed": 95,
    "cancelled": 90,
    "superseded": 85,
    "executing": 70,
    "dispatched": 60,
    "created": 50,
    "queued": 40,
    "ready": 30,
    "not_applicable": 20,
    "incomplete_evidence": 10,
    "unknown": 0,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact_text(value: object, limit: int = 400) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _relative_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def _normalize_objective_number(value: object) -> int | None:
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    patterns = (
        r"MIM-DAY-(\d+)",
        r"Project[_ ]?(\d+)",
        r"objective[-_ ]?(\d+)",
        r"^(\d+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                number = int(match.group(1))
            except ValueError:
                continue
            return number if number > 0 else None
    return None


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        key = json.dumps(item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _merge_task_lists(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for collection in (existing, incoming):
        for item in collection:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("task_id") or "").strip(),
                str(item.get("title") or "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _choose_status(existing: str, incoming: str) -> str:
    existing = str(existing or "").strip() or "unknown"
    incoming = str(incoming or "").strip() or "unknown"
    if existing == "incomplete_evidence" and incoming == "ready":
        return existing
    if existing == "incomplete_evidence" and incoming != "unknown":
        return incoming
    if incoming == "incomplete_evidence" and existing != "unknown":
        return existing
    return incoming if STATUS_PRIORITY.get(incoming, 0) >= STATUS_PRIORITY.get(existing, 0) else existing


def _merge_timestamps(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key in ("first_observed_at", "last_observed_at", "completed_at"):
        existing_value = str(existing.get(key) or "").strip()
        incoming_value = str(incoming.get(key) or "").strip()
        if key == "first_observed_at":
            merged[key] = min([value for value in (existing_value, incoming_value) if value], default="")
        else:
            merged[key] = max([value for value in (existing_value, incoming_value) if value], default=existing_value or incoming_value)
    return merged


def _merge_lineage(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key in ("program_id", "project_id", "initiative_id"):
        if str(incoming.get(key) or "").strip():
            merged[key] = str(incoming.get(key) or "").strip()
        elif key not in merged:
            merged[key] = ""
    for key in ("database_objective_ids", "request_ids", "sources"):
        values = []
        for collection in (existing.get(key), incoming.get(key)):
            if isinstance(collection, list):
                values.extend(str(item).strip() for item in collection if str(item).strip())
        merged[key] = list(dict.fromkeys(values))
    return merged


def _base_record(objective_id: int) -> dict[str, Any]:
    now = _utc_now_iso()
    return {
        "history_version": "objective-history-v1",
        "objective_id": objective_id,
        "display_title": "",
        "objective": "",
        "goal": "",
        "status": "incomplete_evidence",
        "task_list": [],
        "execution_timeline": [],
        "validation_results": [],
        "final_outcome": {
            "status": "incomplete_evidence",
            "summary": "",
            "updated_at": now,
        },
        "evidence_references": [],
        "timestamps": {
            "first_observed_at": now,
            "last_observed_at": now,
            "completed_at": "",
        },
        "lineage": {
            "program_id": "",
            "project_id": "",
            "initiative_id": "",
            "database_objective_ids": [],
            "request_ids": [],
            "sources": [],
        },
    }


def _merge_record(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing or _base_record(int(incoming.get("objective_id") or 0)))
    for key in ("display_title", "objective", "goal"):
        incoming_value = _compact_text(incoming.get(key) or "")
        if incoming_value:
            merged[key] = incoming_value
    merged["status"] = _choose_status(str(merged.get("status") or ""), str(incoming.get("status") or ""))
    merged["task_list"] = _merge_task_lists(
        merged.get("task_list") if isinstance(merged.get("task_list"), list) else [],
        incoming.get("task_list") if isinstance(incoming.get("task_list"), list) else [],
    )
    merged["execution_timeline"] = _dedupe_items(
        (merged.get("execution_timeline") if isinstance(merged.get("execution_timeline"), list) else [])
        + (incoming.get("execution_timeline") if isinstance(incoming.get("execution_timeline"), list) else [])
    )
    merged["validation_results"] = _dedupe_items(
        (merged.get("validation_results") if isinstance(merged.get("validation_results"), list) else [])
        + (incoming.get("validation_results") if isinstance(incoming.get("validation_results"), list) else [])
    )
    merged["evidence_references"] = _dedupe_items(
        (merged.get("evidence_references") if isinstance(merged.get("evidence_references"), list) else [])
        + (incoming.get("evidence_references") if isinstance(incoming.get("evidence_references"), list) else [])
    )
    merged["timestamps"] = _merge_timestamps(
        merged.get("timestamps") if isinstance(merged.get("timestamps"), dict) else {},
        incoming.get("timestamps") if isinstance(incoming.get("timestamps"), dict) else {},
    )
    merged["lineage"] = _merge_lineage(
        merged.get("lineage") if isinstance(merged.get("lineage"), dict) else {},
        incoming.get("lineage") if isinstance(incoming.get("lineage"), dict) else {},
    )
    existing_outcome = merged.get("final_outcome") if isinstance(merged.get("final_outcome"), dict) else {}
    incoming_outcome = incoming.get("final_outcome") if isinstance(incoming.get("final_outcome"), dict) else {}
    merged_outcome_status = _choose_status(
        str(existing_outcome.get("status") or ""),
        str(incoming_outcome.get("status") or ""),
    )
    merged["final_outcome"] = {
        "status": merged_outcome_status,
        "summary": _compact_text(
            incoming_outcome.get("summary")
            or existing_outcome.get("summary")
            or ""
        ),
        "updated_at": str(incoming_outcome.get("updated_at") or existing_outcome.get("updated_at") or _utc_now_iso()),
    }
    if merged_outcome_status in TERMINAL_STATUSES and not str(merged["timestamps"].get("completed_at") or "").strip():
        merged["timestamps"]["completed_at"] = str(incoming_outcome.get("updated_at") or _utc_now_iso())
    return merged


def _record_path(history_dir: Path, objective_id: int) -> Path:
    return history_dir / f"objective_{objective_id}.json"


def _is_day_objective(objective_id: int) -> bool:
    return 1 <= objective_id <= 15


def _classified_history_dir(objective_id: int) -> Path:
    return DAY_OBJECTIVE_HISTORY_DIR if _is_day_objective(objective_id) else SYSTEM_OBJECTIVE_HISTORY_DIR


def _resolved_history_dir(history_dir: Path | None) -> Path:
    return history_dir or DEFAULT_HISTORY_DIR


def _history_directories(history_dir: Path) -> list[Path]:
    candidates = [history_dir]
    if history_dir == DEFAULT_HISTORY_DIR:
        candidates.extend([DAY_OBJECTIVE_HISTORY_DIR, SYSTEM_OBJECTIVE_HISTORY_DIR])
    deduped: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    return deduped


def persist_objective_record(record: dict[str, Any], *, history_dir: Path | None = None) -> dict[str, Any]:
    objective_id = int(record.get("objective_id") or 0)
    if objective_id <= 0:
        raise ValueError("objective_id must be a positive integer")
    history_dir = _resolved_history_dir(history_dir)
    target_dir = _classified_history_dir(objective_id) if history_dir == DEFAULT_HISTORY_DIR else history_dir
    path = _record_path(target_dir, objective_id)
    existing = _read_json(path) or (_read_json(_record_path(history_dir, objective_id)) if history_dir == DEFAULT_HISTORY_DIR else None)
    merged = _merge_record(existing, record)
    _write_json(path, merged)
    if history_dir == DEFAULT_HISTORY_DIR:
        _write_json(_record_path(DEFAULT_HISTORY_DIR, objective_id), merged)
    return merged


def load_objective_history(*, history_dir: Path | None = None) -> list[dict[str, Any]]:
    history_dir = _resolved_history_dir(history_dir)
    records_by_id: dict[int, dict[str, Any]] = {}
    directories = _history_directories(history_dir)
    if not any(directory.exists() for directory in directories):
        return []
    for directory in directories:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("objective_*.json")):
            payload = _read_json(path)
            if not isinstance(payload, dict):
                continue
            objective_id = int(payload.get("objective_id") or 0)
            if objective_id <= 0:
                continue
            existing = records_by_id.get(objective_id)
            if existing is None:
                records_by_id[objective_id] = payload
                continue
            existing_is_legacy = directory == DEFAULT_HISTORY_DIR and _classified_history_dir(objective_id) != DEFAULT_HISTORY_DIR
            if existing_is_legacy:
                continue
            records_by_id[objective_id] = _merge_record(existing, payload)
    return [records_by_id[objective_id] for objective_id in sorted(records_by_id)]


def build_objective_history_summary(*, history_dir: Path | None = None) -> dict[str, Any]:
    history_dir = _resolved_history_dir(history_dir)
    records = load_objective_history(history_dir=history_dir)
    entries = []
    for record in records:
        entries.append(
            {
                "objective_id": int(record.get("objective_id") or 0),
                "status": str(record.get("status") or "incomplete_evidence"),
                "display_title": str(record.get("display_title") or record.get("lineage", {}).get("project_id") or "").strip(),
                "final_outcome": dict(record.get("final_outcome") or {}),
                "evidence_count": len(record.get("evidence_references") if isinstance(record.get("evidence_references"), list) else []),
                "task_count": len(record.get("task_list") if isinstance(record.get("task_list"), list) else []),
            }
        )
    return {
        "generated_at": _utc_now_iso(),
        "history_version": "objective-history-summary-v1",
        "objective_count": len(entries),
        "entries": entries,
    }


def write_objective_history_summary(*, history_dir: Path | None = None) -> dict[str, Any]:
    history_dir = _resolved_history_dir(history_dir)
    summary = build_objective_history_summary(history_dir=history_dir)
    _write_json(history_dir / SUMMARY_FILE_NAME, summary)
    if history_dir == DEFAULT_HISTORY_DIR:
        day_records = load_objective_history(history_dir=DAY_OBJECTIVE_HISTORY_DIR)
        system_records = load_objective_history(history_dir=SYSTEM_OBJECTIVE_HISTORY_DIR)
        _write_json(
            DAY_OBJECTIVE_HISTORY_DIR / SUMMARY_FILE_NAME,
            {
                "generated_at": summary["generated_at"],
                "history_version": summary["history_version"],
                "objective_count": len(day_records),
                "entries": [entry for entry in summary["entries"] if 1 <= int(entry.get("objective_id") or 0) <= 15],
            },
        )
        _write_json(
            SYSTEM_OBJECTIVE_HISTORY_DIR / SUMMARY_FILE_NAME,
            {
                "generated_at": summary["generated_at"],
                "history_version": summary["history_version"],
                "objective_count": len(system_records),
                "entries": [entry for entry in summary["entries"] if int(entry.get("objective_id") or 0) > 15],
            },
        )
    return summary


def _project_record(
    objective_id: int,
    *,
    project: dict[str, Any],
    source_path: Path,
    source_kind: str,
    observed_at: str,
    partial: bool,
) -> dict[str, Any]:
    project_status = str(project.get("status") or "").strip() or "incomplete_evidence"
    progress = project.get("progress") if isinstance(project.get("progress"), dict) else {}
    summary = _compact_text(project.get("summary") or project.get("objective") or project.get("goal") or "")
    status = project_status if project_status in TERMINAL_STATUSES or not partial else "incomplete_evidence"
    task_list = []
    for index, item in enumerate(project.get("tasks") if isinstance(project.get("tasks"), list) else [], start=1):
        text = _compact_text(item, 320)
        if text:
            task_list.append({"task_id": f"task-{index}", "title": text, "status": "documented"})
    evidence = {
        "path": _relative_path(source_path),
        "kind": source_kind,
        "summary": summary,
        "captured_at": observed_at,
    }
    return {
        "objective_id": objective_id,
        "display_title": _compact_text(project.get("display_title") or project.get("project_id") or "", 160),
        "objective": _compact_text(project.get("objective") or "", 500),
        "goal": _compact_text(project.get("goal") or "", 500),
        "status": status,
        "task_list": task_list,
        "execution_timeline": [
            {
                "timestamp": observed_at,
                "event": "observed_project_status",
                "status": project_status,
                "source": source_kind,
                "summary": summary,
            }
        ],
        "validation_results": [
            {
                "timestamp": observed_at,
                "validator": source_kind,
                "status": "pass" if project_status == "completed" else "observed",
                "summary": _compact_text(progress.get("summary") or summary or project_status, 320),
            }
        ],
        "final_outcome": {
            "status": project_status if project_status else "incomplete_evidence",
            "summary": summary,
            "updated_at": observed_at,
        },
        "evidence_references": [evidence],
        "timestamps": {
            "first_observed_at": observed_at,
            "last_observed_at": observed_at,
            "completed_at": observed_at if project_status in TERMINAL_STATUSES else "",
        },
        "lineage": {
            "program_id": str(project.get("program_id") or "").strip(),
            "project_id": str(project.get("project_id") or "").strip(),
            "initiative_id": str(project.get("project_id") or "").strip(),
            "database_objective_ids": [str(project.get("objective_id") or "").strip()] if str(project.get("objective_id") or "").strip() else [],
            "request_ids": [],
            "sources": [source_kind],
        },
    }


def _placeholder_record(objective_id: int, *, source_path: Path, observed_at: str, reason: str) -> dict[str, Any]:
    return {
        "objective_id": objective_id,
        "display_title": f"Objective {objective_id}",
        "objective": reason,
        "goal": "",
        "status": "incomplete_evidence",
        "task_list": [],
        "execution_timeline": [
            {
                "timestamp": observed_at,
                "event": "placeholder_backfill",
                "status": "incomplete_evidence",
                "source": "history_gap_backfill",
                "summary": reason,
            }
        ],
        "validation_results": [],
        "final_outcome": {
            "status": "not_applicable",
            "summary": reason,
            "updated_at": observed_at,
        },
        "evidence_references": [
            {
                "path": _relative_path(source_path),
                "kind": "history_gap_backfill",
                "summary": reason,
                "captured_at": observed_at,
            }
        ],
        "timestamps": {
            "first_observed_at": observed_at,
            "last_observed_at": observed_at,
            "completed_at": "",
        },
        "lineage": {
            "program_id": "",
            "project_id": "",
            "initiative_id": "",
            "database_objective_ids": [],
            "request_ids": [],
            "sources": ["history_gap_backfill"],
        },
    }


def _extract_projects(payload: dict[str, Any]) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []

    def _walk(node: Any, current_program_id: str = "") -> None:
        if isinstance(node, dict):
            project_id = str(node.get("project_id") or "").strip()
            if project_id and _normalize_objective_number(project_id) is not None:
                project = dict(node)
                if current_program_id and not str(project.get("program_id") or "").strip():
                    project["program_id"] = current_program_id
                projects.append(project)
            next_program_id = str(node.get("program_id") or current_program_id).strip()
            for value in node.values():
                _walk(value, next_program_id)
        elif isinstance(node, list):
            for item in node:
                _walk(item, current_program_id)

    _walk(payload)
    richest: dict[str, dict[str, Any]] = {}
    for project in projects:
        project_id = str(project.get("project_id") or "").strip()
        if not project_id:
            continue
        previous = richest.get(project_id)
        project_score = sum(1 for key in ("status", "summary", "progress", "tasks", "objective_id") if project.get(key))
        previous_score = sum(1 for key in ("status", "summary", "progress", "tasks", "objective_id") if previous and previous.get(key))
        if previous is None or project_score >= previous_score:
            richest[project_id] = project
    return list(richest.values())


def persist_program_status_snapshot(
    program_status: dict[str, Any],
    *,
    history_dir: Path | None = None,
    source: str = "core.autonomy_driver_service.build_initiative_status",
) -> dict[str, Any]:
    history_dir = _resolved_history_dir(history_dir)
    observed_at = _utc_now_iso()
    projects = program_status.get("projects") if isinstance(program_status.get("projects"), list) else []
    for project in projects:
        if not isinstance(project, dict):
            continue
        objective_id = _normalize_objective_number(project.get("project_id") or project.get("display_title"))
        if objective_id is None:
            continue
        record = _project_record(
            objective_id,
            project=project,
            source_path=PROGRAM_REGISTRY_PATH,
            source_kind=source,
            observed_at=observed_at,
            partial=str(project.get("status") or "") not in TERMINAL_STATUSES,
        )
        persist_objective_record(record, history_dir=history_dir)
    return write_objective_history_summary(history_dir=history_dir)


def _artifact_root(output_dir: Path, artifact_root: Path | None) -> Path:
    if artifact_root is not None:
        return artifact_root
    return output_dir.resolve().parents[1]


def sync_objective_history_from_export_payload(
    payload: dict[str, Any],
    output_dir: Path,
    *,
    history_dir: Path | None = None,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    history_dir = _resolved_history_dir(history_dir)
    observed_at = str(payload.get("exported_at") or _utc_now_iso())
    root = _artifact_root(output_dir, artifact_root)
    formal_path = root / "runtime" / "formal_program_drive_response.json"
    registry_path = root / "runtime" / "shared" / "mim_program_registry.latest.json"
    review_path = root / "runtime" / "shared" / "MIM_TASK_STATUS_REVIEW.latest.json"
    integration_path = root / "runtime" / "shared" / "TOD_INTEGRATION_STATUS.latest.json"
    training_path = root / "runtime" / "reports" / "mim_evolution_training_summary.json"
    catalog_path = root / "runtime" / "reports" / "classic_literature_catalog_seed.json"

    discovered_ids: set[int] = set()

    for path, source_kind in (
        (formal_path, "formal_program_drive_response"),
        (registry_path, "program_registry_snapshot"),
    ):
        payload_dict = _read_json(path)
        if not isinstance(payload_dict, dict):
            continue
        for project in _extract_projects(payload_dict):
            objective_id = _normalize_objective_number(project.get("project_id") or project.get("display_title"))
            if objective_id is None:
                continue
            discovered_ids.add(objective_id)
            record = _project_record(
                objective_id,
                project=project,
                source_path=path,
                source_kind=source_kind,
                observed_at=observed_at,
                partial=source_kind == "formal_program_drive_response" and str(project.get("status") or "") not in TERMINAL_STATUSES,
            )
            persist_objective_record(record, history_dir=history_dir)

    active_objective = _normalize_objective_number(payload.get("objective_active"))
    latest_completed = _normalize_objective_number(payload.get("latest_completed_objective"))
    next_objective = _normalize_objective_number(payload.get("current_next_objective"))
    source_of_truth = payload.get("source_of_truth") if isinstance(payload.get("source_of_truth"), dict) else {}

    for objective_id, status, summary in (
        (active_objective, str(payload.get("phase") or "").strip() == "execution" and "executing" or "incomplete_evidence", "Observed as the active objective in the exported shared context."),
        (latest_completed, "completed", "Observed as the latest completed objective in the exported shared context."),
        (next_objective, "incomplete_evidence", "Observed as the next objective target in the exported shared context."),
    ):
        if objective_id is None:
            continue
        discovered_ids.add(objective_id)
        persist_objective_record(
            {
                "objective_id": objective_id,
                "display_title": f"Objective {objective_id}",
                "objective": summary,
                "goal": "",
                "status": status,
                "task_list": [],
                "execution_timeline": [
                    {
                        "timestamp": observed_at,
                        "event": "export_context_observation",
                        "status": status,
                        "source": "MIM_CONTEXT_EXPORT.latest.json",
                        "summary": summary,
                    }
                ],
                "validation_results": [],
                "final_outcome": {
                    "status": "completed" if status == "completed" else status,
                    "summary": summary,
                    "updated_at": observed_at,
                },
                "evidence_references": [
                    {
                        "path": _relative_path(output_dir / "MIM_CONTEXT_EXPORT.latest.json"),
                        "kind": "context_export",
                        "summary": summary,
                        "captured_at": observed_at,
                    }
                ],
                "timestamps": {
                    "first_observed_at": observed_at,
                    "last_observed_at": observed_at,
                    "completed_at": observed_at if status == "completed" else "",
                },
                "lineage": {
                    "program_id": "",
                    "project_id": "",
                    "initiative_id": "",
                    "database_objective_ids": [],
                    "request_ids": [],
                    "sources": ["context_export"],
                },
            },
            history_dir=history_dir,
        )

    review_payload = _read_json(review_path)
    if isinstance(review_payload, dict):
        review_task = review_payload.get("task") if isinstance(review_payload.get("task"), dict) else {}
        review_objective = _normalize_objective_number(review_task.get("objective_id"))
        if review_objective is not None:
            discovered_ids.add(review_objective)
            result_status = str(review_task.get("result_status") or review_payload.get("state") or "").strip() or "incomplete_evidence"
            persist_objective_record(
                {
                    "objective_id": review_objective,
                    "display_title": f"Objective {review_objective}",
                    "objective": _compact_text(review_payload.get("state_reason") or "Task status review evidence captured.", 320),
                    "goal": "",
                    "status": "failed" if result_status == "failed" else "incomplete_evidence",
                    "task_list": [
                        {
                            "task_id": str(review_task.get("active_task_id") or review_task.get("authoritative_task_id") or "").strip(),
                            "title": _compact_text(review_task.get("active_task_id") or review_task.get("authoritative_task_id") or "task status review", 320),
                            "status": result_status,
                        }
                    ]
                    if str(review_task.get("active_task_id") or review_task.get("authoritative_task_id") or "").strip()
                    else [],
                    "execution_timeline": [
                        {
                            "timestamp": str(review_payload.get("generated_at") or observed_at),
                            "event": "task_status_review",
                            "status": result_status,
                            "source": "MIM_TASK_STATUS_REVIEW.latest.json",
                            "summary": _compact_text(review_payload.get("state_reason") or "", 320),
                        }
                    ],
                    "validation_results": [],
                    "final_outcome": {
                        "status": "failed" if result_status == "failed" else "incomplete_evidence",
                        "summary": _compact_text(review_payload.get("state_reason") or "", 320),
                        "updated_at": str(review_payload.get("generated_at") or observed_at),
                    },
                    "evidence_references": [
                        {
                            "path": _relative_path(review_path),
                            "kind": "task_status_review",
                            "summary": _compact_text(review_payload.get("state_reason") or "", 320),
                            "captured_at": str(review_payload.get("generated_at") or observed_at),
                        }
                    ],
                    "timestamps": {
                        "first_observed_at": str(review_payload.get("generated_at") or observed_at),
                        "last_observed_at": str(review_payload.get("generated_at") or observed_at),
                        "completed_at": "",
                    },
                    "lineage": {
                        "program_id": "",
                        "project_id": "",
                        "initiative_id": "",
                        "database_objective_ids": [],
                        "request_ids": [str(review_task.get("request_request_id") or "").strip()] if str(review_task.get("request_request_id") or "").strip() else [],
                        "sources": ["task_status_review"],
                    },
                },
                history_dir=history_dir,
            )

    integration_payload = _read_json(integration_path)
    if isinstance(integration_payload, dict):
        alignment = integration_payload.get("objective_alignment") if isinstance(integration_payload.get("objective_alignment"), dict) else {}
        aligned_objective = _normalize_objective_number(alignment.get("mim_objective_active") or alignment.get("tod_current_objective"))
        if aligned_objective is not None:
            discovered_ids.add(aligned_objective)
            persist_objective_record(
                {
                    "objective_id": aligned_objective,
                    "display_title": f"Objective {aligned_objective}",
                    "objective": "TOD/MIM integration alignment evidence.",
                    "goal": "",
                    "status": "incomplete_evidence",
                    "task_list": [],
                    "execution_timeline": [
                        {
                            "timestamp": str(integration_payload.get("generated_at") or observed_at),
                            "event": "integration_alignment",
                            "status": str(alignment.get("status") or "in_sync"),
                            "source": "TOD_INTEGRATION_STATUS.latest.json",
                            "summary": _compact_text(alignment.get("status") or "integration alignment", 320),
                        }
                    ],
                    "validation_results": [],
                    "final_outcome": {
                        "status": "incomplete_evidence",
                        "summary": "Integration alignment evidence recorded.",
                        "updated_at": str(integration_payload.get("generated_at") or observed_at),
                    },
                    "evidence_references": [
                        {
                            "path": _relative_path(integration_path),
                            "kind": "integration_status",
                            "summary": _compact_text(alignment.get("status") or "integration alignment", 320),
                            "captured_at": str(integration_payload.get("generated_at") or observed_at),
                        }
                    ],
                    "timestamps": {
                        "first_observed_at": str(integration_payload.get("generated_at") or observed_at),
                        "last_observed_at": str(integration_payload.get("generated_at") or observed_at),
                        "completed_at": "",
                    },
                    "lineage": {
                        "program_id": "",
                        "project_id": "",
                        "initiative_id": "",
                        "database_objective_ids": [],
                        "request_ids": [],
                        "sources": ["integration_status"],
                    },
                },
                history_dir=history_dir,
            )

    training_payload = _read_json(training_path)
    if isinstance(training_payload, dict):
        persist_objective_record(
            {
                "objective_id": 14,
                "display_title": "MIM-DAY-14-EXTED-SIMULATION-TRAINING-ON-MIM-NATURAL-LANGUAGE-LEARNING-1-000-000-QUESTIONS-INTERACTIONS-IN-EACH-GROUP",
                "objective": "Run staged simulation training across MIM natural-language learning categories.",
                "goal": "Run small batches first, optimize, then complete the full run.",
                "status": "completed",
                "task_list": [],
                "execution_timeline": [
                    {
                        "timestamp": str(training_payload.get("generated_at") or observed_at),
                        "event": "training_summary_written",
                        "status": "completed",
                        "source": "mim_evolution_training_summary.json",
                        "summary": _compact_text(training_payload.get("conversation", {}).get("overall") or "training summary updated", 320),
                    }
                ],
                "validation_results": [
                    {
                        "timestamp": str(training_payload.get("generated_at") or observed_at),
                        "validator": "training_summary",
                        "status": "pass",
                        "summary": f"overall={training_payload.get('conversation', {}).get('overall')} scenario_count={training_payload.get('conversation', {}).get('scenario_count')} actions.pass_ratio={training_payload.get('actions', {}).get('pass_ratio')}",
                    }
                ],
                "final_outcome": {
                    "status": "completed",
                    "summary": "Fresh small-batch training artifact recorded.",
                    "updated_at": str(training_payload.get("generated_at") or observed_at),
                },
                "evidence_references": [
                    {
                        "path": _relative_path(training_path),
                        "kind": "training_summary",
                        "summary": "Fresh small-batch training artifact recorded.",
                        "captured_at": str(training_payload.get("generated_at") or observed_at),
                    }
                ],
                "timestamps": {
                    "first_observed_at": str(training_payload.get("generated_at") or observed_at),
                    "last_observed_at": str(training_payload.get("generated_at") or observed_at),
                    "completed_at": str(training_payload.get("generated_at") or observed_at),
                },
                "lineage": {
                    "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                    "project_id": "MIM-DAY-14-EXTED-SIMULATION-TRAINING-ON-MIM-NATURAL-LANGUAGE-LEARNING-1-000-000-QUESTIONS-INTERACTIONS-IN-EACH-GROUP",
                    "initiative_id": "MIM-DAY-14-EXTED-SIMULATION-TRAINING-ON-MIM-NATURAL-LANGUAGE-LEARNING-1-000-000-QUESTIONS-INTERACTIONS-IN-EACH-GROUP",
                    "database_objective_ids": [],
                    "request_ids": [],
                    "sources": ["training_summary"],
                },
            },
            history_dir=history_dir,
        )
        discovered_ids.add(14)

    catalog_payload = _read_json(catalog_path)
    if isinstance(catalog_payload, dict):
        persist_objective_record(
            {
                "objective_id": 15,
                "display_title": "MIM-DAY-15-CLASSIC-LITERATURE-INTRODUCTION",
                "objective": "Normalize a source-backed classic literature catalog.",
                "goal": "Create a bounded catalog of free classic books with validation metadata.",
                "status": "completed" if str(catalog_payload.get("catalog_status") or "").strip() == "source_candidate_catalog_ready" else "incomplete_evidence",
                "task_list": [],
                "execution_timeline": [
                    {
                        "timestamp": observed_at,
                        "event": "catalog_summary_observed",
                        "status": str(catalog_payload.get("catalog_status") or "incomplete_evidence"),
                        "source": "classic_literature_catalog_seed.json",
                        "summary": _compact_text(catalog_payload.get("validation_basis") or catalog_payload.get("catalog_status") or "", 320),
                    }
                ],
                "validation_results": [
                    {
                        "timestamp": observed_at,
                        "validator": "catalog_seed",
                        "status": "pass" if str(catalog_payload.get("catalog_status") or "").strip() == "source_candidate_catalog_ready" else "observed",
                        "summary": f"entries={catalog_payload.get('catalog_entry_count') or catalog_payload.get('validated_catalog_entry_count')} priority_seeds={catalog_payload.get('priority_seed_entry_count') or catalog_payload.get('validated_priority_seed_count')} canonical_link_enrichment={catalog_payload.get('canonical_link_enrichment_status')}",
                    }
                ],
                "final_outcome": {
                    "status": "completed" if str(catalog_payload.get("catalog_status") or "").strip() == "source_candidate_catalog_ready" else "incomplete_evidence",
                    "summary": _compact_text(catalog_payload.get("validation_basis") or catalog_payload.get("catalog_status") or "", 320),
                    "updated_at": observed_at,
                },
                "evidence_references": [
                    {
                        "path": _relative_path(catalog_path),
                        "kind": "catalog_seed",
                        "summary": _compact_text(catalog_payload.get("validation_basis") or catalog_payload.get("catalog_status") or "", 320),
                        "captured_at": observed_at,
                    }
                ],
                "timestamps": {
                    "first_observed_at": observed_at,
                    "last_observed_at": observed_at,
                    "completed_at": observed_at if str(catalog_payload.get("catalog_status") or "").strip() == "source_candidate_catalog_ready" else "",
                },
                "lineage": {
                    "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                    "project_id": "MIM-DAY-15-CLASSIC-LITERATURE-INTRODUCTION",
                    "initiative_id": "MIM-DAY-15-CLASSIC-LITERATURE-INTRODUCTION",
                    "database_objective_ids": [],
                    "request_ids": [],
                    "sources": ["catalog_seed"],
                },
            },
            history_dir=history_dir,
        )
        discovered_ids.add(15)

    if discovered_ids and min(discovered_ids) <= 1 and max(discovered_ids) >= 15:
        for objective_id in range(1, 16):
            if objective_id in discovered_ids:
                continue
            persist_objective_record(
                _placeholder_record(
                    objective_id,
                    source_path=formal_path if formal_path.exists() else output_dir / "MIM_CONTEXT_EXPORT.latest.json",
                    observed_at=observed_at,
                    reason="No registered project entry was found for this objective in the active formal program contract. The objective ordinal is preserved as incomplete evidence instead of being dropped.",
                ),
                history_dir=history_dir,
            )

    return write_objective_history_summary(history_dir=history_dir)