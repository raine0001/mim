# MIM ARM Objective 107 Remote Attribution

## Scope

Objective 107 asks for one bounded live-safe action, ideally `safe_home`, with a single attributable chain:

- request submitted
- command dispatched
- host received it
- host executed it
- final result attributed to the same request/correlation id

This run used the frozen runtime target `http://127.0.0.1:18001` and the existing remote attribution script after patching it to accept the bridge `request_id` when `bridge_publication.task_id` is empty.

## Follow-Up Summary

- Updated at: `2026-04-06T15:55:05.353601Z`
- Base URL: `http://127.0.0.1:18001`
- Arm host: `192.168.1.90`
- Fresh dispatch id: `objective-97-task-mim-arm-safe-home-20260406155438`
- Fresh correlation id: `obj97-mim-arm-safe-home-20260406155438`

## Newly Proven

- The fresh bridge request was written locally and remotely published with matching request and trigger ids.
- The authoritative remote publication boundary matches the same fresh dispatch id: `objective-97-task-mim-arm-safe-home-20260406155438`.
- The authoritative remote command-status surface is now directly readable from this shell.
- The synced authoritative ACK matches the same fresh request id: `objective-97-task-mim-arm-safe-home-20260406155438`.
- The synced authoritative RESULT matches the same fresh request id and correlation id.
- The refreshed host-state artifact also matches the same request, task, and correlation ids.
- Host command counters advanced from `10355 / 10355` to `10361 / 10361` across the check window, and the latest host command evidence now carries the fresh request id.

## Remote Surface Nuance

- The remote `TOD_MIM_COMMAND_STATUS.latest.json` surface is reachable now, but it still represents the readiness refresh lane rather than echoing the bounded `safe_home` dispatch identifier.
- In this run, that remote file carried `dispatch-attribution-refresh-*` request and task ids, while the bounded dispatch itself was proven by the aligned remote publication boundary, synced authoritative ACK and RESULT, and refreshed host-state attribution surfaces.

## Acceptance Boundary

Objective 107 is considered complete when all three conditions hold:

- local attribution is aligned across request, ACK, RESULT, and refreshed host-state evidence
- remote surface semantics are explicit, so `TOD_MIM_COMMAND_STATUS.latest.json` is interpreted by role rather than by assumption
- readiness files are no longer treated as bounded dispatch-consumption proof

Current state: all three conditions are met.

## Run Summary

- Started at: `2026-04-06T15:54:36.536556Z`
- Completed at: `2026-04-06T15:55:05.353601Z`
- Base URL: `http://127.0.0.1:18001`
- Arm host: `192.168.1.90`
- Dispatch identifier kind: `bridge_request_id`
- Dispatch/request id: `objective-97-task-mim-arm-safe-home-20260406155438`
- Correlation id: `obj97-mim-arm-safe-home-20260406155438`
- Publish generated at: `2026-04-06T15:54:38Z`
- Execution id: `207867`

## Proven In This Run

- MIM accepted and dispatched a bounded `safe_home` execution with explicit operator approval.
- The bridge publication was written locally.
- Remote publish succeeded.
- The fresh request id and correlation id were preserved in the bridge publication payload.
- The authoritative remote publication boundary matches that fresh dispatch identifier.
- The synced authoritative ACK and RESULT match that same dispatch identifier.
- The refreshed host-state artifact matches that same request id, task id, and correlation id.
- The checker concluded `proof_chain_complete = true`.

## No Longer Blocked

- SSH access to `192.168.1.90` is now available to the checker from this shell.
- The previous blocker on direct remote-authoritative verification is resolved.

## Host-Side Evidence

### Remote authoritative status

- direct re-read from this shell: available
- checker classification: `readiness_preflight`
- fresh dispatch identifier expected on this surface: `false`
- supports dispatch-consumption proof on this surface: `false`
- remote status before request id: `dispatch-attribution-refresh-1775490864`
- remote status after request id: `dispatch-attribution-refresh-1775490877`
- remote status role: readiness preflight surface, not the bounded dispatch-consumption echo surface
- implication: the remote file is reachable and authoritative, but fresh dispatch consumption is evidenced by remote publication boundary alignment plus synced ACK/RESULT and host-state attribution

### Arm host before/after state

- before pose: `[90, 90, 90, 90, 90, 90]`
- after pose: `[90, 90, 90, 90, 90, 90]`
- before commands_total / acks_total: `10355 / 10355`
- after commands_total / acks_total: `10361 / 10361`
- before request id: `objective-97-task-mim-arm-safe-home-20260405164802`
- after request id: `objective-97-task-mim-arm-safe-home-20260406155438`
- before correlation id: `obj97-mim-arm-safe-home-20260405164802`
- after correlation id: `obj97-mim-arm-safe-home-20260406155438`
- before last_command_sent: `MOVE 5 90`
- after last_command_sent: `MOVE 5 90`

## Classification

Objective 107 is now proven.

Current classification: `complete_remote_authoritative_attribution`

Reason:

- The fresh safe-home bridge request was successfully published and remotely synced.
- The authoritative remote publication boundary matches that fresh dispatch identifier.
- The synced authoritative ACK and RESULT match that same dispatch identifier.
- The refreshed host-state artifact matches that same request id and correlation id.
- The direct remote-authoritative status surface is now readable from this shell, so the prior SSH blocker is gone.

That means the attribution objective is complete. The remaining nuance is only that the remote command-status file is a readiness surface and does not itself serve as the dispatch-consumption echo surface for this bounded `safe_home` proof.

## Artifacts

- Raw attribution report: `runtime/diagnostics/mim_arm_dispatch_attribution_check.objective-97-task-mim-arm-safe-home-20260406155438.json`
- Machine-readable objective summary: `docs/MIM_ARM_OBJECTIVE_107_REMOTE_ATTRIBUTION_2026-04-04.json`
- Frozen baseline reference: `docs/MIM_TOD_SYSTEM_BASELINE.v1.md`
- Frozen golden run reference: `docs/MIM_TOD_GOLDEN_RUN_2026-04-04.json`

## Immediate Next Step

The next useful change is not additional Objective 107 proof. It is hardening or documenting the remote readiness-surface semantics so future attribution checks do not assume that `TOD_MIM_COMMAND_STATUS.latest.json` will carry the bounded dispatch identifier.
