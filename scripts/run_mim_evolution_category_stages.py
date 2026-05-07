#!/usr/bin/env python3
"""Plan or execute staged per-category MIM evolution training runs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PLAN_PATH = Path("config/mim_evolution_category_plan.json")
DEFAULT_OUTPUT_PATH = Path("runtime/reports/mim_evolution_category_stage_plan.json")


def _slugify(value: str) -> str:
    cleaned = [ch.lower() if ch.isalnum() else "_" for ch in str(value)]
    slug = "".join(cleaned)
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug.strip("_") or "unknown"


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _stage_by_id(plan: dict[str, Any], stage_id: str) -> dict[str, Any]:
    stages = plan.get("stages", []) if isinstance(plan.get("stages"), list) else []
    for stage in stages:
        if isinstance(stage, dict) and str(stage.get("stage_id", "")).strip() == stage_id:
            return stage
    raise ValueError(f"Unknown stage_id: {stage_id}")


def build_stage_run_plan(
    *,
    repo_root: Path,
    plan: dict[str, Any],
    stage_id: str,
    base_url: str,
    python_bin: str,
    request_timeout_seconds: int,
) -> dict[str, Any]:
    stage = _stage_by_id(plan, stage_id)
    scenario_library = str(plan.get("scenario_library") or "conversation_scenarios/mim_evolution_training_set.json")
    profile_library = str(plan.get("profile_library") or "conversation_profiles_evolution.json")
    categories = plan.get("categories", []) if isinstance(plan.get("categories"), list) else []

    runs: list[dict[str, Any]] = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        category_id = str(category.get("category_id") or "general").strip() or "general"
        for split, target in (("train", int(stage.get("train_target") or 0)), ("holdout", int(stage.get("holdout_target") or 0))):
            output_path = repo_root / "runtime" / "reports" / "mim_evolution" / stage_id / f"{_slugify(category_id)}_{split}.json"
            command = [
                python_bin,
                str(repo_root / "conversation_eval_runner.py"),
                "--base-url",
                base_url,
                "--scenarios",
                str(repo_root / scenario_library),
                "--profiles",
                str(repo_root / profile_library),
                "--randomize",
                "--include-categories",
                category_id,
                "--include-splits",
                split,
                "--target-conversations",
                str(target),
                "--max-overall-drop",
                str(stage.get("max_overall_drop", 0.02)),
                "--max-failure-increase",
                str(stage.get("max_failure_increase", 10)),
                "--request-timeout-seconds",
                str(int(request_timeout_seconds)),
                "--output",
                str(output_path),
            ]
            runs.append(
                {
                    "category_id": category_id,
                    "description": str(category.get("description") or "").strip(),
                    "split": split,
                    "target_conversations": target,
                    "output": str(output_path),
                    "command": command,
                }
            )

    return {
        "stage_id": stage_id,
        "base_url": base_url,
        "scenario_library": str(repo_root / scenario_library),
        "profile_library": str(repo_root / profile_library),
        "train_target": int(stage.get("train_target") or 0),
        "holdout_target": int(stage.get("holdout_target") or 0),
        "max_overall_drop": float(stage.get("max_overall_drop") or 0.0),
        "max_failure_increase": int(stage.get("max_failure_increase") or 0),
        "runs": runs,
    }


def execute_stage_run_plan(stage_plan: dict[str, Any]) -> list[dict[str, Any]]:
    execution_results: list[dict[str, Any]] = []
    for run in stage_plan.get("runs", []):
        command = run.get("command", []) if isinstance(run.get("command"), list) else []
        output_path = Path(str(run.get("output") or ""))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        execution_results.append(
            {
                "category_id": run.get("category_id", "general"),
                "split": run.get("split", "train"),
                "returncode": int(completed.returncode),
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
    return execution_results


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan or execute staged per-category MIM evolution runs")
    parser.add_argument("--plan", default=str(DEFAULT_PLAN_PATH))
    parser.add_argument("--stage", default="pilot")
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--python-bin", default=sys.executable)
    parser.add_argument("--request-timeout-seconds", type=int, default=60)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    plan_path = (repo_root / str(args.plan)).resolve() if not Path(str(args.plan)).is_absolute() else Path(str(args.plan)).resolve()
    output_path = (repo_root / str(args.output)).resolve() if not Path(str(args.output)).is_absolute() else Path(str(args.output)).resolve()

    plan = _read_json(plan_path)
    stage_plan = build_stage_run_plan(
        repo_root=repo_root,
        plan=plan,
        stage_id=str(args.stage),
        base_url=str(args.base_url).rstrip("/"),
        python_bin=str(args.python_bin),
        request_timeout_seconds=int(args.request_timeout_seconds),
    )

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan_path": str(plan_path),
        "executed": bool(args.execute),
        "stage_plan": stage_plan,
    }
    if args.execute:
        report["execution_results"] = execute_stage_run_plan(stage_plan)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_path), "stage": stage_plan.get("stage_id"), "run_count": len(stage_plan.get("runs", [])), "executed": bool(args.execute)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())