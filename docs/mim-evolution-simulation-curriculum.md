# MIM Evolution Simulation Curriculum

Date: 2026-03-17
Purpose: Continuously improve MIM across conversation logic, intent understanding, and action execution.

## What This Adds

1. Conversation training set: `conversation_scenarios/mim_evolution_training_set.json`
2. Evolution user profiles: `conversation_profiles_evolution.json`
3. Action simulation runner: `scripts/run_mim_action_simulations.py`
4. Combined curriculum runner: `scripts/run_mim_evolution_simulations.sh`

## Core Dimensions

1. `logic_core`: greeting, ambiguity handling, interruption recovery
2. `understanding_core`: direct answers, context carry, TOD status grounding
3. `execution_intent`: web summary intent, capability introspection, confirmation boundaries
4. `voice_reliability`: low-value/noisy input recovery and wake-word handling patterns
5. `safety_boundary`: refusal/limits for unsafe or private requests
6. `continuity`: multi-turn coherence and concise summarization under context load

## Run Command

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 scripts/run_mim_evolution_simulations.sh
```

## Regression-Guarded Soak

Use bounded windows with explicit regression thresholds enabled.

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 \
DURATION_SECONDS=10800 \
TARGET_CONVERSATIONS=320 \
WINDOW_POINTS=40 \
MAX_OVERALL_DROP=0.01 \
MIN_ACTION_PASS_RATIO=0.95 \
MAX_TAG_RATE_INCREASE=0.05 \
MAX_TAG_RATE_RATIO=1.5 \
WATCH_TAGS=low_relevance,response_loop_risk,missing_safety_boundary,repeated_clarifier_pattern,context_drift,clarification_spam \
scripts/run_mim_evolution_soak.sh
```

If a regression threshold is exceeded, the soak exits non-zero after trend analysis.

## Outputs

1. `runtime/reports/mim_evolution_conversation_report.json`
2. `runtime/reports/mim_action_simulation_report.json`
3. `runtime/reports/mim_evolution_training_summary.json`

## How To Use For Improvement Loops

1. Run the curriculum before and after each conversational logic change.
2. Compare `top_failures` and `bucket_average` in the conversation report.
3. Compare `hour_window_comparison.bucket_delta` to detect first-hour vs last-hour drift.
4. Confirm action pass ratio does not regress.
5. Prioritize fixes by this order:
   - repeated clarifier and context drift
   - low relevance and over-explaining
   - action path failures or policy-guard confusion
6. Re-run until both conditions hold:
   - conversation overall score improves or remains stable
   - action pass ratio is unchanged or better

## Suggested Weekly Cadence

1. Daily smoke run on latest branch.
2. Full run after any voice-gating or prompt-shaping change.
3. Weekly summary review of top recurring failures.

## Notes

1. Action simulation accepts policy guards as valid outcomes (for example disabled web access), because explicit boundary handling is part of healthy behavior.
2. Runtime deployment verification uses state marker fields like `runtime_build` and `runtime_features`.
