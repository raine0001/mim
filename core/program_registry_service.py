from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROGRAM_REGISTRY_PATH = Path("runtime/shared/mim_program_registry.latest.json")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _compact_text(value: object, limit: int = 400) -> str:
    cleaned = " ".join(str(value or "").strip().split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _normalize_identifier(value: object) -> str:
    return " ".join(str(value or "").strip().split())[:120]


def _normalize_project_id(value: object) -> str:
    text = _normalize_identifier(value)
    match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", text)
    return match.group(1) if match else ""


def _slugify_identifier(value: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").strip().upper())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned[:120]


def _normalize_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for item in values:
        text = _compact_text(item, 240)
        if text:
            normalized.append(text)
    return normalized[:20]


def _extract_named_section(block: str, field_name: str) -> str:
    pattern = re.compile(
        rf"(?ims)^\s*{re.escape(field_name)}\s*:\s*(.+?)(?=^\s*(?:OBJECTIVE|GOAL|SCOPE|TASKS|SUCCESS CRITERIA)\s*:|^\s*Project(?:[_ ]?\d+_ID\s*:|\s+\d+\s*-)|\Z)"
    )
    match = pattern.search(str(block or ""))
    return match.group(1).strip() if match else ""


def _extract_bullets_or_numbered_lines(block: str) -> list[str]:
    items: list[str] = []
    for raw_line in str(block or "").splitlines():
        line = str(raw_line or "").strip()
        if not line:
            continue
        numbered_match = re.match(r"^\d+[.)]\s*(.+)$", line)
        bullet_match = re.match(r"^-\s+(.+)$", line)
        item = ""
        if numbered_match:
            item = numbered_match.group(1).strip()
        elif bullet_match:
            item = bullet_match.group(1).strip()
        if item:
            items.append(_compact_text(item, 320))
    return list(dict.fromkeys(items))[:40]


def _project_blocks(user_intent: str) -> list[dict[str, Any]]:
    pattern = re.compile(
        r"(?ims)^\s*(?:(Project[_ ]?(\d+)_ID\s*:\s*([^\n]+))|(Project\s+(\d+)\s*-\s*([^\n]+)))\s*$"
    )
    text = str(user_intent or "")
    matches = list(pattern.finditer(text))
    blocks: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block_text = text[start:end].strip()
        ordinal_text = match.group(2) or match.group(5) or "0"
        title_value = match.group(3) or match.group(6) or ""
        ordinal = int(ordinal_text or 0)
        explicit_project_id = _normalize_project_id(title_value) if match.group(1) else ""
        project_id = explicit_project_id
        if not project_id:
            slug = _slugify_identifier(title_value)
            if slug:
                project_id = f"MIM-DAY-{ordinal:02d}-{slug}"
        objective = _compact_text(_extract_named_section(block_text, "OBJECTIVE"), 280)
        goal = _compact_text(_extract_named_section(block_text, "GOAL"), 280)
        scope = _extract_bullets_or_numbered_lines(_extract_named_section(block_text, "SCOPE"))
        tasks = _extract_bullets_or_numbered_lines(_extract_named_section(block_text, "TASKS"))
        success_criteria = _extract_bullets_or_numbered_lines(_extract_named_section(block_text, "SUCCESS CRITERIA"))
        blocks.append(
            {
                "ordinal": ordinal,
                "project_id": project_id,
                "display_title": _compact_text(title_value, 160),
                "objective": objective,
                "goal": goal,
                "scope": scope,
                "tasks": tasks,
                "success_criteria": success_criteria,
                "status": "ready",
            }
        )
    return [block for block in blocks if str(block.get("project_id") or "").strip()]


def project_program_intent(program_id: str, project: dict[str, Any]) -> str:
    if not isinstance(project, dict):
        return ""
    project_id = str(project.get("project_id") or "").strip()
    if not project_id:
        return ""
    lines = [
        f"PROGRAM_ID: {str(program_id or '').strip()}",
        f"Project_{int(project.get('ordinal') or 0)}_ID: {project_id}",
        f"INITIATIVE_ID: {project_id}",
    ]
    objective = str(project.get("objective") or "").strip()
    if objective:
        lines.extend(["", "OBJECTIVE:", objective])
    goal = str(project.get("goal") or "").strip()
    if goal:
        lines.extend(["", "GOAL:", goal])
    scope_items = project.get("scope") if isinstance(project.get("scope"), list) else []
    if scope_items:
        lines.append("")
        lines.append("SCOPE:")
        lines.extend(f"- {str(item).strip()}" for item in scope_items if str(item).strip())
    task_items = project.get("tasks") if isinstance(project.get("tasks"), list) else []
    if task_items:
        lines.append("")
        lines.append("TASKS:")
        lines.extend(f"{index}. {str(item).strip()}" for index, item in enumerate(task_items, start=1) if str(item).strip())
    success_items = project.get("success_criteria") if isinstance(project.get("success_criteria"), list) else []
    if success_items:
        lines.append("")
        lines.append("SUCCESS CRITERIA:")
        lines.extend(f"- {str(item).strip()}" for item in success_items if str(item).strip())
    return "\n".join(lines)


def next_program_project(payload: dict[str, Any], current_project_id: str) -> dict[str, Any]:
    programs = payload.get("programs") if isinstance(payload.get("programs"), list) else []
    active_program_id = str(payload.get("active_program_id") or "").strip()
    active_program = next(
        (
            program
            for program in programs
            if isinstance(program, dict)
            and str(program.get("program_id") or "").strip() == active_program_id
        ),
        programs[0] if programs and isinstance(programs[0], dict) else {},
    )
    projects = active_program.get("projects") if isinstance(active_program.get("projects"), list) else []
    normalized_current = str(current_project_id or "").strip().lower()
    sorted_projects = sorted(
        [project for project in projects if isinstance(project, dict)],
        key=lambda item: int(item.get("ordinal") or 0),
    )
    for index, project in enumerate(sorted_projects):
        project_id = str(project.get("project_id") or "").strip().lower()
        if project_id == normalized_current:
            for candidate in sorted_projects[index + 1 :]:
                if str(candidate.get("project_id") or "").strip():
                    return candidate
    return {}


def load_program_registry(*, path: Path = PROGRAM_REGISTRY_PATH) -> dict[str, Any]:
    if not path.exists():
        return {"programs": [], "active_program_id": "", "updated_at": ""}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"programs": [], "active_program_id": "", "updated_at": ""}
    if not isinstance(payload, dict):
        return {"programs": [], "active_program_id": "", "updated_at": ""}
    return {
        "programs": payload.get("programs") if isinstance(payload.get("programs"), list) else [],
        "active_program_id": str(payload.get("active_program_id") or "").strip(),
        "updated_at": str(payload.get("updated_at") or "").strip(),
    }


def save_program_registry(payload: dict[str, Any], *, path: Path = PROGRAM_REGISTRY_PATH) -> dict[str, Any]:
    normalized = {
        "programs": payload.get("programs") if isinstance(payload.get("programs"), list) else [],
        "active_program_id": str(payload.get("active_program_id") or "").strip(),
        "updated_at": _utc_now_iso(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, indent=2, ensure_ascii=True), encoding="utf-8")
    return normalized


def program_registry_summary(payload: dict[str, Any]) -> str:
    programs = payload.get("programs") if isinstance(payload.get("programs"), list) else []
    if not programs:
        return "No active multi-project program is registered."
    active_program_id = str(payload.get("active_program_id") or "").strip()
    active_program = next(
        (
            program
            for program in programs
            if isinstance(program, dict)
            and str(program.get("program_id") or "").strip() == active_program_id
        ),
        programs[0] if programs and isinstance(programs[0], dict) else {},
    )
    program_name = str(active_program.get("program_id") or "active program").strip()
    project_summaries = []
    for project in active_program.get("projects") if isinstance(active_program.get("projects"), list) else []:
        if not isinstance(project, dict):
            continue
        project_id = str(project.get("project_id") or "").strip()
        objective = str(project.get("objective") or "").strip()
        status = str(project.get("status") or "ready").strip()
        if project_id:
            detail = f"{project_id}={status}"
            if objective:
                detail += f" ({_compact_text(objective, 80)})"
            project_summaries.append(detail)
    if not project_summaries:
        return f"Program {program_name} is registered with no tracked projects yet."
    project_count = len(project_summaries)
    project_label = "project" if project_count == 1 else "projects"
    return _compact_text(
        f"Program {program_name} status ({project_count} registered {project_label}): " + "; ".join(project_summaries) + ".",
        320,
    )


def _extract_field_block(user_intent: str, field_name: str) -> str:
    pattern = re.compile(
        rf"(?ims)^\s*{re.escape(field_name)}\s*:\s*(.+?)(?=^\s*[A-Z][A-Z0-9_\- ]*\s*:|\Z)"
    )
    match = pattern.search(str(user_intent or ""))
    return match.group(1).strip() if match else ""


def _extract_projects_from_program_text(user_intent: str) -> list[dict[str, Any]]:
    return _project_blocks(user_intent)


def extract_program_projects_from_text(user_intent: str) -> list[dict[str, Any]]:
    return _extract_projects_from_program_text(user_intent)


def _extract_global_rule_text(user_intent: str, heading: str) -> str:
    pattern = re.compile(
        rf"(?ims)^\s*{re.escape(heading)}\s*$\n(.+?)(?=^\s*(?:GLOBAL EXECUTION AUTHORITY|COMPLETION RULE|CONTINUITY RULE|REPORTING RULE|Project(?:[_ ]?\d+_ID\s*:|\s+\d+\s*-))|\Z)"
    )
    match = pattern.search(str(user_intent or ""))
    if not match:
        inline = _extract_field_block(user_intent, heading)
        return _compact_text(inline, 400) if inline else ""
    return _compact_text(match.group(1), 400)


def ensure_program_registration(user_intent: str) -> dict[str, Any]:
    text = str(user_intent or "")
    program_id = _normalize_project_id(_extract_field_block(text, "PROGRAM_ID"))
    if not program_id:
        return load_program_registry()
    registry = load_program_registry()
    programs = registry.get("programs") if isinstance(registry.get("programs"), list) else []
    projects = _extract_projects_from_program_text(text)
    execution_rules = {
        "no_human_prompts": True,
        "no_pause_for_approval": True,
        "auto_continue": True,
        "report_after_execution": True,
        "completion_requires_validation": True,
        "inherit_to_new_ui_projects": True,
        "tod_preferred": True,
        "tod_continue_without_if_stale": True,
        "post_execution_day_summary": True,
        "bounded_remediation_before_escalation": True,
        "auto_start_next_project": True,
    }
    existing_program = next(
        (
            item
            for item in programs
            if isinstance(item, dict) and str(item.get("program_id") or "").strip() == program_id
        ),
        {},
    )
    if len(projects) == 1 and isinstance(existing_program.get("projects"), list):
        existing_projects = [project for project in existing_program.get("projects", []) if isinstance(project, dict)]
        incoming = projects[0]
        merged = []
        replaced = False
        incoming_project_id = str(incoming.get("project_id") or "").strip().lower()
        for project in existing_projects:
            project_id = str(project.get("project_id") or "").strip().lower()
            if project_id and project_id == incoming_project_id:
                merged.append({**project, **incoming})
                replaced = True
            else:
                merged.append(project)
        if not replaced:
            merged.append(incoming)
        projects = sorted(merged, key=lambda item: int(item.get("ordinal") or 0))
    program_payload = {
        "program_id": program_id,
        "objective": _compact_text(_extract_field_block(text, "OBJECTIVE") or "", 280),
        "execution_rules": execution_rules,
        "global_execution_authority": _extract_global_rule_text(text, "GLOBAL EXECUTION AUTHORITY"),
        "completion_rule": _extract_global_rule_text(text, "COMPLETION RULE"),
        "continuity_rule": _extract_global_rule_text(text, "CONTINUITY RULE"),
        "reporting_rule": _extract_global_rule_text(text, "REPORTING RULE"),
        "projects": projects,
        "updated_at": _utc_now_iso(),
    }
    existing_index = next(
        (
            index
            for index, item in enumerate(programs)
            if isinstance(item, dict) and str(item.get("program_id") or "").strip() == program_id
        ),
        -1,
    )
    if existing_index >= 0:
        programs[existing_index] = program_payload
    else:
        programs.insert(0, program_payload)
    return save_program_registry({"programs": programs, "active_program_id": program_id})


def build_program_status_snapshot(
    *,
    active_objective: dict[str, Any] | None = None,
    active_task: dict[str, Any] | None = None,
    objective_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    registry = load_program_registry()
    programs = registry.get("programs") if isinstance(registry.get("programs"), list) else []
    active_program_id = str(registry.get("active_program_id") or "").strip()
    active_program = next(
        (
            program
            for program in programs
            if isinstance(program, dict)
            and str(program.get("program_id") or "").strip() == active_program_id
        ),
        programs[0] if programs and isinstance(programs[0], dict) else {},
    )
    active_objective = active_objective if isinstance(active_objective, dict) else {}
    active_task = active_task if isinstance(active_task, dict) else {}
    active_initiative_id = str(active_objective.get("initiative_id") or "").strip()
    history_map: dict[str, dict[str, Any]] = {}
    for snapshot in objective_history if isinstance(objective_history, list) else []:
        if not isinstance(snapshot, dict):
            continue
        objective_payload = snapshot.get("objective") if isinstance(snapshot.get("objective"), dict) else {}
        initiative_id = str(objective_payload.get("initiative_id") or "").strip()
        if not initiative_id:
            continue
        normalized = initiative_id.lower()
        previous = history_map.get(normalized)
        if previous is None or int(snapshot.get("position") or 999999) < int(previous.get("position") or 999999):
            history_map[normalized] = snapshot
    project_entries: list[dict[str, Any]] = []
    for project in active_program.get("projects") if isinstance(active_program.get("projects"), list) else []:
        if not isinstance(project, dict):
            continue
        project_id = str(project.get("project_id") or "").strip()
        project_status = str(project.get("status") or "ready").strip()
        project_snapshot = history_map.get(project_id.lower()) if project_id else None
        if project_snapshot is not None:
            activity = project_snapshot.get("activity") if isinstance(project_snapshot.get("activity"), dict) else {}
            progress = project_snapshot.get("progress") if isinstance(project_snapshot.get("progress"), dict) else {}
            execution_state = str(project_snapshot.get("execution_state") or project_snapshot.get("status") or "").strip()
            project_status = execution_state or project_status
            project_entries.append(
                {
                    "project_id": project_id,
                    "objective": _compact_text(project.get("objective") or "", 200),
                    "goal": _compact_text(project.get("goal") or "", 200),
                    "scope": _normalize_list(project.get("scope")),
                    "tasks": _normalize_list(project.get("tasks")),
                    "success_criteria": _normalize_list(project.get("success_criteria")),
                    "display_title": _compact_text(project.get("display_title") or project_id, 160),
                    "status": project_status,
                    "objective_id": project_snapshot.get("objective_id"),
                    "summary": str(activity.get("summary") or project_snapshot.get("summary") or "").strip(),
                    "progress": progress,
                }
            )
            continue
        if project_id and active_initiative_id and project_id.lower() == active_initiative_id.lower():
            execution_state = str(active_task.get("execution_state") or active_objective.get("execution_state") or "").strip()
            project_status = execution_state or str(active_objective.get("status") or project_status).strip()
        project_entries.append(
            {
                "project_id": project_id,
                "objective": _compact_text(project.get("objective") or "", 200),
                "goal": _compact_text(project.get("goal") or "", 200),
                "scope": _normalize_list(project.get("scope")),
                "tasks": _normalize_list(project.get("tasks")),
                "success_criteria": _normalize_list(project.get("success_criteria")),
                "display_title": _compact_text(project.get("display_title") or project_id, 160),
                "status": project_status,
            }
        )
    project_summaries = []
    for project in project_entries:
        if not isinstance(project, dict):
            continue
        project_id = str(project.get("project_id") or "").strip()
        status = str(project.get("status") or "ready").strip()
        objective = str(project.get("objective") or "").strip()
        if project_id:
            detail = f"{project_id}={status}"
            if objective:
                detail += f" ({_compact_text(objective, 80)})"
            project_summaries.append(detail)
    if project_summaries:
        summary = _compact_text(
            f"Program {str(active_program.get('program_id') or 'active program').strip()} status ({len(project_summaries)} registered projects): "
            + "; ".join(project_summaries)
            + ".",
            320,
        )
    else:
        summary = program_registry_summary(registry)

    return {
        "program_id": str(active_program.get("program_id") or "").strip(),
        "objective": _compact_text(active_program.get("objective") or "", 240),
        "updated_at": str(registry.get("updated_at") or "").strip(),
        "execution_rules": active_program.get("execution_rules") if isinstance(active_program.get("execution_rules"), dict) else {},
        "global_execution_authority": str(active_program.get("global_execution_authority") or "").strip(),
        "completion_rule": str(active_program.get("completion_rule") or "").strip(),
        "continuity_rule": str(active_program.get("continuity_rule") or "").strip(),
        "reporting_rule": str(active_program.get("reporting_rule") or "").strip(),
        "projects": project_entries,
        "summary": summary,
    }