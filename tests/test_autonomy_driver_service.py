import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.autonomy_driver_service import (
    CONTINUATION_VALIDATION_OBJECTIVE_TITLE,
    SELF_CORRECTION_STALE_PREVENTION_OBJECTIVE_TITLE,
    _dispatch_codex_task,
    build_codex_handoff_payload,
    build_initiative_status,
    build_initiative_task_plan,
    classify_boundary_mode,
    continue_initiative,
    drive_initiative_from_intent,
    extract_explicit_program_id,
    extract_explicit_initiative_id,
)
from core.models import Objective


class _FakeExecuteResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def first(self):
        return self._items[0] if self._items else None


class _InitiativeDriverFakeDB:
    def __init__(self, objectives=None):
        self.objectives = list(objectives or [])
        self.added = []
        existing_ids = [int(getattr(item, "id", 0) or 0) for item in self.objectives]
        self._next_id = max(existing_ids or [0])

    async def execute(self, stmt):
        return _FakeExecuteResult(self.objectives)

    def add(self, obj):
        self._next_id += 1
        obj.id = self._next_id
        if isinstance(obj, Objective):
            self.objectives.insert(0, obj)
        self.added.append(obj)

    async def flush(self):
        return None


class AutonomyDriverServiceTests(unittest.TestCase):
    @staticmethod
    def _completed_tracking(request_id: str) -> dict:
        return {
            "execution_tracking": {
                "task_created": True,
                "task_dispatched": True,
                "execution_started": True,
                "execution_result": "completed",
                "request_id": request_id,
                "execution_trace": f"trace:{request_id}",
                "result_artifact": f"artifact:{request_id}",
            }
        }

    def test_classifies_security_changes_as_hard_boundary(self) -> None:
        boundary = classify_boundary_mode("Rotate the production API token and change access permissions.")

        self.assertEqual(boundary["boundary_mode"], "hard")
        self.assertEqual(boundary["reason"], "credential_or_secret_change")

    def test_standing_auto_approval_overrides_routine_production_boundary(self) -> None:
        boundary = classify_boundary_mode(
            "All natural next steps are automatically approved. Proceed with the production deployment validation step without human confirmation required."
        )

        self.assertEqual(boundary["boundary_mode"], "soft")
        self.assertEqual(boundary["reason"], "standing_auto_approval_override:public_or_production_change")

    def test_standing_auto_approval_does_not_override_secret_rotation_boundary(self) -> None:
        boundary = classify_boundary_mode(
            "All next steps are approved. Rotate the production API token and change access permissions."
        )

        self.assertEqual(boundary["boundary_mode"], "hard")
        self.assertEqual(boundary["reason"], "credential_or_secret_change")

    def test_builds_training_plan_with_local_start_task(self) -> None:
        plan = build_initiative_task_plan(
            user_intent="Start training and keep the natural-language slice moving.",
            actor="test",
            source="unit-test",
            managed_scope="workspace",
            expected_outputs=[],
            verification_commands=[],
        )

        self.assertEqual(plan["objective_title"], "Drive natural-language self-evolution training")
        self.assertEqual(plan["boundary_mode"], "soft")
        self.assertEqual(plan["tasks"][0].assigned_to, "mim")
        self.assertEqual(plan["tasks"][0].metadata_json["automation_kind"], "self_evolution_reset")
        self.assertEqual(plan["tasks"][1].assigned_to, "codex")

    def test_builds_explicit_program_project_plan_from_structured_tasks(self) -> None:
        plan = build_initiative_task_plan(
            user_intent=(
                "PROGRAM_ID: MIM-12-AUTONOMOUS-EVOLUTION\n"
                "Project_2_ID: MIM-DAY-02-INITIATIVE-ISOLATION\n"
                "INITIATIVE_ID: MIM-DAY-02-INITIATIVE-ISOLATION\n"
                "OBJECTIVE:\n"
                "Prevent new initiatives from being overwritten, contaminated, or auto-rerouted into previously authorized initiatives.\n"
                "GOAL:\n"
                "Every incoming initiative stays bound to its own request, objective, task, and result lineage.\n"
                "TASKS:\n"
                "1. Trace request_id -> initiative_id -> objective_id -> task_id -> result path.\n"
                "2. Patch precedence so explicit incoming INITIATIVE_ID wins over prior cached initiative continuation unless explicitly resumed.\n"
                "SUCCESS CRITERIA:\n"
                "- explicit INITIATIVE_ID always wins over stale active initiative continuation\n"
            ),
            actor="test",
            source="unit-test",
            managed_scope="workspace",
            expected_outputs=[],
            verification_commands=[],
        )

        self.assertEqual(plan["objective_title"], "Prevent new initiatives from being overwritten, contaminated, or auto-rerouted into previously authorized initiatives.")
        self.assertEqual(plan["boundary_reason"], "program_project_execution")
        self.assertEqual(len(plan["tasks"]), 2)
        self.assertEqual(plan["tasks"][0].metadata_json["program_project_id"], "MIM-DAY-02-INITIATIVE-ISOLATION")
        self.assertEqual(plan["tasks"][1].execution_scope, "bounded_development")

    def test_collapses_project_task_fragments_into_single_bounded_tasks(self) -> None:
        plan = build_initiative_task_plan(
            user_intent=(
                "PROGRAM_ID: MIM-12-AUTONOMOUS-EVOLUTION\n"
                "Project_1_ID: MIM-DAY-01-EXECUTION-BOUND-COMPLETION\n"
                "INITIATIVE_ID: MIM-DAY-01-EXECUTION-BOUND-COMPLETION\n"
                "OBJECTIVE:\n"
                "Enforce that objectives cannot complete from planning text, broker artifacts, or task creation alone.\n"
                "GOAL:\n"
                "Completion must require real execution evidence.\n"
                "TASKS:\n"
                "1. Inspect current completion conditions across gateway, handoff intake, autonomy/goal lifecycle, and status surfaces.\n"
                "2. Identify every path where planning-only, broker-prep, model text, or non-executed artifacts can mark an objective complete.\n"
                "3. Patch completion logic so completion requires:\n"
                "4. dispatched task or executed broker path\n"
                "5. execution attempt or executed result\n"
                "6. result artifact or execution evidence\n"
                "7. Ensure planning-only objectives remain active/planning/in_progress.\n"
                "8. Ensure failed execution does not mark complete.\n"
                "9. Update regressions and add targeted tests for:\n"
                "10. planning-only objective\n"
                "11. executed objective\n"
                "12. failed objective\n"
                "13. Run validation and summarize exact lifecycle transitions.\n"
                "SUCCESS CRITERIA:\n"
                "- planning-only objective remains active/planning\n"
                "- executed objective completes only after execution evidence exists\n"
                "- failed objective remains failed/blocked, not complete\n"
            ),
            actor="test",
            source="unit-test",
            managed_scope="workspace",
            expected_outputs=[],
            verification_commands=[],
        )

        self.assertEqual(len(plan["tasks"]), 7)
        self.assertEqual(
            plan["tasks"][2].details,
            "Patch completion logic so completion requires: dispatched task or executed broker path; execution attempt or executed result; result artifact or execution evidence",
        )
        self.assertEqual(
            plan["tasks"][5].details,
            "Update regressions and add targeted tests for: planning-only objective; executed objective; failed objective",
        )
        self.assertEqual(plan["tasks"][6].execution_scope, "bounded_validation")

    def test_builds_continuation_validation_plan_from_natural_language_prompt(self) -> None:
        plan = build_initiative_task_plan(
            user_intent=(
                "INITIATIVE_ID: MIM-CONTINUOUS-EXECUTION-VALIDATION\n"
                "This is a controlled continuation test. Verify continuation after task completion, recovery, and readiness transition. "
                "No human confirmation required. Validate auto-resume and 5+ tasks executed."
            ),
            actor="test",
            source="unit-test",
            managed_scope="workspace",
            expected_outputs=[],
            verification_commands=[],
        )

        self.assertEqual(plan["objective_title"], CONTINUATION_VALIDATION_OBJECTIVE_TITLE)
        self.assertEqual(plan["boundary_mode"], "soft")
        self.assertEqual(plan["boundary_reason"], "continuation_validation")
        self.assertEqual(len(plan["tasks"]), 8)
        self.assertTrue(all(task.assigned_to == "mim" for task in plan["tasks"]))
        self.assertTrue(
            all(task.metadata_json["automation_kind"] == "continuation_validation_step" for task in plan["tasks"])
        )

    def test_builds_self_correction_stale_prevention_plan_from_prompt(self) -> None:
        plan = build_initiative_task_plan(
            user_intent=(
                "INITIATIVE_ID: MIM-SELF-CORRECTION-AND-STALE-PREVENTION\n"
                "Teach MIM to detect repetitive non-progressing action patterns, self-correct branch selection, "
                "and generate code-level remediation tasks that reduce stale-state loops.\n"
                "TRAINING TARGETS: repetition detection, progress classification, self-correction, code-oriented remediation."
            ),
            actor="test",
            source="unit-test",
            managed_scope="workspace",
            expected_outputs=[],
            verification_commands=[],
        )

        self.assertEqual(plan["objective_title"], SELF_CORRECTION_STALE_PREVENTION_OBJECTIVE_TITLE)
        self.assertEqual(plan["boundary_mode"], "soft")
        self.assertEqual(plan["boundary_reason"], "stale_prevention_training")
        self.assertEqual(len(plan["tasks"]), 5)
        self.assertTrue(all(task.assigned_to == "mim" for task in plan["tasks"]))
        self.assertTrue(
            all(task.metadata_json["automation_kind"] == "stale_prevention_pass" for task in plan["tasks"])
        )

    def test_builds_codex_handoff_payload_with_dispatch_contract(self) -> None:
        objective = SimpleNamespace(
            id=44,
            constraints_json=["boundary_mode=soft"],
        )
        task = SimpleNamespace(
            id=91,
            title="Implement bounded work for routing fix",
            details="Implement the routing fix and preserve evaluator wording.",
            acceptance_criteria="Routing fix is complete.",
            execution_scope="bounded_development",
            expected_outputs_json=["Routing fix applied"],
            verification_commands_json=["pytest tests/test_objective_lifecycle.py"],
            start_now=True,
            human_prompt_required=False,
        )

        payload = build_codex_handoff_payload(objective=objective, task=task)

        self.assertEqual(payload["dispatch_contract"]["objective_id"], 44)
        self.assertEqual(payload["dispatch_contract"]["task_id"], 91)
        self.assertTrue(payload["dispatch_contract"]["start_now"])
        self.assertFalse(payload["dispatch_contract"]["human_prompt_required"])
        self.assertIn("Implement and verify", payload["requested_outcome"])

    def test_extract_explicit_initiative_id_stops_at_next_field_on_single_line(self) -> None:
        initiative_id = extract_explicit_initiative_id(
            "INITIATIVE_ID: MIM-EXECUTION-COMPLETION-CHECK OBJECTIVE: Dispatch one bounded executable task GOAL: Verify execution evidence"
        )

        self.assertEqual(initiative_id, "MIM-EXECUTION-COMPLETION-CHECK")

    def test_extract_explicit_initiative_id_reads_multiline_identifier_only(self) -> None:
        initiative_id = extract_explicit_initiative_id(
            "INITIATIVE_ID: PLAN-ONLY-ISOLATION-001\nOBJECTIVE: Keep this isolated\nGOAL: Stay planning-only"
        )

        self.assertEqual(initiative_id, "PLAN-ONLY-ISOLATION-001")

    def test_extract_explicit_program_id_reads_identifier_only(self) -> None:
        program_id = extract_explicit_program_id(
            "PROGRAM_ID: MIM-12-AUTONOMOUS-EVOLUTION\nOBJECTIVE: Run the 12-day program"
        )

        self.assertEqual(program_id, "MIM-12-AUTONOMOUS-EVOLUTION")


class InitiativeIdentityRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_initiative_id_creates_distinct_objective_even_when_title_collides(self) -> None:
        existing_objective = Objective(
            id=41,
            title="Carry forward the active corrective objective",
            description="Older active initiative",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="in_progress",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={"initiative_id": "OLD-CORRECTIVE-ID", "managed_scope": "workspace"},
        )
        fake_db = _InitiativeDriverFakeDB(objectives=[existing_objective])

        with patch(
            "core.autonomy_driver_service._tasks_for_objective",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.autonomy_driver_service.recompute_objective_state",
            new=AsyncMock(return_value=None),
        ), patch(
            "core.autonomy_driver_service.build_initiative_status",
            new=AsyncMock(return_value={}),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await drive_initiative_from_intent(
                fake_db,
                actor="mim",
                source="unit-test",
                user_intent=(
                    "INITIATIVE_ID: PLAN-ONLY-ISOLATION-001\n"
                    "OBJECTIVE: Carry forward the active corrective objective\n"
                    "MODE: planning-only"
                ),
                objective_title="Carry forward the active corrective objective",
                priority="high",
                managed_scope="workspace",
                expected_outputs=[],
                verification_commands=[],
                continue_chain=False,
                max_auto_steps=1,
                metadata_json={"request_id": "req-plan-001", "initiative_id": "PLAN-ONLY-ISOLATION-001"},
            )

        objective_payload = result["objective"]
        self.assertNotEqual(int(objective_payload["objective_id"]), 41)
        self.assertEqual(str(objective_payload.get("initiative_id") or "").strip(), "PLAN-ONLY-ISOLATION-001")
        self.assertEqual(str(objective_payload.get("request_id") or "").strip(), "req-plan-001")
        self.assertEqual(str(existing_objective.metadata_json.get("initiative_id") or "").strip(), "OLD-CORRECTIVE-ID")

    async def test_metadata_fallback_initiative_id_is_sanitized_before_persistence(self) -> None:
        fake_db = _InitiativeDriverFakeDB(objectives=[])

        with patch(
            "core.autonomy_driver_service._tasks_for_objective",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.autonomy_driver_service.recompute_objective_state",
            new=AsyncMock(return_value=None),
        ), patch(
            "core.autonomy_driver_service.build_initiative_status",
            new=AsyncMock(return_value={}),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await drive_initiative_from_intent(
                fake_db,
                actor="mim",
                source="unit-test",
                user_intent="Create a bounded implementation plan.",
                objective_title="Create a bounded implementation plan.",
                priority="high",
                managed_scope="workspace",
                expected_outputs=[],
                verification_commands=[],
                continue_chain=False,
                max_auto_steps=1,
                metadata_json={
                    "request_id": "req-metadata-initiative",
                    "initiative_id": "MIM-EXECUTION-COMPLETION-CHECK OBJECTIVE: polluted tail GOAL: still polluted",
                },
            )

        objective_payload = result["objective"]
        self.assertEqual(
            str(objective_payload.get("initiative_id") or "").strip(),
            "MIM-EXECUTION-COMPLETION-CHECK",
        )
        self.assertEqual(
            str((objective_payload.get("metadata_json") or {}).get("initiative_id") or "").strip(),
            "MIM-EXECUTION-COMPLETION-CHECK",
        )
        self.assertTrue(
            all(
                str((task.get("metadata_json") or {}).get("initiative_id") or "").strip()
                == "MIM-EXECUTION-COMPLETION-CHECK"
                for task in result["tasks"]
            )
        )

    async def test_explicit_resume_existing_reuses_matching_inflight_initiative(self) -> None:
        existing_objective = Objective(
            id=88,
            title="Planning-only initiative",
            description="Existing inflight initiative.",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="in_progress",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={
                "initiative_id": "PLAN-ONLY-ISOLATION-001",
                "managed_scope": "workspace",
                "latest_request_id": "req-old-001",
            },
        )
        existing_task = SimpleNamespace(
            id=301,
            objective_id=88,
            title="Continue the existing initiative",
            details="queued",
            dependencies=[],
            acceptance_criteria="do work",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={"initiative_id": "PLAN-ONLY-ISOLATION-001", "request_id": "req-old-001"},
            created_at="2026-04-20T00:00:00Z",
        )
        fake_db = _InitiativeDriverFakeDB(objectives=[existing_objective])

        with patch(
            "core.autonomy_driver_service._tasks_for_objective",
            new=AsyncMock(return_value=[existing_task]),
        ), patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[existing_task]),
        ), patch(
            "core.autonomy_driver_service.recompute_objective_state",
            new=AsyncMock(return_value=None),
        ), patch(
            "core.autonomy_driver_service.build_initiative_status",
            new=AsyncMock(return_value={"summary": "Initiative resumed."}),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await drive_initiative_from_intent(
                fake_db,
                actor="mim",
                source="unit-test",
                user_intent=(
                    "INITIATIVE_ID: PLAN-ONLY-ISOLATION-001\n"
                    "OBJECTIVE: Resume the isolated planning initiative\n"
                    "RESUME_EXISTING: true"
                ),
                objective_title="Planning-only initiative",
                priority="high",
                managed_scope="workspace",
                expected_outputs=[],
                verification_commands=[],
                continue_chain=False,
                max_auto_steps=1,
                metadata_json={
                    "request_id": "req-resume-001",
                    "initiative_id": "PLAN-ONLY-ISOLATION-001",
                    "resume_existing": True,
                },
            )

        objective_payload = result["objective"]
        self.assertEqual(int(objective_payload["objective_id"]), 88)
        self.assertEqual(str(objective_payload.get("request_id") or "").strip(), "req-resume-001")
        self.assertEqual(
            str((objective_payload.get("metadata_json") or {}).get("latest_request_id") or "").strip(),
            "req-resume-001",
        )
        self.assertTrue(
            str(
                ((objective_payload.get("metadata_json") or {}).get("execution_tracking") or {}).get(
                    "activity_started_at"
                )
                or ""
            ).strip()
        )
        self.assertTrue(
            str(
                (((result["tasks"][0].get("metadata_json") or {}).get("execution_tracking") or {}).get(
                    "activity_started_at"
                )
                or ""
            ).strip()
        )
        )
        self.assertEqual(len(result["tasks"]), 1)

    async def test_program_registration_is_persisted_on_objective_and_tasks(self) -> None:
        fake_db = _InitiativeDriverFakeDB(objectives=[])

        with patch(
            "core.autonomy_driver_service._tasks_for_objective",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.autonomy_driver_service.recompute_objective_state",
            new=AsyncMock(return_value=None),
        ), patch(
            "core.autonomy_driver_service.build_initiative_status",
            new=AsyncMock(return_value={}),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await drive_initiative_from_intent(
                fake_db,
                actor="mim",
                source="unit-test",
                user_intent=(
                    "PROGRAM_ID: MIM-12-AUTONOMOUS-EVOLUTION\n"
                    "Project_1_ID: MIM-DAY-01-EXECUTION-BOUND-COMPLETION\n"
                    "OBJECTIVE: Enforce completion only after execution evidence\n"
                    "INITIATIVE_ID: MIM-DAY-01-EXECUTION-BOUND-COMPLETION\n"
                    "Create the bounded implementation plan and execute it."
                ),
                objective_title="",
                priority="high",
                managed_scope="workspace",
                expected_outputs=[],
                verification_commands=[],
                continue_chain=False,
                max_auto_steps=1,
                metadata_json={"request_id": "req-program-001"},
            )

        objective_payload = result["objective"]
        self.assertEqual(
            str((objective_payload.get("metadata_json") or {}).get("program_id") or ""),
            "MIM-12-AUTONOMOUS-EVOLUTION",
        )
        self.assertTrue(
            isinstance((objective_payload.get("metadata_json") or {}).get("program_registry"), dict)
        )
        self.assertTrue(
            all(
                str((task.get("metadata_json") or {}).get("program_id") or "")
                == "MIM-12-AUTONOMOUS-EVOLUTION"
                for task in result["tasks"]
            )
        )

    async def test_full_program_payload_defaults_initiative_id_to_first_project(self) -> None:
        stale_objective = Objective(
            id=215,
            title="Drive natural-language self-evolution training",
            description="Older stale initiative bound to the program id.",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="in_progress",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="hard",
            metadata_json={"initiative_id": "MIM-12-AUTONOMOUS-EVOLUTION", "managed_scope": "workspace"},
        )
        fake_db = _InitiativeDriverFakeDB(objectives=[stale_objective])

        with patch(
            "core.autonomy_driver_service._tasks_for_objective",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.autonomy_driver_service.recompute_objective_state",
            new=AsyncMock(return_value=None),
        ), patch(
            "core.autonomy_driver_service.build_initiative_status",
            new=AsyncMock(return_value={}),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await drive_initiative_from_intent(
                fake_db,
                actor="mim",
                source="unit-test",
                user_intent=(
                    "PROGRAM_ID: MIM-12-AUTONOMOUS-EVOLUTION\n"
                    "Project_1_ID: MIM-DAY-01-EXECUTION-BOUND-COMPLETION\n\n"
                    "OBJECTIVE:\n"
                    "Enforce that objectives cannot complete from planning text, broker artifacts, or task creation alone.\n\n"
                    "GOAL:\n"
                    "Completion must require real execution evidence.\n\n"
                    "TASKS:\n"
                    "1. Inspect current completion conditions across gateway, handoff intake, autonomy/goal lifecycle, and status surfaces.\n"
                    "SUCCESS CRITERIA:\n"
                    "- planning-only objective remains active/planning\n"
                ),
                objective_title="",
                priority="high",
                managed_scope="workspace",
                expected_outputs=[],
                verification_commands=[],
                continue_chain=False,
                max_auto_steps=1,
                metadata_json={
                    "request_id": "req-program-kickoff-001",
                    "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                },
            )

        objective_payload = result["objective"]
        self.assertNotEqual(int(objective_payload["objective_id"]), 215)
        self.assertEqual(
            str(objective_payload.get("initiative_id") or "").strip(),
            "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
        )
        self.assertEqual(
            str((objective_payload.get("metadata_json") or {}).get("initiative_id") or "").strip(),
            "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
        )


class CodexDispatchEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_continue_initiative_recovers_false_blocked_codex_task(self) -> None:
        objective = SimpleNamespace(
            id=16,
            title="Execution recovery objective",
            priority="high",
            state="blocked",
            owner="mim",
            constraints_json=[],
            metadata_json={},
            created_at="2026-04-16T00:00:00Z",
        )
        task = SimpleNamespace(
            id=81,
            objective_id=16,
            title="Dispatch executable task",
            details="dispatch",
            dependencies=[],
            acceptance_criteria="result artifact exists",
            state="blocked",
            assigned_to="codex",
            readiness="blocked",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="blocked",
            dispatch_artifact_json={
                "latest_result": {
                    "broker_preparation": {
                        "broker_response": {"status": "not_configured", "reason": "local_broker_client_not_configured"},
                        "automatic_live_response": {"status": "completed"},
                    }
                }
            },
            metadata_json={"execution_tracking": {"task_created": True, "task_dispatched": True}},
            created_at="2026-04-16T00:00:00Z",
        )
        execute_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [objective])
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

        async def _tasks_for_objective(_db, objective_id):
            return [task] if objective_id == 16 else []

        with patch(
            "core.autonomy_driver_service._tasks_for_objective",
            new=AsyncMock(side_effect=_tasks_for_objective),
        ), patch(
            "core.autonomy_driver_service.submit_handoff_payload",
            new=AsyncMock(
                return_value={
                    "handoff_id": "handoff-exec-81",
                    "status": "queued",
                    "task_path": "/tmp/handoff-exec-81.task.json",
                    "status_path": "/tmp/handoff-exec-81.status.json",
                    "latest_result_summary": "Queued and acknowledged.",
                }
            ),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ), patch(
            "core.autonomy_driver_service.recompute_objective_state",
            new=AsyncMock(return_value="in_progress"),
        ), patch(
            "core.autonomy_driver_service.build_initiative_status",
            new=AsyncMock(return_value={"status": "active"}),
        ):
            result = await continue_initiative(
                fake_db,
                objective_id=16,
                actor="mim",
                source="unit-test",
                max_auto_steps=1,
            )

        self.assertEqual(task.state, "in_progress")
        self.assertEqual(task.dispatch_status, "queued")
        self.assertEqual(len(result["dispatched"]), 1)

    async def test_continue_initiative_recovers_failed_codex_task_from_result_artifact(self) -> None:
        objective = SimpleNamespace(
            id=16,
            title="Execution recovery objective",
            priority="high",
            state="in_progress",
            owner="mim",
            constraints_json=[],
            metadata_json={},
            created_at="2026-04-16T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            result_artifact = Path(tmpdir) / "handoff-result.json"
            result_artifact.write_text(
                json.dumps(
                    {
                        "request_id": "mim-day-02-live-resume-refresh-20260502",
                        "task_id": "objective-2900-task-7117",
                        "status": "failed",
                        "result_status": "failed",
                        "terminal": True,
                        "result_reason_code": "invalid_packet_shape",
                    }
                ),
                encoding="utf-8",
            )
            task = SimpleNamespace(
                id=81,
                objective_id=16,
                title="Dispatch executable task",
                details="dispatch",
                dependencies=[],
                acceptance_criteria="result artifact exists",
                state="in_progress",
                assigned_to="codex",
                readiness="ready",
                boundary_mode="soft",
                start_now=True,
                human_prompt_required=False,
                execution_scope="bounded_development",
                expected_outputs_json=[],
                verification_commands_json=[],
                dispatch_status="queued",
                dispatch_artifact_json={
                    "handoff_id": "mim-day-02-live-resume-refresh-20260502",
                    "task_id": "objective-2900-task-7117",
                    "latest_result_artifact": str(result_artifact),
                    "latest_result_summary": "validator_failed",
                },
                metadata_json={
                    "execution_tracking": {
                        "task_created": True,
                        "task_dispatched": True,
                        "execution_started": True,
                        "request_id": "mim-day-02-live-resume-refresh-20260502",
                    }
                },
                created_at="2026-04-16T00:00:00Z",
            )
            execute_result = SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [objective], first=lambda: objective)
            )
            fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

            async def _tasks_for_objective(_db, objective_id):
                return [task] if objective_id == 16 else []

            with patch(
                "core.autonomy_driver_service._tasks_for_objective",
                new=AsyncMock(side_effect=_tasks_for_objective),
            ), patch(
                "core.autonomy_driver_service._select_next_ready_task",
                new=AsyncMock(return_value=(None, None)),
            ), patch(
                "core.autonomy_driver_service.refresh_task_readinesses",
                new=AsyncMock(return_value=None),
            ), patch(
                "core.autonomy_driver_service.recompute_objective_state",
                new=AsyncMock(return_value="blocked"),
            ), patch(
                "core.autonomy_driver_service.write_journal",
                new=AsyncMock(return_value=None),
            ), patch(
                "core.autonomy_driver_service.build_initiative_status",
                new=AsyncMock(return_value={"status": "active"}),
            ):
                result = await continue_initiative(
                    fake_db,
                    objective_id=16,
                    actor="mim",
                    source="unit-test",
                    max_auto_steps=1,
                )

        self.assertEqual(task.state, "failed")
        self.assertEqual(task.dispatch_status, "failed")
        tracking = task.metadata_json.get("execution_tracking", {})
        self.assertEqual(str(tracking.get("result_artifact") or ""), str(result_artifact))
        self.assertEqual(result["dispatched"], [])

    async def test_completed_handoff_without_result_artifact_stays_in_progress(self) -> None:
        fake_db = SimpleNamespace()
        objective = SimpleNamespace(
            id=12,
            title="Execution completion check",
            constraints_json=[],
        )
        task = SimpleNamespace(
            id=77,
            objective_id=12,
            title="Dispatch executable task",
            details="dispatch",
            dependencies=[],
            acceptance_criteria="result artifact exists",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={"execution_tracking": {"task_created": True}},
            created_at="2026-04-16T00:00:00Z",
        )

        submission = {
            "handoff_id": "handoff-exec-77",
            "status": "completed",
            "task_path": "/tmp/handoff-exec-77.task.json",
            "status_path": "/tmp/handoff-exec-77.status.json",
            "latest_status_path": "/tmp/HANDOFF_STATUS.latest.json",
            "latest_result_summary": "Queued and acknowledged.",
        }

        with patch(
            "core.autonomy_driver_service.submit_handoff_payload",
            new=AsyncMock(return_value=submission),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await _dispatch_codex_task(
                fake_db,
                objective=objective,
                task=task,
                actor="mim",
                source="unit-test",
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(task.dispatch_status, "completed")
        self.assertEqual(task.state, "in_progress")
        tracking = task.metadata_json.get("execution_tracking", {})
        self.assertFalse(bool(tracking.get("result_artifact")))

    async def test_continue_initiative_auto_advances_to_next_registered_project(self) -> None:
        objective = SimpleNamespace(
            id=88,
            title="Execution-bound completion",
            description="Project 1 complete",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="completed",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={
                "actor": "mim",
                "source": "unit-test",
                "managed_scope": "workspace",
                "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                "initiative_id": "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
                "program_registry": {
                    "active_program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                    "programs": [
                        {
                            "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                            "projects": [
                                {
                                    "ordinal": 1,
                                    "project_id": "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
                                    "objective": "Enforce completion only after execution evidence",
                                    "tasks": ["Validate execution evidence"],
                                    "success_criteria": ["Execution evidence exists"],
                                },
                                {
                                    "ordinal": 2,
                                    "project_id": "MIM-DAY-02-INITIATIVE-ISOLATION",
                                    "objective": "Prevent initiative contamination",
                                    "tasks": ["Trace lineage", "Patch precedence"],
                                    "success_criteria": ["Explicit initiative id wins"],
                                },
                            ],
                        }
                    ],
                },
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": True,
                    "execution_started": True,
                    "execution_result": "completed",
                    "execution_state": "completed",
                },
            },
            created_at="2026-04-20T00:00:00Z",
        )
        completed_task = SimpleNamespace(
            id=901,
            objective_id=88,
            title="Validate execution evidence",
            details="done",
            dependencies=[],
            acceptance_criteria="done",
            state="completed",
            assigned_to="codex",
            readiness="completed",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_validation",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="completed",
            dispatch_artifact_json={},
            metadata_json={
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": True,
                    "execution_started": True,
                    "execution_result": "completed",
                    "request_id": "req-901",
                    "execution_trace": "trace:req-901",
                    "result_artifact": "artifact:req-901",
                }
            },
            created_at="2026-04-20T00:00:01Z",
        )
        execute_result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [objective], first=lambda: objective))
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

        with patch(
            "core.autonomy_driver_service._recover_retryable_blocked_codex_tasks",
            new=AsyncMock(return_value=[]),
        ), patch(
            "core.autonomy_driver_service._select_next_ready_task",
            new=AsyncMock(return_value=(None, None)),
        ), patch(
            "core.autonomy_driver_service._tasks_for_objective",
            new=AsyncMock(return_value=[completed_task]),
        ), patch(
            "core.autonomy_driver_service._task_result_task_ids",
            new=AsyncMock(return_value={901}),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ), patch(
            "core.autonomy_driver_service.drive_initiative_from_intent",
            new=AsyncMock(return_value={"objective": {"objective_id": 89, "initiative_id": "MIM-DAY-02-INITIATIVE-ISOLATION"}, "continuation": {"status": {"status": "working"}}}),
        ), patch(
            "core.autonomy_driver_service.build_initiative_status",
            new=AsyncMock(return_value={"status": "idle", "active_objective": {"objective_id": 89, "initiative_id": "MIM-DAY-02-INITIATIVE-ISOLATION"}}),
        ) as build_status_mock:
            result = await continue_initiative(
                fake_db,
                objective_id=88,
                actor="mim",
                source="unit-test",
                max_auto_steps=2,
            )

        self.assertEqual(result["auto_advanced_to"]["project_id"], "MIM-DAY-02-INITIATIVE-ISOLATION")
        self.assertEqual(result["completed_project_summary"]["project_id"], "MIM-DAY-01-EXECUTION-BOUND-COMPLETION")
        self.assertEqual(build_status_mock.await_args.kwargs.get("objective_id"), 89)
        self.assertEqual(
            str(result["status"].get("active_objective", {}).get("initiative_id") or "").strip(),
            "MIM-DAY-02-INITIATIVE-ISOLATION",
        )

    async def test_broker_preparation_artifact_does_not_count_as_completion_evidence(self) -> None:
        fake_db = SimpleNamespace()
        objective = SimpleNamespace(
            id=13,
            title="Execution completion check",
            constraints_json=[],
        )
        task = SimpleNamespace(
            id=78,
            objective_id=13,
            title="Dispatch executable task",
            details="dispatch",
            dependencies=[],
            acceptance_criteria="result artifact exists",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={"execution_tracking": {"task_created": True}},
            created_at="2026-04-16T00:00:00Z",
        )

        submission = {
            "handoff_id": "handoff-exec-78",
            "status": "completed",
            "task_path": "/tmp/handoff-exec-78.task.json",
            "status_path": "/tmp/handoff-exec-78.status.json",
            "latest_result_summary": "Broker preparation completed.",
            "latest_result": {
                "broker_preparation": {
                    "automatic_live_response": {
                        "status": "completed",
                        "result_artifact": "/tmp/handoff-exec-78.broker-result.json",
                    },
                    "automatic_live_interpretation": {
                        "status": "completed",
                        "classification": "model_response_text",
                    },
                }
            },
        }

        with patch(
            "core.autonomy_driver_service.submit_handoff_payload",
            new=AsyncMock(return_value=submission),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await _dispatch_codex_task(
                fake_db,
                objective=objective,
                task=task,
                actor="mim",
                source="unit-test",
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(task.dispatch_status, "completed")
        self.assertEqual(task.state, "in_progress")
        tracking = task.metadata_json.get("execution_tracking", {})
        self.assertEqual(str(tracking.get("result_artifact") or ""), "")

    async def test_bounded_analysis_model_response_counts_as_completion_evidence(self) -> None:
        fake_db = SimpleNamespace()
        objective = SimpleNamespace(
            id=130,
            title="Execution completion analysis",
            constraints_json=[],
        )
        task = SimpleNamespace(
            id=780,
            objective_id=130,
            title="Inspect lifecycle completion conditions",
            details="inspect lifecycle state",
            dependencies=[],
            acceptance_criteria="analysis result artifact exists",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_analysis",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={"execution_tracking": {"task_created": True}},
            created_at="2026-04-20T00:00:00Z",
        )

        submission = {
            "handoff_id": "handoff-analysis-780",
            "status": "completed",
            "task_path": "/tmp/handoff-analysis-780.task.json",
            "status_path": "/tmp/handoff-analysis-780.status.json",
            "latest_result_summary": "Lifecycle inspection completed.",
            "latest_result": {
                "broker_preparation": {
                    "automatic_live_response": {
                        "status": "completed",
                        "result_artifact": "/tmp/handoff-analysis-780.broker-result.json",
                    },
                    "automatic_live_interpretation": {
                        "status": "completed",
                        "classification": "model_response_text",
                    },
                }
            },
        }

        with patch(
            "core.autonomy_driver_service.submit_handoff_payload",
            new=AsyncMock(return_value=submission),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await _dispatch_codex_task(
                fake_db,
                objective=objective,
                task=task,
                actor="mim",
                source="unit-test",
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(task.dispatch_status, "completed")
        self.assertEqual(task.state, "completed")
        tracking = task.metadata_json.get("execution_tracking", {})
        self.assertEqual(
            str(tracking.get("result_artifact") or ""),
            "/tmp/handoff-analysis-780.broker-result.json",
        )

    async def test_continue_initiative_recovers_persisted_analysis_completion_and_dispatches_next_task(self) -> None:
        objective = SimpleNamespace(
            id=131,
            title="Execution completion analysis",
            priority="high",
            state="in_progress",
            owner="mim",
            constraints_json=[],
            metadata_json={},
            created_at="2026-04-20T00:00:00Z",
        )
        stale_task = SimpleNamespace(
            id=781,
            objective_id=131,
            title="Inspect lifecycle completion conditions",
            details="inspect lifecycle state",
            dependencies=[],
            acceptance_criteria="analysis result artifact exists",
            state="in_progress",
            assigned_to="codex",
            readiness="in_progress",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_analysis",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="queued",
            dispatch_artifact_json={
                "handoff_id": "handoff-analysis-781",
                "task_path": "/tmp/handoff-analysis-781.task.json",
                "status_path": "/tmp/handoff-analysis-781.status.json",
                "latest_result_summary": "Lifecycle inspection completed.",
                "latest_result": {
                    "broker_preparation": {
                        "automatic_live_response": {
                            "status": "completed",
                            "result_artifact": "/tmp/handoff-analysis-781.broker-result.json",
                        },
                        "automatic_live_interpretation": {
                            "status": "completed",
                            "classification": "model_response_text",
                        },
                    }
                },
            },
            metadata_json={"execution_tracking": {"task_created": True, "task_dispatched": True}},
            created_at="2026-04-20T00:00:00Z",
        )
        next_task = SimpleNamespace(
            id=782,
            objective_id=131,
            title="Dispatch follow-up implementation",
            details="patch the next step",
            dependencies=[781],
            acceptance_criteria="implementation queued",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={"execution_tracking": {"task_created": True}},
            created_at="2026-04-20T00:00:01Z",
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=_FakeExecuteResult([objective])))

        async def _tasks_for_objective(_db, objective_id):
            return [stale_task, next_task] if objective_id == 131 else []

        with patch(
            "core.autonomy_driver_service._tasks_for_objective",
            new=AsyncMock(side_effect=_tasks_for_objective),
        ), patch(
            "core.autonomy_driver_service._select_next_ready_task",
            new=AsyncMock(side_effect=[(objective, next_task), (None, None)]),
        ), patch(
            "core.autonomy_driver_service.submit_handoff_payload",
            new=AsyncMock(
                return_value={
                    "handoff_id": "handoff-exec-782",
                    "status": "queued",
                    "task_path": "/tmp/handoff-exec-782.task.json",
                    "status_path": "/tmp/handoff-exec-782.status.json",
                    "latest_result_summary": "Queued and acknowledged.",
                }
            ),
        ), patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=None),
        ), patch(
            "core.autonomy_driver_service.recompute_objective_state",
            new=AsyncMock(return_value="in_progress"),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ), patch(
            "core.autonomy_driver_service.build_initiative_status",
            new=AsyncMock(return_value={"status": "active"}),
        ):
            result = await continue_initiative(
                fake_db,
                objective_id=131,
                actor="mim",
                source="unit-test",
                max_auto_steps=2,
            )

        self.assertEqual(stale_task.state, "completed")
        self.assertEqual(stale_task.dispatch_status, "completed")
        stale_tracking = stale_task.metadata_json.get("execution_tracking", {})
        self.assertEqual(
            str(stale_tracking.get("result_artifact") or ""),
            "/tmp/handoff-analysis-781.broker-result.json",
        )
        self.assertEqual(len(result["dispatched"]), 1)
        self.assertEqual(next_task.state, "in_progress")

    async def test_continue_initiative_recovers_shared_terminal_failure_for_codex_task(self) -> None:
        objective = SimpleNamespace(
            id=132,
            title="Execution completion recovery",
            priority="high",
            state="in_progress",
            owner="mim",
            constraints_json=[],
            metadata_json={},
            created_at="2026-04-20T00:00:00Z",
        )
        stale_task = SimpleNamespace(
            id=783,
            objective_id=132,
            title="Run bounded implementation",
            details="dispatch bounded codex task",
            dependencies=[],
            acceptance_criteria="implementation result recorded",
            state="in_progress",
            assigned_to="codex",
            readiness="in_progress",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="queued",
            dispatch_artifact_json={
                "handoff_id": "objective-2900-task-7117",
                "task_path": "/tmp/handoff-exec-783.task.json",
                "status_path": "/tmp/handoff-exec-783.status.json",
                "latest_result_summary": "Queued bounded implementation task.",
                "latest_result": {
                    "broker_preparation": {
                        "automatic_live_response": {
                            "status": "completed",
                            "result_artifact": "/tmp/handoff-exec-783.broker-result.json",
                        },
                        "automatic_live_interpretation": {
                            "status": "completed",
                            "classification": "model_response_text",
                        },
                    }
                },
            },
            metadata_json={"execution_tracking": {"task_created": True, "task_dispatched": True}},
            created_at="2026-04-20T00:00:00Z",
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=_FakeExecuteResult([objective])))

        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_root = Path(tmp_dir)
            (shared_root / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-2900-task-7117",
                        "task_id": "objective-2900-task-7117",
                        "status": "failed",
                        "result_status": "failed",
                        "error": "Execution engine 'local' failed and fallback is unavailable.",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )

            with patch(
                "core.autonomy_driver_service._tasks_for_objective",
                new=AsyncMock(return_value=[stale_task]),
            ), patch(
                "core.autonomy_driver_service._select_next_ready_task",
                new=AsyncMock(return_value=(None, None)),
            ), patch(
                "core.autonomy_driver_service.refresh_task_readinesses",
                new=AsyncMock(return_value=None),
            ), patch(
                "core.autonomy_driver_service.recompute_objective_state",
                new=AsyncMock(return_value="failed"),
            ), patch(
                "core.autonomy_driver_service.write_journal",
                new=AsyncMock(return_value=None),
            ), patch(
                "core.autonomy_driver_service.build_initiative_status",
                new=AsyncMock(return_value={"status": "failed"}),
            ), patch(
                "core.autonomy_driver_service.RUNTIME_SHARED_DIR",
                shared_root,
            ):
                result = await continue_initiative(
                    fake_db,
                    objective_id=132,
                    actor="mim",
                    source="unit-test",
                    max_auto_steps=1,
                )

        self.assertEqual(stale_task.state, "failed")
        self.assertEqual(stale_task.dispatch_status, "failed")
        stale_tracking = stale_task.metadata_json.get("execution_tracking", {})
        self.assertTrue(str(stale_tracking.get("result_artifact") or "").endswith("TOD_MIM_TASK_RESULT.latest.json"))
        self.assertIn("local' failed", str(stale_tracking.get("execution_result") or ""))
        self.assertEqual(result["status"]["status"], "failed")

    async def test_continue_initiative_recovers_bounded_development_completion_from_explicit_completion_artifact(self) -> None:
        objective = SimpleNamespace(
            id=133,
            title="Execution completion recovery",
            priority="high",
            state="in_progress",
            owner="mim",
            constraints_json=[],
            metadata_json={},
            created_at="2026-04-20T00:00:00Z",
        )
        stale_task = SimpleNamespace(
            id=784,
            objective_id=133,
            title="Run bounded implementation",
            details="dispatch bounded codex task",
            dependencies=[],
            acceptance_criteria="implementation result recorded",
            state="in_progress",
            assigned_to="codex",
            readiness="in_progress",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="queued",
            dispatch_artifact_json={
                "handoff_id": "objective-2900-task-7117-implement-bounded-work",
                "task_path": "/tmp/objective-2900-task-7117.task.json",
                "status_path": "/tmp/objective-2900-task-7117.status.json",
                "latest_result_summary": "Queued bounded implementation task.",
                "latest_result": {
                    "broker_preparation": {
                        "automatic_live_response": {
                            "status": "completed",
                            "result_artifact": "/tmp/objective-2900-task-7117.broker-result.json",
                        },
                        "automatic_live_interpretation": {
                            "status": "completed",
                            "classification": "model_response_text",
                        },
                    }
                },
            },
            metadata_json={"execution_tracking": {"task_created": True, "task_dispatched": True}},
            created_at="2026-04-20T00:00:00Z",
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=_FakeExecuteResult([objective])))

        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            task_path = temp_root / "objective-2900-task-7117.task.json"
            status_path = temp_root / "objective-2900-task-7117.status.json"
            completion_path = temp_root / "objective-2900-task-7117-implement-bounded-work.completion.json"
            task_path.write_text("{}\n", encoding="utf-8")
            status_path.write_text("{}\n", encoding="utf-8")
            completion_path.write_text(
                json.dumps(
                    {
                        "request_id": "objective-2900-task-7117-implement-bounded-work",
                        "task_id": "784",
                        "status": "completed",
                        "result_status": "completed",
                        "summary": "Bounded implementation completed with validation evidence.",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            stale_task.dispatch_artifact_json["task_path"] = str(task_path)
            stale_task.dispatch_artifact_json["status_path"] = str(status_path)

            with patch(
                "core.autonomy_driver_service._tasks_for_objective",
                new=AsyncMock(return_value=[stale_task]),
            ), patch(
                "core.autonomy_driver_service._select_next_ready_task",
                new=AsyncMock(return_value=(None, None)),
            ), patch(
                "core.autonomy_driver_service.refresh_task_readinesses",
                new=AsyncMock(return_value=None),
            ), patch(
                "core.autonomy_driver_service.recompute_objective_state",
                new=AsyncMock(return_value="completed"),
            ), patch(
                "core.autonomy_driver_service.write_journal",
                new=AsyncMock(return_value=None),
            ), patch(
                "core.autonomy_driver_service.build_initiative_status",
                new=AsyncMock(return_value={"status": "completed"}),
            ):
                result = await continue_initiative(
                    fake_db,
                    objective_id=133,
                    actor="mim",
                    source="unit-test",
                    max_auto_steps=1,
                )

        self.assertEqual(stale_task.state, "completed")
        self.assertEqual(stale_task.dispatch_status, "completed")
        stale_tracking = stale_task.metadata_json.get("execution_tracking", {})
        self.assertTrue(str(stale_tracking.get("result_artifact") or "").endswith(".completion.json"))
        self.assertEqual(result["status"]["status"], "completed")

    async def test_failed_handoff_stays_failed(self) -> None:
        fake_db = SimpleNamespace()
        objective = SimpleNamespace(
            id=14,
            title="Execution completion check",
            constraints_json=[],
        )
        task = SimpleNamespace(
            id=79,
            objective_id=14,
            title="Dispatch executable task",
            details="dispatch",
            dependencies=[],
            acceptance_criteria="result artifact exists",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={"execution_tracking": {"task_created": True}},
            created_at="2026-04-16T00:00:00Z",
        )

        submission = {
            "handoff_id": "handoff-exec-79",
            "status": "failed",
            "task_path": "/tmp/handoff-exec-79.task.json",
            "status_path": "/tmp/handoff-exec-79.status.json",
            "latest_result_summary": "Execution failed before result artifact creation.",
        }

        with patch(
            "core.autonomy_driver_service.submit_handoff_payload",
            new=AsyncMock(return_value=submission),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await _dispatch_codex_task(
                fake_db,
                objective=objective,
                task=task,
                actor="mim",
                source="unit-test",
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(task.dispatch_status, "failed")
        self.assertEqual(task.state, "failed")

    async def test_blocked_handoff_stays_blocked(self) -> None:
        fake_db = SimpleNamespace()
        objective = SimpleNamespace(
            id=15,
            title="Execution completion check",
            constraints_json=[],
        )
        task = SimpleNamespace(
            id=80,
            objective_id=15,
            title="Dispatch executable task",
            details="dispatch",
            dependencies=[],
            acceptance_criteria="result artifact exists",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={"execution_tracking": {"task_created": True}},
            created_at="2026-04-16T00:00:00Z",
        )

        submission = {
            "handoff_id": "handoff-exec-80",
            "status": "blocked",
            "task_path": "/tmp/handoff-exec-80.task.json",
            "status_path": "/tmp/handoff-exec-80.status.json",
            "latest_result_summary": "Execution could not start because the local broker is unavailable.",
        }

        with patch(
            "core.autonomy_driver_service.submit_handoff_payload",
            new=AsyncMock(return_value=submission),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await _dispatch_codex_task(
                fake_db,
                objective=objective,
                task=task,
                actor="mim",
                source="unit-test",
            )

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(task.dispatch_status, "blocked")
        self.assertEqual(task.state, "blocked")

    async def test_dispatch_codex_task_publishes_fresh_bridge_request_artifacts(self) -> None:
        fake_db = SimpleNamespace()
        objective = SimpleNamespace(
            id=683,
            title="Project 2 activation",
            priority="high",
            constraints_json=[],
        )
        task = SimpleNamespace(
            id=2680,
            objective_id=683,
            title="Patch canonical task publication",
            details="publish a fresh Project 2 TOD task request packet",
            dependencies=[],
            acceptance_criteria="fresh request packet exists",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={"execution_tracking": {"task_created": True}},
            created_at="2026-04-20T00:00:00Z",
        )

        submission = {
            "handoff_id": "objective-683-task-2680-patch-canonical-task-publication",
            "status": "queued",
            "task_path": "/tmp/objective-683-task-2680.task.json",
            "status_path": "/tmp/objective-683-task-2680.status.json",
            "latest_result_summary": "Queued for bounded implementation.",
        }

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "core.autonomy_driver_service.RUNTIME_SHARED_DIR",
            new=Path(temp_dir),
        ), patch(
            "core.autonomy_driver_service.submit_handoff_payload",
            new=AsyncMock(return_value=submission),
        ), patch(
            "core.autonomy_driver_service.write_journal",
            new=AsyncMock(return_value=None),
        ):
            result = await _dispatch_codex_task(
                fake_db,
                objective=objective,
                task=task,
                actor="mim",
                source="unit-test",
            )

            request_payload = json.loads((Path(temp_dir) / "MIM_TOD_TASK_REQUEST.latest.json").read_text(encoding="utf-8"))
            trigger_payload = json.loads((Path(temp_dir) / "MIM_TO_TOD_TRIGGER.latest.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "queued")
        self.assertEqual(request_payload["packet_type"], "mim-tod-task-request-v1")
        self.assertEqual(request_payload["objective_id"], "objective-683")
        self.assertEqual(request_payload["task_id"], "objective-683-task-2680")
        self.assertEqual(request_payload["source_service"], "initiative_codex_dispatch")
        self.assertEqual(
            request_payload["request_id"],
            "objective-683-task-2680-patch-canonical-task-publication",
        )
        self.assertEqual(trigger_payload["packet_type"], "shared-trigger-v1")
        self.assertEqual(trigger_payload["artifact"], "MIM_TOD_TASK_REQUEST.latest.json")
        self.assertEqual(trigger_payload["source_service"], "initiative_codex_dispatch")
        self.assertEqual(
            task.dispatch_artifact_json.get("bridge_artifacts", {}).get("status"),
            "published",
        )


class AutonomyDriverStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_status_ignores_completed_objective_without_active_tasks(self) -> None:
        objective = SimpleNamespace(
            id=17,
            title="Completed initiative",
            description="Finished work",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="completed",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={},
            created_at="2026-04-15T00:00:00Z",
        )
        completed_task = SimpleNamespace(
            id=201,
            objective_id=17,
            title="Completed task",
            details="done",
            dependencies=[],
            acceptance_criteria="done",
            state="completed",
            assigned_to="codex",
            readiness="completed",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="completed",
            dispatch_artifact_json={},
            metadata_json=AutonomyDriverServiceTests._completed_tracking("completed-task-201"),
            created_at="2026-04-15T00:00:01Z",
        )

        execute_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [objective])
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

        with patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[completed_task]),
        ):
            status = await build_initiative_status(db=fake_db)

        self.assertEqual(status["summary"], "No active MIM initiative is currently queued.")
        self.assertEqual(status["active_objective"], {})
        self.assertEqual(status["active_task"], {})
        self.assertEqual(len(status["completed_recently"]), 1)
        self.assertEqual(status["completed_recently"][0]["title"], "Completed task")

    async def test_general_status_prefers_newer_completed_objective_over_older_stale_ready_objective(self) -> None:
        newer_completed_objective = SimpleNamespace(
            id=28,
            title="Drive autonomous continuation validation [-GATEWAY]",
            description="Finished validation",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="completed",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": True,
                    "execution_started": True,
                    "execution_result": "1_tasks_completed",
                    "completed_task_count": 1,
                    "task_count": 1,
                    "execution_state": "completed",
                }
            },
            created_at="2026-04-16T16:28:20Z",
        )
        older_active_objective = SimpleNamespace(
            id=25,
            title="MIM-CONTINUOUS-EXECUTION-VALIDATION-RAW-COMPILE",
            description="Older stale objective",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="in_progress",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={},
            created_at="2026-04-16T15:05:25Z",
        )
        completed_task = SimpleNamespace(
            id=1335,
            objective_id=28,
            title="Continuation validation step 8: repeat continuation C",
            details="done",
            dependencies=[],
            acceptance_criteria="done",
            state="completed",
            assigned_to="mim",
            readiness="completed",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="continuation_validation",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="completed",
            dispatch_artifact_json={},
            metadata_json=AutonomyDriverServiceTests._completed_tracking("completed-task-1335"),
            created_at="2026-04-16T16:28:20Z",
        )
        ready_task = SimpleNamespace(
            id=1313,
            objective_id=25,
            title="Implement bounded work",
            details="queued",
            dependencies=[],
            acceptance_criteria="do work",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={},
            created_at="2026-04-16T15:05:25Z",
        )

        execute_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [newer_completed_objective, older_active_objective])
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

        async def _refresh_task_readinesses(_db, objective_id):
            if objective_id == 28:
                return [completed_task]
            if objective_id == 25:
                return [ready_task]
            return []

        with patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(side_effect=_refresh_task_readinesses),
        ):
            status = await build_initiative_status(db=fake_db)

        self.assertEqual(status["active_objective"]["objective_id"], 28)
        self.assertEqual(
            status["summary"],
            "Objective Drive autonomous continuation validation [-GATEWAY] is complete. Next task after this: Implement bounded work.",
        )
        self.assertEqual(status["active_task"], {})
        self.assertEqual(status["next_task"]["task_id"], 1313)
        self.assertEqual(status["next_task"]["display_title"], "Implement bounded work")
        self.assertEqual(status["completed_recently"][0]["task_id"], 1335)

    async def test_general_status_reports_planning_complete_without_execution_evidence(self) -> None:
        objective = SimpleNamespace(
            id=52,
            title="Planning-only initiative",
            description="Plan generated but nothing executed",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="in_progress",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={
                "planning_only": True,
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": False,
                    "execution_started": False,
                    "execution_result": None,
                    "execution_state": "created",
                }
            },
            created_at="2026-04-16T18:00:00Z",
        )
        planned_task = SimpleNamespace(
            id=1901,
            objective_id=52,
            title="Planned implementation task",
            details="queued",
            dependencies=[],
            acceptance_criteria="do work",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": False,
                    "execution_started": False,
                    "execution_result": None,
                    "request_id": "",
                    "execution_trace": "",
                    "result_artifact": "",
                }
            },
            created_at="2026-04-16T18:00:01Z",
        )

        execute_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [objective])
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

        with patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[planned_task]),
        ):
            status = await build_initiative_status(db=fake_db)

        self.assertEqual(status["execution_state"], "created")
        self.assertEqual(status["status"], "idle")
        self.assertEqual(status["activity"]["label"], "Planned")
        self.assertIn("Planning-only initiative", status["summary"])
        self.assertNotIn("is complete", status["summary"])

    async def test_general_status_uses_resume_activity_start_time_to_avoid_false_stale(self) -> None:
        objective = SimpleNamespace(
            id=53,
            title="Resumed initiative",
            description="Existing inflight work resumed now",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="in_progress",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": True,
                    "execution_started": True,
                    "execution_state": "executing",
                    "activity_started_at": "2099-01-01T00:00:00Z",
                }
            },
            created_at="2026-04-16T18:00:00Z",
        )
        resumed_task = SimpleNamespace(
            id=1902,
            objective_id=53,
            title="Resumed implementation task",
            details="queued",
            dependencies=[],
            acceptance_criteria="do work",
            state="in_progress",
            assigned_to="codex",
            readiness="in_progress",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="queued",
            dispatch_artifact_json={},
            metadata_json={
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": True,
                    "execution_started": True,
                    "activity_started_at": "2099-01-01T00:00:00Z",
                    "request_id": "req-resumed-1902",
                }
            },
            created_at="2026-04-16T18:00:01Z",
        )

        execute_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [objective])
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

        with patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[resumed_task]),
        ):
            status = await build_initiative_status(db=fake_db)

        self.assertEqual(status["active_objective"]["objective_id"], 53)
        self.assertEqual(status["status"], "working")
        self.assertEqual(status["activity"]["state"], "working")
        self.assertIn("actively running", status["activity"]["summary"])

    async def test_general_status_prefers_blocked_execution_over_older_planning_only_objective(self) -> None:
        newer_blocked_objective = SimpleNamespace(
            id=71,
            title="Blocked execution initiative",
            description="Broker unavailable",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="blocked",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": True,
                    "execution_started": False,
                    "execution_result": None,
                    "execution_state": "dispatched",
                }
            },
            created_at="2026-04-19T00:00:00Z",
        )
        older_planning_objective = SimpleNamespace(
            id=70,
            title="Planning-only initiative",
            description="Plan generated but nothing executed",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="in_progress",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={
                "planning_only": True,
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": False,
                    "execution_started": False,
                    "execution_result": None,
                    "execution_state": "created",
                }
            },
            created_at="2026-04-18T00:00:00Z",
        )
        blocked_task = SimpleNamespace(
            id=2001,
            objective_id=71,
            title="Blocked bounded task",
            details="blocked",
            dependencies=[],
            acceptance_criteria="do work",
            state="blocked",
            assigned_to="codex",
            readiness="blocked",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="blocked",
            dispatch_artifact_json={},
            metadata_json={"execution_tracking": {"task_created": True, "task_dispatched": True}},
            created_at="2026-04-19T00:00:01Z",
        )
        planned_task = SimpleNamespace(
            id=2000,
            objective_id=70,
            title="Planned implementation task",
            details="queued",
            dependencies=[],
            acceptance_criteria="do work",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={"execution_tracking": {"task_created": True}},
            created_at="2026-04-18T00:00:01Z",
        )

        execute_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [newer_blocked_objective, older_planning_objective])
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

        async def _refresh_task_readinesses(_db, objective_id):
            if objective_id == 71:
                return [blocked_task]
            if objective_id == 70:
                return [planned_task]
            return []

        with patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(side_effect=_refresh_task_readinesses),
        ):
            status = await build_initiative_status(db=fake_db)

        self.assertEqual(status["active_objective"]["objective_id"], 71)
        self.assertEqual(status["status"], "stuck")

    async def test_general_status_includes_program_status_snapshot(self) -> None:
        objective = SimpleNamespace(
            id=60,
            title="Planning-only initiative",
            description="Plan generated but nothing executed",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="in_progress",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={
                "initiative_id": "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
                "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": False,
                    "execution_started": False,
                    "execution_result": None,
                    "execution_state": "created",
                },
            },
            created_at="2026-04-18T00:00:00Z",
        )
        planned_task = SimpleNamespace(
            id=1902,
            objective_id=60,
            title="Planned implementation task",
            details="queued",
            dependencies=[],
            acceptance_criteria="do work",
            state="queued",
            assigned_to="codex",
            readiness="ready",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="pending",
            dispatch_artifact_json={},
            metadata_json={"program_id": "MIM-12-AUTONOMOUS-EVOLUTION"},
            created_at="2026-04-18T00:00:01Z",
        )

        execute_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [objective])
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

        with patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[planned_task]),
        ), patch(
            "core.autonomy_driver_service.build_program_status_snapshot",
            return_value={
                "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                "summary": "Program MIM-12-AUTONOMOUS-EVOLUTION status: MIM-DAY-01-EXECUTION-BOUND-COMPLETION=created.",
                "projects": [
                    {
                        "project_id": "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
                        "status": "created",
                        "objective": "Enforce completion only after execution evidence",
                    }
                ],
            },
        ):
            status = await build_initiative_status(db=fake_db)

        self.assertEqual(status["program_status"]["program_id"], "MIM-12-AUTONOMOUS-EVOLUTION")
        self.assertIn(
            "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
            status["program_status"]["summary"],
        )

    async def test_general_status_marks_long_running_bounded_task_as_stale(self) -> None:
        objective = SimpleNamespace(
            id=61,
            title="PROGRAM_ID: MIM-12-AUTONOMOUS-EVOLUTION Project_1_ID: MIM-DAY-01-EXECUTION-BOUND-COMPLETION OBJECTIVE: Enforce that o...",
            description="Bounded execution evidence objective",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="in_progress",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={
                "initiative_id": "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": True,
                    "execution_started": True,
                    "execution_result": None,
                    "execution_state": "executing",
                },
            },
            created_at="2026-04-18T00:00:00Z",
        )
        active_task = SimpleNamespace(
            id=1903,
            objective_id=61,
            title="Implement bounded work for: PROGRAM_ID: MIM-12-AUTONOMOUS-EVOLUTION Project_1_ID: MIM-DAY-01-EXECUTION-BOUND-COMPLETION OBJECTIVE: Enforce that o...",
            details="PROGRAM_ID: MIM-12-AUTONOMOUS-EVOLUTION Project_1_ID: MIM-DAY-01-EXECUTION-BOUND-COMPLETION OBJECTIVE: Enforce that objectives cannot complete from planning text alone. GOAL: Completion must require real execution evidence.",
            dependencies=[],
            acceptance_criteria="do work",
            state="in_progress",
            assigned_to="codex",
            readiness="in_progress",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="queued",
            dispatch_artifact_json={},
            metadata_json={
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": True,
                    "execution_started": True,
                    "execution_result": None,
                    "request_id": "objective-61-task-1903",
                    "execution_trace": "trace:objective-61-task-1903",
                    "result_artifact": "",
                }
            },
            created_at="2026-04-18T00:00:01Z",
        )

        execute_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [objective])
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

        with patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[active_task]),
        ), patch(
            "core.autonomy_driver_service.build_program_status_snapshot",
            return_value={
                "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                "summary": "Program MIM-12-AUTONOMOUS-EVOLUTION status: MIM-DAY-01-EXECUTION-BOUND-COMPLETION=executing.",
                "projects": [
                    {
                        "project_id": "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
                        "status": "executing",
                        "objective": "Enforce completion only after execution evidence",
                    }
                ],
            },
        ):
            status = await build_initiative_status(db=fake_db)

        self.assertEqual(status["status"], "stale")
        self.assertEqual(status["activity"]["state"], "stale")
        self.assertEqual(
            status["active_objective"]["display_title"],
            "Enforce completion only after execution evidence",
        )
        self.assertIn(
            "Completion must require real execution evidence",
            status["active_task"]["display_title"],
        )
        self.assertEqual(status["progress"]["percent"], 0)

    async def test_general_status_reports_working_activity_and_progress(self) -> None:
        objective = SimpleNamespace(
            id=62,
            title="Execution evidence objective",
            description="Fresh bounded execution objective",
            priority="high",
            constraints_json=[],
            success_criteria="done",
            state="in_progress",
            owner="mim",
            execution_mode="auto",
            auto_continue=True,
            boundary_mode="soft",
            metadata_json={
                "initiative_id": "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": True,
                    "execution_started": True,
                    "execution_result": "1_tasks_completed",
                    "execution_state": "executing",
                },
            },
            created_at="2999-01-01T00:00:00Z",
        )
        completed_task = SimpleNamespace(
            id=1904,
            objective_id=62,
            title="Completed bounded task",
            details="done",
            dependencies=[],
            acceptance_criteria="done",
            state="completed",
            assigned_to="mim",
            readiness="completed",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="completed",
            dispatch_artifact_json={},
            metadata_json=AutonomyDriverServiceTests._completed_tracking("completed-task-1904"),
            created_at="2999-01-01T00:00:01Z",
        )
        active_task = SimpleNamespace(
            id=1905,
            objective_id=62,
            title="Execute current bounded task",
            details="current task details",
            dependencies=[],
            acceptance_criteria="do work",
            state="in_progress",
            assigned_to="codex",
            readiness="in_progress",
            boundary_mode="soft",
            start_now=True,
            human_prompt_required=False,
            execution_scope="bounded_development",
            expected_outputs_json=[],
            verification_commands_json=[],
            dispatch_status="queued",
            dispatch_artifact_json={},
            metadata_json={
                "execution_tracking": {
                    "task_created": True,
                    "task_dispatched": True,
                    "execution_started": True,
                    "execution_result": None,
                    "request_id": "objective-62-task-1905",
                    "execution_trace": "trace:objective-62-task-1905",
                    "result_artifact": "",
                }
            },
            created_at="2999-01-01T00:00:02Z",
        )

        execute_result = SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [objective])
        )
        fake_db = SimpleNamespace(execute=AsyncMock(return_value=execute_result))

        with patch(
            "core.autonomy_driver_service.refresh_task_readinesses",
            new=AsyncMock(return_value=[completed_task, active_task]),
        ):
            status = await build_initiative_status(db=fake_db)

        self.assertEqual(status["status"], "working")
        self.assertEqual(status["activity"]["state"], "working")
        self.assertEqual(status["progress"]["completed_task_count"], 1)
        self.assertEqual(status["progress"]["task_count"], 2)
        self.assertEqual(status["progress"]["percent"], 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)