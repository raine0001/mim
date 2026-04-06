#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.tod_mim_contract import (  # noqa: E402
    CONTRACT_SCHEMA_PATH,
    CONTRACT_SIGNATURE_PATH,
    CONTRACT_YAML_PATH,
    build_signature_payload,
)


def _load_yaml() -> dict:
    try:
        import yaml
    except ModuleNotFoundError as exc:  # pragma: no cover - CI dependency guard
        raise SystemExit("PyYAML is required for contract preflight. Install with 'pip install PyYAML'.") from exc

    payload = yaml.safe_load(CONTRACT_YAML_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("Contract YAML must deserialize to a JSON object.")
    return payload


def _validate_schema(contract_payload: dict) -> None:
    try:
        import jsonschema
    except ModuleNotFoundError as exc:  # pragma: no cover - CI dependency guard
        raise SystemExit("jsonschema is required for contract preflight. Install with 'pip install jsonschema'.") from exc

    schema = json.loads(CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.validate(contract_payload, schema)


def _validate_signature() -> dict:
    expected = build_signature_payload()
    actual = json.loads(CONTRACT_SIGNATURE_PATH.read_text(encoding="utf-8-sig"))
    mismatches = [
        key
        for key in ("contract_id", "version", "schema_version", "sha256", "source")
        if str(actual.get(key) or "") != str(expected.get(key) or "")
    ]
    if mismatches:
        mismatch_text = ", ".join(sorted(mismatches))
        raise SystemExit(f"Contract signature mismatch: {mismatch_text}")
    return actual


def main() -> int:
    if not CONTRACT_YAML_PATH.exists():
        raise SystemExit(f"Missing contract YAML: {CONTRACT_YAML_PATH}")
    if not CONTRACT_SCHEMA_PATH.exists():
        raise SystemExit(f"Missing contract schema: {CONTRACT_SCHEMA_PATH}")
    if not CONTRACT_SIGNATURE_PATH.exists():
        raise SystemExit(f"Missing contract signature: {CONTRACT_SIGNATURE_PATH}")

    contract_payload = _load_yaml()
    _validate_schema(contract_payload)
    signature = _validate_signature()
    print(
        json.dumps(
            {
                "status": "ok",
                "contract": str(CONTRACT_YAML_PATH.relative_to(PROJECT_ROOT)),
                "schema": str(CONTRACT_SCHEMA_PATH.relative_to(PROJECT_ROOT)),
                "signature": str(CONTRACT_SIGNATURE_PATH.relative_to(PROJECT_ROOT)),
                "sha256": signature.get("sha256"),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())