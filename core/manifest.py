from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from core.config import PROJECT_ROOT, settings

CONTRACT_VERSION = "tod-mim-shared-contract-v1"
MANIFEST_VERSION = "1"
SCHEMA_VERSION = "2026-03-09-01"

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
]

RECENT_CHANGES = [
    "Added review endpoint",
    "Added journal endpoint",
    "Aligned structured workflow schema",
    "Added manifest endpoint and metadata",
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
        },
    }
