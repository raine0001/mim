# Objective 51: Policy Experiment Sandbox

Objective 51 introduces a bounded sandbox for trialing policy and strategy adjustments before any production promotion decision.

## Scope Implemented

- Sandboxed experiment runs for improvement proposals.
- Baseline vs experimental outcome comparison.
- Structured recommendation output: `promote`, `revise`, or `reject`.
- Persisted experiment records for audit and longitudinal analysis.
- Journal traces for each experiment execution.

## Endpoints

- `POST /improvement/experiments/run`
- `GET /improvement/experiments`
- `GET /improvement/experiments/{experiment_id}`

## Request Contract

`POST /improvement/experiments/run`

- `actor`
- `source`
- `proposal_id` (optional)
- `experiment_type`
- `lookback_hours`
- `sandbox_mode`
- `metadata_json`

## Output Contract

Each experiment returns:

- baseline metrics
- experimental metrics
- comparison metrics
- recommendation and recommendation reason

## Safety Model

- Sandbox is non-invasive (`shadow_evaluation` by default).
- No direct runtime mutation occurs from experiment execution.
- Recommendation output is advisory and must pass gated workflow before any rollout.

## Recommendation Logic (V1)

The engine computes an improvement score from:

- friction reduction
- success-rate gain
- decision-quality gain

Then recommends:

- `promote` for meaningful bounded gains
- `revise` for partial gains
- `reject` when gains are insufficient

## Relationship to Objectives 49 and 50

- Objective 49 generates candidate changes (`ImprovementProposal`).
- Objective 51 tests those candidates in sandbox mode.
- Objective 50 operational maintenance outcomes can become additional experiment evidence.

## Promotion Lifecycle

Objective 51 follows the standard lifecycle:

`implement -> focused gate -> full regression gate -> promote -> production verification -> report`
