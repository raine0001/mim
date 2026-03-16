# Objective 75 — TOD Listener Recovery Checklist

Date: 2026-03-16  
Status: active runbook

## Purpose

Recover TOD listener-side trigger/task acknowledgment when Objective 75 is stalled with stale acknowledgments.

Current stall signature:

- `MIM_TOD_TASK_REQUEST.latest.json` task_id is `objective-75-task-3124` (or later)
- `TOD_TO_MIM_TRIGGER_ACK.latest.json.acknowledges` remains `objective-75-task-3123`
- `TOD_MIM_TASK_ACK.latest.json.request_id` remains `objective-75-task-3123`

## Acceptance Criteria

The recovery is successful only when all are true:

1. `runtime/shared/TOD_TO_MIM_TRIGGER_ACK.latest.json`
   - `status` is runtime (`acknowledged`)
   - `acknowledges` equals current request task_id
2. `runtime/shared/TOD_MIM_TASK_ACK.latest.json`
   - `status == accepted`
   - `request_id` equals current request task_id
3. `runtime/shared/TOD_MIM_TASK_RESULT.latest.json`
   - `status == completed`
   - `request_id` equals current request task_id
4. Objective-75 runner resumes normal cadence:
   - next `Cycle PASS; next TASK_NUM=...` appears
   - no immediate repeat `max_same_task_fails` guardrail stop on same task

## Minimal Recovery Sequence (TOD side)

Run this on the TOD listener host/process side.

1. Confirm listener process is alive and not wedged.
2. Reload or restart listener process cleanly (single instance).
3. Confirm listener can read latest MIM artifacts:
   - `MIM_TOD_TASK_REQUEST.latest.json`
   - `MIM_TO_TOD_TRIGGER.latest.json`
4. Trigger one explicit pull/ack cycle for current task.
5. Verify trigger ACK now acknowledges current task_id.
6. Verify task ACK and task RESULT advance to current task_id.

If step 5 fails after restart, inspect listener logic for one of these root causes:

- stale in-memory task cache not invalidated
- trigger file watcher not firing on overwrite/atomic rename
- trigger parser ignoring `task_id` payload
- listener writing ACK from old queue item

## MIM-side Verification Commands

Use from the MIM workspace after TOD-side recovery action:

```bash
cat runtime/shared/MIM_TOD_TASK_REQUEST.latest.json
cat runtime/shared/TOD_TO_MIM_TRIGGER_ACK.latest.json
cat runtime/shared/TOD_MIM_TASK_ACK.latest.json
cat runtime/shared/TOD_MIM_TASK_RESULT.latest.json
grep -E "Cycle PASS; next TASK_NUM=|Cycle FAIL; stale trigger ACK|GUARDRAIL STOP" runtime/logs/objective75_overnight.log | tail -n 30
```

One-command checker (recommended):

```bash
bash scripts/check_objective75_listener_recovery.sh
```

Optional freshness window override:

```bash
MAX_AGE_SECONDS=900 bash scripts/check_objective75_listener_recovery.sh
```

Generated artifacts (each run):

- `runtime/logs/objective75_listener_recovery.latest.json`
- `runtime/logs/objective75_listener_recovery.latest.md`
- `runtime/logs/objective75_listener_recovery.jsonl` (append-only history)

Optional output overrides:

```bash
OUTPUT_DIR=runtime/logs APPEND_JSONL=1 bash scripts/check_objective75_listener_recovery.sh
```

Stale-ACK watchdog (alerts only on repeated stale-ack failures):

```bash
bash scripts/watch_objective75_stale_ack_watchdog.sh
```

Useful overrides:

```bash
MAX_CONSEC_STALE_FAILS=2 POLL_SECONDS=10 MAX_WATCH_SECONDS=600 bash scripts/watch_objective75_stale_ack_watchdog.sh
```

Watchdog artifacts:

- `runtime/logs/objective75_stale_ack_watchdog.latest.json`
- `runtime/logs/objective75_stale_ack_watchdog.latest.md`
- `runtime/logs/objective75_stale_ack_watchdog.jsonl` (append-only history)

Nightly summary snapshot (manual run):

```bash
bash scripts/generate_objective75_nightly_summary.sh
```

Nightly summary artifacts:

- `runtime/logs/objective75_nightly_summary.latest.json`
- `runtime/logs/objective75_nightly_summary.latest.md`
- `runtime/logs/objective75_nightly_summary.jsonl` (append-only history)

Automated schedule:

- system scope: `mim-objective75-nightly-summary.timer` (daily at 00:20 local time)
- user scope mirror: `mim-objective75-nightly-summary.timer`

JSONL retention prune (manual run):

```bash
bash scripts/prune_objective75_jsonl_retention.sh
```

Useful overrides:

```bash
RETENTION_DAYS=30 DRY_RUN=1 bash scripts/prune_objective75_jsonl_retention.sh
```

Retention artifacts:

- `runtime/logs/objective75_jsonl_retention.latest.json`
- `runtime/logs/objective75_jsonl_retention.latest.md`
- `runtime/logs/objective75_jsonl_retention_runs.jsonl` (append-only run history)

Retention automated schedule:

- system scope: `mim-objective75-jsonl-retention.timer` (daily at 00:40 local time)
- user scope mirror: `mim-objective75-jsonl-retention.timer`

## Expected Post-Recovery Flow

1. Trigger ACK `acknowledges` moves to current task_id.
2. Task ACK + RESULT move to current task_id.
3. Objective-75 cycle passes and increments TASK_NUM.
4. Guardrail alert file is not re-emitted for same-task fail loop.

## Escalation Rule

If ACK remains stale for 2 full cycle windows after TOD listener restart, pause overnight loop and treat as listener defect requiring TOD code fix before further unattended retries.
