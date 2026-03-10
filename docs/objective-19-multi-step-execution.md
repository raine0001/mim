# Objective 19: Multi-step Goal Execution Chain

## Scope

Objective 19 extends Objective 18 custody from single-step execution into chained, ordered execution under one goal.

This phase adds:

1. Action sequencing fields (`sequence_index`, `depends_on_action_id`, `parent_action_id`)
2. Persisted goal plan (`ordered_action_ids`, `current_step_index`, `derived_status`)
3. Goal-level chain outcome classification
4. Step-by-step timeline inspection with snapshots and validations
5. Goal inspectability endpoints for plan/timeline/status

## Data Model Additions

### Action
- `sequence_index` (ordered execution index)
- `depends_on_action_id` (optional dependency edge)
- `parent_action_id` (optional hierarchical edge)
- `status` as per-step lifecycle marker

### GoalPlan
- `goal_id`
- `ordered_action_ids`
- `current_step_index`
- `derived_status`

## Chain Outcome Classification

Goal-level derived statuses:
- `achieved`
- `partial`
- `failed`
- `blocked`
- `unknown`

Step status signals used by derivation:
- `completed`/`success`/`achieved`
- `failed`
- `blocked`
- `retried`
- `skipped`

## Endpoints

### Plan
- `POST /goals/{goal_id}/plan`
- `GET /goals/{goal_id}/plan`

### Timeline and status
- `GET /goals/{goal_id}/timeline`
- `GET /goals/{goal_id}/status`

### Existing custody compatibility
- `GET /goals/{goal_id}/custody`
- `GET /tasks/{task_id}/custody`

## Validation Strategy

1. Development machine implementation + tests
2. Deploy to MIM Server test stack
3. Smoke + chained-goal integration probes
4. Clean rebuild gate
5. Promote to prod only after Objective 19 gate pass
