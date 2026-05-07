import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "continuous_task_dispatch.sh"


class ContinuousTaskDispatchGuardTest(unittest.TestCase):
    def test_blocks_canonical_write_while_active_initiative_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            shared_dir = temp_root / "shared"
            shared_dir.mkdir(parents=True, exist_ok=True)
            formal_program_response = temp_root / "formal_program_drive_response.json"
            formal_program_response.write_text(
                json.dumps(
                    {
                        "objective": {
                            "objective_id": "2900",
                            "status": "in_progress",
                            "initiative_id": "MIM-DAY-02-INITIATIVE-ISOLATION",
                            "active_task": {
                                "id": "7117",
                                "title": "Prevent initiative contamination",
                            },
                        },
                        "continuation": {
                            "status": {
                                "execution_state": "executing",
                                "active_task": {
                                    "task_id": "7117",
                                    "title": "Prevent initiative contamination",
                                },
                            }
                        },
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=ROOT,
                env={
                    **os.environ,
                    "SHARED_DIR": str(shared_dir),
                    "FORMAL_PROGRAM_RESPONSE_PATH": str(formal_program_response),
                    "ALLOW_LOCAL_ONLY_CANONICAL_WRITE": "1",
                    "START_ID": "8",
                    "COUNT": "1",
                    "TIMEOUT_SECONDS": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn(
                "active initiative execution is in progress",
                completed.stdout,
            )
            self.assertFalse((shared_dir / "MIM_TOD_TASK_REQUEST.latest.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)