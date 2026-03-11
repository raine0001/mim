# Objective 22: MIM ↔ TOD Execution Feedback Integration

## Scope

Make TOD a first-class participant in execution lifecycle updates instead of relying on manual probing.

## Task A — MIM↔TOD Execution Contract

### Handoff endpoint

- `GET /gateway/capabilities/executions/{execution_id}/handoff`

### Handoff response

- `execution_id`
- `goal_ref`: goal linkage for execution context
- `action_ref`: resolution/action-step linkage
- `capability_name`
- `arguments_json`
- `safety_mode`
- `requested_executor`
- `dispatch_decision`
- `status`
- `correlation_metadata`: event source/target metadata, escalation context

### Feedback endpoint

- `POST /gateway/capabilities/executions/{execution_id}/feedback`

Feedback request now supports either direct status or runtime outcome mapping:

- `status` (optional when `runtime_outcome` provided)
- `reason`
- `runtime_outcome`
- `recovery_state`
- `correlation_json`
- `feedback_json`
- `actor`

## Task B — TOD Feedback Publisher

### New TOD client functions

- `Get-MimExecutionHandoff`
- `Publish-MimExecutionFeedback`
- `Invoke-MimExecutionLifecycle`

### Lifecycle behavior

1. TOD fetches handoff payload.
2. TOD posts `accepted`.
3. TOD posts `running`.
4. TOD executes using configured engine path.
5. TOD posts terminal state (`succeeded`/`failed`/`blocked`) with runtime outcome metadata.
6. TOD can emit recovery/fallback semantics during execution.

## Failure/Recovery Semantics Mapping

- `executor_unavailable` → `failed`
- `guardrail_blocked` → `blocked`
- `retry_in_progress` → `running`
- `fallback_used` → `running`
- `recovered` → `succeeded`
- `unrecovered_failure` → `failed`

## Auth/Safety Boundary

MIM feedback updates are now protected by:

- actor allow-list (`execution_feedback_allowed_actors`)
- optional shared-key enforcement (`execution_feedback_api_key` via `X-MIM-Feedback-Key`)

Default allow-list remains conservative (`tod,executor`).

## Validation Target

End-to-end test should prove:

1. MIM dispatch creates execution.
2. TOD-facing handoff payload is retrievable.
3. TOD feedback updates mutate lifecycle correctly.
4. Unauthorized actor cannot mutate lifecycle.
5. Runtime outcome mapping drives expected terminal state.