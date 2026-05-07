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
IDLE_SECONDS="${IDLE_SECONDS:-60}"
TOD_CONSOLE_URL="${TOD_CONSOLE_URL:-http://192.168.1.161:8844}"
FORMAL_PROGRAM_RESPONSE_FILE="${FORMAL_PROGRAM_RESPONSE_FILE:-${ROOT_DIR}/runtime/formal_program_drive_response.json}"
RUN_ONCE="${RUN_ONCE:-0}"
AUTO_REFRESH_ALIGNMENT="${AUTO_REFRESH_ALIGNMENT:-1}"

mkdir -p "${SHARED_DIR}"
mkdir -p "${LOG_DIR}"
touch "${EVENT_LOG_FILE}"

should_run_flag() {
    local value
    value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    [[ "${value}" == "1" || "${value}" == "true" || "${value}" == "yes" ]]
}

refresh_alignment_if_drifted() {
    should_run_flag "${AUTO_REFRESH_ALIGNMENT}" || return 0

    local decision
    decision="$(python3 - <<'PY' "${SHARED_DIR}" "${FORMAL_PROGRAM_RESPONSE_FILE}"
import json
import re
import sys
from pathlib import Path

shared_dir = Path(sys.argv[1])
formal_program_path = Path(sys.argv[2])


def read_json(path: Path) -> dict:
        if not path.exists():
                return {}
        try:
                payload = json.loads(path.read_text(encoding='utf-8-sig'))
        except Exception:
                return {}
        return payload if isinstance(payload, dict) else {}


def normalize_objective(value: object) -> str:
        text = str(value or '').strip()
        if not text:
                return ''
        match = re.search(r'(\d+(?:\.\d+)?)', text)
        return match.group(1) if match else text


request = read_json(shared_dir / 'MIM_TOD_TASK_REQUEST.latest.json')
context_export = read_json(shared_dir / 'MIM_CONTEXT_EXPORT.latest.json')
integration = read_json(shared_dir / 'TOD_INTEGRATION_STATUS.latest.json')
alignment_request = read_json(shared_dir / 'MIM_TOD_ALIGNMENT_REQUEST.latest.json')
review = read_json(shared_dir / 'MIM_TASK_STATUS_REVIEW.latest.json')
formal_program = read_json(formal_program_path)


def first_text(payload: dict, *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or '').strip()
        if value:
            return value
    return ''

live_objective = normalize_objective(
        request.get('objective_id') or request.get('objective') or request.get('task_id') or request.get('request_id')
)
export_objective = normalize_objective(
        context_export.get('objective_active') or context_export.get('objective_in_flight') or context_export.get('current_next_objective')
)
alignment = integration.get('objective_alignment') if isinstance(integration.get('objective_alignment'), dict) else {}
integration_mim_objective = normalize_objective(alignment.get('mim_objective_active') or alignment.get('mim_objective'))
integration_tod_objective = normalize_objective(alignment.get('tod_current_objective') or alignment.get('tod_objective'))
alignment_request_objective = normalize_objective(
        (alignment_request.get('mim_truth') or {}).get('objective_active') if isinstance(alignment_request.get('mim_truth'), dict) else ''
)
task_payload = review.get('task') if isinstance(review.get('task'), dict) else {}
gate_payload = review.get('gate') if isinstance(review.get('gate'), dict) else {}
review_task_id = first_text(
    task_payload,
    'authoritative_task_id',
    'active_task_id',
    'request_task_id',
    'task_id',
)
review_objective = normalize_objective(task_payload.get('objective_id') or review_task_id)
review_state = first_text(review, 'state').lower()
gate_pass = gate_payload.get('pass') is True
terminal_completed_request = bool(
    live_objective
    and review_objective
    and live_objective == review_objective
    and review_state in {'completed', 'succeeded', 'approved', 'done'}
    and gate_pass
    and export_objective
    and export_objective != live_objective
)
formal_objective = normalize_objective(
    ((formal_program.get('objective') or {}).get('objective_id') if isinstance(formal_program.get('objective'), dict) else '')
    or ((((formal_program.get('continuation') or {}).get('status') or {}).get('active_task') or {}).get('objective_id') if isinstance((((formal_program.get('continuation') or {}).get('status') or {}).get('active_task')), dict) else '')
)
formal_execution_state = first_text(formal_program, 'execution_state').lower()
formal_status = first_text(
    formal_program.get('objective') if isinstance(formal_program.get('objective'), dict) else {},
    'status',
).lower()
formal_active = bool(
    formal_objective
    and (formal_execution_state in {'executing', 'in_progress', 'working', 'queued', 'created'} or formal_status in {'executing', 'in_progress', 'working', 'queued', 'created'})
)

needs_refresh = bool(
    (
        live_objective
        and not terminal_completed_request
        and (
            live_objective != export_objective
            or live_objective != integration_mim_objective
            or (integration_tod_objective and live_objective != integration_tod_objective)
            or live_objective != alignment_request_objective
            or not alignment_request
        )
    )
    or (
        formal_active
        and (
            formal_objective != export_objective
            or formal_objective != integration_mim_objective
            or (integration_tod_objective and formal_objective != integration_tod_objective)
            or formal_objective != alignment_request_objective
            or not alignment_request
        )
    )
)

print(json.dumps({
        'needs_refresh': needs_refresh,
        'live_objective': live_objective,
        'formal_objective': formal_objective,
        'export_objective': export_objective,
        'integration_mim_objective': integration_mim_objective,
        'integration_tod_objective': integration_tod_objective,
        'alignment_request_objective': alignment_request_objective,
    'terminal_completed_request': terminal_completed_request,
}))
PY
)"

    local needs_refresh
    needs_refresh="$(python3 - <<'PY' "${decision}"
import json
import sys
payload = json.loads(sys.argv[1])
print('1' if payload.get('needs_refresh') else '0')
PY
)"

    if [[ "${needs_refresh}" != "1" ]]; then
        return 0
    fi

    if python3 "${ROOT_DIR}/scripts/refresh_formal_program_drive_response.py" --output "${FORMAL_PROGRAM_RESPONSE_FILE}" >/dev/null 2>&1 \
        && python3 "${ROOT_DIR}/scripts/export_mim_context.py" --output-dir "${SHARED_DIR}" >/dev/null 2>&1 \
        && python3 "${ROOT_DIR}/scripts/rebuild_tod_integration_status.py" --shared-dir "${SHARED_DIR}" --mirror-legacy-alias >/dev/null 2>&1; then
        echo "[tod-review] refreshed formal and MIM/TOD alignment artifacts"
    else
        echo "[tod-review] WARN failed to refresh formal or MIM/TOD alignment artifacts" >&2
    fi
}

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
    "${IDLE_SECONDS}" \
    "${TOD_CONSOLE_URL}" \
    "${FORMAL_PROGRAM_RESPONSE_FILE}"
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
tod_console_url = str(sys.argv[13]).strip()
formal_program_response_file = Path(sys.argv[14])

sys.path.insert(0, str(root_dir / 'scripts'))

from tod_status_signal_lib import build_mim_tod_decision_snapshot, build_operator_incident, build_system_alert_summary, reconcile_system_alert_summary_for_review, read_json, read_active_operator_incident, build_task_status_review  # type: ignore


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

review = build_task_status_review(
    task_request=read_json(shared_dir / 'MIM_TOD_TASK_REQUEST.latest.json'),
    trigger=read_json(shared_dir / 'MIM_TO_TOD_TRIGGER.latest.json'),
    trigger_ack=read_json(shared_dir / 'TOD_TO_MIM_TRIGGER_ACK.latest.json'),
    task_ack=read_json(shared_dir / 'TOD_MIM_TASK_ACK.latest.json'),
    task_result=read_json(shared_dir / 'TOD_MIM_TASK_RESULT.latest.json'),
    catchup_gate=read_json(shared_dir / 'TOD_CATCHUP_GATE.latest.json'),
    troubleshooting_authority=read_json(shared_dir / 'MIM_TOD_TROUBLESHOOTING_AUTHORITY.latest.json'),
    persistent_task=read_json(task_state_file),
    formal_program_response=read_json(formal_program_response_file),
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

system_alert_summary = reconcile_system_alert_summary_for_review(
    system_alert_summary=system_alert_summary,
    review=review,
)
review['system_alerts'] = {
    'active': bool((system_alert_summary or {}).get('active', False)),
    'highest_severity': str((system_alert_summary or {}).get('highest_severity') or 'none'),
    'primary_alert_code': str(((system_alert_summary or {}).get('primary_alert') or {}).get('code') or ''),
}
system_alerts_file.write_text(json.dumps(system_alert_summary, indent=2) + '\n', encoding='utf-8')

latest_json_file.write_text(json.dumps(review, indent=2) + '\n', encoding='utf-8')
with event_log_file.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps(review, separators=(',', ':')) + '\n')

pending_actions = review.get('pending_actions', [])
selected_action = {}
if isinstance(pending_actions, list) and pending_actions:
    highest_alert_severity = str((system_alert_summary or {}).get('highest_severity') or '').strip().lower()
    priority_codes = []
    if highest_alert_severity == 'critical':
        priority_codes.append('acknowledge_and_remediate_system_alerts')
    priority_codes.extend([
        'recouple_publisher_objective',
        'fallback_to_codex_direct_execution',
        'declare_tod_emergency',
    ])
    for priority_code in priority_codes:
        for candidate in pending_actions:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get('code') or '').strip() == priority_code:
                selected_action = candidate
                break
        if selected_action:
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

decision_process = build_mim_tod_decision_snapshot(
    review=review,
    next_action=next_action_payload,
    system_alert_summary=system_alert_summary,
    coordination_request=read_json(shared_dir / 'TOD_MIM_COORDINATION_REQUEST.latest.json'),
    coordination_ack=read_json(shared_dir / 'MIM_TOD_COORDINATION_ACK.latest.json'),
    ping_response=read_json(shared_dir / 'TOD_TO_MIM_PING.latest.json'),
    console_probe=read_json(shared_dir / 'TOD_CONSOLE_PROBE.latest.json'),
    tod_console_url=tod_console_url,
)

existing_decision_task = read_json(decision_task_file) or {}
existing_communication_escalation = existing_decision_task.get('communication_escalation') if isinstance(existing_decision_task.get('communication_escalation'), dict) else {}
current_communication_escalation = decision_process.get('communication_escalation') if isinstance(decision_process.get('communication_escalation'), dict) else {}
prior_required_cycles = int(existing_communication_escalation.get('required_cycle_count', 0) or 0)
current_required_cycles = prior_required_cycles + 1 if bool(current_communication_escalation.get('required') is True) else 0
current_communication_escalation['required_cycle_count'] = current_required_cycles
current_communication_escalation['block_dispatch_threshold_cycles'] = 3
decision_process['communication_escalation'] = current_communication_escalation

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
    'decision_process': decision_process,
    'communication_escalation': current_communication_escalation,
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
    refresh_alignment_if_drifted

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