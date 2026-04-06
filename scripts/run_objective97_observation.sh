#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${MIM_TEST_BASE_URL:-http://127.0.0.1:18001}"
SCOPE="${1:-objective97-observe-$(date -u +%Y%m%d%H%M%S)}"
ARTIFACT_DIR="${2:-runtime/reports/objective97_observation}"

mkdir -p "$ARTIFACT_DIR"

slugify() {
  printf '%s' "$1" | tr -cs 'a-zA-Z0-9._-' '-'
}

SCOPE_SLUG="$(slugify "$SCOPE")"
RUN_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
REPORT_PATH="$ARTIFACT_DIR/${SCOPE_SLUG}.json"

post_json() {
  local path="$1"
  local payload="$2"
  curl -fsS -X POST "$BASE_URL$path" \
    -H 'Content-Type: application/json' \
    -d "$payload"
}

get_json() {
  local path="$1"
  curl -fsS "$BASE_URL$path"
}

require_health() {
  local health
  health="$(get_json '/health')"
  python3 - <<'PY' "$health"
import json
import sys
payload = json.loads(sys.argv[1])
if str(payload.get("status", "")).strip().lower() != "ok":
    raise SystemExit("health check failed")
print("PASS: health check")
PY
}

create_execution() {
  local intake_payload execution_payload
  intake_payload="$(cat <<JSON
{
  "text": "objective97 observation workspace check $SCOPE",
  "parsed_intent": "observe_workspace",
  "confidence": 0.97,
  "requested_goal": "objective97 observation $SCOPE",
  "metadata_json": {
    "capability": "workspace_check",
    "managed_scope": "$SCOPE"
  }
}
JSON
)"
  execution_payload="$(post_json '/gateway/intake/text' "$intake_payload")"
  python3 - <<'PY' "$execution_payload"
import json
import sys
payload = json.loads(sys.argv[1])
execution = payload.get("execution") or {}
execution_id = execution.get("execution_id")
trace_id = execution.get("trace_id")
if not execution_id or not trace_id:
    raise SystemExit("failed to create execution")
print(f"{execution_id}\t{trace_id}")
PY
}

inject_feedback() {
  local execution_id="$1"
  local status_value="$2"
  local reason="$3"
  local feedback_payload
  feedback_payload="$(cat <<JSON
{
  "actor": "executor",
  "status": "$status_value",
  "reason": "$reason",
  "feedback_json": {"objective": "97", "observation": true, "scope": "$SCOPE"}
}
JSON
)"
  post_json "/gateway/capabilities/executions/$execution_id/feedback" "$feedback_payload" >/dev/null
}

record_attempt() {
  local trace_id="$1"
  local requested_decision="$2"
  local payload
  payload="$(cat <<JSON
{
  "actor": "objective97-observation",
  "source": "objective97_observation",
  "trace_id": "$trace_id",
  "requested_decision": "$requested_decision"
}
JSON
)"
  post_json '/execution/recovery/attempt' "$payload" >/dev/null
}

seed_negative_pattern() {
  local i execution_id trace_id
  for i in 1 2; do
    IFS=$'\t' read -r execution_id trace_id < <(create_execution)
    inject_feedback "$execution_id" "failed" "objective97 observation seeded failure $i"
    record_attempt "$trace_id" "retry_current_step"
    inject_feedback "$execution_id" "failed" "objective97 observation seeded failed-again $i"
  done
}

run_primary_probe() {
  local execution_id="$1"
  local trace_id="$2"
  local eval_payload
  eval_payload="$(cat <<JSON
{
  "trace_id": "$trace_id",
  "execution_id": $execution_id,
  "managed_scope": "$SCOPE"
}
JSON
)"
  post_json '/execution/recovery/evaluate' "$eval_payload"
}

urlencode_scope_snapshot() {
  python3 - <<'PY' "$1" "$2"
import sys
import urllib.parse
scope = sys.argv[1]
trace = sys.argv[2]
print(urllib.parse.quote(f"execution-recovery:{scope}:{trace}", safe=""))
PY
}

main() {
  echo "Objective 97 observation run"
  echo "- base_url: $BASE_URL"
  echo "- scope: $SCOPE"
  echo "- artifact_dir: $ARTIFACT_DIR"

  require_health
  seed_negative_pattern

  local execution_id trace_id
  IFS=$'\t' read -r execution_id trace_id < <(create_execution)
  inject_feedback "$execution_id" "failed" "objective97 observation probe failure"

  local recovery_eval
  recovery_eval="$(run_primary_probe "$execution_id" "$trace_id")"

  local profiles telemetry ui_state recovery_get snapshot_key snapshot_json
  profiles="$(get_json "/execution/recovery/learning/profiles?managed_scope=$SCOPE")"
  telemetry="$(get_json "/execution/recovery/learning/telemetry?managed_scope=$SCOPE")"
  ui_state="$(get_json '/mim/ui/state?scope=system')"
  recovery_get="$(get_json "/execution/recovery/$trace_id")"
  snapshot_key="$(urlencode_scope_snapshot "$SCOPE" "$trace_id")"
  snapshot_json="$(get_json "/state-bus/snapshots/$snapshot_key")"

  python3 - <<'PY' "$RUN_TS" "$BASE_URL" "$SCOPE" "$execution_id" "$trace_id" "$recovery_eval" "$profiles" "$telemetry" "$ui_state" "$recovery_get" "$snapshot_json" "$REPORT_PATH"
import json
import sys

run_ts, base_url, scope, execution_id, trace_id, recovery_eval_raw, profiles_raw, telemetry_raw, ui_raw, recovery_get_raw, snapshot_raw, report_path = sys.argv[1:]

recovery_eval = json.loads(recovery_eval_raw)
profiles = json.loads(profiles_raw)
telemetry = json.loads(telemetry_raw)
ui_state = json.loads(ui_raw)
recovery_get = json.loads(recovery_get_raw)
snapshot = json.loads(snapshot_raw)

recovery_payload = (recovery_eval.get("recovery") or {}) if isinstance(recovery_eval, dict) else {}
learning = recovery_payload.get("recovery_learning") if isinstance(recovery_payload.get("recovery_learning"), dict) else {}
profiles_latest = profiles.get("latest_profile") if isinstance(profiles.get("latest_profile"), dict) else {}
operator_reasoning = ui_state.get("operator_reasoning") if isinstance(ui_state.get("operator_reasoning"), dict) else {}
state_payload = ((snapshot.get("snapshot") or {}).get("state_payload_json") or {}) if isinstance(snapshot, dict) else {}

checks = [
    {
        "name": "recovery_learning_present",
        "ok": bool(learning),
        "detail": f"learning_state={learning.get('learning_state', '')}",
    },
    {
        "name": "escalation_triggered_after_seed",
        "ok": str(learning.get("escalation_decision", "")) == "require_operator_takeover",
        "detail": f"escalation_decision={learning.get('escalation_decision', '')}",
    },
    {
        "name": "profiles_endpoint_contract",
        "ok": isinstance(profiles.get("profiles"), list) and isinstance(profiles_latest, dict),
        "detail": "profiles/latest_profile keys present",
    },
    {
        "name": "telemetry_endpoint_contract",
        "ok": all(key in telemetry for key in ("window", "metrics", "alerts")),
        "detail": f"keys={sorted(list(telemetry.keys())) if isinstance(telemetry, dict) else []}",
    },
    {
        "name": "operator_ui_surface",
        "ok": isinstance(operator_reasoning.get("execution_recovery_learning"), dict),
        "detail": "operator_reasoning.execution_recovery_learning",
    },
    {
        "name": "state_bus_snapshot_surface",
        "ok": isinstance(state_payload.get("recovery_learning"), dict),
        "detail": "snapshot.state_payload_json.recovery_learning",
    },
]

report = {
    "generated_at": run_ts,
    "base_url": base_url,
    "scope": scope,
    "execution_id": int(execution_id),
    "trace_id": trace_id,
    "checks": checks,
    "recovery_evaluate": recovery_eval,
    "recovery_get": recovery_get,
    "profiles": profiles,
    "telemetry": telemetry,
    "ui_state_excerpt": {
        "runtime_features": ui_state.get("runtime_features", []),
        "operator_reasoning": {
            "execution_recovery_learning": operator_reasoning.get("execution_recovery_learning", {}),
        },
    },
    "state_bus_snapshot": snapshot,
}

with open(report_path, "w", encoding="utf-8") as handle:
    json.dump(report, handle, indent=2)
    handle.write("\n")

failed = [item for item in checks if not item.get("ok")]
for item in checks:
    state = "PASS" if item.get("ok") else "FAIL"
    print(f"{state}: {item['name']} ({item.get('detail', '')})")
print(f"REPORT: {report_path}")

if failed:
    raise SystemExit(1)
PY
}

main "$@"
