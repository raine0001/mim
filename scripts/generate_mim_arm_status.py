#!/usr/bin/env python3
"""Materialize the bounded MIM arm status surface into a stable JSON artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.routers.mim_arm import DEFAULT_SHARED_ROOT, load_mim_arm_status_surface


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate mim_arm_status.latest.json from bounded local artifacts."
    )
    parser.add_argument(
        "--shared-root",
        default=str(DEFAULT_SHARED_ROOT),
        help="Directory containing the bounded arm/TOD shared artifacts.",
    )
    parser.add_argument(
        "--output",
        default="mim_arm_status.latest.json",
        help="Output file name or absolute path for the generated status artifact.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shared_root = Path(args.shared_root).expanduser().resolve()
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = shared_root / output_path

    payload = load_mim_arm_status_surface(shared_root=shared_root)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())