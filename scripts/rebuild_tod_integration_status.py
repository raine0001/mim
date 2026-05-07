#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHARED_DIR = ROOT / "runtime" / "shared"
CANONICAL_COMMUNICATION_HOST = "192.168.1.120"
CANONICAL_COMMUNICATION_ROOT = str(DEFAULT_SHARED_DIR.resolve())
CANONICAL_COMMUNICATION_SURFACE = "mim_runtime_shared"
LOCAL_ONLY_REQUEST_SOURCES = {"objective75_overnight", "continuous_task_dispatch"}
REMOTE_PUBLISH_REQUEST_SOURCES = {
    "mim_tod_auto_reissue",
    "mim_arm_safe_home_dispatch",
    "mim_arm_scan_pose_dispatch",
}
BOUNDARY_STATUS_ARTIFACT = "MIM_TOD_PUBLICATION_BOUNDARY.latest.json"
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
TERMINAL_REVIEW_STATES = {"completed", "succeeded", "approved", "done"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def file_timestamp(path: Path) -> str:
    if not path.exists():
        return ""
    return isoformat_z(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc))


def parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def age_hours(timestamp: object, *, reference: datetime) -> float | None:
    parsed = parse_timestamp(timestamp)
    if parsed is None:
        return None
    return round(max(0.0, (reference - parsed).total_seconds()) / 3600.0, 2)


def normalize_objective(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"(\d+)", text)
    return match.group(1) if match else text


def as_int(value: object) -> int | None:
    text = normalize_objective(value)
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


def parse_boolish(value: object) -> bool | None:
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


def objective_exceeds_ceiling(objective_ref: object, ceiling_ref: object) -> bool:
    objective = as_int(objective_ref)
    ceiling = as_int(ceiling_ref)
    if objective is None or ceiling is None:
        return False
    return objective > ceiling


def cap_objective_to_ceiling(objective_ref: object, ceiling_ref: object) -> str:
    objective = normalize_objective(objective_ref)
    ceiling = normalize_objective(ceiling_ref)
    if objective and ceiling and objective_exceeds_ceiling(objective, ceiling):
        return ceiling
    return objective


def authority_reset_candidate_payloads(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
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


def parse_objective_authority_reset(payload: dict[str, Any] | None, source: Path) -> dict[str, Any] | None:
    candidates = authority_reset_candidate_payloads(payload)
    top_level_enabled = None
    if isinstance(payload, dict):
        for key in ("active", "enabled", "applied"):
            parsed = parse_boolish(payload.get(key))
            if parsed is not None:
                top_level_enabled = parsed
                break

    for index, candidate in enumerate(candidates):
        ceiling_objective = ""
        matched_key = ""
        for key in AUTHORITY_RESET_OBJECTIVE_KEYS:
            ceiling_objective = normalize_objective(candidate.get(key))
            if ceiling_objective:
                matched_key = key
                break
        if not ceiling_objective:
            continue

        enabled = None
        for key in ("active", "enabled", "applied"):
            parsed = parse_boolish(candidate.get(key))
            if parsed is not None:
                enabled = parsed
                break
        if enabled is False:
            continue
        if index > 0 and top_level_enabled is False and enabled is not True:
            continue

        rewrite_completion_history = False
        for key in AUTHORITY_RESET_REWRITE_KEYS:
            parsed = parse_boolish(candidate.get(key))
            if parsed is not None:
                rewrite_completion_history = parsed
                break

        return {
            "objective_ceiling": ceiling_objective,
            "rewrite_completion_history": rewrite_completion_history,
            "source": str(source),
            "matched_key": matched_key,
        }
    return None


def load_objective_authority_reset(shared_dir: Path) -> dict[str, Any] | None:
    candidates = [
        shared_dir / "objective_authority_reset.json",
        shared_dir / "OBJECTIVE_AUTHORITY_RESET.latest.json",
        *AUTHORITY_RESET_ARTIFACT_CANDIDATES,
    ]
    for path in candidates:
        payload = read_json(path)
        details = parse_objective_authority_reset(payload, path)
        if details is not None:
            return details
    return None


def infer_publication_boundary_authority_reset(
    *,
    boundary_status: dict[str, Any],
    latest_completed: str,
    canonical_objective: str,
    formal_program_objective: str = "",
    formal_program_active: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(boundary_status, dict):
        return None
    for request_key in ("authoritative_request", "local_request", "remote_request"):
        request_payload = boundary_status.get(request_key)
        if not isinstance(request_payload, dict):
            continue
        for field in ("objective_id", "task_id", "request_id"):
            boundary_objective = normalize_objective(request_payload.get(field))
            if not boundary_objective:
                continue
            if latest_completed and boundary_objective != latest_completed:
                continue
            if not objective_exceeds_ceiling(canonical_objective, boundary_objective):
                continue
            if formal_program_active and objective_exceeds_ceiling(
                formal_program_objective,
                boundary_objective,
            ):
                continue
            return {
                "objective_ceiling": boundary_objective,
                "rewrite_completion_history": False,
                "source": f"{BOUNDARY_STATUS_ARTIFACT}:{request_key}.{field}",
                "matched_key": f"{request_key}.{field}",
                "inferred_from": "publication_boundary_authoritative_request",
            }
    return None


def live_task_request_is_stale(
    *,
    canonical_objective: str,
    live_task_objective: str,
    request_generated_at: object,
    canonical_generated_at: object,
) -> bool:
    if not canonical_objective or not live_task_objective:
        return False
    if canonical_objective == live_task_objective:
        return False
    request_generated_dt = parse_timestamp(request_generated_at)
    canonical_generated_dt = parse_timestamp(canonical_generated_at)
    if request_generated_dt is None or canonical_generated_dt is None:
        return False
    return request_generated_dt < canonical_generated_dt


def load_task_status_review(shared_dir: Path) -> dict[str, Any]:
    return read_json(shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json")


def terminal_request_review_details(
    review_payload: dict[str, Any] | None,
    *,
    request_task_id: str,
    request_objective: str,
    next_objective: str,
) -> dict[str, Any] | None:
    if not isinstance(review_payload, dict):
        return None

    def first_text(payload: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return ""

    task_payload = review_payload.get("task")
    if not isinstance(task_payload, dict):
        return None
    gate_payload = review_payload.get("gate")
    if not isinstance(gate_payload, dict):
        gate_payload = {}

    review_task_id = first_text(
        task_payload,
        "authoritative_task_id",
        "active_task_id",
        "request_task_id",
        "task_id",
    )
    review_objective = normalize_objective(
        task_payload.get("objective_id") or review_task_id
    )
    task_matches = bool(request_task_id and review_task_id and request_task_id == review_task_id)
    if not task_matches:
        return None

    state = first_text(review_payload, "state").lower()
    gate_pass = gate_payload.get("pass") is True
    promotion_ready = gate_payload.get("promotion_ready") is True
    later_objective_exists = bool(
        next_objective
        and review_objective
        and as_int(next_objective) is not None
        and as_int(review_objective) is not None
        and as_int(next_objective) > as_int(review_objective)
    )
    if state not in TERMINAL_REVIEW_STATES or not gate_pass:
        return None
    if not later_objective_exists:
        return None

    return {
        "review_task_id": review_task_id,
        "review_objective": review_objective,
        "state": state,
        "promotion_ready": promotion_ready,
        "reason": "completed_gate_passing_request",
    }


def build_payload(shared_dir: Path, output_path: Path) -> dict[str, Any]:
    reference = utc_now()
    existing = read_json(output_path)
    context_path = shared_dir / "MIM_CONTEXT_EXPORT.latest.json"
    yaml_path = shared_dir / "MIM_CONTEXT_EXPORT.latest.yaml"
    manifest_path = shared_dir / "MIM_MANIFEST.latest.json"
    handshake_path = shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json"
    request_path = shared_dir / "MIM_TOD_TASK_REQUEST.latest.json"
    boundary_status_path = shared_dir / BOUNDARY_STATUS_ARTIFACT

    context = read_json(context_path)
    manifest = read_json(manifest_path)
    handshake = read_json(handshake_path)
    request = read_json(request_path)
    boundary_status = read_json(boundary_status_path)
    objective_authority_reset = load_objective_authority_reset(shared_dir)
    review = load_task_status_review(shared_dir)
    boundary_request = boundary_status.get("authoritative_request") if isinstance(boundary_status.get("authoritative_request"), dict) else {}
    effective_request = boundary_request if boundary_request else request

    manifest_payload = manifest.get("manifest") if isinstance(manifest.get("manifest"), dict) else {}
    truth = handshake.get("truth") if isinstance(handshake.get("truth"), dict) else {}
    source_of_truth = context.get("source_of_truth") if isinstance(context.get("source_of_truth"), dict) else {}
    formal_program_truth = source_of_truth.get("formal_program_truth") if isinstance(source_of_truth.get("formal_program_truth"), dict) else {}

    raw_canonical_objective = normalize_objective(
        context.get("objective_active")
        or truth.get("objective_active")
        or manifest_payload.get("objective_active")
    )
    raw_latest_completed = normalize_objective(
        context.get("latest_completed_objective") or truth.get("latest_completed_objective")
    )
    formal_program_objective = normalize_objective(
        formal_program_truth.get("objective")
        or formal_program_truth.get("objective_id")
    )
    formal_program_execution_state = str(
        formal_program_truth.get("execution_state")
        or formal_program_truth.get("objective_status")
        or formal_program_truth.get("project_status")
        or ""
    ).strip().lower()
    formal_program_active = bool(
        formal_program_objective
        and formal_program_execution_state in {"executing", "working", "in_progress", "active", "queued", "created"}
    )
    canonical_objective = normalize_objective(
        raw_canonical_objective
    )
    canonical_schema = str(
        context.get("schema_version")
        or truth.get("schema_version")
        or manifest_payload.get("schema_version")
        or ""
    ).strip()
    canonical_release = str(
        context.get("release_tag")
        or truth.get("release_tag")
        or manifest_payload.get("release_tag")
        or ""
    ).strip()
    latest_completed = normalize_objective(
        raw_latest_completed
    )
    next_objective = normalize_objective(
        context.get("current_next_objective") or truth.get("current_next_objective")
    )
    regression_status = str(truth.get("regression_status") or "").strip()
    regression_tests = str(truth.get("regression_tests") or "").strip()
    prod_promotion_status = str(truth.get("prod_promotion_status") or "").strip()
    prod_smoke_status = str(truth.get("prod_smoke_status") or "").strip()
    blockers = truth.get("blockers") if isinstance(truth.get("blockers"), list) else []

    local_request_task_id = str(request.get("task_id") or request.get("request_id") or "").strip()
    local_request_objective = normalize_objective(
        request.get("objective_id") or request.get("task_id") or request.get("request_id")
    )
    local_terminal_request_review = terminal_request_review_details(
        review,
        request_task_id=local_request_task_id,
        request_objective=local_request_objective,
        next_objective=next_objective,
    )
    boundary_task_id = str(
        boundary_request.get("task_id") or boundary_request.get("request_id") or ""
    ).strip()
    boundary_objective = normalize_objective(
        boundary_request.get("objective_id")
        or boundary_request.get("task_id")
        or boundary_request.get("request_id")
    )
    boundary_stale_against_local_terminal = bool(
        boundary_request
        and local_terminal_request_review is not None
        and local_request_task_id
        and boundary_task_id
        and boundary_task_id != local_request_task_id
    )
    if boundary_stale_against_local_terminal:
        effective_request = request
        boundary_request = {}
    terminal_request_review = local_terminal_request_review
    if objective_authority_reset is None and terminal_request_review is None:
        objective_authority_reset = infer_publication_boundary_authority_reset(
            boundary_status=boundary_status,
            latest_completed=raw_latest_completed,
            canonical_objective=raw_canonical_objective,
            formal_program_objective=formal_program_objective,
            formal_program_active=formal_program_active,
        )
    authority_reset_ceiling = normalize_objective(
        objective_authority_reset.get("objective_ceiling")
        if isinstance(objective_authority_reset, dict)
        else ""
    )
    rewrite_completion_history = bool(
        objective_authority_reset.get("rewrite_completion_history")
        if isinstance(objective_authority_reset, dict)
        else False
    )
    if objective_exceeds_ceiling(canonical_objective, authority_reset_ceiling):
        canonical_objective = authority_reset_ceiling
    if rewrite_completion_history:
        latest_completed = cap_objective_to_ceiling(
            latest_completed,
            authority_reset_ceiling,
        )
    if objective_exceeds_ceiling(next_objective, authority_reset_ceiling):
        next_objective = authority_reset_ceiling
    if boundary_stale_against_local_terminal:
        if isinstance(objective_authority_reset, dict) and str(objective_authority_reset.get("inferred_from") or "") == "publication_boundary_authoritative_request":
            objective_authority_reset = None
            authority_reset_ceiling = ""
            canonical_objective = normalize_objective(raw_canonical_objective)
            latest_completed = normalize_objective(raw_latest_completed)
            next_objective = normalize_objective(
                context.get("current_next_objective") or truth.get("current_next_objective")
            )

    objective_from_request = normalize_objective(
        effective_request.get("objective_id") or effective_request.get("task_id") or effective_request.get("request_id")
    )
    request_generated_at = str(effective_request.get("generated_at") or file_timestamp(request_path)).strip()
    request_task_id = str(effective_request.get("task_id") or effective_request.get("request_id") or "").strip()
    request_correlation_id = str(effective_request.get("correlation_id") or request.get("correlation_id") or "").strip()
    request_source_service = str(effective_request.get("source_service") or "").strip()
    request_source_instance_id = str(effective_request.get("source_instance_id") or "").strip()
    terminal_request_review = terminal_request_review_details(
        review,
        request_task_id=request_task_id,
        request_objective=objective_from_request,
        next_objective=next_objective,
    )
    request_source_key = request_source_service.strip().lower()
    publication_lane = "unknown"
    if request_source_key in LOCAL_ONLY_REQUEST_SOURCES:
        publication_lane = "local_only"
    elif request_source_key in REMOTE_PUBLISH_REQUEST_SOURCES:
        publication_lane = "remote_publish_capable"

    mim_contract = str(
        manifest_payload.get("contract_version") or handshake.get("contract_version") or "tod-mim-shared-contract-v1"
    ).strip()
    tod_contract = str(existing.get("tod_contract") or mim_contract).strip()
    compatible = bool(mim_contract and tod_contract and mim_contract == tod_contract)

    context_generated_at = str(context.get("exported_at") or context.get("generated_at") or file_timestamp(context_path)).strip()
    context_age_hours = age_hours(context_generated_at, reference=reference)
    handshake_generated_at = str(handshake.get("generated_at") or file_timestamp(handshake_path)).strip()
    canonical_generated_at = context_generated_at or handshake_generated_at

    stale_live_task_request = live_task_request_is_stale(
        canonical_objective=canonical_objective,
        live_task_objective=objective_from_request,
        request_generated_at=request_generated_at,
        canonical_generated_at=canonical_generated_at,
    )
    terminal_completed_request = terminal_request_review is not None
    original_live_task_objective = objective_from_request
    authority_reset_applied_to_request = objective_exceeds_ceiling(
        objective_from_request,
        authority_reset_ceiling,
    )
    if terminal_completed_request or stale_live_task_request or authority_reset_applied_to_request:
        objective_from_request = canonical_objective

    promotion_applied = bool(
        original_live_task_objective and canonical_objective and original_live_task_objective != canonical_objective
    )
    if authority_reset_applied_to_request:
        promotion_reason = "request_objective_above_authority_reset_ceiling"
    elif promotion_applied and as_int(original_live_task_objective) is not None and as_int(canonical_objective) is not None:
        promotion_reason = "request_objective_ahead_of_canonical_export" if as_int(original_live_task_objective) > as_int(canonical_objective) else "request_objective_differs_from_canonical_export"
    else:
        promotion_reason = ""

    tod_current_objective = objective_from_request or canonical_objective
    objective_delta = None
    if as_int(tod_current_objective) is not None and as_int(canonical_objective) is not None:
        objective_delta = as_int(tod_current_objective) - as_int(canonical_objective)
    aligned = bool(tod_current_objective and canonical_objective and tod_current_objective == canonical_objective)

    required_json_present = context_path.exists()
    required_yaml_present = yaml_path.exists()
    manifest_present = manifest_path.exists()
    handshake_present = handshake_path.exists()
    refresh_failure_reason = ""
    if not required_json_present:
        refresh_failure_reason = "missing_context_export_json"
    elif not required_yaml_present:
        refresh_failure_reason = "missing_context_export_yaml"
    elif not manifest_present:
        refresh_failure_reason = "missing_manifest"
    elif not handshake_present:
        refresh_failure_reason = "missing_handshake_packet"

    payload = {
        "generated_at": isoformat_z(reference),
        "source": "tod-integration-status-local-rebuild-v1",
        "mim_schema": canonical_schema,
        "tod_contract": tod_contract,
        "mim_contract": mim_contract,
        "compatible": compatible,
        "compatibility_reason": "contract_version_match" if compatible else "contract_version_mismatch",
        "mim_status": {
            "available": bool(context),
            "source_path": str(context_path),
            "generated_at": context_generated_at,
            "age_hours": context_age_hours,
            "stale_after_hours": 6,
            "is_stale": bool(context_age_hours is not None and context_age_hours > 6),
            "objective_active": canonical_objective,
            "phase": str(context.get("phase") or "unknown").strip(),
            "blockers": blockers,
        },
        "mim_handshake": {
            "available": bool(handshake),
            "source_path": str(handshake_path),
            "generated_at": handshake_generated_at,
            "handshake_version": str(handshake.get("handshake_version") or "").strip(),
            "objective_active": canonical_objective,
            "latest_completed_objective": latest_completed,
            "current_next_objective": next_objective,
            "schema_version": canonical_schema,
            "release_tag": canonical_release,
            "regression_status": regression_status,
            "regression_tests": regression_tests,
            "prod_promotion_status": prod_promotion_status,
            "prod_smoke_status": prod_smoke_status,
            "blockers": blockers,
        },
        "live_task_request": {
            "available": bool(request),
            "source_path": str(boundary_request.get("path") or request_path),
            "local_source_path": str(request_path),
            "generated_at": request_generated_at,
            "request_id": request_task_id,
            "task_id": request_task_id,
            "objective_id": str(effective_request.get("objective_id") or "").strip(),
            "normalized_objective_id": objective_from_request,
            "correlation_id": request_correlation_id,
            "source_service": request_source_service,
            "source_instance_id": request_source_instance_id,
            "publication_lane": publication_lane,
            "local_only_writer": publication_lane == "local_only",
            "promotion_applied": promotion_applied,
            "promotion_reason": promotion_reason,
            "stale_prior_objective": stale_live_task_request or terminal_completed_request,
            "terminal_completed_request": terminal_completed_request,
            "stale_reason": (
                "request_already_completed_promotion_ready"
                if terminal_completed_request
                else
                "objective_above_authority_reset_ceiling"
                if authority_reset_applied_to_request
                else
                "live_task_request_older_than_canonical_export"
                if stale_live_task_request
                else ""
            ),
        },
        "objective_authority_reset": objective_authority_reset or {},
        "task_status_review": {
            "available": bool(review),
            "terminal_request_review": terminal_request_review or {},
        },
        "publication_boundary": {
            "authoritative_surface": CANONICAL_COMMUNICATION_SURFACE,
            "authoritative_host": CANONICAL_COMMUNICATION_HOST,
            "authoritative_root": CANONICAL_COMMUNICATION_ROOT,
            "authoritative_path": str(boundary_request.get("path") or f"{CANONICAL_COMMUNICATION_ROOT}/MIM_TOD_TASK_REQUEST.latest.json"),
            "local_surface": str(request_path),
            "local_surface_role": str(boundary_status.get("local_surface_role") or "authoritative_communication_root"),
            "local_only_writer_active": publication_lane == "local_only",
            "observed_boundary_authoritative_surface": str(boundary_status.get("authoritative_surface") or ""),
            "observed_boundary_authoritative_host": str(boundary_status.get("authoritative_host") or ""),
            "observed_boundary_authoritative_root": str(boundary_status.get("authoritative_root") or ""),
            "status_artifact": str(boundary_status_path),
            "status_generated_at": str(boundary_status.get("generated_at") or ""),
        },
        "mim_refresh": {
            "attempted": True,
            "copied_json": required_json_present,
            "copied_yaml": required_yaml_present,
            "copied_manifest": manifest_present,
            "source_json": str(context_path) if required_json_present else "",
            "source_yaml": str(yaml_path) if required_yaml_present else "",
            "source_manifest": str(manifest_path) if manifest_present else "",
            "source_handshake_packet": str(handshake_path) if handshake_present else "",
            "resolved_source_root": str(shared_dir),
            "candidate_paths_tried": [
                str(context_path),
                str(yaml_path),
                str(manifest_path),
                str(handshake_path),
            ],
            "failure_reason": refresh_failure_reason,
            "ssh_attempted": False,
            "ssh_host": "",
            "ssh_resolved_host": "",
            "ssh_remote_root": "",
            "ssh_stage_root": "",
            "ssh_auth_mode": "",
        },
        "objective_alignment": {
            "status": "in_sync" if aligned else "mismatch",
            "aligned": aligned,
            "tod_current_objective": tod_current_objective,
            "mim_objective_active": canonical_objective,
            "mim_objective_source": "context_export",
            "delta": objective_delta,
        },
        "tod_status_publish": {
            "attempted": True,
            "enabled": True,
            "status": "local_rebuilt",
            "local_status_path": str(output_path),
            "local_status_sha256": "",
            "receipt_path": "",
            "ssh_host": "",
            "ssh_resolved_host": "",
            "ssh_user": "",
            "ssh_port": 22,
            "remote_root": str(shared_dir),
            "remote_primary_path": str(output_path),
            "remote_alias_path": str(shared_dir / "TOD_integration_status.latest.json"),
            "remote_summary_path": "",
            "remote_consumer_script_path": "",
            "consumer_status": "local_rebuild",
            "uploaded_at": isoformat_z(reference),
            "error": "",
        },
    }
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild TOD_INTEGRATION_STATUS.latest.json from current shared MIM truth."
    )
    parser.add_argument(
        "--shared-dir",
        default=str(DEFAULT_SHARED_DIR),
        help="Shared artifact directory containing the canonical MIM export, manifest, handshake, and request files.",
    )
    parser.add_argument(
        "--output",
        default="TOD_INTEGRATION_STATUS.latest.json",
        help="Output file name or absolute path for the rebuilt canonical integration status.",
    )
    parser.add_argument(
        "--mirror-legacy-alias",
        action="store_true",
        default=False,
        help="Also mirror the rebuilt payload to TOD_integration_status.latest.json.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    shared_dir = Path(args.shared_dir).expanduser().resolve()
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = shared_dir / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    payload = build_payload(shared_dir, output_path)
    encoded = json.dumps(payload, indent=2) + "\n"
    output_path.write_text(encoded, encoding="utf-8")

    if args.mirror_legacy_alias:
        alias_path = shared_dir / "TOD_integration_status.latest.json"
        alias_path.write_text(encoded, encoding="utf-8")

    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())