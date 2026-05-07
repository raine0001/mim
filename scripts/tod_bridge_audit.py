#!/usr/bin/env python3
"""Record durable audit events for TOD bridge artifact writes and publishes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG_DIR = PROJECT_ROOT / "runtime" / "logs"
DEFAULT_JSONL = DEFAULT_LOG_DIR / "tod_bridge_write_audit.jsonl"
DEFAULT_LATEST = DEFAULT_LOG_DIR / "tod_bridge_write_audit.latest.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_summary(path_str: str) -> dict[str, Any]:
    path = Path(path_str).expanduser()
    absolute_path = path if path.is_absolute() else (Path.cwd() / path)
    resolved_path = Path(os.path.realpath(str(absolute_path)))
    exists = resolved_path.exists()
    summary: dict[str, Any] = {
        "path": str(path_str),
        "absolute_path": str(absolute_path),
        "realpath": str(resolved_path),
        "exists": exists,
        "sha256": "",
        "size": None,
        "inode": None,
    }
    if exists:
        stat_result = resolved_path.stat()
        summary.update(
            {
                "sha256": _sha256(resolved_path),
                "size": stat_result.st_size,
                "inode": stat_result.st_ino,
            }
        )
    return summary


def build_event(args: argparse.Namespace) -> dict[str, Any]:
    artifacts = [_artifact_summary(item) for item in args.artifact_path]
    payload: dict[str, Any] = {
        "generated_at": _utcnow(),
        "type": "tod_bridge_write_audit_v1",
        "event": args.event,
        "caller": args.caller,
        "service_name": args.service_name,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "cwd": str(Path.cwd()),
        "task_id": args.task_id,
        "objective_id": args.objective_id,
        "publish_target": args.publish_target,
        "remote_host": args.remote_host,
        "remote_root": args.remote_root,
        "publish_attempted": _to_bool(args.publish_attempted),
        "publish_succeeded": _to_bool(args.publish_succeeded),
        "publish_returncode": args.publish_returncode,
        "publish_output": args.publish_output,
        "artifacts": artifacts,
    }
    if args.extra_json.strip():
        payload["extra"] = json.loads(args.extra_json)
    return payload


def write_event(event: dict[str, Any], *, jsonl_path: Path = DEFAULT_JSONL, latest_path: Path = DEFAULT_LATEST) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
    latest_path.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--caller", default="")
    parser.add_argument("--service-name", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--objective-id", default="")
    parser.add_argument("--publish-target", default="")
    parser.add_argument("--remote-host", default="")
    parser.add_argument("--remote-root", default="")
    parser.add_argument("--publish-attempted", default="false")
    parser.add_argument("--publish-succeeded", default="false")
    parser.add_argument("--publish-returncode", type=int, default=0)
    parser.add_argument("--publish-output", default="")
    parser.add_argument("--artifact-path", action="append", default=[])
    parser.add_argument("--extra-json", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    event = build_event(args)
    write_event(event)
    print(json.dumps(event, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())