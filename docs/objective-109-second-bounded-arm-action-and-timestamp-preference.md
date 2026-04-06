# Objective 109: Second Bounded ARM Action and Executor Timestamp Preference

## Status

- Promoted to production as of 2026-04-06.
- Local implementation, focused validation, and production smoke verification are complete.
- Live bounded proof for `scan_pose` is complete.

## Goal

Objective 109 extends the bounded MIM ARM execution contract in two ways:

1. add one more bounded live action on top of the Objective 108 dispatch telemetry lane
2. prefer executor-originated host timestamps when feedback updates dispatch telemetry

The selected second bounded live action is `scan_pose`.

## Implemented Changes

### Bounded live action expansion

- Added `mim_arm.execute_scan_pose` to the bounded MIM ARM capability set.
- Added `POST /mim/arm/executions/scan-pose`.
- Refactored bounded live execution in `core/routers/mim_arm.py` so `safe_home` and `scan_pose` use one shared governed execution path.
- Generalized publish-time bridge projection and dispatch telemetry emission so request ids, correlation ids, titles, notes, and capability metadata are action-specific rather than hardcoded to `safe_home`.
- Updated control readiness so bounded live actions now advertise both `safe_home` and `scan_pose`.

### Timestamp provenance tightening

- Updated `core/mim_arm_dispatch_telemetry.py` so executor-originated nested feedback timestamps are preferred before flatter fallback fields.
- Preferred fields now include:
  - `feedback_json.executor_timestamps.host_received_timestamp`
  - `feedback_json.executor_timestamps.host_completed_timestamp`
  - `correlation_json.executor_timestamps.host_received_timestamp`
  - `correlation_json.executor_timestamps.host_completed_timestamp`
- Fallback behavior still exists when executor-originated fields are absent, but no longer outranks the explicit executor timestamp contract.

### Proof harness generalization

- Updated `scripts/run_mim_arm_dispatch_attribution_check.py` so the live proof can target bounded actions other than `safe_home`.
- Added `--action` and action-aware execution route selection.
- Preserved the same proof contract and report format so `scan_pose` and `safe_home` can be compared directly.

### Runtime closure hardening

- Updated `scripts/reconcile_tod_task_result.py` so stale-result reconciliation keeps `TOD_MIM_TASK_RESULT.latest.json` contract-valid by mirroring a populated `result_status` into top-level `status` instead of emitting an empty `status` for rebound packets.
- Added focused regression coverage in `tests/integration/test_reconcile_tod_task_result.py` for that contract-valid stale-result behavior.

## Focused Validation

Completed locally:

1. `python -m unittest tests.test_mim_arm_dispatch_telemetry -v`
2. `python -m unittest tests.test_mim_arm_dispatch_attribution_check -v`
3. `python -m unittest tests.integration.test_mim_arm_controlled_access_baseline -v`
4. `python -m unittest tests.integration.test_reconcile_tod_task_result -v`

All four focused lanes passed after the Objective 109 changes.

## Live Proof Closure

Final bounded `scan_pose` proof artifact:

- `runtime/diagnostics/mim_arm_dispatch_attribution_check.objective-109-task-mim-arm-scan-pose-20260406190814.json`

Final per-dispatch telemetry record from that run:

- `runtime/shared/mim_arm_dispatch_telemetry/objective-109-task-mim-arm-scan-pose-20260406190814.json`

### Closed Conditions

- publish route accepted the bounded `scan_pose` request
- remote publication boundary matched the fresh dispatch identifier
- dispatch telemetry was emitted for `mim_arm.execute_scan_pose`
- dispatch telemetry request id, task id, and correlation id all matched the fresh publish response
- the timestamp-preference contract remained active and the telemetry surface recorded both host timestamps
- `dispatch_telemetry_dispatch_status = "completed"`
- `dispatch_telemetry_completion_status = "completed"`
- `dispatch_telemetry_host_received_timestamp_present = true`
- `dispatch_telemetry_host_completed_timestamp_present = true`
- `tod_ack_matches_dispatch_identifier = true`
- `tod_result_matches_dispatch_identifier = true`
- `host_explicitly_attributes_fresh_dispatch_identifier = true`
- `proof_chain_complete = true`
- the reconciled live result surface now records:
  - `status = "succeeded"`
  - `result_status = "succeeded"`
  - `execution_mode = "mim_arm_ssh_http_routine"`

## Current Interpretation

Objective 109 is complete in the bounded slice:

- the product path now supports a second bounded live action
- the dispatch telemetry service now prefers executor-originated timestamp fields as intended
- the proof harness can validate either bounded action through one contract
- the live executor path now closes the same attribution and completion proof chain that Objective 108 established for `safe_home`

## Acceptance Boundary

Objective 109 is complete when all of the following hold for bounded `scan_pose`:

1. the live bounded route dispatches through the same authoritative telemetry lane as `safe_home`
2. executor-originated host-received and host-completed timestamps are preferred and present when feedback provides them
3. TOD ACK, RESULT, and refreshed host attribution align to the fresh `scan_pose` dispatch identifier
4. the live proof script returns `proof_chain_complete = true` for a bounded `scan_pose` run

That boundary is now met by `runtime/diagnostics/mim_arm_dispatch_attribution_check.objective-109-task-mim-arm-scan-pose-20260406190814.json`.

## Next Work

Promotion outcome is now recorded in `docs/objective-109-prod-promotion-report.md`.

The remaining follow-through is:

- document the remote Pi-side controller compatibility fix as operational runbook material
- then move on to the next bounded arm capability
