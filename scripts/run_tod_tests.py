#!/usr/bin/env python3
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _git_sha(repo_root: Path) -> str:
    try:
        return (
            subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True)
            .strip()
        )
    except Exception:
        return "unknown"


def _load_engine_stats(repo_root: Path) -> dict:
    state_file = repo_root / "tod" / "state" / "routing-metrics.json"
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text())
        if isinstance(data, dict):
            return data.get("engine_stats", {}) if isinstance(data.get("engine_stats", {}), dict) else {}
    except Exception:
        return {}
    return {}


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]

    cmd = [
        sys.executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        str(repo_root / "tests" / "tod"),
        "-p",
        "test_*.py",
    ]

    proc = subprocess.run(cmd, cwd=repo_root, text=True, capture_output=True)

    output = f"{proc.stdout}\n{proc.stderr}"
    ran = 0
    failures = 0
    errors = 0
    skipped = 0

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Ran ") and " test" in line:
            try:
                ran = int(line.split()[1])
            except Exception:
                pass
        if line.startswith("FAILED"):
            if "failures=" in line:
                try:
                    failures = int(line.split("failures=")[1].split(")")[0].split(",")[0])
                except Exception:
                    pass
            if "errors=" in line:
                try:
                    errors = int(line.split("errors=")[1].split(")")[0].split(",")[0])
                except Exception:
                    pass
        if "skipped=" in line:
            try:
                skipped = int(line.split("skipped=")[1].split(")")[0].split(",")[0])
            except Exception:
                pass

    fail_count = failures + errors
    pass_count = max(0, ran - fail_count - skipped)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "commit_sha": _git_sha(repo_root),
        "test_command": " ".join(cmd),
        "total_tests": ran,
        "pass_count": pass_count,
        "fail_count": fail_count,
        "skip_count": skipped,
        "retry_count": 0,
        "guardrail_blocks": 0,
        "engine_stats": _load_engine_stats(repo_root),
        "result": "pass" if proc.returncode == 0 else "fail",
    }

    (repo_root / "tod-tests-summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    sys.stdout.write(output)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
