import unittest

from scripts.run_mim_object_inquiry_simulation_sweep import (
    _contains_all,
    _contains_any,
    _contains_none,
    _render,
)


class ObjectInquirySimulationSweepTest(unittest.TestCase):
    def test_render_formats_nested_placeholders(self) -> None:
        rendered = _render(
            {
                "label": "dock_{run_id}",
                "items": ["bench-{run_id}", {"owner": "Jordan-{run_id}"}],
            },
            {"run_id": "abc123"},
        )
        self.assertEqual(rendered["label"], "dock_abc123")
        self.assertEqual(rendered["items"][0], "bench-abc123")
        self.assertEqual(rendered["items"][1]["owner"], "Jordan-abc123")

    def test_marker_helpers_handle_all_any_and_none(self) -> None:
        text = "I researched step 2 and the next step is bounded."
        self.assertTrue(_contains_all(text, ["researched step 2", "next step"]))
        self.assertTrue(_contains_any(text, ["bounded", "missing"]))
        self.assertTrue(_contains_none(text, ["got it:", "direct answer:"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
