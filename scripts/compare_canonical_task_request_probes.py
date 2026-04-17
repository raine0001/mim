#!/usr/bin/env python3
"""Compare two canonical task-request probe outputs and classify divergence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


DEFAULT_FIELDS = [
    "hostname",
    "whoami",
    "absolute_path",
    "realpath",
    "ls_inode",
    "mtime",
    "size",
    "sha256",
    "objective_id",
    "task_id",
    "sequence",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", help="Path to the first probe JSON file.")
    parser.add_argument("right", help="Path to the second probe JSON file.")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser.parse_args()


def _read_json(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"probe output must be a JSON object: {path}")
    return payload


def _samples(report: dict[str, Any]) -> list[dict[str, Any]]:
    items = report.get("samples")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _stable(report: dict[str, Any], samples: list[dict[str, Any]]) -> bool:
    if isinstance(report.get("stable"), bool):
        return bool(report["stable"])
    hashes = {str(sample.get("sha256") or "") for sample in samples if str(sample.get("sha256") or "")}
    return len(hashes) <= 1


def _identity(sample: dict[str, Any]) -> dict[str, Any]:
    return {field: sample.get(field) for field in DEFAULT_FIELDS}


def _compare_fields(left: dict[str, Any], right: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for field in DEFAULT_FIELDS:
        result[field] = {
            "left": left.get(field),
            "right": right.get(field),
            "match": left.get(field) == right.get(field),
        }
    return result


def _sample_comparisons(left_samples: list[dict[str, Any]], right_samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    count = min(len(left_samples), len(right_samples))
    comparisons: list[dict[str, Any]] = []
    for index in range(count):
        left = _identity(left_samples[index])
        right = _identity(right_samples[index])
        comparisons.append(
            {
                "sample_index": index + 1,
                "matches": left == right,
                "fields": _compare_fields(left, right),
            }
        )
    return comparisons


def _classify(
    left_report: dict[str, Any],
    right_report: dict[str, Any],
    left_samples: list[dict[str, Any]],
    right_samples: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    left_stable = _stable(left_report, left_samples)
    right_stable = _stable(right_report, right_samples)
    if not left_stable or not right_stable:
        reasons.append("one_or_both_probe_streams_flipped_content_across_samples")
        return "unstable_or_flipping", reasons

    left_first = _identity(left_samples[0] if left_samples else {})
    right_first = _identity(right_samples[0] if right_samples else {})

    if left_first == right_first:
        reasons.append("all_canonical_identity_fields_match")
        return "identical", reasons

    same_absolute_path = left_first.get("absolute_path") == right_first.get("absolute_path")
    same_realpath = left_first.get("realpath") == right_first.get("realpath")
    same_inode = left_first.get("ls_inode") == right_first.get("ls_inode")
    same_hash = left_first.get("sha256") == right_first.get("sha256")

    if not same_absolute_path or not same_realpath:
        reasons.append("canonical_path_identity_differs")
        return "path_mismatch", reasons

    if same_inode and not same_hash:
        reasons.append("same_path_and_inode_reported_but_content_hash_differs")
        return "same_path_same_inode_different_hash", reasons

    if not same_inode and not same_hash:
        reasons.append("same_path_reported_but_inode_and_content_hash_both_differ")
        return "same_path_different_inode_different_hash", reasons

    if same_hash:
        reasons.append("canonical_content_matches_but_other_metadata_differs")
        return "same_content_metadata_differs", reasons

    reasons.append("stable_samples_match_by_path_but_not_by_content")
    return "stable_but_divergent", reasons


def build_report(left_path: str, right_path: str) -> dict[str, Any]:
    left_report = _read_json(left_path)
    right_report = _read_json(right_path)
    left_samples = _samples(left_report)
    right_samples = _samples(right_report)
    left_first = _identity(left_samples[0] if left_samples else {})
    right_first = _identity(right_samples[0] if right_samples else {})
    classification, reasons = _classify(left_report, right_report, left_samples, right_samples)
    sample_comparisons = _sample_comparisons(left_samples, right_samples)

    return {
        "type": "canonical_task_request_probe_comparison_v1",
        "left_probe": str(Path(left_path)),
        "right_probe": str(Path(right_path)),
        "left_transport": str(left_report.get("transport") or ""),
        "right_transport": str(right_report.get("transport") or ""),
        "left_stable": _stable(left_report, left_samples),
        "right_stable": _stable(right_report, right_samples),
        "left_samples": len(left_samples),
        "right_samples": len(right_samples),
        "classification": classification,
        "reasons": reasons,
        "left_identity": left_first,
        "right_identity": right_first,
        "field_comparison": _compare_fields(left_first, right_first),
        "sample_count_match": len(left_samples) == len(right_samples),
        "samples_compared": len(sample_comparisons),
        "sample_comparisons": sample_comparisons,
        "summary": {
            "same_absolute_path": left_first.get("absolute_path") == right_first.get("absolute_path"),
            "same_realpath": left_first.get("realpath") == right_first.get("realpath"),
            "same_inode": left_first.get("ls_inode") == right_first.get("ls_inode"),
            "same_sha256": left_first.get("sha256") == right_first.get("sha256"),
            "same_task_id": left_first.get("task_id") == right_first.get("task_id"),
            "same_objective_id": left_first.get("objective_id") == right_first.get("objective_id"),
            "same_sequence": left_first.get("sequence") == right_first.get("sequence"),
        },
    }


def main() -> int:
    args = parse_args()
    report = build_report(args.left, args.right)
    json.dump(report, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())