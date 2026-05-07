import unittest
import urllib.error

from unittest.mock import patch

from conversation_eval_runner import (
    _aggregate,
    _adapt_text,
    _build_jobs,
    _evaluate_regression_gate,
    _filter_scenarios,
    _interface_messages_path,
    _post_json,
    _response_text,
    _response_text_from_interface_payload,
    _response_text_from_gateway_payload,
    _resolve_turn_response,
    _timeout_recovery_attempts,
    run_eval,
    _summarize,
    _turn_scores,
    EvalScenarioResult,
)


class ConversationEvalRunnerTest(unittest.TestCase):
    def test_post_json_retries_transient_transport_error(self):
        class _FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok": true}'

        with patch(
            "conversation_eval_runner.urllib.request.urlopen",
            side_effect=[urllib.error.URLError("temporary"), _FakeResponse()],
        ):
            status, payload = _post_json(
                "http://example.test",
                "/gateway/intake/text",
                {"text": "ping"},
                timeout_seconds=1,
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})

    def test_resolve_turn_response_polls_state_until_populated(self):
        def _fake_get_json(_base_url, path, timeout_seconds=1):
            self.assertIn(path, {
                _interface_messages_path("eval-session"),
                "/mim/ui/state",
            })
            if path == _interface_messages_path("eval-session"):
                return 404, {}
            return 200, {"latest_output_text": "Status: online and stable."}

        with patch("conversation_eval_runner._get_json", side_effect=_fake_get_json):
            response, inquiry_prompt, latest_output = _resolve_turn_response(
                base_url="http://example.test",
                payload={},
                timeout_seconds=1,
                session_id="eval-session",
            )

        self.assertEqual(response, "Status: online and stable.")
        self.assertEqual(inquiry_prompt, "")
        self.assertEqual(latest_output, "Status: online and stable.")

    def test_response_text_from_interface_payload_prefers_latest_outbound_mim_message(self):
        response, inquiry_prompt, latest_output = _response_text_from_interface_payload(
            {
                "messages": [
                    {
                        "direction": "inbound",
                        "role": "operator",
                        "actor": "operator",
                        "content": "give me status",
                    },
                    {
                        "direction": "outbound",
                        "role": "mim",
                        "actor": "mim",
                        "content": "Status: online and stable.",
                    },
                ]
            }
        )

        self.assertEqual(response, "Status: online and stable.")
        self.assertEqual(inquiry_prompt, "")
        self.assertEqual(latest_output, "Status: online and stable.")

    def test_resolve_turn_response_recovers_timeout_from_interface_session(self):
        calls: list[str] = []

        def _fake_get_json(_base_url, path, timeout_seconds=1):
            calls.append(path)
            if path == _interface_messages_path("eval-session") and len(calls) < 3:
                return 404, {}
            if path == _interface_messages_path("eval-session"):
                return 200, {
                    "messages": [
                        {
                            "direction": "outbound",
                            "role": "mim",
                            "actor": "mim",
                            "content": "Recovered after timeout.",
                        }
                    ]
                }
            return 401, {}

        with patch("conversation_eval_runner._get_json", side_effect=_fake_get_json):
            response, inquiry_prompt, latest_output = _resolve_turn_response(
                base_url="http://example.test",
                payload={},
                timeout_seconds=1,
                session_id="eval-session",
                allow_timeout_recovery=True,
            )

        self.assertEqual(response, "Recovered after timeout.")
        self.assertEqual(inquiry_prompt, "")
        self.assertEqual(latest_output, "Recovered after timeout.")
        self.assertGreaterEqual(calls.count(_interface_messages_path("eval-session")), 3)

    def test_run_eval_recovers_timeout_turn_from_interface_messages(self):
        scenarios = [
            {
                "scenario_id": "s1",
                "bucket": "logic_core",
                "category": "general",
                "scenario_split": "train",
                "user_turns": ["give me status"],
                "expected_behavior": ["answer_question"],
            }
        ]
        profiles = [{"profile_id": "p1", "style": "concise", "default_confidence": 0.85}]

        with patch(
            "conversation_eval_runner._post_json",
            return_value=(599, {"transport_error_type": "TimeoutError"}),
        ), patch(
            "conversation_eval_runner._resolve_turn_response",
            return_value=("Status: online and stable.", "", "Status: online and stable."),
        ) as mocked_resolve:
            results = run_eval(
                base_url="http://example.test",
                scenarios=scenarios,
                profiles=profiles,
                turn_delay_ms=0,
                limit_scenarios=0,
                limit_profiles=0,
                target_conversations=1,
                randomize=False,
                rng=__import__("random").Random(7),
                include_buckets=None,
                exclude_buckets=None,
                include_categories=None,
                exclude_categories=None,
                include_splits=None,
                exclude_splits=None,
                request_timeout_seconds=1,
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].turns[0].response_text, "Status: online and stable.")
        self.assertEqual(results[0].failures, [])
        self.assertTrue(mocked_resolve.call_args.kwargs["allow_timeout_recovery"])

    def test_timeout_recovery_attempts_have_floor(self):
        self.assertGreaterEqual(_timeout_recovery_attempts(1), 1)

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

    def test_response_text_from_gateway_payload_prefers_reply_text(self):
        response, inquiry_prompt, latest_output = _response_text_from_gateway_payload(
            {
                "mim_interface": {
                    "reply_text": "Direct reply text.",
                    "result": "Short result.",
                }
            }
        )

        self.assertEqual(response, "Direct reply text.")
        self.assertEqual(inquiry_prompt, "")
        self.assertEqual(latest_output, "Short result.")

    def test_response_text_from_gateway_payload_uses_clarification_prompt(self):
        response, inquiry_prompt, latest_output = _response_text_from_gateway_payload(
            {
                "resolution": {
                    "clarification_prompt": "I need one more detail.",
                }
            }
        )

        self.assertEqual(response, "I need one more detail.")
        self.assertEqual(inquiry_prompt, "I need one more detail.")
        self.assertEqual(latest_output, "I need one more detail.")

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

    def test_aggregate_reports_conversation_behavior_metrics(self):
        from conversation_eval_runner import EvalTurn

        turns = [
            EvalTurn(
                user_text="give me your current status in one line",
                adapted_text="give me your current status in one line",
                response_text="Status: online and stable.",
                inquiry_prompt="",
                latest_output_text="Status: online and stable.",
                relevance=0.8,
                non_repetition=1.0,
                brevity=1.0,
                asked_clarification=False,
            )
        ]

        score, failures = _aggregate(turns, ["answer_question", "answer_plainly"])

        self.assertEqual(failures, [])
        self.assertIn("intent_retention", score)
        self.assertIn("directness", score)
        self.assertIn("clarification_efficiency", score)
        self.assertIn("brevity_relevance", score)
        self.assertGreater(score["directness"], 0.7)
        self.assertEqual(score["clarification_efficiency"], 1.0)

    def test_summarize_includes_metric_average(self):
        results = [
            EvalScenarioResult(
                scenario_id="s1",
                profile_id="p1",
                bucket="logic_core",
                category="leadership",
                scenario_split="train",
                score={
                    "overall": 0.8,
                    "intent_retention": 0.9,
                    "directness": 0.7,
                    "clarification_efficiency": 1.0,
                    "brevity_relevance": 0.8,
                },
                failures=[],
                turns=[],
            ),
            EvalScenarioResult(
                scenario_id="s2",
                profile_id="p2",
                bucket="continuity",
                category="software_planning",
                scenario_split="holdout",
                score={
                    "overall": 0.6,
                    "intent_retention": 0.5,
                    "directness": 0.4,
                    "clarification_efficiency": 0.5,
                    "brevity_relevance": 0.6,
                },
                failures=["context_drift"],
                turns=[],
            ),
        ]

        summary = _summarize(results)

        self.assertIn("metric_average", summary)
        self.assertEqual(summary["metric_average"]["intent_retention"], 0.7)
        self.assertEqual(summary["metric_average"]["clarification_efficiency"], 0.75)

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

    def test_filter_scenarios_honors_category_and_split(self):
        scenarios = [
            {"scenario_id": "s1", "bucket": "logic_core", "category": "leadership", "scenario_split": "train"},
            {"scenario_id": "s2", "bucket": "logic_core", "category": "leadership", "scenario_split": "holdout"},
            {"scenario_id": "s3", "bucket": "continuity", "category": "software_planning", "scenario_split": "train"},
        ]

        filtered = _filter_scenarios(
            scenarios,
            include_buckets={"logic_core"},
            exclude_buckets=None,
            include_categories={"leadership"},
            exclude_categories=None,
            include_splits={"holdout"},
            exclude_splits=None,
        )

        self.assertEqual([item["scenario_id"] for item in filtered], ["s2"])

    def test_summarize_reports_category_and_split_averages(self):
        results = [
            EvalScenarioResult(
                scenario_id="s1",
                profile_id="p1",
                bucket="logic_core",
                category="leadership",
                scenario_split="train",
                score={"overall": 0.8},
                failures=["context_drift"],
                turns=[],
            ),
            EvalScenarioResult(
                scenario_id="s2",
                profile_id="p2",
                bucket="continuity",
                category="software_planning",
                scenario_split="holdout",
                score={"overall": 0.6},
                failures=[],
                turns=[],
            ),
        ]

        summary = _summarize(results)

        self.assertEqual(summary["category_average"]["leadership"], 0.8)
        self.assertEqual(summary["category_average"]["software_planning"], 0.6)
        self.assertEqual(summary["split_average"]["train"], 0.8)
        self.assertEqual(summary["split_average"]["holdout"], 0.6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
