#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"

REVIEW_FILE="${REVIEW_FILE:-${SHARED_DIR}/MIM_TASK_STATUS_REVIEW.latest.json}"
EVIDENCE_FILE="${EVIDENCE_FILE:-${SHARED_DIR}/MIM_TOD_CONSUME_EVIDENCE.latest.json}"
AUTO_ESCALATION_FILE="${AUTO_ESCALATION_FILE:-${SHARED_DIR}/MIM_TOD_AUTO_ESCALATION.latest.json}"
STATE_FILE="${STATE_FILE:-${LOG_DIR}/mim_tod_consume_timeout_policy.state.json}"
STATUS_FILE="${STATUS_FILE:-${LOG_DIR}/mim_tod_consume_timeout_policy.latest.json}"
EVENT_LOG_FILE="${EVENT_LOG_FILE:-${LOG_DIR}/mim_tod_consume_timeout_policy.jsonl}"

POLL_SECONDS="${POLL_SECONDS:-10}"
RUN_ONCE="${RUN_ONCE:-0}"
REMOTE_PUBLISH="${REMOTE_PUBLISH:-1}"
RETRY_COOLDOWN_SECONDS="${RETRY_COOLDOWN_SECONDS:-120}"

mkdir -p "${SHARED_DIR}"
mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG_FILE}"

policy_cycle() {
  python3 - <<'PY' \
    "${ROOT_DIR}" \
    "${REVIEW_FILE}" \
    "${EVIDENCE_FILE}" \
    "${AUTO_ESCALATION_FILE}" \
    "${STATE_FILE}" \
    "${STATUS_FILE}" \
    "${EVENT_LOG_FILE}" \
    "${REMOTE_PUBLISH}" \
    "${RETRY_COOLDOWN_SECONDS}"
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

root_dir = Path(sys.argv[1])
review_file = Path(sys.argv[2])
evidence_file = Path(sys.argv[3])
auto_escalation_file = Path(sys.argv[4])
state_file = Path(sys.argv[5])
status_file = Path(sys.argv[6])
event_log_file = Path(sys.argv[7])
remote_publish = str(sys.argv[8]).strip().lower() in {"1", "true", "yes"}
retry_cooldown_seconds = max(0, int(sys.argv[9]))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: object):
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


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


reference = now_iso()
review = read_json(review_file)
evidence = read_json(evidence_file)
state = read_json(state_file)

review_task = review.get("task") if isinstance(review.get("task"), dict) else {}
task_id = str(review_task.get("active_task_id") or evidence.get("task_id") or "").strip()
blocking_reason_codes = review.get("blocking_reason_codes") if isinstance(review.get("blocking_reason_codes"), list) else []
evidence_watch = evidence.get("watch") if isinstance(evidence.get("watch"), dict) else {}
evidence_started_at = str(evidence_watch.get("started_at") or "").strip()
timed_out = bool(evidence_watch.get("timed_out") is True)
phase = str(evidence_watch.get("phase") or "").strip().lower()
timeout_detected = bool(task_id and timed_out and phase == "timeout") or ("consume_watch_timeout" in [str(item) for item in blocking_reason_codes])

already_handled = (
    str(state.get("handled_task_id") or "") == task_id
    and str(state.get("handled_watch_started_at") or "") == evidence_started_at
)
last_failure_at = parse_iso(state.get("last_failure_at"))
cooldown_active = False
if last_failure_at is not None and str(state.get("last_result") or "") == "failure":
    cooldown_active = (datetime.now(timezone.utc) - last_failure_at).total_seconds() < retry_cooldown_seconds

status_payload = {
    "generated_at": reference,
    "type": "mim_tod_consume_timeout_policy_status_v1",
    "task_id": task_id,
    "timeout_detected": timeout_detected,
    "already_handled": already_handled,
    "cooldown_active": cooldown_active,
    "last_action": str(state.get("last_action") or "idle"),
    "last_result": str(state.get("last_result") or ""),
}

event = {
    "generated_at": reference,
    "task_id": task_id,
    "timeout_detected": timeout_detected,
    "already_handled": already_handled,
    "cooldown_active": cooldown_active,
}

if timeout_detected and not already_handled and not cooldown_active:
    command = [str(root_dir / "scripts" / "reissue_active_tod_task.sh")]
    env = None
    if remote_publish:
        env = __import__("os").environ.copy()
        env["REMOTE_PUBLISH"] = "1"
    completed = subprocess.run(command, cwd=root_dir, capture_output=True, text=True, env=env, check=False)
    success = completed.returncode == 0
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    auto_payload = {
        "generated_at": reference,
        "type": "mim_tod_auto_escalation_v1",
        "task_id": task_id,
        "trigger_reason": "consume_watch_timeout",
        "watch_started_at": evidence_started_at,
        "action": {
            "code": "auto_reissue_and_republish_task",
            "remote_publish": remote_publish,
            "success": success,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": completed.returncode,
        },
    }
    write_json(auto_escalation_file, auto_payload)
    if success:
        state.update(
            {
                "handled_task_id": task_id,
                "handled_watch_started_at": evidence_started_at,
                "last_action": "auto_reissue_and_republish_task",
                "last_result": "success",
                "last_failure_at": "",
                "last_updated_at": reference,
            }
        )
    else:
        state.update(
            {
                "last_action": "auto_reissue_and_republish_task",
                "last_result": "failure",
                "last_failure_at": reference,
                "last_updated_at": reference,
            }
        )
    status_payload.update(
        {
            "last_action": "auto_reissue_and_republish_task",
            "last_result": "success" if success else "failure",
            "returncode": completed.returncode,
        }
    )
    event.update(
        {
            "action": "auto_reissue_and_republish_task",
            "success": success,
            "returncode": completed.returncode,
        }
    )
else:
    if not timeout_detected:
        state.update({"last_action": "idle", "last_result": "", "last_failure_at": "", "last_updated_at": reference})

write_json(state_file, state)
write_json(status_file, status_payload)
with event_log_file.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(event, separators=(",", ":")) + "\n")

print("triggered" if timeout_detected else "idle")
print("handled" if already_handled else "pending")
print(status_payload.get("last_result", ""))
PY
}

echo "[tod-consume-timeout-policy] watching for consume timeout every ${POLL_SECONDS}s"

while true; do
  out="$(policy_cycle)"
  state="$(echo "${out}" | sed -n '1p')"
  handled="$(echo "${out}" | sed -n '2p')"
  result="$(echo "${out}" | sed -n '3p')"
  echo "[tod-consume-timeout-policy] state=${state} handled=${handled} result=${result}"

  run_once="$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')"
  if [[ "${run_once}" == "1" || "${run_once}" == "true" || "${run_once}" == "yes" ]]; then
    break
  fi

  sleep "${POLL_SECONDS}"
done
