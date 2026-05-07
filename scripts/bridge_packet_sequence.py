#!/usr/bin/env python3

import argparse
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import fcntl


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"next_sequence": 1}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"next_sequence": 1}
    if not isinstance(data, dict):
        return {"next_sequence": 1}
    try:
        next_sequence = int(data.get("next_sequence", 1))
    except Exception:
        next_sequence = 1
    if next_sequence < 1:
        next_sequence = 1
    data["next_sequence"] = next_sequence
    return data


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Allocate monotonic bridge packet sequence numbers."
    )
    parser.add_argument("--shared-dir", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--host")
    args = parser.parse_args()

    shared_dir = Path(args.shared_dir)
    shared_dir.mkdir(parents=True, exist_ok=True)

    state_path = shared_dir / ".bridge_sequence_state.json"
    lock_path = shared_dir / ".bridge_sequence_state.lock"
    host = args.host or socket.gethostname()

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        state = load_state(state_path)
        sequence = int(state["next_sequence"])
        emitted_at = utc_now()
        state.update(
            {
                "next_sequence": sequence + 1,
                "last_sequence": sequence,
                "updated_at": emitted_at,
                "updated_by": {
                    "source_host": host,
                    "source_service": args.service,
                    "source_instance_id": args.instance_id,
                },
            }
        )
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    sys.stdout.write(f"SEQUENCE={sequence}\n")
    sys.stdout.write(f"EMITTED_AT={emitted_at}\n")
    sys.stdout.write(f"SOURCE_HOST={host}\n")
    sys.stdout.write(f"SOURCE_SERVICE={args.service}\n")
    sys.stdout.write(f"SOURCE_INSTANCE_ID={args.instance_id}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
