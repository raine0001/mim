# Objective 37 — Human-Aware Interruption and Safe Pause Handling

## Summary

Objective 37 adds first-class interruption handling so autonomous execution pauses or stops safely when humans, obstacles, confidence, or workspace state changes invalidate current assumptions.

## Scope Delivered

### Task A — Interruption Event Model

Added persisted interruption events in `workspace_interruption_events` with:

- interruption type
- execution/action-plan/chain linkage
- requested policy outcome and applied outcome
- status (`active`, `applied`, `resolved`)
- actor/source/reason
- resolution metadata (`resolved_by`, `resolved_at`)

Supported interruption types:

- `human_detected_in_workspace`
- `operator_pause`
- `operator_stop`
- `new_obstacle_detected`
- `target_confidence_drop`
- `workspace_state_changed`
- `safety_policy_interrupt`

### Task B — Pause / Stop / Resume Semantics

Added execution control endpoints:

- `POST /workspace/executions/{execution_id}/pause`
- `POST /workspace/executions/{execution_id}/resume`
- `POST /workspace/executions/{execution_id}/stop`

Behavior:

- pause: execution transitions to `paused`; action plan transitions to `plan_paused`/`paused`
- stop: execution transitions to `blocked`; action plan transitions to `plan_aborted`/`aborted`
- resume: only from `paused`, requires explicit `safety_ack` and restored conditions for blocking interruptions

### Task C — Interruption Policy Layer

Policy outcomes are mapped by interruption type:

- auto-pause: `human_detected_in_workspace`, `operator_pause`
- auto-stop: `operator_stop`, `new_obstacle_detected`, `safety_policy_interrupt`
- require operator decision: `target_confidence_drop`, `workspace_state_changed`

Resume gate rules:

- auto-resume is not performed
- unresolved blocking interruptions reject resume
- resume requires operator safety acknowledgement and explicit restored-condition confirmation

### Task D — Chain / State Integration

Interruption actions propagate coherently to:

- capability execution state (`paused`, `blocked`, `running`)
- action plan state (`paused`, `executing`, `aborted`)
- autonomous chain state (`paused`, `active`, `canceled`)
- operator inbox visibility (`paused` bucket)
- journal/audit history (`workspace_execution_pause|resume|stop`, chain audit events)

### Task E — Inspectability Endpoints

Added interruption inspectability:

- `GET /workspace/interruptions`
- `GET /workspace/interruptions/{interruption_id}`

Execution control events are persisted and queryable through these endpoints.
