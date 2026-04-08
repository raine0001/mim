# Objective 119 - Recovery Taxonomy

Date: 2026-04-07
Status: promoted_verified
Depends On: Objective 84, Objective 96, Objective 97, Objective 116
Target Release Tag: objective-122

## Summary

Objective 119 adds a stable recovery taxonomy layer to the execution recovery plane.

Before this slice, recovery evaluation, attempts, and outcomes could explain the current decision, but the recovery family and outcome class were not exposed as a durable, operator-visible classification across all recovery surfaces.

Objective 119 closes that gap by deriving stable recovery taxonomy and classification fields during recovery evaluation, persisting them through attempts and outcomes, exposing them through execution-control payloads and journals, and surfacing them in the operator-facing `/mim/ui/state` recovery read model.

## Delivered Slice

Objective 119 is now implemented as a classification layer on top of the existing recovery plane.

Delivered behavior:

- recovery evaluation responses now expose:
  - `recovery_classification`
  - `recovery_taxonomy`
- recovery attempt persistence now carries recovery taxonomy and classification through metadata, trace evidence, and response payloads
- recovery outcome evaluation now exposes and persists:
  - `recovery_classification`
  - `recovery_taxonomy`
  - `recovery_outcome_classification`
  - `recovery_outcome_taxonomy`
- recovery attempt and outcome journal entries now preserve taxonomy/classification plus caller metadata such as `run_id`
- `/execution/recovery/{trace_id}` now returns latest recovery outcome metadata with outcome classification/taxonomy intact
- `/mim/ui/state` operator reasoning now surfaces `recovery_classification` and `recovery_taxonomy` as part of the recovery snapshot

## Behavioral Anchor

Objective 119 is considered delivered when these statements are true:

- recovery evaluation can classify the recommended recovery path in a stable, inspectable way
- accepted recovery attempts preserve the same recovery classification in downstream evidence
- recovery outcomes classify terminal recovery results instead of exposing only raw status strings
- operator-facing recovery reasoning can explain the current recovery family without needing to reverse-engineer internal recovery decisions

## Key Implementation Anchors

- `core/execution_recovery_service.py`
- `core/routers/execution_control.py`
- `core/routers/mim_ui.py`
- `tests/integration/test_objective119_recovery_taxonomy.py`
- `tests/integration/test_objective96_execution_recovery_safe_resume.py`
- `tests/integration/test_objective97_recovery_learning_escalation_loop.py`
- `tests/integration/test_objective84_operator_visible_system_reasoning.py`

## Validation Evidence

Focused Objective 119 proof:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective119_recovery_taxonomy -v`

The focused Objective 119 lane proves:

- recovery evaluation exposes stable classification and taxonomy fields
- `/mim/ui/state` recovery reasoning exposes coherent taxonomy fields on the operator surface
- accepted recovery attempts preserve classification/taxonomy
- recovery outcomes expose terminal recovery-outcome classification/taxonomy
- `/execution/recovery/{trace_id}` and journal evidence preserve the same metadata

Adjacent regression slice:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective96_execution_recovery_safe_resume tests.integration.test_objective97_recovery_learning_escalation_loop tests.integration.test_objective84_operator_visible_system_reasoning -v`

That slice verifies the Objective 119 work did not break:

- baseline safe-resume and recovery persistence semantics from Objective 96
- recovery-learning escalation behavior and operator-facing recovery-learning explanations from Objective 97
- operator-visible system reasoning on `/mim/ui/state` from Objective 84

## Readiness Assessment

- recovery taxonomy derivation: ready
- recovery attempt and outcome propagation: ready
- operator-visible recovery classification: ready
- adjacent regression coverage: ready

## Readiness Decision

- Objective 119 implementation status: PROMOTED_VERIFIED
- Recommendation: use Objective 119 as the recovery inspectability substrate before extending autonomy tuning or recovery-governance follow-on work.