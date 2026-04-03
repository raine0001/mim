#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    cmd = [
        sys.executable,
        "-m",
        "unittest",
        "tests.integration.test_tod_task_status_review",
        "tests.test_next_step_adjudication_service",
        "-v",
    ]
    completed = subprocess.run(cmd, cwd=repo_root)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())