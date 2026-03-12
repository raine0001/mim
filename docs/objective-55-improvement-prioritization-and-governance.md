# Objective 55: Improvement Prioritization and Governance

Objective 55 adds a self-governing prioritization layer on top of proposal and recommendation generation so MIM can decide what to run now, what to defer, and what requires stricter review.

## Scope Implemented

- Weighted improvement priority scoring over five factors.
- Persistent improvement backlog with inspectable ranking, evidence, and risk context.
- Governance policy decisions for auto-experiment, operator review, defer, and reject.
- Explicit improvement lifecycle states for governed progression.
- Operator visibility endpoints for backlog list and per-item reasoning.

## Priority Scoring Factors

Each backlog item computes a bounded `priority_score` using:

- `impact_estimate`
- `evidence_strength`
- `risk_level` / `risk_score`
- `affected_capabilities`
- `operator_preference_weight`

## Governance Policy

Objective 55 policy chooses one of:

- `auto_experiment`
- `request_operator_review`
- `defer_improvement`
- `reject_improvement`

Policy thresholds are emitted in `reasoning.governance_policy` for operator auditability.

## Improvement Lifecycle States

Backlog entries now use lifecycle states:

- `proposed`
- `queued`
- `experimenting`
- `evaluating`
- `recommended`
- `approved`
- `rejected`

Objective 55 also synchronizes lifecycle status with recommendation approval/rejection outcomes.

## Backlog Endpoints

- `POST /improvement/backlog/refresh`
- `GET /improvement/backlog`
- `GET /improvement/backlog/{improvement_id}`

## Operator Visibility

Per-item detail exposes:

- why item was ranked where it was (`why_ranked`)
- what evidence exists (`evidence_summary`, `evidence_count`)
- what risk exists (`risk_level`, `risk_summary`)
- governance rationale and thresholds (`reasoning`)

## Why Objective 55 Matters

Without prioritization and governance, proposal generation can become noisy and unbounded. Objective 55 creates a controlled improvement pipeline that allocates experimentation budget and surfaces risk-aware operator review points.
