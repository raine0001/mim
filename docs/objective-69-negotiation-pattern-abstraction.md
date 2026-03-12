# Objective 69 — Negotiation Pattern Abstraction

Date: 2026-03-12
Status: implemented_pending_promotion
Schema Version: 2026-03-12-62

## Summary

Objective 69 adds a persistent abstraction layer over negotiation memory so repeated negotiation outcomes become inspectable collaboration concepts, not only per-pattern preference counters.

The implementation is rule-based and bounded:

- Patterns are extracted from repeated negotiation evidence.
- Influence is context-scoped and freshness-aware.
- Patterns suggest defaults and policy shaping only; they do not hard-lock decisions.
- Pattern state remains inspectable and acknowledgeable.

## Scope Delivered

### Task A — Negotiation Pattern Model

Persistent model: `WorkspaceCollaborationPattern` (`workspace_collaboration_patterns`) with:

- `pattern_id` (`id`)
- `pattern_type`
- `context_signature`
- `evidence_count`
- `confidence`
- `dominant_outcome`
- `affected_domains_json`
- `status`
- plus explainability/influence payloads and acknowledgment metadata.

### Task B — Pattern Extraction

Pattern extraction runs during negotiation signal recording and derives abstraction from:

- selected negotiation options
- context signature (task/risk/shared/operator/urgency/environment)
- human-aware signals
- communication urgency
- downstream outcome quality from interaction resolution statuses

Rule-based pattern typing includes:

- `shared_workspace_deferential_preference`
- `urgent_communication_override`
- `occupied_zone_physical_postponement`
- `contextual_collaboration_preference`

### Task C — Pattern Influence

When context signature matches and pattern is fresh+consolidated:

- influences negotiation default suggestion (`default_safe_path`)
- influences collaboration mode resolution
- shapes orchestration policy (defer/surface concise/confirmation)
- shapes collaboration question urgency/priority
- contributes autonomy suppression hints via policy shaping

### Task D — Inspectability

Added endpoints:

- `GET /collaboration/patterns`
- `GET /collaboration/patterns/{pattern_id}`
- `POST /collaboration/patterns/{pattern_id}/acknowledge`

### Task E — Bounded Abstraction

V1 remains bounded and explainable:

- evidence threshold + confidence threshold required for strong influence
- stale patterns are suppressed via decay/freshness checks
- context mismatch prevents overreach
- patterns influence defaults/policy shaping but do not silently force final decisions

## Focused Gate Expectations Covered

Objective 69 focused validation checks:

- repeated similar negotiations create a pattern abstraction
- evidence and confidence growth are reflected in pattern object
- fresh same-context pattern influences default suggestion
- stale or mismatched context pattern does not overreach
- pattern remains explainable and inspectable

## Changed Components

- `core/models.py`
- `core/orchestration_service.py`
- `core/routers/orchestration.py`
- `core/schemas.py`
- `core/manifest.py`
- `tests/integration/test_objective69_negotiation_pattern_abstraction.py`
- `docs/objective-index.md`
