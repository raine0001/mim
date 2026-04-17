# Objective 34 — Continuous Workspace Monitoring Loop

## Summary

Objective 34 introduces a controlled continuous monitoring loop that maintains live workspace state and generates actionable proposals from observation deltas.

## Scope Delivered

- Monitoring scheduler with policy-driven scan triggers:
  - interval-based scans (`interval_seconds`)
  - freshness-based scans (`freshness_threshold_seconds`)
- Observation delta detection for:
  - new objects
  - moved objects
  - missing objects
  - confidence changes
- Delta-to-proposal generation integrated with proposal workflow:
  - moved object → `monitor_recheck_workspace`
  - missing object → `monitor_search_adjacent_zone`
- Monitoring policy guardrails:
  - `max_scan_rate`
  - `cooldown_seconds`
  - `priority_zones`
- Monitoring endpoints:
  - `GET /workspace/monitoring`
  - `POST /workspace/monitoring/start`
  - `POST /workspace/monitoring/stop`

## Runtime Behavior

- Monitoring state is persisted in `workspace_monitoring_states`.
- On startup, monitoring automatically resumes if `desired_running=true`.
- On shutdown, runtime loop is cleanly stopped.

## Safety and Control

- Scan throttling enforces max scans/minute and cooldown windows.
- Freshness mode limits scanning to stale memory conditions, optionally constrained to priority zones.
- Proposal generation is deduplicated over a rolling window to avoid runaway proposal spam.
