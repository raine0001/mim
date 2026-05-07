import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.communication_composer import compose_expert_communication_reply
from core.communication_composer import sanitize_user_facing_reply_text
from core.communication_composer import build_deterministic_communication_reply
from core.routers.gateway import _build_mim_interface_response
from core.routers.gateway import _should_force_deterministic_conversation_reply


class GatewayIntakeStallGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_reply_prefix_is_removed_from_user_facing_reply(self):
        diagnostics: dict[str, object] = {}

        with patch(
            "core.communication_composer._communication_openai_allowed",
            return_value=True,
        ), patch(
            "core.communication_composer._compose_with_openai_sync",
            return_value=SimpleNamespace(
                reply_text="Giving some extra context, Top risk is stale handoff state.",
                topic_hint="status",
                composer_mode="openai_rewrite",
                response_mode="default",
                should_store_memory=True,
                memory_topics=[],
                memory_people=[],
                memory_events=[],
                memory_experiences=[],
                model="gpt-test",
            ),
        ):
            reply = await compose_expert_communication_reply(
                user_input="what is your top risk",
                context={},
                fallback_reply="Top risk is stale handoff state.",
                runtime_diagnostics=diagnostics,
            )

        self.assertEqual(reply.reply_text, "Top risk is stale handoff state.")
        self.assertTrue(diagnostics.get("meta_prefix_removed"))
        self.assertEqual(
            diagnostics.get("raw_model_reply_text"),
            "Giving some extra context, Top risk is stale handoff state.",
        )
        self.assertEqual(
            diagnostics.get("cleaned_model_reply_text"),
            "Top risk is stale handoff state.",
        )

    async def test_gateway_mim_interface_reply_text_is_clean(self):
        event = SimpleNamespace(id=123, metadata_json={}, raw_input="how do we reduce that risk", source="text")
        resolution = SimpleNamespace(
            proposed_goal_description="",
            internal_intent="",
            outcome="resolved",
            reason="",
            clarification_prompt="",
            metadata_json={
                "mim_interface_reply_override": "Giving some extra context, Reduce that risk with regression checks.",
            }
        )

        response = _build_mim_interface_response(
            event=event,
            resolution=resolution,
            execution=None,
        )

        self.assertEqual(
            response.get("reply_text"),
            "Reduce that risk with regression checks.",
        )
        self.assertFalse(
            str(response.get("reply_text") or "").lower().startswith("giving some extra context")
        )

    async def test_deterministic_builder_output_remains_unchanged(self):
        reply = build_deterministic_communication_reply(
            user_input="what is your top risk",
            context={},
            fallback_reply="Top risk is stale handoff state.",
        )

        self.assertEqual(reply.reply_text, "Top risk is stale handoff state.")

    async def test_sanitizer_removes_helper_prefix_only_at_start(self):
        self.assertEqual(
            sanitize_user_facing_reply_text(
                "Giving some extra context, Top risk is stale handoff state."
            ),
            "Top risk is stale handoff state.",
        )

    async def test_eval_requests_force_deterministic_conversation_reply(self):
        event = type(
            "Event",
            (),
            {
                "requested_goal": "conversation_eval",
                "metadata_json": {"adapter": "conversation_eval_runner"},
            },
        )()

        self.assertTrue(_should_force_deterministic_conversation_reply(event))

    async def test_composer_force_deterministic_skips_openai_rewrite(self):
        with patch(
            "core.communication_composer._compose_with_openai_sync",
            side_effect=AssertionError("rewrite path should not run"),
        ):
            reply = await compose_expert_communication_reply(
                user_input="what is your top risk",
                context={"force_deterministic_communication": True},
                fallback_reply="Top risk is stale handoff state.",
            )

        self.assertEqual(reply.reply_text, "Top risk is stale handoff state.")

    async def test_composer_burst_falls_back_gracefully_when_rewrite_queue_saturates(self):
        async def run_burst() -> list[str]:
            tasks = [
                compose_expert_communication_reply(
                    user_input=f"request {index}",
                    context={},
                    fallback_reply=f"fallback {index}",
                    runtime_diagnostics={},
                )
                for index in range(10)
            ]
            results = await asyncio.gather(*tasks)
            return [reply.reply_text for reply in results]

        def slow_rewrite(**_: object):
            import time

            time.sleep(0.2)
            return None

        with patch("core.communication_composer._communication_openai_allowed", return_value=True), patch(
            "core.communication_composer.OPENAI_COMMUNICATION_SEMAPHORE",
            new=asyncio.Semaphore(1),
        ), patch(
            "core.communication_composer.DEFAULT_OPENAI_COMMUNICATION_QUEUE_TIMEOUT_SECONDS",
            0.01,
        ), patch(
            "core.communication_composer.DEFAULT_OPENAI_COMMUNICATION_TIMEOUT_SECONDS",
            0.05,
        ), patch(
            "core.communication_composer._compose_with_openai_sync",
            side_effect=slow_rewrite,
        ):
            replies = await run_burst()

        self.assertEqual(len(replies), 10)
        self.assertTrue(all(reply.startswith("fallback") for reply in replies))


if __name__ == "__main__":
    unittest.main(verbosity=2)