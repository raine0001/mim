import tempfile
import unittest
from pathlib import Path

from scripts.env_file_tools import export_lines, parse_env_file


class EnvFileToolsTest(unittest.TestCase):
    def test_parse_env_file_reads_values_without_shell_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_file = Path(tmp_dir) / ".env"
            env_file.write_text(
                """
# comment
PLAIN=value
QUOTED="hello world"
SINGLE='quoted value'
INLINE=kept # trailing comment
export EXPORTED=1
BROKEN=$(echo nope)
""".strip()
                + "\n",
                encoding="utf-8",
            )

            values = parse_env_file(env_file)

        self.assertEqual(values["PLAIN"], "value")
        self.assertEqual(values["QUOTED"], "hello world")
        self.assertEqual(values["SINGLE"], "quoted value")
        self.assertEqual(values["INLINE"], "kept")
        self.assertEqual(values["EXPORTED"], "1")
        self.assertEqual(values["BROKEN"], "$(echo nope)")

    def test_export_lines_shell_quotes_values(self) -> None:
        exports = export_lines(
            {
                "SAFE": "plain",
                "SPACED": "hello world",
                "DOLLAR": "$(echo nope)",
            },
            ["SAFE", "SPACED", "DOLLAR"],
        )

        self.assertIn("export SAFE=plain", exports)
        self.assertIn("export SPACED='hello world'", exports)
        self.assertIn("export DOLLAR='$(echo nope)'", exports)


if __name__ == "__main__":
    unittest.main(verbosity=2)
