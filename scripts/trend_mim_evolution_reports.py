#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_WATCH_TAGS = [
    "low_relevance",
    "response_loop_risk",
    "missing_safety_boundary",
    "repeated_clarifier_pattern",
    "context_drift",
    "clarification_spam",
]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _extract_point(path: Path, payload: dict[str, Any]) -> dict[str, Any] | None:
    conv = (
        payload.get("conversation", {})
        if isinstance(payload.get("conversation"), dict)
        else {}
    )
    actions = (
        payload.get("actions", {}) if isinstance(payload.get("actions"), dict) else {}
    )

    generated_raw = str(payload.get("generated_at") or "").strip()
    ts = None
    if generated_raw:
        normalized = generated_raw.replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(normalized)
        except Exception:
            ts = None

    if ts is None:
        try:
            ts = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except Exception:
            ts = datetime.now(timezone.utc)

    return {
        "file": str(path),
        "generated_at": ts.astimezone(timezone.utc),
        "overall": float(conv.get("overall", 0.0) or 0.0),
        "scenario_count": int(conv.get("scenario_count", 0) or 0),
        "failure_count": int(conv.get("failure_count", 0) or 0),
        "action_pass_ratio": float(actions.get("pass_ratio", 0.0) or 0.0),
        "action_failed": int(actions.get("failed", 0) or 0),
        "runtime_build": str(actions.get("runtime_build", "") or ""),
        "top_failures": conv.get("top_failures", [])
        if isinstance(conv.get("top_failures"), list)
        else [],
        "bucket_average": conv.get("bucket_average", {})
        if isinstance(conv.get("bucket_average"), dict)
        else {},
    }


def _linear_slope(points: list[dict[str, Any]], key: str) -> float:
    if len(points) < 2:
        return 0.0
    ys = [float(p.get(key, 0.0) or 0.0) for p in points]
    xs = list(range(len(ys)))
    x_mean = sum(xs) / len(xs)
    y_mean = sum(ys) / len(ys)
    denom = sum((x - x_mean) ** 2 for x in xs)
    if denom <= 0:
        return 0.0
    numer = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    return numer / denom


def _top_failure_counts(points: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for point in points:
        for item in point.get("top_failures", []):
            if not isinstance(item, dict):
                continue
            tag = str(item.get("tag", "")).strip()
            count = int(item.get("count", 0) or 0)
            if tag:
                counts[tag] = counts.get(tag, 0) + count
    return counts


def _point_tag_rate(point: dict[str, Any], tag: str) -> float:
    scenario_count = int(point.get("scenario_count", 0) or 0)
    if scenario_count <= 0:
        return 0.0
    for item in point.get("top_failures", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("tag", "")).strip() != tag:
            continue
        count = int(item.get("count", 0) or 0)
        return count / float(scenario_count)
    return 0.0


def _point_tag_count(point: dict[str, Any], tag: str) -> int:
    for item in point.get("top_failures", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("tag", "")).strip() == tag:
            return int(item.get("count", 0) or 0)
    return 0


def _point_bucket(point: dict[str, Any], bucket: str) -> float:
    value = point.get("bucket_average", {}).get(bucket, 0.0)
    return float(value or 0.0)


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _window_split(
    points: list[dict[str, Any]], window_points: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not points:
        return [], []
    size = max(1, min(window_points, len(points)))
    return points[:size], points[-size:]


def _hour_window(points: list[dict[str, Any]], first: bool) -> list[dict[str, Any]]:
    if not points:
        return []
    if first:
        start = points[0]["generated_at"]
        end = start.timestamp() + 3600
        return [p for p in points if p["generated_at"].timestamp() <= end]
    end = points[-1]["generated_at"]
    start = end.timestamp() - 3600
    return [p for p in points if p["generated_at"].timestamp() >= start]


def _tag_trends(
    points: list[dict[str, Any]], watch_tags: list[str], window_points: int
) -> list[dict[str, Any]]:
    first_window, last_window = _window_split(points, window_points)
    trends: list[dict[str, Any]] = []
    for tag in watch_tags:
        tag = str(tag).strip()
        if not tag:
            continue
        rates = [_point_tag_rate(p, tag) for p in points]
        counts = [_point_tag_count(p, tag) for p in points]
        baseline_rate = _average([_point_tag_rate(p, tag) for p in first_window])
        latest_rate = _average([_point_tag_rate(p, tag) for p in last_window])
        trends.append(
            {
                "tag": tag,
                "baseline_rate": baseline_rate,
                "latest_rate": latest_rate,
                "rate_delta": latest_rate - baseline_rate,
                "rate_ratio": (latest_rate / baseline_rate)
                if baseline_rate > 0
                else (999.0 if latest_rate > 0 else 1.0),
                "latest_count": counts[-1] if counts else 0,
                "total_count": sum(counts),
                "slope_per_run": _linear_slope([{"v": rate} for rate in rates], "v"),
            }
        )
    return trends


def _bucket_window_summary(points: list[dict[str, Any]]) -> dict[str, float]:
    buckets: set[str] = set()
    for p in points:
        for key in p.get("bucket_average", {}).keys():
            buckets.add(str(key))
    summary: dict[str, float] = {}
    for bucket in sorted(buckets):
        summary[bucket] = _average([_point_bucket(p, bucket) for p in points])
    return summary


def _hour_bucket_compare(points: list[dict[str, Any]]) -> dict[str, Any]:
    first_hour = _hour_window(points, first=True)
    last_hour = _hour_window(points, first=False)
    first_summary = _bucket_window_summary(first_hour)
    last_summary = _bucket_window_summary(last_hour)
    buckets = sorted(set(first_summary.keys()) | set(last_summary.keys()))
    delta = {
        bucket: float(last_summary.get(bucket, 0.0) - first_summary.get(bucket, 0.0))
        for bucket in buckets
    }
    return {
        "first_hour_points": len(first_hour),
        "last_hour_points": len(last_hour),
        "first_hour_bucket_average": first_summary,
        "last_hour_bucket_average": last_summary,
        "bucket_delta": delta,
    }


def _evaluate_alerts(
    *,
    points: list[dict[str, Any]],
    tag_trends: list[dict[str, Any]],
    max_overall_drop: float,
    min_action_pass_ratio: float,
    max_tag_rate_increase: float,
    max_tag_rate_ratio: float,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    if len(points) >= 2:
        overall_drop = float(points[0]["overall"] - points[-1]["overall"])
        if overall_drop > max_overall_drop:
            violations.append(
                {
                    "type": "overall_drop",
                    "threshold": max_overall_drop,
                    "value": overall_drop,
                    "message": f"overall dropped by {overall_drop:.6f} (> {max_overall_drop:.6f})",
                }
            )

    if points:
        latest_action_pass = float(points[-1].get("action_pass_ratio", 0.0) or 0.0)
        if latest_action_pass < min_action_pass_ratio:
            violations.append(
                {
                    "type": "action_pass_ratio_floor",
                    "threshold": min_action_pass_ratio,
                    "value": latest_action_pass,
                    "message": f"action pass ratio {latest_action_pass:.6f} < floor {min_action_pass_ratio:.6f}",
                }
            )

    for item in tag_trends:
        tag = str(item.get("tag", "") or "")
        baseline = float(item.get("baseline_rate", 0.0) or 0.0)
        delta = float(item.get("rate_delta", 0.0) or 0.0)
        ratio = float(item.get("rate_ratio", 1.0) or 1.0)
        ratio_triggered = baseline > 0.0 and ratio > max_tag_rate_ratio
        delta_triggered = delta > max_tag_rate_increase
        if delta_triggered or ratio_triggered:
            violations.append(
                {
                    "type": "tag_spike",
                    "tag": tag,
                    "threshold_delta": max_tag_rate_increase,
                    "threshold_ratio": max_tag_rate_ratio,
                    "value_delta": delta,
                    "value_ratio": ratio,
                    "message": (
                        f"tag '{tag}' spiked (delta={delta:.6f}, ratio={ratio:.4f})"
                    ),
                }
            )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Trend analyzer for MIM evolution summary reports"
    )
    parser.add_argument(
        "--history-dir", default="runtime/reports/mim_evolution_history"
    )
    parser.add_argument(
        "--output", default="runtime/reports/mim_evolution_trend_report.json"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Use only the latest N points (0 = all)"
    )
    parser.add_argument(
        "--window-points",
        type=int,
        default=30,
        help="Baseline/latest window size for tag drift analysis",
    )
    parser.add_argument(
        "--watch-tag",
        action="append",
        default=[],
        help="Failure tag to track (repeatable)",
    )
    parser.add_argument(
        "--max-overall-drop",
        type=float,
        default=0.01,
        help="Fail if overall drops by more than this amount",
    )
    parser.add_argument(
        "--min-action-pass-ratio",
        type=float,
        default=0.95,
        help="Fail if latest action pass ratio is below this floor",
    )
    parser.add_argument(
        "--max-tag-rate-increase",
        type=float,
        default=0.05,
        help="Fail if watched tag rate increase exceeds this absolute delta",
    )
    parser.add_argument(
        "--max-tag-rate-ratio",
        type=float,
        default=1.5,
        help="Fail if watched tag rate ratio exceeds this multiplier",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="Exit non-zero when regression alerts are triggered",
    )
    args = parser.parse_args()

    history_dir = Path(args.history_dir)
    output = Path(args.output)

    files = sorted(history_dir.glob("mim_evolution_training_summary_*.json"))
    if args.limit > 0:
        files = files[-int(args.limit) :]

    points: list[dict[str, Any]] = []
    for path in files:
        payload = _read_json(path)
        point = _extract_point(path, payload)
        if point:
            points.append(point)

    points.sort(key=lambda p: p["generated_at"])

    overall_slope = _linear_slope(points, "overall")
    failure_slope = _linear_slope(points, "failure_count")
    action_slope = _linear_slope(points, "action_pass_ratio")

    first = points[0] if points else None
    last = points[-1] if points else None
    watch_tags = []
    seen: set[str] = set()
    for tag in [*DEFAULT_WATCH_TAGS, *args.watch_tag]:
        normalized = str(tag).strip()
        if normalized and normalized not in seen:
            watch_tags.append(normalized)
            seen.add(normalized)

    tag_trends = _tag_trends(
        points, watch_tags, window_points=max(1, int(args.window_points))
    )
    violations = _evaluate_alerts(
        points=points,
        tag_trends=tag_trends,
        max_overall_drop=max(0.0, float(args.max_overall_drop)),
        min_action_pass_ratio=max(0.0, min(1.0, float(args.min_action_pass_ratio))),
        max_tag_rate_increase=max(0.0, float(args.max_tag_rate_increase)),
        max_tag_rate_ratio=max(1.0, float(args.max_tag_rate_ratio)),
    )

    trend = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "points": len(points),
        "window": {
            "first": first["generated_at"].isoformat() if first else None,
            "last": last["generated_at"].isoformat() if last else None,
        },
        "delta": {
            "overall": (last["overall"] - first["overall"]) if first and last else 0.0,
            "failure_count": (last["failure_count"] - first["failure_count"])
            if first and last
            else 0,
            "action_pass_ratio": (
                last["action_pass_ratio"] - first["action_pass_ratio"]
            )
            if first and last
            else 0.0,
        },
        "slope_per_run": {
            "overall": overall_slope,
            "failure_count": failure_slope,
            "action_pass_ratio": action_slope,
        },
        "latest": {
            "overall": last["overall"] if last else 0.0,
            "scenario_count": last["scenario_count"] if last else 0,
            "failure_count": last["failure_count"] if last else 0,
            "action_pass_ratio": last["action_pass_ratio"] if last else 0.0,
            "action_failed": last["action_failed"] if last else 0,
            "runtime_build": last["runtime_build"] if last else "",
            "file": last["file"] if last else "",
        },
        "aggregated_top_failures": sorted(
            [
                {"tag": tag, "count": count}
                for tag, count in _top_failure_counts(points).items()
            ],
            key=lambda item: item["count"],
            reverse=True,
        )[:12],
        "per_tag": tag_trends,
        "hour_window_comparison": _hour_bucket_compare(points),
        "alerts": {
            "status": "fail" if violations else "pass",
            "thresholds": {
                "max_overall_drop": float(args.max_overall_drop),
                "min_action_pass_ratio": float(args.min_action_pass_ratio),
                "max_tag_rate_increase": float(args.max_tag_rate_increase),
                "max_tag_rate_ratio": float(args.max_tag_rate_ratio),
                "window_points": int(args.window_points),
            },
            "watched_tags": watch_tags,
            "violations": violations,
        },
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(trend, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "points": len(points),
                "latest": trend["latest"],
                "alerts": trend["alerts"],
            },
            indent=2,
        )
    )

    if args.fail_on_regression and violations:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
