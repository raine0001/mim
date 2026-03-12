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
PROMOTED_STATUSES = {"promoted", "promoted_verified", "promoted_with_regression_exceptions"}


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


def _health(base_urls: list[str]) -> dict:
    for base_url in base_urls:
        payload = _fetch_json(f"{base_url}/health")
        if payload:
            return {
                "base_url": base_url,
                "reachable": True,
                "status": str(payload.get("status", "ok")),
            }
    return {
        "base_url": base_urls[0] if base_urls else "unknown",
        "reachable": False,
        "status": "unreachable",
        "fallback_attempts": base_urls,
    }


def _parse_objective_index(index_path: Path) -> tuple[str, str | None, str, str]:
    if not index_path.exists():
        return "0", None, "1", "none"

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

    promoted = [row for row in rows if row[2] in PROMOTED_STATUSES]
    if promoted:
        promoted.sort(key=lambda item: item[0])
        latest_obj = promoted[-1][1]
    else:
        latest_obj = "0"

    in_flight_rows = [row for row in rows if row[2] not in PROMOTED_STATUSES]
    objective_in_flight: str | None = None
    if in_flight_rows:
        in_flight_rows.sort(key=lambda item: item[0])
        objective_in_flight = in_flight_rows[-1][1]

    major_part = int(latest_obj.split(".")[0]) if latest_obj.split(".")[0].isdigit() else 0
    next_obj = str(major_part + 1 if major_part > 0 else 1)

    most_recent_status = "none"
    if rows:
        rows.sort(key=lambda item: item[0])
        most_recent_status = rows[-1][2]

    return latest_obj, objective_in_flight, next_obj, most_recent_status


def _extract_first(text: str, pattern: str, default: str = "unknown") -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return str(match.group(1)).strip() if match else default


def _load_latest_maintenance_report() -> str:
    candidates = sorted(ROOT.glob("docs/maintenance-*-test-stack-reconciliation.md"))
    if not candidates:
        return ""
    return candidates[-1].read_text(encoding="utf-8")


def _verification_summary(objective_ref: str) -> dict:
    token = objective_ref.replace(".", "_")
    readiness_path = ROOT / "docs" / f"objective-{token}-promotion-readiness-report.md"
    prod_path = ROOT / "docs" / f"objective-{token}-prod-promotion-report.md"

    readiness_text = readiness_path.read_text(encoding="utf-8") if readiness_path.exists() else ""
    prod_text = prod_path.read_text(encoding="utf-8") if prod_path.exists() else ""
    maintenance_text = _load_latest_maintenance_report()

    regression_status = _extract_first(
        maintenance_text,
        r"Full Objective Regression \(Shared Test\).*?Result:\s*([A-Z]+)",
        default="unknown",
    ).upper()
    regression_tests = _extract_first(
        maintenance_text,
        r"Full Objective Regression \(Shared Test\).*?Result:\s*[A-Z]+\s*\(`?(\d+/\d+)`?\)",
        default="unknown",
    )

    readiness_decision = _extract_first(readiness_text, r"Decision:\s*([A-Z_]+)")
    prod_promotion = _extract_first(prod_text, r"Promotion:\s*([A-Z_]+)").upper()
    smoke_status = _extract_first(prod_text, r"Production Smoke:\s*([A-Z_]+)").upper()
    if smoke_status == "UNKNOWN":
        smoke_status = _extract_first(prod_text, r"###\s*Smoke.*?Result:\s*([A-Z]+)", default="unknown").upper()
    objective_probe = _extract_first(
        prod_text,
        r"Focused Objective\s+\d+\s+Probe on Production.*?Result:\s*([A-Z]+)",
        default="unknown",
    ).upper()

    return {
        "readiness_decision": readiness_decision,
        "prod_promotion_status": prod_promotion,
        "prod_smoke_status": smoke_status,
        "prod_objective_probe_status": objective_probe,
        "regression_status": regression_status,
        "regression_tests": regression_tests,
        "sources": {
            "readiness_report": str(readiness_path.relative_to(ROOT)) if readiness_path.exists() else "missing",
            "prod_report": str(prod_path.relative_to(ROOT)) if prod_path.exists() else "missing",
            "maintenance_report": "docs/maintenance-*-test-stack-reconciliation.md" if maintenance_text else "missing",
        },
    }


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
    manifest_sources = [
        "http://127.0.0.1:8000/manifest",
        "http://127.0.0.1:8001/manifest",
        "http://127.0.0.1:18001/manifest",
    ]
    manifest = None
    manifest_source_used = "core/manifest.py"
    for source in manifest_sources:
        payload = _fetch_json(source)
        if payload:
            manifest = payload
            manifest_source_used = source
            break
    if manifest is None:
        manifest = _fallback_manifest_from_source(ROOT / "core" / "manifest.py")

    latest_completed_objective, objective_in_flight, next_objective, latest_row_status = _parse_objective_index(
        ROOT / "docs" / "objective-index.md"
    )
    objective_active = objective_in_flight or latest_completed_objective
    verification = _verification_summary(latest_completed_objective)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    capabilities = manifest.get("capabilities", []) if isinstance(manifest.get("capabilities", []), list) else []

    phase = "operational"
    if latest_row_status in {"implemented", "in_progress", "not_started"}:
        phase = "execution"

    health_prod = _health(["http://127.0.0.1:8000"])
    health_test = _health(["http://127.0.0.1:8001", "http://127.0.0.1:18001"])

    blockers: list[str] = []
    if not health_prod.get("reachable", False):
        blockers.append("prod_unreachable")
    if not health_test.get("reachable", False):
        blockers.append("test_unreachable")
    if str(verification.get("regression_status", "unknown")).upper() not in {"PASS", "OK"}:
        blockers.append("regression_not_green")
    if str(verification.get("prod_promotion_status", "unknown")).upper() not in {"SUCCESS", "PASS"}:
        blockers.append("prod_verification_incomplete")

    payload = {
        "export_version": "mim-context-v2",
        "exported_at": now,
        "source_of_truth": {
            "objective_index": "docs/objective-index.md",
            "manifest_endpoint_priority": manifest_sources,
            "manifest_source_used": manifest_source_used,
            "manifest_source_fallback": "core/manifest.py",
        },
        "objective_active": objective_active,
        "objective_in_flight": objective_in_flight,
        "phase": phase,
        "next_actions": [
            "finalize verification gate",
            f"begin objective {next_objective} planning",
        ],
        "latest_completed_objective": latest_completed_objective,
        "latest_objective_index_status": latest_row_status,
        "current_next_objective": next_objective,
        "schema_version": str(manifest.get("schema_version", "unknown")),
        "release_tag": str(manifest.get("release_tag", "unknown")),
        "verification": verification,
        "capabilities": capabilities,
        "capability_count": len(capabilities),
        "health": {
            "prod": health_prod,
            "test": health_test,
        },
        "blockers": blockers,
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
