#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-$ROOT_DIR/runtime/shared}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/runtime/logs}"
STATE_FILE="${STATE_FILE:-$LOG_DIR/tod_recoupling_gate_state.latest.json}"
EXPECTED_OBJECTIVE="${EXPECTED_OBJECTIVE:-75}"
MAX_ACK_AGE_SECONDS="${MAX_ACK_AGE_SECONDS:-180}"
REQUIRED_CONSECUTIVE="${REQUIRED_CONSECUTIVE:-3}"

ACK_FILE="$SHARED_DIR/TOD_TO_MIM_TRIGGER_ACK.latest.json"
STATUS_FILE="$SHARED_DIR/TOD_INTEGRATION_STATUS.latest.json"
RESULT_FILE="$SHARED_DIR/TOD_MIM_TASK_RESULT.latest.json"
CATCHUP_FILE="$SHARED_DIR/TOD_CATCHUP_GATE.latest.json"
HANDSHAKE_FILE="$SHARED_DIR/MIM_TOD_HANDSHAKE_PACKET.latest.json"
MANIFEST_FILE="$SHARED_DIR/MIM_MANIFEST.latest.json"

mkdir -p "$LOG_DIR"

python3 - <<'PY' "$ACK_FILE" "$STATUS_FILE" "$RESULT_FILE" "$CATCHUP_FILE" "$HANDSHAKE_FILE" "$MANIFEST_FILE" "$STATE_FILE" "$EXPECTED_OBJECTIVE" "$MAX_ACK_AGE_SECONDS" "$REQUIRED_CONSECUTIVE"
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ack_file = Path(sys.argv[1])
status_file = Path(sys.argv[2])
result_file = Path(sys.argv[3])
catchup_file = Path(sys.argv[4])
handshake_file = Path(sys.argv[5])
manifest_file = Path(sys.argv[6])
state_file = Path(sys.argv[7])
expected_objective = int(sys.argv[8])
max_ack_age_seconds = int(sys.argv[9])
required_consecutive = int(sys.argv[10])


def parse_ts(value):
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except Exception:
        return None


def read_json(path: Path):
    if not path.exists():
        return None, f"missing file: {path}"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), ""
    except Exception as exc:
        return None, f"read error {path}: {exc}"


def normalize_objective(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def non_empty(value):
    return isinstance(value, str) and bool(value.strip())


ack, ack_err = read_json(ack_file)
status, status_err = read_json(status_file)
result, result_err = read_json(result_file)
catchup, catchup_err = read_json(catchup_file)
shared_handshake, shared_handshake_err = read_json(handshake_file)
shared_manifest, shared_manifest_err = read_json(manifest_file)

checks = []
now = datetime.now(timezone.utc)

if ack_err:
    checks.append(("trigger_ack_fresh", False, ack_err))
else:
    ack_status = str(ack.get("status") or "").strip().lower()
    ack_ts_raw = ack.get("generated_at")
    ack_ts = parse_ts(ack_ts_raw)
    if ack_ts is None:
        checks.append(("trigger_ack_fresh", False, f"invalid generated_at={ack_ts_raw!r}"))
    else:
        age = int((now - ack_ts).total_seconds())
        fresh = age <= max_ack_age_seconds
        status_ok = ack_status == "acknowledged"
        checks.append((
            "trigger_ack_fresh",
            fresh and status_ok,
            f"status={ack_status or '<empty>'} age_seconds={age} max={max_ack_age_seconds}",
        ))

if status_err:
    checks.append(("objective_alignment", False, status_err))
    checks.append(("canonical_refresh_evidence", False, status_err))
else:
    alignment = status.get("objective_alignment") or {}
    mim_refresh = status.get("mim_refresh") or {}
    published_handshake = status.get("mim_handshake") or {}
    aligned = alignment.get("aligned")
    alignment_status = str(alignment.get("status") or "").strip().lower()
    tod_obj = normalize_objective(alignment.get("tod_current_objective"))
    mim_obj = normalize_objective(alignment.get("mim_objective_active"))

    # Fallback if objective_alignment is sparse.
    if mim_obj is None:
        mim_status = status.get("mim_status") or {}
        mim_handshake = status.get("mim_handshake") or {}
        mim_obj = normalize_objective(mim_status.get("objective_active"))
        if mim_obj is None:
            mim_obj = normalize_objective(mim_handshake.get("objective_active"))

    aligned_ok = (aligned is True) or (alignment_status in {"aligned", "in_sync"})
    objective_ok = (tod_obj == expected_objective) and (mim_obj == expected_objective)
    checks.append((
        "objective_alignment",
        aligned_ok and objective_ok,
        f"aligned={aligned!r} status={alignment_status!r} tod={tod_obj!r} mim={mim_obj!r} expected={expected_objective}",
    ))

    if shared_handshake_err or shared_manifest_err:
        checks.append((
            "canonical_refresh_evidence",
            False,
            shared_handshake_err or shared_manifest_err,
        ))
    elif shared_handshake is None or shared_manifest is None:
        checks.append((
            "canonical_refresh_evidence",
            True,
            "not required; shared manifest/handshake artifacts not present",
        ))
    else:
        shared_truth = shared_handshake.get("truth") or {}
        shared_manifest_payload = shared_manifest.get("manifest") or {}
        shared_schema = shared_truth.get("schema_version") or shared_manifest_payload.get("schema_version")
        shared_release = shared_truth.get("release_tag") or shared_manifest_payload.get("release_tag")
        shared_objective = shared_truth.get("objective_active")
        evidence_ok = all([
            mim_refresh.get("copied_manifest") is True,
            non_empty(mim_refresh.get("source_manifest")),
            non_empty(mim_refresh.get("source_handshake_packet")),
            published_handshake.get("available") is True,
            str(published_handshake.get("objective_active") or "") == str(shared_objective or ""),
            str(published_handshake.get("schema_version") or "") == str(shared_schema or ""),
            str(published_handshake.get("release_tag") or "") == str(shared_release or ""),
            str(status.get("mim_schema") or "") == str(shared_schema or ""),
        ])
        checks.append((
            "canonical_refresh_evidence",
            evidence_ok,
            (
                f"copied_manifest={mim_refresh.get('copied_manifest')!r} "
                f"source_manifest={mim_refresh.get('source_manifest')!r} "
                f"source_handshake_packet={mim_refresh.get('source_handshake_packet')!r} "
                f"handshake_available={published_handshake.get('available')!r} "
                f"published_objective={published_handshake.get('objective_active')!r} shared_objective={shared_objective!r} "
                f"published_schema={published_handshake.get('schema_version')!r} shared_schema={shared_schema!r} "
                f"published_release={published_handshake.get('release_tag')!r} shared_release={shared_release!r} "
                f"mim_schema={status.get('mim_schema')!r}"
            ),
        ))

if result_err:
    checks.append(("review_gate_passed", False, result_err))
else:
    review_gate = result.get("review_gate") or {}
    passed = review_gate.get("passed") is True
    request_id = str(result.get("request_id") or "").strip()
    checks.append(("review_gate_passed", passed, f"passed={review_gate.get('passed')!r} request_id={request_id or '<empty>'}"))

if catchup_err:
    checks.append(("catchup_gate_pass", False, catchup_err))
else:
    gate_pass = catchup.get("gate_pass") is True
    objective = normalize_objective(catchup.get("objective"))
    checks.append((
        "catchup_gate_pass",
        gate_pass,
        f"gate_pass={catchup.get('gate_pass')!r} objective={objective!r}",
    ))

sample_pass = all(ok for _, ok, _ in checks)

state = {
    "last_ack_generated_at": None,
    "pass_streak": 0,
    "last_sample_pass": False,
}
if state_file.exists():
    try:
        state.update(json.loads(state_file.read_text(encoding="utf-8")))
    except Exception:
        pass

current_ack_ts = None
if ack and not ack_err:
    current_ack_ts = str(ack.get("generated_at") or "").strip() or None

if sample_pass:
    if state.get("last_sample_pass"):
        state["pass_streak"] = int(state.get("pass_streak", 0)) + 1
    else:
        state["pass_streak"] = 1
else:
    state["pass_streak"] = 0

state["last_sample_pass"] = sample_pass
state["last_ack_generated_at"] = current_ack_ts
state["updated_at"] = now.isoformat().replace("+00:00", "Z")
state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

recoupling_ready = sample_pass and int(state.get("pass_streak", 0)) >= required_consecutive

print(f"RECOUPLING_GATE: {'PASS' if recoupling_ready else 'FAIL'}")
print(f"- sample_pass: {sample_pass}")
print(f"- pass_streak: {state.get('pass_streak', 0)}/{required_consecutive}")
print(f"- state_file: {state_file}")
for name, ok, detail in checks:
    print(f"- {'PASS' if ok else 'FAIL'}: {name} ({detail})")

sys.exit(0 if recoupling_ready else 1)
PY
