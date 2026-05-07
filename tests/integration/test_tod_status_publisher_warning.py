import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIB_PATH = ROOT / "scripts" / "tod_status_signal_lib.py"
CATCHUP_SCRIPT = ROOT / "scripts" / "watch_tod_catchup_status.sh"
DASHBOARD_SCRIPT = ROOT / "scripts" / "tod_status_dashboard.sh"


def load_module():
    spec = importlib.util.spec_from_file_location("tod_status_signal_lib", LIB_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TodStatusPublisherWarningTest(unittest.TestCase):
    def test_build_publisher_warning_detects_stale_live_task_stream(self) -> None:
        module = load_module()
        warning = module.build_publisher_warning(
            integration={
                "mim_refresh": {
                    "attempted": True,
                    "copied_json": True,
                    "copied_yaml": True,
                    "copied_manifest": True,
                    "failure_reason": "",
                    "resolved_source_root": "/home/testpilot/mim/runtime/shared",
                }
            },
            context_export={"objective_active": "80", "release_tag": "objective-80"},
            handshake={
                "truth": {"objective_active": "80", "release_tag": "objective-80"}
            },
            task_request={
                "objective_id": "objective-75",
                "task_id": "objective-75-task-3271",
                "source_service": "objective75_overnight",
                "source_instance_id": "objective75_overnight:1444581",
            },
            coordination_ack={"objective_id": "objective-75"},
        )

        self.assertTrue(warning["active"])
        self.assertEqual(warning["code"], "publisher_objective_mismatch")
        self.assertEqual(warning["canonical_objective_active"], "80")
        self.assertEqual(warning["live_task_objective"], "75")
        self.assertTrue(warning["stale_publisher_service"])
        self.assertIn("objective 80", warning["message"])
        self.assertIn("objective 75", warning["message"])

    def test_build_publisher_warning_uses_normalized_promoted_objective(self) -> None:
        module = load_module()
        warning = module.build_publisher_warning(
            integration={
                "mim_refresh": {
                    "attempted": True,
                    "copied_json": True,
                    "copied_yaml": True,
                    "copied_manifest": True,
                    "failure_reason": "",
                    "resolved_source_root": "/home/testpilot/mim/runtime/shared",
                },
                "live_task_request": {
                    "objective_id": "objective-663",
                    "normalized_objective_id": "665",
                    "promotion_applied": True,
                    "promotion_reason": "request_objective_differs_from_canonical_export",
                },
            },
            context_export={"objective_active": "665", "release_tag": "objective-665"},
            handshake={
                "truth": {"objective_active": "665", "release_tag": "objective-665"}
            },
            task_request={
                "objective_id": "objective-663",
                "task_id": "objective-663-task-008",
                "source_service": "continuous_task_dispatch",
                "source_instance_id": "continuous_task_dispatch:2239455",
            },
            coordination_ack={"objective_id": "objective-657"},
        )

        self.assertFalse(warning["active"])
        self.assertEqual(warning["code"], "")
        self.assertEqual(warning["canonical_objective_active"], "665")
        self.assertEqual(warning["live_task_objective"], "665")
        self.assertEqual(warning["coordination_objective"], "657")
        self.assertEqual(warning["message"], "")

    def test_watch_tod_catchup_status_emits_publisher_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir) / "shared"
            log_dir = Path(tmp_dir) / "logs"
            shared_dir.mkdir(parents=True, exist_ok=True)
            log_dir.mkdir(parents=True, exist_ok=True)

            (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "generated_at": "2026-03-24T00:34:05Z",
                        "compatible": True,
                        "mim_schema": "2026-03-12-68",
                        "mim_handshake": {
                            "available": True,
                            "objective_active": "75",
                            "schema_version": "2026-03-12-68",
                            "release_tag": "objective-75",
                        },
                        "mim_refresh": {
                            "attempted": True,
                            "copied_json": True,
                            "copied_yaml": True,
                            "copied_manifest": True,
                            "failure_reason": "",
                            "resolved_source_root": "/home/testpilot/mim/runtime/shared",
                        },
                        "objective_alignment": {
                            "status": "in_sync",
                            "tod_current_objective": "75",
                            "mim_objective_active": "75",
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps(
                    {"objective_active": "80", "release_tag": "objective-80"}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json").write_text(
                json.dumps(
                    {
                        "truth": {
                            "objective_active": "80",
                            "schema_version": "2026-03-12-68",
                            "release_tag": "objective-80",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_MANIFEST.latest.json").write_text(
                json.dumps(
                    {
                        "manifest": {
                            "schema_version": "2026-03-12-68",
                            "release_tag": "objective-80",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "task_id": "objective-75-task-3271",
                        "objective_id": "objective-75",
                        "source_service": "objective75_overnight",
                        "source_instance_id": "objective75_overnight:1444581",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_COORDINATION_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "objective_id": "objective-75",
                        "task_id": "objective-75-task-3271",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(CATCHUP_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "LOG_DIR": str(log_dir),
                    "RUN_ONCE": "1",
                    "POLL_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )

            status_payload = json.loads(
                (log_dir / "tod_catchup_status.latest.json").read_text(encoding="utf-8")
            )
            publisher_warning = status_payload.get("publisher_warning", {})
            self.assertTrue(
                bool(publisher_warning.get("active", False)), status_payload
            )
            self.assertEqual(publisher_warning.get("canonical_objective_active"), "80")
            self.assertEqual(publisher_warning.get("live_task_objective"), "75")

    def test_tod_status_dashboard_prints_publisher_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_dir = Path(tmp_dir)
            (shared_dir / "TOD_INTEGRATION_STATUS.latest.json").write_text(
                json.dumps(
                    {
                        "mim_refresh": {
                            "attempted": True,
                            "copied_json": True,
                            "copied_yaml": True,
                            "copied_manifest": True,
                            "failure_reason": "",
                            "resolved_source_root": "/home/testpilot/mim/runtime/shared",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_CONTEXT_EXPORT.latest.json").write_text(
                json.dumps(
                    {"objective_active": "80", "release_tag": "objective-80"}, indent=2
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json").write_text(
                json.dumps(
                    {
                        "truth": {
                            "objective_active": "80",
                            "release_tag": "objective-80",
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text(
                json.dumps(
                    {
                        "objective_id": "objective-75",
                        "task_id": "objective-75-task-3271",
                        "source_service": "objective75_overnight",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "MIM_TOD_COORDINATION_ACK.latest.json").write_text(
                json.dumps({"objective_id": "objective-75"}, indent=2) + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps({"request_id": "objective-75-task-3271"}) + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_MIM_TASK_RESULT.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-75-task-3271",
                        "status": "completed",
                        "compatible": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (shared_dir / "TOD_LOOP_JOURNAL.latest.json").write_text(
                json.dumps({"ok": True}) + "\n", encoding="utf-8"
            )

            completed = subprocess.run(
                ["bash", str(DASHBOARD_SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "STALE_SECONDS": "999999",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            self.assertIn("publisher_warning: ACTIVE", completed.stdout)
            self.assertIn("canonical_objective_active: 80", completed.stdout)
            self.assertIn("live_task_objective: 75", completed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
