from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = PROJECT_ROOT / "contracts"
RUNTIME_SHARED_DIR = PROJECT_ROOT / "runtime" / "shared"

CONTRACT_ID = "TOD_MIM_COMMUNICATION_CONTRACT.v1"
CONTRACT_NAME = "TOD_MIM_COMMUNICATION_CONTRACT"
CONTRACT_VERSION = "v1"
CONTRACT_SCHEMA_VERSION = "2026-04-02-communication-contract-v1"
CONTRACT_SOURCE = "MIM"

CONTRACT_YAML_PATH = CONTRACTS_DIR / "TOD_MIM_COMMUNICATION_CONTRACT.v1.yaml"
CONTRACT_SCHEMA_PATH = CONTRACTS_DIR / "TOD_MIM_COMMUNICATION_CONTRACT.v1.schema.json"
CONTRACT_SIGNATURE_PATH = CONTRACTS_DIR / "TOD_MIM_COMMUNICATION_CONTRACT.v1.signature.json"

CONTRACT_TRANSMISSION_ARTIFACT = "MIM_TOD_COMMUNICATION_CONTRACT_TRANSMISSION.latest.json"
CONTRACT_RECEIPT_ARTIFACT = "TOD_MIM_CONTRACT_RECEIPT.latest.json"
CONTRACT_LOCK_ARTIFACT = "TOD_MIM_CONTRACT_LOCK.latest.json"
CONTRACT_VALIDATION_FAILURE_ARTIFACT = "TOD_MIM_CONTRACT_VALIDATION_FAILURE.latest.json"
CONTRACT_ACTIVATION_REPORT_ARTIFACT = "TOD_MIM_CONTRACT_ACTIVATION_REPORT.latest.json"

CONTRACT_TRANSMISSION_PATH = RUNTIME_SHARED_DIR / CONTRACT_TRANSMISSION_ARTIFACT
CONTRACT_RECEIPT_PATH = RUNTIME_SHARED_DIR / CONTRACT_RECEIPT_ARTIFACT
CONTRACT_LOCK_PATH = RUNTIME_SHARED_DIR / CONTRACT_LOCK_ARTIFACT
CONTRACT_VALIDATION_FAILURE_PATH = RUNTIME_SHARED_DIR / CONTRACT_VALIDATION_FAILURE_ARTIFACT
CONTRACT_ACTIVATION_REPORT_PATH = RUNTIME_SHARED_DIR / CONTRACT_ACTIVATION_REPORT_ARTIFACT

PRIMARY_TRANSPORT_ID = "mim_server_shared_artifact_boundary"
PRIMARY_TRANSPORT_SURFACE = "/home/testpilot/mim/runtime/shared"
LOCAL_TRANSPORT_SURFACE = "/home/testpilot/mim/runtime/shared"

MESSAGE_KIND_TO_PACKET_TYPE = {
    "request": "mim-tod-task-request-v1",
    "ack": "tod-mim-task-ack-v1",
    "result": "tod-mim-task-result-v1",
    "heartbeat": "tod-mim-heartbeat-v1",
    "fallback": "tod-mim-fallback-activation-v1",
    "trigger": "shared-trigger-v1",
    "trigger_ack": "shared-trigger-ack-v1",
    "contract_transmission": "tod-mim-contract-distribution-v1",
    "contract_receipt": "tod-mim-contract-receipt-v1",
}

MESSAGE_REQUIRED_FIELDS = {
    "request": [
        "packet_type",
        "schema_version",
        "contract_version",
        "generated_at",
        "source_identity",
        "transport",
        "objective_id",
        "request_id",
        "correlation_id",
        "message_kind",
        "sequence",
        "task_classification",
        "target_executor",
        "command",
        "execution_policy",
        "idempotency",
        "fallback_policy",
    ],
    "ack": [
        "packet_type",
        "schema_version",
        "contract_version",
        "generated_at",
        "source_identity",
        "transport",
        "objective_id",
        "task_id",
        "request_id",
        "correlation_id",
        "message_kind",
        "sequence",
        "ack_status",
        "acknowledged_trigger_sequence",
        "ack_reason_code",
    ],
    "result": [
        "packet_type",
        "schema_version",
        "contract_version",
        "generated_at",
        "source_identity",
        "transport",
        "objective_id",
        "task_id",
        "request_id",
        "correlation_id",
        "message_kind",
        "sequence",
        "result_status",
        "terminal",
        "execution_outcome",
        "result_reason_code",
    ],
    "heartbeat": [
        "packet_type",
        "schema_version",
        "contract_version",
        "generated_at",
        "source_identity",
        "transport",
        "objective_id",
        "task_id",
        "request_id",
        "correlation_id",
        "message_kind",
        "sequence",
        "heartbeat_status",
        "responds_to",
        "freshness",
    ],
    "fallback": [
        "packet_type",
        "schema_version",
        "contract_version",
        "generated_at",
        "source_identity",
        "transport",
        "objective_id",
        "task_id",
        "request_id",
        "correlation_id",
        "message_kind",
        "sequence",
        "fallback_reason_code",
        "primary_transport_state",
        "fallback_scope",
    ],
    "trigger": [
        "packet_type",
        "generated_at",
        "sequence",
        "task_id",
        "correlation_id",
        "artifact",
        "action_required",
        "ack_file_expected",
    ],
    "trigger_ack": [
        "packet_type",
        "generated_at",
        "sequence",
        "task_id",
        "correlation_id",
    ],
    "contract_transmission": [
        "packet_type",
        "contract_id",
        "contract_name",
        "contract_version",
        "schema_version",
        "generated_at",
        "source_identity",
        "checksum_sha256",
        "signature",
        "payload",
    ],
    "contract_receipt": [
        "packet_type",
        "contract_id",
        "contract_version",
        "generated_at",
        "checksum_sha256",
        "checksum_match",
        "version_accepted",
    ],
}

CANONICAL_WRITERS = [
    {
        "writer_id": "core/routers/mim_arm.py:publish_mim_arm_execution_to_tod",
        "status": "active",
        "artifact_domain": "execution_request",
        "role": "canonical_request_writer",
    },
    {
        "writer_id": "scripts/reissue_active_tod_task.sh",
        "status": "edge_routed_through_contract_validation",
        "artifact_domain": "execution_request",
        "role": "reissue_path",
    },
    {
        "writer_id": "scripts/run_objective75_overnight_loop.sh",
        "status": "edge_writer_guarded",
        "artifact_domain": "execution_request",
        "role": "background_loop",
    },
    {
        "writer_id": "scripts/continuous_task_dispatch.sh",
        "status": "edge_writer_guarded",
        "artifact_domain": "execution_request",
        "role": "background_loop",
    },
    {
        "writer_id": "scripts/publish_tod_bridge_artifacts_remote.py",
        "status": "active",
        "artifact_domain": "communication_boundary_sync",
        "role": "canonical_communication_publisher",
    },
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        temp_path = Path(handle.name)
    temp_path.replace(path)


def contract_yaml_bytes() -> bytes:
    return CONTRACT_YAML_PATH.read_bytes()


def compute_contract_sha256() -> str:
    return hashlib.sha256(contract_yaml_bytes()).hexdigest()


def build_signature_payload() -> dict[str, Any]:
    return {
        "contract_id": CONTRACT_ID,
        "version": CONTRACT_VERSION,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "sha256": compute_contract_sha256(),
        "timestamp": utc_now(),
        "source": CONTRACT_SOURCE,
    }


def ensure_contract_signature() -> dict[str, Any]:
    payload = build_signature_payload()
    _write_json_atomic(CONTRACT_SIGNATURE_PATH, payload)
    return payload


def load_contract_signature() -> dict[str, Any]:
    payload = _read_json(CONTRACT_SIGNATURE_PATH)
    if not payload:
        payload = ensure_contract_signature()
    return payload


def build_source_identity(*, actor: str = "MIM", service_name: str = "", instance_id: str = "") -> dict[str, Any]:
    host = socket.gethostname()
    service = str(service_name or "unknown_service").strip() or "unknown_service"
    instance = str(instance_id or f"{service}:{os.getpid()}").strip() or f"{service}:{os.getpid()}"
    return {
        "actor": actor,
        "host": host,
        "service": service,
        "instance_id": instance,
    }


def build_transport(*, transport_id: str = PRIMARY_TRANSPORT_ID, surface: str = PRIMARY_TRANSPORT_SURFACE) -> dict[str, Any]:
    return {
        "transport_id": str(transport_id or PRIMARY_TRANSPORT_ID),
        "surface": str(surface or PRIMARY_TRANSPORT_SURFACE),
    }


def normalize_objective_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("objective-"):
        return text
    return f"objective-{text}"


def _default_sequence(payload: dict[str, Any]) -> int:
    try:
        return int(payload.get("sequence") or 1)
    except Exception:
        return 1


def _default_objective_id(payload: dict[str, Any]) -> str:
    return normalize_objective_id(payload.get("objective_id") or payload.get("objective") or "")


def _default_task_id(payload: dict[str, Any]) -> str:
    explicit_task_id = str(payload.get("task_id") or "").strip()
    if explicit_task_id:
        return explicit_task_id
    return str(payload.get("request_id") or "").strip()


def _default_request_id(payload: dict[str, Any]) -> str:
    return str(payload.get("request_id") or _default_task_id(payload)).strip()


def _default_correlation_id(payload: dict[str, Any]) -> str:
    request_id = _default_request_id(payload)
    return str(payload.get("correlation_id") or request_id).strip()


def _default_command(payload: dict[str, Any]) -> dict[str, Any]:
    command = payload.get("command")
    if isinstance(command, dict):
        return command
    action = str(payload.get("action") or payload.get("capability_name") or "operation").strip() or "operation"
    return {"name": action, "args": {}}


def normalize_message(
    payload: dict[str, Any],
    *,
    message_kind: str,
    service_name: str,
    instance_id: str = "",
    actor: str = "MIM",
    transport_id: str = PRIMARY_TRANSPORT_ID,
    transport_surface: str = PRIMARY_TRANSPORT_SURFACE,
) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault("packet_type", MESSAGE_KIND_TO_PACKET_TYPE.get(message_kind, ""))
    normalized.setdefault("schema_version", CONTRACT_SCHEMA_VERSION)
    normalized.setdefault("contract_version", CONTRACT_VERSION)
    normalized.setdefault("generated_at", str(normalized.get("generated_at") or normalized.get("emitted_at") or utc_now()))
    normalized.setdefault("message_kind", message_kind)
    normalized.setdefault("sequence", _default_sequence(normalized))

    if message_kind in {"request", "ack", "result", "heartbeat", "fallback"}:
        objective_id = _default_objective_id(normalized)
        task_id = _default_task_id(normalized)
        request_id = _default_request_id(normalized)
        correlation_id = _default_correlation_id(normalized)
        normalized["objective_id"] = objective_id
        normalized["task_id"] = task_id
        normalized["request_id"] = request_id
        normalized["correlation_id"] = correlation_id
        normalized.setdefault(
            "source_identity",
            build_source_identity(actor=actor, service_name=service_name, instance_id=instance_id),
        )
        normalized.setdefault(
            "transport",
            build_transport(transport_id=transport_id, surface=transport_surface),
        )

    if message_kind == "request":
        normalized.setdefault("task_classification", "governed_execution")
        normalized.setdefault("target_executor", str(normalized.get("requested_executor") or normalized.get("target") or "TOD"))
        normalized.setdefault("command", _default_command(normalized))
        normalized.setdefault(
            "execution_policy",
            {
                "policy_outcome": "allow",
                "transport": transport_id,
                "shadow_mode": True,
            },
        )
        normalized.setdefault(
            "idempotency",
            {
                "key": normalized.get("request_id"),
                "duplicate_execution_allowed": False,
            },
        )
        normalized.setdefault(
            "fallback_policy",
            {
                "activation_rule": "primary_transport_unavailable_or_reconciliation_blocked",
                "allowed": True,
            },
        )
    elif message_kind == "heartbeat":
        normalized.setdefault("heartbeat_status", str(normalized.get("status") or "alive"))
        normalized.setdefault("responds_to", str(normalized.get("responds_to") or ""))
        normalized.setdefault(
            "freshness",
            {
                "generated_at": normalized.get("generated_at"),
                "state": "fresh",
            },
        )
    elif message_kind == "fallback":
        normalized.setdefault("fallback_reason_code", str(normalized.get("fallback_reason_code") or "primary_transport_unavailable"))
        normalized.setdefault("primary_transport_state", str(normalized.get("primary_transport_state") or "unavailable"))
        normalized.setdefault("fallback_scope", str(normalized.get("fallback_scope") or normalized.get("task_id") or "global"))
    elif message_kind == "trigger":
        normalized.setdefault("source_identity", build_source_identity(actor=actor, service_name=service_name, instance_id=instance_id))
        normalized.setdefault("transport", build_transport(transport_id=transport_id, surface=transport_surface))
    elif message_kind == "contract_transmission":
        normalized.setdefault("contract_id", CONTRACT_ID)
        normalized.setdefault("contract_name", CONTRACT_NAME)
        normalized.setdefault(
            "source_identity",
            build_source_identity(actor=actor, service_name=service_name, instance_id=instance_id),
        )
    elif message_kind == "contract_receipt":
        normalized.setdefault("contract_id", CONTRACT_ID)
    return normalized


def validate_message(payload: dict[str, Any], message_kind: str) -> list[str]:
    errors: list[str] = []
    required = MESSAGE_REQUIRED_FIELDS.get(message_kind, [])
    for field in required:
        value = payload.get(field)
        if value in (None, "", [], {}):
            errors.append(f"missing_required_field:{field}")

    expected_packet_type = MESSAGE_KIND_TO_PACKET_TYPE.get(message_kind)
    if expected_packet_type and str(payload.get("packet_type") or "") != expected_packet_type:
        errors.append(f"packet_type_mismatch:{expected_packet_type}")

    if message_kind in {"request", "ack", "result", "heartbeat", "fallback"}:
        if str(payload.get("schema_version") or "") != CONTRACT_SCHEMA_VERSION:
            errors.append("schema_version_mismatch")
        if str(payload.get("contract_version") or "") != CONTRACT_VERSION:
            errors.append("contract_version_mismatch")
        if not isinstance(payload.get("source_identity"), dict):
            errors.append("invalid_source_identity")
        if not isinstance(payload.get("transport"), dict):
            errors.append("invalid_transport")

    if message_kind == "request":
        if not isinstance(payload.get("command"), dict):
            errors.append("invalid_command")
        if not isinstance(payload.get("execution_policy"), dict):
            errors.append("invalid_execution_policy")
        if not isinstance(payload.get("idempotency"), dict):
            errors.append("invalid_idempotency")
        if not isinstance(payload.get("fallback_policy"), dict):
            errors.append("invalid_fallback_policy")
    elif message_kind == "ack":
        status = str(payload.get("ack_status") or "").strip().lower()
        if status and status not in {"accepted", "rejected", "superseded_ignored", "stale_ignored"}:
            errors.append("invalid_ack_status")
    elif message_kind == "result":
        status = str(payload.get("result_status") or "").strip().lower()
        if status and status not in {"succeeded", "failed", "timed_out", "aborted", "blocked"}:
            errors.append("invalid_result_status")
    elif message_kind == "heartbeat":
        status = str(payload.get("heartbeat_status") or "").strip().lower()
        if status and status not in {"alive", "degraded", "unavailable"}:
            errors.append("invalid_heartbeat_status")
    elif message_kind == "fallback":
        state = str(payload.get("primary_transport_state") or "").strip().lower()
        if state and state not in {"unavailable", "degraded", "blocked", "recovered"}:
            errors.append("invalid_primary_transport_state")
    elif message_kind == "contract_transmission":
        if str(payload.get("schema_version") or "") != CONTRACT_SCHEMA_VERSION:
            errors.append("schema_version_mismatch")
        if str(payload.get("contract_version") or "") != CONTRACT_VERSION:
            errors.append("contract_version_mismatch")
        if str(payload.get("contract_id") or "") != CONTRACT_ID:
            errors.append("contract_id_mismatch")
        if str(payload.get("contract_name") or "") != CONTRACT_NAME:
            errors.append("contract_name_mismatch")
        if not isinstance(payload.get("signature"), dict):
            errors.append("invalid_signature")
        if not isinstance(payload.get("source_identity"), dict):
            errors.append("invalid_source_identity")
    elif message_kind == "contract_receipt":
        if str(payload.get("contract_id") or "") != CONTRACT_ID:
            errors.append("contract_id_mismatch")
        if str(payload.get("contract_version") or "") != CONTRACT_VERSION:
            errors.append("contract_version_mismatch")
        if not isinstance(payload.get("checksum_match"), bool):
            errors.append("invalid_checksum_match")
        if not isinstance(payload.get("version_accepted"), bool):
            errors.append("invalid_version_accepted")
    return errors


def emit_validation_failure(
    *,
    artifact_path: str,
    message_kind: str,
    errors: list[str],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failure = {
        "generated_at": utc_now(),
        "packet_type": "tod-mim-contract-validation-failure-v1",
        "contract_id": CONTRACT_ID,
        "contract_version": CONTRACT_VERSION,
        "message_kind": message_kind,
        "artifact_path": artifact_path,
        "failure_reason_code": errors[0] if errors else "unknown_validation_failure",
        "errors": errors,
        "payload_excerpt": payload or {},
    }
    _write_json_atomic(CONTRACT_VALIDATION_FAILURE_PATH, failure)
    return failure


def normalize_and_validate_file(
    path: Path,
    *,
    message_kind: str,
    service_name: str,
    instance_id: str = "",
    actor: str = "MIM",
    transport_id: str = PRIMARY_TRANSPORT_ID,
    transport_surface: str = PRIMARY_TRANSPORT_SURFACE,
) -> tuple[dict[str, Any], list[str]]:
    payload = _read_json(path)
    normalized = normalize_message(
        payload,
        message_kind=message_kind,
        service_name=service_name,
        instance_id=instance_id,
        actor=actor,
        transport_id=transport_id,
        transport_surface=transport_surface,
    )
    errors = validate_message(normalized, message_kind)
    if errors:
        emit_validation_failure(
            artifact_path=str(path),
            message_kind=message_kind,
            errors=errors,
            payload=normalized,
        )
        return normalized, errors
    _write_json_atomic(path, normalized)
    return normalized, []


def build_contract_transmission_payload(*, service_name: str, instance_id: str = "") -> dict[str, Any]:
    signature = load_contract_signature()
    return {
        "packet_type": MESSAGE_KIND_TO_PACKET_TYPE["contract_transmission"],
        "contract_id": CONTRACT_ID,
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "source": CONTRACT_SOURCE,
        "source_identity": build_source_identity(actor=CONTRACT_SOURCE, service_name=service_name, instance_id=instance_id),
        "checksum_sha256": signature.get("sha256") or compute_contract_sha256(),
        "signature": signature,
        "artifact_name": CONTRACT_YAML_PATH.name,
        "payload": CONTRACT_YAML_PATH.read_text(encoding="utf-8"),
    }


def write_contract_transmission_artifact(*, service_name: str, instance_id: str = "") -> dict[str, Any]:
    payload = build_contract_transmission_payload(service_name=service_name, instance_id=instance_id)
    _write_json_atomic(CONTRACT_TRANSMISSION_PATH, payload)
    return payload


def receipt_status() -> dict[str, Any]:
    signature = load_contract_signature()
    receipt = _read_json(CONTRACT_RECEIPT_PATH)
    checksum = str(signature.get("sha256") or "")
    if not receipt:
        return {
            "status": "pending",
            "receipt_present": False,
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "expected_sha256": checksum,
            "checksum_match": False,
            "version_accepted": False,
        }

    receipt_checksum = str(receipt.get("checksum_sha256") or "").strip()
    receipt_version = str(receipt.get("contract_version") or "").strip()
    checksum_match = bool(receipt.get("checksum_match") is True and receipt_checksum == checksum)
    version_accepted = bool(receipt.get("version_accepted") is True and receipt_version == CONTRACT_VERSION)
    status = "accepted" if checksum_match and version_accepted else "mismatch"
    return {
        "status": status,
        "receipt_present": True,
        "contract_id": CONTRACT_ID,
        "contract_version": CONTRACT_VERSION,
        "expected_sha256": checksum,
        "receipt_sha256": receipt_checksum,
        "checksum_match": checksum_match,
        "version_accepted": version_accepted,
        "receipt": receipt,
    }


def ensure_runtime_contract_lock() -> dict[str, Any]:
    signature = load_contract_signature()
    current_sha = compute_contract_sha256()
    if str(signature.get("sha256") or "") != current_sha:
        raise RuntimeError("TOD↔MIM contract signature checksum mismatch; regenerate signature before startup")
    status = {
        "generated_at": utc_now(),
        "packet_type": "tod-mim-contract-lock-v1",
        "contract_id": CONTRACT_ID,
        "contract_version": CONTRACT_VERSION,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "sha256": current_sha,
        "runtime_lock": "active",
        "source": CONTRACT_SOURCE,
    }
    _write_json_atomic(CONTRACT_LOCK_PATH, status)
    return status


def build_activation_report() -> dict[str, Any]:
    signature = load_contract_signature()
    receipt = receipt_status()
    receipt_accepted = receipt.get("status") == "accepted"
    active_request_writers = [
        writer for writer in CANONICAL_WRITERS if writer.get("artifact_domain") == "execution_request"
    ]
    report = {
        "generated_at": utc_now(),
        "packet_type": "tod-mim-contract-activation-report-v1",
        "contract_id": CONTRACT_ID,
        "contract_version": CONTRACT_VERSION,
        "contract_checksum": signature.get("sha256") or "",
        "tod_receipt_status": receipt,
        "active_writers": active_request_writers,
        "active_writer_count": len([writer for writer in active_request_writers if writer.get("status") == "active"]),
        "schema_enforcement": {
            "request_writer_binding": "enabled",
            "ack_result_runtime_binding": "ready_for_tod_runtime_binding" if receipt_accepted else "pending_tod_contract_acceptance",
            "heartbeat_fallback_binding": "validator_available",
        },
        "shadow_mode": {
            "status": "tod_exact_match_confirmed" if receipt_accepted else "pending_tod_exact_match_confirmation",
            "comparison_ready": receipt_accepted,
            "reason": (
                "TOD returned an exact-match contract receipt; shadow comparison can advance to runtime binding."
                if receipt_accepted
                else "Contract transmission is written, but TOD has not yet returned an exact-match receipt."
            ),
        },
        "cutover_readiness": {
            "ready": False,
            "reason": (
                "TOD exact-match receipt is confirmed; request writer enforcement is active, but ack/result cutover is still pending runtime binding."
                if receipt_accepted
                else "TOD exact-match receipt is still pending."
            ),
        },
    }
    _write_json_atomic(CONTRACT_ACTIVATION_REPORT_PATH, report)
    return report