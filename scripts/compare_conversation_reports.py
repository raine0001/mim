#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path) -> dict:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        return {}
    return data


def _summary(report: dict) -> dict:
    summary = (
        report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    )
    top_failures = (
        summary.get("top_failures", [])
        if isinstance(summary.get("top_failures"), list)
        else []
    )
    failure_map = {}
    for item in top_failures:
        if isinstance(item, dict):
            tag = str(item.get("tag", "")).strip()
            count = int(item.get("count", 0) or 0)
            if tag:
                failure_map[tag] = count
    return {
        "overall": float(summary.get("overall", 0.0) or 0.0),
        "scenario_count": int(summary.get("scenario_count", 0) or 0),
        "failure_count": int(summary.get("failure_count", 0) or 0),
        "bucket_average": summary.get("bucket_average", {})
        if isinstance(summary.get("bucket_average"), dict)
        else {},
        "failure_map": failure_map,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare two conversation eval reports (A/B)"
    )
    parser.add_argument("--a", required=True, help="Report A path")
    parser.add_argument("--b", required=True, help="Report B path")
    parser.add_argument(
        "--focus-tags",
        default="low_relevance,response_loop_risk,missing_safety_boundary",
    )
    args = parser.parse_args()

    report_a = _load(Path(args.a))
    report_b = _load(Path(args.b))
    a = _summary(report_a)
    b = _summary(report_b)

    tags = [item.strip() for item in str(args.focus_tags).split(",") if item.strip()]

    focused = []
    for tag in tags:
        a_count = int(a["failure_map"].get(tag, 0))
        b_count = int(b["failure_map"].get(tag, 0))
        focused.append(
            {
                "tag": tag,
                "a": a_count,
                "b": b_count,
                "delta": b_count - a_count,
                "improved": b_count < a_count,
            }
        )

    payload = {
        "report_a": str(args.a),
        "report_b": str(args.b),
        "overall": {
            "a": round(a["overall"], 4),
            "b": round(b["overall"], 4),
            "delta": round(b["overall"] - a["overall"], 4),
            "improved": b["overall"] > a["overall"],
        },
        "failure_count": {
            "a": a["failure_count"],
            "b": b["failure_count"],
            "delta": b["failure_count"] - a["failure_count"],
            "improved": b["failure_count"] < a["failure_count"],
        },
        "focused_tags": focused,
    }

    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
