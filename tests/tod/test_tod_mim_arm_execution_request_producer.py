from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TodMimArmExecutionRequestProducerTest(unittest.TestCase):
    def test_build_and_post_request_keeps_contract_envelope(self) -> None:
        from scripts import submit_tod_mim_arm_execution_request as producer

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            producer,
            "_post_json",
            return_value=(200, {"submission": {"result": {"result_status": "succeeded"}}}),
        ):
            payload = producer.build_tod_execution_request(
                request_id="tod-mim-arm-test-001",
                command_name="move_to",
                command_args={"x": 10, "y": 20, "z": 30},
                expires_at="2026-04-01T01:00:00Z",
                metadata_json={"producer": "tod"},
            )
            request_path = producer.tod_request_path(Path(tmp_dir))
            request_path.parent.mkdir(parents=True, exist_ok=True)
            request_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            self.assertEqual(payload["target"], "mim_arm")
            self.assertEqual(payload["command"]["name"], "move_to")
            self.assertEqual(payload["command"]["args"], {"x": 10, "y": 20, "z": 30})
            self.assertTrue(request_path.exists())

    def test_new_command_shapes_fit_same_request_envelope(self) -> None:
        from scripts import submit_tod_mim_arm_execution_request as producer

        move_home = producer.build_tod_execution_request(
            request_id="tod-mim-arm-home-001",
            command_name="move_home",
            command_args={},
            expires_at="2026-04-01T01:00:00Z",
            metadata_json={"producer": "tod"},
        )
        set_gripper = producer.build_tod_execution_request(
            request_id="tod-mim-arm-gripper-001",
            command_name="set_gripper",
            command_args={"position": 40},
            expires_at="2026-04-01T01:00:00Z",
            metadata_json={"producer": "tod"},
        )
        set_speed = producer.build_tod_execution_request(
            request_id="tod-mim-arm-speed-001",
            command_name="set_speed",
            command_args={"level": "slow"},
            expires_at="2026-04-01T01:00:00Z",
            metadata_json={"producer": "tod"},
        )
        move_relative = producer.build_tod_execution_request(
            request_id="tod-mim-arm-relative-001",
            command_name="move_relative",
            command_args={"dx": 5, "dy": -10, "dz": 15},
            expires_at="2026-04-01T01:00:00Z",
            metadata_json={"producer": "tod"},
        )
        compound = producer.build_tod_execution_request(
            request_id="tod-mim-arm-compound-001",
            command_name="move_relative_then_set_gripper",
            command_args={"dx": 5, "dy": -10, "dz": 15, "position": 40},
            expires_at="2026-04-01T01:00:00Z",
            metadata_json={"producer": "tod"},
        )
        pick_at = producer.build_tod_execution_request(
            request_id="tod-mim-arm-pick-001",
            command_name="pick_at",
            command_args={"x": 110, "y": 55, "z": 45},
            expires_at="2026-04-01T01:00:00Z",
            metadata_json={"producer": "tod"},
        )
        pick_and_place = producer.build_tod_execution_request(
            request_id="tod-mim-arm-pick-and-place-001",
            command_name="pick_and_place",
            command_args={"pick_x": 110, "pick_y": 55, "pick_z": 45, "place_x": 130, "place_y": 60, "place_z": 50},
            expires_at="2026-04-01T01:00:00Z",
            metadata_json={"producer": "tod"},
        )
        place_at = producer.build_tod_execution_request(
            request_id="tod-mim-arm-place-001",
            command_name="place_at",
            command_args={"x": 110, "y": 55, "z": 45},
            expires_at="2026-04-01T01:00:00Z",
            metadata_json={"producer": "tod"},
        )

        self.assertEqual(move_home["command"], {"name": "move_home", "args": {}})
        self.assertEqual(move_relative["command"], {"name": "move_relative", "args": {"dx": 5, "dy": -10, "dz": 15}})
        self.assertEqual(
            compound["command"],
            {"name": "move_relative_then_set_gripper", "args": {"dx": 5, "dy": -10, "dz": 15, "position": 40}},
        )
        self.assertEqual(pick_at["command"], {"name": "pick_at", "args": {"x": 110, "y": 55, "z": 45}})
        self.assertEqual(
            pick_and_place["command"],
            {"name": "pick_and_place", "args": {"pick_x": 110, "pick_y": 55, "pick_z": 45, "place_x": 130, "place_y": 60, "place_z": 50}},
        )
        self.assertEqual(place_at["command"], {"name": "place_at", "args": {"x": 110, "y": 55, "z": 45}})
        self.assertEqual(set_gripper["command"], {"name": "set_gripper", "args": {"position": 40}})
        self.assertEqual(set_speed["command"], {"name": "set_speed", "args": {"level": "slow"}})

    def test_main_only_persists_local_request_after_acceptance(self) -> None:
        from scripts import submit_tod_mim_arm_execution_request as producer

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            producer,
            "_post_json",
            return_value=(200, {"submission": {"ack": {"ack_status": "accepted"}}}),
        ), patch.object(
            sys,
            "argv",
            [
                "submit_tod_mim_arm_execution_request.py",
                "--shared-root",
                tmp_dir,
                "--command",
                "move_home",
            ],
        ):
            exit_code = producer.main()
            self.assertEqual(exit_code, 0)
            self.assertTrue(producer.tod_request_path(Path(tmp_dir)).exists())

    def test_main_skips_local_request_when_submission_not_accepted(self) -> None:
        from scripts import submit_tod_mim_arm_execution_request as producer

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            producer,
            "_post_json",
            return_value=(200, {"submission": {"ack": {"ack_status": "rejected"}}}),
        ), patch.object(
            sys,
            "argv",
            [
                "submit_tod_mim_arm_execution_request.py",
                "--shared-root",
                tmp_dir,
                "--command",
                "move_home",
            ],
        ):
            exit_code = producer.main()
            self.assertEqual(exit_code, 1)
            self.assertFalse(producer.tod_request_path(Path(tmp_dir)).exists())