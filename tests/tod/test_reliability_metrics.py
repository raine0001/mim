import unittest


def compute_confidence(success_rate, failure_penalty, fallback_penalty, sample_size_weight):
    confidence = (
        success_rate * 0.5
        - failure_penalty * 0.3
        - fallback_penalty * 0.1
        + sample_size_weight * 0.1
    )
    return max(0.0, min(1.0, round(confidence, 4)))


def classify_recovery(success, fallback_used, retry_count, guardrail_block, needs_manual):
    if guardrail_block:
        return "guardrail_block"
    if not success:
        return "unrecovered_failure"
    if fallback_used:
        return "recovered_on_fallback"
    if retry_count > 0:
        return "recovered_on_retry"
    if needs_manual:
        return "manual_intervention"
    return "clean_success"


class ReliabilityMetricsTest(unittest.TestCase):
    def test_confidence_formula_windowed_weights(self):
        value = compute_confidence(0.9, 0.1, 0.05, 0.8)
        self.assertGreaterEqual(value, 0.0)
        self.assertLessEqual(value, 1.0)
        self.assertAlmostEqual(value, 0.5 * 0.9 - 0.3 * 0.1 - 0.1 * 0.05 + 0.1 * 0.8, places=4)

    def test_recovery_category_priority(self):
        self.assertEqual(classify_recovery(False, False, 0, False, False), "unrecovered_failure")
        self.assertEqual(classify_recovery(True, True, 0, False, False), "recovered_on_fallback")
        self.assertEqual(classify_recovery(True, False, 1, False, False), "recovered_on_retry")
        self.assertEqual(classify_recovery(True, False, 0, True, False), "guardrail_block")
        self.assertEqual(classify_recovery(True, False, 0, False, True), "manual_intervention")
        self.assertEqual(classify_recovery(True, False, 0, False, False), "clean_success")


if __name__ == "__main__":
    unittest.main(verbosity=2)
