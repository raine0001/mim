# Objective 77 - Conversational Simulation and Evaluation Harness

## Summary

Objective 77 introduces an automated conversation simulation/evaluation harness for MIM.
It focuses on policy and orchestration quality, not base-model retraining.

Key outcomes:
- Structured scenario library and synthetic persona coverage.
- Repeatable staged evaluation runs (smoke, expanded, stress, regression).
- Deterministic seeded runs for comparison.
- Baseline drift gate to catch behavior regressions.

## Artifacts

- `conversation_eval_runner.py`
- `conversation_profiles.json`
- `conversation_scenarios/scenario_library.json`
- `conversation_scenarios/focused_failure_tag_scenarios.json`
- `tests/tod/test_conversation_eval_runner.py`
- `scripts/run_conversation_eval_regression.sh`
- `scripts/run_conversation_targeted_ab.sh`
- `scripts/compare_conversation_reports.py`

## Stages

Default target conversation counts:
- `smoke`: 25
- `expanded`: 100
- `stress`: 500
- `regression`: 1000

Each conversation is a scenario x persona pair. If stage target exceeds unique pairs,
the runner samples additional pairs deterministically from the same pool.

## Scoring Dimensions

- relevance
- non_repetition
- brevity
- initiative
- safety
- smoothness
- task_completion
- overall

Additional targeted failure checks:
- `context_drift`
- `over_explaining`
- `repeated_clarifier_pattern`
- `missing_confirmation`

## Regression Gate

Optional baseline comparison enforces drift budgets:
- `max_overall_drop` (default: 0.03)
- `max_bucket_drop` (default: 0.08)
- `max_failure_increase` (default: 10)

If gate is enabled and fails, runner exits non-zero (`2`).

## Commands

Single stage run:

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 \
/home/testpilot/mim/.venv/bin/python conversation_eval_runner.py \
  --stage smoke \
  --seed 20260317 \
  --randomize \
  --turn-delay-ms 0 \
  --output runtime/reports/conversation_score_report.smoke.json
```

Write baseline from regression stage:

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 \
/home/testpilot/mim/.venv/bin/python conversation_eval_runner.py \
  --stage regression \
  --seed 20260317 \
  --randomize \
  --turn-delay-ms 0 \
  --write-baseline runtime/reports/conversation_baseline.json \
  --output runtime/reports/conversation_score_report.regression.json
```

Run unattended full staged workflow:

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 scripts/run_conversation_eval_regression.sh
```

## Focused Failure-Tag Packs

Focused pack file: `conversation_scenarios/focused_failure_tag_scenarios.json`

Buckets:
- `low_relevance_focus`
- `response_loop_focus`
- `safety_boundary_focus`

Targeted A/B workflow:

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 scripts/run_conversation_targeted_ab.sh
```

This produces:
- `runtime/reports/conversation_targeted_ab_A.json`
- `runtime/reports/conversation_targeted_ab_B.json`
- `runtime/reports/conversation_targeted_ab_diff.json`

Comparison focuses by default on:
- `low_relevance`
- `response_loop_risk`
- `missing_safety_boundary`

## CI Regression Gates

Primary gate scripts:
- `scripts/enforce_conversation_regression_gate.py`
- `scripts/run_conversation_quality_gate.sh`

PR gate (targeted pack):

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 scripts/run_conversation_quality_gate.sh pr
```

Nightly gate (full staged regression):

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 scripts/run_conversation_quality_gate.sh nightly
```

Gate metrics:
- `overall`
- `failure_count`
- `low_relevance`
- `response_loop_risk`
- `missing_safety_boundary`

Policy:
- warning bands for small movement (`overall`, `failure_count`, `low_relevance`)
- hard fail on `response_loop_risk` regressions
- hard fail on `missing_safety_boundary` regressions
- hard fail on meaningful `low_relevance` increase and overall/failure-count drops

## Notes

- The harness uses `/gateway/intake/text` and `/mim/ui/state`.
- It evaluates behavior-level quality and policy consistency.
- It should be paired with live UI voice-loop checks for microphone/TTS boundary validation.
