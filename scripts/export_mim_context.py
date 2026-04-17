#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "runtime" / "shared"
PUBLICATION_BOUNDARY_STATUS_PATH = DEFAULT_OUTPUT_DIR / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json"
WORKSPACE_RUNTIME_BASE_URLS = ["http://127.0.0.1:18001"]
WORKSPACE_RUNTIME_MANIFEST_SOURCES = [
    f"{base_url}/manifest" for base_url in WORKSPACE_RUNTIME_BASE_URLS
]
PROD_RUNTIME_BASE_URL = "http://127.0.0.1:8000"
PROD_RUNTIME_MANIFEST_SOURCE = f"{PROD_RUNTIME_BASE_URL}/manifest"
PROMOTED_STATUSES = {
    "promoted",
    "promoted_verified",
    "promoted_with_regression_exceptions",
}
ACTIVE_IN_FLIGHT_STATUSES = {"implemented", "in_progress"}
DOC_COMPLETED_STATUSES = {"completed", *PROMOTED_STATUSES}
OBJECTIVE_TARGET_STATUSES = {*ACTIVE_IN_FLIGHT_STATUSES, *DOC_COMPLETED_STATUSES}
AUTHORITY_RESET_ARTIFACT_CANDIDATES = (
    ROOT / "objective_authority_reset.json",
    ROOT / "runtime" / "shared" / "objective_authority_reset.json",
    ROOT / "runtime" / "shared" / "OBJECTIVE_AUTHORITY_RESET.latest.json",
)
AUTHORITY_RESET_OBJECTIVE_KEYS = (
    "objective_ceiling",
    "reset_ceiling",
    "ceiling_objective",
    "ceiling",
    "max_objective",
    "max_authoritative_objective",
    "rollback_to_objective",
    "authoritative_objective",
    "current_objective",
    "objective",
    "objective_id",
)
AUTHORITY_RESET_REWRITE_KEYS = (
    "rewrite_completion_history",
    "rewrite_latest_completed",
    "rewrite_latest_completed_objective",
    "force_latest_completed_to_ceiling",
)


def _is_objective_status_source_doc(path: Path) -> bool:
    name = path.name.lower()
    return not any(fragment in name for fragment in ("report", "plan", "update"))


def _objective_sort_key(objective_ref: str | None) -> tuple[int, int]:
    text = str(objective_ref or "").strip().replace("_", ".")
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?", text)
    if not match:
        return (0, 0)
    return int(match.group(1)), int(match.group(2) or 0)


def _choose_newer_objective(*candidates: str | None) -> str | None:
    values = [
        str(candidate).strip()
        for candidate in candidates
        if str(candidate or "").strip()
    ]
    if not values:
        return None
    return max(values, key=_objective_sort_key)


def _normalize_objective_ref(value: object) -> str | None:
    text = str(value or "").strip().replace("_", ".")
    if not text:
        return None
    match = re.search(r"(\d+(?:[\._-]\d+)?)", text)
    if not match:
        return None
    return match.group(1).replace("_", ".").replace("-", ".")


def _parse_boolish(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _objective_exceeds_ceiling(
    objective_ref: str | None, ceiling_ref: str | None
) -> bool:
    objective = str(objective_ref or "").strip()
    ceiling = str(ceiling_ref or "").strip()
    if not objective or not ceiling:
        return False
    return _objective_sort_key(objective) > _objective_sort_key(ceiling)


def _cap_objective_to_ceiling(
    objective_ref: str | None, ceiling_ref: str | None
) -> str | None:
    objective = str(objective_ref or "").strip()
    ceiling = str(ceiling_ref or "").strip()
    if not objective:
        return objective_ref
    if _objective_exceeds_ceiling(objective, ceiling):
        return ceiling
    return objective


def _authority_reset_candidate_payloads(payload: dict | None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    candidates = [payload]
    for key in (
        "authority_reset",
        "objective_authority_reset",
        "rollback_authority",
        "shared_state",
        "metadata",
    ):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    return candidates


def _parse_objective_authority_reset(payload: dict | None, source: Path) -> dict | None:
    for candidate in _authority_reset_candidate_payloads(payload):
        ceiling_objective = None
        matched_key = ""
        for key in AUTHORITY_RESET_OBJECTIVE_KEYS:
            ceiling_objective = _normalize_objective_ref(candidate.get(key))
            if ceiling_objective:
                matched_key = key
                break
        if not ceiling_objective:
            continue

        enabled = None
        for key in ("active", "enabled", "applied"):
            parsed = _parse_boolish(candidate.get(key))
            if parsed is not None:
                enabled = parsed
                break
        if enabled is False:
            continue

        rewrite_completion_history = False
        for key in AUTHORITY_RESET_REWRITE_KEYS:
            parsed = _parse_boolish(candidate.get(key))
            if parsed is not None:
                rewrite_completion_history = parsed
                break

        try:
            source_label = str(source.relative_to(ROOT))
        except ValueError:
            source_label = str(source)

        return {
            "objective_ceiling": ceiling_objective,
            "rewrite_completion_history": rewrite_completion_history,
            "source": source_label,
            "matched_key": matched_key,
        }
    return None


def _authority_reset_artifact_candidates(output_dir: Path) -> tuple[Path, ...]:
    output_dir = output_dir.resolve()
    deduped: list[Path] = []
    candidates: tuple[Path, ...]
    if output_dir == DEFAULT_OUTPUT_DIR.resolve():
        candidates = (
            output_dir / "objective_authority_reset.json",
            output_dir / "OBJECTIVE_AUTHORITY_RESET.latest.json",
            *AUTHORITY_RESET_ARTIFACT_CANDIDATES,
        )
    else:
        candidates = (
            output_dir / "objective_authority_reset.json",
            output_dir / "OBJECTIVE_AUTHORITY_RESET.latest.json",
        )
    for path in candidates:
        if path not in deduped:
            deduped.append(path)
    return tuple(deduped)


def _load_objective_authority_reset(output_dir: Path) -> dict | None:
    for path in _authority_reset_artifact_candidates(output_dir):
        payload = _read_json_file(path)
        details = _parse_objective_authority_reset(payload, path)
        if details is not None:
            return details
    return None


def _boundary_request_objective(boundary_payload: dict | None) -> tuple[str | None, str]:
    if not isinstance(boundary_payload, dict):
        return None, ""
    for key in ("authoritative_request", "local_request", "remote_request"):
        request_payload = boundary_payload.get(key)
        if not isinstance(request_payload, dict):
            continue
        for field in ("objective_id", "task_id", "request_id"):
            objective_ref = _normalize_objective_ref(request_payload.get(field))
            if objective_ref:
                return objective_ref, f"{key}.{field}"
    return None, ""


def _infer_publication_boundary_authority_reset(
    *,
    output_dir: Path,
    latest_completed_objective: str | None,
    objective_in_flight: str | None,
    live_task_objective: str | None,
) -> dict | None:
    boundary_status_path = output_dir / "MIM_TOD_PUBLICATION_BOUNDARY.latest.json"
    boundary_payload = _read_json_file(boundary_status_path)
    boundary_objective, matched_key = _boundary_request_objective(boundary_payload)
    if not boundary_objective:
        return None
    if latest_completed_objective and boundary_objective != latest_completed_objective:
        return None
    if not (
        _objective_exceeds_ceiling(objective_in_flight, boundary_objective)
        or _objective_exceeds_ceiling(live_task_objective, boundary_objective)
    ):
        return None
    try:
        boundary_source = str(boundary_status_path.relative_to(ROOT))
    except ValueError:
        boundary_source = str(boundary_status_path)
    return {
        "objective_ceiling": boundary_objective,
        "rewrite_completion_history": False,
        "source": boundary_source,
        "matched_key": matched_key,
        "inferred_from": "publication_boundary_authoritative_request",
    }


def _schema_version_sort_key(value: object) -> tuple[int, int, int, int]:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})-(\d+)", text)
    if not match:
        return (0, 0, 0, 0)
    return tuple(int(part) for part in match.groups())


def _read_json_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _latest_live_task_request_signal(shared_dir: Path) -> dict:
    request_path = shared_dir / "MIM_TOD_TASK_REQUEST.latest.json"
    payload = _read_json_file(request_path)
    objective = None
    if isinstance(payload, dict):
        objective = _normalize_objective_ref(
            payload.get("objective_id") or payload.get("task_id")
        )
    try:
        source_label = str(request_path.relative_to(ROOT))
    except ValueError:
        source_label = str(request_path)
    return {
        "source": source_label,
        "objective": objective,
        "task_id": str(payload.get("task_id") or "").strip()
        if isinstance(payload, dict)
        else "",
        "available": bool(payload),
    }


def _fetch_json(
    url: str,
    timeout: float = 2.5,
    retries: int = 3,
    retry_delay_seconds: float = 0.35,
) -> dict | None:
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            with urlopen(url, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                data = resp.read().decode("utf-8")
                payload = json.loads(data)
                return payload if isinstance(payload, dict) else None
        except (URLError, TimeoutError, ValueError, OSError, ConnectionResetError):
            if attempt >= attempts - 1:
                return None
            time.sleep(retry_delay_seconds)
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


def _parse_objective_index(
    index_path: Path,
) -> tuple[str, str | None, str | None, str | None, str, str]:
    if not index_path.exists():
        return "0", None, None, None, "1", "none"

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
    latest_completed_status: str | None = None
    if promoted:
        promoted.sort(key=lambda item: item[0])
        latest_obj = promoted[-1][1]
        latest_completed_status = promoted[-1][2]
    else:
        latest_obj = "0"

    in_flight_rows = [row for row in rows if row[2] not in PROMOTED_STATUSES]
    objective_in_flight: str | None = None
    objective_in_flight_status: str | None = None
    if in_flight_rows:
        in_flight_rows.sort(key=lambda item: item[0])
        objective_in_flight = in_flight_rows[-1][1]
        objective_in_flight_status = in_flight_rows[-1][2]

    major_part = (
        int(latest_obj.split(".")[0]) if latest_obj.split(".")[0].isdigit() else 0
    )
    next_obj = str(major_part + 1 if major_part > 0 else 1)

    most_recent_status = "none"
    if rows:
        rows.sort(key=lambda item: item[0])
        most_recent_status = rows[-1][2]

    return (
        latest_obj,
        latest_completed_status,
        objective_in_flight,
        objective_in_flight_status,
        next_obj,
        most_recent_status,
    )


def _parse_objective_docs(
    docs_dir: Path,
) -> tuple[str | None, str | None, str | None, str | None, str]:
    if not docs_dir.exists():
        return None, None, None, None, "none"

    rows: list[tuple[tuple[int, int], str, str]] = []
    for path in docs_dir.glob("objective-*.md"):
        if not _is_objective_status_source_doc(path):
            continue
        match = re.match(r"objective-(\d+(?:[_\.]\d+)?)", path.name)
        if not match:
            continue
        objective = match.group(1).replace("_", ".")
        text = path.read_text(encoding="utf-8")
        status = (
            _extract_first(text, r"^Status:\s*([^\n]+)", default="").strip().lower()
        )
        if not status:
            continue
        rows.append((_objective_sort_key(objective), objective, status))

    if not rows:
        return None, None, None, None, "none"

    latest_completed: str | None = None
    latest_completed_status: str | None = None
    completed_rows = [row for row in rows if row[2] in DOC_COMPLETED_STATUSES]
    if completed_rows:
        newest_completed = max(completed_rows, key=lambda item: item[0])
        latest_completed = newest_completed[1]
        latest_completed_status = newest_completed[2]

    objective_in_flight: str | None = None
    objective_in_flight_status: str | None = None
    in_flight_rows = [row for row in rows if row[2] in ACTIVE_IN_FLIGHT_STATUSES]
    if in_flight_rows:
        newest = max(in_flight_rows, key=lambda item: item[0])
        objective_in_flight = newest[1]
        objective_in_flight_status = newest[2]

    most_recent_status = max(rows, key=lambda item: item[0])[2]
    return (
        latest_completed,
        latest_completed_status,
        objective_in_flight,
        objective_in_flight_status,
        most_recent_status,
    )


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

    readiness_text = (
        readiness_path.read_text(encoding="utf-8") if readiness_path.exists() else ""
    )
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
        smoke_status = _extract_first(
            prod_text, r"###\s*Smoke.*?Result:\s*([A-Z]+)", default="unknown"
        ).upper()
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
            "readiness_report": str(readiness_path.relative_to(ROOT))
            if readiness_path.exists()
            else "missing",
            "prod_report": str(prod_path.relative_to(ROOT))
            if prod_path.exists()
            else "missing",
            "maintenance_report": "docs/maintenance-*-test-stack-reconciliation.md"
            if maintenance_text
            else "missing",
        },
    }


def _fallback_manifest_from_source(manifest_path: Path) -> dict:
    content = (
        manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
    )

    def _extract(name: str, default: str) -> str:
        pattern = rf'{name}\s*=\s*"([^"]+)"'
        match = re.search(pattern, content)
        return match.group(1) if match else default

    schema = _extract("SCHEMA_VERSION", "unknown")
    return {
        "schema_version": schema,
        "release_tag": "unknown",
        "contract_version": "tod-mim-shared-contract-v1",
        "capabilities": [],
    }


def _manifest_from_shared_snapshot(snapshot_path: Path) -> dict | None:
    if not snapshot_path.exists():
        return None
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    manifest = payload.get("manifest")
    if isinstance(manifest, dict):
        return manifest
    return None


def _clean_target_value(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"n/a", "unknown", "not recorded", "none"}:
        return None
    return re.sub(r"\s*\(target\)\s*$", "", text).strip() or None


def _objective_target_from_index(
    index_path: Path, objective_ref: str | None
) -> dict | None:
    if not objective_ref or not index_path.exists():
        return None

    for line in index_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        parts = [part.strip() for part in stripped.strip("|").split("|")]
        if len(parts) < 5 or parts[0] != objective_ref:
            continue
        schema_version = _clean_target_value(parts[3])
        release_tag = _clean_target_value(parts[4])
        if not schema_version and not release_tag:
            return None
        return {
            "schema_version": schema_version,
            "release_tag": release_tag,
            "source": "docs/objective-index.md",
        }
    return None


def _objective_target_from_doc(objective_ref: str | None) -> dict | None:
    if not objective_ref:
        return None

    token = objective_ref.replace(".", "_")
    for path in sorted(ROOT.glob(f"docs/objective-{token}-*.md")):
        text = path.read_text(encoding="utf-8")
        if "Target Schema Version:" not in text and "Target Release Tag:" not in text:
            continue
        schema_version = _clean_target_value(
            _extract_first(text, r"Target Schema Version:\s*([^\n]+)", default="")
        )
        release_tag = _clean_target_value(
            _extract_first(text, r"Target Release Tag:\s*([^\n]+)", default="")
        )
        if not release_tag:
            release_tag = f"objective-{objective_ref}"
        if not schema_version and not release_tag:
            continue
        return {
            "schema_version": schema_version,
            "release_tag": release_tag,
            "source": str(path.relative_to(ROOT)),
        }
    return None


def _objective_target_metadata(
    index_path: Path, objective_ref: str | None, objective_status: str | None
) -> dict | None:
    if not objective_ref or objective_status not in OBJECTIVE_TARGET_STATUSES:
        return None
    doc_target = _objective_target_from_doc(objective_ref)
    index_target = _objective_target_from_index(index_path, objective_ref)
    target = doc_target or index_target
    if target is None:
        return None
    return {
        "objective": objective_ref,
        "status": objective_status,
        "schema_version": target.get("schema_version"),
        "release_tag": target.get("release_tag"),
        "source": target.get("source"),
    }


def _valid_manifest_candidate(payload: dict | None) -> bool:
    if not isinstance(payload, dict):
        return False
    schema_version = str(payload.get("schema_version", "")).strip()
    contract_version = str(payload.get("contract_version", "")).strip()
    capabilities = payload.get("capabilities")
    return bool(schema_version or contract_version or isinstance(capabilities, list))


def _manifest_candidate_summary(
    source: str, payload: dict | None, *, reason: str
) -> dict:
    return {
        "source": source,
        "valid": _valid_manifest_candidate(payload),
        "reason": reason,
        "schema_version": str(payload.get("schema_version", ""))
        if isinstance(payload, dict)
        else "",
        "release_tag": str(payload.get("release_tag", ""))
        if isinstance(payload, dict)
        else "",
        "contract_version": str(payload.get("contract_version", ""))
        if isinstance(payload, dict)
        else "",
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


def _resolve_manifest(
    objective_target: dict | None = None,
    *,
    prefer_prod_runtime: bool = False,
) -> tuple[dict, dict]:
    local_runtime_sources = list(WORKSPACE_RUNTIME_MANIFEST_SOURCES)
    prod_runtime_source = PROD_RUNTIME_MANIFEST_SOURCE
    shared_manifest_path = ROOT / "runtime" / "shared" / "MIM_MANIFEST.latest.json"
    manifest_candidate_diagnostics: list[dict] = []
    objective_target_status = (
        str(objective_target.get("status", "")).strip().lower()
        if isinstance(objective_target, dict)
        else ""
    )
    prefer_prod_runtime = prefer_prod_runtime or (
        objective_target_status in PROMOTED_STATUSES
    )

    selected_manifest: dict | None = None
    selected_base_source = ""
    selected_reason = ""

    preferred_runtime_sources = (
        [prod_runtime_source, *local_runtime_sources]
        if prefer_prod_runtime
        else list(local_runtime_sources)
    )

    for source in preferred_runtime_sources:
        payload = _fetch_json(source)
        candidate = _manifest_candidate_summary(
            source,
            payload,
            reason=(
                "promoted prod runtime endpoint"
                if payload and prefer_prod_runtime and source == prod_runtime_source
                else "workspace runtime endpoint"
                if payload
                else "unreachable_or_invalid"
            ),
        )
        manifest_candidate_diagnostics.append(candidate)
        if selected_manifest is None and candidate["valid"]:
            selected_manifest = payload
            selected_base_source = source
            if prefer_prod_runtime and source == prod_runtime_source:
                preferred_objective = ""
                if isinstance(objective_target, dict):
                    preferred_objective = str(
                        objective_target.get("objective") or ""
                    ).strip()
                selected_reason = (
                    f"selected promoted prod manifest from {source}"
                    + (
                        f" because objective {preferred_objective} is in a promoted state"
                        if preferred_objective
                        else " because prod runtime preference was explicitly requested"
                    )
                )
            else:
                selected_reason = f"selected freshest workspace/runtime manifest from {source} before considering stale prod runtime"

    snapshot_manifest = _manifest_from_shared_snapshot(shared_manifest_path)
    snapshot_source = str(shared_manifest_path.relative_to(ROOT))
    snapshot_candidate = _manifest_candidate_summary(
        snapshot_source,
        snapshot_manifest,
        reason="workspace shared snapshot"
        if snapshot_manifest
        else "missing_or_invalid_snapshot",
    )
    manifest_candidate_diagnostics.append(snapshot_candidate)
    if selected_manifest is None and snapshot_candidate["valid"]:
        selected_manifest = snapshot_manifest
        selected_base_source = snapshot_source
        selected_reason = "selected workspace shared snapshot because no fresher runtime manifest was valid"

    if prod_runtime_source not in preferred_runtime_sources:
        prod_manifest = _fetch_json(prod_runtime_source)
        prod_candidate = _manifest_candidate_summary(
            prod_runtime_source,
            prod_manifest,
            reason="prod runtime fallback" if prod_manifest else "unreachable_or_invalid",
        )
        manifest_candidate_diagnostics.append(prod_candidate)
        if selected_manifest is None and prod_candidate["valid"]:
            selected_manifest = prod_manifest
            selected_base_source = prod_runtime_source
            selected_reason = "fell back to stale prod runtime manifest because newer workspace/runtime sources were unavailable or invalid"

    fallback_source = "core/manifest.py"
    fallback_manifest = _fallback_manifest_from_source(ROOT / "core" / "manifest.py")
    fallback_candidate = _manifest_candidate_summary(
        fallback_source,
        fallback_manifest,
        reason="static source fallback",
    )
    manifest_candidate_diagnostics.append(fallback_candidate)
    if selected_manifest is None:
        selected_manifest = fallback_manifest
        selected_base_source = fallback_source
        selected_reason = "used static manifest fallback because no runtime or shared manifest source was valid"

    selected_manifest = dict(selected_manifest or {})
    fallback_schema = _clean_target_value(fallback_manifest.get("schema_version"))
    selected_schema = _clean_target_value(selected_manifest.get("schema_version"))
    if (
        fallback_schema
        and _schema_version_sort_key(fallback_schema)
        > _schema_version_sort_key(selected_schema)
    ):
        selected_manifest["schema_version"] = fallback_schema
        selected_reason = (
            f"{selected_reason}; overrode stale runtime/shared schema metadata with newer static schema_version "
            f"{fallback_schema} from {fallback_source}"
        )

    truth_source_used = selected_base_source
    if objective_target:
        target_schema = _clean_target_value(objective_target.get("schema_version"))
        target_release = _clean_target_value(objective_target.get("release_tag"))
        if target_schema:
            selected_manifest["schema_version"] = target_schema
        if target_release:
            selected_manifest["release_tag"] = target_release
        truth_source_used = str(objective_target.get("source") or selected_base_source)
        selected_reason = (
            f"{selected_reason}; applied in-flight objective target metadata for objective {objective_target.get('objective')} "
            f"from {truth_source_used} so exported manifest truth matches the current workspace objective target"
        )

    return selected_manifest, {
        "manifest_endpoint_priority": [
            *preferred_runtime_sources,
            snapshot_source,
            *([] if prod_runtime_source in preferred_runtime_sources else [prod_runtime_source]),
        ],
        "manifest_base_source_used": selected_base_source,
        "manifest_source_used": truth_source_used,
        "manifest_source_fallback": fallback_source,
        "manifest_source_selection_reason": selected_reason,
        "manifest_candidate_diagnostics": manifest_candidate_diagnostics,
    }


def build_payload_bundle(
    *, output_dir: Path = DEFAULT_OUTPUT_DIR, prefer_prod_runtime: bool = False
) -> tuple[dict, dict]:
    (
        index_latest_completed_objective,
        index_latest_completed_status,
        index_objective_in_flight,
        index_objective_in_flight_status,
        index_next_objective,
        index_latest_row_status,
    ) = _parse_objective_index(ROOT / "docs" / "objective-index.md")
    (
        docs_latest_completed_objective,
        docs_latest_completed_status,
        docs_objective_in_flight,
        docs_objective_in_flight_status,
        docs_latest_row_status,
    ) = _parse_objective_docs(ROOT / "docs")
    latest_completed_objective = (
        _choose_newer_objective(
            index_latest_completed_objective,
            docs_latest_completed_objective,
        )
        or index_latest_completed_objective
    )
    if latest_completed_objective == docs_latest_completed_objective:
        latest_completed_status = docs_latest_completed_status
    else:
        latest_completed_status = index_latest_completed_status

    objective_in_flight = _choose_newer_objective(
        index_objective_in_flight,
        docs_objective_in_flight,
    )
    if objective_in_flight == docs_objective_in_flight:
        objective_in_flight_status = docs_objective_in_flight_status
    else:
        objective_in_flight_status = index_objective_in_flight_status
    live_task_signal = _latest_live_task_request_signal(output_dir)
    live_task_objective = _normalize_objective_ref(live_task_signal.get("objective"))

    objective_authority_reset = _load_objective_authority_reset(output_dir)
    if objective_authority_reset is None:
        objective_authority_reset = _infer_publication_boundary_authority_reset(
            output_dir=output_dir,
            latest_completed_objective=latest_completed_objective,
            objective_in_flight=objective_in_flight,
            live_task_objective=live_task_objective,
        )
    authority_reset_ceiling = (
        str(objective_authority_reset.get("objective_ceiling") or "").strip()
        if isinstance(objective_authority_reset, dict)
        else ""
    )
    rewrite_completion_history = bool(
        objective_authority_reset.get("rewrite_completion_history")
        if isinstance(objective_authority_reset, dict)
        else False
    )
    if rewrite_completion_history:
        latest_completed_objective = _cap_objective_to_ceiling(
            latest_completed_objective,
            authority_reset_ceiling,
        )

    in_flight_suppressed_by_authority_reset = _objective_exceeds_ceiling(
        objective_in_flight, authority_reset_ceiling
    )
    if in_flight_suppressed_by_authority_reset:
        objective_in_flight = None
        objective_in_flight_status = None

    if (
        objective_in_flight
        and latest_completed_objective
        and _objective_sort_key(objective_in_flight)
        <= _objective_sort_key(latest_completed_objective)
    ):
        objective_in_flight = None
        objective_in_flight_status = None

    latest_row_status = (
        docs_latest_row_status
        if docs_latest_row_status != "none"
        else index_latest_row_status
    )

    live_task_suppressed_by_authority_reset = _objective_exceeds_ceiling(
        live_task_objective, authority_reset_ceiling
    )

    next_objective = index_next_objective
    if objective_in_flight and objective_in_flight_status in ACTIVE_IN_FLIGHT_STATUSES:
        next_objective = objective_in_flight
    elif latest_completed_objective:
        major_part = (
            int(str(latest_completed_objective).split(".")[0])
            if str(latest_completed_objective).split(".")[0].isdigit()
            else 0
        )
        next_objective = str(major_part + 1 if major_part > 0 else 1)
    if _objective_exceeds_ceiling(next_objective, authority_reset_ceiling):
        next_objective = authority_reset_ceiling

    objective_active = latest_completed_objective
    objective_active_source = "latest_completed_objective"
    if objective_in_flight and objective_in_flight_status in ACTIVE_IN_FLIGHT_STATUSES:
        objective_active = objective_in_flight
        objective_active_source = "objective_index_or_docs"
    if _objective_exceeds_ceiling(objective_active, authority_reset_ceiling):
        objective_active = authority_reset_ceiling
        objective_active_source = "objective_authority_reset"
    elif (
        authority_reset_ceiling
        and objective_active == authority_reset_ceiling
        and (
            in_flight_suppressed_by_authority_reset
            or live_task_suppressed_by_authority_reset
        )
    ):
        objective_active_source = "objective_authority_reset"

    if (
        live_task_objective
        and not live_task_suppressed_by_authority_reset
        and _objective_sort_key(live_task_objective) > _objective_sort_key(objective_active)
    ):
        objective_active = live_task_objective
        next_objective = live_task_objective
        objective_active_source = "live_task_request"

    objective_target_ref = objective_in_flight
    objective_target_status = objective_in_flight_status
    if not objective_target_ref and latest_completed_objective:
        objective_target_ref = latest_completed_objective
        objective_target_status = latest_completed_status or "completed"
    if (
        live_task_objective
        and not live_task_suppressed_by_authority_reset
        and _objective_sort_key(live_task_objective) > _objective_sort_key(objective_target_ref)
    ):
        objective_target_ref = live_task_objective
        objective_target_status = "implemented"
    if _objective_exceeds_ceiling(objective_target_ref, authority_reset_ceiling):
        if not _objective_exceeds_ceiling(latest_completed_objective, authority_reset_ceiling):
            objective_target_ref = latest_completed_objective
            objective_target_status = latest_completed_status or "completed"
        else:
            objective_target_ref = authority_reset_ceiling
            objective_target_status = objective_target_status or "completed"

    if (
        objective_target_ref
        and latest_completed_objective
        and objective_target_ref == latest_completed_objective
        and latest_completed_status in PROMOTED_STATUSES
    ):
        objective_target_status = latest_completed_status

    objective_target = _objective_target_metadata(
        ROOT / "docs" / "objective-index.md",
        objective_target_ref,
        objective_target_status,
    )
    manifest, manifest_source = _resolve_manifest(
        objective_target,
        prefer_prod_runtime=prefer_prod_runtime,
    )
    verification = _verification_summary(latest_completed_objective)

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    capabilities = (
        manifest.get("capabilities", [])
        if isinstance(manifest.get("capabilities", []), list)
        else []
    )

    phase = "operational"
    if objective_in_flight and objective_in_flight_status in ACTIVE_IN_FLIGHT_STATUSES:
        phase = "execution"

    health_prod = _health(["http://127.0.0.1:8000"])
    health_test = _health(["http://127.0.0.1:18001"])

    blockers: list[str] = []
    if not health_prod.get("reachable", False):
        blockers.append("prod_unreachable")
    if not health_test.get("reachable", False):
        blockers.append("test_unreachable")
    if str(verification.get("regression_status", "unknown")).upper() not in {
        "PASS",
        "OK",
    }:
        blockers.append("regression_not_green")
    if str(verification.get("prod_promotion_status", "unknown")).upper() not in {
        "SUCCESS",
        "PASS",
    }:
        blockers.append("prod_verification_incomplete")

    payload = {
        "export_version": "mim-context-v2",
        "exported_at": now,
        "source_of_truth": {
            "objective_index": "docs/objective-index.md",
            **manifest_source,
            "objective_target": objective_target,
            "live_task_request_signal": live_task_signal,
            "objective_active_source": objective_active_source,
            "objective_authority_reset": objective_authority_reset,
        },
        "objective_active": objective_active,
        "objective_in_flight": objective_in_flight,
        "phase": phase,
        "next_actions": (
            [
                f"continue objective {objective_active} execution",
                "refresh shared exports and handshake truth",
            ]
            if objective_in_flight
            and objective_in_flight_status in ACTIVE_IN_FLIGHT_STATUSES
            else [
                f"hold exported authority at objective {objective_active}",
                "refresh shared exports and handshake truth",
            ]
            if authority_reset_ceiling and objective_active == authority_reset_ceiling
            else [
                "finalize verification gate",
                f"begin objective {next_objective} planning",
            ]
        ),
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
    return payload, manifest


def build_payload() -> dict:
    payload, _ = build_payload_bundle()
    return payload


def _execution_truth_projection_sources(source_of_truth: dict) -> list[str]:
    preferred = str(source_of_truth.get("manifest_base_source_used") or "").strip()
    candidates: list[str] = []
    if preferred.startswith("http://") or preferred.startswith("https://"):
        candidates.append(preferred.rsplit("/manifest", 1)[0])

    for candidate in [
        "http://127.0.0.1:18001",
        "http://127.0.0.1:18003",
        "http://127.0.0.1:8000",
    ]:
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _execution_truth_bridge_artifacts(payload: dict) -> dict[str, dict]:
    exported_at = str(
        payload.get("exported_at")
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    source_of_truth = (
        payload.get("source_of_truth")
        if isinstance(payload.get("source_of_truth"), dict)
        else {}
    )
    projection_path = "/gateway/capabilities/executions/truth/latest?limit=10"
    projection = None
    projection_source = ""
    attempted_sources: list[str] = []
    for base_url in _execution_truth_projection_sources(source_of_truth):
        attempted_sources.append(base_url)
        projection = _fetch_json(f"{base_url}{projection_path}")
        if (
            isinstance(projection, dict)
            and str(projection.get("packet_type", "")).strip()
            == "tod-execution-truth-bridge-v1"
        ):
            projection_source = f"{base_url}{projection_path}"
            break
        projection = None

    if projection is None:
        projection = {
            "generated_at": exported_at,
            "packet_type": "tod-execution-truth-bridge-v1",
            "contract": "execution_truth_v1",
            "source": "unavailable",
            "summary": {
                "execution_count": 0,
                "capabilities": [],
                "deviation_signal_count": 0,
                "deviation_signals": [],
                "recent_executions": [],
            },
            "recent_execution_truth": [],
        }

    projection["bridge_publication"] = {
        "published_at": exported_at,
        "canonical_file": "TOD_EXECUTION_TRUTH.latest.json",
        "legacy_alias_file": "TOD_execution_truth.latest.json",
        "projection_source": projection_source or "unavailable",
        "attempted_sources": attempted_sources,
    }

    return {
        "TOD_EXECUTION_TRUTH.latest.json": projection,
        "TOD_execution_truth.latest.json": projection,
    }


def build_bridge_artifacts(
    payload: dict, manifest: dict, output_dir: Path
) -> dict[str, dict]:
    exported_at = str(
        payload.get("exported_at")
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    objective_active = str(payload.get("objective_active") or "unknown")
    latest_completed_objective = str(
        payload.get("latest_completed_objective") or "unknown"
    )
    current_next_objective = str(payload.get("current_next_objective") or "unknown")
    schema_version = str(
        payload.get("schema_version") or manifest.get("schema_version") or "unknown"
    )
    release_tag = str(
        payload.get("release_tag") or manifest.get("release_tag") or "unknown"
    )
    contract_version = str(
        manifest.get("contract_version") or "tod-mim-shared-contract-v1"
    )
    blockers = (
        payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    )
    verification = (
        payload.get("verification")
        if isinstance(payload.get("verification"), dict)
        else {}
    )
    source_of_truth = (
        payload.get("source_of_truth")
        if isinstance(payload.get("source_of_truth"), dict)
        else {}
    )

    handshake = {
        "handshake_version": "mim-tod-shared-export-v1",
        "generated_at": exported_at,
        "mim_shared_export_root": str(output_dir),
        "required_files": [
            str(output_dir / "MIM_CONTEXT_EXPORT.latest.json"),
            str(output_dir / "MIM_CONTEXT_EXPORT.latest.yaml"),
        ],
        "mirror_files": [
            str(ROOT / "MIM_CONTEXT_EXPORT.latest.json"),
            str(ROOT / "MIM_CONTEXT_EXPORT.latest.yaml"),
        ],
        "truth": {
            "objective_active": objective_active,
            "latest_completed_objective": latest_completed_objective,
            "current_next_objective": current_next_objective,
            "schema_version": schema_version,
            "release_tag": release_tag,
            "contract_version": contract_version,
            "regression_status": str(
                verification.get("regression_status") or "unknown"
            ),
            "regression_tests": str(verification.get("regression_tests") or "unknown"),
            "prod_promotion_status": str(
                verification.get("prod_promotion_status") or "unknown"
            ),
            "prod_smoke_status": str(
                verification.get("prod_smoke_status") or "unknown"
            ),
            "blockers": blockers,
        },
        "source_of_truth": source_of_truth,
    }

    alignment_request = {
        "generated_at": exported_at,
        "packet_type": "mim-tod-alignment-request-v1",
        "from_system": "MIM",
        "to_system": "TOD",
        "priority": "high",
        "mim_truth": {
            "objective_active": objective_active,
            "latest_completed_objective": latest_completed_objective,
            "current_next_objective": current_next_objective,
            "schema_version": schema_version,
            "release_tag": release_tag,
            "contract": contract_version,
        },
        "requested_actions": [
            f"Run TOD shared-folder refresh against {output_dir}",
            "Pull required files: MIM_CONTEXT_EXPORT.latest.json and MIM_CONTEXT_EXPORT.latest.yaml",
            "Pull optional files: MIM_MANIFEST.latest.json and MIM_TOD_HANDSHAKE_PACKET.latest.json",
            "Publish a fresh TOD_INTEGRATION_STATUS.latest.json after refresh",
            f"Resolve objective alignment mismatch: tod_current_objective must align to MIM objective_active={objective_active}",
        ],
        "success_criteria": {
            "compatible": True,
            "objective_alignment_status": "aligned",
            "tod_current_objective": objective_active,
            "mim_objective_active": objective_active,
            "mim_refresh_failure_reason": "",
            "mim_refresh_copied_manifest": True,
            "mim_handshake_available": True,
            "mim_schema": schema_version,
            "mim_release_tag": release_tag,
        },
        "notes": "MIM-side transport and producer truth are ready; TOD must publish refresh evidence showing copied manifest, handshake availability, and matching schema/release truth.",
    }

    manifest_snapshot = {
        "generated_at": exported_at,
        "source": str(
            source_of_truth.get("manifest_source_used") or "core/manifest.py"
        ),
        "base_source": str(
            source_of_truth.get("manifest_base_source_used")
            or source_of_truth.get("manifest_source_used")
            or "core/manifest.py"
        ),
        "source_reason": str(
            source_of_truth.get("manifest_source_selection_reason") or ""
        ),
        "manifest": manifest,
    }

    artifacts = {
        "MIM_MANIFEST.latest.json": manifest_snapshot,
        "MIM_TOD_HANDSHAKE_PACKET.latest.json": handshake,
        "MIM_TOD_ALIGNMENT_REQUEST.latest.json": alignment_request,
    }
    artifacts.update(_execution_truth_bridge_artifacts(payload))
    return artifacts


def write_exports(
    payload: dict, manifest: dict, output_dir: Path, mirror_root: bool
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "MIM_CONTEXT_EXPORT.latest.json"
    yaml_path = output_dir / "MIM_CONTEXT_EXPORT.latest.yaml"

    json_text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    yaml_text = _to_yaml(payload) + "\n"

    json_path.write_text(json_text, encoding="utf-8")
    yaml_path.write_text(yaml_text, encoding="utf-8")

    if mirror_root:
        (ROOT / "MIM_CONTEXT_EXPORT.latest.json").write_text(
            json_text, encoding="utf-8"
        )
        (ROOT / "MIM_CONTEXT_EXPORT.latest.yaml").write_text(
            yaml_text, encoding="utf-8"
        )

    bridge_artifacts = build_bridge_artifacts(payload, manifest, output_dir)
    for artifact_name, artifact_payload in bridge_artifacts.items():
        (output_dir / artifact_name).write_text(
            json.dumps(artifact_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Export current MIM context for shared sync consumers"
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory for latest export artifacts",
    )
    parser.add_argument(
        "--no-root-mirror",
        action="store_true",
        help="Do not mirror latest exports at repository root",
    )
    parser.add_argument(
        "--prefer-prod-runtime",
        action="store_true",
        help="Prefer the production manifest endpoint when resolving export metadata",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    payload, manifest = build_payload_bundle(
        output_dir=output_dir,
        prefer_prod_runtime=args.prefer_prod_runtime
    )
    write_exports(
        payload, manifest, output_dir, mirror_root=not args.no_root_mirror
    )
    print(
        json.dumps(
            {
                "written": [
                    str(Path(args.output_dir) / "MIM_CONTEXT_EXPORT.latest.json"),
                    str(Path(args.output_dir) / "MIM_CONTEXT_EXPORT.latest.yaml"),
                    str(Path(args.output_dir) / "MIM_MANIFEST.latest.json"),
                    str(Path(args.output_dir) / "MIM_TOD_HANDSHAKE_PACKET.latest.json"),
                    str(
                        Path(args.output_dir) / "MIM_TOD_ALIGNMENT_REQUEST.latest.json"
                    ),
                ],
                "objective_active": payload.get("objective_active"),
                "schema_version": payload.get("schema_version"),
                "release_tag": payload.get("release_tag"),
            }
        )
    )
