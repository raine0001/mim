# Objective 49 — Self-Improvement Proposal Engine

## Goal

Enable MIM to detect repeated friction patterns from experience and produce structured, review-gated self-improvement proposals instead of mutating runtime behavior directly.

## Delivered Scope

### A) Improvement Proposal Model

- Added persistent improvement proposal model with:
  - `proposal_type`
  - `trigger_pattern`
  - `evidence_summary` + structured `evidence`
  - `affected_component`
  - `suggested_change`
  - `confidence`
  - `safety_class`
  - `status`
- Added persistent improvement artifact model for accepted proposals.

### B) Evidence Aggregation

Rule-based aggregation over repeated signals from:

- soft-constraint friction with successful outcomes
- repeated manual strategy overrides
- repeated replan events
- repeated action retries
- strategy starvation (active/stable strategies never influencing plans)
- throttle/cooldown friction patterns

### C) Proposal Generation Rules

- Rule-based generation endpoint produces proposals only when pattern counts exceed configurable thresholds.
- Duplicate suppression prevents repeated open proposals for the same trigger/component pair.

### D) Improvement Review Surface

- Added review APIs:
  - `POST /improvement/proposals/generate`
  - `GET /improvement/proposals`
  - `GET /improvement/proposals/{proposal_id}`
  - `POST /improvement/proposals/{proposal_id}/accept`
  - `POST /improvement/proposals/{proposal_id}/reject`
- Accepted proposals create bounded artifacts (`policy_change_candidate`, `test_candidate`, or `gated_workflow_item`) with `pending_review` status.

### E) Explainability

Each proposal includes:

- triggering pattern
- evidence summary + structured evidence payload
- suggested change
- risk summary
- recommended test-first path

## 49A Decision Record Foundation Note

- Decision record layer from Objective 48 is extended with `result_quality` to strengthen downstream improvement evidence quality modeling.
- Improvement engine uses decision/constraint/strategy/plan/action signals while preserving explicit auditability.
