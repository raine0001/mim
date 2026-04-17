# Objective 76 — TOD Catch-up and Reporting

Date: 2026-03-13
Status: completed
Depends On: Objective 75

## Summary

Objective 76 establishes deterministic, continuous tracking of TOD catch-up relative to MIM and publishes a compact status signal suitable for unattended operation.

## Catch-up Definition

TOD is considered **caught up** when all conditions hold:

- `compatible=true`
- `objective_alignment.status` in `{aligned, in_sync}`
- `objective_alignment.tod_current_objective == objective_alignment.mim_objective_active`
- `mim_refresh.failure_reason` is empty
- integration status freshness is within SLO (`generated_at` age <= configured threshold)
- conditions persist for the configured consecutive pass window

## Deliverables

- `runtime/logs/tod_catchup_status.latest.json`
- `runtime/logs/tod_catchup_status.latest.md`
- `runtime/logs/tod_catchup_status.jsonl`
- `runtime/shared/TOD_CATCHUP_GATE.latest.json`
- user service: `mim-watch-tod-catchup-status.service`

## Task List

| Task | Description | Status |
|---:|---|---|
| 1 | Define catch-up criteria and thresholds | completed |
| 2 | Implement watcher that computes catch-up from integration artifacts | completed |
| 3 | Persist latest JSON + markdown + event stream | completed |
| 4 | Add user service for unattended execution | completed |
| 5 | Verify service health and artifact output | completed |
| 6 | Add promotion-ready catch-up gate signal | completed |

## Notes

- This objective does not replace Objective 75 loop execution; it adds explicit TOD parity observability and reporting.
- Thresholds are runtime-configurable via service environment variables.
