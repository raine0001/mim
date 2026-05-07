import unittest

from core.routers.mim_ui import _serialize_chat_message


class MimUiChatMessageSerializationTest(unittest.TestCase):
    def test_operator_message_serializes_as_user(self) -> None:
        serialized = _serialize_chat_message(
            {
                "role": "operator",
                "direction": "inbound",
                "content": "What is the current objective?",
                "metadata_json": {"interaction_mode": "text"},
            }
        )

        self.assertEqual(serialized["message_type"], "user")
        self.assertEqual(serialized["inline_text"], "What is the current objective?")
        self.assertEqual(serialized["execution_text"], "")

    def test_structured_execution_message_is_parsed_and_truncated(self) -> None:
        filler_lines = [f"detail line {index}" for index in range(1, 28)]
        serialized = _serialize_chat_message(
            {
                "role": "mim",
                "direction": "outbound",
                "content": "\n".join(
                    [
                        "Iteration 1:",
                        "Task: Inspect the current chat renderer",
                        "Result: Found raw execution output mixed into reply text",
                        "Delta: Add typed execution rendering with containment",
                        "Iteration 2:",
                        "Task: Patch the UI renderer",
                        "Result: Added collapsible structured execution panel",
                        "Delta: Chat keeps only short summaries inline",
                        *filler_lines,
                    ]
                ),
                "metadata_json": {
                    "interaction_mode": "text",
                    "execution_id": 42,
                },
            }
        )

        self.assertEqual(serialized["message_type"], "system_execution")
        self.assertTrue(serialized["execution_truncated"])
        self.assertEqual(serialized["structured_output"]["step_count"], 2)
        self.assertEqual(
            serialized["structured_output"]["steps"][0]["task"],
            "Inspect the current chat renderer",
        )
        self.assertIn("Execution trace with 2 steps", serialized["summary_text"])
        self.assertNotEqual(serialized["execution_preview"], serialized["execution_text"])
        self.assertIn("detail line 1", serialized["execution_preview"])
        self.assertNotIn("detail line 27", serialized["execution_preview"])

    def test_explicit_system_summary_is_retained(self) -> None:
        serialized = _serialize_chat_message(
            {
                "role": "mim",
                "direction": "outbound",
                "content": "Text chat cleared. Ready for your next message.",
                "metadata_json": {
                    "interaction_mode": "text",
                    "message_type": "system_summary",
                },
            }
        )

        self.assertEqual(serialized["message_type"], "system_summary")
        self.assertEqual(serialized["inline_text"], "Text chat cleared. Ready for your next message.")


if __name__ == "__main__":
    unittest.main()