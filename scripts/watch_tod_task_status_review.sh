#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_DIR="${SHARED_DIR:-${ROOT_DIR}/runtime/shared}"
LOG_DIR="${LOG_DIR:-${ROOT_DIR}/runtime/logs}"
TASK_STATE_FILE="${TASK_STATE_FILE:-${ROOT_DIR}/tod/state/tasks.json}"
LATEST_JSON_FILE="${LATEST_JSON_FILE:-${SHARED_DIR}/MIM_TASK_STATUS_REVIEW.latest.json}"
NEXT_ACTION_FILE="${NEXT_ACTION_FILE:-${SHARED_DIR}/MIM_TASK_STATUS_NEXT_ACTION.latest.json}"
DECISION_TASK_FILE="${DECISION_TASK_FILE:-${SHARED_DIR}/MIM_DECISION_TASK.latest.json}"
OPERATOR_INCIDENT_FILE="${OPERATOR_INCIDENT_FILE:-${SHARED_DIR}/MIM_OPERATOR_INCIDENT.latest.json}"
SYSTEM_ALERTS_FILE="${SYSTEM_ALERTS_FILE:-${SHARED_DIR}/MIM_SYSTEM_ALERTS.latest.json}"
LATEST_MD_FILE="${LATEST_MD_FILE:-${LOG_DIR}/mim_task_status_review.latest.md}"
EVENT_LOG_FILE="${EVENT_LOG_FILE:-${LOG_DIR}/mim_task_status_review.jsonl}"
POLL_SECONDS="${POLL_SECONDS:-15}"
IDLE_SECONDS="${IDLE_SECONDS:-120}"
RUN_ONCE="${RUN_ONCE:-0}"

mkdir -p "${SHARED_DIR}"
mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG_FILE}"

emit_review() {
  python3 - <<'PY' \
    "${ROOT_DIR}" \
    "${SHARED_DIR}" \
    "${TASK_STATE_FILE}" \
    "${LATEST_JSON_FILE}" \
    "${NEXT_ACTION_FILE}" \
    "${DECISION_TASK_FILE}" \
    "${OPERATOR_INCIDENT_FILE}" \
    "${SYSTEM_ALERTS_FILE}" \
    "${LOG_DIR}" \
    "${LATEST_MD_FILE}" \
    "${EVENT_LOG_FILE}" \
    "${IDLE_SECONDS}"
import json
import sys
from pathlib import Path

root_dir = Path(sys.argv[1])
shared_dir = Path(sys.argv[2])
task_state_file = Path(sys.argv[3])
latest_json_file = Path(sys.argv[4])
next_action_file = Path(sys.argv[5])
decision_task_file = Path(sys.argv[6])
operator_incident_file = Path(sys.argv[7])
system_alerts_file = Path(sys.argv[8])
log_dir = Path(sys.argv[9])
latest_md_file = Path(sys.argv[10])
event_log_file = Path(sys.argv[11])
idle_seconds = int(sys.argv[12])

sys.path.insert(0, str(root_dir / 'scripts'))

from tod_status_signal_lib import build_operator_incident, build_system_alert_summary, read_json, read_active_operator_incident, build_task_status_review  # type: ignore


def read_jsonl_tail(path: Path, limit: int = 200) -> list[dict]:
    if not path.exists() or limit <= 0:
        return []
    try:
        lines = path.read_text(encoding='utf-8-sig').splitlines()
    except Exception:
        return []
    rows: list[dict] = []
    for raw in lines[-limit:]:
        text = str(raw).strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


system_alert_summary = build_system_alert_summary(
    stale_ack_watchdog=read_json(log_dir / 'objective75_stale_ack_watchdog.latest.json'),
    catchup_status=read_json(log_dir / 'tod_catchup_status.latest.json'),
    liveness_events=read_jsonl_tail(shared_dir / 'TOD_LIVENESS_EVENTS.latest.jsonl', limit=200),
)
system_alerts_file.write_text(json.dumps(system_alert_summary, indent=2) + '\n', encoding='utf-8')

review = build_task_status_review(
    task_request=read_json(shared_dir / 'MIM_TOD_TASK_REQUEST.latest.json'),
    trigger=read_json(shared_dir / 'MIM_TO_TOD_TRIGGER.latest.json'),
    trigger_ack=read_json(shared_dir / 'TOD_TO_MIM_TRIGGER_ACK.latest.json'),
    task_ack=read_json(shared_dir / 'TOD_MIM_TASK_ACK.latest.json'),
    task_result=read_json(shared_dir / 'TOD_MIM_TASK_RESULT.latest.json'),
    catchup_gate=read_json(shared_dir / 'TOD_CATCHUP_GATE.latest.json'),
    troubleshooting_authority=read_json(shared_dir / 'MIM_TOD_TROUBLESHOOTING_AUTHORITY.latest.json'),
    persistent_task=read_json(task_state_file),
    system_alert_summary=system_alert_summary,
    idle_seconds=idle_seconds,
)

consume_evidence = read_json(shared_dir / 'MIM_TOD_CONSUME_EVIDENCE.latest.json') or {}
consume_watch = consume_evidence.get('watch') if isinstance(consume_evidence.get('watch'), dict) else {}
consume_mutations = consume_evidence.get('first_mutations') if isinstance(consume_evidence.get('first_mutations'), dict) else {}
active_task_id = str((review.get('task') or {}).get('active_task_id') or '').strip()
consume_task_id = str(consume_evidence.get('task_id') or '').strip()
if active_task_id and consume_task_id == active_task_id:
    watch_phase = str(consume_watch.get('phase') or '').strip().lower()
    watch_timed_out = bool(consume_watch.get('timed_out') is True)
    ack_mutation = consume_mutations.get('task_ack') if isinstance(consume_mutations.get('task_ack'), dict) else None
    result_mutation = consume_mutations.get('task_result') if isinstance(consume_mutations.get('task_result'), dict) else None
    review_state = str(review.get('state') or '')
    if watch_timed_out and watch_phase == 'timeout' and review_state not in {'completed', 'failed'}:
        reasons = review.get('blocking_reason_codes', [])
        if isinstance(reasons, list) and 'consume_watch_timeout' not in reasons:
            reasons.append('consume_watch_timeout')
        review['blocking_reason_codes'] = reasons
        actions = review.get('pending_actions', [])
        if not isinstance(actions, list):
            actions = []
        if all(not isinstance(item, dict) or str(item.get('code') or '') != 'escalate_tod_consume_timeout' for item in actions):
            actions.append(
                {
                    'code': 'escalate_tod_consume_timeout',
                    'detail': 'TOD consume watch window elapsed without current-task ACK/RESULT mutation; escalate consume bridge recovery and reissue task under guarded dispatch.',
                }
            )
        review['pending_actions'] = actions
        if review_state in {'queued', 'awaiting_trigger_ack', 'awaiting_task_ack', 'awaiting_result'}:
            review['state'] = 'idle_blocked'
            review['state_reason'] = 'consume_watch_timeout'
    if ack_mutation is not None and result_mutation is not None and str(review.get('state') or '') in {'idle_blocked', 'dispatch_blocked'}:
        reasons = [
            item
            for item in (review.get('blocking_reason_codes') or [])
            if str(item or '') != 'consume_watch_timeout'
        ]
        review['blocking_reason_codes'] = reasons

latest_json_file.write_text(json.dumps(review, indent=2) + '\n', encoding='utf-8')
with event_log_file.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps(review, separators=(',', ':')) + '\n')

pending_actions = review.get('pending_actions', [])
selected_action = {}
if isinstance(pending_actions, list) and pending_actions:
    highest_alert_severity = str((system_alert_summary or {}).get('highest_severity') or '').strip().lower()
    if highest_alert_severity == 'critical':
        for candidate in pending_actions:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get('code') or '').strip() == 'acknowledge_and_remediate_system_alerts':
                selected_action = candidate
                break
    if not selected_action:
        first = pending_actions[0]
        selected_action = first if isinstance(first, dict) else {}

next_action_payload = {
    'generated_at': review.get('generated_at'),
    'type': 'mim_task_status_next_action_v1',
    'state': review.get('state'),
    'state_reason': review.get('state_reason'),
    'idle_active': bool((review.get('idle') or {}).get('active', False)),
    'active_task_id': (review.get('task') or {}).get('active_task_id', ''),
    'objective_id': (review.get('task') or {}).get('objective_id', ''),
    'selected_action': {
        'code': str(selected_action.get('code') or 'monitor_only'),
        'detail': str(selected_action.get('detail') or 'No blocking action selected; continue monitoring.'),
    },
    'blocking_reason_codes': review.get('blocking_reason_codes', []),
    'system_alerts': {
        'active': bool((system_alert_summary or {}).get('active', False)),
        'highest_severity': str((system_alert_summary or {}).get('highest_severity') or 'none'),
        'primary_alert_code': str(((system_alert_summary or {}).get('primary_alert') or {}).get('code') or ''),
    },
    'pending_action_count': len(pending_actions) if isinstance(pending_actions, list) else 0,
    'escalation_recommended': bool(
        str(review.get('state') or '') in {'idle_blocked', 'dispatch_blocked', 'failed'}
        or bool(review.get('blocking_reason_codes'))
        or str((system_alert_summary or {}).get('highest_severity') or '').strip().lower() == 'critical'
    ),
}
next_action_file.write_text(json.dumps(next_action_payload, indent=2) + '\n', encoding='utf-8')

decision_task_payload = {
    'generated_at': review.get('generated_at'),
    'type': 'mim_decision_task_v1',
    'owner_actor': 'MIM',
    'target_actor': 'TOD',
    'state': review.get('state'),
    'state_reason': review.get('state_reason'),
    'active_task_id': (review.get('task') or {}).get('active_task_id', ''),
    'objective_id': (review.get('task') or {}).get('objective_id', ''),
    'decision': {
        'code': str(selected_action.get('code') or 'monitor_only'),
        'detail': str(selected_action.get('detail') or 'No blocking action selected; continue monitoring.'),
        'decision_owner': 'MIM',
        'execution_required': bool(next_action_payload.get('escalation_recommended', False)),
    },
    'system_alerts': next_action_payload.get('system_alerts', {}),
    'blocking_reason_codes': review.get('blocking_reason_codes', []),
}
decision_task_file.write_text(json.dumps(decision_task_payload, indent=2) + '\n', encoding='utf-8')

existing_incident = read_active_operator_incident(shared_dir)
operator_incident = build_operator_incident(
    review=review,
    next_action=next_action_payload,
    decision_task=decision_task_payload,
    existing_incident=existing_incident,
    shared_dir=shared_dir,
)
if operator_incident.get('active') is True:
    review_path = Path(str(operator_incident.get('review_path') or '')).expanduser()
    next_action_path = Path(str(operator_incident.get('next_action_path') or '')).expanduser()
    decision_path = Path(str(operator_incident.get('decision_task_path') or '')).expanduser()
    for path in (review_path, next_action_path, decision_path, operator_incident_file):
        path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(review, indent=2) + '\n', encoding='utf-8')
    next_action_path.write_text(json.dumps(next_action_payload, indent=2) + '\n', encoding='utf-8')
    decision_path.write_text(json.dumps(decision_task_payload, indent=2) + '\n', encoding='utf-8')
    operator_incident_file.write_text(json.dumps(operator_incident, indent=2) + '\n', encoding='utf-8')

task = review.get('task', {}) if isinstance(review.get('task'), dict) else {}
md_lines = [
    '# MIM Task Status Review',
    '',
    f"Generated: {review.get('generated_at', '')}",
    '',
    f"- state: {review.get('state', '')}",
    f"- state_reason: {review.get('state_reason', '')}",
    f"- active_task_id: {task.get('active_task_id', '')}",
    f"- objective_id: {task.get('objective_id', '')}",
    f"- trigger_name: {task.get('trigger_name', '')}",
    f"- gate_pass: {review.get('gate', {}).get('pass')}",
    f"- authority_ok: {review.get('authority', {}).get('ok')}",
    f"- idle_active: {review.get('idle', {}).get('active')}",
    f"- latest_progress_age_seconds: {review.get('idle', {}).get('latest_progress_age_seconds')}",
]
reason_codes = review.get('blocking_reason_codes', [])
if isinstance(reason_codes, list) and reason_codes:
    md_lines.extend(['', 'Blocking reasons:'])
    for code in reason_codes:
        md_lines.append(f"- {code}")
pending_actions = review.get('pending_actions', [])
if isinstance(pending_actions, list) and pending_actions:
    md_lines.extend(['', 'Pending actions:'])
    for action in pending_actions:
        if not isinstance(action, dict):
            continue
        md_lines.append(f"- {action.get('code', '')}: {action.get('detail', '')}")
latest_md_file.write_text('\n'.join(md_lines) + '\n', encoding='utf-8')

print(review.get('state', ''))
print(review.get('state_reason', ''))
print(str(review.get('idle', {}).get('active', False)).lower())
PY
}

echo "[tod-task-review] watching ${SHARED_DIR} every ${POLL_SECONDS}s (idle>=${IDLE_SECONDS}s)"

while true; do
  out="$(emit_review)"
  state="$(echo "${out}" | sed -n '1p')"
  reason="$(echo "${out}" | sed -n '2p')"
  idle_flag="$(echo "${out}" | sed -n '3p')"
  echo "[tod-task-review] state=${state} reason=${reason} idle=${idle_flag}"

  if [[ "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "1" || "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "true" || "$(printf '%s' "${RUN_ONCE}" | tr '[:upper:]' '[:lower:]')" == "yes" ]]; then
    break
  fi

  sleep "${POLL_SECONDS}"
done