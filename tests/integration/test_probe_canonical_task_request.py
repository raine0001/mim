import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "probe_canonical_task_request.py"
PYTHON = ROOT / ".venv" / "bin" / "python"


class ProbeCanonicalTaskRequestTest(unittest.TestCase):
    def test_local_probe_emits_stable_fingerprint_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "MIM_TOD_TASK_REQUEST.latest.json"
            path.write_text(
                json.dumps(
                    {
                        "task_id": "objective-97-task-3422",
                        "objective_id": "objective-97",
                        "correlation_id": "obj97-task3422",
                        "generated_at": "2026-04-02T15:41:50Z",
                        "emitted_at": "2026-04-02T15:41:50Z",
                        "sequence": 273444,
                        "source_service": "mim_tod_auto_reissue",
                        "source_instance_id": "mim_tod_auto_reissue:823390",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    str(PYTHON),
                    str(SCRIPT),
                    "--path",
                    str(path),
                    "--samples",
                    "2",
                    "--interval-seconds",
                    "0",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            payload = json.loads(completed.stdout)
            self.assertEqual(payload["type"], "canonical_task_request_probe_v1")
            self.assertTrue(payload["stable"])
            self.assertEqual(payload["hostname"], payload["samples"][0]["hostname"])
            self.assertEqual(payload["whoami"], payload["samples"][0]["whoami"])
            self.assertEqual(payload["absolute_path"], str(path.absolute()))
            self.assertEqual(payload["realpath"], os.path.realpath(str(path.absolute())))
            self.assertEqual(payload["transport"], "local")
            self.assertEqual(payload["unique_task_ids"], ["objective-97-task-3422"])
            self.assertEqual(payload["unique_objective_ids"], ["objective-97"])
            self.assertEqual(len(payload["samples"]), 2)
            sample = payload["samples"][0]
            self.assertEqual(sample["absolute_path"], str(path.absolute()))
            self.assertEqual(sample["realpath"], os.path.realpath(str(path.absolute())))
            self.assertEqual(sample["task_id"], "objective-97-task-3422")
            self.assertEqual(sample["objective_id"], "objective-97")
            self.assertEqual(sample["correlation_id"], "obj97-task3422")
            self.assertEqual(sample["sequence"], 273444)
            self.assertTrue(sample["sha256"])


class ProbeCanonicalTaskRequestAbsoluteVsRealPathTest(unittest.TestCase):
    def test_probe_distinguishes_absolute_path_from_realpath_for_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            real_dir = tmp_root / "real"
            link_dir = tmp_root / "linked"
            real_dir.mkdir()
            link_dir.symlink_to(real_dir, target_is_directory=True)
            path = real_dir / "MIM_TOD_TASK_REQUEST.latest.json"
            path.write_text(
                json.dumps(
                    {
                        "task_id": "objective-97-task-3422",
                        "objective_id": "objective-97",
                        "generated_at": "2026-04-02T15:41:50Z",
                        "emitted_at": "2026-04-02T15:41:50Z",
                        "sequence": 273444,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            via_symlink = link_dir / "MIM_TOD_TASK_REQUEST.latest.json"

            completed = subprocess.run(
                [
                    str(PYTHON),
                    str(SCRIPT),
                    "--path",
                    str(via_symlink),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

            payload = json.loads(completed.stdout)
            self.assertEqual(payload["absolute_path"], str(via_symlink.absolute()))
            self.assertEqual(payload["realpath"], os.path.realpath(str(via_symlink.absolute())))
            self.assertNotEqual(payload["absolute_path"], payload["realpath"])


if __name__ == "__main__":
    unittest.main(verbosity=2)