# Objective 68 — Negotiation Memory Decay and Contextualization

## Summary

Objective 68 makes negotiation memory adaptive by adding freshness decay and context-sensitive preference application, preventing stale or environment-mismatched decisions from becoming persistent dogma.

## Scope Delivered

- Added confidence/evidence decay based on memory age.
- Added stale pattern suppression for old negotiation memory.
- Added context-sensitive negotiation memory keying by environment profile.
- Added inspectability fields for decay, freshness, effective-vs-raw evidence/confidence, and context-match signal.
- Preserved Objective 67 revision behavior while ensuring memory guidance remains non-dogmatic.

## Behavioral Details

1. **Decay and staleness**
   - Negotiation memory entries now compute effective confidence/evidence using a time-based decay factor.
   - Entries beyond stale threshold are treated as `learning` for application decisions.

2. **Contextualized memory selection**
   - Negotiation pattern key now includes `environment_profile` (`env:<profile>`), so preferences consolidate per context.
   - Same user preference can differ safely across environments.

3. **Safe application behavior**
   - Consolidated memory can still shape default safe path and collaboration behavior.
   - Stale or weak memory is prevented from overriding fresh context.

4. **Inspectability expansion**
   - `GET /collaboration/preferences` now surfaces:
     - `raw_confidence` vs `confidence`
     - `raw_evidence_count` vs `evidence_count`
     - `freshness`, `decay_applied`, `decay_factor`, `age_days`
     - `context_match_score`
     - objective thresholds for consolidation and decay.

## Files Updated

- `core/orchestration_service.py`
- `core/preferences.py`
- `core/manifest.py`
- `tests/integration/test_objective65_human_aware_collaboration_negotiation.py`
- `tests/integration/test_objective66_negotiated_task_resolution_follow_through.py`
- `tests/integration/test_objective67_negotiation_memory_preference_consolidation.py`
- `tests/integration/test_objective68_negotiation_memory_decay_and_contextualization.py`
- `docs/objective-68-negotiation-memory-decay-and-contextualization.md`
