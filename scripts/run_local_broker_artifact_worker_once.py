#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.local_broker_artifact_worker import consume_broker_request_artifact  # noqa: E402
from core.local_broker_boundary import LATEST_BROKER_REQUEST_ARTIFACT  # noqa: E402
from core.handoff_intake_service import DEFAULT_HANDOFF_ROOT, ensure_handoff_directories  # noqa: E402


def _resolve_request_artifact(argv: list[str]) -> Path:
    if len(argv) > 1 and str(argv[1] or "").strip():
        return Path(argv[1]).expanduser().resolve()
    handoff_root = Path(os.environ.get("MIM_HANDOFF_ROOT", str(DEFAULT_HANDOFF_ROOT))).expanduser().resolve()
    status_dir = ensure_handoff_directories(handoff_root=handoff_root)["status"]
    return (status_dir / LATEST_BROKER_REQUEST_ARTIFACT).resolve()


def _resolve_placeholder_intent(argv: list[str]) -> tuple[str | None, dict[str, object] | None]:
    tool_name = str(argv[2] or "").strip() if len(argv) > 2 else ""
    if not tool_name:
        return None, None
    if len(argv) > 3 and str(argv[3] or "").strip():
        return tool_name, json.loads(argv[3])
    return tool_name, {}


def main() -> int:
    request_artifact_path = _resolve_request_artifact(sys.argv)
    tool_name, arguments = _resolve_placeholder_intent(sys.argv)
    result = consume_broker_request_artifact(
        request_artifact_path=request_artifact_path,
        tool_name=tool_name,
        arguments=arguments,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())