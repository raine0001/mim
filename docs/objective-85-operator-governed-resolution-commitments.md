# Objective 85 - Operator-Governed Resolution Commitments

Date: 2026-03-24
Status: implemented
Depends On: Objective 58, Objective 59, Objective 60, Objective 74, Objective 81, Objective 83, Objective 84
Target Release Tag: objective-85

## Implementation Status

Objective 85 is now implemented across the operator commitment lifecycle and the downstream managed-scope consumers that need to honor it.

Delivered so far:

- durable `WorkspaceOperatorResolutionCommitment` persistence model
- operator commitment create/list/get/revoke/expire endpoints under `/operator/resolution-commitments`
- duplicate suppression for identical active commitments in the same scope/family
- deterministic supersede behavior for conflicting active commitments in the same scope/family
- Objective 84 read-model extension showing:
  - `operator_reasoning.current_recommendation`
  - `operator_reasoning.resolution_commitment`
  - `conversation_context["operator_resolution_summary"]`
  - `runtime_features += ["operator_resolution_commitments"]`
- scoped autonomy-boundary propagation that can lower autonomy for a managed scope when an active commitment requires it
- scoped inquiry propagation that suppresses duplicate stewardship-triggered asks while an active `require_additional_evidence` commitment with `suppress_duplicate_inquiry` is still in force
- scoped execution-truth inquiry suppression using stable managed-scope derivation for `execution_truth_runtime_mismatch`
- scoped strategy scoring propagation through a shared operator-resolution service
- scoped stewardship auto-execution shaping and effect attribution for active commitments
- scoped maintenance auto-execution shaping and effect attribution for active commitments
- shared `core/operator_resolution_service.py` used by operator endpoints, UI, inquiry, autonomy, strategy, stewardship, and maintenance surfaces
- focused Objective 85 integration coverage for persistence, duplicate suppression, read-model visibility, autonomy propagation, execution-truth inquiry suppression, strategy scoring shaping, stewardship shaping, and maintenance shaping
- adjacent regression coverage across Objectives 57, 58, 60, 80, 83, 84, and 85 proving the shared operator reasoning bundle stays scope coherent while downstream consumers honor active commitments

## Validation Notes

The Objective 85 integration suite now uses a repo-native cleanup helper to remove durable `objective85-*` rows from shared operator-reasoning tables before and after each test. This keeps adjacent Objective 84/85 validation meaningful even when the local integration environment points at a persistent Postgres database.

The implementation was validated against:

- `tests.integration.test_objective85_operator_governed_resolution_commitments` (9 tests)
- adjacent regression lane covering:
  - `tests.integration.test_objective57_goal_strategy_engine`
  - `tests.integration.test_objective58_adaptive_autonomy_boundaries`
  - `tests.integration.test_objective60_environment_stewardship_loop`
  - `tests.integration.test_objective60_stewardship_inquiry_followup`
  - `tests.integration.test_objective80_execution_truth_inquiry_hook`
  - `tests.integration.test_objective83_governed_inquiry_resolution_loop`
  - `tests.integration.test_objective84_operator_visible_system_reasoning`
  - `tests.integration.test_objective85_operator_governed_resolution_commitments`

The adjacent lane completed with 23 passing tests against the local validation server.

## Problem Statement

Objective 84 makes system reasoning visible to the operator, but it is still a read-only explanation surface.

MIM can already:

- reason across strategy, governance, inquiry, autonomy, and stewardship state
- expose that reasoning in `/mim/ui/state`
- ask bounded questions through the governed inquiry loop
- accept narrow operator actions for specific execution rows through the operator API

The missing layer is a durable operator resolution object that can shape later behavior for a managed scope.

Without that layer, operator involvement is treated as a transient interruption rather than as a first-class governance input. The system can explain why it is concerned, but it cannot yet persist a bounded operator commitment and apply it coherently across downstream surfaces.

## Goal

Add operator-governed resolution commitments that persist bounded operator decisions and influence downstream behavior for the affected scope until the commitment expires, is superseded, or is explicitly closed.

Objective 85 should let MIM:

- present a bounded recommendation to the operator
- accept operator commitment or override for a managed scope
- persist that resolution with scope, duration, authority, and provenance
- propagate the active commitment into downstream reasoning surfaces
- show in operator-visible reasoning whether that commitment changed behavior and whether it is still active

## Why Objective 85 Fits After Objective 84

Objective 81 added shared execution-truth governance.

Objective 83 turned inquiry into a governed decision loop with cooldown and reuse.

Objective 84 made the current reasoning chain visible to the operator as one bounded read model.

The next gap is not more explanation. The next gap is durable operator commitment.

Objective 85 turns the operator from a passive observer of system reasoning into an explicit participant in the governance loop, while keeping the interaction bounded, auditable, and scoped.

## Existing Surfaces Objective 85 Builds On

Objective 85 should extend current architecture rather than introduce a separate operator workflow stack.

Relevant existing surfaces:

- `core/routers/mim_ui.py`
  - exposes Objective 84 `operator_reasoning` on `/mim/ui/state`
- `core/routers/operator.py`
  - already records execution-scoped operator actions such as approve, reject, retry, resume, cancel, and ignore
- `core/models.py`
  - already contains durable review and approval precedents such as `WorkspaceStrategyGoalReview` and `WorkspaceInterfaceApproval`
- `core/inquiry_service.py`
  - already supports governed suppression, cooldown, and reuse behavior
- `core/autonomy_boundary_service.py`
  - already applies governance inputs and keeps hard ceilings authoritative
- `core/stewardship_service.py`
  - already shapes maintenance follow-up and defers action when operator boundaries apply

Objective 85 should add one durable commitment model that these existing surfaces can consume.

## In Scope

### 1. Resolution Commitment Object

Add a durable operator resolution model for bounded decisions such as:

- `approve_current_path`
- `override_recommendation`
- `defer_action`
- `require_additional_evidence`
- `lower_autonomy_for_scope`
- `elevate_remediation_priority`

The commitment should be narrower than free-form operator chat and broader than a single execution approval.

At minimum the object should persist:

- `managed_scope`
- `decision_type`
- `status` (`active`, `expired`, `superseded`, `revoked`)
- `reason`
- `recommendation_snapshot_json`
- `authority_level`
- `provenance_json`
- `confidence`
- `created_by`
- `expires_at`
- `superseded_by_commitment_id`
- `downstream_effects_json`
- `metadata_json`

Recommended model name:

- `WorkspaceOperatorResolutionCommitment`

Optional audit companion if separation becomes useful:

- `WorkspaceOperatorResolutionCommitmentEvent`

The design should follow the auditability precedent used by `WorkspaceStrategyGoalReview` rather than mutating opaque JSON blobs without history.

### 2. Commitment Propagation

An active commitment should fan out into downstream behavior for the same managed scope.

Objective 85 propagation targets:

- strategy scoring and goal pressure
- stewardship follow-up behavior
- autonomy-boundary posture
- inquiry suppression, reuse, or escalation behavior
- maintenance action shaping

Propagation does not require every consumer to copy the commitment into its own table. It does require every affected consumer to read the active commitment and reflect the applied effect in its reasoning or metadata.

Minimum propagation expectations:

- strategy can raise or lower scoped remediation pressure
- stewardship can defer or narrow maintenance actions
- autonomy can lower the allowed level for the scoped window
- inquiry can suppress duplicate asks while a commitment requiring more evidence is still active
- maintenance can remain bounded when the commitment defers autonomous action

### 3. Scope, Duration, and Authority

Every commitment must be explicitly bounded.

Required fields:

- scope
- duration or expiration
- authority level
- provenance

This lets the system distinguish:

- one-time operator override
- temporary caution mode
- persistent operator preference-like guidance
- evidence-gated hold on autonomous action

Target authority examples:

- `informational`
- `governance_override`
- `temporary_safety_hold`
- `operator_required`

Objective 85 should keep durable commitment authority separate from long-lived preference learning in `UserPreference`. A commitment may later inform preferences, but it is not itself a preference row.

### 4. Inspectability

Objective 84's operator-visible reasoning should be extended so the operator can inspect not just system reasoning, but also the active operator commitment and its effect.

The `/mim/ui/state` contract should grow to show:

- current recommendation
- active operator commitment
- commitment decision type
- commitment reason
- whether the commitment changed downstream behavior
- whether the commitment is still active, expired, or superseded
- when the commitment expires

Recommended additions to the Objective 84 payload family:

- `operator_reasoning.resolution_commitment`
- `operator_reasoning.current_recommendation`
- `conversation_context["operator_resolution_summary"]`
- `runtime_features += ["operator_resolution_commitments"]`

The operator-facing UI should make clear whether later autonomy, inquiry, stewardship, or maintenance behavior is being shaped by an active operator commitment rather than by raw governance inference alone.

### 5. Anti-Thrash Safeguards

Objective 85 should prevent commitment churn and stale operator decisions from dominating the system forever.

Required safeguards:

- duplicate commitment suppression for the same scope and recommendation family
- conflict handling for incompatible active commitments on the same scope
- explicit supersede or revoke behavior
- expiration checks before a commitment is applied downstream
- stale-commitment visibility in operator reasoning
- no repeated operator ask loops while an active commitment already answers the same governance question

Preferred conflict rule:

- one active commitment per `managed_scope + commitment_family`
- later incompatible commitments supersede earlier ones with explicit audit linkage

## Out Of Scope

- free-form operator chat memory
- broad workflow redesign outside the governance layer
- unrelated MIM UI cleanup
- unconstrained autonomy expansion
- bypassing existing safety ceilings or constraint evaluation

## Proposed Surface

### Persistence

Primary implementation anchors are expected to include:

- `core/models.py`
- `core/schemas.py`
- a dedicated service such as `core/operator_resolution_service.py`
- a router surface such as `core/routers/operator_commitments.py` or an extension of `core/routers/operator.py`
- `core/routers/mim_ui.py`

### Suggested API Shape

Recommended endpoints:

- `POST /operator/resolution-commitments`
- `GET /operator/resolution-commitments`
- `GET /operator/resolution-commitments/{commitment_id}`
- `POST /operator/resolution-commitments/{commitment_id}/expire`
- `POST /operator/resolution-commitments/{commitment_id}/revoke`

The create contract should accept a bounded operator decision, not arbitrary free text.

Suggested request fields:

- `actor`
- `managed_scope`
- `decision_type`
- `reason`
- `recommendation_snapshot_json`
- `authority_level`
- `confidence`
- `expires_at` or `duration_seconds`
- `provenance_json`
- `metadata_json`

## Behavioral Contract

Example target path:

1. governance and stewardship produce a scoped recommendation for zone A
2. `/mim/ui/state` shows that recommendation in operator reasoning
3. operator commits `defer_action` with `require_additional_evidence` semantics for zone A
4. commitment persists as an active resolution object
5. autonomy, inquiry, stewardship, and maintenance read that active commitment on later passes
6. downstream outputs explicitly report that the active commitment lowered autonomy, suppressed duplicate inquiry, and deferred auto-maintenance
7. once the commitment expires or is superseded, downstream behavior returns to normal evidence-driven evaluation

Concrete example:

- system signal: retries rising and stewardship degrading in zone A
- operator commitment: defer autonomous maintenance in zone A until additional camera evidence is available
- expected result:
  - autonomy lowered for zone A
  - duplicate inquiry suppressed for a bounded window
  - maintenance stays deferred or scan-only
  - operator reasoning shows the active commitment and its effect

## Acceptance Criteria

Objective 85 is complete when all are true:

1. operator can commit a bounded resolution for a managed scope
2. the resolution is persisted durably and remains queryable after the immediate interaction ends
3. downstream surfaces reflect the active commitment for the same scope
4. `/mim/ui/state` and the System Reasoning panel show the current recommendation, active operator decision, and effect status
5. stale or expired commitments stop applying automatically
6. conflicting commitments are either rejected or superseded explicitly
7. hard safety ceilings and existing constraints still win over operator commitment when required

## Regression Expectations

Objective 85 should add focused integration coverage for:

1. operator commitment persists and is returned by inspectability endpoints
2. active commitment changes downstream autonomy, inquiry, stewardship, and maintenance behavior for the same scope
3. no effect leaks into unrelated scopes
4. duplicate commitment spam is suppressed
5. expired commitments no longer apply
6. conflicting commitments are surfaced and handled deterministically
7. hard safety ceilings still override improper operator commitments
8. operator commitment does not bypass constraints or cause unsafe autonomous execution

Recommended focused lane name:

- `tests.integration.test_objective85_operator_governed_resolution_commitments`

Recommended adjacent lane after implementation:

- Objective 81 governance
- Objective 83 governed inquiry
- Objective 84 operator-visible reasoning
- Objective 85 operator-governed commitments

## Implementation Notes

- Prefer one durable commitment object over multiple feature-specific override tables.
- Reuse existing scope vocabulary (`managed_scope`, `scope`) so propagation stays compatible with Objectives 58, 60, 81, 83, and 84.
- Keep operator commitment separate from `UserPreference`; commitments are durable governance inputs, not generic learned preferences.
- Follow the auditability pattern used by Objective 59 reviews and Objective 74 approvals.
- Extend Objective 84's read model rather than building a second operator-facing explanation surface.

## Exit Criteria

Objective 85 is ready to implement when this document is treated as the bounded contract for:

- persistence model
- API surface
- propagation semantics
- inspectability additions
- anti-thrash rules
- regression proof
