# Objective 39 — Policy-Based Autonomous Priority Selection

Date: 2026-03-11

## Goal

Introduce a policy-based proposal priority engine so autonomous scheduling can consistently select the next most important safe workspace proposal.

## Scope Delivered

- Added persistent priority fields to workspace proposals:
  - `priority_score`
  - `priority_reason`
- Added policy-driven scoring using weighted signals:
  - urgency by proposal type
  - confidence
  - safety (risk inversion)
  - operator preference
  - zone importance
  - age factor (time saturation)
- Added priority policy inspectability and configuration:
  - `GET /workspace/proposals/priority-policy`
  - `POST /workspace/proposals/priority-policy`
- Added scheduler selection endpoint:
  - `GET /workspace/proposals/next`
- Added audit visibility:
  - Journal action `workspace_proposal_priority_next` captures selected proposal, policy version, score, and breakdown.

## Behavior Notes

- Pending proposals are rescored when listed, fetched, or considered by the scheduler endpoint.
- The scheduler returns the highest-scoring proposal using tie-breakers on confidence and recency.
- Policy updates are stored with monitoring metadata and can be tuned without changing code.
