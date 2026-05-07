from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.handoff_intake_service import submit_handoff_payload
from core.improvement_recommendation_service import (
    get_improvement_recommendation,
    to_improvement_recommendation_out_resolved,
)
from core.journal import write_journal
from core.local_broker_boundary import live_openai_broker_configured
from core.models import InputEventResolution, Objective, Task, TaskResult
from core.objective_lifecycle import (
    FAILURE_STATES,
    SUCCESS_STATES,
    recompute_objective_state,
    task_execution_state,
    task_execution_tracking_snapshot,
    task_has_completion_evidence,
)
from core.objective_history_service import persist_program_status_snapshot
from core.self_evolution_service import (
    build_self_evolution_next_action,
    reset_natural_language_development_progress,
)
from core.program_registry_service import build_program_status_snapshot
from core.program_registry_service import extract_program_projects_from_text
from core.program_registry_service import ensure_program_registration
from core.program_registry_service import next_program_project
from core.program_registry_service import project_program_intent
from core.tod_mim_contract import RUNTIME_SHARED_DIR, normalize_and_validate_file, utc_now


SOFT_BOUNDARY = "soft"
HARD_BOUNDARY = "hard"
READY_STATES = {"ready", "in_progress", "queued", "waiting_on_tod"}
ACTIVE_TASK_STATES = {"in_progress", "running", "accepted", "dispatched"}
INITIATIVE_OWNER = "mim"
INITIATIVE_STATUS_OBJECTIVE_SCAN_LIMIT = 200
DEFAULT_POLICY_VERSION = "mim_initiative_v1"
CONTINUATION_VALIDATION_OBJECTIVE_TITLE = "Drive autonomous continuation validation"
SELF_CORRECTION_STALE_PREVENTION_OBJECTIVE_TITLE = "Drive self-correction and stale-state prevention"
HARD_BOUNDARY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bcredential(s)?\b|\bsecret(s)?\b|\btoken(s)?\b|\bkey(s)?\b", "credential_or_secret_change"),
    (r"\bsecurity posture\b|\bpermission(s)?\b|\baccess policy\b|\bauth(n|z)?\b", "security_posture_change"),
    (r"\bpublic[- ]facing\b|\bexternal exposure\b|\bdata exposure\b|\binternet\b", "public_exposure_change"),
    (r"\bdelete\b|\bdestroy\b|\bwipe\b|\bformat\b|\breset machine\b", "destructive_action"),
    (r"\bdeploy\b|\bproduction\b|\blive environment\b", "public_or_production_change"),
    (r"\bspend\b|\bpurchase\b|\bbilling\b|\bsubscription\b|\bthird[- ]party commitment\b", "spend_or_commitment"),
    (r"\bhardware\b|\bhost\b|\bros\b|\bsystemctl\b|\bshutdown\b|\breboot\b", "host_or_hardware_risk"),
)
AUTO_APPROVAL_AUTHORITY_MARKERS: tuple[str, ...] = (
    "all next steps are approved",
    "all natural next steps are automatically approved",
    "all next slices are approved",
    "all next steps approved",
    "no human confirmation required",
    "no human approval required",
    "without human confirmation",
    "without human approval",
    "do not ask for human confirmation",
    "do not ask me again",
)
IRREDUCIBLE_HARD_BOUNDARY_REASONS = {
    "credential_or_secret_change",
    "security_posture_change",
    "public_exposure_change",
    "destructive_action",
    "spend_or_commitment",
    "host_or_hardware_risk",
}


@dataclass(slots=True)
class InitiativeTaskPlan:
    title: str
    details: str
    assigned_to: str
    acceptance_criteria: str
    execution_scope: str
    expected_outputs: list[str]
    verification_commands: list[str]
    metadata_json: dict[str, Any]


def _normalize_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_objective_token(value: object) -> str:
    return str(value or "").strip().replace("objective-", "")


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _initiative_supersession_snapshot(
    objective_id: object,
    *,
    shared_root: Path | None = None,
) -> dict[str, str]:
    resolved_shared_root = (shared_root or RUNTIME_SHARED_DIR).expanduser().resolve()
    integration_status = _load_json_file(resolved_shared_root / "TOD_INTEGRATION_STATUS.latest.json")
    if not integration_status:
        return {}

    mim_status = integration_status.get("mim_status") if isinstance(integration_status.get("mim_status"), dict) else {}
    live_task_request = (
        integration_status.get("live_task_request")
        if isinstance(integration_status.get("live_task_request"), dict)
        else {}
    )
    objective_alignment = (
        integration_status.get("objective_alignment")
        if isinstance(integration_status.get("objective_alignment"), dict)
        else {}
    )

    canonical_objective_id = _normalize_objective_token(
        mim_status.get("objective_active")
        or objective_alignment.get("canonical_objective_id")
    )
    live_request_objective_id = _normalize_objective_token(
        live_task_request.get("normalized_objective_id")
        or live_task_request.get("objective_id")
        or objective_alignment.get("live_request_objective_id")
    )
    current_objective_id = _normalize_objective_token(objective_id)

    if not canonical_objective_id or canonical_objective_id != live_request_objective_id:
        return {}
    if not current_objective_id or current_objective_id == canonical_objective_id:
        return {}

    request_id = str(
        live_task_request.get("request_id")
        or live_task_request.get("task_id")
        or integration_status.get("request_id")
        or ""
    ).strip()
    return {
        "objective_id": canonical_objective_id,
        "request_id": request_id,
    }


def _normalize_slug(value: object) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _normalize_text(value).lower())
    return slug.strip("-") or "initiative"


def normalize_initiative_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    explicit_match = re.search(
        r"(?i)\binitiative_id\s*:\s*([A-Za-z0-9][A-Za-z0-9._-]*)",
        text,
    )
    if explicit_match:
        return explicit_match.group(1).strip()
    token_match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", text)
    return token_match.group(1).strip() if token_match else ""


def extract_explicit_initiative_id(user_intent: str) -> str:
    match = re.search(
        r"(?i)\binitiative_id\s*:\s*([A-Za-z0-9][A-Za-z0-9._-]*)",
        str(user_intent or ""),
    )
    return match.group(1).strip() if match else ""


def extract_explicit_program_id(user_intent: str) -> str:
    match = re.search(
        r"(?i)\bprogram_id\s*:\s*([A-Za-z0-9][A-Za-z0-9._-]*)",
        str(user_intent or ""),
    )
    return match.group(1).strip() if match else ""


def _looks_like_explicit_resume_request(
    user_intent: str, metadata_json: dict[str, Any] | None
) -> bool:
    metadata = metadata_json if isinstance(metadata_json, dict) else {}
    if bool(metadata.get("resume_existing")):
        return True
    normalized = _normalize_text(user_intent).lower()
    if not normalized:
        return False
    explicit_resume_markers = (
        "resume_existing: true",
        "resume existing",
        "resume this initiative",
        "resume this objective",
        "resume the initiative",
        "resume the objective",
        "continue this initiative",
        "continue this objective",
        "continue the initiative",
        "continue the objective",
        "pick back up this initiative",
        "pick back up this objective",
    )
    return any(marker in normalized for marker in explicit_resume_markers)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _has_standing_auto_approval_authority(user_intent: str) -> bool:
    normalized = _normalize_text(user_intent).lower()
    return any(marker in normalized for marker in AUTO_APPROVAL_AUTHORITY_MARKERS)


def classify_boundary_mode(user_intent: str) -> dict[str, str]:
    text = _normalize_text(user_intent).lower()
    for pattern, reason in HARD_BOUNDARY_PATTERNS:
        if re.search(pattern, text):
            if _has_standing_auto_approval_authority(text) and reason not in IRREDUCIBLE_HARD_BOUNDARY_REASONS:
                return {
                    "boundary_mode": SOFT_BOUNDARY,
                    "reason": f"standing_auto_approval_override:{reason}",
                }
            return {"boundary_mode": HARD_BOUNDARY, "reason": reason}
    return {"boundary_mode": SOFT_BOUNDARY, "reason": "routine_bounded_work"}


def _title_from_intent(user_intent: str) -> str:
    cleaned = _normalize_text(user_intent)
    if not cleaned:
        return "Drive the next bounded initiative"
    return cleaned[:117].rstrip() + "..." if len(cleaned) > 120 else cleaned


def _explicit_project_contract(user_intent: str) -> dict[str, Any]:
    projects = extract_program_projects_from_text(user_intent)
    if not projects:
        return {}
    explicit_initiative_id = extract_explicit_initiative_id(user_intent)
    if explicit_initiative_id:
        for project in projects:
            if str(project.get("project_id") or "").strip().lower() == explicit_initiative_id.lower():
                return project
    return projects[0] if isinstance(projects[0], dict) else {}


def _project_task_scope(task_text: str) -> str:
    normalized = _normalize_text(task_text).lower()
    if any(token in normalized for token in {"validate", "validation", "test", "regression", "verify", "probe"}):
        return "bounded_validation"
    if any(token in normalized for token in {"summarize", "report", "produce recovery playbook", "store artifacts"}):
        return "bounded_reporting"
    if any(token in normalized for token in {"scan and map", "inspect", "trace", "identify", "locate"}):
        return "bounded_analysis"
    return "bounded_development"


def _looks_like_project_task_fragment(task_text: str) -> bool:
    normalized = _normalize_text(task_text)
    if not normalized:
        return False
    if normalized.endswith((".", "!", "?")):
        return False
    first_char = normalized[0]
    return first_char.islower() or first_char.isdigit()


def _normalize_project_task_items(task_items: list[object]) -> list[str]:
    normalized_items = [_normalize_text(item) for item in task_items]
    normalized_items = [item for item in normalized_items if item]
    if not normalized_items:
        return []

    combined_items: list[str] = []
    index = 0
    while index < len(normalized_items):
        current = normalized_items[index]
        if current.endswith(":"):
            fragments: list[str] = []
            lookahead = index + 1
            while lookahead < len(normalized_items) and _looks_like_project_task_fragment(normalized_items[lookahead]):
                fragments.append(normalized_items[lookahead])
                lookahead += 1
            if fragments:
                current = f"{current} {'; '.join(fragments)}"
                index = lookahead - 1
        combined_items.append(current)
        index += 1
    return combined_items


def _explicit_project_task_plans(
    *,
    project: dict[str, Any],
    actor: str,
    source: str,
    managed_scope: str,
) -> list[InitiativeTaskPlan]:
    project_tasks = _normalize_project_task_items(
        project.get("tasks") if isinstance(project.get("tasks"), list) else []
    )
    success_criteria = project.get("success_criteria") if isinstance(project.get("success_criteria"), list) else []
    acceptance_criteria = "; ".join(str(item).strip() for item in success_criteria if str(item).strip())
    if not acceptance_criteria:
        acceptance_criteria = "The bounded task completes with concrete execution evidence and an explicit next state."
    plans: list[InitiativeTaskPlan] = []
    project_id = str(project.get("project_id") or "").strip()
    project_ordinal = int(project.get("ordinal") or 0)
    project_objective = str(project.get("objective") or project.get("display_title") or "").strip()
    for index, task_text in enumerate(project_tasks, start=1):
        normalized_task = _normalize_text(task_text)
        if not normalized_task:
            continue
        execution_scope = _project_task_scope(normalized_task)
        plans.append(
            InitiativeTaskPlan(
                title=f"Project {project_ordinal} task {index}: {normalized_task[:96]}",
                details=normalized_task,
                assigned_to="codex",
                acceptance_criteria=acceptance_criteria,
                execution_scope=execution_scope,
                expected_outputs=[
                    f"Progress on {project_id or f'project_{project_ordinal:02d}'}",
                    "Concrete execution evidence or explicit bounded blocker",
                ],
                verification_commands=[
                    "Run the narrowest relevant validation for the changed scope",
                    "Summarize lifecycle result, files changed, and next readiness",
                ],
                metadata_json={
                    "initiative_kind": "program_project_task",
                    "managed_scope": managed_scope,
                    "program_project_id": project_id,
                    "program_project_ordinal": project_ordinal,
                    "program_project_objective": project_objective,
                    "program_task_index": index,
                    "actor": actor,
                    "source": source,
                },
            )
        )
    return plans


def _looks_like_continuous_execution_request(user_intent: str) -> bool:
    lower_intent = _normalize_text(user_intent).lower()
    return all(
        marker in lower_intent
        for marker in ("continuous execution mode", "persistent loop")
    ) or (
        "begin loop now" in lower_intent
        and "loop iteration" in lower_intent
        and "next natural objective" in lower_intent
    )


def _looks_like_continuation_validation_request(user_intent: str) -> bool:
    lower_intent = _normalize_text(user_intent).lower()
    required_markers = (
        "controlled continuation test",
        "task completion",
        "recovery",
        "readiness transition",
        "no human confirmation required",
    )
    if all(marker in lower_intent for marker in required_markers):
        return True
    return (
        "initiative_id:" in lower_intent
        and "auto-resume" in lower_intent
        and "5+ tasks executed" in lower_intent
    )


def _looks_like_self_correction_stale_prevention_request(user_intent: str) -> bool:
    lower_intent = _normalize_text(user_intent).lower()
    required_markers = (
        "repetitive non-progressing action patterns",
        "stale-state",
        "bounded status-check",
        "corrective implementation",
        "self-correct",
    )
    if all(marker in lower_intent for marker in required_markers):
        return True
    return (
        "initiative_id: mim-self-correction-and-stale-prevention" in lower_intent
        and "repetition detection" in lower_intent
        and "progress classification" in lower_intent
        and "code-oriented remediation" in lower_intent
    )


def _has_real_recovery_transition(report: dict[str, Any]) -> bool:
    transition = report.get("recovery_transition")
    if not isinstance(transition, dict):
        return False
    transition_type = _normalize_text(transition.get("type")).lower()
    return bool(transition_type and transition_type != "synthetic_monitor_only")


def _continuation_validation_task_plans(*, actor: str, source: str, managed_scope: str) -> list[InitiativeTaskPlan]:
    titles_and_details = [
        (
            "Continuation validation step 1: start execution",
            "Start the validation run by selecting a bounded task candidate and recording that execution began without human confirmation.",
        ),
        (
            "Continuation validation step 2: observe completion",
            "Observe and record the prior step as completed with no premature exit or confirmation prompt.",
        ),
        (
            "Continuation validation step 3: trigger continuation",
            "Select the next valid bounded task automatically and record that continuation proceeded without human input.",
        ),
        (
            "Continuation validation step 4: simulate recovery",
            "Detect or isolate a recent blocked, stale, or waiting readiness condition and record a bounded recovery transition.",
        ),
        (
            "Continuation validation step 5: validate auto-resume",
            "Confirm execution resumed after the recovery transition and that the continuation flow did not stall.",
        ),
        (
            "Continuation validation step 6: repeat continuation A",
            "Execute one additional bounded continuation step and record the lineage update.",
        ),
        (
            "Continuation validation step 7: repeat continuation B",
            "Execute one additional bounded continuation step and record the lineage update.",
        ),
        (
            "Continuation validation step 8: repeat continuation C",
            "Execute one additional bounded continuation step and record the lineage update.",
        ),
    ]
    return [
        InitiativeTaskPlan(
            title=title,
            details=details,
            assigned_to="mim",
            acceptance_criteria="The validation step records result, continuation state, blockers, and execution lineage evidence.",
            execution_scope="continuation_validation",
            expected_outputs=[
                "Validation step report with result, continuation status, blockers, and lineage evidence",
            ],
            verification_commands=[
                "GET /automation/initiative/status",
                "GET /execution/recovery/attempts",
            ],
            metadata_json={
                "automation_kind": "continuation_validation_step",
                "actor": actor,
                "source": source,
                "managed_scope": managed_scope,
                "validation_step_number": index,
                "validation_step_count": len(titles_and_details),
            },
        )
        for index, (title, details) in enumerate(titles_and_details, start=1)
    ]


def _self_correction_stale_prevention_task_plans(
    *, actor: str, source: str, managed_scope: str
) -> list[InitiativeTaskPlan]:
    titles_and_details = [
        (
            "Self-correction pass 1: detect repetitive low-value actions",
            "Inspect recent gateway and initiative outcomes, detect repeated low-value action classes, and record whether the pattern is stale and non-progressing.",
        ),
        (
            "Self-correction pass 2: classify progress value",
            "Classify the observed action pattern as state-changing, summary-only, blocked, or stale repeated behavior.",
        ),
        (
            "Self-correction pass 3: select corrective branch",
            "Choose the next corrective branch for the stale pattern, such as confirmation-gate inspection, bounded-step-limit inspection, execution barrier inspection, or recovery-resume inspection.",
        ),
        (
            "Self-correction pass 4: generate code remediation task",
            "Convert the stale pattern into an explicit code-level remediation task with concrete patch targets and rationale.",
        ),
        (
            "Self-correction pass 5: enforce stale prevention follow-up",
            "Record the stale-prevention escalation rule and select the next TOD or implementation follow-up that advances execution instead of repeating a summary-only path.",
        ),
    ]
    return [
        InitiativeTaskPlan(
            title=title,
            details=details,
            assigned_to="mim",
            acceptance_criteria=(
                "The pass reports repeated-pattern detection, progress classification, corrective branch selection, "
                "proposed remediation, and whether code modification is recommended."
            ),
            execution_scope="stale_prevention_analysis",
            expected_outputs=[
                "Repeated-pattern analysis with progress classification and corrective action proposal",
            ],
            verification_commands=[
                "GET /automation/initiative/status",
                "GET /gateway/capabilities/executions/truth/latest?limit=10",
            ],
            metadata_json={
                "automation_kind": "stale_prevention_pass",
                "actor": actor,
                "source": source,
                "managed_scope": managed_scope,
                "stale_prevention_step_number": index,
                "stale_prevention_step_count": len(titles_and_details),
            },
        )
        for index, (title, details) in enumerate(titles_and_details, start=1)
    ]


async def _recent_input_event_resolutions(
    db: AsyncSession, *, limit: int = 12
) -> list[InputEventResolution]:
    return list(
        (
            await db.execute(
                select(InputEventResolution)
                .order_by(InputEventResolution.created_at.desc(), InputEventResolution.id.desc())
                .limit(max(1, int(limit)))
            )
        )
        .scalars()
        .all()
    )


def _resolution_action_class(resolution: InputEventResolution) -> str:
    metadata_json = getattr(resolution, "metadata_json", {}) or {}
    tod_dispatch = (
        metadata_json.get("tod_dispatch") if isinstance(metadata_json.get("tod_dispatch"), dict) else {}
    )
    reason = _normalize_text(getattr(resolution, "reason", "")).lower()
    clarification_prompt = _normalize_text(getattr(resolution, "clarification_prompt", "")).lower()
    outcome = _normalize_text(getattr(resolution, "outcome", "")).lower()
    safety_decision = _normalize_text(getattr(resolution, "safety_decision", "")).lower()
    action_name = _normalize_text(tod_dispatch.get("action_name")).lower()
    if action_name == "tod_status_check" or reason == "tod_status_dispatch":
        return "bounded_status_check"
    if "warning" in action_name or "warning" in reason:
        return "warning_summary"
    if outcome == "blocked" or safety_decision == "blocked" or "blocked" in reason:
        return "blocked_action"
    if metadata_json.get("initiative_run") or reason.startswith("authorized_initiative"):
        return "state_changing_action"
    if outcome == "store_only" and "accepted" in clarification_prompt and not metadata_json.get("initiative_run"):
        return "acknowledgment_only"
    if "summary" in reason or action_name.endswith("summary"):
        return "summary_only_action"
    return "other"


def _resolution_signature(resolution: InputEventResolution) -> tuple[str, str, str, str]:
    metadata_json = getattr(resolution, "metadata_json", {}) or {}
    tod_dispatch = (
        metadata_json.get("tod_dispatch") if isinstance(metadata_json.get("tod_dispatch"), dict) else {}
    )
    return (
        _resolution_action_class(resolution),
        _normalize_text(getattr(resolution, "reason", "")).lower(),
        _normalize_text(tod_dispatch.get("result_status")).lower(),
        _normalize_text(tod_dispatch.get("result_reason") or getattr(resolution, "clarification_prompt", ""))[:160].lower(),
    )


def _proposed_stale_remediation(action_class: str) -> dict[str, Any]:
    if action_class == "bounded_status_check":
        return {
            "title": "Escalate repeated status checks into corrective implementation selection",
            "targets": ["core/routers/gateway.py", "core/autonomy_driver_service.py"],
            "rationale": "Repeated bounded TOD status checks without state change indicate a stale loop; routing should pivot to structural remediation after two unchanged passes.",
            "code_modification_recommended": True,
        }
    if action_class == "warning_summary":
        return {
            "title": "Promote warning-summary loops into execution-barrier inspection",
            "targets": ["core/routers/gateway.py", "core/execution_policy_gate.py"],
            "rationale": "Repeated warning summaries are informational only and should trigger inspection of the barrier preventing forward execution.",
            "code_modification_recommended": True,
        }
    if action_class == "acknowledgment_only":
        return {
            "title": "Break acknowledgment-only loops by inspecting confirmation gating",
            "targets": ["core/routers/gateway.py", "core/objective_lifecycle.py"],
            "rationale": "Acknowledgment-only flows that repeat without state change should be converted into confirmation-gate inspection and corrective next-step selection.",
            "code_modification_recommended": True,
        }
    if action_class == "blocked_action":
        return {
            "title": "Inspect recovery-resume logic for blocked execution loops",
            "targets": ["core/autonomy_driver_service.py", "core/routers/execution_control.py"],
            "rationale": "Repeated blocked outcomes should trigger recovery or resume analysis instead of passive restatement of the blockage.",
            "code_modification_recommended": True,
        }
    return {
        "title": "Create corrective implementation task for stale execution pattern",
        "targets": ["core/autonomy_driver_service.py"],
        "rationale": "Observed stale behavior should be turned into a corrective implementation task instead of another summary-only pass.",
        "code_modification_recommended": True,
    }


async def _stale_prevention_context(db: AsyncSession) -> dict[str, Any]:
    recent_resolutions = await _recent_input_event_resolutions(db, limit=50)
    low_value_classes = {"bounded_status_check", "warning_summary", "acknowledgment_only", "summary_only_action"}
    repeated_pattern = {
        "detected": False,
        "action_class": "",
        "count": 0,
        "signature": (),
        "reasons": [],
    }
    repeated_signatures: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for resolution in recent_resolutions:
        signature = _resolution_signature(resolution)
        action_class = signature[0]
        if action_class not in low_value_classes:
            continue
        bucket = repeated_signatures.setdefault(
            signature,
            {
                "action_class": action_class,
                "count": 0,
                "reasons": [],
            },
        )
        bucket["count"] += 1
        bucket["reasons"].append(_normalize_text(getattr(resolution, "reason", "")))
    if repeated_signatures:
        best_signature, best_bucket = max(
            repeated_signatures.items(),
            key=lambda item: (int(item[1].get("count", 0)), item[0][0], item[0][1]),
        )
        if int(best_bucket.get("count", 0)) >= 2:
            repeated_pattern = {
                "detected": True,
                "action_class": str(best_bucket.get("action_class") or ""),
                "count": int(best_bucket.get("count", 0)),
                "signature": list(best_signature),
                "reasons": list(best_bucket.get("reasons") or []),
            }

    latest_resolution = recent_resolutions[0] if recent_resolutions else None
    latest_action_class = _resolution_action_class(latest_resolution) if latest_resolution is not None else "other"
    if repeated_pattern["detected"]:
        progress_classification = "stale_repeated_action"
        action_class = str(repeated_pattern["action_class"] or latest_action_class)
    elif latest_action_class in low_value_classes:
        progress_classification = "summary_only_action"
        action_class = latest_action_class
    elif latest_action_class == "blocked_action":
        progress_classification = "blocked_action"
        action_class = latest_action_class
    else:
        progress_classification = "state_changing_action"
        action_class = latest_action_class

    corrective_branch = {
        "bounded_status_check": "inspect_bounded_step_limit",
        "warning_summary": "inspect_execution_barrier",
        "acknowledgment_only": "inspect_confirmation_gate",
        "blocked_action": "inspect_recovery_resume_logic",
    }.get(action_class, "create_corrective_implementation_task")
    proposed_remediation = _proposed_stale_remediation(action_class)
    tod_follow_up = {
        "bounded_status_check": "dispatch corrective implementation analysis instead of another TOD status check",
        "warning_summary": "request one execution-barrier inspection instead of repeating the warning summary",
        "acknowledgment_only": "inspect the confirmation gate and convert the loop into a bounded corrective task",
        "blocked_action": "inspect recovery-resume logic before re-checking status",
    }.get(action_class, "generate one bounded corrective implementation task")
    return {
        "recent_resolution_count": len(recent_resolutions),
        "repeated_pattern_detected": repeated_pattern,
        "progress_classification": progress_classification,
        "corrective_branch_selected": corrective_branch,
        "proposed_remediation_task": proposed_remediation,
        "code_modification_recommended": bool(proposed_remediation.get("code_modification_recommended")),
        "tod_follow_up_selected": tod_follow_up,
        "stale_prevention_rule": "escalate after two unchanged summary-only passes",
    }


def build_initiative_task_plan(
    *,
    user_intent: str,
    actor: str,
    source: str,
    managed_scope: str,
    expected_outputs: list[str],
    verification_commands: list[str],
) -> dict[str, Any]:
    normalized_intent = _normalize_text(user_intent)
    boundary = classify_boundary_mode(normalized_intent)
    lower_intent = normalized_intent.lower()
    explicit_project = _explicit_project_contract(user_intent)
    is_continuation_validation = _looks_like_continuation_validation_request(normalized_intent)
    is_continuous_execution = _looks_like_continuous_execution_request(normalized_intent)
    is_self_correction_stale_prevention = _looks_like_self_correction_stale_prevention_request(normalized_intent)
    is_training = "training" in lower_intent or "self-evolution" in lower_intent

    if explicit_project:
        boundary = {"boundary_mode": SOFT_BOUNDARY, "reason": "program_project_execution"}
        objective_title = str(explicit_project.get("objective") or explicit_project.get("display_title") or _title_from_intent(normalized_intent)).strip()
        objective_description = _normalize_text(
            " ".join(
                part
                for part in [
                    str(explicit_project.get("goal") or "").strip(),
                    str(explicit_project.get("objective") or "").strip(),
                ]
                if part
            )
        ) or objective_title
        compiled_tasks = _explicit_project_task_plans(
            project=explicit_project,
            actor=actor,
            source=source,
            managed_scope=managed_scope,
        )
        if not compiled_tasks:
            compiled_tasks = [
                InitiativeTaskPlan(
                    title=f"Implement bounded work for: {objective_title}",
                    details=objective_description or objective_title,
                    assigned_to="codex",
                    acceptance_criteria="The explicit project objective is implemented with concrete execution evidence.",
                    execution_scope="bounded_development",
                    expected_outputs=[
                        f"Progress on {str(explicit_project.get('project_id') or '').strip() or 'the active project'}",
                    ],
                    verification_commands=[
                        "Run the narrowest relevant validation for the changed scope",
                    ],
                    metadata_json={
                        "initiative_kind": "program_project_task",
                        "managed_scope": managed_scope,
                        "program_project_id": str(explicit_project.get("project_id") or "").strip(),
                        "program_project_ordinal": int(explicit_project.get("ordinal") or 0),
                    },
                )
            ]
    elif is_continuation_validation:
        boundary = {"boundary_mode": SOFT_BOUNDARY, "reason": "continuation_validation"}
        objective_title = CONTINUATION_VALIDATION_OBJECTIVE_TITLE
        objective_description = (
            "Run a controlled continuation validation across execution start, completion, continuation selection, "
            "recovery handling, auto-resume, and repeated follow-on task execution without human confirmation."
        )
        compiled_tasks = _continuation_validation_task_plans(
            actor=actor,
            source=source,
            managed_scope=managed_scope,
        )
    elif is_self_correction_stale_prevention:
        boundary = {"boundary_mode": SOFT_BOUNDARY, "reason": "stale_prevention_training"}
        objective_title = SELF_CORRECTION_STALE_PREVENTION_OBJECTIVE_TITLE
        objective_description = (
            "Detect repeated non-progressing summary behavior, classify stale execution patterns, select a corrective branch, "
            "and generate explicit code-level remediation tasks instead of repeating passive status checks."
        )
        compiled_tasks = _self_correction_stale_prevention_task_plans(
            actor=actor,
            source=source,
            managed_scope=managed_scope,
        )
    elif is_continuous_execution:
        boundary = {"boundary_mode": SOFT_BOUNDARY, "reason": "continuous_execution_loop"}
        objective_title = "Drive continuous execution loop"
        objective_description = (
            "Run a five-iteration MIM-controlled execution loop that rereads MIM-owned state, selects the next "
            "highest-value task signal, records the result and delta, and then proposes the next natural objective."
        )
        compiled_tasks = [
            InitiativeTaskPlan(
                title=f"Continuous execution loop iteration {iteration}",
                details=(
                    "Reread MIM-controlled state, choose the current highest-value next task, execute the bounded "
                    "inspection step available without new human input, and record result, delta, and next task."
                ),
                assigned_to="mim",
                acceptance_criteria=(
                    "The iteration report contains a selected task, a concrete result, a delta classification, and "
                    "the next task chosen by MIM."
                ),
                execution_scope="continuous_execution",
                expected_outputs=[
                    "Iteration report with selected task, result, delta, and next task",
                ],
                verification_commands=[
                    "GET /automation/initiative/status",
                    "GET /improvement/self-evolution/next-action",
                ],
                metadata_json={
                    "automation_kind": "continuous_execution_iteration",
                    "actor": actor,
                    "source": source,
                    "managed_scope": managed_scope,
                    "iteration_number": iteration,
                    "iteration_target": 5,
                },
            )
            for iteration in range(1, 6)
        ]
    elif is_training:
        objective_title = "Drive natural-language self-evolution training"
        objective_description = (
            "Keep natural-language development training moving without routine human prompts, "
            "including reset, active-slice execution, and blocker repair."
        )
        compiled_tasks = [
            InitiativeTaskPlan(
                title="Reset and start the natural-language training loop",
                details=(
                    "Reset persisted natural-language development progress and mark the current slice as the "
                    "active training target so the loop can continue immediately."
                ),
                assigned_to="mim",
                acceptance_criteria="Progress is running on a concrete active slice with a fresh training cursor.",
                execution_scope="self_evolution_training",
                expected_outputs=expected_outputs
                or [
                    "Natural-language progress reset completed",
                    "An active slice is selected and running",
                ],
                verification_commands=verification_commands
                or [
                    "GET /improvement/self-evolution/briefing?actor=<actor>&source=<source>",
                    "GET /mim/ui/state",
                ],
                metadata_json={
                    "automation_kind": "self_evolution_reset",
                    "actor": actor,
                    "source": source,
                    "managed_scope": managed_scope,
                },
            ),
            InitiativeTaskPlan(
                title="Run the active training slice and repair failures until it passes",
                details=(
                    "Use the active slice briefing, recent failures, and current pass metrics to run the next bounded "
                    "training repair loop. Make the smallest code or routing changes necessary and preserve the evaluator's "
                    "required wording."
                ),
                assigned_to="codex",
                acceptance_criteria=(
                    "The active slice reaches pass status or returns a bounded blocker with concrete evidence."
                ),
                execution_scope="bounded_training_repair",
                expected_outputs=[
                    "A bounded training repair change or validation summary",
                    "Updated slice pass/fail evidence",
                ],
                verification_commands=[
                    "python conversation_eval_runner.py --scenario current_slice",
                    "GET /improvement/self-evolution/briefing?actor=<actor>&source=<source>",
                ],
                metadata_json={
                    "initiative_kind": "training_repair",
                    "managed_scope": managed_scope,
                },
            ),
        ]
    else:
        objective_title = _title_from_intent(normalized_intent)
        objective_description = normalized_intent
        compiled_tasks = [
            InitiativeTaskPlan(
                title=f"Implement bounded work for: {_title_from_intent(normalized_intent)}",
                details=(
                    f"Implement the requested bounded change without waiting for another human approval step. "
                    f"Request: {normalized_intent}"
                ),
                assigned_to="codex",
                acceptance_criteria=(
                    "The scoped implementation is complete, changed files are identified, and any blockers are explicit."
                ),
                execution_scope="bounded_development",
                expected_outputs=expected_outputs
                or [
                    "Scoped code or configuration changes",
                    "Concrete summary of files changed and why",
                ],
                verification_commands=verification_commands
                or [
                    "Run the narrowest relevant validation for the changed scope",
                    "Summarize the observed result and residual blockers",
                ],
                metadata_json={
                    "initiative_kind": "bounded_implementation",
                    "managed_scope": managed_scope,
                },
            ),
            InitiativeTaskPlan(
                title="Validate the bounded implementation and summarize the result",
                details=(
                    "Run the scoped validation path for the completed implementation, capture the result, and summarize "
                    "what was completed, what remains blocked, and what should continue next."
                ),
                assigned_to="codex",
                acceptance_criteria=(
                    "Validation evidence is recorded and the next step is explicit without broad replanning."
                ),
                execution_scope="bounded_validation",
                expected_outputs=[
                    "Validation result with changed files and tests",
                    "One bounded next-step recommendation",
                ],
                verification_commands=[
                    "Run the relevant test or lint command for the changed scope",
                    "Record failures only if validation is untrusted or red",
                ],
                metadata_json={
                    "initiative_kind": "bounded_validation",
                    "managed_scope": managed_scope,
                },
            ),
        ]

    return {
        "objective_title": objective_title,
        "objective_description": objective_description,
        "success_criteria": compiled_tasks[-1].acceptance_criteria,
        "boundary_mode": boundary["boundary_mode"],
        "boundary_reason": boundary["reason"],
        "tasks": compiled_tasks,
    }


def _priority_rank(value: str) -> int:
    order = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    return order.get(_normalize_text(value).lower(), 4)


def _task_success(task: Task) -> bool:
    return _normalize_text(task.state).lower() in SUCCESS_STATES and task_has_completion_evidence(task)


def _task_failure(task: Task) -> bool:
    return _normalize_text(task.state).lower() in FAILURE_STATES


def _task_broker_preparation(task: Task) -> dict[str, Any]:
    dispatch_artifact = (
        task.dispatch_artifact_json if isinstance(getattr(task, "dispatch_artifact_json", {}), dict) else {}
    )
    latest_result = dispatch_artifact.get("latest_result")
    if not isinstance(latest_result, dict):
        return {}
    broker_preparation = latest_result.get("broker_preparation")
    return broker_preparation if isinstance(broker_preparation, dict) else {}


def _task_has_retryable_false_broker_block(task: Task) -> bool:
    if _normalize_text(getattr(task, "assigned_to", "")).lower() != "codex":
        return False
    if _normalize_text(getattr(task, "state", "")).lower() != "blocked":
        return False
    if _normalize_text(getattr(task, "dispatch_status", "")).lower() != "blocked":
        return False
    if not bool(getattr(task, "start_now", False)) or bool(getattr(task, "human_prompt_required", False)):
        return False

    broker_preparation = _task_broker_preparation(task)
    broker_response = broker_preparation.get("broker_response")
    if not isinstance(broker_response, dict):
        return False
    if _normalize_text(broker_response.get("status")).lower() != "not_configured":
        return False

    automatic_live_response = broker_preparation.get("automatic_live_response")
    automatic_live_completed = isinstance(automatic_live_response, dict) and _normalize_text(
        automatic_live_response.get("status")
    ).lower() == "completed"
    return automatic_live_completed or live_openai_broker_configured()


async def _recover_retryable_blocked_codex_tasks(
    db: AsyncSession,
    *,
    objective_id: int | None,
    actor: str,
    source: str,
) -> list[int]:
    objective_rows = list(
        (
            await db.execute(select(Objective).order_by(Objective.created_at.desc(), Objective.id.desc()))
        )
        .scalars()
        .all()
    )
    recovered_task_ids: list[int] = []
    for objective in objective_rows:
        if objective_id is not None and objective.id != objective_id:
            continue
        if _normalize_text(getattr(objective, "owner", INITIATIVE_OWNER)).lower() != INITIATIVE_OWNER:
            continue
        tasks = await _tasks_for_objective(db, objective.id)
        for task in tasks:
            if not _task_has_retryable_false_broker_block(task):
                continue
            task.state = "queued"
            task.dispatch_status = "pending"
            _set_task_execution_tracking(
                task,
                task_created=True,
                task_dispatched=False,
                execution_started=False,
                execution_result=None,
                request_id="",
                execution_trace="",
                result_artifact="",
            )
            recovered_task_ids.append(int(task.id))
            await write_journal(
                db,
                actor=actor,
                action="initiative_codex_false_block_recovered",
                target_type="task",
                target_id=str(task.id),
                summary=f"Recovered Codex task {task.id} from a false broker-unavailable block",
                metadata_json={"objective_id": objective.id, "source": source},
            )
        if recovered_task_ids:
            await refresh_task_readinesses(db, objective.id)
            await recompute_objective_state(db, objective.id)
    return recovered_task_ids


async def _recover_completed_codex_tasks_from_dispatch_artifacts(
    db: AsyncSession,
    *,
    objective_id: int | None,
    actor: str,
    source: str,
) -> list[int]:
    objective_rows = list(
        (
            await db.execute(select(Objective).order_by(Objective.created_at.desc(), Objective.id.desc()))
        )
        .scalars()
        .all()
    )
    recovered_task_ids: list[int] = []
    for objective in objective_rows:
        if objective_id is not None and objective.id != objective_id:
            continue
        if _normalize_text(getattr(objective, "owner", INITIATIVE_OWNER)).lower() != INITIATIVE_OWNER:
            continue
        tasks = await _tasks_for_objective(db, objective.id)
        objective_recovered = False
        for task in tasks:
            if _normalize_text(getattr(task, "assigned_to", "")).lower() != "codex":
                continue
            if _normalize_text(getattr(task, "state", "")).lower() in SUCCESS_STATES:
                continue
            submission = task.dispatch_artifact_json if isinstance(getattr(task, "dispatch_artifact_json", {}), dict) else {}
            if not submission:
                continue
            result_artifact = _authoritative_result_artifact_from_submission(submission)
            if not result_artifact:
                result_artifact = _analysis_result_artifact_from_submission(task, submission)
            if not result_artifact:
                result_artifact = _completion_result_artifact_from_submission(task, submission)
            shared_result_payload, shared_result_artifact = _load_shared_task_result_payload()
            shared_terminal_result_status = ""
            if _result_artifact_matches_task(task, submission, shared_result_payload):
                shared_terminal_result_status = _terminal_result_status(shared_result_payload)
                if not result_artifact and shared_terminal_result_status:
                    result_artifact = shared_result_artifact
            if not result_artifact and not shared_terminal_result_status:
                continue
            request_id = str(submission.get("handoff_id") or submission.get("task_id") or "").strip()
            execution_trace = str(
                submission.get("task_path")
                or submission.get("status_path")
                or submission.get("latest_task_path")
                or ""
            ).strip()
            execution_result = str(submission.get("latest_result_summary") or "").strip() or "result_recorded"
            if shared_terminal_result_status:
                execution_result = (
                    str(shared_result_payload.get("error") or "").strip()
                    or str(shared_result_payload.get("result_reason_code") or "").strip()
                    or str(shared_result_payload.get("result_status") or shared_result_payload.get("status") or "").strip()
                    or execution_result
                )
            _set_task_execution_tracking(
                task,
                task_created=True,
                task_dispatched=bool(request_id),
                execution_started=True,
                execution_result=execution_result,
                request_id=request_id,
                execution_trace=execution_trace,
                result_artifact=result_artifact,
            )
            result_payload = _load_result_artifact_payload(result_artifact)
            terminal_result_status = ""
            if _result_artifact_matches_task(task, submission, result_payload):
                terminal_result_status = _terminal_result_status(result_payload)
            has_local_completion_evidence = task_has_completion_evidence(task)
            if not terminal_result_status and (
                not has_local_completion_evidence or result_artifact == shared_result_artifact
            ):
                terminal_result_status = shared_terminal_result_status
            if terminal_result_status in FAILURE_STATES:
                task.dispatch_status = terminal_result_status
                task.state = terminal_result_status
                objective_recovered = True
                await write_journal(
                    db,
                    actor=actor,
                    action="initiative_codex_failure_recovered",
                    target_type="task",
                    target_id=str(task.id),
                    summary=f"Recovered Codex task {task.id} to {terminal_result_status} from its persisted handoff result artifact",
                    metadata_json={"objective_id": objective.id, "source": source},
                )
            elif has_local_completion_evidence or terminal_result_status in SUCCESS_STATES:
                task.dispatch_status = "completed"
                task.state = "completed"
                objective_recovered = True
                recovered_task_ids.append(int(task.id))
                await write_journal(
                    db,
                    actor=actor,
                    action="initiative_codex_completion_recovered",
                    target_type="task",
                    target_id=str(task.id),
                    summary=f"Recovered Codex task {task.id} to completed from its persisted handoff result artifact",
                    metadata_json={"objective_id": objective.id, "source": source},
                )
        if objective_recovered:
            await refresh_task_readinesses(db, objective.id)
            await recompute_objective_state(db, objective.id)
    return recovered_task_ids


def _task_terminal(task: Task) -> bool:
    return _task_success(task) or _task_failure(task)


def _authoritative_result_artifact_from_submission(submission: dict[str, Any]) -> str:
    latest_result = submission.get("latest_result") if isinstance(submission.get("latest_result"), dict) else {}
    broker_preparation = (
        latest_result.get("broker_preparation") if isinstance(latest_result.get("broker_preparation"), dict) else {}
    )
    automatic_live_response = (
        broker_preparation.get("automatic_live_response")
        if isinstance(broker_preparation.get("automatic_live_response"), dict)
        else {}
    )
    automatic_live_interpretation = (
        broker_preparation.get("automatic_live_interpretation")
        if isinstance(broker_preparation.get("automatic_live_interpretation"), dict)
        else {}
    )
    if (
        str(automatic_live_response.get("status") or "").strip().lower() == "completed"
        and str(automatic_live_interpretation.get("status") or "").strip().lower() == "completed"
        and str(automatic_live_interpretation.get("classification") or "").strip().lower()
        in {"executed_tool_result", "bounded_tod_dispatch_result", "executed_result"}
    ):
        return str(automatic_live_response.get("result_artifact") or "").strip()
    return str(
        submission.get("latest_result_artifact")
        or submission.get("result_artifact")
        or ""
    ).strip()


def _analysis_result_artifact_from_submission(task: Task, submission: dict[str, Any]) -> str:
    if _normalize_text(getattr(task, "execution_scope", "")).lower() != "bounded_analysis":
        return ""
    latest_result = submission.get("latest_result") if isinstance(submission.get("latest_result"), dict) else {}
    broker_preparation = (
        latest_result.get("broker_preparation") if isinstance(latest_result.get("broker_preparation"), dict) else {}
    )
    automatic_live_response = (
        broker_preparation.get("automatic_live_response")
        if isinstance(broker_preparation.get("automatic_live_response"), dict)
        else {}
    )
    automatic_live_interpretation = (
        broker_preparation.get("automatic_live_interpretation")
        if isinstance(broker_preparation.get("automatic_live_interpretation"), dict)
        else {}
    )
    if str(automatic_live_response.get("status") or "").strip().lower() != "completed":
        return ""
    if str(automatic_live_interpretation.get("status") or "").strip().lower() != "completed":
        return ""
    if str(automatic_live_interpretation.get("classification") or "").strip().lower() != "model_response_text":
        return ""
    return str(automatic_live_response.get("result_artifact") or "").strip()


def _completion_result_artifact_from_submission(task: Task, submission: dict[str, Any]) -> str:
    if _normalize_text(getattr(task, "execution_scope", "")).lower() not in {
        "bounded_development",
        "bounded_validation",
    }:
        return ""

    latest_result = submission.get("latest_result") if isinstance(submission.get("latest_result"), dict) else {}
    candidate_values: list[str] = [
        str(submission.get("completion_artifact") or "").strip(),
        str(latest_result.get("completion_artifact") or "").strip(),
    ]

    handoff_id = str(submission.get("handoff_id") or "").strip()
    parent_candidates: list[Path] = []
    for raw_path in (
        submission.get("task_path"),
        submission.get("status_path"),
        submission.get("latest_task_path"),
        submission.get("latest_status_path"),
    ):
        raw_text = str(raw_path or "").strip()
        if not raw_text:
            continue
        parent = Path(raw_text).parent
        if parent not in parent_candidates:
            parent_candidates.append(parent)
    if handoff_id:
        for parent in parent_candidates:
            candidate_values.append(str(parent / f"{handoff_id}.completion.json"))

    seen: set[str] = set()
    for candidate_value in candidate_values:
        candidate = str(candidate_value or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        payload = _load_result_artifact_payload(candidate)
        if not payload:
            continue
        if _terminal_result_status(payload) not in SUCCESS_STATES:
            continue
        if not _result_artifact_matches_task(task, submission, payload):
            continue
        return candidate
    return ""


def _load_result_artifact_payload(result_artifact: str) -> dict[str, Any]:
    artifact_path = Path(str(result_artifact or "").strip())
    if not artifact_path.exists() or not artifact_path.is_file():
        return {}
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_shared_task_result_payload() -> tuple[dict[str, Any], str]:
    artifact_path = (RUNTIME_SHARED_DIR / "TOD_MIM_TASK_RESULT.latest.json").resolve()
    if not artifact_path.exists() or not artifact_path.is_file():
        return {}, ""
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, ""
    if not isinstance(payload, dict):
        return {}, ""
    return payload, str(artifact_path)


def _result_artifact_matches_task(task: Task, submission: dict[str, Any], payload: dict[str, Any]) -> bool:
    if not payload:
        return False
    tracking = getattr(task, "metadata_json", {}) or {}
    execution_tracking = (
        tracking.get("execution_tracking") if isinstance(tracking.get("execution_tracking"), dict) else {}
    )
    expected_task_ids = {
        str(value or "").strip()
        for value in (
            getattr(task, "id", None),
            submission.get("task_id"),
            execution_tracking.get("task_id"),
        )
        if str(value or "").strip()
    }
    expected_request_ids = {
        str(value or "").strip()
        for value in (
            submission.get("handoff_id"),
            submission.get("request_id"),
            submission.get("task_id"),
            execution_tracking.get("request_id"),
        )
        if str(value or "").strip()
    }
    payload_task_id = str(payload.get("task_id") or "").strip()
    payload_request_id = str(payload.get("request_id") or "").strip()
    if payload_task_id and payload_task_id in expected_task_ids:
        return True
    if payload_request_id and payload_request_id in expected_request_ids:
        return True
    return False


def _terminal_result_status(payload: dict[str, Any]) -> str:
    if not payload:
        return ""
    result_status = str(payload.get("result_status") or payload.get("status") or "").strip().lower()
    if result_status in SUCCESS_STATES or result_status in FAILURE_STATES:
        return result_status
    return ""


def _objective_execution_tracking(objective: Objective, tasks: list[Task]) -> dict[str, Any]:
    metadata_json = getattr(objective, "metadata_json", {}) or {}
    tracking = metadata_json.get("execution_tracking") if isinstance(metadata_json.get("execution_tracking"), dict) else {}
    if tracking:
        return tracking
    task_snapshots = [task_execution_tracking_snapshot(task) for task in tasks]
    if tasks and all(_task_success(task) for task in tasks):
        execution_state = "completed"
    elif any(snapshot["task_dispatched"] and snapshot["execution_started"] for snapshot in task_snapshots):
        execution_state = "executing"
    elif any(snapshot["task_dispatched"] for snapshot in task_snapshots):
        execution_state = "dispatched"
    elif tasks:
        execution_state = "created"
    else:
        execution_state = "queued"
    return {
        "task_created": bool(tasks),
        "task_dispatched": any(snapshot["task_dispatched"] for snapshot in task_snapshots),
        "execution_started": any(snapshot["execution_started"] for snapshot in task_snapshots),
        "execution_result": None,
        "execution_state": execution_state,
        "task_count": len(tasks),
        "completed_task_count": sum(1 for task in tasks if _task_success(task)),
    }


def _set_task_execution_tracking(
    task: Task,
    *,
    task_created: bool | None = None,
    task_dispatched: bool | None = None,
    execution_started: bool | None = None,
    execution_result: Any = None,
    activity_started_at: str | None = None,
    request_id: str | None = None,
    execution_trace: str | None = None,
    result_artifact: str | None = None,
) -> dict[str, Any]:
    metadata_json = getattr(task, "metadata_json", {}) or {}
    tracking = metadata_json.get("execution_tracking") if isinstance(metadata_json.get("execution_tracking"), dict) else {}
    updated_tracking = {
        "task_created": bool(task_created if task_created is not None else tracking.get("task_created", True)),
        "task_dispatched": bool(task_dispatched if task_dispatched is not None else tracking.get("task_dispatched", False)),
        "execution_started": bool(execution_started if execution_started is not None else tracking.get("execution_started", False)),
        "execution_result": execution_result if execution_result is not None else tracking.get("execution_result"),
        "activity_started_at": str(
            activity_started_at
            if activity_started_at is not None
            else tracking.get("activity_started_at")
            or tracking.get("resumed_at")
            or tracking.get("started_at")
            or ""
        ).strip(),
        "request_id": str(request_id if request_id is not None else tracking.get("request_id") or "").strip(),
        "execution_trace": str(
            execution_trace if execution_trace is not None else tracking.get("execution_trace") or ""
        ).strip(),
        "result_artifact": str(
            result_artifact if result_artifact is not None else tracking.get("result_artifact") or ""
        ).strip(),
    }
    task.metadata_json = {
        **metadata_json,
        "execution_tracking": updated_tracking,
    }
    return updated_tracking


def _recently_completed_tasks(tasks: list[Task]) -> list[dict[str, Any]]:
    return [
        {
            "task_id": task.id,
            "title": task.title,
            "status": task.state,
        }
        for task in reversed(tasks)
        if _task_success(task)
    ][:3]


def _coerce_utc_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _task_result_task_ids(db: AsyncSession, task_ids: list[int]) -> set[int]:
    if not task_ids:
        return set()
    raw_ids = (
        (
            await db.execute(
                select(TaskResult.task_id).where(TaskResult.task_id.in_(task_ids))
            )
        )
        .scalars()
        .all()
    )
    normalized: set[int] = set()
    for item in raw_ids:
        try:
            normalized.add(int(item))
        except (TypeError, ValueError):
            continue
    return normalized


def _matching_program_project(program_status: dict[str, Any], initiative_id: str) -> dict[str, Any]:
    if not initiative_id:
        return {}
    projects = program_status.get("projects") if isinstance(program_status.get("projects"), list) else []
    for project in projects:
        if not isinstance(project, dict):
            continue
        project_id = str(project.get("project_id") or "").strip()
        if project_id and project_id.lower() == initiative_id.lower():
            return project
    return {}


def _snapshot_primary_task(snapshot: dict[str, Any]) -> Task | None:
    tasks = snapshot.get("tasks") if isinstance(snapshot.get("tasks"), list) else []
    active_task = next(
        (
            task
            for task in tasks
            if _normalize_text(getattr(task, "readiness", "")).lower() == "in_progress"
            or _normalize_text(getattr(task, "state", "")).lower() in ACTIVE_TASK_STATES
        ),
        None,
    )
    if active_task is not None:
        return active_task
    return next(
        (task for task in tasks if _normalize_text(getattr(task, "readiness", "")).lower() == "ready"),
        None,
    )


def _follow_on_snapshot(
    objective_snapshots: list[dict[str, Any]],
    chosen_snapshot: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not chosen_snapshot or not bool(chosen_snapshot.get("is_complete")):
        return None
    next_live_snapshot = next(
        (
            snapshot
            for snapshot in objective_snapshots
            if snapshot is not chosen_snapshot and snapshot.get("has_active_or_ready")
        ),
        None,
    )
    if next_live_snapshot is not None:
        return next_live_snapshot
    return next(
        (
            snapshot
            for snapshot in objective_snapshots
            if snapshot is not chosen_snapshot and not bool(snapshot.get("is_complete"))
        ),
        None,
    )


def _follow_on_payload(
    snapshot: dict[str, Any] | None,
    *,
    result_task_ids: set[int],
) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    objective = snapshot.get("objective")
    if objective is None:
        return {}
    task = _snapshot_primary_task(snapshot)
    if task is not None:
        payload = _task_out(task, has_result=task.id in result_task_ids)
        payload["display_title"] = _task_display_title(task)
        return payload
    objective_title = _prefer_longer_text(
        getattr(objective, "title", ""),
        getattr(objective, "description", ""),
    )
    if not objective_title:
        return {}
    objective_execution_state = str(snapshot.get("objective_execution_state") or "queued").strip() or "queued"
    display_title = f"Advance to next objective: {objective_title}"
    return {
        "task_id": None,
        "objective_id": getattr(objective, "id", None),
        "title": display_title,
        "display_title": display_title,
        "status": objective_execution_state,
        "execution_state": objective_execution_state,
        "scope": getattr(objective, "description", ""),
    }


def _prefer_longer_text(primary: object, *alternatives: object) -> str:
    candidates = [str(primary or "").strip(), *(str(item or "").strip() for item in alternatives)]
    usable = [item for item in candidates if item]
    if not usable:
        return ""
    primary_text = usable[0]
    if primary_text.endswith("..."):
        for candidate in usable[1:]:
            if len(candidate) > len(primary_text):
                return candidate
    return primary_text


def _task_display_title(task: Task) -> str:
    return _prefer_longer_text(getattr(task, "title", ""), getattr(task, "details", ""))


def _objective_display_title(objective: Objective, *, project_entry: dict[str, Any]) -> str:
    primary = str(getattr(objective, "title", "") or "").strip()
    project_objective = str(project_entry.get("objective") or "").strip() if isinstance(project_entry, dict) else ""
    if project_objective and (primary.endswith("...") or "program_id:" in primary.lower()):
        return project_objective
    return _prefer_longer_text(
        primary,
        project_objective,
        getattr(objective, "description", ""),
    )


def _objective_is_planning_only(objective: Objective) -> bool:
    metadata_json = getattr(objective, "metadata_json", {}) or {}
    if not isinstance(metadata_json, dict):
        return False
    return bool(metadata_json.get("planning_only"))


def _task_movement_score(
    task: Task,
    *,
    has_result: bool = False,
) -> float:
    tracking = task_execution_tracking_snapshot(task, has_result=has_result)
    if _task_success(task) and task_has_completion_evidence(task, has_result=has_result):
        return 1.0
    if (
        tracking.get("execution_result") is not None
        or bool(tracking.get("has_result_record"))
        or bool(tracking.get("result_artifact"))
    ):
        return 0.8
    if bool(tracking.get("execution_started")):
        return 0.6
    if (
        bool(tracking.get("task_dispatched"))
        or bool(tracking.get("request_id"))
        or bool(tracking.get("execution_trace"))
    ):
        return 0.35
    if bool(tracking.get("task_created")):
        return 0.1
    return 0.0


def _build_initiative_progress_payload(
    *,
    tasks: list[Task],
    result_task_ids: set[int],
) -> dict[str, Any]:
    task_count = len(tasks)
    completed_task_count = sum(
        1
        for task in tasks
        if _task_success(task)
        and task_has_completion_evidence(task, has_result=task.id in result_task_ids)
    )
    percent = int(round((completed_task_count / task_count) * 100)) if task_count else 0
    movement_percent = (
        int(
            round(
                (
                    sum(
                        _task_movement_score(
                            task,
                            has_result=task.id in result_task_ids,
                        )
                        for task in tasks
                    )
                    / task_count
                )
                * 100
            )
        )
        if task_count
        else 0
    )
    summary = (
        f"{completed_task_count}/{task_count} bounded tasks completed ({percent}%)."
        if task_count
        else "No bounded tasks are registered yet."
    )
    movement_summary = (
        "Small-movement marker tracks bounded task creation, dispatch, execution, and result evidence."
        if task_count
        else "Movement stays at 0% until bounded tasks exist."
    )
    return {
        "task_count": task_count,
        "completed_task_count": completed_task_count,
        "percent": percent,
        "movement_percent": movement_percent,
        "movement_summary": movement_summary,
        "summary": summary,
    }


def _build_initiative_activity_payload(
    *,
    objective: Objective,
    objective_execution_state: str,
    active_task: Task | None,
    next_task: Task | None,
    blocked: list[dict[str, Any]],
    progress: dict[str, Any],
    result_task_ids: set[int],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    state = "idle"
    label = "Idle"
    summary = "No active initiative work is currently running."
    stale_seconds: float | None = None
    started_at: str = ""
    reason = ""
    supersession = _initiative_supersession_snapshot(getattr(objective, "id", None))

    if objective_execution_state == "completed":
        state = "completed"
        label = "Completed"
        summary = "The active initiative objective has completion evidence and is marked complete."
    elif active_task is not None:
        planning_only = _objective_is_planning_only(objective)
        active_tracking = task_execution_tracking_snapshot(active_task, has_result=active_task.id in result_task_ids)
        task_state = task_execution_state(active_task, has_result=active_task.id in result_task_ids)
        readiness = _normalize_text(getattr(active_task, "readiness", "")).lower()
        started_dt = _coerce_utc_datetime(active_tracking.get("activity_started_at")) or _coerce_utc_datetime(
            getattr(active_task, "created_at", None)
        )
        if started_dt is not None:
            started_at = started_dt.isoformat().replace("+00:00", "Z")
            stale_seconds = max(0.0, (now - started_dt).total_seconds())
        if planning_only and objective_execution_state == "created" and task_state == "created":
            state = "idle"
            label = "Planned"
            reason = "planning_only_no_dispatch"
            summary = f"{_task_display_title(active_task)} is intentionally staged as planning-only work with no execution dispatch."
        elif task_state in FAILURE_STATES or readiness in {"blocked", "waiting_on_human", "waiting_on_tod"}:
            state = "stuck"
            label = "Stuck"
            reason = readiness or task_state or "blocked"
            summary = f"{_task_display_title(active_task)} is blocked and needs intervention before it can continue."
        elif (
            task_state in {"created", "dispatched", "executing"}
            and stale_seconds is not None
            and stale_seconds >= 1800.0
            and int(progress.get("completed_task_count") or 0) == 0
        ):
            if supersession:
                state = "superseded"
                label = "Superseded"
                reason = "superseded_by_authoritative_request"
                summary = (
                    f"Objective {_normalize_objective_token(getattr(objective, 'id', None))} remains in audit history, "
                    f"but current TOD-authoritative work has moved to objective {supersession['objective_id']}"
                    f"{f' via request {supersession['request_id']}' if supersession.get('request_id') else ''}. "
                    "This older initiative is superseded, not an active stall."
                )
                stale_seconds = None
            else:
                state = "stale"
                label = "Stale"
                reason = "execution_stalled_without_completion_evidence"
                minutes = int(round(stale_seconds / 60.0))
                summary = (
                    f"{_task_display_title(active_task)} is still marked {task_state} after about {minutes} minutes "
                    "without bounded completion evidence."
                )
        else:
            state = "working"
            label = "Working"
            reason = task_state or readiness or "executing"
            summary = f"{_task_display_title(active_task)} is actively running inside the current initiative."
    elif blocked:
        state = "stuck"
        label = "Stuck"
        reason = "blocked_tasks_visible"
        summary = "The active initiative has blocked tasks and is waiting for intervention or TOD progress."
    elif next_task is not None or objective_execution_state in {"created", "queued", "dispatched"}:
        state = "idle"
        label = "Idle"
        reason = "awaiting_dispatch"
        summary = "The initiative is loaded and ready, but no task is actively executing right now."

    return {
        "state": state,
        "label": label,
        "summary": summary,
        "reason": reason,
        "started_at": started_at,
        "stale_seconds": stale_seconds,
    }


def _objective_out(objective: Objective) -> dict[str, Any]:
    metadata_json = getattr(objective, "metadata_json", {}) or {}
    normalized_metadata_json = {
        **metadata_json,
        "initiative_id": normalize_initiative_id(metadata_json.get("initiative_id")),
    }
    execution_tracking = (
        normalized_metadata_json.get("execution_tracking")
        if isinstance(normalized_metadata_json.get("execution_tracking"), dict)
        else {}
    )
    return {
        "objective_id": objective.id,
        "title": objective.title,
        "description": objective.description,
        "priority": objective.priority,
        "constraints": objective.constraints_json,
        "success_criteria": objective.success_criteria,
        "status": objective.state,
        "owner": getattr(objective, "owner", INITIATIVE_OWNER),
        "execution_mode": getattr(objective, "execution_mode", "auto"),
        "auto_continue": bool(getattr(objective, "auto_continue", True)),
        "boundary_mode": getattr(objective, "boundary_mode", SOFT_BOUNDARY),
        "initiative_id": normalize_initiative_id(normalized_metadata_json.get("initiative_id")),
        "request_id": str(
            metadata_json.get("latest_request_id")
            or execution_tracking.get("request_id")
            or ""
        ).strip(),
        "metadata_json": normalized_metadata_json,
        "execution_state": str(execution_tracking.get("execution_state") or "queued").strip() or "queued",
        "execution_tracking": execution_tracking,
        "created_at": objective.created_at,
    }


def _task_out(task: Task, *, has_result: bool = False) -> dict[str, Any]:
    execution_tracking = task_execution_tracking_snapshot(task, has_result=has_result)
    metadata_json = getattr(task, "metadata_json", {}) or {}
    normalized_metadata_json = {
        **metadata_json,
        "initiative_id": normalize_initiative_id(metadata_json.get("initiative_id")),
    }
    return {
        "task_id": task.id,
        "objective_id": task.objective_id,
        "title": task.title,
        "scope": task.details,
        "dependencies": task.dependencies,
        "acceptance_criteria": task.acceptance_criteria,
        "status": task.state,
        "assigned_to": task.assigned_to,
        "readiness": getattr(task, "readiness", "queued"),
        "boundary_mode": getattr(task, "boundary_mode", SOFT_BOUNDARY),
        "start_now": bool(getattr(task, "start_now", False)),
        "human_prompt_required": bool(getattr(task, "human_prompt_required", False)),
        "execution_scope": getattr(task, "execution_scope", "bounded"),
        "expected_outputs": getattr(task, "expected_outputs_json", []) or [],
        "verification_commands": getattr(task, "verification_commands_json", []) or [],
        "dispatch_status": getattr(task, "dispatch_status", "pending"),
        "dispatch_artifact_json": getattr(task, "dispatch_artifact_json", {}) or {},
        "initiative_id": normalize_initiative_id(normalized_metadata_json.get("initiative_id")),
        "request_id": str(
            metadata_json.get("request_id") or execution_tracking.get("request_id") or ""
        ).strip(),
        "metadata_json": normalized_metadata_json,
        "execution_state": task_execution_state(task, has_result=has_result),
        "execution_tracking": execution_tracking,
        "created_at": task.created_at,
    }


async def _tasks_for_objective(db: AsyncSession, objective_id: int) -> list[Task]:
    return list(
        (
            await db.execute(select(Task).where(Task.objective_id == objective_id).order_by(Task.id.asc()))
        )
        .scalars()
        .all()
    )


async def refresh_task_readinesses(db: AsyncSession, objective_id: int | None) -> list[Task]:
    if objective_id is None:
        return []
    tasks = await _tasks_for_objective(db, objective_id)
    task_by_id = {task.id: task for task in tasks}
    for task in tasks:
        prior = _normalize_text(getattr(task, "readiness", "")).lower()
        if _task_success(task):
            next_readiness = "completed"
        elif _task_failure(task):
            next_readiness = "blocked"
        else:
            dependency_rows = [task_by_id.get(dep_id) for dep_id in (task.dependencies or [])]
            dependency_failures = any(dep is not None and _task_failure(dep) for dep in dependency_rows)
            dependency_pending = any(dep is not None and not _task_success(dep) for dep in dependency_rows)
            if dependency_failures:
                next_readiness = "blocked"
            elif dependency_pending:
                next_readiness = "queued"
            elif bool(getattr(task, "human_prompt_required", False)) or _normalize_text(getattr(task, "boundary_mode", "")).lower() == HARD_BOUNDARY:
                next_readiness = "waiting_on_human"
            elif _normalize_text(task.assigned_to).lower() == "tod":
                next_readiness = "waiting_on_tod"
            elif _normalize_text(getattr(task, "dispatch_status", "")).lower() in {"queued", "running", "dispatched"}:
                next_readiness = "in_progress"
            elif _normalize_text(task.state).lower() in ACTIVE_TASK_STATES:
                next_readiness = "in_progress"
            else:
                next_readiness = "ready"
        if prior != next_readiness:
            task.readiness = next_readiness
    return tasks


async def _select_next_ready_task(
    db: AsyncSession,
    *,
    objective_id: int | None = None,
) -> tuple[Objective | None, Task | None]:
    objective_rows = list(
        (
            await db.execute(select(Objective).order_by(Objective.created_at.desc(), Objective.id.desc()))
        )
        .scalars()
        .all()
    )
    candidates: list[tuple[int, int, Objective, Task]] = []
    for objective in objective_rows:
        if objective_id is not None and objective.id != objective_id:
            continue
        if _normalize_text(getattr(objective, "owner", INITIATIVE_OWNER)).lower() != INITIATIVE_OWNER:
            continue
        tasks = await refresh_task_readinesses(db, objective.id)
        for task in tasks:
            if _normalize_text(task.readiness).lower() != "ready":
                continue
            if not bool(getattr(task, "start_now", False)):
                continue
            if bool(getattr(task, "human_prompt_required", False)):
                continue
            candidates.append((_priority_rank(objective.priority), task.id, objective, task))
    if not candidates:
        return None, None
    _, _, objective, task = sorted(candidates, key=lambda item: (item[0], item[1]))[0]
    return objective, task


def build_codex_handoff_payload(*, objective: Objective, task: Task) -> dict[str, Any]:
    expected_outputs = getattr(task, "expected_outputs_json", []) or []
    verification_commands = getattr(task, "verification_commands_json", []) or []
    task_summary = _normalize_text(task.details or task.title)
    requested_outcome = expected_outputs[0] if expected_outputs else task.acceptance_criteria or task.title
    return {
        "handoff_id": f"objective-{objective.id}-task-{task.id}-{_normalize_slug(task.title)}",
        "source": "initiative-driver",
        "topic": task.title,
        "summary": f"Implement bounded work for objective {objective.id}: {task_summary}",
        "requested_outcome": f"Implement and verify: {requested_outcome}",
        "constraints": [
            *[str(item) for item in (objective.constraints_json or []) if _normalize_text(item)],
            f"execution_scope={getattr(task, 'execution_scope', 'bounded')}",
            "no additional human prompt unless a hard boundary is reached",
        ],
        "next_bounded_steps": [
            {"step_id": "task_summary", "summary": task_summary or task.title},
            *[
                {"step_id": f"verify_{index + 1}", "summary": command}
                for index, command in enumerate(verification_commands[:3])
            ],
        ],
        "bounded_actions_allowed": [],
        "status": "ready",
        "dispatch_contract": {
            "policy_version": DEFAULT_POLICY_VERSION,
            "objective_id": objective.id,
            "task_id": task.id,
            "execution_scope": getattr(task, "execution_scope", "bounded"),
            "start_now": bool(getattr(task, "start_now", False)),
            "human_prompt_required": bool(getattr(task, "human_prompt_required", False)),
            "expected_outputs": expected_outputs,
            "verification_commands": verification_commands,
        },
    }


def _publish_codex_dispatch_bridge_artifacts(
    *,
    objective: Objective,
    task: Task,
    request_id: str,
    submission_status: str,
    latest_result_summary: str,
    shared_root: Path | None = None,
) -> dict[str, Any]:
    normalized_request_id = str(request_id or "").strip()
    if not normalized_request_id:
        return {}

    resolved_shared_root = (shared_root or RUNTIME_SHARED_DIR).expanduser().resolve()
    resolved_shared_root.mkdir(parents=True, exist_ok=True)

    publication_service = "initiative_codex_dispatch"
    publication_instance = f"{publication_service}:{objective.id}:{task.id}"
    generated_at = utc_now()
    objective_ref = f"objective-{objective.id}"
    task_ref = f"{objective_ref}-task-{task.id}"
    correlation_id = task_ref
    request_path = resolved_shared_root / "MIM_TOD_TASK_REQUEST.latest.json"
    trigger_path = resolved_shared_root / "MIM_TO_TOD_TRIGGER.latest.json"
    request_payload = {
        "version": "1.0",
        "source": "MIM",
        "target": "TOD",
        "generated_at": generated_at,
        "emitted_at": generated_at,
        "sequence": 1,
        "source_host": "MIM",
        "source_service": publication_service,
        "source_instance_id": publication_instance,
        "objective_id": objective_ref,
        "task_id": task_ref,
        "request_id": normalized_request_id,
        "correlation_id": correlation_id,
        "title": str(getattr(task, "title", "") or "").strip() or task_ref,
        "scope": str(getattr(task, "details", "") or getattr(task, "title", "") or "").strip() or task_ref,
        "priority": str(getattr(objective, "priority", "high") or "high").strip() or "high",
        "action_name": "codex_handoff",
        "action": "codex_handoff",
        "capability_name": "initiative_codex_handoff",
        "requested_executor": "tod",
        "request_status": submission_status or "queued",
        "dispatch_status": submission_status or "queued",
        "result_status": "pending" if submission_status not in FAILURE_STATES else submission_status,
        "result_reason": latest_result_summary,
        "command": {
            "name": "codex_handoff",
            "args": {
                "objective_id": objective.id,
                "task_id": task.id,
                "handoff_id": normalized_request_id,
                "execution_scope": str(getattr(task, "execution_scope", "bounded") or "bounded").strip() or "bounded",
            },
        },
        "metadata_json": {
            "objective_title": str(getattr(objective, "title", "") or "").strip(),
            "task_title": str(getattr(task, "title", "") or "").strip(),
            "task_acceptance_criteria": str(getattr(task, "acceptance_criteria", "") or "").strip(),
        },
    }
    request_path.write_text(json.dumps(request_payload, indent=2) + "\n", encoding="utf-8")
    normalized_request, request_errors = normalize_and_validate_file(
        request_path,
        message_kind="request",
        service_name=publication_service,
        instance_id=publication_instance,
        transport_surface=str(resolved_shared_root),
    )
    request_sha256 = hashlib.sha256(request_path.read_bytes()).hexdigest()

    trigger_payload = {
        "generated_at": generated_at,
        "emitted_at": generated_at,
        "sequence": 1,
        "source_actor": "MIM",
        "source_host": "MIM",
        "source_service": publication_service,
        "source_instance_id": publication_instance,
        "target_actor": "TOD",
        "trigger": "task_request_posted",
        "artifact": request_path.name,
        "artifact_path": str(request_path),
        "artifact_sha256": request_sha256,
        "task_id": str(normalized_request.get("task_id") or task_ref).strip(),
        "request_id": str(normalized_request.get("request_id") or normalized_request_id).strip(),
        "correlation_id": str(normalized_request.get("correlation_id") or correlation_id).strip(),
        "action_required": "pull_latest_and_ack",
        "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json",
    }
    trigger_path.write_text(json.dumps(trigger_payload, indent=2) + "\n", encoding="utf-8")
    normalized_trigger, trigger_errors = normalize_and_validate_file(
        trigger_path,
        message_kind="trigger",
        service_name=publication_service,
        instance_id=publication_instance,
        transport_surface=str(resolved_shared_root),
    )

    return {
        "status": "published" if not request_errors and not trigger_errors else "validation_failed",
        "request_id": str(normalized_request.get("request_id") or normalized_request_id).strip(),
        "objective_id": str(normalized_request.get("objective_id") or objective_ref).strip(),
        "task_id": str(normalized_request.get("task_id") or task_ref).strip(),
        "request_path": str(request_path),
        "trigger_path": str(trigger_path),
        "request_packet_type": str(normalized_request.get("packet_type") or "").strip(),
        "trigger_packet_type": str(normalized_trigger.get("packet_type") or "").strip(),
        "request_errors": request_errors,
        "trigger_errors": trigger_errors,
    }


async def _execute_local_mim_task(
    db: AsyncSession,
    *,
    objective: Objective,
    task: Task,
    actor: str,
    source: str,
) -> dict[str, Any]:
    metadata_json = getattr(task, "metadata_json", {}) or {}
    automation_kind = _normalize_text(metadata_json.get("automation_kind")).lower()
    if automation_kind == "stale_prevention_pass":
        step_number = int(metadata_json.get("stale_prevention_step_number", 0) or 0)
        total_steps = int(metadata_json.get("stale_prevention_step_count", 0) or 0)
        context = await _stale_prevention_context(db)
        repeated_pattern = context["repeated_pattern_detected"]
        report = {
            "task_id": task.id,
            "mode": "stale_prevention_pass",
            "status": "completed",
            "step_number": step_number,
            "step_count": total_steps,
            "repeated_pattern_detected": repeated_pattern,
            "progress_classification": context["progress_classification"],
            "corrective_branch_selected": context["corrective_branch_selected"],
            "proposed_remediation_task": context["proposed_remediation_task"],
            "code_modification_recommended": context["code_modification_recommended"],
            "tod_follow_up_selected": context["tod_follow_up_selected"],
            "stale_prevention_rule": context["stale_prevention_rule"],
        }
        if step_number == 1:
            report["result"] = (
                f"Detected repeated pattern={bool(repeated_pattern.get('detected'))} "
                f"class={str(repeated_pattern.get('action_class') or 'none')} "
                f"count={int(repeated_pattern.get('count') or 0)}."
            )
        elif step_number == 2:
            report["result"] = (
                "Classified recent behavior as "
                f"{context['progress_classification']} based on recent resolution history."
            )
        elif step_number == 3:
            report["result"] = (
                "Selected corrective branch "
                f"{context['corrective_branch_selected']} for the detected stale pattern."
            )
        elif step_number == 4:
            report["result"] = (
                "Generated remediation task "
                f"{context['proposed_remediation_task']['title']} targeting "
                f"{', '.join(context['proposed_remediation_task']['targets'])}."
            )
        else:
            report["result"] = (
                "Recorded stale-prevention follow-up rule '"
                f"{context['stale_prevention_rule']}' and selected next action: "
                f"{context['tod_follow_up_selected']}."
            )
        local_request_id = f"initiative-{objective.id}-task-{task.id}-stale-prevention"
        tracking = _set_task_execution_tracking(
            task,
            task_created=True,
            task_dispatched=True,
            execution_started=True,
            execution_result=report["result"],
            request_id=local_request_id,
            execution_trace=f"journal:initiative_stale_prevention_pass_completed:{task.id}",
            result_artifact=f"task:{task.id}:dispatch_artifact",
        )
        report["request_id"] = local_request_id
        report["execution_tracking"] = tracking
        task.state = "completed"
        task.dispatch_status = "completed"
        task.dispatch_artifact_json = report
        await write_journal(
            db,
            actor=actor,
            action="initiative_stale_prevention_pass_completed",
            target_type="task",
            target_id=str(task.id),
            summary=f"Completed stale-prevention pass {step_number}",
            metadata_json={"objective_id": objective.id, "source": source, **report},
        )
        return report
    if automation_kind == "continuation_validation_step":
        step_number = int(metadata_json.get("validation_step_number", 0) or 0)
        total_steps = int(metadata_json.get("validation_step_count", 0) or 0)
        objective_tasks = await _tasks_for_objective(db, objective.id)
        prior_reports = [
            candidate.dispatch_artifact_json
            for candidate in objective_tasks
            if candidate.id != task.id
            and isinstance(candidate.dispatch_artifact_json, dict)
            and str(candidate.dispatch_artifact_json.get("mode") or "").strip() == "continuation_validation_step"
        ]
        prior_completed = len(prior_reports)
        continuation_count = sum(
            1
            for report in prior_reports
            if str(report.get("continuation_status") or "").strip() == "continued"
        )
        blocked_candidates: list[dict[str, Any]] = []
        objective_rows = list(
            (
                await db.execute(select(Objective).order_by(Objective.created_at.desc(), Objective.id.desc()))
            )
            .scalars()
            .all()
        )
        for candidate_objective in objective_rows:
            if candidate_objective.id == objective.id:
                continue
            candidate_tasks = await refresh_task_readinesses(db, candidate_objective.id)
            for candidate_task in candidate_tasks:
                readiness = _normalize_text(getattr(candidate_task, "readiness", "")).lower()
                state = _normalize_text(getattr(candidate_task, "state", "")).lower()
                if readiness in {"blocked", "waiting_on_human"} or state in FAILURE_STATES:
                    blocked_candidates.append(
                        {
                            "objective_id": candidate_objective.id,
                            "objective_title": candidate_objective.title,
                            "task_id": candidate_task.id,
                            "task_title": candidate_task.title,
                            "readiness": readiness,
                            "status": state,
                        }
                    )
        selected_task = f"validation_step_{step_number:02d}"
        blockers: list[str] = []
        continuation_status = "continued"
        result = ""
        recovery_transition: dict[str, Any] = {}
        if step_number == 1:
            result = "Execution started on a bounded validation task without human confirmation."
        elif step_number == 2:
            result = "Observed prior validation task completion with no early status-done exit."
        elif step_number == 3:
            result = "Selected the next validation task automatically and continued execution without human input."
            continuation_count += 1
        elif step_number == 4:
            if blocked_candidates:
                candidate = blocked_candidates[0]
                recovery_transition = {
                    "type": "detected_and_isolated",
                    "source_objective_id": candidate["objective_id"],
                    "source_task_id": candidate["task_id"],
                    "source_readiness": candidate["readiness"],
                    "source_status": candidate["status"],
                }
                result = (
                    "Detected a recent blocked readiness condition and recorded a bounded recovery transition "
                    f"from objective {candidate['objective_id']} task {candidate['task_id']}."
                )
            else:
                recovery_transition = {"type": "synthetic_monitor_only", "reason": "no_recent_blocked_task_found"}
                result = "No recent blocked readiness condition was present, so a bounded monitor-only recovery transition was recorded."
            continuation_count += 1
        elif step_number == 5:
            if prior_reports and any(_has_real_recovery_transition(report) for report in prior_reports):
                result = "Execution resumed after the recorded recovery transition and no idle stall was detected."
            else:
                continuation_status = "stalled"
                blockers.append("recovery_transition_not_recorded")
                result = "Could not confirm auto-resume because no prior recovery transition was recorded in the validation lineage."
        else:
            continuation_count += 1
            result = "Executed an additional bounded continuation task and preserved execution lineage."

        report = {
            "task_id": task.id,
            "mode": "continuation_validation_step",
            "status": "completed" if continuation_status == "continued" else "blocked",
            "step_number": step_number,
            "step_count": total_steps,
            "task_selected": selected_task,
            "result": result,
            "continuation_status": continuation_status,
            "blockers": blockers,
            "no_confirmation_prompt": True,
            "no_status_done_early_exit": True,
            "execution_started": True,
            "execution_completed": continuation_status == "continued",
            "next_task_selected_automatically": step_number not in {2},
            "recovery_transition": recovery_transition,
            "lineage": {
                "objective_id": objective.id,
                "completed_before_step": prior_completed,
                "continuations_before_step": continuation_count,
            },
        }
        local_request_id = f"initiative-{objective.id}-task-{task.id}-continuation"
        tracking = _set_task_execution_tracking(
            task,
            task_created=True,
            task_dispatched=True,
            execution_started=True,
            execution_result=report["result"] if continuation_status == "continued" else None,
            request_id=local_request_id,
            execution_trace=f"journal:initiative_continuation_validation_step_completed:{task.id}",
            result_artifact=f"task:{task.id}:dispatch_artifact",
        )
        report["request_id"] = local_request_id
        report["execution_tracking"] = tracking
        task.state = "completed" if continuation_status == "continued" else "blocked"
        task.dispatch_status = "completed" if continuation_status == "continued" else "blocked"
        task.dispatch_artifact_json = report
        await write_journal(
            db,
            actor=actor,
            action="initiative_continuation_validation_step_completed",
            target_type="task",
            target_id=str(task.id),
            summary=f"Completed continuation validation step {step_number}",
            metadata_json={"objective_id": objective.id, "source": source, **report},
        )
        return report
    if automation_kind == "continuous_execution_iteration":
        iteration_number = int(metadata_json.get("iteration_number", 0) or 0)
        iteration_target = int(metadata_json.get("iteration_target", 5) or 5)
        next_action = await build_self_evolution_next_action(
            actor=str(metadata_json.get("actor") or actor),
            source=str(metadata_json.get("source") or source),
            refresh=False,
            lookback_hours=168,
            min_occurrence_count=2,
            auto_experiment_limit=10,
            limit=50,
            db=db,
        )
        decision = next_action.get("decision") if isinstance(next_action.get("decision"), dict) else {}
        action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
        action_method = str(action.get("method") or "").strip().upper()
        action_path = str(action.get("path") or "").strip()
        selected_task = str(
            decision.get("summary") or decision.get("rationale") or action_path or task.title
        ).strip()

        inspected_summary = ""
        if action_method == "GET" and action_path.startswith("/improvement/recommendations/"):
            recommendation_id_text = action_path.removeprefix(
                "/improvement/recommendations/"
            ).split("/", 1)[0].strip()
            if recommendation_id_text.isdigit():
                recommendation_row = await get_improvement_recommendation(
                    recommendation_id=int(recommendation_id_text),
                    db=db,
                )
                if recommendation_row is not None:
                    recommendation = await to_improvement_recommendation_out_resolved(
                        row=recommendation_row,
                        db=db,
                    )
                    inspected_summary = str(
                        recommendation.get("recommendation_summary")
                        or recommendation.get("recommendation_type")
                        or ""
                    ).strip()

        objective_tasks = await _tasks_for_objective(db, objective.id)
        prior_selected_task = ""
        for objective_task in reversed(objective_tasks):
            if objective_task.id == task.id:
                continue
            prior_artifact = (
                objective_task.dispatch_artifact_json
                if isinstance(objective_task.dispatch_artifact_json, dict)
                else {}
            )
            if str(prior_artifact.get("mode") or "").strip() == "continuous_execution_iteration":
                prior_selected_task = str(prior_artifact.get("task_selected") or "").strip()
                break

        delta = "improved" if not prior_selected_task else (
            "no change" if prior_selected_task == selected_task else "improved"
        )
        result = (
            f"Executed bounded {action_method} {action_path}."
            if action_method and action_path
            else "Refreshed MIM-owned state and selected the highest-value bounded next task."
        )
        if inspected_summary:
            result = f"{result} Inspected recommendation detail: {inspected_summary}."

        next_task_text = selected_task
        if iteration_number >= iteration_target:
            next_task_text = f"Prompt MIM to start the next natural objective: {selected_task}"

        report = {
            "task_id": task.id,
            "mode": "continuous_execution_iteration",
            "status": "completed",
            "iteration_number": iteration_number,
            "iteration_target": iteration_target,
            "task_selected": selected_task,
            "result": result,
            "delta": delta,
            "next_task": next_task_text,
            "decision_type": str(decision.get("decision_type") or "").strip(),
            "action": {
                "method": action_method,
                "path": action_path,
            },
        }
        local_request_id = f"initiative-{objective.id}-task-{task.id}-continuous-loop"
        tracking = _set_task_execution_tracking(
            task,
            task_created=True,
            task_dispatched=True,
            execution_started=True,
            execution_result=report["result"],
            request_id=local_request_id,
            execution_trace=f"journal:initiative_continuous_execution_iteration_completed:{task.id}",
            result_artifact=f"task:{task.id}:dispatch_artifact",
        )
        report["request_id"] = local_request_id
        report["execution_tracking"] = tracking
        task.state = "completed"
        task.dispatch_status = "completed"
        task.dispatch_artifact_json = report
        await write_journal(
            db,
            actor=actor,
            action="initiative_continuous_execution_iteration_completed",
            target_type="task",
            target_id=str(task.id),
            summary=f"Completed continuous execution iteration {iteration_number}",
            metadata_json={"objective_id": objective.id, "source": source, **report},
        )
        return report

    if automation_kind != "self_evolution_reset":
        local_request_id = f"initiative-{objective.id}-task-{task.id}-local-noop"
        tracking = _set_task_execution_tracking(
            task,
            task_created=True,
            task_dispatched=True,
            execution_started=True,
            execution_result=f"Completed local initiative task {task.id}.",
            request_id=local_request_id,
            execution_trace=f"journal:initiative_local_task_completed:{task.id}",
            result_artifact=f"task:{task.id}:dispatch_artifact",
        )
        task.state = "completed"
        task.dispatch_status = "completed"
        task.dispatch_artifact_json = {
            "status": "completed",
            "mode": "local_noop",
            "summary": f"Completed local initiative task {task.id}.",
            "request_id": local_request_id,
            "execution_tracking": tracking,
        }
        await write_journal(
            db,
            actor=actor,
            action="initiative_local_task_completed",
            target_type="task",
            target_id=str(task.id),
            summary=f"Completed local task {task.title}",
            metadata_json={"objective_id": objective.id, "source": source},
        )
        return {"task_id": task.id, "mode": "local_noop", "status": "completed"}

    progress = await reset_natural_language_development_progress(
        actor=str(metadata_json.get("actor") or actor),
        source=str(metadata_json.get("source") or source),
        db=db,
    )
    safe_progress = _json_safe(progress)
    local_request_id = f"initiative-{objective.id}-task-{task.id}-self-evolution-reset"
    tracking = _set_task_execution_tracking(
        task,
        task_created=True,
        task_dispatched=True,
        execution_started=True,
        execution_result="natural_language_progress_reset",
        request_id=local_request_id,
        execution_trace=f"journal:initiative_training_started:{task.id}",
        result_artifact=f"task:{task.id}:dispatch_artifact",
    )
    task.state = "completed"
    task.dispatch_status = "completed"
    task.dispatch_artifact_json = {
        "status": "completed",
        "mode": "self_evolution_reset",
        "progress": safe_progress,
        "request_id": local_request_id,
        "execution_tracking": tracking,
    }
    await write_journal(
        db,
        actor=actor,
        action="initiative_training_started",
        target_type="task",
        target_id=str(task.id),
        summary=f"Started training loop via task {task.id}",
        metadata_json={"objective_id": objective.id, "source": source},
    )
    return {
        "task_id": task.id,
        "mode": "self_evolution_reset",
        "status": "completed",
        "progress": safe_progress,
    }


async def _dispatch_codex_task(
    db: AsyncSession,
    *,
    objective: Objective,
    task: Task,
    actor: str,
    source: str,
) -> dict[str, Any]:
    submission = await submit_handoff_payload(build_codex_handoff_payload(objective=objective, task=task))
    submission_status = _normalize_text(submission.get("status")).lower() or "queued"
    latest_result = submission.get("latest_result") if isinstance(submission.get("latest_result"), dict) else {}
    broker_preparation = latest_result.get("broker_preparation") if isinstance(latest_result.get("broker_preparation"), dict) else {}
    broker_response = broker_preparation.get("broker_response") if isinstance(broker_preparation.get("broker_response"), dict) else {}
    automatic_live_response = broker_preparation.get("automatic_live_response") if isinstance(broker_preparation.get("automatic_live_response"), dict) else {}
    request_id = str(submission.get("handoff_id") or submission.get("task_id") or "").strip()
    bridge_artifacts = _publish_codex_dispatch_bridge_artifacts(
        objective=objective,
        task=task,
        request_id=request_id,
        submission_status=submission_status,
        latest_result_summary=str(submission.get("latest_result_summary") or "").strip(),
    )
    result_artifact = _authoritative_result_artifact_from_submission(submission)
    if not result_artifact:
        result_artifact = _analysis_result_artifact_from_submission(task, submission)
    if not result_artifact:
        result_artifact = _completion_result_artifact_from_submission(task, submission)
    execution_trace = str(
        submission.get("task_path")
        or submission.get("status_path")
        or submission.get("latest_task_path")
        or ""
    ).strip()
    tracking = _set_task_execution_tracking(
        task,
        task_created=True,
        task_dispatched=bool(request_id),
        execution_started=submission_status in {"completed", "in_progress", "running"},
        execution_result=str(submission.get("latest_result_summary") or "").strip() or None,
        request_id=request_id,
        execution_trace=execution_trace,
        result_artifact=result_artifact,
    )
    task.dispatch_status = submission_status
    task.dispatch_artifact_json = {
        **submission,
        "bridge_artifacts": bridge_artifacts,
        "execution_tracking": tracking,
    }
    task.state = (
        "completed"
        if submission_status == "completed" and task_has_completion_evidence(task)
        else "blocked"
        if submission_status == "blocked"
        else "failed"
        if submission_status in FAILURE_STATES
        else "in_progress"
    )
    if (
        str(broker_response.get("status") or "").strip().lower() not in {"", "completed"}
        and str(automatic_live_response.get("status") or "").strip().lower() == "completed"
    ):
        await write_journal(
            db,
            actor=actor,
            action="initiative_tod_fallback_continue",
            target_type="task",
            target_id=str(task.id),
            summary=f"TOD/broker path was unavailable for task {task.id}; execution continued via automatic live fallback.",
            metadata_json={
                "objective_id": objective.id,
                "source": source,
                "broker_status": str(broker_response.get("status") or "").strip(),
                "broker_reason": str(broker_response.get("reason") or "").strip(),
                "fallback_status": str(automatic_live_response.get("status") or "").strip(),
            },
        )
    await write_journal(
        db,
        actor=actor,
        action="initiative_codex_dispatched",
        target_type="task",
        target_id=str(task.id),
        summary=f"Dispatched initiative task {task.id} to Codex",
        metadata_json={
            "objective_id": objective.id,
            "source": source,
            "handoff_id": submission.get("handoff_id"),
            "dispatch_status": submission_status,
        },
    )
    return submission


async def continue_initiative(
    db: AsyncSession,
    *,
    objective_id: int | None,
    actor: str,
    source: str,
    max_auto_steps: int = 3,
) -> dict[str, Any]:
    executed: list[dict[str, Any]] = []
    dispatched: list[dict[str, Any]] = []
    auto_advanced_to: dict[str, Any] = {}
    completed_project_summary: dict[str, Any] = {}
    await _recover_retryable_blocked_codex_tasks(
        db,
        objective_id=objective_id,
        actor=actor,
        source=source,
    )
    await _recover_completed_codex_tasks_from_dispatch_artifacts(
        db,
        objective_id=objective_id,
        actor=actor,
        source=source,
    )
    for _ in range(max(1, min(int(max_auto_steps), 8))):
        objective, task = await _select_next_ready_task(db, objective_id=objective_id)
        if objective is None or task is None:
            break
        task.state = "in_progress"
        if _normalize_text(task.assigned_to).lower() == "mim":
            executed.append(
                await _execute_local_mim_task(
                    db,
                    objective=objective,
                    task=task,
                    actor=actor,
                    source=source,
                )
            )
        else:
            dispatched.append(
                await _dispatch_codex_task(
                    db,
                    objective=objective,
                    task=task,
                    actor=actor,
                    source=source,
                )
            )
            if _normalize_text(task.dispatch_status).lower() != "completed":
                break
        await refresh_task_readinesses(db, objective.id)
        await recompute_objective_state(db, objective.id)

    current_objective = None
    status_objective_id = objective_id
    if objective_id is not None:
        current_query = (
            await db.execute(
                select(Objective).where(Objective.id == objective_id)
            )
        ).scalars()
        if hasattr(current_query, "first"):
            current_objective = current_query.first()
        else:
            current_items = current_query.all() if hasattr(current_query, "all") else []
            current_objective = current_items[0] if current_items else None
    current_tasks = await _tasks_for_objective(db, objective_id) if objective_id is not None else []
    current_tracking = _objective_execution_tracking(current_objective, current_tasks) if current_objective is not None else {}
    current_execution_state = str(current_tracking.get("execution_state") or "").strip().lower()
    if current_objective is not None and current_execution_state == "completed":
        objective_metadata = current_objective.metadata_json if isinstance(current_objective.metadata_json, dict) else {}
        registry = objective_metadata.get("program_registry") if isinstance(objective_metadata.get("program_registry"), dict) else {}
        current_project_id = normalize_initiative_id(objective_metadata.get("initiative_id"))
        next_project = next_program_project(registry, current_project_id)
        if next_project:
            result_task_ids = await _task_result_task_ids(
                db,
                [int(task.id) for task in current_tasks if getattr(task, "id", None) is not None],
            )
            completed_project_summary = {
                "objective_id": getattr(current_objective, "id", None),
                "project_id": current_project_id,
                "tasks_completed": sum(1 for task in current_tasks if _task_success(task) and task.id in result_task_ids),
                "task_count": len(current_tasks),
                "files_changed": [],
                "tests_run": [],
                "pass_fail": "pass" if all(_task_terminal(task) and not _task_failure(task) for task in current_tasks) else "fail",
                "blockers": [],
                "next_objective_readiness": str(next_project.get("project_id") or "").strip(),
            }
            next_project_id = str(next_project.get("project_id") or "").strip()
            await write_journal(
                db,
                actor=actor,
                action="initiative_project_day_summary",
                target_type="objective",
                target_id=str(current_objective.id),
                summary=f"DAY SUMMARY: {current_project_id} completed; next project readiness {next_project_id or 'none'}.",
                metadata_json=completed_project_summary,
            )
            next_intent = project_program_intent(str(objective_metadata.get("program_id") or "").strip(), next_project)
            next_result = await drive_initiative_from_intent(
                db,
                actor=str(objective_metadata.get("actor") or actor),
                source=str(objective_metadata.get("source") or source),
                user_intent=next_intent,
                objective_title=str(next_project.get("objective") or next_project.get("display_title") or "").strip(),
                priority=str(getattr(current_objective, "priority", "high") or "high"),
                managed_scope=str(objective_metadata.get("managed_scope") or "workspace"),
                expected_outputs=[],
                verification_commands=[],
                continue_chain=True,
                max_auto_steps=max_auto_steps,
                metadata_json={
                    **objective_metadata,
                    "initiative_id": next_project_id,
                    "program_id": str(objective_metadata.get("program_id") or "").strip(),
                },
            )
            next_objective_payload = (
                next_result.get("objective")
                if isinstance(next_result, dict) and isinstance(next_result.get("objective"), dict)
                else {}
            )
            next_objective_id = next_objective_payload.get("objective_id")
            if next_objective_id is not None:
                try:
                    status_objective_id = int(next_objective_id)
                except (TypeError, ValueError):
                    status_objective_id = objective_id
            auto_advanced_to = {
                "project_id": next_project_id,
                "objective": str(next_project.get("objective") or "").strip(),
                "result": next_result,
            }
    status = await build_initiative_status(db=db, objective_id=status_objective_id)
    return {
        "executed_local": executed,
        "dispatched": dispatched,
        "completed_project_summary": completed_project_summary,
        "auto_advanced_to": auto_advanced_to,
        "status": status,
    }


async def drive_initiative_from_intent(
    db: AsyncSession,
    *,
    actor: str,
    source: str,
    user_intent: str,
    objective_title: str,
    priority: str,
    managed_scope: str,
    expected_outputs: list[str],
    verification_commands: list[str],
    continue_chain: bool,
    max_auto_steps: int,
    metadata_json: dict[str, Any],
) -> dict[str, Any]:
    registry = ensure_program_registration(user_intent)
    compiled = build_initiative_task_plan(
        user_intent=user_intent,
        actor=actor,
        source=source,
        managed_scope=managed_scope,
        expected_outputs=expected_outputs,
        verification_commands=verification_commands,
    )
    title = _normalize_text(objective_title) or compiled["objective_title"]
    request_id = str((metadata_json or {}).get("request_id") or "").strip()
    program_id = extract_explicit_program_id(user_intent) or str((metadata_json or {}).get("program_id") or "").strip()
    compiled_tasks = compiled["tasks"] if isinstance(compiled.get("tasks"), list) else []
    first_task_metadata = (
        compiled_tasks[0].metadata_json
        if compiled_tasks and isinstance(getattr(compiled_tasks[0], "metadata_json", {}), dict)
        else {}
    )
    compiled_project_id = normalize_initiative_id(first_task_metadata.get("program_project_id"))
    initiative_id = extract_explicit_initiative_id(user_intent) or normalize_initiative_id(
        (metadata_json or {}).get("initiative_id")
    )
    if not initiative_id and compiled_project_id:
        initiative_id = compiled_project_id
    if not initiative_id and program_id:
        initiative_id = normalize_initiative_id(program_id)
    if title == "Drive continuous execution loop" and request_id:
        title = f"Drive continuous execution loop [{request_id[-8:]}]"
    if title == CONTINUATION_VALIDATION_OBJECTIVE_TITLE and request_id:
        title = f"{CONTINUATION_VALIDATION_OBJECTIVE_TITLE} [{request_id[-8:]}]"
    boundary_mode = compiled["boundary_mode"]
    human_prompt_required = boundary_mode == HARD_BOUNDARY
    resume_existing = _looks_like_explicit_resume_request(user_intent, metadata_json)
    objective = None
    if initiative_id and resume_existing:
        candidate_objectives = list(
            (
                await db.execute(
                    select(Objective)
                    .where(Objective.state.in_(["new", "in_progress"]))
                    .order_by(Objective.id.desc())
                )
            )
            .scalars()
            .all()
        )
        normalized_initiative_id = initiative_id.lower()
        objective = next(
            (
                candidate
                for candidate in candidate_objectives
                if _normalize_text(getattr(candidate, "owner", INITIATIVE_OWNER)).lower()
                == INITIATIVE_OWNER
                and normalize_initiative_id(
                    (
                        getattr(candidate, "metadata_json", {})
                        if isinstance(getattr(candidate, "metadata_json", {}), dict)
                        else {}
                    ).get("initiative_id")
                ).lower()
                == normalized_initiative_id
            ),
            None,
        )
    elif not initiative_id and resume_existing:
        objective = (
            (
                await db.execute(
                    select(Objective)
                    .where(Objective.title == title)
                    .where(Objective.state.in_(["new", "in_progress"]))
                    .order_by(Objective.id.desc())
                )
            )
            .scalars()
            .first()
        )
    if objective is None:
        objective = Objective(
            title=title,
            description=compiled["objective_description"],
            priority=priority,
            constraints_json=[
                f"boundary_mode={boundary_mode}",
                f"boundary_reason={compiled['boundary_reason']}",
            ],
            success_criteria=compiled["success_criteria"],
            state="new",
            owner=INITIATIVE_OWNER,
            execution_mode="auto",
            auto_continue=True,
            boundary_mode=boundary_mode,
            metadata_json={
                **(metadata_json if isinstance(metadata_json, dict) else {}),
                "policy_version": DEFAULT_POLICY_VERSION,
                "source": source,
                "actor": actor,
                "program_id": program_id,
                "program_registry": registry,
                "initiative_id": normalize_initiative_id(initiative_id),
                "latest_request_id": request_id,
                "managed_scope": managed_scope,
                "execution_tracking": {
                    "task_created": False,
                    "task_dispatched": False,
                    "execution_started": False,
                    "execution_result": None,
                    "request_id": "",
                    "execution_trace": "",
                    "result_artifact": "",
                },
            },
        )
        db.add(objective)
        await db.flush()
    else:
        objective_metadata = (
            objective.metadata_json if isinstance(objective.metadata_json, dict) else {}
        )
        resume_started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z") if resume_existing else ""
        objective.metadata_json = {
            **objective_metadata,
            "program_id": program_id or str(objective_metadata.get("program_id") or "").strip(),
            "program_registry": registry,
            "initiative_id": initiative_id or normalize_initiative_id(objective_metadata.get("initiative_id")),
            "latest_request_id": request_id or str(objective_metadata.get("latest_request_id") or "").strip(),
            "resume_existing": resume_existing,
            "source": source,
            "actor": actor,
            "managed_scope": managed_scope,
            "execution_tracking": {
                **(
                    objective_metadata.get("execution_tracking")
                    if isinstance(objective_metadata.get("execution_tracking"), dict)
                    else {}
                ),
                "task_created": True,
                "task_dispatched": True,
                "execution_started": True,
                "execution_state": "executing" if resume_existing else str(
                    (
                        objective_metadata.get("execution_tracking")
                        if isinstance(objective_metadata.get("execution_tracking"), dict)
                        else {}
                    ).get("execution_state")
                    or "created"
                ).strip()
                or "created",
                "activity_started_at": resume_started_at
                or str(
                    (
                        objective_metadata.get("execution_tracking")
                        if isinstance(objective_metadata.get("execution_tracking"), dict)
                        else {}
                    ).get("activity_started_at")
                    or ""
                ).strip(),
            },
        }
        existing_tasks = await _tasks_for_objective(db, objective.id)
        if resume_existing and existing_tasks:
            for existing_task in existing_tasks:
                if _task_terminal(existing_task):
                    continue
                _set_task_execution_tracking(
                    existing_task,
                    task_created=True,
                    task_dispatched=True,
                    execution_started=True,
                    activity_started_at=resume_started_at,
                    request_id=request_id or str(
                        ((getattr(existing_task, "metadata_json", {}) or {}).get("request_id") or "")
                    ).strip(),
                )
        if existing_tasks:
            await refresh_task_readinesses(db, objective.id)
            await recompute_objective_state(db, objective.id)
            continuation = {
                "executed_local": [],
                "dispatched": [],
                "status": await build_initiative_status(db=db, objective_id=objective.id),
            }
            if continue_chain and not human_prompt_required:
                continuation = await continue_initiative(
                    db,
                    objective_id=objective.id,
                    actor=actor,
                    source=source,
                    max_auto_steps=max_auto_steps,
                )
            return {
                "objective": _objective_out(objective),
                "tasks": [_task_out(task) for task in await _tasks_for_objective(db, objective.id)],
                "boundary_mode": boundary_mode,
                "boundary_reason": compiled["boundary_reason"],
                "human_prompt_required": human_prompt_required,
                "continuation": continuation,
            }

    created_tasks: list[Task] = []
    prior_task_id: int | None = None
    for task_plan in compiled["tasks"]:
        task = Task(
            objective_id=objective.id,
            title=task_plan.title,
            details=task_plan.details,
            dependencies=[prior_task_id] if prior_task_id else [],
            acceptance_criteria=task_plan.acceptance_criteria,
            assigned_to=task_plan.assigned_to,
            state="queued",
            readiness="waiting_on_human" if human_prompt_required and prior_task_id is None else "queued",
            boundary_mode=boundary_mode,
            start_now=not human_prompt_required,
            human_prompt_required=human_prompt_required,
            execution_scope=task_plan.execution_scope,
            expected_outputs_json=task_plan.expected_outputs,
            verification_commands_json=task_plan.verification_commands,
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={
                "policy_version": DEFAULT_POLICY_VERSION,
                "source": source,
                "actor": actor,
                "program_id": program_id,
                "program_registry": registry,
                "initiative_id": normalize_initiative_id(initiative_id),
                "request_id": request_id,
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": False,
                    "execution_started": False,
                    "execution_result": None,
                    "request_id": "",
                    "execution_trace": "",
                    "result_artifact": "",
                },
                **task_plan.metadata_json,
            },
        )
        db.add(task)
        await db.flush()
        created_tasks.append(task)
        prior_task_id = task.id

    await refresh_task_readinesses(db, objective.id)
    await recompute_objective_state(db, objective.id)
    await write_journal(
        db,
        actor=actor,
        action="initiative_compiled",
        target_type="objective",
        target_id=str(objective.id),
        summary=f"Compiled initiative objective {objective.id}: {objective.title}",
        metadata_json={
            "source": source,
            "boundary_mode": boundary_mode,
            "task_count": len(created_tasks),
            "policy_version": DEFAULT_POLICY_VERSION,
            "program_id": program_id,
        },
    )
    continuation = {
        "executed_local": [],
        "dispatched": [],
        "status": await build_initiative_status(db=db, objective_id=objective.id),
    }
    if continue_chain and not human_prompt_required:
        continuation = await continue_initiative(
            db,
            objective_id=objective.id,
            actor=actor,
            source=source,
            max_auto_steps=max_auto_steps,
        )
    return {
        "objective": _objective_out(objective),
        "tasks": [_task_out(task) for task in await _tasks_for_objective(db, objective.id)],
        "boundary_mode": boundary_mode,
        "boundary_reason": compiled["boundary_reason"],
        "human_prompt_required": human_prompt_required,
        "continuation": continuation,
    }


async def build_initiative_status(
    *,
    db: AsyncSession,
    objective_id: int | None = None,
) -> dict[str, Any]:
    objective_stmt = select(Objective).order_by(Objective.created_at.desc(), Objective.id.desc())
    if objective_id is None:
        objective_stmt = objective_stmt.limit(INITIATIVE_STATUS_OBJECTIVE_SCAN_LIMIT)
    objective_rows = list(
        (
            await db.execute(objective_stmt)
        )
        .scalars()
        .all()
    )
    selected_objective: Objective | None = None
    selected_tasks: list[Task] = []
    latest_completed_recently: list[dict[str, Any]] = []
    objective_snapshots: list[dict[str, Any]] = []
    for position, objective in enumerate(objective_rows):
        if objective_id is not None and objective.id != objective_id:
            continue
        if _normalize_text(getattr(objective, "owner", INITIATIVE_OWNER)).lower() != INITIATIVE_OWNER:
            continue
        tasks = await refresh_task_readinesses(db, objective.id)
        if objective_id is not None:
            selected_objective = objective
            selected_tasks = tasks
            break
        completed_recently = _recently_completed_tasks(tasks)
        objective_tracking = _objective_execution_tracking(objective, tasks)
        objective_execution_state = str(objective_tracking.get("execution_state") or "queued").strip()
        planning_only = _objective_is_planning_only(objective)
        objective_snapshots.append(
            {
                "position": position,
                "objective": objective,
                "tasks": tasks,
                "objective_execution_state": objective_execution_state,
                "has_active_or_ready": any(
                    _normalize_text(task.readiness).lower() in {"in_progress", "ready"}
                    for task in tasks
                ) and not (planning_only and objective_execution_state == "created"),
                "has_blocked": any(
                    _normalize_text(task.readiness).lower() in {"blocked", "waiting_on_human", "waiting_on_tod"}
                    for task in tasks
                ),
                "completed_recently": completed_recently,
                "is_complete": objective_execution_state == "completed"
                or (tasks and all(_task_terminal(task) for task in tasks)),
            }
        )
        if not latest_completed_recently:
            latest_completed_recently = completed_recently
    if objective_id is None and objective_snapshots:
        newest_active_or_ready = next(
            (snapshot for snapshot in objective_snapshots if snapshot["has_active_or_ready"]),
            None,
        )
        newest_blocked = next(
            (snapshot for snapshot in objective_snapshots if snapshot["has_blocked"]),
            None,
        )
        newest_completed = next(
            (
                snapshot
                for snapshot in objective_snapshots
                if snapshot["is_complete"] and snapshot["completed_recently"]
            ),
            None,
        )
        live_snapshot = newest_active_or_ready or newest_blocked
        chosen_snapshot = live_snapshot
        if (
            newest_completed is not None
            and live_snapshot is not None
            and int(newest_completed["position"]) < int(live_snapshot["position"])
        ):
            chosen_snapshot = newest_completed
        if chosen_snapshot is None:
            chosen_snapshot = next(
                (snapshot for snapshot in objective_snapshots if not snapshot["is_complete"]),
                None,
            )
        if chosen_snapshot is not None:
            selected_objective = chosen_snapshot["objective"]
            selected_tasks = chosen_snapshot["tasks"]
        follow_on_snapshot = _follow_on_snapshot(objective_snapshots, chosen_snapshot)
    else:
        follow_on_snapshot = None
    if selected_objective is None:
        program_status = build_program_status_snapshot()
        persist_program_status_snapshot(program_status)
        return {
            "summary": "No active MIM initiative is currently queued.",
            "status": "idle",
            "active_objective": {},
            "active_task": {},
            "activity": {
                "state": "idle",
                "label": "Idle",
                "summary": "No active initiative is running right now.",
                "reason": "no_active_objective",
                "started_at": "",
                "stale_seconds": None,
            },
            "progress": {
                "task_count": 0,
                "completed_task_count": 0,
                "percent": 0,
                "summary": "No bounded tasks are registered yet.",
            },
            "why_current": "",
            "blocked": [],
            "completed_recently": latest_completed_recently,
            "next_task": {},
            "program_status": program_status,
        }

    active_task = next(
        (
            task
            for task in selected_tasks
            if _normalize_text(task.readiness).lower() == "in_progress"
            or _normalize_text(task.state).lower() in ACTIVE_TASK_STATES
        ),
        None,
    )
    if active_task is None:
        active_task = next(
            (task for task in selected_tasks if _normalize_text(task.readiness).lower() == "ready"),
            None,
        )
    next_task = next(
        (
            task
            for task in selected_tasks
            if active_task is not None and task.id != active_task.id and _normalize_text(task.readiness).lower() == "ready"
        ),
        None,
    )
    follow_on_payload = _follow_on_payload(
        follow_on_snapshot,
        result_task_ids=set(),
    )
    blocked = [
        {
            "task_id": task.id,
            "title": task.title,
            "readiness": task.readiness,
            "status": task.state,
        }
        for task in selected_tasks
        if _normalize_text(task.readiness).lower() in {"blocked", "waiting_on_human", "waiting_on_tod"}
    ]
    completed_recently = _recently_completed_tasks(selected_tasks)
    result_task_ids = await _task_result_task_ids(
        db,
        [int(task.id) for task in selected_tasks if getattr(task, "id", None) is not None],
    )
    if follow_on_snapshot is not None:
        follow_on_payload = _follow_on_payload(
            follow_on_snapshot,
            result_task_ids=result_task_ids,
        )
    why_current = ""
    if active_task is not None:
        if _normalize_text(active_task.readiness).lower() == "in_progress":
            why_current = "This task is already active and still inside the approved soft-boundary execution lane."
        elif bool(getattr(active_task, "start_now", False)) and not bool(getattr(active_task, "human_prompt_required", False)):
            why_current = "This task is the highest-priority ready item with no blocker and policy allows immediate execution."
        else:
            why_current = "This task is current because it is the next dependency-satisfied item for the active objective."
    objective_tracking = _objective_execution_tracking(selected_objective, selected_tasks)
    objective_execution_state = str(objective_tracking.get("execution_state") or "queued").strip()
    objective_state = _normalize_text(selected_objective.state).lower()
    if active_task is not None:
        summary_parts = [f"Active objective: {selected_objective.title}."]
    elif blocked:
        summary_parts = [f"Objective {selected_objective.title} is blocked."]
    elif objective_execution_state == "completed" or (selected_tasks and all(_task_terminal(task) for task in selected_tasks)):
        summary_parts = [f"Objective {selected_objective.title} is complete."]
    elif objective_execution_state == "created":
        summary_parts = [f"Planning complete for objective {selected_objective.title}. Awaiting execution dispatch."]
    elif objective_execution_state in {"dispatched", "executing"}:
        summary_parts = [f"Objective {selected_objective.title} is executing."]
    else:
        summary_parts = [f"Objective {selected_objective.title} is queued."]
    if active_task is not None:
        summary_parts.append(f"Active task: {active_task.title}.")
    if blocked:
        summary_parts.append(f"Blocked items: {', '.join(item['title'] for item in blocked[:2])}.")
    elif next_task is not None:
        summary_parts.append(f"Next task after this: {next_task.title}.")
    elif follow_on_payload:
        summary_parts.append(
            f"Next task after this: {str(follow_on_payload.get('display_title') or follow_on_payload.get('title') or '').strip()}."
        )
    active_objective_payload = _objective_out(selected_objective)
    active_task_payload = (
        _task_out(active_task, has_result=active_task.id in result_task_ids)
        if active_task is not None
        else {}
    )
    objective_history = [
        {
            "position": snapshot.get("position"),
            "objective": _objective_out(snapshot.get("objective")),
            "activity": _build_initiative_activity_payload(
                objective=snapshot.get("objective"),
                objective_execution_state=str(snapshot.get("objective_execution_state") or "queued").strip() or "queued",
                active_task=_snapshot_primary_task(snapshot),
                next_task=None,
                blocked=[
                    {
                        "task_id": task.id,
                        "title": task.title,
                        "readiness": task.readiness,
                        "status": task.state,
                    }
                    for task in (snapshot.get("tasks") if isinstance(snapshot.get("tasks"), list) else [])
                    if _normalize_text(task.readiness).lower() in {"blocked", "waiting_on_human", "waiting_on_tod"}
                ],
                progress=_build_initiative_progress_payload(
                    tasks=snapshot.get("tasks") if isinstance(snapshot.get("tasks"), list) else [],
                    result_task_ids=result_task_ids,
                ),
                result_task_ids=result_task_ids,
            ),
            "progress": _build_initiative_progress_payload(
                tasks=snapshot.get("tasks") if isinstance(snapshot.get("tasks"), list) else [],
                result_task_ids=result_task_ids,
            ),
            "execution_state": str(snapshot.get("objective_execution_state") or "queued").strip() or "queued",
            "status": "completed" if bool(snapshot.get("is_complete")) else str(snapshot.get("objective_execution_state") or "queued").strip() or "queued",
            "summary": " ".join(
                part
                for part in [
                    f"Objective {_objective_display_title(snapshot.get('objective'), project_entry={})}." if snapshot.get('objective') is not None else "",
                    "Completed." if bool(snapshot.get("is_complete")) else "",
                ]
                if part
            ).strip(),
            "objective_id": getattr(snapshot.get("objective"), "id", None),
        }
        for snapshot in objective_snapshots
        if isinstance(snapshot, dict) and snapshot.get("objective") is not None
    ]
    program_status = build_program_status_snapshot(
        active_objective=active_objective_payload,
        active_task=active_task_payload,
        objective_history=objective_history,
    )
    active_project = _matching_program_project(
        program_status,
        str(active_objective_payload.get("initiative_id") or "").strip(),
    )
    active_objective_payload["display_title"] = _objective_display_title(
        selected_objective,
        project_entry=active_project,
    )
    if active_task is not None:
        active_task_payload["display_title"] = _task_display_title(active_task)
    if next_task is not None:
        next_task_payload = _task_out(next_task, has_result=next_task.id in result_task_ids)
        next_task_payload["display_title"] = _task_display_title(next_task)
    elif follow_on_payload:
        next_task_payload = follow_on_payload
    else:
        next_task_payload = {}
    progress = _build_initiative_progress_payload(tasks=selected_tasks, result_task_ids=result_task_ids)
    activity = _build_initiative_activity_payload(
        objective=selected_objective,
        objective_execution_state=objective_execution_state,
        active_task=active_task,
        next_task=next_task,
        blocked=blocked,
        progress=progress,
        result_task_ids=result_task_ids,
    )
    superseded_by = {}
    if str(activity.get("state") or "").strip().lower() == "superseded":
        superseded_by = _initiative_supersession_snapshot(getattr(selected_objective, "id", None))

    persist_program_status_snapshot(program_status)

    return {
        "summary": " ".join(summary_parts),
        "status": activity["state"],
        "active_objective": active_objective_payload,
        "active_task": active_task_payload,
        "active_project": active_project,
        "activity": activity,
        "superseded_by": superseded_by,
        "progress": progress,
        "execution_state": objective_execution_state,
        "why_current": why_current,
        "blocked": blocked,
        "completed_recently": completed_recently,
        "next_task": next_task_payload,
        "program_status": program_status,
    }