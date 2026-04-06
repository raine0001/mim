import unittest
from unittest.mock import patch

from core.routers import workspace


class WorkspaceExecutionHealthGateTest(unittest.TestCase):
    def test_healthy_keeps_dispatched_path(self):
        with patch.object(
            workspace._mim_health_monitor,
            "get_health_summary",
            return_value={"status": "healthy"},
        ):
            gate = workspace._physical_execution_health_gate()

        self.assertFalse(gate["active"])
        self.assertEqual(gate["requested_decision"], "queued_for_executor")
        self.assertEqual(gate["requested_status"], "dispatched")

    def test_degraded_requires_confirmation(self):
        with patch.object(
            workspace._mim_health_monitor,
            "get_health_summary",
            return_value={"status": "degraded"},
        ):
            gate = workspace._physical_execution_health_gate()

        self.assertTrue(gate["active"])
        self.assertEqual(gate["requested_decision"], "requires_confirmation")
        self.assertEqual(gate["requested_status"], "pending_confirmation")
        self.assertEqual(gate["requested_reason"], "system_health_degraded")

    def test_critical_requires_confirmation(self):
        with patch.object(
            workspace._mim_health_monitor,
            "get_health_summary",
            return_value={"status": "critical"},
        ):
            gate = workspace._physical_execution_health_gate()

        self.assertTrue(gate["active"])
        self.assertEqual(gate["requested_decision"], "requires_confirmation")
        self.assertEqual(gate["requested_status"], "pending_confirmation")
