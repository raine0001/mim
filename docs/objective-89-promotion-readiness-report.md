# Objective 89 Promotion Readiness Report

Date: 2026-03-26
Objective: 89
Title: Proposal Policy Convergence
Status: ready_for_promotion_review

## Scope Delivered

Objective 89 closes the next loop after proposal arbitration learning.

Objective 88.2 taught MIM to learn from arbitration outcomes.

Objective 88.3 propagated that learning into downstream governance consumers.

Objective 88.4 surfaced the signal in autonomy review visibility.

Objective 89 adds a bounded, durable proposal-policy layer that shapes proposal behavior before TOD has to arbitrate the same recurring pattern again.

The slice stays intentionally narrow.

It does not remove TOD arbitration, it does not rewrite proposal content autonomously, and it does not let weak or stale evidence silently hard-lock proposal behavior.

## Implementation Summary

Objective 89 introduces durable proposal-policy preference profiles derived from repeated workspace proposal arbitration outcomes.

Delivered behavior:

- repeated losses can converge to bounded downgrade or suppression before arbitration
- repeated wins can converge to preferred shaping
- stale or weak evidence remains advisory rather than policy-active
- proposal payloads expose why shaping happened
- proposal-policy state is queryable through the workspace API and visible in MIM operator reasoning

Most importantly, contradictory fresh evidence reopens an existing converged policy instead of letting the system become stubborn.

That keeps the behavior adaptive rather than freezing the first strong signal forever.

## Exact Affected Surfaces

- `core/proposal_policy_convergence_service.py`
- `core/models.py`
- `core/schemas.py`
- `core/routers/workspace.py`
- `core/routers/mim_ui.py`
- `tests/integration/test_objective89_proposal_policy_convergence.py`
- `docs/objective-89-proposal-policy-convergence.md`

## Behavioral Anchor

The promotion-relevant contract for Objective 89 is:

- durable proposal-policy memory remains scope-aware and inspectable
- shaping happens before arbitration through bounded score deltas and optional score caps
- suppression and downgrade require repeated evidence rather than one-off noise
- stale evidence decays back toward advisory behavior
- contradictory fresh evidence reopens an active policy instead of reinforcing a bad lock-in
- operator-facing payloads explain why a proposal was preferred, downgraded, or suppressed

## Inspectability Contract

Objective 89 is promotion-relevant because the shaping is not hidden.

- workspace proposal payloads now carry `proposal_policy_convergence`
- workspace proposal priority breakdown now carries `proposal_policy_convergence`
- `/workspace/proposals/policy-preferences` exposes converged profiles directly
- `/mim/ui/state` now carries `operator_reasoning.proposal_policy`
- rationale fields include explicit pre-arbitration explanations such as `why_this_proposal_was_deprioritized_before_emission`

## Validation Summary

Focused Objective 89 lane on a fresh isolated server (`127.0.0.1:18001`):

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective89_proposal_policy_convergence -v`
- Result: PASS (`2/2`)

Adjacent proposal-arbitration and policy lane requested for promotion readiness:

- `/home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective88_2_proposal_arbitration_learning tests.integration.test_objective88_3_proposal_arbitration_learning_propagation tests.integration.test_objective88_4_proposal_arbitration_learning_autonomy_visibility tests.integration.test_objective89_proposal_policy_convergence -v`
- Result: PASS (`8/8`)

Validated coverage in that adjacent lane:

- 88.2 proposal arbitration learning still biases ranking and strategy weighting correctly
- 88.3 propagation still influences commitment monitoring, stewardship follow-up, and inquiry weighting
- 88.4 autonomy review visibility still surfaces bounded arbitration-learning influence
- 89 repeated-loss suppression and contradictory-fresh-evidence reopening both behave as intended

## Known Caveats

- This readiness decision is based on the directly affected 88.2/88.3/88.4/89 arbitration-policy neighborhood, which is the correct lane for detecting overcorrection in pre-arbitration shaping.
- A full branch-wide objective sweep was not rerun as part of this promotion-readiness step.
- During Objective 89 validation, a real serializer bug was found and fixed: the new policy-profile payload builder assumed an `updated_at` field that the model does not currently persist.
- The isolated server still emits an existing `SyntaxWarning` in `core/routers/mim_ui.py` for an invalid escape sequence. It did not block the readiness lane, but it remains a cleanup item.

## Readiness Assessment

- bounded proposal shaping: ready
- contradictory-fresh-evidence reopening: ready
- inspectability of pre-arbitration behavior: ready
- adjacent arbitration-policy lane: green
- broad branch-wide promotion baseline: not revalidated in this step

## Readiness Decision

- Objective 89 feature slice: READY_FOR_PROMOTION_REVIEW
- Recommendation: treat Objective 89 as promotion-ready based on the green adjacent arbitration/policy lane, with the explicit caveat that contradictory fresh evidence reopens policy and prevents the convergence layer from becoming stubborn.