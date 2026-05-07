# Objective 108: Remote Dispatch Telemetry and True Execution Attribution

## Status

- Implemented as a bounded MIM ARM slice on 2026-04-06.
- Acceptance boundary met for bounded `safe_home` dispatches.

## Problem

Objective 107 closed the attribution gap for bounded `safe_home`, but it still depended on aligned artifacts and refreshed host-state interpretation. That was enough to prove correctness for a bounded closure, but not enough to give operators a single durable surface answering:

- which exact request was dispatched
- which exact task lane it used
- which exact correlation id ties the bounded execution together
- when the host received it
- when the host completed it
- what evidence proves each step

Objective 108 adds that first-class proof surface.

## Implemented Contract

### Durable surfaces

- Latest pointer: `runtime/shared/MIM_ARM_DISPATCH_TELEMETRY.latest.json`
- Per-dispatch records: `runtime/shared/mim_arm_dispatch_telemetry/<request_id>.json`
- API endpoint: `/mim/arm/dispatch-telemetry/latest`
- API endpoint: `/mim/arm/dispatch-telemetry/{request_id}`

### Required outputs per dispatch

Each dispatch telemetry record carries:

- `request_id`
- `task_id`
- `correlation_id`
- `execution_lane`
- `command_name`
- `dispatch_timestamp`
- `host_received_timestamp`
- `host_completed_timestamp`
- `dispatch_status`
- `completion_status`
- `result_reason`

### Properties of the surface

- emitted from the actual bounded dispatch path in `core/routers/mim_arm.py`
- advanced by the real executor feedback path in `core/routers/gateway.py`
- refreshable from authoritative local ACK/RESULT artifacts without depending on readiness files
- clearly named so it is not confused with `TOD_MIM_COMMAND_STATUS.latest.json`
- tied to one exact execution via per-dispatch records and a stable latest pointer

## Operator-Facing Exposure

The telemetry is exposed to operators and reasoning consumers through:

- top-level `/mim/ui/state.mim_arm_dispatch_telemetry`
- `/mim/ui/state.operator_reasoning.dispatch_telemetry`
- the operator reasoning summary, which now includes a dispatch telemetry sentence when a bounded dispatch record is present

## Proof Artifact

Authoritative bounded proof run:

- `runtime/diagnostics/mim_arm_dispatch_attribution_check.objective-97-task-mim-arm-safe-home-20260406174608.json`

Key proof outcomes from that run:

- `dispatch_telemetry_available = true`
- `dispatch_telemetry_request_id_matches = true`
- `dispatch_telemetry_task_id_matches = true`
- `dispatch_telemetry_correlation_id_matches = true`
- `dispatch_telemetry_dispatch_status = "completed"`
- `dispatch_telemetry_completion_status = "completed"`
- `dispatch_telemetry_host_received_timestamp_present = true`
- `dispatch_telemetry_host_completed_timestamp_present = true`
- `proof_chain_complete = true`

Latest bounded dispatch record from that run:

- `runtime/shared/mim_arm_dispatch_telemetry/objective-97-task-mim-arm-safe-home-20260406174608.json`

## Validation

Focused validation completed:

1. `python -m unittest tests.test_mim_arm_dispatch_telemetry -v`
2. `python -m unittest tests.test_mim_arm_dispatch_attribution_check -v`
3. `python -m unittest tests.integration.test_mim_arm_controlled_access_baseline -v`

All three lanes passed.

Runtime exposure also verified on `http://127.0.0.1:18001`:

- `/mim/arm/dispatch-telemetry/latest` returned the completed bounded dispatch record
- `/mim/ui/state` returned both `mim_arm_dispatch_telemetry` and `operator_reasoning.dispatch_telemetry`

## Acceptance Boundary

Objective 108 is complete for the bounded `safe_home` slice when all of the following hold:

1. The real dispatch path emits a durable per-dispatch telemetry record at publish time.
2. The record advances to host-received and host-completed with matching request/task/correlation identity.
3. The surface is distinct from readiness and ACK-only semantics.
4. Operators can inspect the same telemetry through MIM UI and API surfaces.

That boundary is now met for bounded `safe_home` dispatches.
