# MIM Consumer Plan: TOD Reliability State

## Purpose

Define how MIM will consume TOD reliability state once TOD contract export is stable.

## Trigger Timing (When MIM asks TOD)

- Before high-risk execution chains begin (pre-flight)
- Before promotion-like orchestration transitions
- On explicit operator request for health context
- Periodically during long-running workflows (optional, low frequency)

## Required vs Optional Fields

### Required

- `contract_version`
- `schema_version`
- `capabilities`
- `latest_snapshot.pass_count`
- `latest_snapshot.fail_count`
- `latest_snapshot.retry_count`
- `latest_snapshot.guardrail_blocks`
- `latest_snapshot.engine_stats`

### Optional

- `dashboard.*` rate metrics
- `latest_history_entry`
- `trend_window.*`
- `integration_contract_for_mim.*` hints

## Reliability State Classification in MIM

MIM classifies TOD reliability into four levels:

- `stable`: low failures, low retries, low guardrail blocks
- `warning`: moderate retries or rising guardrail blocks
- `degraded`: high retry/failure rates, clear drift trend
- `critical`: persistent failures or missing required contract fields

## Contract Mismatch Handling

- If `contract_version` mismatches expected:
  - Set MIM reliability state to `critical`
  - Halt automated trust-sensitive steps
  - Require operator acknowledgement / fallback path
- If `schema_version` mismatches but contract matches:
  - Set MIM reliability state to `warning`
  - Continue with conservative behavior

## Failure Behavior

- If TOD reliability payload is unavailable:
  - Degrade to `warning`
  - Continue only for non-critical orchestration
  - Record event in MIM journal

## Implementation Phases

1. **Planning complete** (this doc)
2. Add MIM-side reader interface (no blocking behavior yet)
3. Add classification logic + status surface
4. Add guarded decision hooks for critical orchestration points
5. Add integration tests for stable/warning/degraded/critical paths
