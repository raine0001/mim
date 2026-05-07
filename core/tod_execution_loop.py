from __future__ import annotations

from pathlib import Path
from typing import Any


def build_execution_loop_contract_artifacts(
    *,
    started_at: str,
    source: str,
    surface: str,
    session_key: str,
    request_id: str,
    task_id: str,
    execution_id: str,
    objective_id: str,
    normalized_objective_id: str,
    title: str,
    summary: str,
    task_focus: str,
    mission: str,
    primary_outcome: str,
    strongest_evidence: str,
    next_validation: str,
) -> dict[str, Any]:
    inspection_step = {
        "step_id": "step-1-inspect-repo-slice",
        "title": "Inspect repo for smallest execution-loop slice",
        "status": "planned",
        "summary": "Inspect the current repo surfaces and identify the smallest implementation slice for the TOD execution loop contract.",
        "expected_outputs": [
            "task intake",
            "bounded step planner",
            "command runner",
            "patch writer",
            "validator",
            "result publisher",
        ],
    }
    implementation_step = {
        "step_id": "step-2-prepare-bounded-patch",
        "title": "Prepare first bounded execution-loop patch",
        "status": "planned",
        "summary": "Prepare the first bounded patch for the inspected execution-loop surfaces and carry it through focused validation.",
        "expected_outputs": [
            "target file edits",
            "focused unittest command",
            "updated execution evidence",
        ],
    }
    planned_steps = [inspection_step, implementation_step]
    execution_contract = {
        "contract_version": "tod-execution-loop-v1",
        "status": "accepted",
        "task_intake": {
            "status": "accepted",
            "task_focus": task_focus,
            "title": title,
            "mission": mission,
            "primary_outcome": primary_outcome,
            "strongest_evidence": strongest_evidence,
        },
        "bounded_step_planner": {
            "status": "ready",
            "active_step": inspection_step,
            "planned_steps": planned_steps,
            "next_step_id": implementation_step["step_id"],
            "next_validation": next_validation,
        },
        "command_runner": {
            "status": "pending",
            "summary": "No local command has been executed yet for this accepted task.",
        },
        "patch_writer": {
            "status": "pending",
            "summary": "No patch has been prepared yet for this accepted task.",
        },
        "validator": {
            "status": "pending",
            "target": next_validation,
            "summary": f"Validation is pending for {task_focus}.",
        },
        "result_publisher": {
            "status": "active",
            "artifacts": [
                "TOD_ACTIVE_OBJECTIVE.latest.json",
                "TOD_ACTIVE_TASK.latest.json",
                "TOD_ACTIVITY_STREAM.latest.json",
                "TOD_VALIDATION_RESULT.latest.json",
            ],
        },
    }
    base_payload = {
        "generated_at": started_at,
        "source": source,
        "surface": surface,
        "session_key": session_key,
        "request_id": request_id,
        "task_id": task_id,
        "execution_id": execution_id,
        "objective_id": objective_id,
        "normalized_objective_id": normalized_objective_id,
        "title": title,
        "summary": summary,
        "execution_contract": execution_contract,
    }
    active_objective_payload = {
        **base_payload,
        "packet_type": "tod-active-objective-v1",
        "mission": mission,
        "primary_outcome": primary_outcome,
        "status": "active",
    }
    active_task_payload = {
        **base_payload,
        "packet_type": "tod-active-task-v1",
        "task_focus": task_focus,
        "status": "accepted",
        "current_action": "Publishing local execution confirmation and phase-1 execution artifacts.",
        "next_step": "Continue the task through bounded step execution, validation, evidence publication, and next-step selection.",
        "next_validation": next_validation,
    }
    activity_event = {
        **base_payload,
        "packet_type": "tod-activity-stream-v1",
        "event": "task_accepted",
        "status": "running",
        "phase": "task_intake",
        "strongest_evidence": strongest_evidence,
        "current_action": active_task_payload["current_action"],
        "next_step": active_task_payload["next_step"],
        "next_validation": next_validation,
    }
    validation_payload = {
        **base_payload,
        "packet_type": "tod-validation-result-v1",
        "status": "pending",
        "validation_target": next_validation,
        "summary": f"Validation is pending for {task_focus}.",
    }
    execution_result_payload = {
        **base_payload,
        "packet_type": "tod-execution-result-v1",
        "execution_state": "accepted",
        "status": "running",
        "current_action": active_task_payload["current_action"],
    }
    execution_truth_payload = {
        "generated_at": started_at,
        "source": source,
        "summary": {
            "execution_count": 1,
            "latest_execution_at": started_at,
            "objective_id": objective_id,
            "task_id": task_id,
            "request_id": request_id,
            "summary": summary,
        },
        "recent_execution_truth": [
            {
                "generated_at": started_at,
                "objective_id": objective_id,
                "task_id": task_id,
                "execution_id": execution_id,
                "request_id": request_id,
                "execution_state": "accepted",
                "status": "running",
                "summary": summary,
                "current_action": active_task_payload["current_action"],
                "next_validation": next_validation,
                "execution_contract": execution_contract,
            }
        ],
    }
    return {
        "base_payload": base_payload,
        "active_objective_payload": active_objective_payload,
        "active_task_payload": active_task_payload,
        "activity_event": activity_event,
        "validation_payload": validation_payload,
        "execution_result_payload": execution_result_payload,
        "execution_truth_payload": execution_truth_payload,
    }


def execute_bounded_local_inspection(
    *,
    workspace_root: str | Path,
    project_root: str | Path,
    task_focus: str,
    next_validation: str,
) -> dict[str, Any]:
    workspace_path = Path(workspace_root)
    project_path = Path(project_root)
    inspection_root = project_path
    required_candidates: list[Path] = []
    optional_candidates: list[Path] = []

    if (project_path / "core" / "tod_execution_loop.py").exists() or (project_path / "core" / "routers" / "tod_ui.py").exists():
        required_candidates.extend(
            [
                project_path / "core" / "tod_execution_loop.py",
                project_path / "core" / "routers" / "tod_ui.py",
            ]
        )
        if (project_path / "tests" / "integration").exists():
            optional_candidates.append(project_path / "tests" / "integration" / "test_tod_ui_console.py")

    if (workspace_path / "tmp_remote_mim").exists():
        inspection_root = workspace_path
        optional_candidates.extend(
            [
                workspace_path / "tmp_remote_mim" / "tests" / "integration" / "test_tod_ui_console.py",
                workspace_path / "scripts" / "Deploy-TodUiConsoleChat.ps1",
            ]
        )
    elif (workspace_path / "scripts").exists():
        optional_candidates.append(workspace_path / "scripts" / "Deploy-TodUiConsoleChat.ps1")

    if not required_candidates and project_path.name == "tmp_remote_mim":
        required_candidates.extend(
            [
                project_path / "core" / "tod_execution_loop.py",
                project_path / "core" / "routers" / "tod_ui.py",
            ]
        )
        inspection_root = project_path.parent

    seen_paths: set[str] = set()
    candidate_paths: list[tuple[Path, bool]] = []
    for path in required_candidates:
        normalized = str(path)
        if normalized in seen_paths:
            continue
        seen_paths.add(normalized)
        candidate_paths.append((path, True))
    for path in optional_candidates:
        normalized = str(path)
        if normalized in seen_paths:
            continue
        seen_paths.add(normalized)
        candidate_paths.append((path, False))

    validation_checks: list[dict[str, Any]] = []
    matched_files: list[str] = []
    required_total = 0
    required_matched = 0
    optional_total = 0
    optional_matched = 0
    for path, required in candidate_paths:
        exists = path.exists()
        if required:
            required_total += 1
            if exists:
                required_matched += 1
        else:
            optional_total += 1
            if exists:
                optional_matched += 1
        validation_checks.append(
            {
                "name": f"exists:{path.name}",
                "path": str(path),
                "passed": exists,
                "required": required,
            }
        )
        if exists:
            matched_files.append(str(path))

    inspected_paths = [str(path) for path, _required in candidate_paths]
    passed = all(bool(item.get("passed")) for item in validation_checks if bool(item.get("required")))
    command_output = (
        f"Inspected {len(inspected_paths)} local execution-loop surfaces under {inspection_root}; matched {required_matched}/{required_total} required files and {optional_matched}/{optional_total} optional files."
    )
    summary = (
        f"Completed the bounded local workspace inspection for {task_focus} and identified the owning router, helper, test, and deploy surfaces."
    )
    next_step = (
        "Implement the next bounded execution-loop slice in the inspected surfaces and rerun the focused validation path."
    )
    wait_reason = (
        "The next bounded implementation slice has not been dispatched through execute-chat-task into "
        "scripts/engines/LocalExecutionEngine.ps1::Invoke-LocalExecutionEngine."
    )
    return {
        "step_id": "step-1-inspect-repo-slice",
        "step_title": "Inspect repo for smallest execution-loop slice",
        "status": "completed" if passed else "blocked",
        "summary": summary,
        "current_action": "Completed local workspace inspection and published execution evidence for the owning slice.",
        "next_step": next_step,
        "next_validation": next_validation,
        "wait_target": "scripts/engines/LocalExecutionEngine.ps1::Invoke-LocalExecutionEngine",
        "wait_target_label": "scripts/engines/LocalExecutionEngine.ps1::Invoke-LocalExecutionEngine",
        "wait_reason": wait_reason,
        "command_output": command_output,
        "workspace_root": str(workspace_path),
        "project_root": str(project_path),
        "inspected_paths": inspected_paths,
        "matched_files": matched_files,
        "validation_checks": validation_checks,
        "validation_passed": passed,
        "files_changed": [],
        "rollback_state": "not_needed",
        "recovery_state": "not_needed",
    }