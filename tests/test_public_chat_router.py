import asyncio
import unittest

from core.routers import public_chat
from core.identity import MIM_LEGAL_CONTACT_EMAIL
from core.identity import MIM_LEGAL_ENTITY_NAME
from core.identity import MIM_LEGAL_JURISDICTION
from core.identity import public_channel_definition
from core.identity import public_system_identity_summary


class PublicChatRouterTest(unittest.TestCase):
    def test_blocks_operator_commands_in_public_chat(self) -> None:
        reason = public_chat._public_command_block_reason(
            "Please restart the runtime service on /tod and deploy the branch."
        )
        self.assertIn("Public chat", reason)

    def test_allows_programming_help_in_tod_mode(self) -> None:
        reason = public_chat._public_command_block_reason(
            "Write a Python function that parses JSON and explain the edge cases."
        )
        self.assertEqual(reason, "")

    def test_extracts_profile_updates(self) -> None:
        updates = public_chat._extract_profile_updates(
            "My name is Jordan Hale. My goal is ship a cleaner landing page. My birthday is May 12. I enjoy robotics and design."
        )
        self.assertEqual(updates["name"], "Jordan Hale")
        self.assertIn("ship a cleaner landing page", updates["goals"])
        self.assertIn("May 12", updates["special_dates"])
        self.assertIn("robotics and design", updates["interests"])

    def test_merge_profile_dedupes_lists(self) -> None:
        merged = public_chat._merge_profile(
            {
                "name": "Jordan",
                "goals": ["ship the landing page"],
                "special_dates": ["May 12"],
                "interests": ["robotics"],
            },
            {
                "goals": ["ship the landing page", "tighten the chat UX"],
                "special_dates": ["May 12", "June 1"],
                "interests": ["robotics", "design"],
            },
        )
        self.assertEqual(merged["goals"], ["ship the landing page", "tighten the chat UX"])
        self.assertEqual(merged["special_dates"], ["May 12", "June 1"])
        self.assertEqual(merged["interests"], ["robotics", "design"])

    def test_public_identity_reply_explains_mim_and_tod_without_clarifier(self) -> None:
        reply = public_chat._build_public_fallback_reply(
            message="what is MIM and TOD",
            mode="mim",
            profile={},
            recall_summary="",
        )

        self.assertIn("operator-facing application and public channel", reply)
        self.assertIn("separate execution and validation application", reply)
        self.assertIn("MIM determines what should happen", reply)
        self.assertIn("not just answering a prompt", reply)
        self.assertNotIn("clarify", reply.lower())

    def test_tod_identity_reply_stays_in_tod_frame(self) -> None:
        reply = public_chat._build_public_fallback_reply(
            message="what is TOD",
            mode="tod",
            profile={},
            recall_summary="",
        )

        self.assertIn("separate execution-facing application", reply)
        self.assertIn("execution-truth and validation authority", reply)
        self.assertIn("prove it", reply)
        self.assertNotIn("I'm MIM", reply)

    def test_tod_general_reply_is_not_limited_to_coding_only(self) -> None:
        reply = public_chat._build_public_fallback_reply(
            message="what changed and what failed",
            mode="tod",
            profile={},
            recall_summary="",
        )

        self.assertIn("execution and verification side", reply)
        self.assertIn("what changed", reply)
        self.assertNotIn("TOD public mode is for programming conversation", reply)

    def test_public_channel_definition_separates_mim_and_tod(self) -> None:
        mim_channel = public_channel_definition("mim")
        tod_channel = public_channel_definition("tod")

        self.assertEqual(mim_channel["channel"], "public_mim_chat")
        self.assertEqual(tod_channel["channel"], "public_tod_chat")
        self.assertNotEqual(mim_channel["channel"], tod_channel["channel"])
        self.assertEqual(tod_channel["application_name"], "TOD")

    def test_privacy_policy_legal_details_are_hard_coded(self) -> None:
        self.assertEqual(MIM_LEGAL_ENTITY_NAME, "MIM Robots LLC")
        self.assertEqual(MIM_LEGAL_CONTACT_EMAIL, "MIM@agentmim.com")
        self.assertEqual(MIM_LEGAL_JURISDICTION, "Wyoming")

    def test_public_system_identity_includes_managed_app_stack(self) -> None:
        summary = public_system_identity_summary()
        self.assertIn("agentmim.com", summary)
        self.assertIn("coachmim.com", summary)
        self.assertIn("visaion.com", summary)
        self.assertIn("mimrobots.com", summary)
        self.assertIn("mim_arm", summary)

    def test_public_home_uses_single_title_and_human_subtitle(self) -> None:
        response = asyncio.run(public_chat.public_chat_home())
        body = response.body.decode()
        self.assertIn("Talk to a system that doesn't just respond. It tries to act, verify, and improve.", body)
        self.assertNotIn('<h1 id="stageTitle">MIM &amp; TOD</h1>', body)
        self.assertIn('data-starter="What is this system?"', body)
        self.assertIn('href="https://mim.mimtod.com/mim/login?next=/mim"', body)


if __name__ == "__main__":
    unittest.main()