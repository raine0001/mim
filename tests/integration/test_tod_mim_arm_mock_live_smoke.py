from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import request as urllib_request


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _wait_for_http(url: str, *, timeout_seconds: float) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib_request.urlopen(urllib_request.Request(url, method="GET"), timeout=2) as response:
                if int(response.status) < 500:
                    return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {url}: {last_error}")


def _make_mock_arm_handler() -> type[BaseHTTPRequestHandler]:
    state = {
        "current_pose": [116, 62, 62, 95, 53, 91],
        "commands_total": 0,
        "acks_total": 0,
        "last_command_sent": "",
        "last_command_sent_at": "",
        "last_serial_event": "startup",
        "last_request_id": "",
        "last_task_id": "",
        "last_correlation_id": "",
        "last_command_lane": "",
        "last_error": None,
        "motion_stop_requested": False,
        "motion_active": False,
    }

    class MockArmHandler(BaseHTTPRequestHandler):
        def _send_json(self, payload: dict, *, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/arm_state":
                self._send_json(
                    {
                        "app_alive": True,
                        "status": "ok",
                        "runtime": "mock",
                        "mode": "development",
                        "serial_ready": True,
                        "current_pose": list(state["current_pose"]),
                        "last_command_result": {
                            "commands_total": state["commands_total"],
                            "acks_total": state["acks_total"],
                            "last_command_sent": state["last_command_sent"],
                            "last_command_sent_at": state["last_command_sent_at"],
                            "request_id": state["last_request_id"],
                            "task_id": state["last_task_id"],
                            "correlation_id": state["last_correlation_id"],
                            "lane": state["last_command_lane"],
                        },
                        "last_error": state["last_error"],
                        "last_request_id": state["last_request_id"],
                        "last_task_id": state["last_task_id"],
                        "last_correlation_id": state["last_correlation_id"],
                        "last_command_lane": state["last_command_lane"],
                        "last_command_sent_at": state["last_command_sent_at"],
                        "serial": {
                            "last_serial_event": state["last_serial_event"],
                            "last_command_sent": state["last_command_sent"],
                            "serial_command_count": state["commands_total"],
                            "serial_ack_count": state["acks_total"],
                            "request_id": state["last_request_id"],
                            "task_id": state["last_task_id"],
                            "correlation_id": state["last_correlation_id"],
                            "lane": state["last_command_lane"],
                        },
                    }
                )
                return
            self._send_json({"status": "not_found"}, status=404)

        def do_POST(self) -> None:  # noqa: N802
            content_length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(content_length).decode("utf-8") if content_length else "{}"
            try:
                body_payload = json.loads(raw_body or "{}")
            except Exception:
                body_payload = {}
            if not isinstance(body_payload, dict):
                body_payload = {}

            def _record_context() -> None:
                state["last_request_id"] = str(
                    body_payload.get("request_id") or self.headers.get("X-MIM-Request-ID") or ""
                ).strip()
                state["last_task_id"] = str(
                    body_payload.get("task_id") or self.headers.get("X-MIM-Task-ID") or state["last_request_id"] or ""
                ).strip()
                state["last_correlation_id"] = str(
                    body_payload.get("correlation_id") or self.headers.get("X-MIM-Correlation-ID") or ""
                ).strip()
                state["last_command_lane"] = str(
                    body_payload.get("lane") or self.headers.get("X-MIM-Lane") or ""
                ).strip()
                state["last_command_sent_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            if self.path == "/go_safe":
                state["motion_active"] = True
                _record_context()
                if state["motion_stop_requested"]:
                    state["motion_stop_requested"] = False
                    state["motion_active"] = False
                    state["commands_total"] += 1
                    state["acks_total"] += 1
                    state["last_command_sent"] = "STOP"
                    state["last_serial_event"] = "stop_motion_honored"
                    state["last_error"] = None
                    self._send_json(
                        {
                            "status": "stopped",
                            "safe": [90, 90, 90, 90, 90, 50],
                            "current_pose": list(state["current_pose"]),
                            "completed": [],
                        },
                        status=409,
                    )
                    return
                state["current_pose"] = [90, 90, 90, 90, 90, 50]
                state["motion_active"] = False
                state["commands_total"] += 1
                state["acks_total"] += 1
                state["last_command_sent"] = "GO_SAFE"
                state["last_serial_event"] = "go_safe_command_ack"
                state["last_error"] = None
                self._send_json({"status": "ok", "sent": state["last_command_sent"]})
                return
            if self.path == "/stop":
                _record_context()
                state["commands_total"] += 1
                state["last_command_sent"] = "STOP"
                state["last_error"] = None
                if state["motion_active"]:
                    state["motion_stop_requested"] = True
                    state["acks_total"] += 1
                    state["last_serial_event"] = "stop_motion_honored"
                    self._send_json(
                        {
                            "status": "ok",
                            "sent": state["last_command_sent"],
                            "response": "HOST_STOP_CONFIRMED",
                            "ack_source": "mock_transport",
                        }
                    )
                    return
                state["last_serial_event"] = "stop_idle_no_motion"
                self._send_json(
                    {
                        "status": "ok",
                        "sent": state["last_command_sent"],
                        "response": "HOST_STOP_IDLE_NO_MOTION",
                        "ack_source": "idle_state",
                        "motion_active": False,
                    }
                )
                return
            if self.path == "/set_speed":
                _record_context()
                speed = int(body_payload.get("speed", 150))
                state["commands_total"] += 1
                state["last_command_sent"] = f"SET_SPEED {speed}"
                state["last_serial_event"] = "speed_updated"
                state["last_error"] = None
                self._send_json({"status": "ok", "speed": speed, "sent": state["last_command_sent"]})
                return
            if self.path != "/move":
                self._send_json({"status": "not_found"}, status=404)
                return
            _record_context()
            servo = int(body_payload.get("servo", 0))
            angle = int(body_payload.get("angle", 0))
            if 0 <= servo < len(state["current_pose"]):
                state["current_pose"][servo] = angle
            state["commands_total"] += 1
            state["acks_total"] += 1
            state["last_command_sent"] = f"MOVE {servo} {angle}"
            state["last_serial_event"] = "move_command_ack"
            state["last_error"] = None
            self._send_json({"status": "ok", "sent": state["last_command_sent"]})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    return MockArmHandler


class TodMimArmMockLiveSmokeTest(unittest.TestCase):
    def test_live_smoke_uses_mock_arm_host_without_hardware(self) -> None:
        mock_arm_port = _free_port()
        mock_arm_server = ThreadingHTTPServer(("127.0.0.1", mock_arm_port), _make_mock_arm_handler())
        mock_thread = threading.Thread(target=mock_arm_server.serve_forever, daemon=True)
        mock_thread.start()

        mim_port = _free_port()
        env = os.environ.copy()
        env["MIM_ARM_HTTP_BASE_URL"] = f"http://127.0.0.1:{mock_arm_port}"
        env["MIM_ARM_EXECUTION_ENABLE"] = "1"
        mim_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "core.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(mim_port),
                "--log-level",
                "warning",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        try:
            _wait_for_http(f"http://127.0.0.1:{mim_port}/mim/arm/execution-target", timeout_seconds=30)

            with tempfile.TemporaryDirectory() as tmp_dir:
                completed = subprocess.run(
                    [
                        sys.executable,
                        "scripts/run_tod_mim_arm_live_smoke.py",
                        "--base-url",
                        f"http://127.0.0.1:{mim_port}",
                        "--shared-root",
                        str(Path(tmp_dir) / "shared"),
                        "--arm-base-url",
                        f"http://127.0.0.1:{mock_arm_port}",
                    ],
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            if completed.returncode != 0:
                self.fail(f"mock live smoke failed:\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")

            payload = json.loads(completed.stdout)
            self.assertTrue(payload["passed"])
            submission = payload["producer_result"]["response"]["submission"]
            self.assertEqual(submission["ack"]["ack_status"], "accepted")
            self.assertEqual(submission["result"]["result_status"], "succeeded")
            self.assertEqual(
                submission["result"]["output"]["after_state"]["last_command_result"]["request_id"],
                payload["producer_result"]["request"]["request_id"],
            )

            dispatches = submission["result"]["output"]["dispatches"]
            self.assertEqual(len(dispatches), 3)
            for dispatch in dispatches:
                self.assertEqual(dispatch["status_code"], 200)
                self.assertTrue(dispatch["url"].startswith(f"http://127.0.0.1:{mock_arm_port}/move"))

            command_matrix = [
                ("move_home", [], "succeeded"),
                ("move_relative", ["--dx", "5", "--dy", "-10", "--dz", "15"], "succeeded"),
                ("move_relative", ["--dx", "100", "--dy", "-100", "--dz", "200"], "succeeded"),
                (
                    "move_relative_then_set_gripper",
                    ["--dx", "5", "--dy", "-10", "--dz", "15", "--position", "40"],
                    "succeeded",
                ),
                (
                    "pick_and_place",
                    [
                        "--pick-x",
                        "110",
                        "--pick-y",
                        "55",
                        "--pick-z",
                        "45",
                        "--place-x",
                        "130",
                        "--place-y",
                        "60",
                        "--place-z",
                        "50",
                    ],
                    "succeeded",
                ),
                ("pick_at", ["--x", "110", "--y", "55", "--z", "45"], "succeeded"),
                ("place_at", ["--x", "110", "--y", "55", "--z", "45"], "succeeded"),
                ("set_gripper", ["--position", "40"], "succeeded"),
                ("set_speed", ["--level", "fast"], "succeeded"),
                ("stop", [], "succeeded"),
            ]
            for command_name, extra_args, expected_result in command_matrix:
                with self.subTest(command=command_name):
                    command_completed = subprocess.run(
                        [
                            sys.executable,
                            "scripts/submit_tod_mim_arm_execution_request.py",
                            "--base-url",
                            f"http://127.0.0.1:{mim_port}",
                            "--shared-root",
                            str(Path(tempfile.gettempdir()) / f"mim-arm-mock-{command_name}"),
                            "--command",
                            command_name,
                            *extra_args,
                        ],
                        cwd=PROJECT_ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if command_completed.returncode != 0:
                        self.fail(
                            f"producer failed for {command_name}:\nstdout:\n{command_completed.stdout}\nstderr:\n{command_completed.stderr}"
                        )
                    command_payload = json.loads(command_completed.stdout)
                    submission = command_payload["response"]["submission"]
                    self.assertEqual(submission["ack"]["ack_status"], "accepted")
                    self.assertEqual(submission["result"]["result_status"], expected_result)

                    if command_name == "move_home":
                        self.assertEqual(submission["result"]["output"]["dispatches"][0]["route"], "go_safe")
                    if command_name == "move_relative":
                        translation = submission["result"]["output"]["translation"]
                        self.assertEqual(translation["translation_strategy"], "relative_servo_projection")
                        if extra_args == ["--dx", "5", "--dy", "-10", "--dz", "15"]:
                            self.assertEqual(translation["projected_pose"][:3], [95, 80, 105])
                            self.assertFalse(translation["clamp_applied"])
                        else:
                            self.assertEqual(translation["projected_pose"][:3], [180, 15, 180])
                            self.assertTrue(translation["clamp_applied"])
                    if command_name == "move_relative_then_set_gripper":
                        translation = submission["result"]["output"]["translation"]
                        self.assertEqual(translation["translation_strategy"], "relative_servo_projection_then_gripper")
                        self.assertEqual(translation["projected_pose"][:3], [180, 15, 180])
                        self.assertEqual(translation["gripper_step"]["angle"], 80)
                        self.assertEqual([item["servo"] for item in submission["result"]["output"]["dispatches"]], [0, 1, 2, 5])
                    if command_name == "pick_at":
                        translation = submission["result"]["output"]["translation"]
                        self.assertEqual(translation["translation_strategy"], "pick_at_macro")
                        self.assertEqual(submission["result"]["output"]["phase"], "completed")
                        self.assertEqual(
                            submission["result"]["output"]["completed_subactions"],
                            ["move_above_target", "descend_to_target", "close_gripper", "lift_from_target"],
                        )
                        self.assertIsNone(submission["result"]["output"]["failed_subaction"])
                        self.assertIsNone(submission["result"]["output"]["interruption_cause"])
                        self.assertEqual([entry["status"] for entry in submission["result"]["output"]["phase_history"]], ["completed", "completed", "completed", "completed"])
                        self.assertEqual(submission["result"]["output"]["end_effector_state"]["gripper_state"], "closed")
                        self.assertEqual(len(submission["result"]["output"]["dispatches"]), 10)
                    if command_name == "pick_and_place":
                        translation = submission["result"]["output"]["translation"]
                        self.assertEqual(translation["translation_strategy"], "pick_and_place_macro")
                        self.assertEqual(submission["result"]["output"]["phase"], "completed")
                        self.assertEqual(
                            submission["result"]["output"]["completed_subactions"],
                            [
                                "move_above_pick_target",
                                "descend_to_pick_target",
                                "close_gripper",
                                "lift_from_pick_target",
                                "move_above_place_target",
                                "descend_to_place_target",
                                "open_gripper",
                                "lift_from_place_target",
                            ],
                        )
                        self.assertIsNone(submission["result"]["output"]["failed_subaction"])
                        self.assertIsNone(submission["result"]["output"]["interruption_cause"])
                        self.assertEqual([entry["status"] for entry in submission["result"]["output"]["phase_history"]], ["completed"] * 8)
                        self.assertEqual(submission["result"]["output"]["end_effector_state"]["gripper_state"], "open")
                        self.assertEqual(len(submission["result"]["output"]["dispatches"]), 20)
                    if command_name == "place_at":
                        translation = submission["result"]["output"]["translation"]
                        self.assertEqual(translation["translation_strategy"], "place_at_macro")
                        self.assertEqual(submission["result"]["output"]["phase"], "completed")
                        self.assertEqual(
                            submission["result"]["output"]["completed_subactions"],
                            ["move_above_target", "descend_to_target", "open_gripper", "retract_or_lift"],
                        )
                        self.assertIsNone(submission["result"]["output"]["failed_subaction"])
                        self.assertIsNone(submission["result"]["output"]["interruption_cause"])
                        self.assertEqual([entry["status"] for entry in submission["result"]["output"]["phase_history"]], ["completed", "completed", "completed", "completed"])
                        self.assertEqual(submission["result"]["output"]["end_effector_state"]["gripper_state"], "open")
                        self.assertEqual(len(submission["result"]["output"]["dispatches"]), 10)
                    if command_name == "set_gripper":
                        self.assertEqual(submission["result"]["output"]["translation"]["steps"][0]["angle"], 80)
                    if command_name == "set_speed":
                        self.assertEqual(submission["result"]["output"]["dispatches"][0]["route"], "set_speed")
                    if command_name == "stop":
                        self.assertEqual(submission["result"]["output"]["dispatches"][0]["route"], "stop")
                        self.assertEqual(
                            submission["result"]["output"]["dispatches"][0]["payload"]["response"],
                            "HOST_STOP_IDLE_NO_MOTION",
                        )
                        self.assertEqual(
                            submission["result"]["output"]["after_state"]["serial"]["last_serial_event"],
                            "stop_idle_no_motion",
                        )
        finally:
            mock_arm_server.shutdown()
            mock_arm_server.server_close()
            mim_process.terminate()
            try:
                mim_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mim_process.kill()