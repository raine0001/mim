# Objective 75 Re-engagement Plan

## Goal

Recover quickly from shared-truth drift (canonical vs legacy TOD integration status mismatch), then safely re-engage the overnight objective-75 loop.

## Resolve Phase (fail-fast)

1. Detect drift with:
   - `scripts/check_tod_integration_alias_sync.sh`
2. If drift detected, stop overnight execution via guardrail reason:
   - `integration_alias_drift`
3. Reconcile shared truth:
   - Refresh canonical integration status producer.
   - Re-run catchup watcher once to mirror alias:
     - `POLL_SECONDS=300 timeout 8 bash scripts/watch_tod_catchup_status.sh`
4. Confirm no drift:
   - `scripts/check_tod_integration_alias_sync.sh` must return `ALIAS_SYNC: PASS`.

## Re-engage Phase (controlled restart)

1. Confirm objective state and runner exclusivity:
   - Ensure only one `run_objective75_overnight_loop.sh` process is active.
2. Start/restart overnight loop with alias sync requirement enabled:
   - `REQUIRE_INTEGRATION_ALIAS_SYNC=1 bash scripts/run_objective75_overnight_loop.sh`
3. Validate first cycle:
   - Look for `Cycle PASS` in `runtime/logs/objective75_overnight.log`.
   - Confirm no `GUARDRAIL STOP` and no alias drift entries.
4. Monitor health window:
   - Verify consecutive pass progression and no guardrail artifacts.

## Success Criteria

- Alias sync check remains PASS.
- Catchup status remains `in_sync`.
- Overnight loop advances `TASK_NUM` with cycle PASS entries.
- No guardrail stop for `integration_alias_drift`.

## Listener Recovery Runbook

- TOD listener stale-ack remediation checklist: `docs/objective-75-tod-listener-recovery-checklist.md`
