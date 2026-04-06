import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "compare_canonical_task_request_probes.py"
PYTHON = ROOT / ".venv" / "bin" / "python"


class CompareCanonicalTaskRequestProbesTest(unittest.TestCase):
    def test_same_path_different_inode_and_hash_is_classified_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            left_path = Path(tmp_dir) / "left.json"
            right_path = Path(tmp_dir) / "right.json"
            left_path.write_text(
                json.dumps(
                    {
                        "transport": "local",
                        "stable": True,
                        "samples": [
                            {
                                "hostname": "MIM",
                                "whoami": "testpilot",
                                "absolute_path": "/home/testpilot/mim/runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                "realpath": "/home/testpilot/mim/runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                "ls_inode": 54950863,
                                "mtime": "2026-04-02T15:57:40.140606Z",
                                "size": 874,
                                "sha256": "18953bdb12ff465631a2b759357c61faa13eb648b9670d5857b5e65198ee9c86",
                                "objective_id": "objective-97",
                                "task_id": "objective-97-task-3422",
                                "sequence": 273444,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            right_path.write_text(
                json.dumps(
                    {
                        "transport": "ssh",
                        "stable": True,
                        "samples": [
                            {
                                "hostname": "raspberrypi",
                                "whoami": "testpilot",
                                "absolute_path": "/home/testpilot/mim/runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                "realpath": "/home/testpilot/mim/runtime/shared/MIM_TOD_TASK_REQUEST.latest.json",
                                "ls_inode": 649265,
                                "mtime": "2026-04-02T15:48:30.097756Z",
                                "size": 875,
                                "sha256": "2ef2574dd4dfc7889da67f98296f06ca2c38801e3bc046e870da371a03bde105",
                                "objective_id": "objective-75",
                                "task_id": "objective-75-task-3422",
                                "sequence": 381549,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), str(left_path), str(right_path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            payload = json.loads(completed.stdout)
            self.assertEqual(payload["type"], "canonical_task_request_probe_comparison_v1")
            self.assertEqual(payload["classification"], "same_path_different_inode_different_hash")
            self.assertTrue(payload["left_stable"])
            self.assertTrue(payload["right_stable"])
            self.assertTrue(payload["summary"]["same_absolute_path"])
            self.assertTrue(payload["summary"]["same_realpath"])
            self.assertFalse(payload["summary"]["same_inode"])
            self.assertFalse(payload["summary"]["same_sha256"])
            self.assertFalse(payload["summary"]["same_task_id"])
            self.assertFalse(payload["summary"]["same_objective_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)