from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib import request as urllib_request


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _get_json(url: str) -> dict:
    with urllib_request.urlopen(urllib_request.Request(url, method="GET"), timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {"data": payload}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an end-to-end live TOD -> MIM -> Arm -> MIM smoke using the execution-lane envelope.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="MIM base URL.")
    parser.add_argument("--shared-root", default="runtime/shared", help="Shared root for latest artifacts.")
    parser.add_argument(
        "--command",
        default="move_to",
        choices=["move_to", "move_relative", "move_relative_then_set_gripper", "pick_at", "place_at", "pick_and_place"],
        help="Execution-lane command to drive through the TOD producer.",
    )
    parser.add_argument("--x", type=float, default=None, help="pick_at/place_at target x value. Defaults to the current pose x.")
    parser.add_argument("--y", type=float, default=None, help="pick_at/place_at target y value. Defaults to the current pose y.")
    parser.add_argument("--z", type=float, default=None, help="pick_at/place_at target z value. Defaults to the current pose z.")
    parser.add_argument("--pick-x", type=float, default=None, help="pick_and_place source x value. Defaults to the current pose x.")
    parser.add_argument("--pick-y", type=float, default=None, help="pick_and_place source y value. Defaults to the current pose y.")
    parser.add_argument("--pick-z", type=float, default=None, help="pick_and_place source z value. Defaults to the current pose z.")
    parser.add_argument("--place-x", type=float, default=None, help="pick_and_place destination x value. Defaults to the current pose x.")
    parser.add_argument("--place-y", type=float, default=None, help="pick_and_place destination y value. Defaults to the current pose y.")
    parser.add_argument("--place-z", type=float, default=None, help="pick_and_place destination z value. Defaults to the current pose z.")
    parser.add_argument("--dx", type=float, default=5.0, help="move_relative dx delta.")
    parser.add_argument("--dy", type=float, default=-5.0, help="move_relative dy delta.")
    parser.add_argument("--dz", type=float, default=0.0, help="move_relative dz delta.")
    parser.add_argument("--position", type=float, default=40.0, help="Gripper position for the compound command.")
    parser.add_argument(
        "--arm-base-url",
        default=os.getenv("MIM_ARM_HTTP_BASE_URL", "http://192.168.1.90:5000"),
        help="Arm host base URL used to read the current pose before producing the TOD request.",
    )
    args = parser.parse_args()

    arm_state = _get_json(f"{str(args.arm_base_url).rstrip('/')}/arm_state")
    pose = arm_state.get("current_pose") or [90, 90, 90, 90, 90, 50]
    command = [
        str(PROJECT_ROOT / ".venv" / "bin" / "python"),
        str(PROJECT_ROOT / "scripts" / "submit_tod_mim_arm_execution_request.py"),
        "--base-url",
        str(args.base_url),
        "--shared-root",
        str(args.shared_root),
        "--command",
        str(args.command),
    ]
    if args.command == "move_to":
        command.extend(["--x", str(pose[0]), "--y", str(pose[1]), "--z", str(pose[2])])
    elif args.command == "move_relative":
        command.extend(["--dx", str(args.dx), "--dy", str(args.dy), "--dz", str(args.dz)])
    elif args.command == "move_relative_then_set_gripper":
        command.extend(
            [
                "--dx",
                str(args.dx),
                "--dy",
                str(args.dy),
                "--dz",
                str(args.dz),
                "--position",
                str(args.position),
            ]
        )
    elif args.command == "pick_at":
        command.extend(
            [
                "--x",
                str(args.x if args.x is not None else pose[0]),
                "--y",
                str(args.y if args.y is not None else pose[1]),
                "--z",
                str(args.z if args.z is not None else pose[2]),
            ]
        )
    elif args.command == "place_at":
        command.extend(
            [
                "--x",
                str(args.x if args.x is not None else pose[0]),
                "--y",
                str(args.y if args.y is not None else pose[1]),
                "--z",
                str(args.z if args.z is not None else pose[2]),
            ]
        )
    elif args.command == "pick_and_place":
        command.extend(
            [
                "--pick-x",
                str(args.pick_x if args.pick_x is not None else pose[0]),
                "--pick-y",
                str(args.pick_y if args.pick_y is not None else pose[1]),
                "--pick-z",
                str(args.pick_z if args.pick_z is not None else pose[2]),
                "--place-x",
                str(args.place_x if args.place_x is not None else pose[0]),
                "--place-y",
                str(args.place_y if args.place_y is not None else pose[1]),
                "--place-z",
                str(args.place_z if args.place_z is not None else pose[2]),
            ]
        )
    completed = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        print(completed.stdout)
        print(completed.stderr)
        return completed.returncode
    payload = json.loads(completed.stdout)
    report = {
        "check_type": "tod_mim_arm_live_smoke",
        "command": str(args.command),
        "passed": bool(
            payload.get("status_code") == 200
            and payload.get("response", {}).get("submission", {}).get("result", {}).get("result_status") == "succeeded"
        ),
        "producer_result": payload,
    }
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())