import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class Objective75OvernightConfigurationTest(unittest.TestCase):
    def test_runner_defaults_to_objective75(self) -> None:
        script = (ROOT / "scripts" / "run_objective75_overnight_loop.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('OBJECTIVE_ID="${OBJECTIVE_ID:-75}"', script)
        self.assertIn('ALLOW_LOCAL_ONLY_CANONICAL_WRITE:=0', script)

    def test_user_installer_retires_overnight_unit(self) -> None:
        script = (ROOT / "scripts" / "install_objective75_user_units.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("mim-objective75-overnight-loop.service", script.split("UNITS=(", 1)[1].split(")", 1)[0])
        self.assertIn("disable --now mim-objective75-overnight-loop.service", script)

    def test_system_installer_retires_overnight_unit(self) -> None:
        script = (ROOT / "scripts" / "install_objective75_systemd_units.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("mim-objective75-overnight-loop.service", script.split("UNITS=(", 1)[1].split(")", 1)[0])
        self.assertIn("disable --now mim-objective75-overnight-loop.service", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)