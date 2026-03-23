# Objective 75 — MIM→TOD Interface Hardening (First Project)

Date: 2026-03-12
Status: completed
Target Schema Version: 2026-03-12-68
Target Release Tag: objective-75

## Summary

Objective 75 defines and hardens the MIM→TOD shared interface contract as the first post-Objective-74 project. The immediate goal is to convert the current transport-success/objective-mismatch state into deterministic contract compatibility and objective alignment.

Closure update: 2026-03-23

- MIM shared exports, manifest truth, and handshake truth are aligned on objective `75`, schema `2026-03-12-68`, and release `objective-75`.
- TOD canonical status now publishes manifest and handshake refresh evidence instead of alignment-only partial truth.
- The canonical catch-up writer is aligned with the refreshed status publication path and now reports `gate_pass=true` on the real shared-state evidence.
- MIM-side recoupling now passes under the stricter evidence rules, so the interface is closed as a verified baseline rather than a partial transport fix.

## Why This Is First

- Shared transport path is healthy (`/home/testpilot/mim/runtime/shared`).
- MIM exports and handshake packet are available and current.
- Remaining blocker is interface/consumer alignment (contract compatibility and objective synchronization), not infrastructure.

## Scope

In scope:

- Contract normalization for shared files and compatibility rules.
- Producer-side conformance checks on MIM artifacts.
- Consumer-side acceptance criteria for TOD sync and status publication.
- Regression gating for interface compatibility before feature expansion.

Out of scope:

- New autonomy features unrelated to shared-state interface.
- Hardware or actuator behavior changes.
- Expansion of state-bus domain capabilities beyond interface reliability.

## Interface Contract v1.1 (Proposed)

### Required Shared Files

- `MIM_CONTEXT_EXPORT.latest.json`
- `MIM_CONTEXT_EXPORT.latest.yaml`

### Optional Shared Files

- `MIM_MANIFEST.latest.json`
- `MIM_TOD_HANDSHAKE_PACKET.latest.json`
- `MIM_TOD_ALIGNMENT_REQUEST.latest.json`

### Required Compatibility Fields

- `mim_schema`
- `tod_contract`
- `compatible`
- `objective_alignment.status`
- `objective_alignment.tod_current_objective`
- `objective_alignment.mim_objective_active`

## Execution Checklist (Tasks + Owners)

### Phase 1 — Contract Definition and Freeze

1. Publish contract addendum for required vs optional file semantics and compatibility policy.
   - Owner: MIM platform lead
   - Output: `docs/tod-mim-bridge.md` update and version note
2. Define objective alignment rule: TOD objective must equal MIM objective active for compatible=true.
   - Owner: MIM orchestration lead + TOD integration lead
   - Output: rule entry in shared contract docs
3. Freeze interface version identifier for this cycle.
   - Owner: MIM release owner
   - Output: manifest capability/contract version update plan

### Phase 2 — MIM Producer Conformance

1. Add automated producer conformance check for required files and key fields.
   - Owner: MIM backend lead
   - Output: integration test module for shared export integrity
2. Add validation that handshake packet truth matches manifest and context export objective fields.
   - Owner: MIM backend lead
   - Output: assertion coverage in integration tests
3. Ensure alignment request packet generation is deterministic and timestamped.
   - Owner: MIM backend lead
   - Output: script step in export/refresh workflow

### Phase 3 — TOD Consumer Conformance

1. Run TOD sync against shared root and publish fresh integration status artifact.
   - Owner: TOD integration lead
   - Output: new `TOD_INTEGRATION_STATUS.latest.json`
2. Confirm manifest and handshake pulls succeed and are surfaced in canonical status publication when present.
   - Owner: TOD integration lead
   - Output: status fields `copied_manifest=true`, non-empty `source_manifest` / `source_handshake_packet`, and populated handshake schema/release/objective truth
3. Align TOD current objective with MIM objective active.
   - Owner: TOD product owner
   - Output: objective alignment status `aligned`

### Phase 4 — Promotion Gate

1. Enforce pre-promotion interface gate requiring compatibility and objective alignment.
    - Owner: MIM QA/release lead
    - Output: promotion checklist update
2. Record objective-75 readiness and production promotion evidence.
    - Owner: MIM release owner
    - Output: readiness + prod promotion reports

## Acceptance Tests

### A. Shared Artifact Presence and Freshness

- Verify required files exist in shared path.
- Verify generated timestamps are within freshness SLO.
- Verify MIM objective fields are internally consistent across export/handshake/manifest.

### B. Contract Compatibility

- Verify TOD-published status reports `compatible=true`.
- Verify `failure_reason` is empty for refresh path.
- Verify canonical status shows `copied_manifest=true` and non-empty source paths when shared manifest/handshake artifacts are present.
- Verify canonical `mim_handshake` truth matches the shared handshake packet for objective/schema/release fields.

### C. Objective Alignment

- Verify `objective_alignment.status=aligned`.
- Verify `objective_alignment.tod_current_objective == objective_alignment.mim_objective_active`.
- Verify alignment survives one additional refresh cycle (stability check).

### D. Regression Gate

- Re-run Objective 73 and Objective 74 integration suites against prod base URL.
- Verify no regression in state-bus reaction endpoint and interface session/message/approval routes.

## Definition of Done

Objective 75 is done when all conditions below are true in a fresh TOD status publication:

- `compatible=true`
- `objective_alignment.status=aligned`
- `objective_alignment.tod_current_objective` equals MIM active objective
- refresh `failure_reason` remains empty
- `mim_refresh.copied_manifest=true`
- `mim_refresh.source_manifest` and `mim_refresh.source_handshake_packet` are populated
- `mim_handshake.available=true`
- `mim_handshake.objective_active`, `mim_handshake.schema_version`, and `mim_handshake.release_tag` match the shared MIM handshake truth
- required artifacts are present and parseable
- Objective 73/74 integration suites pass

## Risks and Mitigations

- Risk: Contract drift between MIM and TOD parsing logic.
  - Mitigation: explicit contract version freeze and conformance tests on both sides.
- Risk: Stale artifacts causing false mismatch.
  - Mitigation: freshness SLO checks and timestamp-based validation.
- Risk: Optional artifact ambiguity.
  - Mitigation: document required/optional behavior and enforce when-present pull success.

## Immediate Next Action

Use Objective 75 as the stable interface baseline for Objective 80 planning, with the assumptions that shared truth export is stable, canonical status publication is trustworthy, recoupling logic is proven, and catch-up status no longer fakes success.
