import json
import os
import unittest
import urllib.error
import urllib.request
from uuid import uuid4


from tests.integration.runtime_target_guard import DEFAULT_BASE_URL


BASE_URL = os.getenv("MIM_TEST_BASE_URL", DEFAULT_BASE_URL)


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
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


def get_json(path: str) -> tuple[int, dict | list]:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        parsed = json.loads(body) if body else {}
        return exc.code, parsed


class Objective42MultiCapabilityCoordinationTest(unittest.TestCase):
    def test_objective42_safe_chain_policy_dependencies_verification_and_escalation(self) -> None:
        run_id = uuid4().hex[:8]
        zone = f"front-center-obj42-{run_id}"
        label = f"obj42-target-{run_id}"

        # Safe chain example: workspace_scan -> observation_update
        status, created = post_json(
            "/workspace/capability-chains",
            {
                "actor": "objective42-test",
                "reason": "safe scan+memory chain",
                "chain_name": f"obj42-safe-{run_id}",
                "chain_type": "safe_capability_chain",
                "steps": [
                    {
                        "step_id": "scan",
                        "capability": "workspace_scan",
                        "depends_on": [],
                        "params": {"zone": zone, "label": label, "confidence": 0.94},
                        "verify": {"require": "observation_count>0"},
                    },
                    {
                        "step_id": "memory",
                        "capability": "observation_update",
                        "depends_on": ["scan"],
                        "params": {"zone": zone, "label": label},
                        "verify": {"require": "observation_exists"},
                    },
                ],
                "policy_json": {"objective": "objective42"},
                "stop_on_failure": True,
                "escalate_on_failure": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, created)
        chain_id = int(created.get("chain_id", 0))
        self.assertGreater(chain_id, 0)

        status, advanced_1 = post_json(
            f"/workspace/capability-chains/{chain_id}/advance",
            {
                "actor": "objective42-test",
                "reason": "execute step 1",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, advanced_1)
        self.assertEqual(advanced_1.get("last_step", {}).get("step_id"), "scan")
        self.assertTrue(bool(advanced_1.get("last_step", {}).get("verification", {}).get("observation_id")))

        status, advanced_2 = post_json(
            f"/workspace/capability-chains/{chain_id}/advance",
            {
                "actor": "objective42-test",
                "reason": "execute step 2",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, advanced_2)
        self.assertEqual(advanced_2.get("status"), "completed")
        self.assertEqual(advanced_2.get("last_step", {}).get("step_id"), "memory")
        self.assertGreaterEqual(int(advanced_2.get("last_step", {}).get("verification", {}).get("observation_count", 0)), 1)

        status, audit = get_json(f"/workspace/capability-chains/{chain_id}/audit")
        self.assertEqual(status, 200, audit)
        events = [item.get("event") for item in audit.get("audit_trail", []) if isinstance(item, dict)]
        self.assertIn("capability_chain_created", events)
        self.assertIn("capability_step_completed", events)

        # Dependency validation at creation
        status, dep_invalid = post_json(
            "/workspace/capability-chains",
            {
                "actor": "objective42-test",
                "reason": "invalid dependency",
                "chain_name": f"obj42-dep-invalid-{run_id}",
                "steps": [
                    {
                        "step_id": "s1",
                        "capability": "workspace_scan",
                        "depends_on": [],
                        "params": {"zone": zone, "label": f"dep-{run_id}"},
                    },
                    {
                        "step_id": "s2",
                        "capability": "observation_update",
                        "depends_on": ["missing-step"],
                        "params": {"zone": zone},
                    },
                ],
            },
        )
        self.assertEqual(status, 422, dep_invalid)

        # stop-on-failure / escalate behavior on safe combo: rescan_zone -> proposal_resolution
        status, failure_chain = post_json(
            "/workspace/capability-chains",
            {
                "actor": "objective42-test",
                "reason": "escalation path",
                "chain_name": f"obj42-fail-{run_id}",
                "steps": [
                    {
                        "step_id": "rescan",
                        "capability": "rescan_zone",
                        "depends_on": [],
                        "params": {"zone": zone, "confidence": 0.82},
                    },
                    {
                        "step_id": "resolve",
                        "capability": "proposal_resolution",
                        "depends_on": ["rescan"],
                        "params": {"proposal_id": -1},
                    },
                ],
                "stop_on_failure": True,
                "escalate_on_failure": True,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, failure_chain)
        fail_chain_id = int(failure_chain.get("chain_id", 0))
        self.assertGreater(fail_chain_id, 0)

        status, fail_step1 = post_json(
            f"/workspace/capability-chains/{fail_chain_id}/advance",
            {
                "actor": "objective42-test",
                "reason": "failure chain step1",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, fail_step1)
        self.assertEqual(fail_step1.get("last_step", {}).get("step_id"), "rescan")

        status, fail_step2 = post_json(
            f"/workspace/capability-chains/{fail_chain_id}/advance",
            {
                "actor": "objective42-test",
                "reason": "failure chain step2",
                "force": False,
                "metadata_json": {"run_id": run_id},
            },
        )
        self.assertEqual(status, 200, fail_step2)
        self.assertEqual(fail_step2.get("status"), "failed")
        self.assertFalse(bool(fail_step2.get("last_step", {}).get("success", True)))
        escalation = (fail_step2.get("metadata_json", {}) or {}).get("escalation", {})
        self.assertTrue(bool(escalation.get("required", False)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
