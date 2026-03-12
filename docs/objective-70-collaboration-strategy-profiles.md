# Objective 70 — Collaboration Strategy Profiles

Date: 2026-03-12
Status: promoted_verified
Schema Version: 2026-03-12-63

## Summary

Objective 70 introduces collaboration strategy profiles that synthesize lower-level negotiation patterns into higher-order, inspectable collaboration style models.

The implementation is rule-based and bounded:

- Profiles are synthesized from negotiation patterns plus contextual behavioral evidence.
- Profile influence is context-scoped, freshness-aware, and confidence-thresholded.
- Profiles bias defaults and policy shaping only; they do not hard-lock outcomes.
- Profile state is inspectable and recomputable.

## Scope Delivered

### Task A — Collaboration Profile Model

Persistent model: `WorkspaceCollaborationProfile` (`workspace_collaboration_profiles`) with:

- `profile_id` (`id`)
- `profile_type`
- `context_scope`
- `dominant_collaboration_mode`
- `supporting_pattern_ids_json`
- `confidence`
- `freshness`
- `status`
- plus explainability/influence payloads and evidence metadata.

### Task B — Profile Synthesis

Profile synthesis combines:

- latest Objective 69 collaboration pattern for matching context scope
- operator collaboration mode preference (override signal)
- urgency outcomes from recent negotiations
- shared workspace behavior ratios
- follow-through quality from negotiation resolution outcomes

### Task C — Profile Influence

When profile context matches and profile is fresh+consolidated:

- influences collaboration mode selection
- shapes deferential/suppression policy flags
- influences negotiation default preference selection
- affects collaboration question priority framing
- contributes explainable orchestration style shaping

### Task D — Inspectability

Added endpoints:

- `GET /collaboration/profiles`
- `GET /collaboration/profiles/{profile_id}`
- `POST /collaboration/profiles/recompute`

### Task E — Bounded Behavior

V1 remains bounded and overridable:

- profile influence requires consolidation + confidence threshold
- stale profiles are suppressed
- context mismatch prevents leakage across scopes
- explicit operator/requested mode still takes precedence
- hard safety/constraint branches remain authoritative

## Focused Gate Expectations Covered

Objective 70 focused validation checks:

- repeated negotiation patterns create a collaboration strategy profile
- profile confidence/evidence update with additional observations
- profile influences future collaboration mode selection in matching scope
- stale or conflicting/mismatched scope influence is bounded
- inspectability exposes profile reasoning and supporting evidence

## Changed Components

- `core/models.py`
- `core/orchestration_service.py`
- `core/routers/orchestration.py`
- `core/schemas.py`
- `core/manifest.py`
- `tests/integration/test_objective70_collaboration_strategy_profiles.py`
- `docs/objective-index.md`
