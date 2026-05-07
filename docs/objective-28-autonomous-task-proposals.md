# Objective 28 — Autonomous Task Proposals from Workspace State

Date: 2026-03-10

## Goal

Generate actionable proposals from workspace state changes so MIM can suggest follow-up actions (rescan, verify movement, confirm target readiness) under operator control.

## Implemented Scope

- Added `workspace_proposals` persistence model for generated proposals.
- Added proposal generation in `workspace_scan` feedback flow from object/memory state.
- Added dedupe window to avoid repeated proposal spam.
- Added proposal review endpoints:
  - `GET /workspace/proposals`
  - `GET /workspace/proposals/{proposal_id}`
  - `POST /workspace/proposals/{proposal_id}/accept`
  - `POST /workspace/proposals/{proposal_id}/reject`
- Accept action creates a queued task for follow-up execution planning.
- Proposal generation evidence is attached to execution feedback (`workspace_proposal_ids`).

## Proposal Triggers

- `rescan_zone`
  - object status is `missing`
- `verify_moved_object`
  - object status is `uncertain` with movement marker
- `confirm_target_ready`
  - object status `active` with strong confidence

## Safety and Control

- Proposals are non-actuating suggestions only.
- Operator must accept/reject before they become actionable tasks.
- Journal entries are written for proposal generation and operator decisions.
