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
            self.assertEqual(rebuilt["publication_boundary"]["authoritative_surface"], "remote_raspberry_pi")

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

    def test_rebuild_prefers_remote_boundary_request_when_status_artifact_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            self._seed_shared_truth(shared_dir, request_objective="objective-75")
            self._write_json(
                shared_dir / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json",
                {
                    "generated_at": "2026-04-02T23:30:00Z",
                    "authoritative_surface": "remote_raspberry_pi",
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
            self.assertEqual(rebuilt["live_task_request"]["task_id"], "objective-97-task-remote-9001")
            self.assertEqual(rebuilt["live_task_request"]["objective_id"], "objective-97")
            self.assertEqual(rebuilt["live_task_request"]["publication_lane"], "remote_publish_capable")
            self.assertEqual(rebuilt["publication_boundary"]["authoritative_path"], "/home/testpilot/mim/runtime/shared/MIM_TOD_TASK_REQUEST.latest.json")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)