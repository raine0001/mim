# Objective 38 — Predictive Workspace Change and Replanning

Date: 2026-03-11

## Goal

Advance from interruption-after-change handling to predictive stale-plan detection and preemptive replanning before unsafe continuation.

## Scope Delivered

### Task A — Workspace Change Signal Model

Added persisted predictive replan signals in `workspace_replan_signals` with:

- `signal_id`
- `execution_id`
- `action_plan_id`
- `chain_id`
- `signal_type`
- `predicted_outcome`
- `confidence`
- `source`
- `status`
- `reason`
- `actor`
- `resolved_by` / `resolved_at`
- `metadata_json`

Supported signal types:

- `object_moved`
- `object_missing`
- `confidence_drop`
- `zone_state_changed`
- `new_obstacle_detected`
- `target_no_longer_valid`

### Task B — Replan Policy

Implemented predictive freshness evaluation across:

- target memory freshness
- map context stability
- simulation assumption drift

Policy outcomes:

- `continue_monitor`
- `pause_and_resimulate`
- `require_replan`
- `abort_chain`

Behavior examples implemented:

- slight movement / uncertain identity -> pause and resimulate
- obstacle or simulation drift -> require replan
- missing/stale target in severe path -> abort-chain/block
- confidence drop and severe drift require operator confirmation

### Task C — Replan Endpoint / Workflow

Added:

- `POST /workspace/action-plans/{plan_id}/replan`
- `GET /workspace/action-plans/{plan_id}/replan-history`

Replan workflow tracks:

- prior plan snapshot
- reason and optional linked predictive signal
- selected replan outcome
- regenerated plan result
- operator confirmation requirement
- persisted `replan_history` in plan metadata

### Task D — Predictive Freshness Checks

Added predictive gates to progression points:

- `POST /workspace/action-plans/{plan_id}/execute` now blocks if predictive outcome requires resim/replan/abort.
- `POST /workspace/executions/{execution_id}/resume` now evaluates active predictive signals + freshness drift and blocks unsafe resume.

This cleanly extends Objective 37 resume safety logic.

### Task E — Audit and Operator Visibility

Added inspectability endpoints:

- `POST /workspace/executions/{execution_id}/predict-change`
- `GET /workspace/replan-signals`
- `GET /workspace/replan-signals/{signal_id}`

Operator inbox execution payload now includes:

- `replan_required`
- `latest_replan_outcome`
- `latest_predictive_signal_id`

Journal actions added:

- `workspace_execution_predict_change`
- `workspace_action_plan_replan`
