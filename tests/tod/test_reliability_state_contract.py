import json
import os
import subprocess
import unittest
from pathlib import Path


class ReliabilityStateContractTest(unittest.TestCase):
    def test_reliability_state_envelope_shape(self):
        repo_root = Path(__file__).resolve().parents[2]
        tod_dir = repo_root / "tod"

        command = [
            "pwsh",
            "-NoProfile",
            "-File",
            str(tod_dir / "TOD.ps1"),
            "-Action",
            "reliability-state",
            "-ConfigPath",
            str(tod_dir / "config" / "tod.config.json"),
        ]

        result = subprocess.check_output(
            command,
            cwd=repo_root,
            env={**os.environ, **{"TOD_MODE_OVERRIDE": "local"}},
            text=True,
        )
        payload = json.loads(result)

        self.assertIn("contract_version", payload)
        self.assertIn("schema_version", payload)
        self.assertIn("capabilities", payload)
        self.assertIsInstance(payload["capabilities"], list)

        self.assertIn("integration_contract_for_mim", payload)
        integration = payload["integration_contract_for_mim"]
        self.assertIn("required_fields", integration)
        required = set(integration["required_fields"])

        expected = {
            "timestamp",
            "commit_sha",
            "pass_count",
            "fail_count",
            "retry_count",
            "guardrail_blocks",
            "engine_stats",
        }
        self.assertTrue(expected.issubset(required))


if __name__ == "__main__":
    unittest.main(verbosity=2)
