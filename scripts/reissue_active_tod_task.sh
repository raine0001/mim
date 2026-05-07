#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
SERVICE_NAME="${SERVICE_NAME:-mim_tod_auto_reissue}"
REQUEST_FILE="${REQUEST_FILE:-${SHARED_DIR}/MIM_TOD_TASK_REQUEST.latest.json}"
TRIGGER_FILE="${TRIGGER_FILE:-${SHARED_DIR}/MIM_TO_TOD_TRIGGER.latest.json}"
REMOTE_PUBLISH="${REMOTE_PUBLISH:-0}"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/env/.env}"
AUDIT_SCRIPT="${AUDIT_SCRIPT:-${ROOT_DIR}/scripts/tod_bridge_audit.py}"
CONTRACT_TOOL="${CONTRACT_TOOL:-${ROOT_DIR}/scripts/tod_mim_contract_tools.py}"
ENV_TOOLS="${ENV_TOOLS:-${ROOT_DIR}/scripts/env_file_tools.py}"

mkdir -p "${SHARED_DIR}"

if [[ ( -z "${MIM_TOD_SSH_HOST:-}" || -z "${MIM_TOD_SSH_HOST_USER:-}" ) && -f "${ENV_FILE}" ]]; then
  eval "$(python3 "${ENV_TOOLS}" export --file "${ENV_FILE}" --keys MIM_TOD_SSH_HOST MIM_TOD_SSH_USER MIM_TOD_SSH_HOST_USER MIM_TOD_SSH_PORT MIM_TOD_SSH_PASS MIM_TOD_SSH_PASSWORD MIM_TOD_SSH_REMOTE_ROOT REMOTE_ROOT)"
fi

next_bridge_meta() {
  eval "$(python3 "${ROOT_DIR}/scripts/bridge_packet_sequence.py" --shared-dir "${SHARED_DIR}" --service "${SERVICE_NAME}" --instance-id "${SERVICE_NAME}:$$")"
}

record_bridge_audit() {
  local event_name="$1"
  local artifact_path="$2"
  local publish_attempted="${3:-false}"
  local publish_succeeded="${4:-false}"
  local publish_returncode="${5:-0}"
  local publish_output="${6:-}"
  python3 "${AUDIT_SCRIPT}" \
    --event "${event_name}" \
    --caller "scripts/reissue_active_tod_task.sh" \
    --service-name "${SERVICE_NAME}" \
    --task-id "${TASK_ID:-}" \
    --objective-id "${OBJECTIVE_ID:-}" \
    --publish-target "/home/testpilot/mim/runtime/shared -> ${MIM_TOD_SSH_HOST:-192.168.1.120}:${REMOTE_ROOT:-${MIM_TOD_SSH_REMOTE_ROOT:-/home/testpilot/mim/runtime/shared}}" \
    --remote-host "${MIM_TOD_SSH_HOST:-192.168.1.120}" \
    --remote-root "${REMOTE_ROOT:-${MIM_TOD_SSH_REMOTE_ROOT:-/home/testpilot/mim/runtime/shared}}" \
    --publish-attempted "${publish_attempted}" \
    --publish-succeeded "${publish_succeeded}" \
    --publish-returncode "${publish_returncode}" \
    --publish-output "${publish_output}" \
    --artifact-path "${artifact_path}" >/dev/null
}

readarray -t request_meta < <(python3 - <<'PY' "${REQUEST_FILE}"
import json
import sys
from pathlib import Path

request_file = Path(sys.argv[1])
if not request_file.exists():
  raise SystemExit("request file missing")
payload = json.loads(request_file.read_text(encoding="utf-8-sig"))
if not isinstance(payload, dict):
  raise SystemExit("request payload must be a JSON object")
task_id = str(payload.get("task_id") or payload.get("request_id") or "").strip()
if not task_id:
  raise SystemExit("request payload missing task_id")
print(str(payload.get("task_id") or payload.get("request_id") or "").strip())
print(str(payload.get("objective_id") or "").strip())
PY
)

TASK_ID="${request_meta[0]:-}"
OBJECTIVE_ID="${request_meta[1]:-}"

if [[ -z "${TASK_ID}" ]]; then
  echo "[reissue-active-task] request payload missing task_id" >&2
  exit 1
fi

if ! python3 - <<'PY' "${SHARED_DIR}" "${OBJECTIVE_ID}" "${TASK_ID}"
import json
import sys
from pathlib import Path


def read_json(path: Path) -> dict:
  if not path.exists():
    return {}
  try:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
  except Exception:
    return {}
  return payload if isinstance(payload, dict) else {}


def normalize_objective(value: object) -> str:
  text = str(value or "").strip().lower()
  if not text:
    return ""
  if text.startswith("objective-"):
    text = text[len("objective-"):]
  if text.endswith(".0"):
    text = text[:-2]
  digits = []
  for char in text:
    if char.isdigit() or char == ".":
      digits.append(char)
    elif digits:
      break
  return "".join(digits) or text


shared_dir = Path(sys.argv[1])
request_objective = normalize_objective(sys.argv[2])
task_id = str(sys.argv[3]).strip()

context = read_json(shared_dir / "MIM_CONTEXT_EXPORT.latest.json")
handshake = read_json(shared_dir / "MIM_TOD_HANDSHAKE_PACKET.latest.json")
manifest = read_json(shared_dir / "MIM_MANIFEST.latest.json")
integration = read_json(shared_dir / "TOD_INTEGRATION_STATUS.latest.json")

manifest_payload = manifest.get("manifest") if isinstance(manifest.get("manifest"), dict) else {}
truth = handshake.get("truth") if isinstance(handshake.get("truth"), dict) else {}
alignment = integration.get("objective_alignment") if isinstance(integration.get("objective_alignment"), dict) else {}

canonical_objective = normalize_objective(
  context.get("objective_active")
  or truth.get("objective_active")
  or manifest_payload.get("objective_active")
  or alignment.get("mim_objective_active")
  or alignment.get("tod_current_objective")
)

if request_objective and canonical_objective and request_objective != canonical_objective:
  print(
    f"[reissue-active-task] objective mismatch; refusing to republish stale task_id={task_id or '<unknown>'} request_objective={request_objective} canonical_objective={canonical_objective}",
    file=sys.stderr,
  )
  raise SystemExit(1)

raise SystemExit(0)
PY
then
  exit 1
fi

if python3 - <<'PY' "${SHARED_DIR}" "${OBJECTIVE_ID}" "${TASK_ID}"
import json
import re
import sys
from pathlib import Path


TERMINAL_REVIEW_STATES = {"completed", "succeeded", "approved", "done"}


def read_json(path: Path) -> dict:
  if not path.exists():
    return {}
  try:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
  except Exception:
    return {}
  return payload if isinstance(payload, dict) else {}


def normalize_objective(value: object) -> str:
  text = str(value or "").strip().lower()
  if not text:
    return ""
  match = re.search(r"(\d+(?:\.\d+)?)", text)
  return match.group(1) if match else text


shared_dir = Path(sys.argv[1])
request_objective = normalize_objective(sys.argv[2])
task_id = str(sys.argv[3]).strip()
review = read_json(shared_dir / "MIM_TASK_STATUS_REVIEW.latest.json")
task = review.get("task") if isinstance(review.get("task"), dict) else {}
gate = review.get("gate") if isinstance(review.get("gate"), dict) else {}
review_task_id = str(
  task.get("authoritative_task_id")
  or task.get("active_task_id")
  or task.get("request_task_id")
  or task.get("task_id")
  or ""
).strip()
review_objective = normalize_objective(task.get("objective_id") or review_task_id)
state = str(review.get("state") or "").strip().lower()
gate_pass = gate.get("pass") is True
promotion_ready = gate.get("promotion_ready") is True
task_matches = bool(task_id and review_task_id and task_id == review_task_id)
objective_matches = bool(request_objective and review_objective and request_objective == review_objective)
if (task_matches or objective_matches) and state in TERMINAL_REVIEW_STATES and gate_pass:
  print(
    f"[reissue-active-task] request already completed and gate-passing; refusing to republish task_id={task_id or '<unknown>'}",
    file=sys.stderr,
  )
  raise SystemExit(0)
raise SystemExit(1)
PY
then
  echo "${TASK_ID}"
  exit 0
fi

if python3 - <<'PY' "${ROOT_DIR}" "${SHARED_DIR}"
import sys
from pathlib import Path

root_dir = Path(sys.argv[1])
shared_dir = Path(sys.argv[2])
sys.path.insert(0, str(root_dir / "scripts"))

from tod_status_signal_lib import detect_completed_stream_supersession, read_json  # type: ignore

supersession = detect_completed_stream_supersession(
    task_request=read_json(shared_dir / "MIM_TOD_TASK_REQUEST.latest.json"),
    trigger=read_json(shared_dir / "MIM_TO_TOD_TRIGGER.latest.json"),
    task_ack=read_json(shared_dir / "TOD_MIM_TASK_ACK.latest.json"),
    task_result=read_json(shared_dir / "TOD_MIM_TASK_RESULT.latest.json"),
)
if supersession.get("active") is True:
    authoritative_task_id = str(supersession.get("authoritative_task_id") or "").strip()
    stale_request_task_id = str(supersession.get("stale_request_task_id") or "").strip()
    print(
        f"[reissue-active-task] superseded request stream detected; authoritative_task={authoritative_task_id or '<unknown>'} stale_request={stale_request_task_id or '<unknown>'}",
        file=sys.stderr,
    )
    raise SystemExit(0)
raise SystemExit(1)
PY
then
  echo "${TASK_ID}"
  exit 0
fi

next_bridge_meta
REQUEST_SEQ="${SEQUENCE}"
REQUEST_AT="${EMITTED_AT}"
REQUEST_HOST="${SOURCE_HOST}"
REQUEST_SERVICE="${SOURCE_SERVICE}"
REQUEST_INSTANCE="${SOURCE_INSTANCE_ID}"

python3 - <<'PY' \
  "${REQUEST_FILE}" \
  "${REQUEST_AT}" \
  "${REQUEST_SEQ}" \
  "${REQUEST_HOST}" \
  "${REQUEST_SERVICE}" \
  "${REQUEST_INSTANCE}"
import json
import sys
from pathlib import Path

request_file = Path(sys.argv[1])
generated_at = sys.argv[2]
sequence = int(sys.argv[3])
source_host = sys.argv[4]
source_service = sys.argv[5]
source_instance = sys.argv[6]

payload = json.loads(request_file.read_text(encoding="utf-8-sig"))
payload["generated_at"] = generated_at
payload["emitted_at"] = generated_at
payload["sequence"] = sequence
payload["source_host"] = source_host
payload["source_service"] = source_service
payload["source_instance_id"] = source_instance
request_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

python3 "${CONTRACT_TOOL}" normalize-packet \
  --kind request \
  --file "${REQUEST_FILE}" \
  --source-service "${SERVICE_NAME}" >/dev/null

REQUEST_SHA256="$(sha256sum "${REQUEST_FILE}" | awk '{print $1}')"
record_bridge_audit "local_request_write" "${REQUEST_FILE}"

next_bridge_meta
TRIGGER_SEQ="${SEQUENCE}"
TRIGGER_AT="${EMITTED_AT}"
TRIGGER_HOST="${SOURCE_HOST}"
TRIGGER_SERVICE="${SOURCE_SERVICE}"
TRIGGER_INSTANCE="${SOURCE_INSTANCE_ID}"

python3 - <<'PY' \
  "${REQUEST_FILE}" \
  "${TRIGGER_FILE}" \
  "${TRIGGER_AT}" \
  "${TRIGGER_SEQ}" \
  "${TRIGGER_HOST}" \
  "${TRIGGER_SERVICE}" \
  "${TRIGGER_INSTANCE}" \
  "${REQUEST_SHA256}"
import json
import sys
from pathlib import Path

request_file = Path(sys.argv[1])
trigger_file = Path(sys.argv[2])
generated_at = sys.argv[3]
sequence = int(sys.argv[4])
source_host = sys.argv[5]
source_service = sys.argv[6]
source_instance = sys.argv[7]
request_sha = sys.argv[8]

request = json.loads(request_file.read_text(encoding="utf-8-sig"))
task_id = str(request.get("task_id") or request.get("request_id") or "").strip()
objective_id = str(request.get("objective_id") or "").strip()
corr_id = str(request.get("correlation_id") or f"obj{objective_id}-auto-reissue-{task_id}").strip()

payload = {
    "generated_at": generated_at,
    "emitted_at": generated_at,
    "sequence": sequence,
    "packet_type": "shared-trigger-v1",
    "source_actor": "MIM",
    "target_actor": "TOD",
    "source_host": source_host,
    "source_service": source_service,
    "source_instance_id": source_instance,
    "trigger": "task_request_posted",
    "artifact": request_file.name,
    "artifact_path": str(request_file),
    "artifact_sha256": request_sha,
    "task_id": task_id,
    "correlation_id": corr_id,
    "action_required": "pull_latest_and_ack",
    "ack_file_expected": "TOD_TO_MIM_TRIGGER_ACK.latest.json",
}
trigger_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

python3 "${CONTRACT_TOOL}" normalize-packet \
  --kind trigger \
  --file "${TRIGGER_FILE}" \
  --source-service "${SERVICE_NAME}" >/dev/null

record_bridge_audit "local_trigger_write" "${TRIGGER_FILE}"

if [[ "$(printf '%s' "${REMOTE_PUBLISH}" | tr '[:upper:]' '[:lower:]')" == "1" || "$(printf '%s' "${REMOTE_PUBLISH}" | tr '[:upper:]' '[:lower:]')" == "true" || "$(printf '%s' "${REMOTE_PUBLISH}" | tr '[:upper:]' '[:lower:]')" == "yes" ]]; then
  publish_output_file="$(mktemp)"
  publish_returncode=0
  if ! python3 "${ROOT_DIR}/scripts/publish_tod_bridge_artifacts_remote.py" --caller "scripts/reissue_active_tod_task.sh" --request-file "${REQUEST_FILE}" --trigger-file "${TRIGGER_FILE}" --verify-task-id "${TASK_ID}" >"${publish_output_file}" 2>&1; then
    publish_returncode=$?
  fi
  publish_output="$(cat "${publish_output_file}")"
  rm -f "${publish_output_file}"
  record_bridge_audit "remote_publish_result" "${REQUEST_FILE}" true "$([[ ${publish_returncode} -eq 0 ]] && printf true || printf false)" "${publish_returncode}" "${publish_output}"
  record_bridge_audit "remote_publish_result" "${TRIGGER_FILE}" true "$([[ ${publish_returncode} -eq 0 ]] && printf true || printf false)" "${publish_returncode}" "${publish_output}"
  if [[ ${publish_returncode} -ne 0 ]]; then
    printf '%s\n' "${publish_output}" >&2
    exit ${publish_returncode}
  fi
fi

echo "${TASK_ID}"
