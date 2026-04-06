# Objective 82 - Live Perception Governance Grounding

Objective 82 extends execution-truth governance with bounded live perception grounding.

## What changed

- Camera and microphone adapter events now persist consistent adapter status and request metadata into perception source state.
- Execution-truth governance now reads recent scoped perception sources and derives a perception grounding summary.
- Governance evidence now includes:
  - perception freshness
  - perception confidence
  - sensor noise weight
  - camera grounding weight
  - microphone grounding weight
  - latest camera and microphone snapshots
- Governance classifies perception contribution as one of:
  - `world_drift`
  - `execution_drift`
  - `sensor_noise`
  - `mixed`
  - `insufficient_signal`

## Safety behavior

- Fresh high-confidence camera evidence can corroborate runtime world drift.
- Low-confidence, stale, duplicated, or degraded perception contributes noise weight instead of driving escalation.
- When runtime mismatch clusters exist but perception is currently noisy, governance is limited to `increase_visibility` rather than overreacting into sandbox escalation.

## Inspectability

`POST /execution-truth/governance/evaluate` now returns perception grounding in:

- `trigger_evidence.perception_grounding`
- `trigger_evidence.perception_grounding_classification`
- `reasoning.perception_grounding`

## Regression proof

Integration coverage lives in:

- `tests/integration/test_objective82_live_perception_governance_grounding.py`