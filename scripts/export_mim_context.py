#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "runtime" / "shared"


def _fetch_json(url: str, timeout: float = 2.5) -> dict | None:
    try:
        with urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = resp.read().decode("utf-8")
            payload = json.loads(data)
            return payload if isinstance(payload, dict) else None
    except (URLError, TimeoutError, ValueError):
        return None


def _health(base_url: str) -> dict:
    payload = _fetch_json(f"{base_url}/health")
    if payload:
        return {
            "base_url": base_url,
            "reachable": True,
            "status": str(payload.get("status", "ok")),
        }
    return {
        "base_url": base_url,
        "reachable": False,
        "status": "unreachable",
    }


def _parse_objective_index(index_path: Path) -> tuple[str, str, str]:
    if not index_path.exists():
        return "0", "none", "1"

    rows: list[tuple[tuple[int, int], str, str]] = []
    for line in index_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        parts = [part.strip() for part in stripped.strip("|").split("|")]
        if len(parts) < 3:
            continue
        objective = parts[0]
        status = parts[2]
        match = re.fullmatch(r"(\d+)(?:\.(\d+))?", objective)
        if not match:
            continue
        major = int(match.group(1))
        minor = int(match.group(2) or 0)
        rows.append(((major, minor), objective, status))

    promoted = [row for row in rows if row[2] in {"promoted", "promoted_verified", "promoted_with_regression_exceptions"}]
    if promoted:
        promoted.sort(key=lambda item: item[0])
        latest_obj = promoted[-1][1]
    else:
        latest_obj = "0"

    major_part = int(latest_obj.split(".")[0]) if latest_obj.split(".")[0].isdigit() else 0
    next_obj = str(major_part + 1 if major_part > 0 else 1)

    most_recent_status = "none"
    if rows:
        rows.sort(key=lambda item: item[0])
        most_recent_status = rows[-1][2]

    return latest_obj, most_recent_status, next_obj


def _fallback_manifest_from_source(manifest_path: Path) -> dict:
    content = manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""

    def _extract(name: str, default: str) -> str:
        pattern = rf'{name}\s*=\s*"([^"]+)"'
        match = re.search(pattern, content)
        return match.group(1) if match else default

    schema = _extract("SCHEMA_VERSION", "unknown")
    return {
        "schema_version": schema,
        "release_tag": "unknown",
        "capabilities": [],
    }


def _to_yaml(value, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_to_yaml(item, indent + 2))
            else:
                serialized = json.dumps(item)
                lines.append(f"{prefix}{key}: {serialized}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(_to_yaml(item, indent + 2))
            else:
                serialized = json.dumps(item)
                lines.append(f"{prefix}- {serialized}")
        return "\n".join(lines)
    return f"{prefix}{json.dumps(value)}"


def build_payload() -> dict:
    prod_manifest = _fetch_json("http://127.0.0.1:8000/manifest")
    test_manifest = _fetch_json("http://127.0.0.1:18001/manifest")

    manifest = prod_manifest or test_manifest or _fallback_manifest_from_source(ROOT / "core" / "manifest.py")

    objective_active, latest_row_status, next_objective = _parse_objective_index(ROOT / "docs" / "objective-index.md")

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    capabilities = manifest.get("capabilities", []) if isinstance(manifest.get("capabilities", []), list) else []

    phase = "operational"
    if latest_row_status in {"implemented", "in_progress", "not_started"}:
        phase = "execution"

    payload = {
        "export_version": "mim-context-v2",
        "exported_at": now,
        "source_of_truth": {
            "objective_index": "docs/objective-index.md",
            "manifest_endpoint_priority": ["http://127.0.0.1:8000/manifest", "http://127.0.0.1:18001/manifest"],
            "manifest_source_fallback": "core/manifest.py",
        },
        "objective_active": objective_active,
        "phase": phase,
        "next_actions": [
            "finalize verification gate",
            f"begin objective {next_objective} planning",
        ],
        "latest_completed_objective": objective_active,
        "current_next_objective": next_objective,
        "schema_version": str(manifest.get("schema_version", "unknown")),
        "release_tag": str(manifest.get("release_tag", "unknown")),
        "capabilities": capabilities,
        "capability_count": len(capabilities),
        "health": {
            "prod": _health("http://127.0.0.1:8000"),
            "test": _health("http://127.0.0.1:18001"),
        },
        "blockers": [],
        "notes": [
            "Export regenerated from live manifest and objective index",
            "Replaces stale bootstrap snapshots (e.g., objective 17 warming phase)",
        ],
    }
    return payload


def write_exports(payload: dict, output_dir: Path, mirror_root: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "MIM_CONTEXT_EXPORT.latest.json"
    yaml_path = output_dir / "MIM_CONTEXT_EXPORT.latest.yaml"

    json_text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    yaml_text = _to_yaml(payload) + "\n"

    json_path.write_text(json_text, encoding="utf-8")
    yaml_path.write_text(yaml_text, encoding="utf-8")

    if mirror_root:
        (ROOT / "MIM_CONTEXT_EXPORT.latest.json").write_text(json_text, encoding="utf-8")
        (ROOT / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(yaml_text, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export current MIM context for shared sync consumers")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for latest export artifacts")
    parser.add_argument("--no-root-mirror", action="store_true", help="Do not mirror latest exports at repository root")
    args = parser.parse_args()

    payload = build_payload()
    write_exports(payload, Path(args.output_dir), mirror_root=not args.no_root_mirror)
    print(json.dumps({
        "written": [
            str(Path(args.output_dir) / "MIM_CONTEXT_EXPORT.latest.json"),
            str(Path(args.output_dir) / "MIM_CONTEXT_EXPORT.latest.yaml"),
        ],
        "objective_active": payload.get("objective_active"),
        "schema_version": payload.get("schema_version"),
        "release_tag": payload.get("release_tag"),
    }))
