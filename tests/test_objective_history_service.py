import json
import tempfile
import unittest
from pathlib import Path

from core.objective_history_service import build_objective_history_summary
from core.objective_history_service import load_objective_history
from core.objective_history_service import persist_program_status_snapshot
from core.objective_history_service import sync_objective_history_from_export_payload


class ObjectiveHistoryServiceTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def test_sync_backfills_objectives_and_marks_gap_as_incomplete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root / "runtime" / "shared"
            history_dir = root / "runtime" / "history" / "objective_history"
            self._write_json(
                root / "runtime" / "formal_program_drive_response.json",
                {
                    "program_status": {
                        "projects": [
                            {
                                "project_id": "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
                                "display_title": "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
                                "objective": "Enforce completion only after execution evidence.",
                                "status": "created",
                                "objective_id": 539,
                                "summary": "The active initiative has blocked tasks and is waiting for intervention or TOD progress.",
                                "progress": {"task_count": 13, "completed_task_count": 0, "percent": 0},
                            },
                            {
                                "project_id": "MIM-DAY-02-INITIATIVE-ISOLATION",
                                "display_title": "MIM-DAY-02-INITIATIVE-ISOLATION",
                                "objective": "Prevent new initiatives from being overwritten.",
                                "goal": "Every incoming initiative stays bound to its own lineage.",
                                "tasks": [
                                    "Trace request_id -> initiative_id -> objective_id -> task_id -> result path.",
                                    "Patch precedence so explicit incoming INITIATIVE_ID wins.",
                                ],
                                "status": "completed",
                                "objective_id": 2900,
                                "summary": "The active initiative objective has completion evidence and is marked complete.",
                                "progress": {"task_count": 2, "completed_task_count": 2, "percent": 100},
                            },
                            {
                                "project_id": "MIM-DAY-15-CLASSIC-LITERATURE-INTRODUCTION",
                                "display_title": "MIM-DAY-15-CLASSIC-LITERATURE-INTRODUCTION",
                                "objective": "Normalize a source-backed classic literature catalog.",
                                "status": "ready",
                            },
                        ]
                    }
                },
            )
            self._write_json(
                root / "runtime" / "shared" / "mim_program_registry.latest.json",
                {
                    "active_program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                    "programs": [
                        {
                            "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                            "projects": [
                                {
                                    "ordinal": 1,
                                    "project_id": "MIM-DAY-01-EXECUTION-BOUND-COMPLETION",
                                    "objective": "Enforce completion only after execution evidence.",
                                    "status": "ready",
                                },
                                {
                                    "ordinal": 2,
                                    "project_id": "MIM-DAY-02-INITIATIVE-ISOLATION",
                                    "objective": "Prevent new initiatives from being overwritten.",
                                    "status": "ready",
                                },
                            ],
                        }
                    ],
                },
            )
            self._write_json(
                root / "runtime" / "reports" / "mim_evolution_training_summary.json",
                {
                    "generated_at": "2026-05-05T14:13:45.034611+00:00",
                    "conversation": {"overall": 0.8424, "scenario_count": 10},
                    "actions": {"pass_ratio": 1.0},
                },
            )
            self._write_json(
                root / "runtime" / "reports" / "classic_literature_catalog_seed.json",
                {
                    "catalog_status": "source_candidate_catalog_ready",
                    "validated_catalog_entry_count": 200,
                    "validated_priority_seed_count": 22,
                    "canonical_link_enrichment_status": "optional_future_enrichment",
                    "validation_basis": "metadata complete; canonical links optional future enrichment",
                },
            )

            payload = {
                "exported_at": "2026-05-05T14:34:57Z",
                "objective_active": "15",
                "latest_completed_objective": "2",
                "current_next_objective": "15",
                "phase": "execution",
                "source_of_truth": {},
            }

            summary = sync_objective_history_from_export_payload(
                payload,
                shared_dir,
                history_dir=history_dir,
                artifact_root=root,
            )

            objective_1 = json.loads((history_dir / "objective_1.json").read_text(encoding="utf-8"))
            objective_2 = json.loads((history_dir / "objective_2.json").read_text(encoding="utf-8"))
            objective_13 = json.loads((history_dir / "objective_13.json").read_text(encoding="utf-8"))

            self.assertEqual(objective_1["status"], "incomplete_evidence")
            self.assertEqual(objective_1["final_outcome"]["status"], "created")
            self.assertEqual(objective_2["status"], "completed")
            self.assertEqual(objective_2["final_outcome"]["status"], "completed")
            self.assertEqual(objective_13["status"], "incomplete_evidence")
            self.assertEqual(objective_13["final_outcome"]["status"], "not_applicable")
            self.assertGreaterEqual(summary["objective_count"], 15)

    def test_summary_can_be_regenerated_from_history_only_after_session_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            history_dir = Path(tmp_dir) / "runtime" / "history" / "objective_history"
            persist_program_status_snapshot(
                {
                    "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                    "projects": [
                        {
                            "project_id": "MIM-DAY-02-INITIATIVE-ISOLATION",
                            "display_title": "MIM-DAY-02-INITIATIVE-ISOLATION",
                            "objective": "Prevent new initiatives from being overwritten.",
                            "tasks": ["Trace lineage", "Patch precedence"],
                            "status": "completed",
                            "summary": "Objective complete.",
                            "progress": {"task_count": 2, "completed_task_count": 2, "percent": 100},
                        }
                    ],
                },
                history_dir=history_dir,
                source="unit_test",
            )

            summary = build_objective_history_summary(history_dir=history_dir)
            records = load_objective_history(history_dir=history_dir)

            self.assertEqual(summary["objective_count"], 1)
            self.assertEqual(summary["entries"][0]["objective_id"], 2)
            self.assertEqual(summary["entries"][0]["status"], "completed")
            self.assertEqual(records[0]["task_list"][0]["title"], "Trace lineage")

    def test_default_archive_separates_day_and_system_objectives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            history_dir = root / "runtime" / "history" / "objective_history"
            day_dir = root / "runtime" / "history" / "day_objectives"
            system_dir = root / "runtime" / "history" / "system_objectives"

            from core import objective_history_service as history_service

            original_default = history_service.DEFAULT_HISTORY_DIR
            original_day = history_service.DAY_OBJECTIVE_HISTORY_DIR
            original_system = history_service.SYSTEM_OBJECTIVE_HISTORY_DIR
            history_service.DEFAULT_HISTORY_DIR = history_dir
            history_service.DAY_OBJECTIVE_HISTORY_DIR = day_dir
            history_service.SYSTEM_OBJECTIVE_HISTORY_DIR = system_dir
            try:
                history_service.persist_objective_record(
                    {
                        "objective_id": 2,
                        "display_title": "MIM-DAY-02-INITIATIVE-ISOLATION",
                        "status": "completed",
                        "final_outcome": {"status": "completed", "summary": "done", "updated_at": "2026-05-05T00:00:00Z"},
                    }
                )
                history_service.persist_objective_record(
                    {
                        "objective_id": 2913,
                        "display_title": "Objective 2913",
                        "status": "incomplete_evidence",
                        "final_outcome": {"status": "incomplete_evidence", "summary": "active", "updated_at": "2026-05-05T00:00:00Z"},
                    }
                )
                summary = history_service.write_objective_history_summary()
            finally:
                history_service.DEFAULT_HISTORY_DIR = original_default
                history_service.DAY_OBJECTIVE_HISTORY_DIR = original_day
                history_service.SYSTEM_OBJECTIVE_HISTORY_DIR = original_system

            self.assertTrue((day_dir / "objective_2.json").exists())
            self.assertTrue((system_dir / "objective_2913.json").exists())
            self.assertTrue((history_dir / "objective_2913.json").exists())
            self.assertEqual(summary["objective_count"], 2)
            day_summary = json.loads((day_dir / "OBJECTIVE_HISTORY_SUMMARY.latest.json").read_text(encoding="utf-8"))
            system_summary = json.loads((system_dir / "OBJECTIVE_HISTORY_SUMMARY.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(day_summary["entries"][0]["objective_id"], 2)
            self.assertEqual(system_summary["entries"][0]["objective_id"], 2913)

    def test_persist_program_status_snapshot_records_terminal_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            history_dir = Path(tmp_dir) / "runtime" / "history" / "objective_history"

            persist_program_status_snapshot(
                {
                    "program_id": "MIM-12-AUTONOMOUS-EVOLUTION",
                    "projects": [
                        {
                            "project_id": "MIM-DAY-05-AUTO-RESUME-AFTER-RECOVERY",
                            "display_title": "MIM-DAY-05-AUTO-RESUME-AFTER-RECOVERY",
                            "objective": "Enable automatic resumption after recovery.",
                            "status": "completed",
                            "summary": "Objective complete with bounded recovery evidence.",
                            "progress": {"task_count": 3, "completed_task_count": 3, "percent": 100},
                        }
                    ],
                },
                history_dir=history_dir,
                source="unit_test",
            )

            payload = json.loads((history_dir / "objective_5.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["final_outcome"]["status"], "completed")
            self.assertTrue(payload["timestamps"]["completed_at"])


if __name__ == "__main__":
    unittest.main()