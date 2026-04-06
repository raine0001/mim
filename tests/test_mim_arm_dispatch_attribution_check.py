from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_mim_arm_dispatch_attribution_check.py"
SPEC = importlib.util.spec_from_file_location("run_mim_arm_dispatch_attribution_check", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class DispatchIdentifierResolutionTests(unittest.TestCase):
    def test_action_slug_converts_underscores_to_hyphens(self) -> None:
        self.assertEqual(MODULE._action_slug("scan_pose"), "scan-pose")

    def test_action_slug_defaults_when_empty(self) -> None:
        self.assertEqual(MODULE._action_slug(""), "safe-home")

    def test_prefers_bridge_task_id_when_present(self) -> None:
        id_kind, identifier = MODULE._resolve_dispatch_identifier(
            {
                "task_id": "objective-107-task-001",
                "request_id": "objective-107-request-001",
            }
        )

        self.assertEqual(id_kind, "bridge_task_id")
        self.assertEqual(identifier, "objective-107-task-001")

    def test_falls_back_to_bridge_request_id(self) -> None:
        id_kind, identifier = MODULE._resolve_dispatch_identifier(
            {
                "task_id": "",
                "request_id": "objective-107-request-001",
            }
        )

        self.assertEqual(id_kind, "bridge_request_id")
        self.assertEqual(identifier, "objective-107-request-001")

    def test_returns_empty_values_when_no_identifier_present(self) -> None:
        id_kind, identifier = MODULE._resolve_dispatch_identifier({})

        self.assertEqual(id_kind, "")
        self.assertEqual(identifier, "")


class PublicationBoundaryTests(unittest.TestCase):
    def test_boundary_matches_when_remote_request_and_trigger_align(self) -> None:
        payload = {
            "remote_request": {"request_id": "objective-107-request-001"},
            "remote_trigger": {"request_id": "objective-107-request-001"},
            "request_alignment": {"request_id_match": True},
            "trigger_alignment": {"request_id_match": True},
        }

        self.assertTrue(
            MODULE._boundary_matches_dispatch_identifier(payload, "objective-107-request-001")
        )

    def test_boundary_does_not_match_when_remote_trigger_differs(self) -> None:
        payload = {
            "remote_request": {"request_id": "objective-107-request-001"},
            "remote_trigger": {"request_id": "objective-107-request-002"},
            "request_alignment": {"request_id_match": True},
            "trigger_alignment": {"request_id_match": False},
        }

        self.assertFalse(
            MODULE._boundary_matches_dispatch_identifier(payload, "objective-107-request-001")
        )


class ResponseArtifactMatchTests(unittest.TestCase):
    def test_response_artifact_match_requires_direct_request_fields(self) -> None:
        payload = {
            "request_id": "objective-107-request-001",
            "bridge_runtime": {
                "current_processing": {
                    "task_id": "objective-107-request-001",
                }
            },
        }

        match = MODULE._response_artifact_match(payload, "objective-107-request-001")

        self.assertTrue(match["matched"])
        self.assertIn("request_id", match["matched_fields"])
        self.assertIn("bridge_runtime.current_processing.task_id", match["matched_fields"])

    def test_response_artifact_match_ignores_reconciliation_mentions(self) -> None:
        payload = {
            "request_id": "objective-107-request-000",
            "reconciliation": {
                "active_task_id": "objective-107-request-001",
            },
        }

        match = MODULE._response_artifact_match(payload, "objective-107-request-001")

        self.assertFalse(match["matched"])
        self.assertEqual(match["matched_fields"], [])


class TodResponsePollingTests(unittest.TestCase):
    def test_poll_local_tod_responses_waits_for_both_ack_and_result(self) -> None:
        ack_payload = {"request_id": "objective-109-task-001"}
        result_payload = {"request_id": "objective-109-task-001"}

        with patch.object(
            MODULE,
            "_read_local_json",
            side_effect=[
                {},
                result_payload,
                ack_payload,
                result_payload,
            ],
        ) as mock_read, patch.object(
            MODULE.time,
            "monotonic",
            side_effect=[0.0, 0.0, 1.0],
        ), patch.object(MODULE.time, "sleep", return_value=None):
            responses = MODULE._poll_local_tod_responses(
                task_id="objective-109-task-001",
                task_ack_path="/tmp/TOD_MIM_TASK_ACK.latest.json",
                task_result_path="/tmp/TOD_MIM_TASK_RESULT.latest.json",
                timeout_seconds=10,
                interval_seconds=0.1,
            )

        self.assertEqual(mock_read.call_count, 4)
        self.assertTrue(responses["task_ack_matches"])
        self.assertTrue(responses["task_result_matches"])
        self.assertEqual(responses["task_ack"]["request_id"], "objective-109-task-001")
        self.assertEqual(responses["task_result"]["request_id"], "objective-109-task-001")


class HostPayloadMergeTests(unittest.TestCase):
    def test_merge_prefers_host_state_attribution_fields(self) -> None:
        merged = MODULE._merge_host_payload(
            {
                "last_command_result": {
                    "commands_total": 5,
                    "acks_total": 5,
                    "last_command_sent": "MOVE 5 90",
                },
                "last_request_id": None,
                "last_task_id": None,
                "last_correlation_id": None,
            },
            {
                "command_evidence": {
                    "request_id": "objective-107-request-001",
                    "task_id": "objective-107-request-001",
                    "correlation_id": "obj107-request-001",
                },
                "last_request_id": "objective-107-request-001",
                "last_task_id": "objective-107-request-001",
                "last_correlation_id": "obj107-request-001",
                "last_command_result": {
                    "request_id": "objective-107-request-001",
                    "task_id": "objective-107-request-001",
                    "correlation_id": "obj107-request-001",
                },
            },
        )

        evidence = MODULE._extract_command_evidence(merged)

        self.assertEqual(evidence["request_id"], "objective-107-request-001")
        self.assertEqual(evidence["task_id"], "objective-107-request-001")
        self.assertEqual(evidence["correlation_id"], "obj107-request-001")


class RemoteCommandStatusClassificationTests(unittest.TestCase):
    def test_classifies_readiness_preflight_surface(self) -> None:
        classification = MODULE._classify_remote_command_status(
            {
                "source": "tod-mim-command-status-v1",
                "execution_readiness": {"status": "valid"},
                "metadata_json": {"refreshed_by": "refresh_execution_readiness.py"},
            },
            fresh_dispatch_identifier_matches=[],
            authoritative_fresh_dispatch_identifier_matches=[],
        )

        self.assertEqual(classification["surface_kind"], "readiness_preflight")
        self.assertFalse(classification["dispatch_identifier_expected_on_surface"])
        self.assertFalse(classification["supports_dispatch_consumption_proof"])
        self.assertFalse(classification["fresh_dispatch_identifier_visible"])
        self.assertFalse(classification["fresh_dispatch_identifier_consumed_on_surface"])

    def test_unknown_surface_defaults_to_dispatch_capable(self) -> None:
        classification = MODULE._classify_remote_command_status(
            {"source": "custom-surface"},
            fresh_dispatch_identifier_matches=["$.request_id"],
            authoritative_fresh_dispatch_identifier_matches=["$.request_id"],
        )

        self.assertEqual(classification["surface_kind"], "unknown")
        self.assertTrue(classification["dispatch_identifier_expected_on_surface"])
        self.assertTrue(classification["supports_dispatch_consumption_proof"])
        self.assertTrue(classification["fresh_dispatch_identifier_visible"])
        self.assertTrue(classification["fresh_dispatch_identifier_consumed_on_surface"])


if __name__ == "__main__":
    unittest.main()