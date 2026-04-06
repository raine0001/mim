# Objective 88.3 Production Promotion Plan

Date: 2026-03-25
Objective: 88.3
Title: Proposal Arbitration Learning Propagation
Target Release Tag: objective-88-3
Status: pending_promotion_execution

## Preconditions

Promotion should not be attempted until all are true:

1. Focused Objective 88.3 regression is green.
2. Adjacent 57/60/80/86/87/88.2/88.3/88.4 lane is green.
3. The branch-wide objective integration sweep is either green or the remaining failures are explicitly accepted as unrelated promotion exceptions.
4. A privileged host operator is available if the production promotion path requires `sudo`.

## Promotion Path

1. Re-run the isolated validation lane on the candidate revision:
   - `MIM_TEST_BASE_URL=http://127.0.0.1:18001 /home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective88_3_proposal_arbitration_learning_propagation`
2. Re-run the adjacent lane:
   - `MIM_TEST_BASE_URL=http://127.0.0.1:18001 /home/testpilot/mim/.venv/bin/python -m unittest tests.integration.test_objective57_goal_strategy_engine tests.integration.test_objective60_stewardship_inquiry_followup tests.integration.test_objective80_execution_truth_strategy_scoring tests.integration.test_objective80_execution_truth_inquiry_hook tests.integration.test_objective86_commitment_enforcement_drift_monitoring_loop tests.integration.test_objective87_commitment_outcome_learning_loop tests.integration.test_objective88_2_proposal_arbitration_learning tests.integration.test_objective88_3_proposal_arbitration_learning_propagation tests.integration.test_objective88_4_proposal_arbitration_learning_autonomy_visibility`
3. Execute the repository production promotion script with the target tag for this slice.
4. Verify `/health` and `/manifest` on `:8000` after restart.
5. Capture a read-only probe for:
   - proposal arbitration learning payload visibility
   - stewardship follow-up preference inspectability
   - inquiry answer-path weighting inspectability
6. If the host requires privilege escalation, capture the exact blocking point in the production report instead of marking the objective promoted.

## Required Production Verification Evidence

The production report should verify that:

1. the live manifest reflects the intended promoted build identity
2. workspace proposal arbitration learning remains queryable
3. stewardship follow-up summaries expose proposal arbitration follow-up metadata
4. governed inquiry question payloads expose arbitration-weighted answer paths
5. `/mim/ui/state` remains healthy after the neighboring runtime fixes

## Promotion Outcome Rules

- If promotion executes and the live build matches the intended release tag, record a production promotion report.
- If the runtime is healthy but still serving an older manifest or lacks the expected 88.3 surfaces, mark promotion blocked.
- If the host prevents restart or deployment due to privilege requirements, capture the block and leave the objective at readiness-complete but not production-promoted.
