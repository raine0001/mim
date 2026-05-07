from __future__ import annotations

import unittest

from scripts.run_mim_tod_coordination_simulation import run_simulation


class MimTodCoordinationSimulationHarnessTests(unittest.TestCase):
    def test_5000_lane_simulation_is_lineage_safe(self) -> None:
        report = run_simulation(total_runs=5000, output_dir=None)
        summary = report["summary"]

        self.assertTrue(summary["pass"])
        self.assertEqual(summary["total_runs"], 5000)
        self.assertEqual(summary["lineage_safe_runs"], 5000)
        self.assertEqual(summary["stale_lineage_accepted"], 0)
        self.assertEqual(summary["wrong_task_completions_accepted"], 0)
        self.assertEqual(summary["false_idle_blocked_from_task_mismatch"], 0)
        self.assertEqual(summary["fallback_task_mutations"], 0)
        self.assertEqual(summary["failure_count"], 0)
