#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.next_step_adjudication_service import DEFAULT_SHARED_ROOT, publish_next_step_artifacts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate MIM next-step adjudication and consensus artifacts.")
    parser.add_argument(
        "--input",
        default=str(DEFAULT_SHARED_ROOT / "mim_codex_next_steps.latest.json"),
        help="Structured next-step input artifact.",
    )
    parser.add_argument(
        "--tod-adjudication",
        default="",
        help="Optional TOD adjudication artifact used during consensus merge.",
    )
    parser.add_argument(
        "--shared-root",
        default=str(DEFAULT_SHARED_ROOT),
        help="Shared root where adjudication and consensus artifacts are written.",
    )
    return parser.parse_args()


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return payload


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise SystemExit(f"Input artifact missing: {input_path}")
    next_steps_payload = _read_json(input_path)
    tod_payload = None
    if str(args.tod_adjudication or "").strip():
        tod_path = Path(args.tod_adjudication).expanduser().resolve()
        if not tod_path.exists():
            raise SystemExit(f"TOD adjudication artifact missing: {tod_path}")
        tod_payload = _read_json(tod_path)
    result = publish_next_step_artifacts(
        next_steps_payload=next_steps_payload,
        shared_root=Path(args.shared_root).expanduser().resolve(),
        tod_adjudication=tod_payload,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())