from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import subprocess

from core.config import PROJECT_ROOT, settings

CONTRACT_VERSION = "tod-mim-shared-contract-v1"
MANIFEST_VERSION = "1"
SCHEMA_VERSION = "2026-03-10-03"

SIGNATURE_FILES = [
    "core/models.py",
    "core/schemas.py",
    "core/routers/__init__.py",
    "core/routers/manifest.py",
    "core/config.py",
    "core/manifest.py",
    "docs/tod-mim-bridge.md",
]

CAPABILITIES = [
    "health",
    "status",
    "manifest",
    "objectives",
    "tasks",
    "results",
    "reviews",
    "journal",
    "routing_history",
    "routing_engines",
    "routing_stats",
    "routing_engine_detail",
    "routing_task_stats",
    "goals",
    "actions",
    "custody_chain",
    "goal_plans",
    "goal_timeline",
    "goal_status",
    "goal_recovery",
]

RECENT_CHANGES = [
    "Added review endpoint",
    "Added journal endpoint",
    "Aligned structured workflow schema",
    "Added manifest endpoint and metadata",
    "Added goal/action/state/validation custody chain",
    "Added multi-step goal execution planning and timeline/status endpoints",
    "Added retry/skip/replace/resume recovery operations for goal chains",
]


def _signature_input() -> str:
    blocks: list[str] = [
        f"contract_version={CONTRACT_VERSION}",
        f"schema_version={SCHEMA_VERSION}",
    ]

    for rel_path in SIGNATURE_FILES:
        file_path = PROJECT_ROOT / rel_path
        if file_path.exists():
            content_hash = sha256(file_path.read_bytes()).hexdigest()
            blocks.append(f"{rel_path}:{content_hash}")
        else:
            blocks.append(f"{rel_path}:missing")

    return "\n".join(blocks)


def _last_updated_at() -> datetime:
    timestamps: list[float] = []
    for rel_path in SIGNATURE_FILES:
        file_path = PROJECT_ROOT / rel_path
        if file_path.exists():
            timestamps.append(file_path.stat().st_mtime)

    if not timestamps:
        return datetime.now(timezone.utc)

    return datetime.fromtimestamp(max(timestamps), tz=timezone.utc)


def build_repo_signature() -> str:
    seed = _signature_input()
    return f"sha256:{sha256(seed.encode('utf-8')).hexdigest()}"


def _resolve_git_sha() -> str:
    if settings.build_git_sha and settings.build_git_sha != "unknown":
        return settings.build_git_sha

    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def build_manifest() -> dict:
    return {
        "system_name": "MIM",
        "system_version": settings.app_version,
        "manifest_version": MANIFEST_VERSION,
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "environment": settings.environment,
        "release_tag": settings.release_tag,
        "config_profile": settings.config_profile,
        "git_sha": _resolve_git_sha(),
        "build_timestamp": settings.build_timestamp,
        "repo_signature": build_repo_signature(),
        "capabilities": CAPABILITIES,
        "recent_changes": RECENT_CHANGES,
        "last_updated_at": _last_updated_at(),
        "generated_at": datetime.now(timezone.utc),
        "endpoints": [
            "/health",
            "/status",
            "/manifest",
            "/objectives",
            "/tasks",
            "/results",
            "/reviews",
            "/routing/history",
            "/routing/engines",
            "/routing/engines/{engine_name}",
            "/routing/stats",
            "/routing/tasks/{task_id}",
            "/routing/tasks/{task_id}/stats",
            "/goals",
            "/goals/{goal_id}",
            "/goals/{goal_id}/plan",
            "/goals/{goal_id}/timeline",
            "/goals/{goal_id}/status",
            "/goals/{goal_id}/resume",
            "/actions/{action_id}",
            "/actions/{action_id}/retry",
            "/actions/{action_id}/skip",
            "/actions/{action_id}/replace",
            "/goals/{goal_id}/custody",
            "/tasks/{task_id}/custody",
            "/journal",
            "/memory",
            "/tools",
            "/services",
        ],
        "objects": {
            "Objective": [
                "objective_id",
                "title",
                "description",
                "priority",
                "constraints",
                "success_criteria",
                "status",
                "created_at",
            ],
            "Task": [
                "task_id",
                "objective_id",
                "title",
                "scope",
                "dependencies",
                "acceptance_criteria",
                "status",
                "assigned_to",
            ],
            "Result": [
                "result_id",
                "task_id",
                "summary",
                "files_changed",
                "tests_run",
                "test_results",
                "failures",
                "recommendations",
                "created_at",
            ],
            "Review": [
                "review_id",
                "task_id",
                "decision",
                "rationale",
                "continue_allowed",
                "escalate_to_user",
                "created_at",
            ],
            "JournalEntry": [
                "entry_id",
                "actor",
                "action",
                "target_type",
                "target_id",
                "summary",
                "timestamp",
            ],
            "Goal": [
                "goal_id",
                "objective_id",
                "task_id",
                "goal_type",
                "goal_description",
                "requested_by",
                "priority",
                "status",
                "created_at",
            ],
            "Action": [
                "action_id",
                "goal_id",
                "engine",
                "action_type",
                "input_ref",
                "expected_state_delta",
                "validation_method",
                "retry_of_action_id",
                "retry_count",
                "replaced_action_id",
                "replacement_action_id",
                "recovery_classification",
                "chain_event",
                "started_at",
                "completed_at",
                "status",
            ],
            "StateSnapshot": [
                "snapshot_id",
                "goal_id",
                "action_id",
                "snapshot_phase",
                "state_type",
                "state_payload",
                "captured_at",
            ],
            "ValidationResult": [
                "validation_id",
                "goal_id",
                "action_id",
                "validation_method",
                "validation_status",
                "validation_details",
                "validated_at",
            ],
            "GoalPlan": [
                "goal_id",
                "ordered_action_ids",
                "current_step_index",
                "derived_status",
            ],
        },
    }
