# Objective 86 - Commitment Enforcement and Drift Monitoring Loop

Date: 2026-03-24
Status: implemented
Depends On: Objective 81, Objective 83, Objective 84, Objective 85
Target Release Tag: objective-86

## Goal

Objective 86 turns operator resolution commitments from passive decision records into active constraints that are monitored, scored, surfaced, and corrected over time.

The implementation adds a durable monitoring profile that evaluates whether a commitment is still being honored, whether it is drifting away from current workspace evidence, whether it is blocking too much useful action, and whether MIM should ask for fresh operator guidance.

## Delivered

- durable `WorkspaceOperatorResolutionCommitmentMonitoringProfile` persistence model
- evaluation service in `core/operator_commitment_monitoring_service.py`
- inspectable monitoring endpoints under `/operator/resolution-commitments/{commitment_id}/monitoring...`
- commitment health scoring with compliance and drift metrics
- governance feedback for stable, watch, drifting, violating, expired, and inactive commitment states
- Objective 84 operator reasoning extension with `operator_reasoning.commitment_monitoring`
- commitment-drift inquiry generation through the existing governed inquiry loop
- bounded inquiry answer effect to revoke or expire a drifting commitment without bypassing auditability

## Monitoring Contract

Each monitoring evaluation records:

- the active commitment and managed scope under review
- recent stewardship, maintenance, and inquiry evidence tied to that commitment
- blocked versus allowed auto-execution counts
- potential violation count when downstream behavior contradicts a blocking commitment
- `drift_score`, `compliance_score`, and `health_score`
- `governance_state` and `governance_decision`
- recommended bounded follow-up actions for operator review or inquiry handling

## Inquiry Integration

When monitoring shows meaningful drift or harm, Objective 86 reuses the governed inquiry loop rather than creating a second review workflow. The resulting question can ask whether to:

- keep the commitment active
- revoke the commitment
- expire the commitment and request fresh guidance

The answer effect is bounded to commitment status changes and remains auditable through the normal inquiry-answer state transition.

## Inspectability

Objective 86 extends operator-visible reasoning so `/mim/ui/state` exposes:

- the current active commitment
- the latest monitoring snapshot for that commitment
- the latest recommendation when the commitment is drifting or harming execution

This closes the loop between operator commitments, downstream behavior, and later operator review.