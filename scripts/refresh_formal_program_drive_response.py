#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_OUTPUT = ROOT / "runtime" / "formal_program_drive_response.json"


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def build_payload() -> dict:
    from core.autonomy_driver_service import build_initiative_status
    from core.db import SessionLocal

    async with SessionLocal() as db:
        status = await build_initiative_status(db=db)

    if not isinstance(status, dict):
        raise RuntimeError("initiative status unavailable")

    active_objective = status.get("active_objective") if isinstance(status.get("active_objective"), dict) else {}
    active_task = status.get("active_task") if isinstance(status.get("active_task"), dict) else {}
    active_project = status.get("active_project") if isinstance(status.get("active_project"), dict) else {}

    objective_id = active_objective.get("id") or active_objective.get("objective_id")
    if objective_id is None:
        raise RuntimeError("no active objective available to refresh formal program response")

    project_payload = {
        **active_project,
        "objective_id": objective_id,
        "status": str(active_project.get("status") or status.get("status") or status.get("execution_state") or "").strip(),
    }

    return {
        "generated_at": iso_now(),
        "source": "refresh_formal_program_drive_response_v1",
        "summary": str(status.get("summary") or "").strip(),
        "execution_state": str(status.get("execution_state") or status.get("status") or "").strip(),
        "objective": {
            **active_objective,
            "objective_id": objective_id,
        },
        "continuation": {
            "status": {
                "summary": str(status.get("summary") or "").strip(),
                "status": str(status.get("status") or "").strip(),
                "execution_state": str(status.get("execution_state") or status.get("status") or "").strip(),
                "active_task": active_task,
                "active_project": project_payload,
                "progress": status.get("progress") if isinstance(status.get("progress"), dict) else {},
                "next_task": status.get("next_task") if isinstance(status.get("next_task"), dict) else {},
                "why_current": str(status.get("why_current") or "").strip(),
                "blocked": status.get("blocked") if isinstance(status.get("blocked"), list) else [],
                "completed_recently": status.get("completed_recently") if isinstance(status.get("completed_recently"), list) else [],
            }
        },
        "program_status": status.get("program_status") if isinstance(status.get("program_status"), dict) else {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh runtime/formal_program_drive_response.json from live initiative state.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    payload = asyncio.run(build_payload())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "objective_id": payload.get("objective", {}).get("objective_id"),
        "project_id": payload.get("continuation", {}).get("status", {}).get("active_project", {}).get("project_id"),
        "task_id": payload.get("continuation", {}).get("status", {}).get("active_task", {}).get("id") or payload.get("continuation", {}).get("status", {}).get("active_task", {}).get("task_id"),
    }, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())