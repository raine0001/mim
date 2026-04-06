"""MIM self-optimization service: proposes and executes bounded self-improvements.

Bridges health monitor diagnostics with approval governance and runtime adjustment.
All changes are approval-gated, audited, and reversible.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import signal
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
            self.created_at = datetime.datetime.utcnow().isoformat() + "Z"
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
        proposal_id = f"opt-{recommendation_id}-{int(datetime.datetime.utcnow().timestamp()*1000)}"
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
        proposal.approved_at = datetime.datetime.utcnow().isoformat() + "Z"
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
            proposal.executed_at = datetime.datetime.utcnow().isoformat() + "Z"
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
            proposal.rolled_back_at = datetime.datetime.utcnow().isoformat() + "Z"
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
            "deduplicate_bridge_watchers": self._action_deduplicate_bridge_watchers,
            "inspect_runtime_devices_and_browser": self._action_inspect_runtime_devices_and_browser,
            "monitor_runtime_recovery_evidence": self._action_monitor_runtime_recovery_evidence,
        }
        if action_name not in actions:
            raise ValueError(f"Unknown optimization action: {action_name}")
        return actions[action_name]()

    def _action_gc(self) -> dict[str, Any]:
        """Trigger GC and return result summary."""
        import gc
        collected = gc.collect()
        return {"action": "gc", "objects_collected": collected, "timestamp": datetime.datetime.utcnow().isoformat() + "Z"}

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

    def _action_deduplicate_bridge_watchers(self) -> dict[str, Any]:
        """Terminate duplicate watcher processes while keeping one instance per script."""
        patterns = [
            "watch_tod_liveness.sh",
            "watch_shared_triggers.sh",
            "watch_mim_coordination_responder.sh",
        ]
        kept: dict[str, int] = {}
        removed: dict[str, list[int]] = {}
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
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
                "event": event,
                "data": data or {},
            }
        )

    def _persist(self) -> None:
        """Persist proposals to disk."""
        proposals_file = self.state_dir / "mim_self_optimization_proposals.latest.json"
        proposals_file.parent.mkdir(parents=True, exist_ok=True)
        artifact = {
            "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
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
