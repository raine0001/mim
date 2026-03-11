# Objective 44 — Constraint Evaluation Engine

## Goal

Introduce a centralized constraint evaluator that returns structured decisions (`allowed`, `allowed_with_conditions`, `requires_confirmation`, `requires_replan`, `blocked`) and explanation metadata, then integrate it into key execution paths.

## Scope Delivered

### Task A: Constraint model

Added persisted evaluation model:

- `ConstraintEvaluation` (table: `constraint_evaluations`)
  - source/actor
  - goal/action/workspace/system/policy inputs
  - decision
  - violations and warnings
  - recommended next step
  - confidence
  - explanation metadata

### Task B: Evaluation engine

Implemented Objective 44 core evaluator:

- `core/constraint_engine.py`
- `core/constraint_service.py`

Added endpoints:

- `POST /constraints/evaluate`
- `GET /constraints/last-evaluation`
- `GET /constraints/history`

Input model:

- `goal`
- `action_plan`
- `workspace_state`
- `system_state`
- `policy_state`

Output model:

- `decision`
- `violations`
- `warnings`
- `recommended_next_step`
- `confidence`
- `explanation`

### Task C: System integration

Integrated evaluator into:

- autonomous proposal auto-execution path (`_maybe_auto_execute_workspace_proposal`)
- action-plan execution dispatch (`/workspace/action-plans/{plan_id}/execute`)
- capability-chain step advancement (`/workspace/capability-chains/{chain_id}/advance`)
- execution resume gating (`/workspace/executions/{execution_id}/resume`)

### Task D: Explanation interface

Added read APIs for inspectability:

- latest decision: `GET /constraints/last-evaluation`
- historical decisions: `GET /constraints/history`

All evaluations are journaled for traceability.

### Task E: Focused tests

Added focused Objective 44 integration coverage:

- `tests/integration/test_objective44_constraint_evaluation_engine.py`

Scenarios:

- allowed decision
- blocked hard constraint
- soft warning with replan recommendation
- requires confirmation decision
- explanation + history endpoint validation

## Manifest Updates

- schema version: `2026-03-11-35`
- capability added: `constraint_evaluation_engine`
- endpoints added:
  - `/constraints/evaluate`
  - `/constraints/last-evaluation`
  - `/constraints/history`
