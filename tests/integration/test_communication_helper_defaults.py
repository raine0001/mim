import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def _load_module(module_name: str, script_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CommunicationHelperDefaultsTest(unittest.TestCase):
    def test_probe_task_request_prefers_tod_env_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MIM_TOD_SSH_HOST_USER": "tod-user",
                "MIM_TOD_SSH_PORT": "2201",
                "MIM_ARM_SSH_HOST_USER": "arm-user",
                "MIM_ARM_SSH_HOST_PORT": "2202",
            },
            clear=False,
        ):
            module = _load_module("probe_canonical_task_request", ROOT / "scripts" / "probe_canonical_task_request.py")
            with patch.object(sys, "argv", [str(ROOT / "scripts" / "probe_canonical_task_request.py")]):
                args = module.parse_args()
            self.assertEqual(args.ssh_user, "tod-user")
            self.assertEqual(args.ssh_port, 2201)
            self.assertEqual(args.password_env, "MIM_TOD_SSH_PASS")

    def test_probe_filesystem_prefers_tod_env_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MIM_TOD_SSH_HOST_USER": "tod-user",
                "MIM_TOD_SSH_PORT": "2201",
                "MIM_ARM_SSH_HOST_USER": "arm-user",
                "MIM_ARM_SSH_HOST_PORT": "2202",
            },
            clear=False,
        ):
            module = _load_module("probe_canonical_task_request_filesystem", ROOT / "scripts" / "probe_canonical_task_request_filesystem.py")
            with patch.object(sys, "argv", [str(ROOT / "scripts" / "probe_canonical_task_request_filesystem.py")]):
                args = module.parse_args()
            self.assertEqual(args.ssh_user, "tod-user")
            self.assertEqual(args.ssh_port, 2201)
            self.assertEqual(args.password_env, "MIM_TOD_SSH_PASS")

    def test_publish_defaults_do_not_fall_back_to_arm_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MIM_TOD_SSH_HOST_USER": "tod-user",
                "MIM_TOD_SSH_PORT": "2201",
                "MIM_ARM_SSH_HOST_USER": "arm-user",
                "MIM_ARM_SSH_HOST_PORT": "2202",
            },
            clear=False,
        ):
            module = _load_module("publish_tod_bridge_artifacts_remote", ROOT / "scripts" / "publish_tod_bridge_artifacts_remote.py")
            with patch.object(sys, "argv", [str(ROOT / "scripts" / "publish_tod_bridge_artifacts_remote.py")]):
                args = module.parse_args()
            self.assertEqual(args.ssh_user, "tod-user")
            self.assertEqual(args.ssh_port, 2201)

    def test_pull_defaults_do_not_fall_back_to_arm_env(self) -> None:
        with patch.dict(
            os.environ,
            {
                "MIM_TOD_SSH_HOST_USER": "tod-user",
                "MIM_TOD_SSH_PORT": "2201",
                "MIM_ARM_SSH_HOST_USER": "arm-user",
                "MIM_ARM_SSH_HOST_PORT": "2202",
            },
            clear=False,
        ):
            module = _load_module("pull_tod_bridge_artifacts_remote", ROOT / "scripts" / "pull_tod_bridge_artifacts_remote.py")
            with patch.object(sys, "argv", [str(ROOT / "scripts" / "pull_tod_bridge_artifacts_remote.py")]):
                args = module.parse_args()
            self.assertEqual(args.ssh_user, "tod-user")
            self.assertEqual(args.ssh_port, 2201)


if __name__ == "__main__":
    unittest.main(verbosity=2)