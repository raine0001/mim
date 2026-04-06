import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "probe_canonical_task_request_filesystem.py"
PYTHON = ROOT / ".venv" / "bin" / "python"


class ProbeCanonicalTaskRequestFilesystemTest(unittest.TestCase):
    def test_filesystem_probe_emits_real_absolute_and_mount_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "MIM_TOD_TASK_REQUEST.latest.json"
            path.write_text("{}\n", encoding="utf-8")

            completed = subprocess.run(
                [str(PYTHON), str(SCRIPT), "--path", str(path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            payload = json.loads(completed.stdout)
            self.assertEqual(payload["type"], "canonical_task_request_filesystem_probe_v1")
            self.assertEqual(payload["absolute_path"], str(path.absolute()))
            self.assertEqual(payload["realpath"], os.path.realpath(str(path.absolute())))
            self.assertEqual(payload["target"]["stats"]["device_id"], path.stat().st_dev)
            self.assertTrue(payload["target"]["mount_point"])
            self.assertTrue(payload["parent_chain"])
            self.assertIn("mount_namespace", payload["namespace"])