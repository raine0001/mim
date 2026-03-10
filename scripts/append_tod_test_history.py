#!/usr/bin/env python3
import json
from datetime import datetime, timezone
from pathlib import Path


def _safe_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    summary_path = repo_root / "tod-tests-summary.json"
    history_dir = repo_root / "tod" / "history"
    history_file = history_dir / "tod-tests-history.json"
    dashboard_file = history_dir / "reliability-dashboard.json"

    if not summary_path.exists():
        raise SystemExit("tod-tests-summary.json not found; run scripts/run_tod_tests.py first")

    summary = json.loads(summary_path.read_text())
    history_dir.mkdir(parents=True, exist_ok=True)

    if history_file.exists():
        try:
            history = json.loads(history_file.read_text())
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
    else:
        history = []

    entry = {
        "timestamp": summary.get("timestamp", datetime.now(timezone.utc).isoformat()),
        "commit_sha": summary.get("commit_sha", "unknown"),
        "pass_count": int(summary.get("pass_count", 0)),
        "fail_count": int(summary.get("fail_count", 0)),
        "retry_count": int(summary.get("retry_count", 0)),
        "guardrail_blocks": int(summary.get("guardrail_blocks", 0)),
        "engine_stats": summary.get("engine_stats", {}),
    }
    history.append(entry)

    history = history[-500:]
    history_file.write_text(json.dumps(history, indent=2) + "\n")

    recent = history[-50:]
    total_runs = len(recent)
    pass_total = sum(int(row.get("pass_count", 0)) for row in recent)
    fail_total = sum(int(row.get("fail_count", 0)) for row in recent)
    retry_total = sum(int(row.get("retry_count", 0)) for row in recent)
    guardrail_total = sum(int(row.get("guardrail_blocks", 0)) for row in recent)
    test_total = pass_total + fail_total

    dashboard = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_runs": total_runs,
        "engine_success_rate": _safe_rate(pass_total, test_total),
        "engine_retry_rate": _safe_rate(retry_total, max(1, pass_total)),
        "guardrail_block_rate": _safe_rate(guardrail_total, max(1, total_runs)),
        "recovery_rate": _safe_rate(retry_total, max(1, test_total)),
        "average_latency": None,
    }
    dashboard_file.write_text(json.dumps(dashboard, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
