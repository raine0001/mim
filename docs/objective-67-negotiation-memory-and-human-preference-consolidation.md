# Objective 67 — Negotiation Memory and Human Preference Consolidation

## Summary

Objective 67 converts repeated negotiation outcomes into durable, inspectable preference memory that influences collaboration defaults while remaining safely revisable as user choices evolve.

## Scope Delivered

- Added durable negotiation memory preference store (`collaboration_negotiation_memory`).
- Added consolidation logic from repeated outcomes into stable preference signals.
- Added preference influence on collaboration mode, orchestration shaping, question prompting, and fallback default selection.
- Added inspectability endpoint for learned negotiation preferences and evidence.
- Added safe revision behavior so diverging future responses can weaken or change prior consolidated preferences.

## Behavioral Details

1. **Negotiation memory model**
   - Stores per-context pattern entries with:
     - `option_counts`
     - `evidence_count`
     - `dominant_option_id`
     - `confidence`
     - `state` (`learning` or `consolidated`)
     - `source_interactions` (actor, resolution status, selected option, timestamp, negotiation id)

2. **Consolidation logic**
   - Patterns consolidate only after minimum evidence and confidence thresholds.
   - Low evidence remains in `learning` state and does not overfit defaults.

3. **Preference influence**
   - Consolidated memory can influence:
     - collaboration mode resolution for auto mode
     - collaboration policy shaping (`ask_question`, concise updates, defer behavior)
     - fallback default path choice for negotiations
   - negotiation default shaping without forcing auto-resolution of memory-only preferences

4. **Safe decay and revision**
   - Divergent future outcomes can lower dominance ratio/confidence.
   - Previously consolidated patterns can return to `learning` when confidence drops below revision floor.

5. **Inspectability**
   - New endpoint: `GET /collaboration/preferences`
   - Returns thresholds, per-pattern preference state, confidence, evidence, option counts, and source interactions.

## Files Updated

- `core/orchestration_service.py`
- `core/routers/orchestration.py`
- `core/preferences.py`
- `core/manifest.py`
- `docs/objective-index.md`
- `docs/objective-67-negotiation-memory-and-human-preference-consolidation.md`
