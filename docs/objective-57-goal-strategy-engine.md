# Objective 57: Goal Strategy Engine

Objective 57 shifts MIM from cross-domain context understanding to strategic goal selection.

## Scope Implemented

- Persistent strategy-goal model separate from immediate proposals.
- Strategy synthesis from cross-domain context (workspace, communication, external information, development, self-improvement).
- Deterministic multi-factor strategy ranking.
- Strategy-to-plan bridge into horizon plans and improvement proposals.
- Inspectable strategy APIs with explainability and downstream influence tracing.

## Strategy Goal Model

Each strategy goal persists:

- `strategy_goal_id`
- `strategy_type`
- `origin_context_id`
- `priority` + `priority_score`
- `supporting_evidence`
- linked downstream objects (horizon plans/proposals/maintenance)
- `status`
- `success_criteria`

## Strategy Synthesis

Objective 57 synthesizes strategy candidates such as:

- `maintain_workspace_readiness`
- `reduce_operator_interruption_load`
- `stabilize_uncertain_zones_before_action`
- `prioritize_development_improvements_affecting_active_workflows`

## Strategy Ranking

Candidate strategic goals are scored using:

- urgency
- confidence
- expected impact
- risk
- operator preference influence
- developmental friction patterns

Sorting is deterministic by score and strategy type for stable ordering.

## Strategy-to-Plan Bridge

A strategic goal can generate or influence:

- horizon plans
- improvement proposals
- maintenance cycles (optional)
- operator recommendations

## Contract Surface

- `POST /strategy/goals/build`
- `GET /strategy/goals`
- `GET /strategy/goals/{strategy_goal_id}`

## Explainability

Strategy responses expose:

- why this strategic goal was formed
- which domains contributed
- which cross-domain links supported it
- what downstream actions/plans it influences
