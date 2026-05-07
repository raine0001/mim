# Objective 33 — Autonomous Execution Proposals

## Summary

Objective 33 introduces a policy-driven proposal layer that suggests safe execution for already-approved and simulation-safe action plans. Proposals remain operator-mediated and reuse Objective 32 execution guardrails.

## Added API

- `GET /workspace/execution-proposals/policy`
  - Returns autonomous proposal policy requirements and default thresholds.

- `GET /workspace/execution-proposals`
  - Lists execution proposals (`pending` by default).

- `POST /workspace/action-plans/{plan_id}/propose-execution`
  - Creates an autonomous execution proposal when execution preconditions are satisfied.

- `POST /workspace/execution-proposals/{proposal_id}/accept`
  - Accepts proposal and forwards to guarded execute flow.

- `POST /workspace/execution-proposals/{proposal_id}/reject`
  - Rejects proposal without execution.

## Policy

Proposal/acceptance is constrained by:

- operator approval required on plan
- simulation outcome must be `plan_safe`
- simulation gate must pass
- collision risk must remain below threshold
- target confidence must remain above policy minimum

## Safety Posture

- No autonomous direct actuation bypass is introduced.
- Acceptance remains explicit operator control.
- Final execution still runs through Objective 32 precondition enforcement.
