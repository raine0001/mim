# Objective 63: Cross-Domain Task Orchestration

Objective 63 adds a persistent orchestration layer that turns cross-domain reasoning into inspectable, dependency-aware execution paths with linked downstream artifacts.

## Scope Implemented

- Added persisted orchestration model in `workspace_task_orchestrations`.
- Added orchestration service that:
  - reuses cross-domain reasoning context as input,
  - computes contributing-domain signal coverage and priority,
  - resolves unmet dependencies via `ask`, `defer`, `replan`, or `escalate` policy paths,
  - creates linked downstream artifacts (goal/plan/proposal/question) when appropriate.
- Added orchestration API endpoints:
  - `POST /orchestration/build`
  - `GET /orchestration`
  - `GET /orchestration/{orchestration_id}`
- Added explainability fields on orchestration records:
  - contributing domains,
  - priority reason and domain signal counts,
  - dependency resolution decision and unmet dependency details,
  - downstream artifact links.

## Validation Intent

Objective 63 verifies that multi-domain signals are coordinated into coherent task orchestration, blocked dependencies are handled with explicit policy paths, and orchestration state remains fully inspectable for operator review.
