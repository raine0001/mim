# Objective 88.2 - Proposal Arbitration Learning

Date: 2026-03-25
Status: implemented_extended_slice
Depends On: Objective 22, Objective 74, Objective 75, Objective 85, Objective 86, Objective 87, Objective 88
Target Schema Version: 2026-03-24-70
Target Release Tag: objective-88-2

## Problem Statement

TOD can now arbitrate competing proposals, apply merge posture, and preserve trust-chain state.

MIM still generates proposals as if that arbitration layer does not exist.

That creates a wasteful loop:

- MIM proposes
- TOD rejects, suppresses, or reshapes
- MIM proposes the same shape again

Objective 88.2 closes the first part of that gap by teaching MIM to remember which proposal types usually win or lose under arbitration and to reflect that memory back into proposal ranking.

## Goal

Use real arbitration outcomes as a bounded learning signal that adjusts future proposal ranking without creating a hidden policy engine.

## First Slice

The first slice is intentionally narrow.

It does three things:

1. Records proposal arbitration outcomes through a dedicated API.
2. Computes a simple learned bias from recent win/loss/merge patterns per proposal type.
3. Applies a small priority bias during workspace proposal ranking.

## Extended Slice

The current implementation now carries that learning one layer downstream.

It also:

1. Aggregates arbitration learning for strategy-relevant proposal families.
2. Applies a bounded strategy-goal weighting adjustment during strategy goal scoring.
3. Preserves the influence in strategy ranking factors and reasoning payloads.

## In Scope

- arbitration outcome persistence
- proposal-type win-rate learning
- inspectable learning summary and recent outcome trail
- bounded proposal ranking bias in workspace proposal priority refresh
- bounded strategy-goal weighting adjustments for strategy types tied to workspace proposal families

## Remaining Out Of Scope

- automatic proposal suppression beyond bounded negative bias
- commitment expectation shaping from arbitration outcomes
- direct TOD transport automation for arbitration-outcome publishing

## Persistence Surface

- `WorkspaceProposalArbitrationOutcome`

Representative stored fields:

- `proposal_id`
- `proposal_type`
- `related_zone`
- `arbitration_decision`
- `arbitration_posture`
- `trust_chain_status`
- `downstream_execution_outcome`
- `outcome_score`
- `confidence`
- `conflict_context_json`
- `commitment_state_json`

## API Surface

- `POST /workspace/proposals/arbitration-outcomes`
- `GET /workspace/proposals/arbitration-outcomes`
- `GET /workspace/proposals/arbitration-learning`

## Behavioral Contract

If a proposal type repeatedly loses arbitration in a zone, MIM should apply a small negative priority bias when ranking later proposals of the same type in the same zone.

If a proposal type repeatedly wins or merges successfully, MIM should apply a small positive priority bias.

The bias must remain inspectable in proposal payloads and learning summaries.

## Validation

The first slice is complete when:

- arbitration outcomes can be recorded
- learned bias can be inspected by proposal type
- proposal priority ordering changes in response to repeated arbitration wins/losses
- strategy-goal ranking factors expose arbitration learning influence when related proposal families accumulate outcomes
