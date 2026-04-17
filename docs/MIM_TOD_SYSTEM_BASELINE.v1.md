# MIM TOD System Baseline v1

This document freezes the current known-good TOD↔MIM communication baseline after the 2026-04-04 stabilization and proof pass.

## Frozen Contract and Runtime

- Contract id: `TOD_MIM_COMMUNICATION_CONTRACT.v1`
- Contract version: `v1`
- Schema version: `2026-04-02-communication-contract-v1`
- Contract sha256: `fca83d97a9ac490660e48fcf9fc0aa86cc93cd575b7f995ce7e8b8e45e6d33af`
- Canonical validation target: `http://127.0.0.1:18001`
- Live execution target on the frozen runtime: `target=mim_arm`, `execution_mode=hardware_transport`, `live_transport_available=true`

Do not change the contract version, runtime binding, validation target selection, lane separation, or bridge publication path as part of cleanup or convenience refactors. Any future change to those surfaces is a topology or protocol change and must be treated as a new baseline event.

## Golden Run Evidence

Authoritative artifact: `docs/MIM_TOD_GOLDEN_RUN_2026-04-04.json`

### 1. Direct Execution-Lane Result Proof

- Fresh request id: `tod-mim-arm-83df0c3abcab`
- Classification: `governed_execution`
- Lane: `mim_arm_execution`
- ACK: `accepted`
- RESULT: `succeeded`
- Result reason: `hardware_transport_succeeded`
- Safe-path evidence: live `hardware_transport` mode on `:18001`, no override configuration, request generated during this run, and the inline submission response returned both ACK and RESULT for the same request id.

This is the strongest current-source proof that the direct execution lane remains bound to real hardware transport and can complete successfully on the frozen runtime.

### 2. Bridge Publication and Listener Classification Proof

- Fresh bridge request id: `objective-97-task-mim-arm-safe-home-1775325722`
- Fresh correlation id: `obj97-mim-arm-safe-home-1775325722`
- Bridge publication: local write succeeded and `remote_publish.succeeded=true`
- Derived execution path: `remote_ephemeral`
- Fresh ACK classification: `stale_ignored`
- ACK reason code: `stale_request_ignored`

This is the strongest available proof that the MIM bridge publication path is still wired correctly and that TOD is classifying the fresh request according to the active authoritative-task rule instead of silently executing stale work.

### 3. Why the Baseline Uses Two Proof Legs

The frozen system has two distinct lanes and they must not be conflated:

- `/mim/arm/execution-lane/requests` is the direct execution lane. It returns inline ACK and RESULT on the current-source runtime.
- `/mim/arm/executions/safe-home` is the bridge publication lane. It writes shared-latest bridge artifacts, publishes remotely, and depends on TOD listener governance for downstream ACK and RESULT behavior.

During the 2026-04-04 proof run, the bridge lane emitted a fresh ACK for the new request id and correctly classified it as `stale_ignored` because a higher authoritative task ordinal was already active. The shared latest RESULT file did not emit a fresh result body for that same request id during this run, so successful RESULT evidence remains anchored to the direct execution lane.

That separation is part of the system contract now. Do not blur these lanes in future validation or documentation.

## Publication Path Freeze

- Shared-artifact transport id: `mim_server_shared_artifact_boundary`
- Local publication surface: `/home/testpilot/mim/runtime/shared`
- Communication authority host: `192.168.1.120`
- Communication authority root: `/home/testpilot/mim/runtime/shared`
- Frozen bridge files:
  - `MIM_TOD_TASK_REQUEST.latest.json`
  - `MIM_TO_TOD_TRIGGER.latest.json`
  - `TOD_MIM_TASK_ACK.latest.json`
  - `TOD_MIM_TASK_RESULT.latest.json`
  - `MIM_TOD_PUBLICATION_BOUNDARY.latest.json`

The baseline term `remote_ephemeral` refers to the execution-evidence mirror path only. The authoritative TOD/MIM communication contract remains the canonical shared root on `192.168.1.120:/home/testpilot/mim/runtime/shared`.

## Memory Profile

- Runtime self-health: `healthy`
- Uvicorn pid: `1060705`
- RSS: `157344 KB`
- VSZ: `309116 KB`
- Detailed self-health memory sample: `153 MB`, `0.23949008630008453%`, trend `stable`
- API latency: `45.2 ms`
- API error rate: `0.005`
- State-bus lag: `250 ms`

This is the memory-safe baseline for the frozen proof run. There was no evidence of degradation during the sample window.

## Passing Validation Set

The frozen adjacent validation set remains:

- `tests.integration.test_objective75_interface_hardening`
- `tests.integration.test_objective23_operator_control`
- `tests.integration.test_objective37_human_aware_interruption_and_safe_pause_handling`
- `tests.integration.test_objective38_predictive_workspace_change_and_replanning`

## Known Constraints

- The bridge listener emitted the correct fresh ACK for the new request id, but active authoritative-task governance prevented a new RESULT body for that request during this run.
- The shell used for this baseline did not have `MIM_ARM_SSH_*` credentials configured, so direct remote command-status polling could not be completed from `scripts/run_mim_arm_dispatch_attribution_check.py`.
- The bridge publication response currently returns an empty `task_id`; the authoritative identifier for this baseline is the `request_id`.

## Promotion Decision

This baseline is sufficient to freeze the current contract, runtime target, lane separation rule, and publication path.

The next change stream should start from MIM ARM validation and attribution, not from additional cleanup in the TOD↔MIM baseline.
