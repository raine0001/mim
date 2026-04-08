from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TodMimContractTest(unittest.TestCase):
    def test_normalize_request_adds_frozen_contract_fields(self) -> None:
        from core.tod_mim_contract import normalize_message

        payload = normalize_message(
            {
                "objective_id": "objective-97",
                "request_id": "objective-97-request-001",
                "correlation_id": "objective-97-task-001",
                "command": {"name": "safe_home", "args": {}},
            },
            message_kind="request",
            service_name="unit_test",
            instance_id="unit_test:1",
        )

        self.assertEqual(payload["packet_type"], "mim-tod-task-request-v1")
        self.assertEqual(payload["message_kind"], "request")
        self.assertEqual(payload["contract_version"], "v1")
        self.assertEqual(payload["schema_version"], "2026-04-02-communication-contract-v1")
        self.assertEqual(payload["target_executor"], "TOD")
        self.assertEqual(payload["command"], {"name": "safe_home", "args": {}})
        self.assertEqual(payload["task_id"], "objective-97-request-001")
        self.assertIn("source_identity", payload)
        self.assertIn("transport", payload)
        self.assertIn("execution_policy", payload)
        self.assertIn("idempotency", payload)
        self.assertIn("fallback_policy", payload)

    def test_normalize_request_preserves_explicit_registry_task_id_when_present(self) -> None:
        from core.tod_mim_contract import normalize_message

        payload = normalize_message(
            {
                "objective_id": "objective-97",
                "request_id": "objective-97-request-002",
                "task_id": "registry-task-77",
                "correlation_id": "objective-97-task-002",
                "command": {"name": "safe_home", "args": {}},
            },
            message_kind="request",
            service_name="unit_test",
            instance_id="unit_test:2",
        )

        self.assertEqual(payload["request_id"], "objective-97-request-002")
        self.assertEqual(payload["task_id"], "registry-task-77")

    def test_validate_request_reports_missing_fields(self) -> None:
        from core.tod_mim_contract import validate_message

        errors = validate_message({"message_kind": "request"}, "request")
        self.assertIn("missing_required_field:packet_type", errors)
        self.assertIn("missing_required_field:command", errors)
        self.assertIn("contract_version_mismatch", errors)

    def test_contract_transmission_payload_uses_frozen_signature(self) -> None:
        from core.tod_mim_contract import build_contract_transmission_payload, ensure_contract_signature

        signature = ensure_contract_signature()
        payload = build_contract_transmission_payload(service_name="unit_test", instance_id="unit_test:tx")

        self.assertEqual(payload["packet_type"], "tod-mim-contract-distribution-v1")
        self.assertEqual(payload["contract_id"], "TOD_MIM_COMMUNICATION_CONTRACT.v1")
        self.assertEqual(payload["contract_version"], "v1")
        self.assertEqual(payload["checksum_sha256"], signature["sha256"])
        self.assertEqual(payload["signature"]["sha256"], signature["sha256"])
        self.assertIn("payload", payload)

    def test_validate_contract_receipt_requires_exact_contract_identity(self) -> None:
        from core.tod_mim_contract import validate_message

        errors = validate_message(
            {
                "packet_type": "tod-mim-contract-receipt-v1",
                "contract_id": "wrong-contract",
                "contract_version": "v0",
                "generated_at": "2026-04-02T00:00:00Z",
                "checksum_sha256": "abc",
                "checksum_match": "yes",
                "version_accepted": "yes",
            },
            "contract_receipt",
        )

        self.assertIn("contract_id_mismatch", errors)
        self.assertIn("contract_version_mismatch", errors)
        self.assertIn("invalid_checksum_match", errors)
        self.assertIn("invalid_version_accepted", errors)

    def test_receipt_status_reports_accepted_for_matching_receipt(self) -> None:
        from core import tod_mim_contract

        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            signature_path = temp_root / "contract.signature.json"
            receipt_path = temp_root / "contract.receipt.json"
            activation_report_path = temp_root / "contract.activation.report.json"
            signature_payload = {
                "contract_id": tod_mim_contract.CONTRACT_ID,
                "version": tod_mim_contract.CONTRACT_VERSION,
                "schema_version": tod_mim_contract.CONTRACT_SCHEMA_VERSION,
                "sha256": "deadbeef",
                "timestamp": "2026-04-02T00:00:00Z",
                "source": tod_mim_contract.CONTRACT_SOURCE,
            }
            receipt_payload = {
                "packet_type": "tod-mim-contract-receipt-v1",
                "contract_id": tod_mim_contract.CONTRACT_ID,
                "contract_version": tod_mim_contract.CONTRACT_VERSION,
                "generated_at": "2026-04-02T00:01:00Z",
                "checksum_sha256": "deadbeef",
                "checksum_match": True,
                "version_accepted": True,
            }
            signature_path.write_text(json.dumps(signature_payload), encoding="utf-8")
            receipt_path.write_text(json.dumps(receipt_payload), encoding="utf-8")

            with mock.patch.object(tod_mim_contract, "CONTRACT_SIGNATURE_PATH", signature_path), mock.patch.object(
                tod_mim_contract,
                "CONTRACT_RECEIPT_PATH",
                receipt_path,
            ), mock.patch.object(
                tod_mim_contract,
                "CONTRACT_ACTIVATION_REPORT_PATH",
                activation_report_path,
            ):
                status = tod_mim_contract.receipt_status()
                report = tod_mim_contract.build_activation_report()

        self.assertEqual(status["status"], "accepted")
        self.assertTrue(status["checksum_match"])
        self.assertTrue(status["version_accepted"])
        self.assertEqual(report["shadow_mode"]["status"], "tod_exact_match_confirmed")
        self.assertTrue(report["shadow_mode"]["comparison_ready"])
        self.assertEqual(report["schema_enforcement"]["ack_result_runtime_binding"], "ready_for_tod_runtime_binding")


if __name__ == "__main__":
    unittest.main()