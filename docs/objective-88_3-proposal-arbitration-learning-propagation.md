# Objective 88.3 Proposal Arbitration Learning Propagation

Status: implemented_first_slice

Objective

Propagate proposal arbitration learning beyond proposal ranking and strategy scoring into the next aligned downstream surfaces.

Implemented Scope

1. Commitment expectation shaping in `core/operator_commitment_monitoring_service.py`
2. Stewardship follow-up preference surfacing in `core/stewardship_service.py`
3. Governed inquiry answer-path weighting and ordering in `core/inquiry_service.py`

Design Constraints

1. Keep the influence bounded and inspectable.
2. Reuse the same proposal-arbitration learning signal rather than creating a second learning mechanism.
3. Do not auto-select inquiry answers or override operator decisions.
4. Do not fan the signal into autonomy-review visibility in this slice.

Inspectability

1. Commitment monitoring reasoning now includes `proposal_arbitration_expectation`.
2. Stewardship cycle summaries and history now include `preferred_followup_type`, `preferred_followup_weight`, and `proposal_arbitration_followup`.
3. Inquiry question `candidate_answer_paths` now carry `score`, `proposal_arbitration_weight`, and `proposal_arbitration_learning`.

Validation Target

1. Focused propagation regression: `tests.integration.test_objective88_3_proposal_arbitration_learning_propagation`
2. Adjacent lane after implementation: Objective 57, 60, 80 inquiry/strategy, 86, 87, 88.2, and 88.3 on a fresh isolated server.
