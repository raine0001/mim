import unittest

from conversation_eval_runner import (
    _aggregate,
    _adapt_text,
    _build_jobs,
    _evaluate_regression_gate,
    _response_text,
    _turn_scores,
)


class ConversationEvalRunnerTest(unittest.TestCase):
    def test_profile_adaptation_typo_heavy(self):
        adapted = _adapt_text("please review what you are doing", "typo_heavy")
        self.assertIn("pls", adapted)
        self.assertIn("u", adapted)

    def test_turn_scores_detect_repetition(self):
        relevance, non_repetition, brevity, asked = _turn_scores(
            user_text="check health status",
            response_text="check health status",
            previous_response="check health status",
        )
        self.assertGreaterEqual(relevance, 0.4)
        self.assertEqual(non_repetition, 0.0)
        self.assertTrue(brevity > 0.0)
        self.assertFalse(asked)

    def test_turn_scores_question_not_auto_clarifier(self):
        relevance, non_repetition, brevity, asked = _turn_scores(
            user_text="what's up",
            response_text="How are you doing today?",
            previous_response="",
        )
        self.assertGreaterEqual(relevance, 0.0)
        self.assertGreaterEqual(non_repetition, 0.0)
        self.assertGreaterEqual(brevity, 0.0)
        self.assertFalse(asked)

    def test_turn_scores_detects_clarifier_markers(self):
        _, _, _, asked = _turn_scores(
            user_text="help",
            response_text="I am still missing one detail. Options: 1) now 2) later",
            previous_response="",
        )
        self.assertTrue(asked)

    def test_response_text_merges_distinct_fields(self):
        response, inquiry_prompt, latest_output = _response_text(
            {
                "inquiry_prompt": "Can you clarify your request?",
                "latest_output_text": "I can help with that.",
            }
        )
        self.assertEqual(
            response, "I can help with that. Can you clarify your request?"
        )
        self.assertEqual(inquiry_prompt, "Can you clarify your request?")
        self.assertEqual(latest_output, "I can help with that.")

    def test_response_text_dedupes_when_latest_contains_inquiry(self):
        response, _, _ = _response_text(
            {
                "inquiry_prompt": "Can you clarify your request?",
                "latest_output_text": "I can help. Can you clarify your request?",
            }
        )
        self.assertEqual(response, "I can help. Can you clarify your request?")

    def test_aggregate_flags_missing_clarification(self):
        from conversation_eval_runner import EvalTurn

        turns = [
            EvalTurn(
                user_text="can you handle that thing",
                adapted_text="can you handle that thing",
                response_text="I can help.",
                inquiry_prompt="",
                latest_output_text="I can help.",
                relevance=0.3,
                non_repetition=1.0,
                brevity=1.0,
                asked_clarification=False,
            )
        ]
        score, failures = _aggregate(turns, ["ask_single_clarification"])
        self.assertIn("missing_clarification", failures)
        self.assertGreaterEqual(score["overall"], 0.0)

    def test_build_jobs_expands_to_target(self):
        scenarios = [{"scenario_id": "s1"}, {"scenario_id": "s2"}]
        profiles = [{"profile_id": "p1"}]
        jobs = _build_jobs(
            scenarios=scenarios,
            profiles=profiles,
            target_conversations=5,
            randomize=False,
            rng=__import__("random").Random(7),
        )
        self.assertEqual(len(jobs), 5)

    def test_regression_gate_fails_on_drop(self):
        gate = _evaluate_regression_gate(
            summary={
                "overall": 0.6,
                "failure_count": 20,
                "bucket_average": {"greetings": 0.5},
            },
            baseline_summary={
                "overall": 0.8,
                "failure_count": 5,
                "bucket_average": {"greetings": 0.8},
            },
            max_overall_drop=0.05,
            max_bucket_drop=0.1,
            max_failure_increase=3,
        )
        self.assertFalse(gate["passed"])
        self.assertTrue(
            any("overall_drop_exceeded" in item for item in gate["failures"])
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
