from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sync_mim_arm_host_state.py"
SPEC = importlib.util.spec_from_file_location("sync_mim_arm_host_state", MODULE_PATH)
sync_mim_arm_host_state = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sync_mim_arm_host_state)


class SyncMimArmHostStateTest(unittest.TestCase):
    def test_http_fallback_passes_shared_attribution_inputs_to_generator(self):
        local_output = Path("/tmp/mim_arm_host_state.latest.json")
        args = SimpleNamespace(
            host="192.168.1.90",
            ssh_user="testpilot",
            ssh_port=22,
            remote_root="/home/testpilot/mim_arm/runtime/shared",
            remote_script_path="/home/testpilot/mim_arm/runtime/tools/generate_mim_arm_host_state.py",
            remote_output="/home/testpilot/mim_arm/runtime/shared/mim_arm_host_state.latest.json",
            local_output=str(local_output),
            password_env="MIM_ARM_SSH_HOST_PASS",
            skip_remote_run=False,
            http_fallback=True,
            arm_api_port=5000,
        )

        with patch.object(sync_mim_arm_host_state, "parse_args", return_value=args), patch.object(
            sync_mim_arm_host_state.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0),
        ) as mock_run:
            exit_code = sync_mim_arm_host_state.main()

        self.assertEqual(exit_code, 0)
        command = mock_run.call_args.args[0]
        self.assertIn("--shared-root", command)
        self.assertIn(str(sync_mim_arm_host_state.LOCAL_SHARED_ROOT), command)
        self.assertIn("--input-json", command)
        self.assertIn(str(sync_mim_arm_host_state.LOCAL_SHARED_ROOT / "TOD_MIM_TASK_ACK.latest.json"), command)
        self.assertIn(str(sync_mim_arm_host_state.LOCAL_SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json"), command)
        self.assertIn(str(sync_mim_arm_host_state.LOCAL_SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json"), command)


if __name__ == "__main__":
    unittest.main(verbosity=2)