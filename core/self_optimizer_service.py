"""MIM self-optimization service: proposes and executes bounded self-improvements.

Bridges health monitor diagnostics with approval governance and runtime adjustment.
All changes are approval-gated, audited, and reversible.
"""

from __future__ import annotations

import dataclasses
import asyncio
import datetime
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from sqlalchemy import select
from enum import Enum
from pathlib import Path
from typing import Any
from types import SimpleNamespace

from core.privileged_actions import run_privileged_action

logger = logging.getLogger(__name__)

_AUTHORITATIVE_REQUEST_PATTERN = re.compile(r"objective-(?P<objective_id>\d+)-task-(?P<task_id>\d+)")


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


class OptimizationStatus(str, Enum):
    """Optimization proposal lifecycle."""
    PROPOSED = "proposed"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


@dataclasses.dataclass
class OptimizationProposal:
    """Tracked optimization proposal with approval/execution state."""
    proposal_id: str
    recommendation_id: str
    title: str
    description: str
    proposed_action: str
    rollback_action: str | None
    requires_approval: bool
    severity: str  # "low", "medium", "high"
    estimated_impact_percent: int | None
    status: OptimizationStatus = OptimizationStatus.PROPOSED
    created_at: str | None = None
    approved_at: str | None = None
    approval_reason: str | None = None
    executed_at: str | None = None
    execution_result: dict[str, Any] | None = None
    error_message: str | None = None
    rolled_back_at: str | None = None
    audit_trail: list[dict[str, Any]] | None = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = _utc_now_iso()
        if self.audit_trail is None:
            self.audit_trail = []


class SelfOptimizerService:
    """Service that proposes, tracks, and executes self-optimizations with governance."""

    def __init__(self, state_dir: Path = Path("runtime/shared")):
        """Initialize optimizer service."""
        self.state_dir = state_dir
        self.proposals: dict[str, OptimizationProposal] = {}
        self._load_proposals()

    def _load_proposals(self) -> None:
        """Load existing proposals from disk."""
        proposals_file = self.state_dir / "mim_self_optimization_proposals.latest.json"
        if proposals_file.exists():
            try:
                data = json.loads(proposals_file.read_text(encoding="utf-8"))
                for prop_data in data.get("proposals", []):
                    prop = OptimizationProposal(
                        proposal_id=prop_data["proposal_id"],
                        recommendation_id=prop_data["recommendation_id"],
                        title=prop_data["title"],
                        description=prop_data["description"],
                        proposed_action=prop_data["proposed_action"],
                        rollback_action=prop_data.get("rollback_action"),
                        requires_approval=prop_data.get("requires_approval", True),
                        severity=prop_data.get("severity", "medium"),
                        estimated_impact_percent=prop_data.get("estimated_impact_percent"),
                        status=OptimizationStatus(prop_data.get("status", "proposed")),
                    )
                    self.proposals[prop.proposal_id] = prop
            except Exception as e:
                logger.warning(f"Failed to load proposals: {e}")

    def propose_optimization(
        self,
        recommendation_id: str,
        title: str,
        description: str,
        proposed_action: str,
        rollback_action: str | None = None,
        requires_approval: bool = True,
        severity: str = "medium",
        estimated_impact_percent: int | None = None,
    ) -> OptimizationProposal:
        """Create and track a new optimization proposal."""
        proposal_id = f"opt-{recommendation_id}-{int(_utc_now().timestamp()*1000)}"
        proposal = OptimizationProposal(
            proposal_id=proposal_id,
            recommendation_id=recommendation_id,
            title=title,
            description=description,
            proposed_action=proposed_action,
            rollback_action=rollback_action,
            requires_approval=requires_approval,
            severity=severity,
            estimated_impact_percent=estimated_impact_percent,
        )
        self.proposals[proposal_id] = proposal
        self._audit(proposal, "created")
        self._persist()
        return proposal

    def approve_proposal(self, proposal_id: str, reason: str = "") -> OptimizationProposal:
        """Operator approval of a proposed optimization."""
        proposal = self.proposals.get(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found")
        if proposal.status != OptimizationStatus.PROPOSED:
            raise ValueError(f"Cannot approve proposal in status {proposal.status}")

        proposal.status = OptimizationStatus.APPROVED
        proposal.approved_at = _utc_now_iso()
        proposal.approval_reason = reason
        self._audit(proposal, "approved", {"reason": reason})
        self._persist()
        return proposal

    def reject_proposal(self, proposal_id: str, reason: str = "") -> OptimizationProposal:
        """Operator rejection of a proposed optimization."""
        proposal = self.proposals.get(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found")
        if proposal.status != OptimizationStatus.PROPOSED:
            raise ValueError(f"Cannot reject proposal in status {proposal.status}")

        proposal.status = OptimizationStatus.REJECTED
        self._audit(proposal, "rejected", {"reason": reason})
        self._persist()
        return proposal

    def execute_proposal(self, proposal_id: str) -> OptimizationProposal:
        """Execute an approved optimization (typically called by orchestration service)."""
        proposal = self.proposals.get(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found")

        if proposal.requires_approval and proposal.status != OptimizationStatus.APPROVED:
            raise ValueError(
                f"Proposal {proposal_id} requires approval before execution (current status: {proposal.status})"
            )

        # Mark execution state
        proposal.status = OptimizationStatus.EXECUTING
        self._audit(proposal, "execution_started")
        self._persist()

        # In production, this would call actual optimization handlers.
        # For now, just simulate successful completion.
        try:
            result = self._execute_action(proposal.proposed_action)
            proposal.status = OptimizationStatus.COMPLETED
            proposal.executed_at = _utc_now_iso()
            proposal.execution_result = result
            self._audit(proposal, "execution_completed", result)
        except Exception as e:
            proposal.status = OptimizationStatus.FAILED
            proposal.error_message = str(e)
            self._audit(proposal, "execution_failed", {"error": str(e)})
            logger.error(f"Optimization execution failed: {e}")

        self._persist()
        return proposal

    async def execute_proposal_async(self, proposal_id: str) -> OptimizationProposal:
        """Execute an optimization proposal from an async caller without nesting event loops."""
        proposal = self.proposals.get(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found")

        if proposal.requires_approval and proposal.status != OptimizationStatus.APPROVED:
            raise ValueError(
                f"Proposal {proposal_id} requires approval before execution (current status: {proposal.status})"
            )

        proposal.status = OptimizationStatus.EXECUTING
        self._audit(proposal, "execution_started")
        self._persist()

        try:
            result = await self._execute_action_async(proposal.proposed_action)
            proposal.status = OptimizationStatus.COMPLETED
            proposal.executed_at = _utc_now_iso()
            proposal.execution_result = result
            self._audit(proposal, "execution_completed", result)
        except Exception as e:
            proposal.status = OptimizationStatus.FAILED
            proposal.error_message = str(e)
            self._audit(proposal, "execution_failed", {"error": str(e)})
            logger.error(f"Optimization execution failed: {e}")

        self._persist()
        return proposal

    def rollback_proposal(self, proposal_id: str) -> OptimizationProposal:
        """Rollback a completed optimization to previous state."""
        proposal = self.proposals.get(proposal_id)
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found")
        if proposal.status != OptimizationStatus.COMPLETED:
            raise ValueError(f"Cannot rollback proposal in status {proposal.status}")
        if not proposal.rollback_action:
            raise ValueError(f"Proposal {proposal_id} does not support rollback")

        try:
            self._execute_action(proposal.rollback_action)
            proposal.status = OptimizationStatus.ROLLED_BACK
            proposal.rolled_back_at = _utc_now_iso()
            self._audit(proposal, "rolled_back")
        except Exception as e:
            self._audit(proposal, "rollback_failed", {"error": str(e)})
            logger.error(f"Rollback failed: {e}")
            raise

        self._persist()
        return proposal

    def get_proposal(self, proposal_id: str) -> OptimizationProposal | None:
        """Retrieve a proposal by ID."""
        return self.proposals.get(proposal_id)

    def list_proposals(
        self, status: OptimizationStatus | None = None, severity: str | None = None
    ) -> list[OptimizationProposal]:
        """List all proposals, optionally filtered."""
        proposals = list(self.proposals.values())
        if status:
            proposals = [p for p in proposals if p.status == status]
        if severity:
            proposals = [p for p in proposals if p.severity == severity]
        return sorted(proposals, key=lambda p: p.created_at or "", reverse=True)

    def _execute_action(self, action_name: str) -> dict[str, Any]:
        """Execute an optimization action. Stub for actual handlers."""
        # In production, dispatch to actual optimization handlers
        actions = {
            "trigger_garbage_collection": self._action_gc,
            "increase_worker_pool_size": self._action_scale_workers,
            "increase_cache_size": self._action_scale_cache,
            "reduce_state_bus_batch_timeout": self._action_tune_statebus,
            "decrease_worker_pool_size": self._action_scale_workers_down,
            "decrease_cache_size": self._action_scale_cache_down,
            "restore_state_bus_batch_timeout": self._action_restore_statebus,
            "refresh_shared_export_artifacts": self._action_refresh_shared_export_artifacts,
            "recover_bridge_coordination": self._action_recover_bridge_coordination,
            "fallback_to_codex_direct_execution": self._action_fallback_to_codex_direct_execution,
            "deduplicate_bridge_watchers": self._action_deduplicate_bridge_watchers,
            "inspect_runtime_devices_and_browser": self._action_inspect_runtime_devices_and_browser,
            "monitor_runtime_recovery_evidence": self._action_monitor_runtime_recovery_evidence,
        }
        if action_name not in actions:
            raise ValueError(f"Unknown optimization action: {action_name}")
        return actions[action_name]()

    async def _execute_action_async(self, action_name: str) -> dict[str, Any]:
        """Execute an optimization action from an async context."""
        async_actions = {
            "recover_bridge_coordination": self._action_recover_bridge_coordination_async,
            "fallback_to_codex_direct_execution": self._action_fallback_to_codex_direct_execution_async,
        }
        handler = async_actions.get(action_name)
        if handler is not None:
            return await handler()
        return self._execute_action(action_name)

    def _action_gc(self) -> dict[str, Any]:
        """Trigger GC and return result summary."""
        import gc
        collected = gc.collect()
        return {"action": "gc", "objects_collected": collected, "timestamp": _utc_now_iso()}

    def _action_scale_workers(self) -> dict[str, Any]:
        """Increase worker pool (stub)."""
        return {"action": "scale_workers", "new_size": 12, "previous_size": 8, "status": "simulated"}

    def _action_scale_workers_down(self) -> dict[str, Any]:
        """Decrease worker pool (stub)."""
        return {"action": "scale_workers_down", "new_size": 8, "previous_size": 12, "status": "simulated"}

    def _action_scale_cache(self) -> dict[str, Any]:
        """Increase cache size (stub)."""
        return {"action": "scale_cache", "new_size_mb": 512, "previous_size_mb": 256, "status": "simulated"}

    def _action_scale_cache_down(self) -> dict[str, Any]:
        """Decrease cache size (stub)."""
        return {"action": "scale_cache_down", "new_size_mb": 256, "previous_size_mb": 512, "status": "simulated"}

    def _action_tune_statebus(self) -> dict[str, Any]:
        """Tune state bus batch timeout (stub)."""
        return {"action": "tune_statebus", "new_timeout_ms": 50, "previous_timeout_ms": 100, "status": "simulated"}

    def _action_restore_statebus(self) -> dict[str, Any]:
        """Restore state bus batch timeout (stub)."""
        return {"action": "restore_statebus", "new_timeout_ms": 100, "previous_timeout_ms": 50, "status": "simulated"}

    def _action_refresh_shared_export_artifacts(self) -> dict[str, Any]:
        """Refresh shared export artifacts from current workspace state."""
        root_dir = self.state_dir.parent.parent
        exporter = root_dir / "scripts" / "export_mim_context.py"
        if not exporter.exists():
            raise ValueError(f"exporter not found: {exporter}")
        completed = subprocess.run(
            [
                sys.executable,
                str(exporter),
                "--output-dir",
                str(self.state_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(root_dir),
        )
        if completed.returncode != 0:
            raise ValueError((completed.stderr or completed.stdout or "shared export refresh failed").strip())
        payload = {}
        stdout = (completed.stdout or "").strip()
        if stdout.startswith("{"):
            try:
                payload = json.loads(stdout)
            except Exception:
                payload = {}
        return {
            "action": "refresh_shared_export_artifacts",
            "status": "completed",
            "objective_active": payload.get("objective_active"),
            "schema_version": payload.get("schema_version"),
            "release_tag": payload.get("release_tag"),
            "stdout": stdout,
        }

    def _republish_active_task_request_surface(self) -> dict[str, Any]:
        """Republish the active initiative request surface into the TOD bridge artifacts."""
        return asyncio.run(self._republish_active_task_request_surface_async())

    def _authoritative_stale_guard_request(self) -> dict[str, Any] | None:
        """Stale-guard metadata is diagnostic only and cannot become authoritative request lineage."""
        return None

    def _same_task_bridge_evidence_present(self, *, active_task_id: str) -> bool:
        if not active_task_id:
            return False

        def _matches(payload: dict[str, Any]) -> bool:
            if not isinstance(payload, dict):
                return False
            bridge_runtime = payload.get("bridge_runtime") if isinstance(payload.get("bridge_runtime"), dict) else {}
            current_processing = (
                bridge_runtime.get("current_processing")
                if isinstance(bridge_runtime.get("current_processing"), dict)
                else {}
            )
            candidate_values = {
                str(payload.get("task_id") or "").strip(),
                str(payload.get("request_id") or "").strip(),
                str(current_processing.get("task_id") or "").strip(),
                str(current_processing.get("request_id") or "").strip(),
            }
            candidate_values.discard("")
            return active_task_id in candidate_values

        return any(
            _matches(self._read_json(path))
            for path in (
                self.state_dir / "TOD_MIM_TASK_ACK.latest.json",
                self.state_dir / "TOD_MIM_TASK_RESULT.latest.json",
                self.state_dir / "TOD_MIM_COMMAND_STATUS.latest.json",
            )
        )

    async def _republish_active_task_request_surface_async(self) -> dict[str, Any]:
        """Async variant of active-task request surface republishing."""
        from core.autonomy_driver_service import (  # local import keeps startup light
            _publish_codex_dispatch_bridge_artifacts,
            build_initiative_status,
        )

        authoritative_request = self._authoritative_stale_guard_request()
        if authoritative_request is not None:
            objective_id = int(authoritative_request["objective_id"])
            task_id = int(authoritative_request["task_id"])
            request_id = str(authoritative_request["request_id"])
            objective = SimpleNamespace(
                id=objective_id,
                priority="high",
                title=f"objective-{objective_id}",
            )
            task = SimpleNamespace(
                id=task_id,
                title=str(authoritative_request.get("title") or request_id).strip(),
                details=str(authoritative_request.get("details") or request_id).strip(),
                acceptance_criteria="Restore bridge publication to the stale-guard authoritative request.",
                execution_scope="bounded_development",
            )
            bridge_artifacts = _publish_codex_dispatch_bridge_artifacts(
                objective=objective,
                task=task,
                request_id=request_id,
                submission_status="queued",
                latest_result_summary="Republished the stale-guard authoritative request surface.",
                shared_root=self.state_dir,
            )
            return {
                "objective_id": objective_id,
                "task_id": task_id,
                "request_id": request_id,
                "bridge_artifacts": bridge_artifacts,
                "source": "stale_guard_high_watermark",
            }

        from core.db import SessionLocal

        async with SessionLocal() as db:
            initiative = await build_initiative_status(db=db)

        active_objective = initiative.get("active_objective") if isinstance(initiative.get("active_objective"), dict) else {}
        active_task = initiative.get("active_task") if isinstance(initiative.get("active_task"), dict) else {}
        if not active_objective or not active_task:
            raise ValueError("No active initiative task is available to republish")

        objective_id = int(active_objective.get("objective_id") or active_objective.get("id") or 0)
        task_id = int(active_task.get("task_id") or active_task.get("id") or 0)
        if objective_id <= 0 or task_id <= 0:
            raise ValueError("Active initiative status is missing objective/task identifiers")

        tracking = active_task.get("execution_tracking") if isinstance(active_task.get("execution_tracking"), dict) else {}
        request_id = str(
            tracking.get("request_id")
            or active_task.get("request_id")
            or active_objective.get("request_id")
            or f"objective-{objective_id}-task-{task_id}"
        ).strip()
        objective = SimpleNamespace(
            id=objective_id,
            priority=str(active_objective.get("priority") or "high").strip() or "high",
            title=str(active_objective.get("display_title") or active_objective.get("title") or "").strip(),
        )
        task = SimpleNamespace(
            id=task_id,
            title=str(active_task.get("display_title") or active_task.get("title") or "").strip() or f"objective-{objective_id}-task-{task_id}",
            details=str(active_task.get("description") or active_task.get("display_title") or active_task.get("title") or "").strip(),
            acceptance_criteria=str(active_task.get("acceptance_criteria") or "").strip(),
            execution_scope=str(active_task.get("execution_scope") or "bounded_development").strip() or "bounded_development",
        )
        bridge_artifacts = _publish_codex_dispatch_bridge_artifacts(
            objective=objective,
            task=task,
            request_id=request_id,
            submission_status=str(active_task.get("dispatch_status") or active_task.get("state") or "queued").strip() or "queued",
            latest_result_summary=str(initiative.get("summary") or "Republished active objective request surface.").strip(),
            shared_root=self.state_dir,
        )
        return {
            "objective_id": objective_id,
            "task_id": task_id,
            "request_id": request_id,
            "bridge_artifacts": bridge_artifacts,
            "source": "initiative_status",
        }

    def _run_coordination_responder_once(self) -> dict[str, Any]:
        """Refresh the coordination ACK from the current request state."""
        root_dir = self.state_dir.parent.parent
        script = root_dir / "scripts" / "watch_mim_coordination_responder.sh"
        if not script.exists():
            raise ValueError(f"coordination responder not found: {script}")
        completed = subprocess.run(
            [str(script)],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(root_dir),
            env={
                **os.environ,
                "SHARED_DIR": str(self.state_dir),
                "LOG_DIR": str(root_dir / "runtime" / "logs"),
                "RUN_ONCE": "1",
            },
        )
        if completed.returncode != 0:
            raise ValueError((completed.stderr or completed.stdout or "coordination responder failed").strip())
        return {
            "action": "run_coordination_responder_once",
            "status": "completed",
            "stdout": (completed.stdout or "").strip(),
        }

    def _refresh_publication_boundary(self, *, request_path: str, trigger_path: str, request_id: str) -> dict[str, Any]:
        """Refresh the publication-boundary artifact for the current request surface."""
        root_dir = self.state_dir.parent.parent
        publisher = root_dir / "scripts" / "publish_tod_bridge_artifacts_remote.py"
        if not publisher.exists():
            raise ValueError(f"bridge publisher not found: {publisher}")
        completed = subprocess.run(
            [
                sys.executable,
                str(publisher),
                "--request-file",
                str(request_path),
                "--trigger-file",
                str(trigger_path),
                "--verify-task-id",
                str(request_id),
                "--caller",
                "core.self_optimizer_service:recover_bridge_coordination",
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(root_dir),
        )
        if completed.returncode != 0:
            raise ValueError((completed.stderr or completed.stdout or "publication boundary refresh failed").strip())
        payload = {}
        stdout = (completed.stdout or "").strip()
        if stdout.startswith("{"):
            try:
                payload = json.loads(stdout)
            except Exception:
                payload = {}
        return {
            "action": "refresh_publication_boundary",
            "status": "completed",
            "request_request_id": payload.get("request_request_id"),
            "trigger_request_id": payload.get("trigger_request_id"),
            "publication_mode": payload.get("publication_mode"),
            "stdout": stdout,
        }

    def _restart_bridge_watchers(self) -> dict[str, Any]:
        """Restart the core bridge watcher processes with the current shared root."""
        root_dir = self.state_dir.parent.parent
        scripts = [
            root_dir / "scripts" / "watch_shared_triggers.sh",
            root_dir / "scripts" / "watch_mim_coordination_responder.sh",
        ]
        managed_units = {
            "watch_shared_triggers.sh": "mim-watch-shared-triggers.service",
            "watch_mim_coordination_responder.sh": "mim-watch-mim-coordination-responder.service",
        }
        restarted: list[str] = []
        for script in scripts:
            if not script.exists():
                raise ValueError(f"watcher script not found: {script}")
            subprocess.run(["pkill", "-f", str(script)], capture_output=True, text=True, check=False)
            unit_name = managed_units.get(script.name)
            if unit_name and self._restart_user_watcher_unit(unit_name):
                restarted.append(unit_name)
                continue
            subprocess.Popen(
                [str(script)],
                cwd=str(root_dir),
                env={
                    **os.environ,
                    "SHARED_DIR": str(self.state_dir),
                    "LOG_DIR": str(root_dir / "runtime" / "logs"),
                },
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            restarted.append(script.name)
        return {
            "action": "restart_bridge_watchers",
            "status": "completed",
            "restarted": restarted,
        }

    def _restart_user_watcher_unit(self, unit_name: str) -> bool:
        """Restart a user-managed watcher unit when systemd owns the watcher lifecycle."""
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return False
        unit_present = subprocess.run(
            [systemctl, "--user", "cat", unit_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if unit_present.returncode != 0:
            return False
        restarted = subprocess.run(
            [systemctl, "--user", "restart", unit_name],
            capture_output=True,
            text=True,
            check=False,
        )
        if restarted.returncode != 0:
            raise ValueError(
                (restarted.stderr or restarted.stdout or f"failed to restart watcher unit {unit_name}").strip()
            )
        return True

    def _read_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _fallback_activation_path(self) -> Path:
        return self.state_dir / "MIM_TOD_FALLBACK_ACTIVATION.latest.json"

    def _publish_direct_execution_fallback_artifact(
        self,
        *,
        objective_id: str,
        task_id: str,
        request_id: str,
        execution_state: str,
        summary: str,
    ) -> dict[str, Any]:
        from core.tod_mim_contract import normalize_and_validate_file

        artifact_path = self._fallback_activation_path()
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "objective_id": objective_id,
            "task_id": task_id,
            "request_id": request_id,
            "correlation_id": request_id,
            "fallback_reason_code": "tod_silence_direct_execution_ready",
            "primary_transport_state": "blocked",
            "fallback_scope": task_id,
            "execution_state": execution_state,
            "decision_outcome": "mim_direct_execution_takeover",
            "summary": summary,
        }
        artifact_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        normalized, errors = normalize_and_validate_file(
            artifact_path,
            message_kind="fallback",
            service_name="core.self_optimizer_service",
            actor="MIM",
        )
        if errors:
            raise ValueError(f"fallback activation validation failed: {', '.join(errors)}")
        return normalized

    def _dispatch_active_task_to_codex_direct_execution(self) -> dict[str, Any]:
        return asyncio.run(self._dispatch_active_task_to_codex_direct_execution_async())

    async def _dispatch_active_task_to_codex_direct_execution_async(self) -> dict[str, Any]:
        from core.autonomy_driver_service import _dispatch_codex_task, build_initiative_status
        from core.db import SessionLocal
        from core.models import Objective, Task

        async with SessionLocal() as db:
            initiative = await build_initiative_status(db=db)
            active_objective = initiative.get("active_objective") if isinstance(initiative.get("active_objective"), dict) else {}
            active_task = initiative.get("active_task") if isinstance(initiative.get("active_task"), dict) else {}
            objective_id = int(active_objective.get("objective_id") or active_objective.get("id") or 0)
            task_id = int(active_task.get("task_id") or active_task.get("id") or 0)
            if objective_id <= 0 or task_id <= 0:
                raise ValueError("No active objective/task is available for direct execution takeover")

            objective = (await db.execute(select(Objective).where(Objective.id == objective_id))).scalar_one()
            task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one()
            if str(getattr(task, "assigned_to", "") or "").strip().lower() != "codex":
                raise ValueError(f"Active task {task.id} is not assigned to Codex and cannot use the direct execution fallback")

            submission = await _dispatch_codex_task(
                db,
                objective=objective,
                task=task,
                actor="mim",
                source="self_optimizer_direct_execution_fallback",
            )
            await db.commit()
            return {
                "objective_id": str(objective.id),
                "task_id": str(task.id),
                "request_id": str(submission.get("handoff_id") or submission.get("task_id") or "").strip(),
                "dispatch_status": str(submission.get("status") or "").strip(),
                "submission": submission,
            }

    def _action_recover_bridge_coordination(self) -> dict[str, Any]:
        """Republish the active request surface and restart bridge coordination watchers."""
        return asyncio.run(self._action_recover_bridge_coordination_async())

    async def _action_recover_bridge_coordination_async(self) -> dict[str, Any]:
        """Async variant of bridge coordination recovery."""
        from core.self_health_monitor import SelfHealthMonitor

        republish = await self._republish_active_task_request_surface_async()
        bridge_artifacts = republish.get("bridge_artifacts") if isinstance(republish.get("bridge_artifacts"), dict) else {}
        boundary_refresh = self._refresh_publication_boundary(
            request_path=str(bridge_artifacts.get("request_path") or self.state_dir / "MIM_TOD_TASK_REQUEST.latest.json"),
            trigger_path=str(bridge_artifacts.get("trigger_path") or self.state_dir / "MIM_TO_TOD_TRIGGER.latest.json"),
            request_id=str(republish.get("request_id") or "").strip(),
        )
        export_refresh = self._action_refresh_shared_export_artifacts()
        responder = self._run_coordination_responder_once()
        watcher_restart = self._restart_bridge_watchers()

        monitor = SelfHealthMonitor(state_dir=self.state_dir)
        remaining_codes = [
            item.code
            for item in monitor.get_runtime_diagnostics()
            if item.code in {
                "shared_export_stale_or_misaligned",
                "coordination_ack_stale_or_misaligned",
                "communication_authority_drift",
            }
        ]
        return {
            "action": "recover_bridge_coordination",
            "status": "completed" if not remaining_codes else "needs_followup",
            "republish": republish,
            "boundary_refresh": boundary_refresh,
            "export_refresh": export_refresh,
            "coordination_responder": responder,
            "watcher_restart": watcher_restart,
            "remaining_diagnostic_codes": remaining_codes,
        }

    def _action_fallback_to_codex_direct_execution(self) -> dict[str, Any]:
        """Claim bounded fallback authority and continue the active task through the local Codex handoff path."""
        return asyncio.run(self._action_fallback_to_codex_direct_execution_async())

    async def _action_fallback_to_codex_direct_execution_async(self) -> dict[str, Any]:
        """Async variant of direct-execution fallback takeover."""
        review = self._read_json(self.state_dir / "MIM_TASK_STATUS_REVIEW.latest.json")
        idle = review.get("idle") if isinstance(review.get("idle"), dict) else {}
        task = review.get("task") if isinstance(review.get("task"), dict) else {}
        next_action = self._read_json(self.state_dir / "MIM_TASK_STATUS_NEXT_ACTION.latest.json")
        selected_action = next_action.get("selected_action") if isinstance(next_action.get("selected_action"), dict) else {}
        active_task_id = str(task.get("active_task_id") or "").strip()
        if not bool(idle.get("direct_execution_ready") is True):
            raise ValueError("Direct execution takeover is not armed by the current task-status review")
        if str(selected_action.get("code") or "").strip() != "fallback_to_codex_direct_execution":
            raise ValueError("Current task-status review did not select direct execution takeover")
        if self._same_task_bridge_evidence_present(active_task_id=active_task_id):
            raise ValueError("TOD already has same-task bridge evidence; refusing direct execution takeover")

        existing = self._read_json(self._fallback_activation_path())
        existing_task_id = str(existing.get("task_id") or "").strip()
        existing_state = str(existing.get("execution_state") or "").strip().lower()
        if active_task_id and existing_task_id == active_task_id and existing_state in {"accepted", "running", "completed"}:
            return {
                "action": "fallback_to_codex_direct_execution",
                "status": "already_active",
                "fallback_activation": existing,
            }

        objective_id = str(task.get("objective_id") or "").strip()
        summary = str(selected_action.get("detail") or "MIM claimed direct execution fallback after TOD silence.").strip()
        activation = self._publish_direct_execution_fallback_artifact(
            objective_id=objective_id,
            task_id=active_task_id,
            request_id=active_task_id or objective_id or "direct-execution-fallback",
            execution_state="accepted",
            summary=summary,
        )
        dispatch = await self._dispatch_active_task_to_codex_direct_execution_async()
        dispatch_status = str(dispatch.get("dispatch_status") or "").strip().lower()
        activation = self._publish_direct_execution_fallback_artifact(
            objective_id=str(dispatch.get("objective_id") or objective_id or "").strip(),
            task_id=str(dispatch.get("task_id") or active_task_id or "").strip(),
            request_id=str(dispatch.get("request_id") or activation.get("request_id") or "").strip(),
            execution_state=("completed" if dispatch_status == "completed" else "running" if dispatch_status else "accepted"),
            summary=(
                f"MIM claimed bounded direct execution fallback for {str(dispatch.get('request_id') or active_task_id or 'the active task').strip()} after TOD silence."
            ),
        )
        return {
            "action": "fallback_to_codex_direct_execution",
            "status": "completed",
            "fallback_activation": activation,
            "dispatch": dispatch,
        }

    def _action_deduplicate_bridge_watchers(self) -> dict[str, Any]:
        """Terminate duplicate watcher processes while keeping one instance per script."""
        patterns = [
            "watch_tod_liveness.sh",
            "watch_shared_triggers.sh",
            "watch_mim_coordination_responder.sh",
        ]
        kept: dict[str, int] = {}
        removed: dict[str, list[int]] = {}
        privileged_coordination: dict[str, Any] | None = None
        for pattern in patterns:
            completed = subprocess.run(
                ["pgrep", "-af", pattern],
                capture_output=True,
                text=True,
                check=False,
            )
            processes: list[int] = []
            for row in (completed.stdout or "").splitlines():
                parts = row.strip().split(maxsplit=1)
                if not parts or not parts[0].isdigit():
                    continue
                pid = int(parts[0])
                if pid == os.getpid():
                    continue
                processes.append(pid)
            if not processes:
                continue
            processes.sort()
            if pattern == "watch_tod_liveness.sh" and len(processes) > 1:
                privileged_coordination = run_privileged_action(
                    "disable-system-tod-liveness-watcher"
                )
            kept[pattern] = processes[0]
            if len(processes) == 1:
                continue
            removed[pattern] = []
            for pid in processes[1:]:
                os.kill(pid, signal.SIGTERM)
                removed[pattern].append(pid)
        return {
            "action": "deduplicate_bridge_watchers",
            "status": "completed",
            "kept": kept,
            "removed": removed,
            "privileged_coordination": privileged_coordination,
        }

    def _action_inspect_runtime_devices_and_browser(self) -> dict[str, Any]:
        """Return a manual follow-up recommendation for browser/device runtime instability."""
        return {
            "action": "inspect_runtime_devices_and_browser",
            "status": "manual_check_recommended",
            "summary": "Inspect browser media permissions, selected devices, and local runtime health for repeated stale-lane recovery attempts.",
        }

    def _action_monitor_runtime_recovery_evidence(self) -> dict[str, Any]:
        """Return an informational recommendation for bounded runtime recovery evidence."""
        return {
            "action": "monitor_runtime_recovery_evidence",
            "status": "observation_recommended",
            "summary": "Recent runtime recovery evidence is bounded and successful; continue monitoring to detect regressions or repeated stale recurrences.",
        }

    def _audit(self, proposal: OptimizationProposal, event: str, data: dict[str, Any] | None = None) -> None:
        """Log an audit event."""
        if proposal.audit_trail is None:
            proposal.audit_trail = []
        proposal.audit_trail.append(
            {
                "timestamp": _utc_now_iso(),
                "event": event,
                "data": data or {},
            }
        )

    def _persist(self) -> None:
        """Persist proposals to disk."""
        proposals_file = self.state_dir / "mim_self_optimization_proposals.latest.json"
        proposals_file.parent.mkdir(parents=True, exist_ok=True)
        artifact = {
            "generated_at": _utc_now_iso(),
            "proposals": [
                {
                    **dataclasses.asdict(p),
                    "status": p.status.value,
                    "created_at": p.created_at,
                    "approved_at": p.approved_at,
                    "executed_at": p.executed_at,
                    "rolled_back_at": p.rolled_back_at,
                }
                for p in self.proposals.values()
            ],
        }
        proposals_file.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
