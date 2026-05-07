#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import re
import shlex
from pathlib import Path


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not ENV_KEY_PATTERN.match(key):
            continue
        value = raw_value.strip()
        if value and value[0] in {'"', "'"} and value[-1:] == value[0]:
            try:
                parsed = ast.literal_eval(value)
            except Exception:
                parsed = value[1:-1]
            values[key] = str(parsed)
            continue
        if " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def export_lines(values: dict[str, str], keys: list[str]) -> str:
    lines: list[str] = []
    for key in keys:
        if not ENV_KEY_PATTERN.match(key):
            raise ValueError(f"invalid env key: {key}")
        if key not in values:
            continue
        lines.append(f"export {key}={shlex.quote(values[key])}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read dotenv files safely without shell source.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Emit shell-safe export lines for selected keys.")
    export_parser.add_argument("--file", required=True)
    export_parser.add_argument("--keys", nargs="+", required=True)

    get_parser = subparsers.add_parser("get", help="Read a single key from a dotenv file.")
    get_parser.add_argument("--file", required=True)
    get_parser.add_argument("key")
    get_parser.add_argument("--default", default="")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    values = parse_env_file(Path(args.file))

    if args.command == "export":
        output = export_lines(values, list(args.keys))
        if output:
            print(output)
        return 0

    print(values.get(args.key, args.default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
