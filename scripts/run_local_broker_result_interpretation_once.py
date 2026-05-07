#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.handoff_intake_service import DEFAULT_HANDOFF_ROOT, ensure_handoff_directories  # noqa: E402
from core.local_broker_boundary import LATEST_BROKER_RESULT_ARTIFACT  # noqa: E402
from core.local_broker_result_artifact_interpretation_worker import (  # noqa: E402
    persist_broker_result_artifact_interpretation,
)


def _resolve_result_artifact(argv: list[str]) -> Path:
    if len(argv) > 1 and str(argv[1] or "").strip():
        return Path(argv[1]).expanduser().resolve()
    handoff_root = Path(os.environ.get("MIM_HANDOFF_ROOT", str(DEFAULT_HANDOFF_ROOT))).expanduser().resolve()
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    return (status_dir / LATEST_BROKER_RESULT_ARTIFACT).resolve()


def main() -> int:
    result_artifact_path = _resolve_result_artifact(sys.argv)
    result = persist_broker_result_artifact_interpretation(
        result_artifact_path=result_artifact_path,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())