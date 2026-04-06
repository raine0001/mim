# Objective 60: Environment Stewardship Loop

Objective 60 extends MIM from strategy persistence into active environment stewardship: maintaining desired workspace conditions over time with an inspectable corrective loop.

## Scope Implemented

- Stewardship state model:
  - persistent stewardship object with target state, managed scope, maintenance priority, current health, and cycle schedule.
  - linkage to strategy goals, maintenance runs, and autonomy boundary profile.
- Desired-state model:
  - explicit desired-state record persists scope, target conditions, priority, strategy linkage, and provenance.
  - stewardship now links to a concrete desired-state id instead of relying only on inline target JSON.
- Desired-state maintenance:
  - explicit target environment state (freshness, confidence, instability thresholds, proactive monitoring intent).
- Safety throttle:
  - stewardship intervention policy supports max interventions per window, per-scope cooldown, and per-strategy caps.
  - blocked autonomous execution is recorded directly in cycle decision and verification payloads.
- Stewardship cycle engine:
  - evaluates environment degradation signals.
  - compares health vs desired state.
  - selects safe maintenance actions through existing maintenance cycle machinery.
  - verifies post-cycle health and records improvement delta.
- Strategy and memory integration:
  - incorporates recent strategy goals, concept memory, developmental patterns, autonomy boundaries, and operator preferences.
- Desired-state deviation analysis:
  - target state can now be overridden per stewardship cycle instead of relying only on defaults.
  - deviations are evaluated against explicit thresholds for degraded zones, zone uncertainty, drift, and key-object loss.
- Stability scoring:
  - stewardship now computes per-zone, per-object, and system-level `stability_score`, `uncertainty_score`, and `drift_rate` values.
  - key objects are tracked as first-class desired-state elements and surfaced in cycle assessment output.
- Verification and inspectability:
  - stewardship cycles now record pre-vs-post assessment snapshots, verification results, and candidate inquiry triggers when degradation persists.
  - cycle summary and verification now surface `persistent_degradation` and `inquiry_candidate_count` directly so downstream inquiry follow-up is inspectable without digging through raw metadata.
  - stewardship history/read-model output now surfaces `inquiry_candidate_types`, `followup_status`, and whether inquiry follow-up was generated or suppressed.
  - inquiry-triggered bounded follow-up now remains queue-compatible with the workspace proposal system: `trigger_rescan` answer paths create `WorkspaceProposal` rows in `pending` status so bounded rescans enter the same scheduler/accept/reject flow as other workspace proposals.
- Improvement governance integration:
  - stewardship now includes improvement backlog and governance summary evidence alongside strategy, memory, and autonomy evidence.
- Inspectability endpoints:
  - `POST /stewardship/cycle`
  - `GET /stewardship/cycle`
  - `GET /stewardship`
  - `GET /stewardship/{stewardship_id}`
  - `GET /stewardship/history`

## Desired-State Contract

`POST /stewardship/cycle` now accepts an optional `target_environment_state` object. Supported keys include:

- `zone_freshness_seconds`
- `critical_object_confidence`
- `max_degraded_zones`
- `max_zone_uncertainty_score`
- `max_object_uncertainty_score`
- `max_zone_drift_rate`
- `max_system_drift_rate`
- `max_missing_key_objects`
- `key_objects`
- `proactive_drift_monitoring`
- `intervention_policy.max_interventions_per_window`
- `intervention_policy.window_minutes`
- `intervention_policy.scope_cooldown_seconds`
- `intervention_policy.per_strategy_limit`

This makes stewardship explicitly about preserving a desired condition, not only reacting to observed staleness.

## Cycle Inspectability

Each cycle now answers the minimum debugging questions directly in the payload:

- what triggered intervention: `degraded_signals` and `metadata_json.trigger_summary.triggered_by`
- expected vs actual state: `decision.desired_state`, `metadata_json.trigger_summary.actual_state`, and `assessment.pre/post`
- what action was chosen: `selected_actions`
- why it was allowed or blocked: `decision.autonomy_level`, `decision.boundary_allowed`, and `decision.throttle_state`
- what changed: `verification`, `improvement_delta`, and `post_health`

## Why Objective 60 Matters

Objective 60 provides continuity of care. MIM no longer only reacts to degradation; it maintains readiness, reduces uncertainty, and preserves stable conditions with auditable stewardship decisions.

## 2026-03-24 Closure Addendum

Objective 60 was already promoted earlier, but the stewardship inquiry follow-up path still needed one post-promotion correction and closure pass.

Closed follow-up items:

- persistent degradation surfacing remained visible in cycle summary, verification, and history payloads.
- inquiry candidate shaping remained stable for degraded stewardship scopes.
- inquiry-triggered bounded follow-up remained active for stewardship and improvement paths.
- queue-status correction is now explicit: inquiry-triggered workspace rescans use `pending`, not `proposed`, so they can be selected by `GET /workspace/proposals/next` and handled by normal workspace proposal actions.

Closure validation on 2026-03-24 re-proved the stewardship base loop plus the inquiry follow-up path, including the bounded rescan queue insertion contract.
