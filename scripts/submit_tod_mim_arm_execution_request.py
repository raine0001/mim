from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import request as urllib_request


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.execution_lane_service import build_tod_execution_request, tod_request_path
from core.tod_mim_contract import normalize_and_validate_file


def _utc_expiry(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _post_json(url: str, payload: dict[str, Any], timeout_seconds: int) -> tuple[int, dict[str, Any]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read().decode("utf-8", "replace")
        parsed = json.loads(raw) if raw else {}
        return int(response.status), parsed if isinstance(parsed, dict) else {"data": parsed}


def _response_indicates_acceptance(response: dict[str, Any]) -> bool:
    submission = response.get("submission") if isinstance(response.get("submission"), dict) else {}
    ack = submission.get("ack") if isinstance(submission.get("ack"), dict) else {}
    result = submission.get("result") if isinstance(submission.get("result"), dict) else {}

    ack_status = str(ack.get("ack_status") or ack.get("status") or "").strip().lower()
    if ack_status:
        return ack_status in {"accepted", "queued", "submitted", "ok"}

    result_status = str(result.get("result_status") or result.get("status") or "").strip().lower()
    if result_status:
        return result_status not in {"rejected", "failed", "error"}

    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Produce TOD-style execution-lane requests for MIM Arm.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="MIM base URL.")
    parser.add_argument("--shared-root", default="runtime/shared", help="Shared root for latest TOD request artifact.")
    parser.add_argument("--request-id", default="", help="Request id override.")
    parser.add_argument("--sequence", type=int, default=1, help="Request sequence.")
    parser.add_argument(
        "--command",
        required=True,
        choices=["move_to", "move_relative", "move_relative_then_set_gripper", "pick_at", "place_at", "pick_and_place", "move_home", "open_gripper", "close_gripper", "set_gripper", "set_speed", "stop"],
        help="Execution command name.",
    )
    parser.add_argument("--x", type=float, default=0.0, help="move_to x value.")
    parser.add_argument("--y", type=float, default=0.0, help="move_to y value.")
    parser.add_argument("--z", type=float, default=0.0, help="move_to z value.")
    parser.add_argument("--pick-x", type=float, default=0.0, help="pick_and_place source x value.")
    parser.add_argument("--pick-y", type=float, default=0.0, help="pick_and_place source y value.")
    parser.add_argument("--pick-z", type=float, default=0.0, help="pick_and_place source z value.")
    parser.add_argument("--place-x", type=float, default=0.0, help="pick_and_place destination x value.")
    parser.add_argument("--place-y", type=float, default=0.0, help="pick_and_place destination y value.")
    parser.add_argument("--place-z", type=float, default=0.0, help="pick_and_place destination z value.")
    parser.add_argument("--dx", type=float, default=0.0, help="move_relative dx value.")
    parser.add_argument("--dy", type=float, default=0.0, help="move_relative dy value.")
    parser.add_argument("--dz", type=float, default=0.0, help="move_relative dz value.")
    parser.add_argument("--position", type=float, default=100.0, help="set_gripper position percentage (0-100).")
    parser.add_argument("--level", default="normal", help="set_speed level.")
    parser.add_argument("--timeout-seconds", type=int, default=15, help="HTTP timeout for the MIM request.")
    parser.add_argument("--expiry-minutes", type=int, default=5, help="Request expiry horizon.")
    parser.add_argument("--supersedes-request-id", default="", help="Optional superseded request id.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request_id = str(args.request_id or f"tod-mim-arm-{uuid.uuid4().hex[:12]}").strip()
    command_args = {}
    if args.command == "move_to":
        command_args = {"x": args.x, "y": args.y, "z": args.z}
    elif args.command == "move_relative":
        command_args = {"dx": args.dx, "dy": args.dy, "dz": args.dz}
    elif args.command == "move_relative_then_set_gripper":
        command_args = {"dx": args.dx, "dy": args.dy, "dz": args.dz, "position": args.position}
    elif args.command == "pick_at":
        command_args = {"x": args.x, "y": args.y, "z": args.z}
    elif args.command == "place_at":
        command_args = {"x": args.x, "y": args.y, "z": args.z}
    elif args.command == "pick_and_place":
        command_args = {
            "pick_x": args.pick_x,
            "pick_y": args.pick_y,
            "pick_z": args.pick_z,
            "place_x": args.place_x,
            "place_y": args.place_y,
            "place_z": args.place_z,
        }
    elif args.command == "set_gripper":
        command_args = {"position": args.position}
    elif args.command == "set_speed":
        command_args = {"level": str(args.level or "").strip().lower()}

    payload = build_tod_execution_request(
        request_id=request_id,
        command_name=args.command,
        command_args=command_args,
        sequence=int(args.sequence),
        supersedes_request_id=str(args.supersedes_request_id or "").strip(),
        expires_at=_utc_expiry(int(args.expiry_minutes)),
        metadata_json={
            "producer": "tod",
            "lane": "mim_arm_execution",
            "task_id": request_id,
            "correlation_id": request_id,
        },
    )

    status_code, response = _post_json(
        f"{str(args.base_url).rstrip('/')}/mim/arm/execution-lane/requests",
        payload,
        timeout_seconds=int(args.timeout_seconds),
    )
    local_request_written = False
    local_request_path = ""
    if _response_indicates_acceptance(response):
        shared_root = Path(args.shared_root).expanduser().resolve()
        request_path = tod_request_path(shared_root)
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _, errors = normalize_and_validate_file(
            request_path,
            message_kind="request",
            service_name="submit_tod_mim_arm_execution_request.py",
        )
        if errors:
            raise RuntimeError(f"TOD↔MIM contract validation failed for execution request artifact: {errors}")
        local_request_written = True
        local_request_path = str(request_path)

    print(
        json.dumps(
            {
                "request": payload,
                "status_code": status_code,
                "response": response,
                "local_request_written": local_request_written,
                "local_request_path": local_request_path,
            },
            indent=2,
        )
    )
    return 0 if local_request_written else 1


if __name__ == "__main__":
    raise SystemExit(main())