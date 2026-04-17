# Objective 29 — Directed Workspace Targeting

## Summary

Objective 29 adds a non-actuating directed targeting layer for workspace objects. The system resolves a requested target label into object-memory candidates, applies confidence and safety policy, and optionally creates operator-facing proposals.

## Added API

- `POST /workspace/targets/resolve`
  - Request:
    - `target_label` (required)
    - `preferred_zone` (optional)
    - `source` (optional)
    - `unsafe_zones` (optional)
    - `create_proposal` (optional)
  - Response includes:
    - `target_resolution_id`
    - `match_outcome` (`exact_match`, `likely_match`, `ambiguous_candidates`, `no_match`)
    - `policy_outcome` (`target_confirmed`, `target_requires_confirmation`, `target_not_found`, `target_stale_reobserve`, `target_blocked_unsafe_zone`)
    - `status`, `confidence`, candidate/object references, and suggested actions.

- `GET /workspace/targets/{target_resolution_id}`
  - Returns persisted target resolution details.

- `POST /workspace/targets/{target_resolution_id}/confirm`
  - Confirms a pending resolution and generates a follow-on proposal.

## Policy Behavior

- `target_confirmed`
  - Exact, active high-confidence match.
- `target_requires_confirmation`
  - Ambiguous/likely match requiring operator confirmation.
- `target_not_found`
  - No meaningful match.
- `target_stale_reobserve`
  - Candidate exists but is stale/missing; suggests re-observation.
- `target_blocked_unsafe_zone`
  - Candidate is in a caller-declared unsafe zone; blocks progression.

## Safety Constraints

- No direct actuation occurs from target resolution.
- Proposal creation is optional and remains operator-mediated.
- Confirmation endpoint updates targeting state and proposals only.

## Data Model

- Added `WorkspaceTargetResolution`:
  - Stores request inputs, match/policy outcomes, status, confidence, candidate IDs, suggested actions, source, and metadata.

## Verification

- Integration test coverage in `tests/integration/test_objective29_directed_targeting.py` validates:
  - exact match confirmation path,
  - ambiguous candidate requiring confirmation,
  - stale/missing re-observe policy,
  - unsafe-zone blocking,
  - no-match handling,
  - confirm endpoint behavior.
