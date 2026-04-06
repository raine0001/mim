#!/usr/bin/env python3
"""Pull real arm-host state into runtime/shared/mim_arm_status.latest.json via SSH."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import sys

try:
    import paramiko
except Exception:  # pragma: no cover - optional dependency fallback
    paramiko = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_OUTPUT = PROJECT_ROOT / "runtime" / "shared" / "mim_arm_status.latest.json"
LOCAL_GENERATOR_SCRIPT = PROJECT_ROOT / "scripts" / "generate_mim_arm_host_state.py"
LOCAL_SHARED_ROOT = PROJECT_ROOT / "runtime" / "shared"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("MIM_ARM_SSH_HOST", "192.168.1.90"))
    parser.add_argument("--ssh-user", default=os.getenv("MIM_ARM_SSH_HOST_USER", os.getenv("MIM_ARM_SSH_USER", "testpilot")))
    parser.add_argument("--ssh-port", type=int, default=int(os.getenv("MIM_ARM_SSH_HOST_PORT", "22") or "22"))
    parser.add_argument("--remote-root", default="/home/testpilot/mim_arm/runtime/shared")
    parser.add_argument("--remote-script-path", default="/home/testpilot/mim_arm/runtime/tools/generate_mim_arm_host_state.py")
    parser.add_argument("--remote-output", default="/home/testpilot/mim_arm/runtime/shared/mim_arm_host_state.latest.json")
    parser.add_argument("--local-output", default=str(DEFAULT_LOCAL_OUTPUT))
    parser.add_argument("--password-env", default="MIM_ARM_SSH_HOST_PASS")
    parser.add_argument("--skip-remote-run", action="store_true")
    parser.add_argument(
        "--http-fallback",
        action="store_true",
        default=False,
        help="Run the state generator locally, probing the arm host via HTTP instead of SSH. "
        "Used when SSH auth is unavailable. Passes --sim-estop-ok to the generator.",
    )
    parser.add_argument(
        "--arm-api-port",
        type=int,
        default=5000,
        help="HTTP API port on the arm host (default 5000). Used with --http-fallback.",
    )
    return parser.parse_args()


def _resolve_password(password_env: str) -> str:
    return os.getenv(password_env, "") or os.getenv("MIM_ARM_SSH_HOST_PASS", "") or os.getenv("MIM_ARM_SSH_PASSWORD", "")


def _base_ssh_command(host: str, ssh_user: str, ssh_port: int, password_env: str) -> list[str]:
    password = _resolve_password(password_env)
    base = []
    if password:
        sshpass = shutil.which("sshpass")
        if sshpass:
            base.extend([sshpass, "-p", password])
    base.extend(["ssh", "-o", "ConnectTimeout=5", "-p", str(ssh_port)])
    if not password:
        base.extend(["-o", "BatchMode=yes"])
    base.append(f"{ssh_user}@{host}")
    return base


def _scp_base_command(host: str, ssh_user: str, ssh_port: int, password_env: str) -> list[str]:
    password = _resolve_password(password_env)
    base = []
    if password:
        sshpass = shutil.which("sshpass")
        if sshpass:
            base.extend([sshpass, "-p", password])
    base.extend(["scp", "-o", "ConnectTimeout=5", "-P", str(ssh_port)])
    if not password:
        base.extend(["-o", "BatchMode=yes"])
    return base + [f"{ssh_user}@{host}:"]


def _run_with_paramiko(*, host: str, ssh_user: str, ssh_port: int, password: str, remote_root: str, remote_script_path: str, remote_output: str, local_output: Path, skip_remote_run: bool) -> int:
    if paramiko is None:
        raise RuntimeError("Password-based SSH without sshpass requires paramiko. Install with '.venv/bin/pip install paramiko'.")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(hostname=host, username=ssh_user, password=password, port=ssh_port, timeout=8)
    try:
        sftp = client.open_sftp()
        try:
            remote_script = Path(remote_script_path)
            if not LOCAL_GENERATOR_SCRIPT.exists():
                raise RuntimeError(f"Local generator script is missing: {LOCAL_GENERATOR_SCRIPT}")
            remote_tools_dir = remote_script.parent
            client.exec_command(f"mkdir -p {remote_tools_dir}")[1].channel.recv_exit_status()
            # Keep the host generator aligned with local safety logic on every sync.
            sftp.put(str(LOCAL_GENERATOR_SCRIPT), str(remote_script))
        finally:
            sftp.close()

        if not skip_remote_run:
            command = f"python3 {remote_script_path} --shared-root {remote_root} --output {remote_output} --sim-estop-ok"
            _stdin, stdout, stderr = client.exec_command(command)
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                err = stderr.read().decode("utf-8", "replace").strip()
                raise RuntimeError(f"Remote host-state generation failed: {err or 'unknown error'}")

        sftp = client.open_sftp()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                temp_path = Path(tmpdir) / "mim_arm_host_state.latest.json"
                sftp.get(remote_output, str(temp_path))
                payload = json.loads(temp_path.read_text(encoding="utf-8"))
                local_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        finally:
            sftp.close()
    finally:
        client.close()
    return 0


def main() -> int:
    args = parse_args()
    local_output = Path(args.local_output).expanduser().resolve()
    local_output.parent.mkdir(parents=True, exist_ok=True)
    password = _resolve_password(args.password_env)

    # HTTP-direct fallback: run the generator locally against the arm host's HTTP API.
    # This avoids SSH entirely and is safe because no SSH credentials are used.
    if getattr(args, "http_fallback", False):
        arm_url = f"http://{args.host}:{args.arm_api_port}/arm_state"
        attribution_inputs = [
            LOCAL_SHARED_ROOT / "TOD_MIM_TASK_ACK.latest.json",
            LOCAL_SHARED_ROOT / "MIM_TOD_TASK_REQUEST.latest.json",
            LOCAL_SHARED_ROOT / "TOD_MIM_TASK_RESULT.latest.json",
            LOCAL_SHARED_ROOT / "TOD_AUTHORITY_SUMMARY.latest.json",
        ]
        generator_cmd = [
            sys.executable,
            str(LOCAL_GENERATOR_SCRIPT),
            "--shared-root", str(LOCAL_SHARED_ROOT),
            "--output", str(local_output),
            "--arm-url", arm_url,
            "--sim-estop-ok",
        ]
        for input_path in attribution_inputs:
            generator_cmd.extend(["--input-json", str(input_path)])
        result = subprocess.run(generator_cmd, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"HTTP-fallback generator failed (exit {result.returncode})")
        print(str(local_output))
        return 0

    # Use Paramiko fallback for password-based auth when sshpass is unavailable.
    if password and shutil.which("sshpass") is None:
        return _run_with_paramiko(
            host=args.host,
            ssh_user=args.ssh_user,
            ssh_port=args.ssh_port,
            password=password,
            remote_root=args.remote_root,
            remote_script_path=args.remote_script_path,
            remote_output=args.remote_output,
            local_output=local_output,
            skip_remote_run=bool(args.skip_remote_run),
        )

    if not args.skip_remote_run:
        remote_command = [
            *(_base_ssh_command(args.host, args.ssh_user, args.ssh_port, args.password_env)),
            f"python3 {args.remote_script_path} --shared-root {args.remote_root} --output {args.remote_output} --sim-estop-ok",
        ]
        subprocess.run(remote_command, check=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_path = Path(tmpdir) / "mim_arm_host_state.latest.json"
        scp_command = _scp_base_command(args.host, args.ssh_user, args.ssh_port, args.password_env)
        scp_command[-1] = f"{args.ssh_user}@{args.host}:{args.remote_output}"
        scp_command.append(str(temp_path))
        subprocess.run(scp_command, check=True)
        payload = json.loads(temp_path.read_text(encoding="utf-8"))
        local_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(str(local_output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())