from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "generate_mim_arm_host_state.py"
SPEC = importlib.util.spec_from_file_location("generate_mim_arm_host_state", MODULE_PATH)
generate_mim_arm_host_state = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(generate_mim_arm_host_state)


class GenerateMimArmHostStateTest(unittest.TestCase):
    def test_main_prefers_live_ack_candidate_for_attribution_when_present(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "mim_arm_host_state.latest.json"
            (root / "mim_arm_status.latest.json").write_text(
                json.dumps(
                    {
                        "last_command_result": {
                            "commands_total": 12,
                            "acks_total": 12,
                        }
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "TOD_MIM_TASK_ACK.latest.json").write_text(
                json.dumps(
                    {
                        "request_id": "objective-97-task-mim-arm-safe-home-20260404194149",
                        "task_id": "objective-97-task-mim-arm-safe-home-20260404194149",
                        "correlation_id": "obj97-mim-arm-safe-home-20260404194149",
                        "bridge_runtime": {
                            "current_processing": {
                                "task_id": "objective-97-task-mim-arm-safe-home-20260404194149",
                                "correlation_id": "obj97-mim-arm-safe-home-20260404194149",
                            }
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            args = SimpleNamespace(
                shared_root=str(root),
                output=str(output_path),
                process_match="mim_arm|arm_ui|uvicorn|python.*app",
                controller_glob=["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"],
                camera_glob=["/dev/video*"],
                input_json=[],
                arm_url="http://192.168.1.90:5000/arm_state",
                sim_estop_ok=True,
            )
            arm_state_payload = {
                "app_alive": True,
                "current_pose": [90, 90, 90, 90, 90, 50],
                "estop": {"active": None, "supported": False},
                "last_command_result": {
                    "acks_total": 85,
                    "commands_total": 85,
                    "last_command_sent": "GO_SAFE",
                    "last_command_sent_at": "2026-04-05T16:21:57Z",
                },
                "mode": "development",
                "runtime": "sim",
                "serial": {
                    "serial_ready": True,
                    "status": "ok",
                },
                "camera": {
                    "status": "ok",
                },
                "status": "ok",
            }

            with patch.object(generate_mim_arm_host_state, "parse_args", return_value=args), patch.object(
                generate_mim_arm_host_state,
                "_read_local_json_url",
                return_value=arm_state_payload,
            ), patch.object(
                generate_mim_arm_host_state,
                "run_local",
                side_effect=[
                    {"stdout": "python3 app.py", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                    {"stdout": "/dev/ttyACM0", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                    {"stdout": "/dev/video0", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                ],
            ), patch.object(
                generate_mim_arm_host_state,
                "_uptime_seconds",
                return_value=123.0,
            ), patch.object(
                generate_mim_arm_host_state.socket,
                "gethostname",
                return_value="raspberrypi",
            ):
                exit_code = generate_mim_arm_host_state.main()
                payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["source_payload_path"].endswith("TOD_MIM_TASK_ACK.latest.json"))
        self.assertEqual(payload["command_evidence"]["request_id"], "objective-97-task-mim-arm-safe-home-20260404194149")
        self.assertEqual(payload["last_request_id"], "objective-97-task-mim-arm-safe-home-20260404194149")

    def test_main_prefers_arm_state_fields_when_source_payload_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "mim_arm_host_state.latest.json"
            args = SimpleNamespace(
                shared_root=str(root),
                output=str(output_path),
                process_match="mim_arm|arm_ui|uvicorn|python.*app",
                controller_glob=["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"],
                camera_glob=["/dev/video*"],
                input_json=[],
                arm_url="http://192.168.1.90:5000/arm_state",
                sim_estop_ok=True,
            )
            arm_state_payload = {
                "app_alive": True,
                "current_pose": [116, 62, 62, 95, 53, 91],
                "estop": {"active": None, "supported": False},
                "last_command_result": {
                    "acks_total": 84,
                    "commands_total": 84,
                    "last_command_sent": "MOVE 0 108",
                    "last_command_sent_at": "2026-04-02T23:40:00Z",
                    "request_id": "objective-97-task-mim-arm-safe-home-1775172258",
                    "task_id": "objective-97-task-mim-arm-safe-home-1775172258",
                    "correlation_id": "obj97-mim-arm-safe-home-1775172258",
                    "lane": "mim_arm_execution",
                },
                "last_error": None,
                "mode": "development",
                "runtime": "sim",
                "serial": {
                    "serial_ready": True,
                    "status": "ok",
                },
                "camera": {
                    "status": "ok",
                },
                "status": "ok",
            }

            with patch.object(generate_mim_arm_host_state, "parse_args", return_value=args), patch.object(
                generate_mim_arm_host_state,
                "_find_first_json",
                return_value=(None, {}),
            ), patch.object(
                generate_mim_arm_host_state,
                "_read_local_json_url",
                return_value=arm_state_payload,
            ), patch.object(
                generate_mim_arm_host_state,
                "run_local",
                side_effect=[
                    {"stdout": "python3 app.py", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                    {"stdout": "/dev/ttyACM0", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                    {"stdout": "/dev/video0", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                ],
            ), patch.object(
                generate_mim_arm_host_state,
                "_uptime_seconds",
                return_value=123.0,
            ), patch.object(
                generate_mim_arm_host_state.socket,
                "gethostname",
                return_value="raspberrypi",
            ):
                exit_code = generate_mim_arm_host_state.main()
                payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["current_pose"], [116, 62, 62, 95, 53, 91])
        self.assertEqual(payload["mode"], "development")
        self.assertTrue(payload["serial_ready"])
        self.assertTrue(payload["camera_online"])
        self.assertTrue(payload["arm_online"])
        self.assertEqual(payload["estop_status"], "sim_clear")
        self.assertEqual(payload["last_command_result"]["commands_total"], 84)
        self.assertEqual(
            payload["command_evidence"]["task_id"],
            "objective-97-task-mim-arm-safe-home-1775172258",
        )
        self.assertEqual(payload["command_evidence"]["request_id"], "objective-97-task-mim-arm-safe-home-1775172258")

    def test_main_lifts_command_attribution_from_source_summary_when_arm_state_lacks_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "mim_arm_host_state.latest.json"
            args = SimpleNamespace(
                shared_root=str(root),
                output=str(output_path),
                process_match="mim_arm|arm_ui|uvicorn|python.*app",
                controller_glob=["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"],
                camera_glob=["/dev/video*"],
                input_json=[],
                arm_url="http://192.168.1.90:5000/arm_state",
                sim_estop_ok=True,
            )
            source_payload = {
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": "objective-97-task-mim-arm-safe-home-20260404194149",
                        "correlation_id": "obj97-mim-arm-safe-home-20260404194149",
                    }
                },
                "request_id": "objective-97-task-mim-arm-safe-home-20260404194149",
                "task_id": "objective-97-task-mim-arm-safe-home-20260404194149",
                "correlation_id": "obj97-mim-arm-safe-home-20260404194149",
                "last_command_result": {
                    "acks_total": 85,
                    "commands_total": 85,
                    "last_command_sent": "GO_SAFE",
                    "last_command_sent_at": "2026-04-05T16:21:57Z",
                },
            }
            arm_state_payload = {
                "app_alive": True,
                "current_pose": [90, 90, 90, 90, 90, 50],
                "estop": {"active": None, "supported": False},
                "last_command_result": {
                    "acks_total": 85,
                    "commands_total": 85,
                    "last_command_sent": "GO_SAFE",
                    "last_command_sent_at": "2026-04-05T16:21:57Z",
                },
                "mode": "development",
                "runtime": "sim",
                "serial": {
                    "serial_ready": True,
                    "status": "ok",
                },
                "camera": {
                    "status": "ok",
                },
                "status": "ok",
            }

            with patch.object(generate_mim_arm_host_state, "parse_args", return_value=args), patch.object(
                generate_mim_arm_host_state,
                "_find_first_json",
                return_value=(root / "TOD_AUTHORITY_SUMMARY.latest.json", source_payload),
            ), patch.object(
                generate_mim_arm_host_state,
                "_read_local_json_url",
                return_value=arm_state_payload,
            ), patch.object(
                generate_mim_arm_host_state,
                "run_local",
                side_effect=[
                    {"stdout": "python3 app.py", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                    {"stdout": "/dev/ttyACM0", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                    {"stdout": "/dev/video0", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                ],
            ), patch.object(
                generate_mim_arm_host_state,
                "_uptime_seconds",
                return_value=123.0,
            ), patch.object(
                generate_mim_arm_host_state.socket,
                "gethostname",
                return_value="raspberrypi",
            ):
                exit_code = generate_mim_arm_host_state.main()
                payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["command_evidence"]["request_id"], "objective-97-task-mim-arm-safe-home-20260404194149")
        self.assertEqual(payload["command_evidence"]["task_id"], "objective-97-task-mim-arm-safe-home-20260404194149")
        self.assertEqual(payload["command_evidence"]["correlation_id"], "obj97-mim-arm-safe-home-20260404194149")
        self.assertEqual(payload["command_evidence"]["attribution_source"], "source_payload")
        self.assertEqual(payload["last_command_result"]["request_id"], "objective-97-task-mim-arm-safe-home-20260404194149")
        self.assertEqual(payload["last_request_id"], "objective-97-task-mim-arm-safe-home-20260404194149")

    def test_main_prefers_fresh_ack_identifiers_over_stale_arm_state_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            output_path = root / "mim_arm_host_state.latest.json"
            args = SimpleNamespace(
                shared_root=str(root),
                output=str(output_path),
                process_match="mim_arm|arm_ui|uvicorn|python.*app",
                controller_glob=["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"],
                camera_glob=["/dev/video*"],
                input_json=[],
                arm_url="http://192.168.1.90:5000/arm_state",
                sim_estop_ok=True,
            )
            source_payload = {
                "request_id": "objective-109-task-mim-arm-scan-pose-fresh",
                "task_id": "objective-109-task-mim-arm-scan-pose-fresh",
                "correlation_id": "obj109-mim-arm-scan-pose-fresh",
                "bridge_runtime": {
                    "current_processing": {
                        "task_id": "objective-109-task-mim-arm-scan-pose-fresh",
                        "correlation_id": "obj109-mim-arm-scan-pose-fresh",
                    }
                },
            }
            arm_state_payload = {
                "app_alive": True,
                "current_pose": [90, 90, 90, 90, 90, 90],
                "estop": {"active": None, "supported": False},
                "last_command_result": {
                    "acks_total": 10445,
                    "commands_total": 10445,
                    "last_command_sent": "MOVE 5 90",
                    "last_command_sent_at": "2026-04-06T18:01:09.679250+00:00",
                    "request_id": "objective-109-task-mim-arm-scan-pose-stale",
                    "task_id": "objective-109-task-mim-arm-scan-pose-stale",
                    "correlation_id": "obj109-mim-arm-scan-pose-stale",
                },
                "mode": "development",
                "runtime": "sim",
                "serial": {
                    "serial_ready": True,
                    "status": "ok",
                    "last_request_id": "objective-109-task-mim-arm-scan-pose-stale",
                    "last_task_id": "objective-109-task-mim-arm-scan-pose-stale",
                    "last_correlation_id": "obj109-mim-arm-scan-pose-stale",
                },
                "camera": {
                    "status": "ok",
                },
                "status": "ok",
            }

            with patch.object(generate_mim_arm_host_state, "parse_args", return_value=args), patch.object(
                generate_mim_arm_host_state,
                "_find_first_json",
                return_value=(root / "TOD_MIM_TASK_ACK.latest.json", source_payload),
            ), patch.object(
                generate_mim_arm_host_state,
                "_read_local_json_url",
                return_value=arm_state_payload,
            ), patch.object(
                generate_mim_arm_host_state,
                "run_local",
                side_effect=[
                    {"stdout": "python3 app.py", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                    {"stdout": "/dev/ttyACM0", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                    {"stdout": "/dev/video0", "ok": True, "returncode": 0, "stderr": "", "duration_ms": 1.0, "command": []},
                ],
            ), patch.object(
                generate_mim_arm_host_state,
                "_uptime_seconds",
                return_value=123.0,
            ), patch.object(
                generate_mim_arm_host_state.socket,
                "gethostname",
                return_value="raspberrypi",
            ):
                exit_code = generate_mim_arm_host_state.main()
                payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["command_evidence"]["request_id"], "objective-109-task-mim-arm-scan-pose-fresh")
        self.assertEqual(payload["command_evidence"]["task_id"], "objective-109-task-mim-arm-scan-pose-fresh")
        self.assertEqual(payload["command_evidence"]["correlation_id"], "obj109-mim-arm-scan-pose-fresh")
        self.assertEqual(payload["command_evidence"]["attribution_source"], "source_payload")
        self.assertEqual(payload["last_request_id"], "objective-109-task-mim-arm-scan-pose-fresh")
