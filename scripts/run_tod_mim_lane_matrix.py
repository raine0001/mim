from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as urllib_request


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _wait_for_http(url: str, *, timeout_seconds: float) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib_request.urlopen(urllib_request.Request(url, method="GET"), timeout=2) as response:
                if int(response.status) < 500:
                    return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def _get_json(url: str) -> dict[str, object]:
    with urllib_request.urlopen(urllib_request.Request(url, method="GET"), timeout=8) as response:
        payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {"data": payload}


def _run_command(command: list[str], *, cwd: Path) -> dict[str, object]:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _start_local_mim_server(port: int) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "core.app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        _wait_for_http(f"http://127.0.0.1:{port}/mim/arm/execution-target", timeout_seconds=30)
        return process
    except Exception:
        stdout, stderr = process.communicate(timeout=5) if process.poll() is not None else ("", "")
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        raise RuntimeError(f"failed to start local MIM server on port {port}: {stderr or stdout}")


def _lane_record(name: str, result: dict[str, object]) -> dict[str, object]:
    passed = int(result.get("returncode", 1)) == 0
    return {
        "name": name,
        "passed": passed,
        "returncode": int(result.get("returncode", 1)),
        "command": result.get("command"),
        "stdout": result.get("stdout"),
        "stderr": result.get("stderr"),
    }


def _capability_note(command_name: str, capability: dict[str, object]) -> str:
    transport_support = capability.get("transport_support") if isinstance(capability.get("transport_support"), dict) else {}
    mim_arm_support = str(transport_support.get("mim_arm", "unsupported"))
    available = bool(capability.get("available"))
    if available and mim_arm_support == "supported":
        return "supported/live-backed"
    if mim_arm_support == "supported":
        return "contract-supported, transport-disabled"
    return "contract-defined, transport-unsupported"


def _build_capability_summary(base_url: str) -> dict[str, object]:
    profile = _get_json(f"{base_url.rstrip('/')}/mim/arm/execution-target")
    command_capabilities = profile.get("command_capabilities") if isinstance(profile.get("command_capabilities"), dict) else {}
    interesting_commands = ["move_home", "move_relative", "move_relative_then_set_gripper", "pick_and_place", "pick_at", "place_at", "set_gripper", "stop", "set_speed"]
    highlighted = []
    for command_name in interesting_commands:
        capability = command_capabilities.get(command_name)
        if not isinstance(capability, dict):
            continue
        highlighted.append(
            {
                "command": command_name,
                "note": _capability_note(command_name, capability),
                "available": bool(capability.get("available")),
                "transport_mode": capability.get("transport_mode"),
            }
        )
    return {
        "target": profile.get("target"),
        "execution_mode": profile.get("execution_mode"),
        "live_transport_available": bool(profile.get("live_transport_available")),
        "highlighted_commands": highlighted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the TOD↔MIM synthetic gate plus the live arm validation lanes and print a PASS/FAIL matrix.")
    parser.add_argument("--shared-root", default="runtime/shared", help="Shared root passed to the live arm scripts.")
    parser.add_argument("--base-url", default="", help="Existing MIM base URL for the live smoke. If omitted, a temporary local server is started.")
    args = parser.parse_args()

    lanes: list[dict[str, object]] = []
    local_server: subprocess.Popen[str] | None = None
    base_url = str(args.base_url or "").strip()
    capability_summary: dict[str, object] = {}
    live_scenarios = [
        {
            "lane": "mim_arm_live_transport_check",
            "command": "move_to",
            "note": "No-op pose replay verifies the baseline live transport path without changing the current pose.",
        },
        {
            "lane": "mim_arm_live_relative_transport_check",
            "command": "move_relative",
            "args": {"dx": 5, "dy": -5, "dz": 0},
            "note": "Small bounded relative delta verifies that live execution resolves against the latest host pose before projecting servo motion.",
        },
        {
            "lane": "mim_arm_live_relative_z_transport_check",
            "command": "move_relative",
            "args": {"dx": 0, "dy": 0, "dz": 5},
            "note": "Bounded z-axis delta verifies the third motion axis on live transport and keeps the relative proof three-dimensional.",
        },
        {
            "lane": "mim_arm_live_compound_transport_check",
            "command": "move_relative_then_set_gripper",
            "args": {"dx": 5, "dy": -5, "dz": 0, "position": 40},
            "note": "First compound slice validates ordered relative motion followed by bounded gripper adjustment on the same execution envelope.",
        },
        {
            "lane": "mim_arm_live_pick_at_transport_check",
            "command": "pick_at",
            "note": "Bounded pick_at macro validates truthful phase reporting, partial-completion semantics, and the first scripted grasp slice on live transport.",
        },
        {
            "lane": "mim_arm_live_pick_and_place_transport_check",
            "command": "pick_and_place",
            "note": "Bounded pick_and_place macro validates composed truthful transfer phases without claiming broader object intelligence.",
        },
        {
            "lane": "mim_arm_live_place_at_transport_check",
            "command": "place_at",
            "note": "Bounded place_at macro validates truthful phase reporting, partial-completion semantics, and the first scripted release slice on live transport.",
        },
        {
            "lane": "tod_mim_arm_live_execution_smoke",
            "command": "move_relative",
            "args": {"dx": 5, "dy": -5, "dz": 0},
            "note": "Producer-backed end-to-end smoke validates relative motion through the TOD producer and MIM HTTP surface.",
        },
        {
            "lane": "tod_mim_arm_pick_at_live_execution_smoke",
            "command": "pick_at",
            "note": "Producer-backed end-to-end smoke validates the bounded pick_at macro through the TOD producer and MIM HTTP surface.",
        },
        {
            "lane": "tod_mim_arm_pick_and_place_live_execution_smoke",
            "command": "pick_and_place",
            "note": "Producer-backed end-to-end smoke validates the bounded pick_and_place transfer macro through the TOD producer and MIM HTTP surface.",
        },
        {
            "lane": "tod_mim_arm_place_at_live_execution_smoke",
            "command": "place_at",
            "note": "Producer-backed end-to-end smoke validates the bounded place_at macro through the TOD producer and MIM HTTP surface.",
        },
    ]

    try:
        lanes.append(
            _lane_record(
                "tod_mim_synthetic_contract_gate",
                _run_command(["bash", "scripts/run_tod_mim_contract_gate.sh"], cwd=PROJECT_ROOT),
            )
        )

        lanes.append(
            _lane_record(
                "mim_arm_live_transport_check",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_mim_arm_live_transport_check.py",
                        "--shared-root",
                        str(args.shared_root),
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )

        lanes.append(
            _lane_record(
                "mim_arm_live_relative_transport_check",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_mim_arm_live_transport_check.py",
                        "--shared-root",
                        str(args.shared_root),
                        "--command",
                        "move_relative",
                        "--dx",
                        "5",
                        "--dy",
                        "-5",
                        "--dz",
                        "0",
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )

        lanes.append(
            _lane_record(
                "mim_arm_live_relative_z_transport_check",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_mim_arm_live_transport_check.py",
                        "--shared-root",
                        str(args.shared_root),
                        "--command",
                        "move_relative",
                        "--dx",
                        "0",
                        "--dy",
                        "0",
                        "--dz",
                        "5",
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )

        lanes.append(
            _lane_record(
                "mim_arm_live_compound_transport_check",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_mim_arm_live_transport_check.py",
                        "--shared-root",
                        str(args.shared_root),
                        "--command",
                        "move_relative_then_set_gripper",
                        "--dx",
                        "5",
                        "--dy",
                        "-5",
                        "--dz",
                        "0",
                        "--position",
                        "40",
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )

        lanes.append(
            _lane_record(
                "mim_arm_live_pick_at_transport_check",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_mim_arm_live_transport_check.py",
                        "--shared-root",
                        str(args.shared_root),
                        "--command",
                        "pick_at",
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )

        lanes.append(
            _lane_record(
                "mim_arm_live_pick_and_place_transport_check",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_mim_arm_live_transport_check.py",
                        "--shared-root",
                        str(args.shared_root),
                        "--command",
                        "pick_and_place",
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )

        lanes.append(
            _lane_record(
                "mim_arm_live_place_at_transport_check",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_mim_arm_live_transport_check.py",
                        "--shared-root",
                        str(args.shared_root),
                        "--command",
                        "place_at",
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )

        if not base_url:
            port = _free_port()
            local_server = _start_local_mim_server(port)
            base_url = f"http://127.0.0.1:{port}"

        capability_summary = _build_capability_summary(base_url)

        lanes.append(
            _lane_record(
                "tod_mim_arm_live_execution_smoke",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_tod_mim_arm_live_smoke.py",
                        "--base-url",
                        base_url,
                        "--shared-root",
                        str(args.shared_root),
                        "--command",
                        "move_relative",
                        "--dx",
                        "5",
                        "--dy",
                        "-5",
                        "--dz",
                        "0",
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )

        lanes.append(
            _lane_record(
                "tod_mim_arm_pick_at_live_execution_smoke",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_tod_mim_arm_live_smoke.py",
                        "--base-url",
                        base_url,
                        "--shared-root",
                        str(args.shared_root),
                        "--command",
                        "pick_at",
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )

        lanes.append(
            _lane_record(
                "tod_mim_arm_pick_and_place_live_execution_smoke",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_tod_mim_arm_live_smoke.py",
                        "--base-url",
                        base_url,
                        "--shared-root",
                        str(args.shared_root),
                        "--command",
                        "pick_and_place",
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )

        lanes.append(
            _lane_record(
                "tod_mim_arm_place_at_live_execution_smoke",
                _run_command(
                    [
                        sys.executable,
                        "scripts/run_tod_mim_arm_live_smoke.py",
                        "--base-url",
                        base_url,
                        "--shared-root",
                        str(args.shared_root),
                        "--command",
                        "place_at",
                    ],
                    cwd=PROJECT_ROOT,
                ),
            )
        )
    finally:
        if local_server is not None:
            local_server.terminate()
            try:
                local_server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                local_server.kill()

    passed = all(bool(lane["passed"]) for lane in lanes)
    report = {
        "generated_at": _utcnow(),
        "summary": {
            "passed": passed,
            "total_lanes": len(lanes),
            "passed_lanes": sum(1 for lane in lanes if lane["passed"]),
            "failed_lanes": [lane["name"] for lane in lanes if not lane["passed"]],
        },
        "capability_summary": capability_summary,
        "live_scenarios": live_scenarios,
        "lanes": lanes,
    }

    print("lane | status")
    print("--- | ---")
    for lane in lanes:
        print(f"{lane['name']} | {'PASS' if lane['passed'] else 'FAIL'}")
    highlighted_commands = capability_summary.get("highlighted_commands") if isinstance(capability_summary.get("highlighted_commands"), list) else []
    if highlighted_commands:
        print()
        print("command | capability")
        print("--- | ---")
        for item in highlighted_commands:
            if not isinstance(item, dict):
                continue
            print(f"{item.get('command')} | {item.get('note')}")
    if live_scenarios:
        print()
        print("live lane | scenario")
        print("--- | ---")
        for item in live_scenarios:
            if not isinstance(item, dict):
                continue
            note = str(item.get("note") or "").strip()
            command = str(item.get("command") or "").strip()
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            arg_text = ""
            if args:
                arg_text = " " + " ".join(f"{key}={value}" for key, value in args.items())
            print(f"{item.get('lane')} | {command}{arg_text}: {note}")
    print()
    print(json.dumps(report, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())