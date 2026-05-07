# Objective 45 — Constraint Weight Learning

## Goal

Introduce a proposal-only learning loop for soft constraint tuning so MIM can learn from outcomes without modifying hard safety constraints.

## Scope (V1)

- record outcomes for prior constraint evaluations
- compute simple rolling success/failure patterns by constraint key
- generate soft constraint adjustment proposals from observed outcomes
- keep all changes auditable, reversible, and operator-visible

## Hard Boundary

The following remain non-learnable and cannot be modified autonomously:

- human safety constraints
- unlawful behavior constraints
- system integrity constraints
- irreversible damage risk constraints

## API Surface

- `POST /constraints/outcomes`
- `GET /constraints/learning/stats`
- `POST /constraints/learning/proposals/generate`
- `GET /constraints/learning/proposals`

## Data Model Additions

- `constraint_evaluations`
  - `outcome_result`
  - `outcome_quality`
  - `outcome_recorded_at`
- `constraint_adjustment_proposals`
  - proposal metadata for soft constraint changes

## Learning Flow

`evaluate -> record outcome -> aggregate patterns -> generate proposal -> validate -> test -> gated promotion`

MIM proposes policy adjustments; it does not silently apply them.

## V1 Heuristics

- counters per constraint key
- rolling success rates
- threshold proposal generation for repeated success under soft warnings
- proposal dedupe for active equivalent proposals
