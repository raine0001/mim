# Objective 120 - Recovery Policy Tuning

Date: 2026-04-07
Status: promoted_verified
Depends On: Objective 84, Objective 96, Objective 97, Objective 119
Target Release Tag: objective-122

## Summary

Objective 120 adds an explicit recovery-policy tuning layer on top of recovery learning and the existing autonomy boundary envelope.

Before this slice, repeated operator-mediated recovery outcomes could change recovery learning state, but the system did not expose a stable contract describing whether future recovery attempts should keep current recovery autonomy, lower it, or require operator takeover.

Objective 120 closes that gap by deriving `recovery_policy_tuning` during recovery evaluation, persisting it through attempt and outcome metadata, projecting it into journal evidence, and surfacing it in `/mim/ui/state` as operator-visible recovery guidance.

## Delivered Slice

Objective 120 is now implemented as an inspectable recommendation layer. It does not mutate workspace autonomy boundaries directly.

Delivered behavior:

- recovery evaluation responses now expose `recovery_policy_tuning`
- `recovery_policy_tuning` normalizes future recovery guidance into:
  - `maintain_current_recovery_autonomy`
  - `lower_scope_autonomy_for_recovery`
  - `require_operator_takeover`
- the contract carries:
  - current boundary level
  - recommended future recovery boundary level
  - floor handling when the scope is already at `operator_required`
  - operator-review requirement
  - evidence and rationale from recovery learning
- accepted recovery attempts preserve the same tuning contract in metadata and response payloads
- evaluated recovery outcomes preserve the same tuning contract in metadata and response payloads
- recovery attempt and outcome journal entries now persist `recovery_policy_tuning`
- `/execution/recovery/{trace_id}` returns the tuning contract with the recovery state snapshot
- `/mim/ui/state` operator reasoning now exposes `execution_recovery_policy_tuning`
- `/mim/ui/state.current_recommendation` prefers recovery-policy tuning guidance when the policy action is not `maintain_current_recovery_autonomy`

## Behavioral Anchor

Objective 120 is considered delivered when these statements are true:

- repeated operator-mediated recovery history can produce a stable recommendation to lower future recovery autonomy before another retry
- that recommendation remains inspectable even when the active scope is already at the `operator_required` floor
- attempt, outcome, trace, journal, and operator UI surfaces preserve the same tuning contract
- operator-facing reasoning can answer not just what recovery happened, but how future recovery autonomy should change and why

## Key Implementation Anchors

- `core/execution_recovery_service.py`
- `core/routers/execution_control.py`
- `core/routers/mim_ui.py`
- `core/schemas.py`
- `tests/integration/test_objective120_recovery_policy_tuning.py`
- `tests/integration/test_objective97_recovery_learning_escalation_loop.py`
- `tests/integration/test_objective96_execution_recovery_safe_resume.py`
- `tests/integration/test_objective84_operator_visible_system_reasoning.py`
- `tests/integration/test_objective119_recovery_taxonomy.py`

## Validation Evidence

Focused Objective 120 proof:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective120_recovery_policy_tuning -v`

The focused Objective 120 lane proves:

- repeated operator-mediated recovery outcomes escalate the next recovery into `lower_scope_autonomy_for_recovery`
- recovery evaluation exposes `recovery_policy_tuning` with stable evidence and rationale
- floor behavior is explicit when the active scope is already clamped to `operator_required`
- accepted attempts, evaluated outcomes, trace state, journal evidence, and `/mim/ui/state` all preserve the same tuning contract
- operator-visible current recommendation prefers recovery-policy tuning when future recovery autonomy should change

Adjacent regression slice:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective97_recovery_learning_escalation_loop tests.integration.test_objective84_operator_visible_system_reasoning tests.integration.test_objective96_execution_recovery_safe_resume tests.integration.test_objective119_recovery_taxonomy -v`

That slice verifies the Objective 120 work did not break:

- Objective 97 recovery-learning escalation behavior
- Objective 84 operator-visible system reasoning
- Objective 96 safe-resume and recovery persistence semantics
- Objective 119 recovery taxonomy propagation

## Readiness Assessment

- recovery-policy tuning derivation: ready
- attempt, outcome, trace, and journal propagation: ready
- operator-visible tuning guidance: ready
- shared-runtime UI proof hardening: ready
- adjacent regression coverage: ready

## Readiness Decision

- Objective 120 implementation status: PROMOTED_VERIFIED
- Recommendation: use Objective 120 as the inspectable autonomy-tuning layer above recovery learning, while keeping actual boundary mutation decisions in the existing autonomy-boundary governance path.