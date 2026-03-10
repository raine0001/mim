# Objective 17: Routing Learning and Decision Quality

## Objective

Implement evidence-backed routing quality improvements so engine selection confidence is learned from outcomes instead of static reasoning.

## Task Groups

- A: Capture routing execution metrics
- B: Build rolling engine summary stats
- C: Evidence-backed confidence inputs
- D: Standardize failure categorization
- E: Add inspectability endpoints

## Metric Fields (per run)

- `engine_name`
- `task_category`
- `started_at`, `completed_at`, `latency_ms`
- `outcome` (`success`/`fail`)
- `blocked_pre_invocation` (bool)
- `review_passed` (bool)
- `fallback_used` (bool)
- `result_category`
- `performance_delta`
- `failure_category`

## Standard Failure Categories

- `contract_drift_breaking`
- `validation_failure`
- `execution_error`
- `timeout`
- `review_rejection`
- `no_eligible_engine`

## Engine Summary Stats

Per engine, compute rolling:

- `total_runs`
- `pass_rate`
- `review_correction_rate`
- `blocked_rate`
- `average_latency_ms`
- `weighted_recent_score`

## Confidence Inputs

Routing confidence should include:

- sample size
- recent pass rate
- fallback frequency
- historical performance by task category
- penalty for recent failures

## Inspectability Endpoints (initial JSON)

- `GET /routing/history`
- `GET /routing/metrics`
- `GET /routing/summary`

## Deployment Pattern

1. Implement on Development PC in `feature/objective-17-*`
2. Merge to `dev`
3. Deploy to server test stack
4. Validate metrics/summary endpoints + smoke
5. Promote approved SHA to prod
