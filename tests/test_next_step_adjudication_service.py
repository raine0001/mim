import json
import tempfile
import unittest
from pathlib import Path

from core.next_step_adjudication_service import (
    build_interface_auto_approval_decision,
    build_mim_adjudication,
    build_next_step_consensus,
    load_local_posture,
    publish_next_step_artifacts,
)


class NextStepAdjudicationServiceTest(unittest.TestCase):
    def test_auto_approves_low_risk_direct_inquiry(self) -> None:
        decision = build_interface_auto_approval_decision(
            parsed_intent="next_tod_tasks_inquiry",
            content="Direct inquiry on next TOD tasks locally after the canonical-only validation pass.",
            metadata_json={
                "owner_workspace": "TOD",
                "action_type": "inquire",
                "risk": "low",
                "cross_system": True,
                "approval_required": False,
            },
        )
        self.assertTrue(decision.get("auto_approve"), decision)
        self.assertEqual(decision.get("decision"), "approved", decision)

    def test_blocks_auto_approval_for_live_arm_execution(self) -> None:
        decision = build_interface_auto_approval_decision(
            parsed_intent="next_tod_tasks_inquiry",
            content="Please directly execute live arm safe_home as the next TOD task.",
            metadata_json={
                "owner_workspace": "TOD",
                "action_type": "execute",
                "risk": "medium",
                "cross_system": True,
                "approval_required": True,
                "live_arm_execution": True,
            },
        )
        self.assertFalse(decision.get("auto_approve"), decision)

    def test_builds_mim_adjudication_and_consensus(self) -> None:
        next_steps = {
            "source_workspace": "MIM",
            "run_id": "mim_run_2026_04_01_01",
            "objective_id": "98A",
            "items": [
                {
                    "step_id": "step_001",
                    "description": "Run canonical-only validation pass",
                    "owner_workspace": "TOD",
                    "action_type": "validate",
                    "risk": "low",
                    "cross_system": True,
                    "approval_required": False,
                },
                {
                    "step_id": "step_002",
                    "description": "Refresh local MIM status surfaces",
                    "owner_workspace": "MIM",
                    "action_type": "refresh",
                    "risk": "low",
                    "cross_system": False,
                    "approval_required": False,
                },
            ],
        }
        posture = {
            "evaluated_at": "2026-04-01T20:00:00Z",
            "active_task_id": "objective-97-task-mim-arm-safe-home-207749",
            "objective_id": "97",
            "review_state": "completed",
            "review_reason": "task_result_current",
            "gate_pass": True,
            "promotion_ready": True,
            "system_alerts_active": False,
            "highest_severity": "none",
            "blocking_reason_codes": [],
            "arm_operator_approval_required": True,
            "tod_execution_allowed": True,
            "tod_execution_block_reason": "",
        }
        mim = build_mim_adjudication(next_steps, posture=posture)
        step1 = next(item for item in mim["items"] if item["step_id"] == "step_001")
        step2 = next(item for item in mim["items"] if item["step_id"] == "step_002")
        self.assertEqual(step1["posture"], "proposal_only", mim)
        self.assertTrue(step1["requires_tod_input"], mim)
        self.assertEqual(step2["posture"], "auto_execute_candidate", mim)

        tod = {
            "items": [
                {"step_id": "step_001", "tod_decision": "approve"},
            ]
        }
        consensus = build_next_step_consensus(next_steps, mim, tod_adjudication=tod)
        consensus_by_step = {item["step_id"]: item for item in consensus["items"]}
        self.assertEqual(consensus_by_step["step_001"]["consensus_action"], "proposal_only")
        self.assertEqual(consensus_by_step["step_002"]["consensus_action"], "auto_execute")

    def test_publish_next_step_artifacts(self) -> None:
        next_steps = {
            "source_workspace": "MIM",
            "run_id": "mim_run_publish",
            "objective_id": "98A",
            "items": [
                {
                    "step_id": "step_001",
                    "description": "Run canonical-only validation pass",
                    "owner_workspace": "TOD",
                    "action_type": "validate",
                    "risk": "low",
                    "cross_system": True,
                    "approval_required": False,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            shared = Path(tmpdir)
            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                '{"task":{"active_task_id":"objective-97-task"},"state":"completed","gate":{"pass":true,"promotion_ready":true},"blocking_reason_codes":[]}',
                encoding="utf-8",
            )
            (shared / "MIM_SYSTEM_ALERTS.latest.json").write_text(
                '{"active":false,"highest_severity":"none"}',
                encoding="utf-8",
            )
            (shared / "TOD_CATCHUP_GATE.latest.json").write_text(
                '{"gate_pass":true,"promotion_ready":true}',
                encoding="utf-8",
            )
            (shared / "mim_arm_control_readiness.latest.json").write_text(
                '{"operator_approval_required":true,"tod_execution_allowed":true}',
                encoding="utf-8",
            )
            result = publish_next_step_artifacts(next_steps_payload=next_steps, shared_root=shared)
            self.assertTrue(Path(result["next_steps_path"]).exists(), result)
            self.assertTrue(Path(result["mim_adjudication_path"]).exists(), result)
            self.assertTrue(Path(result["consensus_path"]).exists(), result)

    def test_load_local_posture_prefers_active_operator_incident_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            shared = Path(tmpdir)
            incidents = shared / "incidents"
            incidents.mkdir(parents=True, exist_ok=True)

            (shared / "MIM_TASK_STATUS_REVIEW.latest.json").write_text(
                '{"task":{"active_task_id":"objective-97-task-smoke","objective_id":"97"},"state":"completed","state_reason":"task_result_current","gate":{"pass":true,"promotion_ready":true},"blocking_reason_codes":[]}',
                encoding="utf-8",
            )
            incident_review = incidents / "objective-97-executor_memory_pressure.review.json"
            incident_review.write_text(
                '{"task":{"active_task_id":"objective-97-task-mim-arm-safe-home-1775231977","objective_id":"97"},"state":"failed","state_reason":"executor_failed","gate":{"pass":true,"promotion_ready":true},"blocking_reason_codes":["executor_failed","executor_memory_pressure"]}',
                encoding="utf-8",
            )
            (shared / "MIM_OPERATOR_INCIDENT.latest.json").write_text(
                json.dumps(
                    {
                        "active": True,
                        "precedence": "prefer_incident_over_latest",
                        "review_path": str(incident_review),
                    }
                ),
                encoding="utf-8",
            )
            (shared / "MIM_SYSTEM_ALERTS.latest.json").write_text(
                '{"active":false,"highest_severity":"none"}',
                encoding="utf-8",
            )
            (shared / "TOD_CATCHUP_GATE.latest.json").write_text(
                '{"gate_pass":true,"promotion_ready":true}',
                encoding="utf-8",
            )
            (shared / "mim_arm_control_readiness.latest.json").write_text(
                '{"operator_approval_required":false,"tod_execution_allowed":true}',
                encoding="utf-8",
            )

            posture = load_local_posture(shared)
            self.assertEqual(posture["review_state"], "failed")
            self.assertEqual(posture["review_reason"], "executor_failed")
            self.assertIn("executor_memory_pressure", posture["blocking_reason_codes"])


if __name__ == "__main__":
    unittest.main()