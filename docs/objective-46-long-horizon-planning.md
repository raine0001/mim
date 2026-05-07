# Objective 46 — Long-Horizon Planning

## Goal

Enable MIM to plan across multiple pending goals over a longer horizon with staged execution, checkpointed progress, and future-drift replanning.

## Delivered Scope

### A) Planning Horizon Model

- Added persistent long-horizon plan model:
  - ranked goals over a planning horizon
  - staged action graph
  - expected future constraints
  - scoring and explanation context
- Added persistent checkpoint model:
  - checkpoint sequence and type
  - status lifecycle (`planned`, `active`, `checkpoint_reached`, `needs_re_evaluation`, `replanned`, `complete`)
  - replan trigger metadata.
- Added persistent replan event model for future-assumption drift audit.

### B) Multi-Goal Planner

- Added multi-goal ranking and staged graph generation.
- Planner composes prerequisites when needed:
  - `refresh_workspace_zone` when map freshness is stale
  - `rescan_target_area` when object confidence is below threshold.
- Supports dependency-aware ordering across goals.
- Defers lower-priority physical goals when human/shared-workspace presence is active.

### C) Checkpointed Execution

- Added checkpoint progression endpoint.
- Supports explicit transitions through the Objective 46 state model.
- Advances next checkpoint automatically when a checkpoint is reached.

### D) Future-State Scoring

Planner scoring combines:

- goal priority and urgency
- expected value
- map freshness
- object confidence
- human-aware state and physical-risk deferral policy
- operator preferences
- learned constraint signal (derived from Objective 45 proposal outcomes).

### E) Inspectability

Plan response includes:

- ranked goals with score breakdown
- staged action graph
- explanation metadata for selection and replan triggers
- current and next checkpoint
- replan metadata after future drift.

## API Endpoints

- `POST /planning/horizon/plans`
- `GET /planning/horizon/plans/current`
- `GET /planning/horizon/plans/{plan_id}`
- `POST /planning/horizon/plans/{plan_id}/checkpoints/advance`
- `POST /planning/horizon/plans/{plan_id}/future-drift`

## Notes

- Objective 46 is proposal/planning focused and does not introduce direct actuation behavior.
- Hard constraints remain governed by existing constraint and safety engines.
