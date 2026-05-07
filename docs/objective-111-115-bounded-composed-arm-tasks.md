# Objectives 111-115: bounded composed arm tasks

This slice adds a bounded composed-task layer on top of the existing MIM arm execution routes.

## Scope

- Objective 111: compose bounded steps into a single task contract
- Objective 112: classify failures and expose bounded retry decisions
- Objective 113: persist compact task snapshots and prune retained artifacts
- Objective 114: return operator summaries and actionable follow-up commands
- Objective 115: expose an explainable decision endpoint for the current task state

## Endpoints

- `POST /mim/arm/tasks/composed`
  - creates a composed task
  - dispatches the first bounded step immediately
- `GET /mim/arm/tasks/composed/{trace_id}`
  - refreshes proof state and returns the current task snapshot
- `GET /mim/arm/tasks/composed/{trace_id}/decision`
  - returns the current decision, operator summary, and memory hygiene snapshot
- `POST /mim/arm/tasks/composed/{trace_id}/advance`
  - refreshes current state and either advances or retries when policy allows

## Decision codes

- `task_completed`
- `operator_review_current_step`
- `await_operator_approval_for_next_step`
- `dispatch_next_step`
- `retry_current_step`
- `rollback_safe_home`
- `await_current_step_proof`

## Validation

- Focused regression coverage:
  - proof promotion from ACK/RESULT plus host attribution
  - retry recommendation for retryable transport failures within budget
  - blocked-step reconciliation surfaces operator review instead of proof wait
  - artifact retention pruning stays bounded
- Focused test module passed with `28` tests under `unittest`

## Live validation notes

- The local `:18001` lane was restarted from the current workspace and the new routes were confirmed in `openapi.json`.
- A live composed-task create request with `safe_home -> scan_pose -> capture_frame` exercised the real runtime path.
- On this lane, the first `safe_home` step was blocked by readiness policy (`execution_readiness_blocked`).
- The decision layer now reports that state correctly as `operator_review_current_step` with `status = awaiting_operator`.
- This fixes the earlier mismatch where a blocked first step incorrectly returned `await_current_step_proof`.