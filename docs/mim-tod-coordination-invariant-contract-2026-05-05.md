# MIM/TOD Coordination Invariant Contract

## Contract

The MIM-side coordination harness enforces these invariants against the real consumer chain.

1. `task_id` never changes inside one execution lane.
2. Objective-only matches cannot produce accepted completion.
3. Objective-only task mismatches cannot produce `idle_blocked` by themselves.
4. `stale_guard` is warning-only metadata and cannot become authoritative lineage.
5. `MIM_TASK_STATUS_REVIEW` remains advisory unless its `task_id` matches the active lane.
6. MIM fallback preserves `objective_id`, `task_id`, `request_id`, and `correlation_id` exactly.
7. ACK, result, and `current_processing` must all align to the active task before completion is accepted.
8. Wrapper-only execution results cannot be accepted as active task completion.

## Runtime Enforcement

The simulation harness records counters for the failure modes that matter operationally:

- `stale_lineage_accepted`
- `wrong_task_completions_accepted`
- `false_idle_blocked_from_task_mismatch`
- `fallback_task_mutations`

The run fails if any counter is non-zero or if any lane violates its scenario-specific acceptance contract.

## Correction Guidance

If a run fails, the first correction target is the consumer that promoted the invalid lineage:

1. Patch `scripts.tod_status_signal_lib.py` if the failure is a task-status review or dispatch-state promotion issue.
2. Patch `core/primitive_request_recovery_service.py` if the failure is authoritative request lineage recovery.
3. Patch `core/routers/mim_ui.py` if the failure is UI truth reconciliation or fallback confirmation.
4. Patch TOD publishers only when the consumer is correct and the producer is emitting malformed lineage.
