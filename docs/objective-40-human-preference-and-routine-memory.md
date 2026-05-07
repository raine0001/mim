# Objective 40 — Human Preference and Routine Memory

Date: 2026-03-11

## Goal

Extend workspace intelligence with operator preference memory so MIM can adapt policy behavior to how the operator repeatedly chooses to run the system.

## Scope Delivered

### Task A — Preference Model

Added persistent `UserPreference` records with:

- `user_id`
- `preference_type`
- `value`
- `confidence`
- `source`
- `last_updated`

### Task B — Preference Lookup API

Added simple preference API:

- `GET /preferences`
- `GET /preferences/{preference_type}`
- `POST /preferences`

### Task C — Policy Integration

Integrated preferences into existing policy behavior:

- Objective 39 proposal priority scoring now reads preference context:
  - preferred scan zones
  - auto-exec tolerance
- Target confirmation behavior now reads preferred confirmation threshold and applies safe-threshold adjustment when `auto_exec_safe_tasks=true`.
- Proposal scheduler and proposal decision responses now include notification messages shaped by `notification_verbosity`.

### Task D — Learning Signals

Added first-step behavioral adaptation:

- Learning signals update preference confidence/value on repeated:
  - proposal accept
  - proposal reject
  - operator approve/reject execution
  - autonomy override
- Derived preferences updated from behavior trend:
  - `action_approval_bias`
  - `auto_exec_tolerance`
  - `auto_exec_safe_tasks`

## Safety Note

No direct autonomy bypass was introduced. Objective 40 only augments policy inputs and preference memory with operator-visible controls.
