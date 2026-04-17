import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "arm_host_communication_cutover.py"
PYTHON = ROOT / ".venv" / "bin" / "python"


class ArmHostCommunicationCutoverTest(unittest.TestCase):
    def test_disable_renames_surface_and_installs_read_only_trap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            surface_dir = Path(tmp_dir) / "shared"
            surface_dir.mkdir(parents=True)
            (surface_dir / "MIM_TOD_TASK_REQUEST.latest.json").write_text("{}\n", encoding="utf-8")

            completed = subprocess.run(
                [
                    str(PYTHON),
                    str(SCRIPT),
                    "--mode",
                    "disable",
                    "--local-surface-root",
                    str(surface_dir),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            result = payload["result"]
            disabled_dir = Path(result["disabled_dir"])
            trap_path = Path(result["trap_path"])

            self.assertTrue(result["renamed"])
            self.assertTrue(disabled_dir.exists())
            self.assertTrue((disabled_dir / "MIM_TOD_TASK_REQUEST.latest.json").exists())
            self.assertTrue(trap_path.exists())
            self.assertIn("not allowed on ARM host", trap_path.read_text(encoding="utf-8"))
            self.assertFalse(os.access(trap_path, os.W_OK))

    def test_purge_removes_disabled_directory_after_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            surface_dir = Path(tmp_dir) / "shared"
            surface_dir.mkdir(parents=True)
            (surface_dir / "payload.json").write_text("{}\n", encoding="utf-8")

            disable = subprocess.run(
                [
                    str(PYTHON),
                    str(SCRIPT),
                    "--mode",
                    "disable",
                    "--local-surface-root",
                    str(surface_dir),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(disable.returncode, 0, disable.stdout + disable.stderr)

            purge = subprocess.run(
                [
                    str(PYTHON),
                    str(SCRIPT),
                    "--mode",
                    "purge",
                    "--local-surface-root",
                    str(surface_dir),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(purge.returncode, 0, purge.stdout + purge.stderr)
            payload = json.loads(purge.stdout)
            result = payload["result"]
            self.assertFalse(Path(result["disabled_dir"]).exists())
            self.assertTrue(Path(result["trap_path"]).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)