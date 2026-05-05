# MIM/TOD Coordination Simulation Scenarios

## Purpose

This catalog defines the MIM-side coordination simulation harness for objective `MIM-SPAWNED TOD COORDINATION SIMULATION HARNESS`.

The harness synthesizes shared artifacts from the MIM side, then executes the real consumer surfaces:

- `core.primitive_request_recovery_service.load_authoritative_request_status`
- `scripts.tod_status_signal_lib.build_task_status_review`
- `core.routers.mim_ui._build_tod_truth_reconciliation_snapshot`

Each lane preserves a single MIM-owned `objective_id`, `task_id`, `request_id`, and `correlation_id` unless the scenario explicitly injects a competing or stale artifact to prove that the consumers reject it.

## Scenario Set

1. `normal_tod_ack_result`
   Same-task request, ACK, result, and current processing confirm the active lane.

2. `delayed_ack`
   The ACK arrives late, but it still carries the active task lineage and must remain acceptable.

3. `missing_ack`
   ACK is absent, but the terminal result and current processing still match the active task, so the lane can complete.

4. `wrong_task_ack`
   TOD publishes an ACK for a different task and MIM must reject it.

5. `wrong_task_result`
   TOD publishes a terminal result for a different task and MIM must reject it.

6. `stale_guard_high_watermark`
   `stale_guard` metadata appears on the active task, but it remains diagnostic only and cannot override current lineage.

7. `stale_ack_result`
   Only stale ACK and result artifacts are present for an older task and they must not be accepted.

8. `objective_match_task_mismatch`
   Objective identity matches the active lane, but task identity does not; MIM must not accept completion or report idle blockage from that mismatch alone.

9. `wrapper_only_execution_result`
   A wrapper reports success without active task lineage; MIM must treat it as dispatch lineage failure, not as accepted completion.

10. `stuck_current_processing`
    Current processing remains pinned to a stale task and must not confirm the active request.

11. `tod_silence`
    TOD stops mutating the active lane long enough to arm bounded direct execution fallback.

12. `replay_same_task`
    A replay stays on the same task lineage and remains safe to accept.

13. `mim_fallback_same_task`
    MIM takes direct execution fallback while preserving the active objective, task, request, and correlation ids.

14. `competing_publisher`
    A competing publisher mutates the trigger lane to another task and MIM must keep the request lane authoritative.

15. `stale_ui_mirror_artifacts`
    UI or mirror artifacts are stale, but normalized canonical lineage remains current and must still reconcile.

16. `review_task_mismatch`
    `MIM_TASK_STATUS_REVIEW` references a different task and must remain advisory rather than authoritative.

## Expected Outputs

The harness emits runtime artifacts under `runtime/reports/`:

- `mim_tod_coordination_simulation_report.latest.json`
- `mim_tod_coordination_simulation_failure_examples.latest.json`
- `mim_tod_coordination_simulation_scenario_catalog.latest.json`
- `mim_tod_coordination_simulation_invariant_contract.latest.json`

The pass condition is a full 5,000-lane run with:

- `5,000/5,000` lineage-safe runs
- `0` stale lineage accepted
- `0` wrong-task completions accepted
- `0` false `idle_blocked` states caused only by task mismatch
- `0` fallback lineage mutations
