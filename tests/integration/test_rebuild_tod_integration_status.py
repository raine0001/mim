import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "rebuild_tod_integration_status.py"
GATE_SCRIPT = ROOT / "scripts" / "validate_mim_tod_gate.sh"
PYTHON = ROOT / ".venv" / "bin" / "python"


class RebuildTodIntegrationStatusTest(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _seed_shared_truth(self, shared_dir: Path, *, request_objective: str = "objective-97") -> None:
        self._write_json(
            shared_dir / "MIM_CONTEXT_EXPORT.latest.json",
            {
                "exported_at": "2026-03-30T23:32:24Z",
                "objective_active": "97",
                "latest_completed_objective": "89",
                "current_next_objective": "97",
                "schema_version": "2026-03-24-70",
                "release_tag": "objective-97",
                "phase": "execution",
            },
        )
        (shared_dir / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(
            "objective_active: '97'\n", encoding="utf-8"
        )
        self._write_json(
            shared_dir / "MIM_MANIFEST.latest.json",
            {
                "manifest": {
                    "contract_version": "tod-mim-shared-contract-v1",
                    "schema_version": "2026-03-24-70",
                    "release_tag": "objective-97",
                }
            },
        )
        self._write_json(
            shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json",
            {
                "generated_at": "2026-03-30T23:32:24Z",
                "handshake_version": "mim-tod-shared-export-v1",
                "truth": {
                    "objective_active": "97",
                    "latest_completed_objective": "89",
                    "current_next_objective": "97",
                    "schema_version": "2026-03-24-70",
                    "release_tag": "objective-97",
                    "regression_status": "PASS",
                    "regression_tests": "66/66",
                    "prod_promotion_status": "SUCCESS",
                    "prod_smoke_status": "PASS",
                    "blockers": [],
                },
            },
        )
        self._write_json(
            shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
            {
                "generated_at": "2026-03-31T02:07:23Z",
                "task_id": "objective-97-task-3422",
                "objective_id": request_objective,
                "correlation_id": "obj97-task3422",
                "source_service": "objective75_overnight",
                "source_instance_id": "objective75_overnight:2943914",
            },
        )

    def test_rebuild_produces_gate_passing_status_from_current_shared_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._seed_shared_truth(shared_dir)
            stale_payload = {
                "generated_at": "2026-03-29T21:00:06Z",
                "source": "tod-integration-status-v1",
                "mim_schema": "2026-03-12-67",
                "compatible": True,
                "mim_handshake": {
                    "available": True,
                    "objective_active": "75",
                    "schema_version": "2026-03-12-67",
                    "release_tag": "objective-74",
                },
                "objective_alignment": {
                    "status": "in_sync",
                    "aligned": True,
                    "tod_current_objective": "97",
                    "mim_objective_active": "97",
                },
                "mim_refresh": {
                    "attempted": True,
                    "copied_manifest": True,
                    "source_manifest": "old-manifest",
                    "source_handshake_packet": "old-handshake",
                    "failure_reason": "",
                },
            }
            self._write_json(shared_dir / "TOD_INTEGRATION_STATUS.latest.json", stale_payload)

            completed = subprocess.run(
                [
                    str(PYTHON),
                    str(SCRIPT),
                    "--shared-dir",
                    str(shared_dir),
                    "--mirror-legacy-alias",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuilt["mim_schema"], "2026-03-24-70")
            self.assertEqual(rebuilt["mim_handshake"]["objective_active"], "97")
            self.assertEqual(rebuilt["mim_handshake"]["release_tag"], "objective-97")
            self.assertEqual(rebuilt["objective_alignment"]["status"], "in_sync")
            self.assertTrue(rebuilt["objective_alignment"]["aligned"])
            self.assertEqual(rebuilt["objective_alignment"]["tod_current_objective"], "97")
            self.assertEqual(rebuilt["objective_alignment"]["mim_objective_active"], "97")
            self.assertEqual(rebuilt["mim_refresh"]["failure_reason"], "")
            self.assertEqual(rebuilt["live_task_request"]["publication_lane"], "local_only")
            self.assertTrue(rebuilt["live_task_request"]["local_only_writer"])
            self.assertEqual(rebuilt["publication_boundary"]["authoritative_surface"], "mim_runtime_shared")
            self.assertEqual(rebuilt["publication_boundary"]["authoritative_host"], "192.168.1.120")
            self.assertEqual(rebuilt["publication_boundary"]["authoritative_root"], "/home/testpilot/mim/runtime/shared")

            gate = subprocess.run(
                ["bash", str(GATE_SCRIPT)],
                cwd=ROOT,
                env={**os.environ, "SHARED_DIR": str(shared_dir), "EXPECTED_OBJECTIVE": "97"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
            self.assertIn("GATE: PASS", gate.stdout)

    def test_rebuild_keeps_live_task_objective_mismatch_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._seed_shared_truth(shared_dir, request_objective="objective-98")

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuilt["live_task_request"]["normalized_objective_id"], "98")
            self.assertTrue(rebuilt["live_task_request"]["promotion_applied"])
            self.assertEqual(rebuilt["objective_alignment"]["status"], "mismatch")
            self.assertFalse(rebuilt["objective_alignment"]["aligned"])
            self.assertEqual(rebuilt["objective_alignment"]["tod_current_objective"], "98")
            self.assertEqual(rebuilt["objective_alignment"]["mim_objective_active"], "97")
            self.assertEqual(rebuilt["objective_alignment"]["mim_objective_source"], "context_export")
            self.assertEqual(rebuilt["live_task_request"]["publication_lane"], "local_only")

    def test_rebuild_promotes_completed_stale_request_to_newer_canonical_objective(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._seed_shared_truth(shared_dir, request_objective="objective-152")
            self._write_json(
                shared_dir / "MIM_CONTEXT_EXPORT.latest.json",
                {
                    "exported_at": "2026-04-20T09:32:24Z",
                    "objective_active": "216",
                    "latest_completed_objective": "152",
                    "current_next_objective": "216",
                    "schema_version": "2026-03-24-70",
                    "release_tag": "objective-216",
                    "phase": "execution",
                },
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(
                "objective_active: '216'\n",
                encoding="utf-8",
            )
            self._write_json(
                shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json",
                {
                    "generated_at": "2026-04-20T09:32:24Z",
                    "handshake_version": "mim-tod-shared-export-v1",
                    "truth": {
                        "objective_active": "216",
                        "latest_completed_objective": "152",
                        "current_next_objective": "216",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-216",
                        "regression_status": "PASS",
                        "regression_tests": "4/4",
                        "prod_promotion_status": "EXECUTED",
                        "prod_smoke_status": "PASSED",
                        "blockers": [],
                    },
                },
            )
            self._write_json(
                shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json",
                {
                    "task": {
                        "active_task_id": "objective-152-task-008",
                        "objective_id": "152",
                        "request_task_id": "objective-152-task-008",
                        "authoritative_task_id": "objective-152-task-008",
                    },
                    "state": "completed",
                    "gate": {"pass": True, "promotion_ready": True},
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuilt["objective_alignment"]["status"], "in_sync")
            self.assertTrue(rebuilt["objective_alignment"]["aligned"])
            self.assertEqual(rebuilt["objective_alignment"]["tod_current_objective"], "216")
            self.assertEqual(rebuilt["objective_alignment"]["mim_objective_active"], "216")
            self.assertFalse(rebuilt["live_task_request"].get("terminal_completed_request", False))
            self.assertEqual(rebuilt["live_task_request"]["normalized_objective_id"], "216")

    def test_rebuild_ignores_boundary_reset_when_formal_program_truth_is_newer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._seed_shared_truth(shared_dir, request_objective="objective-152")
            self._write_json(
                shared_dir / "MIM_CONTEXT_EXPORT.latest.json",
                {
                    "exported_at": "2026-04-20T16:16:28Z",
                    "objective_active": "216",
                    "latest_completed_objective": "152",
                    "current_next_objective": "216",
                    "schema_version": "2026-03-24-70",
                    "release_tag": "objective-216",
                    "phase": "execution",
                    "source_of_truth": {
                        "formal_program_truth": {
                            "objective": "216",
                            "execution_state": "executing",
                            "project_status": "executing",
                            "task_id": "1719",
                        }
                    },
                },
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(
                "objective_active: '216'\n",
                encoding="utf-8",
            )
            self._write_json(
                shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json",
                {
                    "generated_at": "2026-04-20T16:16:28Z",
                    "handshake_version": "mim-tod-shared-export-v1",
                    "truth": {
                        "objective_active": "216",
                        "latest_completed_objective": "152",
                        "current_next_objective": "216",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-216",
                        "regression_status": "PASS",
                        "regression_tests": "4/4",
                        "prod_promotion_status": "EXECUTED",
                        "prod_smoke_status": "PASSED",
                        "blockers": [],
                    },
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json",
                {
                    "authoritative_request": {
                        "objective_id": "objective-152",
                        "task_id": "objective-152-task-008",
                        "request_id": "objective-152-task-008",
                    }
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuilt["mim_status"]["objective_active"], "216")
            self.assertEqual(rebuilt["mim_handshake"]["objective_active"], "216")
            self.assertEqual(rebuilt["objective_alignment"]["mim_objective_active"], "216")
            self.assertEqual(rebuilt["objective_alignment"]["tod_current_objective"], "152")
            self.assertFalse(rebuilt["objective_alignment"]["aligned"])
            self.assertEqual(rebuilt["objective_alignment"]["status"], "mismatch")
            self.assertEqual(rebuilt["objective_authority_reset"], {})

    def test_catchup_watcher_can_auto_rebuild_stale_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_dir = root / "shared"
            log_dir = root / "logs"
            shared_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)
            self._seed_shared_truth(shared_dir)
            self._write_json(
                shared_dir / "TOD_INTEGRATION_STATUS.latest.json",
                {
                    "generated_at": "2026-03-29T21:00:06Z",
                    "source": "tod-integration-status-v1",
                    "mim_schema": "2026-03-12-67",
                    "compatible": True,
                    "mim_handshake": {
                        "available": True,
                        "objective_active": "75",
                        "schema_version": "2026-03-12-67",
                        "release_tag": "objective-74",
                    },
                    "objective_alignment": {
                        "status": "in_sync",
                        "aligned": True,
                        "tod_current_objective": "97",
                        "mim_objective_active": "97",
                    },
                    "mim_refresh": {
                        "attempted": True,
                        "copied_manifest": True,
                        "source_manifest": "old-manifest",
                        "source_handshake_packet": "old-handshake",
                        "failure_reason": "",
                    },
                },
            )

            completed = subprocess.run(
                ["bash", str(ROOT / "scripts" / "watch_tod_catchup_status.sh")],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "LOG_DIR": str(log_dir),
                    "RUN_ONCE": "1",
                    "POLL_SECONDS": "1",
                    "EXPECTED_OBJECTIVE": "97",
                    "AUTO_REBUILD_INTEGRATION_STATUS": "1",
                    "PYTHON_BIN": str(PYTHON),
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads((shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rebuilt["mim_schema"], "2026-03-24-70")

            gate_signal = json.loads((shared_dir / "TOD_CATCHUP_GATE.latest.json").read_text(encoding="utf-8"))
            self.assertTrue(gate_signal["gate_pass"])

    def test_rebuild_prefers_local_authoritative_boundary_request_when_status_artifact_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._seed_shared_truth(shared_dir, request_objective="objective-75")
            self._write_json(
                shared_dir / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json",
                {
                    "generated_at": "2026-04-02T23:30:00Z",
                    "authoritative_surface": "mim_runtime_shared",
                    "authoritative_host": "192.168.1.120",
                    "authoritative_root": "/home/testpilot/mim/runtime/shared",
                    "authoritative_request": {
                        "path": str(shared_dir / "MIM_TOD_TASK_REQUEST.latest.json"),
                        "task_id": "objective-97-task-3422",
                        "objective_id": "objective-75",
                        "generated_at": "2026-03-31T02:07:23Z",
                        "source_service": "objective75_overnight",
                        "source_instance_id": "objective75_overnight:2943914",
                    },
                    "remote_request": {
                        "path": "/home/testpilot/mim/runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                        "task_id": "objective-97-task-remote-9001",
                        "objective_id": "objective-97",
                        "generated_at": "2026-04-02T23:29:59Z",
                        "source_service": "mim_tod_auto_reissue",
                        "source_instance_id": "mim_tod_auto_reissue:999",
                    },
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads((shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8"))
            self.assertEqual(rebuilt["live_task_request"]["task_id"], "objective-97-task-3422")
            self.assertEqual(rebuilt["live_task_request"]["objective_id"], "objective-75")
            self.assertEqual(rebuilt["live_task_request"]["publication_lane"], "local_only")
            self.assertEqual(rebuilt["publication_boundary"]["authoritative_path"], str(shared_dir / "MIM_TOD_TASK_REQUEST.latest.json"))

    def test_rebuild_ignores_older_prior_objective_request_for_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "MIM_CONTEXT_EXPORT.latest.json",
                {
                    "exported_at": "2026-04-06T18:42:26Z",
                    "objective_active": "109",
                    "latest_completed_objective": "89",
                    "current_next_objective": "109",
                    "schema_version": "not recorded",
                    "release_tag": "objective-109",
                    "phase": "execution",
                },
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(
                "objective_active: '109'\n", encoding="utf-8"
            )
            self._write_json(
                shared_dir / "MIM_MANIFEST.latest.json",
                {
                    "manifest": {
                        "contract_version": "tod-mim-shared-contract-v1",
                        "schema_version": "not recorded",
                        "release_tag": "objective-109",
                    }
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json",
                {
                    "generated_at": "2026-04-06T18:42:26Z",
                    "handshake_version": "mim-tod-shared-export-v1",
                    "truth": {
                        "objective_active": "109",
                        "latest_completed_objective": "89",
                        "current_next_objective": "109",
                        "schema_version": "not recorded",
                        "release_tag": "objective-109",
                    },
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
                {
                    "generated_at": "2026-04-06T18:10:44Z",
                    "request_id": "objective-108-task-mim-arm-scan-pose-20260406181044",
                    "objective_id": "objective-108",
                    "correlation_id": "obj108-mim-arm-scan-pose-20260406181044",
                    "source_service": "mim_arm_scan_pose_dispatch",
                    "source_instance_id": "mim_arm_scan_pose_dispatch:207873",
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuilt["objective_alignment"]["status"], "in_sync")
            self.assertTrue(rebuilt["objective_alignment"]["aligned"])
            self.assertEqual(rebuilt["objective_alignment"]["tod_current_objective"], "109")
            self.assertEqual(rebuilt["objective_alignment"]["mim_objective_active"], "109")
            self.assertEqual(rebuilt["live_task_request"]["objective_id"], "objective-108")
            self.assertEqual(rebuilt["live_task_request"]["normalized_objective_id"], "109")
            self.assertTrue(rebuilt["live_task_request"]["stale_prior_objective"])
            self.assertEqual(
                rebuilt["live_task_request"]["stale_reason"],
                "live_task_request_older_than_canonical_export",
            )
            self.assertEqual(rebuilt["live_task_request"]["publication_lane"], "remote_publish_capable")

    def test_rebuild_does_not_promote_terminal_review_on_objective_only_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "MIM_CONTEXT_EXPORT.latest.json",
                {
                    "exported_at": "2026-05-04T18:25:00Z",
                    "objective_active": "2913",
                    "latest_completed_objective": "2912",
                    "current_next_objective": "2914",
                    "schema_version": "2026-03-24-70",
                    "release_tag": "objective-2913",
                    "phase": "execution",
                },
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(
                "objective_active: '2913'\n", encoding="utf-8"
            )
            self._write_json(
                shared_dir / "MIM_MANIFEST.latest.json",
                {
                    "manifest": {
                        "contract_version": "tod-mim-shared-contract-v1",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-2913",
                    }
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json",
                {
                    "generated_at": "2026-05-04T18:25:00Z",
                    "truth": {
                        "objective_active": "2913",
                        "latest_completed_objective": "2912",
                        "current_next_objective": "2914",
                    },
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
                {
                    "generated_at": "2026-05-04T18:24:50Z",
                    "request_id": "objective-2913-task-7144-project-3-task-2",
                    "task_id": "objective-2913-task-7144",
                    "objective_id": "objective-2913",
                    "source_service": "mim_tod_auto_reissue",
                },
            )
            self._write_json(
                shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json",
                {
                    "state": "completed",
                    "task": {
                        "active_task_id": "objective-2913-task-1777951503",
                        "authoritative_task_id": "objective-2913-task-1777951503",
                        "objective_id": "2913",
                    },
                    "gate": {"pass": True, "promotion_ready": True},
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )

        self.assertFalse(rebuilt["live_task_request"].get("terminal_completed_request", False))

    def test_rebuild_honors_objective_authority_reset_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "MIM_CONTEXT_EXPORT.latest.json",
                {
                    "exported_at": "2026-04-14T18:34:03Z",
                    "objective_active": "152",
                    "latest_completed_objective": "152",
                    "current_next_objective": "152",
                    "schema_version": "2026-03-24-70",
                    "release_tag": "objective-152",
                    "phase": "operational",
                },
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(
                "objective_active: '152'\n", encoding="utf-8"
            )
            self._write_json(
                shared_dir / "MIM_MANIFEST.latest.json",
                {
                    "manifest": {
                        "contract_version": "tod-mim-shared-contract-v1",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-152",
                    }
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json",
                {
                    "generated_at": "2026-04-14T18:34:03Z",
                    "handshake_version": "mim-tod-shared-export-v1",
                    "truth": {
                        "objective_active": "152",
                        "latest_completed_objective": "152",
                        "current_next_objective": "152",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-152",
                    },
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
                {
                    "generated_at": "2026-04-14T18:40:00Z",
                    "request_id": "objective-170-task-001",
                    "objective_id": "objective-170",
                    "source_service": "mim_arm_safe_home_dispatch",
                    "source_instance_id": "mim_arm_safe_home_dispatch:777",
                },
            )
            self._write_json(
                shared_dir / "OBJECTIVE_AUTHORITY_RESET.latest.json",
                {
                    "objective_ceiling": "152",
                    "rewrite_completion_history": False,
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuilt["mim_status"]["objective_active"], "152")
            self.assertEqual(rebuilt["mim_handshake"]["objective_active"], "152")
            self.assertEqual(rebuilt["objective_alignment"]["status"], "in_sync")
            self.assertEqual(rebuilt["objective_alignment"]["tod_current_objective"], "152")
            self.assertEqual(rebuilt["live_task_request"]["normalized_objective_id"], "152")
            self.assertEqual(
                rebuilt["live_task_request"]["promotion_reason"],
                "request_objective_above_authority_reset_ceiling",
            )
            self.assertEqual(
                rebuilt["live_task_request"]["stale_reason"],
                "objective_above_authority_reset_ceiling",
            )
            self.assertEqual(
                rebuilt["objective_authority_reset"]["objective_ceiling"],
                "152",
            )

    def test_rebuild_infers_authority_reset_from_publication_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "MIM_CONTEXT_EXPORT.latest.json",
                {
                    "exported_at": "2026-04-14T18:34:03Z",
                    "objective_active": "170",
                    "latest_completed_objective": "152",
                    "current_next_objective": "170",
                    "schema_version": "2026-03-24-70",
                    "release_tag": "objective-170",
                    "phase": "execution",
                },
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(
                "objective_active: '170'\n", encoding="utf-8"
            )
            self._write_json(
                shared_dir / "MIM_MANIFEST.latest.json",
                {
                    "manifest": {
                        "contract_version": "tod-mim-shared-contract-v1",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-170",
                    }
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json",
                {
                    "generated_at": "2026-04-14T18:34:03Z",
                    "handshake_version": "mim-tod-shared-export-v1",
                    "truth": {
                        "objective_active": "170",
                        "latest_completed_objective": "152",
                        "current_next_objective": "170",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-170",
                    },
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
                {
                    "generated_at": "2026-04-14T18:40:00Z",
                    "request_id": "mim-request-170-001",
                    "objective_id": "objective-170",
                    "source_service": "mim_tod_auto_reissue",
                    "source_instance_id": "mim_tod_auto_reissue:999",
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json",
                {
                    "generated_at": "2026-04-14T18:41:00Z",
                    "authoritative_request": {
                        "path": str(shared_dir / "MIM_TOD_TASK_REQUEST.latest.json"),
                        "task_id": "objective-152-task-001",
                        "objective_id": "objective-152",
                        "generated_at": "2026-04-14T18:34:03Z",
                        "source_service": "mim_arm_safe_home_dispatch",
                        "source_instance_id": "mim_arm_safe_home_dispatch:777",
                    },
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuilt["mim_status"]["objective_active"], "152")
            self.assertEqual(rebuilt["mim_handshake"]["objective_active"], "152")
            self.assertEqual(rebuilt["objective_alignment"]["status"], "in_sync")
            self.assertEqual(rebuilt["objective_alignment"]["tod_current_objective"], "152")
            self.assertEqual(rebuilt["live_task_request"]["normalized_objective_id"], "152")
            self.assertEqual(
                rebuilt["objective_authority_reset"]["objective_ceiling"],
                "152",
            )
            self.assertEqual(
                rebuilt["objective_authority_reset"]["inferred_from"],
                "publication_boundary_authoritative_request",
            )

    def test_rebuild_does_not_infer_boundary_reset_for_completed_promotion_ready_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "MIM_CONTEXT_EXPORT.latest.json",
                {
                    "exported_at": "2026-04-20T01:15:15Z",
                    "objective_active": "152",
                    "latest_completed_objective": "152",
                    "current_next_objective": "153",
                    "schema_version": "2026-03-24-70",
                    "release_tag": "objective-152",
                    "phase": "operational",
                },
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(
                "objective_active: '152'\n", encoding="utf-8"
            )
            self._write_json(
                shared_dir / "MIM_MANIFEST.latest.json",
                {
                    "manifest": {
                        "contract_version": "tod-mim-shared-contract-v1",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-152",
                    }
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json",
                {
                    "generated_at": "2026-04-20T01:15:15Z",
                    "handshake_version": "mim-tod-shared-export-v1",
                    "truth": {
                        "objective_active": "152",
                        "latest_completed_objective": "152",
                        "current_next_objective": "153",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-152",
                    },
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
                {
                    "generated_at": "2026-04-18T21:49:04Z",
                    "request_id": "objective-152-task-smoke-20260418214904",
                    "task_id": "objective-152-task-smoke-20260418214904",
                    "objective_id": "objective-152",
                    "source_service": "mim_tod_auto_reissue",
                    "source_instance_id": "mim_tod_auto_reissue:999",
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json",
                {
                    "generated_at": "2026-04-20T01:03:38Z",
                    "authoritative_request": {
                        "path": str(shared_dir / "MIM_TOD_TASK_REQUEST.latest.json"),
                        "task_id": "objective-152-task-smoke-20260418214904",
                        "objective_id": "objective-152",
                    },
                },
            )
            self._write_json(
                shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json",
                {
                    "state": "completed",
                    "task": {
                        "active_task_id": "objective-152-task-smoke-20260418214904",
                        "authoritative_task_id": "objective-152-task-smoke-20260418214904",
                        "objective_id": "152",
                    },
                    "gate": {"pass": True, "promotion_ready": True},
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuilt["objective_authority_reset"], {})
            self.assertTrue(rebuilt["live_task_request"]["terminal_completed_request"])
            self.assertEqual(
                rebuilt["live_task_request"]["stale_reason"],
                "request_already_completed_promotion_ready",
            )
            self.assertEqual(rebuilt["objective_alignment"]["status"], "in_sync")
            self.assertEqual(rebuilt["objective_alignment"]["tod_current_objective"], "152")

    def test_rebuild_ignores_inactive_authority_reset_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._write_json(
                shared_dir / "MIM_CONTEXT_EXPORT.latest.json",
                {
                    "exported_at": "2026-04-20T23:30:01Z",
                    "objective_active": "720",
                    "latest_completed_objective": "152",
                    "current_next_objective": "720",
                    "schema_version": "2026-03-24-70",
                    "release_tag": "objective-720",
                    "phase": "execution",
                },
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(
                "objective_active: '720'\n", encoding="utf-8"
            )
            self._write_json(
                shared_dir / "MIM_MANIFEST.latest.json",
                {
                    "manifest": {
                        "contract_version": "tod-mim-shared-contract-v1",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-720",
                    }
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json",
                {
                    "generated_at": "2026-04-20T23:30:01Z",
                    "handshake_version": "mim-tod-shared-export-v1",
                    "truth": {
                        "objective_active": "720",
                        "latest_completed_objective": "152",
                        "current_next_objective": "720",
                        "schema_version": "2026-03-24-70",
                        "release_tag": "objective-720",
                    },
                },
            )
            self._write_json(
                shared_dir / "MIM_TOD_TASK_REQUEST.latest.json",
                {
                    "generated_at": "2026-04-20T23:27:46Z",
                    "request_id": "objective-720-task-008",
                    "task_id": "objective-720-task-008",
                    "objective_id": "objective-720",
                    "source_service": "mim_tod_auto_reissue",
                    "source_instance_id": "mim_tod_auto_reissue:999",
                },
            )
            self._write_json(
                shared_dir / "OBJECTIVE_AUTHORITY_RESET.latest.json",
                {
                    "active": False,
                    "authoritative_current_objective": "216",
                    "metadata": {
                        "rollback_to_objective": "216",
                    },
                },
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--shared-dir", str(shared_dir)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            rebuilt = json.loads(
                (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rebuilt["mim_status"]["objective_active"], "720")
            self.assertEqual(rebuilt["objective_alignment"]["tod_current_objective"], "720")
            self.assertEqual(rebuilt["live_task_request"]["normalized_objective_id"], "720")
            self.assertEqual(rebuilt["objective_authority_reset"], {})



if __name__ == "__main__":
    unittest.main(verbosity=2)