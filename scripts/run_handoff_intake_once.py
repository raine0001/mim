#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.handoff_intake_service import (  # noqa: E402
    DEFAULT_HANDOFF_ROOT,
    ensure_handoff_directories,
    ingest_one_handoff_artifact,
)


async def run_one_handoff_intake(
    *,
    handoff_root: Path | None = None,
    shared_root: Path | None = None,
) -> dict[str, object]:
    resolved_handoff_root = (
        handoff_root
        or Path(os.environ.get("MIM_HANDOFF_ROOT", str(DEFAULT_HANDOFF_ROOT))).expanduser().resolve()
    )
    resolved_shared_root = (
        shared_root
        or Path(os.environ.get("MIM_SHARED_ROOT", str(PROJECT_ROOT / "runtime" / "shared"))).expanduser().resolve()
    )
    ensure_handoff_directories(handoff_root=resolved_handoff_root)
    return await ingest_one_handoff_artifact(
        handoff_root=resolved_handoff_root,
        shared_root=resolved_shared_root,
    )


async def _run() -> dict[str, object]:
    handoff_root = Path(os.environ.get("MIM_HANDOFF_ROOT", str(DEFAULT_HANDOFF_ROOT))).expanduser().resolve()
    shared_root = Path(os.environ.get("MIM_SHARED_ROOT", str(PROJECT_ROOT / "runtime" / "shared"))).expanduser().resolve()
    ensure_handoff_directories(handoff_root=handoff_root)
    return await run_one_handoff_intake(handoff_root=handoff_root, shared_root=shared_root)


def main() -> int:
    result = asyncio.run(_run())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())