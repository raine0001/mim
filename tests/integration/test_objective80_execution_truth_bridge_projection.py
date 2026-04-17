import importlib.util
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
import unittest
from pathlib import Path

from tests.integration.runtime_target_guard import DEFAULT_BASE_URL, probe_current_source_runtime


ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCRIPT = ROOT / "scripts" / "export_mim_context.py"
ALIAS_SYNC_SCRIPT = ROOT / "scripts" / "check_tod_execution_truth_alias_sync.sh"
VALIDATE_SCRIPT = ROOT / "scripts" / "validate_tod_execution_truth_bridge.sh"
BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)


def load_export_module():
    spec = importlib.util.spec_from_file_location("export_mim_context", EXPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def post_json(path: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        return exc.code, json.loads(body) if body else {}


def get_json(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class Objective80ExecutionTruthBridgeProjectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        probe_current_source_runtime(
            suite_name="Objective 80 bridge projection",
            base_url=BASE_URL,
            require_execution_truth_projection=True,
        )

    def test_latest_execution_truth_projection_endpoint(self) -> None:
        capability_name = "execution_truth_bridge_probe"
        status, _ = post_json(
            "/gateway/capabilities",
            {
                "capability_name": capability_name,
                "category": "diagnostic",
                "description": "Objective 80.2 bridge probe",
                "requires_confirmation": False,
                "enabled": True,
            },
        )
        self.assertEqual(status, 200)

        status, event = post_json(
            "/gateway/intake/text",
            {
                "text": "run execution truth bridge probe",
                "parsed_intent": "workspace_check",
                "requested_goal": "project execution truth",
                "metadata_json": {"capability": capability_name},
            },
        )
        self.assertEqual(status, 200, event)
        execution_id = int(
            (
                event.get("execution", {})
                if isinstance(event.get("execution", {}), dict)
                else {}
            ).get("execution_id", 0)
        )
        self.assertGreater(execution_id, 0)

        for payload in [
            {
                "status": "accepted",
                "reason": "accepted",
                "actor": "tod",
                "feedback_json": {},
            },
            {
                "status": "running",
                "reason": "running",
                "actor": "tod",
                "feedback_json": {},
            },
            {
                "status": "succeeded",
                "reason": "bridge-ready execution truth",
                "actor": "tod",
                "runtime_outcome": "recovered",
                "feedback_json": {},
                "execution_truth": {
                    "execution_id": execution_id,
                    "capability_name": capability_name,
                    "expected_duration_ms": 900,
                    "actual_duration_ms": 1350,
                    "retry_count": 1,
                    "fallback_used": True,
                    "runtime_outcome": "recovered",
                    "environment_shift_detected": False,
                    "simulation_match_status": "partial_match",
                    "truth_confidence": 0.88,
                    "published_at": "2026-03-23T22:40:00Z",
                },
            },
        ]:
            status, result = post_json(
                f"/gateway/capabilities/executions/{execution_id}/feedback",
                payload,
            )
            self.assertEqual(status, 200, result)

        status, projection = get_json(
            "/gateway/capabilities/executions/truth/latest?limit=5"
        )
        self.assertEqual(status, 200, projection)
        self.assertEqual(projection.get("packet_type"), "tod-execution-truth-bridge-v1")
        self.assertEqual(projection.get("contract"), "execution_truth_v1")
        summary = (
            projection.get("summary", {})
            if isinstance(projection.get("summary", {}), dict)
            else {}
        )
        self.assertGreaterEqual(int(summary.get("execution_count", 0) or 0), 1)
        self.assertGreaterEqual(int(summary.get("deviation_signal_count", 0) or 0), 1)
        recent = (
            projection.get("recent_execution_truth", [])
            if isinstance(projection.get("recent_execution_truth", []), list)
            else []
        )
        target = next(
            (
                item
                for item in recent
                if isinstance(item, dict)
                and int(item.get("execution_id", 0) or 0) == execution_id
            ),
            None,
        )
        self.assertIsNotNone(target, recent)
        self.assertEqual(
            (target or {}).get("execution_truth", {}).get("contract"),
            "execution_truth_v1",
        )

    def test_export_writes_execution_truth_bridge_artifacts_and_validation_passes(
        self,
    ) -> None:
        export_module = load_export_module()
        payload = {
            "exported_at": "2026-03-23T22:45:00Z",
            "objective_active": "80",
            "latest_completed_objective": "75",
            "current_next_objective": "80",
            "schema_version": "2026-03-12-68",
            "release_tag": "objective-80",
            "blockers": [],
            "verification": {},
            "source_of_truth": {
                "manifest_source_used": "core/manifest.py",
                "manifest_base_source_used": f"{BASE_URL}/manifest",
            },
        }
        manifest = {
            "schema_version": "2026-03-12-68",
            "release_tag": "objective-80",
            "contract_version": "tod-mim-shared-contract-v1",
        }
        projection = {
            "generated_at": "2026-03-23T22:45:01Z",
            "packet_type": "tod-execution-truth-bridge-v1",
            "contract": "execution_truth_v1",
            "source": "gateway.capability_execution_feedback",
            "summary": {
                "execution_count": 1,
                "capabilities": ["execution_truth_bridge_probe"],
                "deviation_signal_count": 2,
                "deviation_signals": [],
                "recent_executions": [
                    {
                        "execution_id": 41,
                        "capability_name": "execution_truth_bridge_probe",
                        "runtime_outcome": "recovered",
                        "truth_confidence": 0.88,
                        "published_at": "2026-03-23T22:40:00Z",
                        "signal_types": ["fallback_path_used"],
                    }
                ],
            },
            "recent_execution_truth": [
                {
                    "execution_id": 41,
                    "capability_name": "execution_truth_bridge_probe",
                    "status": "succeeded",
                    "reason": "bridge-ready execution truth",
                    "execution_truth": {
                        "contract": "execution_truth_v1",
                        "execution_id": 41,
                        "capability_name": "execution_truth_bridge_probe",
                        "expected_duration_ms": 900,
                        "actual_duration_ms": 1350,
                        "duration_delta_ratio": 0.5,
                        "retry_count": 1,
                        "fallback_used": True,
                        "runtime_outcome": "recovered",
                        "environment_shift_detected": False,
                        "simulation_match_status": "partial_match",
                        "truth_confidence": 0.88,
                        "published_at": "2026-03-23T22:40:00Z",
                    },
                    "deviation_signals": [],
                }
            ],
        }

        def fake_fetch(url: str, timeout: float = 2.5):
            if (
                url
                == f"{BASE_URL}/gateway/capabilities/executions/truth/latest?limit=10"
            ):
                return projection
            return None

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir)
            original_fetch = export_module._fetch_json
            export_module._fetch_json = fake_fetch
            try:
                export_module.write_exports(
                    payload, manifest, output_dir, mirror_root=False
                )
            finally:
                export_module._fetch_json = original_fetch

            canonical_payload = json.loads(
                (output_dir / "TOD_EXECUTION_TRUTH.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            alias_payload = json.loads(
                (output_dir / "TOD_execution_truth.latest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                canonical_payload.get("packet_type"), "tod-execution-truth-bridge-v1"
            )
            self.assertEqual(
                alias_payload.get("packet_type"), canonical_payload.get("packet_type")
            )
            self.assertEqual(
                canonical_payload.get("bridge_publication", {}).get("canonical_file"),
                "TOD_EXECUTION_TRUTH.latest.json",
            )
            self.assertEqual(
                canonical_payload.get("bridge_publication", {}).get(
                    "legacy_alias_file"
                ),
                "TOD_execution_truth.latest.json",
            )

            alias_check = subprocess.run(
                [
                    "bash",
                    str(ALIAS_SYNC_SCRIPT),
                    str(output_dir / "TOD_EXECUTION_TRUTH.latest.json"),
                    str(output_dir / "TOD_execution_truth.latest.json"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                alias_check.returncode, 0, alias_check.stdout + alias_check.stderr
            )
            self.assertIn("EXECUTION_TRUTH_ALIAS_SYNC: PASS", alias_check.stdout)

            validate = subprocess.run(
                [
                    "bash",
                    str(VALIDATE_SCRIPT),
                    str(output_dir / "TOD_EXECUTION_TRUTH.latest.json"),
                ],
                cwd=ROOT,
                env={**os.environ, "MAX_AGE_SECONDS": "99999999"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)
            self.assertIn("EXECUTION_TRUTH_GATE: PASS", validate.stdout)

    def test_validate_execution_truth_bridge_rejects_stale_projection(self) -> None:
        stale_payload = {
            "generated_at": "2020-01-01T00:00:00Z",
            "packet_type": "tod-execution-truth-bridge-v1",
            "contract": "execution_truth_v1",
            "summary": {"execution_count": 0, "deviation_signal_count": 0},
            "recent_execution_truth": [],
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            truth_path = Path(tmp_dir) / "TOD_EXECUTION_TRUTH.latest.json"
            truth_path.write_text(
                json.dumps(stale_payload, indent=2) + "\n", encoding="utf-8"
            )
            result = subprocess.run(
                ["bash", str(VALIDATE_SCRIPT), str(truth_path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("EXECUTION_TRUTH_GATE: FAIL", result.stdout)
            self.assertIn("generated_at fresh", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
