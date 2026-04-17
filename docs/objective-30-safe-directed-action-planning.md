# Objective 30 — Safe Directed Action Planning

## Summary

Objective 30 adds an operator-mediated planning layer that converts directed target resolutions into safe, inspectable action plans. Plans remain non-actuating and require explicit approval before queue handoff.

## Added API

- `POST /workspace/action-plans`
  - Request:
    - `target_resolution_id` (required)
    - `action_type` (required, safe set only)
    - `source` (optional)
    - `notes` (optional)
    - `metadata_json` (optional)
  - Safe action types:
    - `observe`
    - `rescan`
    - `speak`
    - `prepare_reach_plan`
    - `request_confirmation`

- `GET /workspace/action-plans/{plan_id}`
  - Returns persisted plan details, status, outcomes, and generated step list.

- `POST /workspace/action-plans/{plan_id}/approve`
  - Approves a reviewable plan for queue handoff.

- `POST /workspace/action-plans/{plan_id}/reject`
  - Rejects a plan and records operator rationale.

- `POST /workspace/action-plans/{plan_id}/queue`
  - Queue handoff stub that creates a queued task and records handoff metadata.

## Planning Policy

Planning outcome is derived from target resolution policy:

- `target_confirmed` -> `plan_ready_for_approval`
- `target_requires_confirmation` / `target_stale_reobserve` -> `plan_requires_review`
- `target_not_found` / `target_blocked_unsafe_zone` -> `plan_blocked`

Operator actions then transition plans to:

- `plan_approved`
- `plan_rejected`
- `plan_queued`

## Safety Constraints

- Plan creation rejects unsupported action types.
- No direct actuation endpoints are introduced.
- Queue endpoint only performs a safe handoff stub by creating a queued task.
- Operator approval is required before queueing.

## Data Model

Added `WorkspaceActionPlan`:

- `plan_id`
- `target_resolution_id`
- `target_label`
- `target_zone`
- `action_type`
- `safety_mode`
- `planning_outcome`
- `status`
- `steps`
- `queued_task_id`
- `source`
- `metadata_json`
- `created_at`
