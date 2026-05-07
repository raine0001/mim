#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-$ROOT_DIR/runtime/shared}"
EXPECTED_OBJECTIVE="${EXPECTED_OBJECTIVE:-}"

STATUS_FILE="$SHARED_DIR/TOD_INTEGRATION_STATUS.latest.json"
HANDSHAKE_FILE="$SHARED_DIR/MIM_TOD_HANDSHAKE_PACKET.latest.json"
MANIFEST_FILE="$SHARED_DIR/MIM_MANIFEST.latest.json"
CONTEXT_FILE="$SHARED_DIR/MIM_CONTEXT_EXPORT.latest.json"

if [[ ! -f "$STATUS_FILE" ]]; then
  echo "GATE: FAIL"
  echo "- missing file: $STATUS_FILE"
  exit 1
fi

python3 - "$STATUS_FILE" "$HANDSHAKE_FILE" "$MANIFEST_FILE" "$CONTEXT_FILE" "$EXPECTED_OBJECTIVE" <<'PY'
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
handshake_path = Path(sys.argv[2])
manifest_path = Path(sys.argv[3])
context_path = Path(sys.argv[4])
expected_objective_raw = str(sys.argv[5] or "").strip()

status = json.loads(status_path.read_text(encoding="utf-8-sig"))


def read_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None

def get_path(d, *path):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur

def as_int(v):
    try:
        return int(v)
    except Exception:
        return None


def non_empty(value):
    return isinstance(value, str) and bool(value.strip())

compatible = get_path(status, "compatible")
alignment_status = (
    get_path(status, "objective_alignment", "status")
    or get_path(status, "alignment", "status")
    or get_path(status, "status")
)

mim_objective = (
    get_path(status, "objective_alignment", "mim_objective")
    or get_path(status, "objective_alignment", "mim_objective_active")
    or get_path(status, "mim_objective")
    or get_path(status, "mim", "objective")
    or get_path(status, "mim", "current_objective")
    or get_path(status, "mim_status", "objective_active")
    or get_path(status, "mim_handshake", "objective_active")
)

tod_objective = (
    get_path(status, "objective_alignment", "tod_objective")
    or get_path(status, "objective_alignment", "tod_current_objective")
    or get_path(status, "tod_objective")
    or get_path(status, "tod", "objective")
    or get_path(status, "tod", "current_objective")
)

refresh_failure = (
    get_path(status, "refresh", "failure_reason")
    or get_path(status, "refresh", "last_failure_reason")
    or get_path(status, "refresh_failure_reason")
    or get_path(status, "last_refresh_failure")
    or get_path(status, "mim_refresh", "failure_reason")
)

mim_refresh = get_path(status, "mim_refresh") or {}
published_handshake = get_path(status, "mim_handshake") or {}
shared_handshake = read_json(handshake_path) if handshake_path.exists() else None
shared_manifest = read_json(manifest_path) if manifest_path.exists() else None
shared_context = read_json(context_path) if context_path.exists() else None
shared_truth = (shared_handshake or {}).get("truth") or {}
shared_manifest_payload = (shared_manifest or {}).get("manifest") or {}

shared_artifacts_present = shared_handshake is not None and shared_manifest is not None
shared_schema = shared_truth.get("schema_version") or shared_manifest_payload.get("schema_version")
shared_release = shared_truth.get("release_tag") or shared_manifest_payload.get("release_tag")
shared_objective = shared_truth.get("objective_active")

expected_objective = as_int(expected_objective_raw)
if expected_objective is None:
    expected_objective = (
        as_int((shared_context or {}).get("objective_active"))
        or as_int((shared_context or {}).get("objective_in_flight"))
        or as_int(shared_objective)
        or as_int(published_handshake.get("objective_active"))
        or as_int(mim_objective)
        or as_int(tod_objective)
    )

refresh_evidence_ok = True
refresh_evidence_detail = "not required; shared manifest/handshake artifacts not present"
if shared_artifacts_present:
    refresh_evidence_checks = [
        mim_refresh.get("copied_manifest") is True,
        non_empty(mim_refresh.get("source_manifest")),
        non_empty(mim_refresh.get("source_handshake_packet")),
        published_handshake.get("available") is True,
        str(published_handshake.get("objective_active") or "") == str(shared_objective or ""),
        str(published_handshake.get("schema_version") or "") == str(shared_schema or ""),
        str(published_handshake.get("release_tag") or "") == str(shared_release or ""),
        str(get_path(status, "mim_schema") or "") == str(shared_schema or ""),
    ]
    refresh_evidence_ok = all(refresh_evidence_checks)
    refresh_evidence_detail = (
        f"copied_manifest={mim_refresh.get('copied_manifest')!r} "
        f"source_manifest={mim_refresh.get('source_manifest')!r} "
        f"source_handshake_packet={mim_refresh.get('source_handshake_packet')!r} "
        f"handshake_available={published_handshake.get('available')!r} "
        f"published_objective={published_handshake.get('objective_active')!r} shared_objective={shared_objective!r} "
        f"published_schema={published_handshake.get('schema_version')!r} shared_schema={shared_schema!r} "
        f"published_release={published_handshake.get('release_tag')!r} shared_release={shared_release!r} "
        f"mim_schema={get_path(status, 'mim_schema')!r}"
    )

checks = []
checks.append(("compatible == true", compatible is True, compatible))
checks.append((
    "objective_alignment.status in {aligned, in_sync}",
    str(alignment_status).lower() in {"aligned", "in_sync"},
    alignment_status,
))
checks.append((
    f"tod objective == {expected_objective}",
    expected_objective is not None and as_int(tod_objective) == expected_objective,
    tod_objective,
))
checks.append((
    f"mim objective == {expected_objective}",
    expected_objective is not None and as_int(mim_objective) == expected_objective,
    mim_objective,
))
checks.append((
    "refresh failure empty",
    refresh_failure in (None, ""),
    refresh_failure,
))
checks.append((
    "canonical refresh evidence matches shared handshake/manifest truth",
    refresh_evidence_ok,
    refresh_evidence_detail,
))

all_ok = all(ok for _, ok, _ in checks)
print(f"GATE: {'PASS' if all_ok else 'FAIL'}")
for name, ok, value in checks:
    state = "PASS" if ok else "FAIL"
    print(f"- {state}: {name} (value={value!r})")

sys.exit(0 if all_ok else 1)
PY
