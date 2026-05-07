# Handoff Watcher Supervision Contract v1

## Status

This document defines the implemented operational boundary for handoff watcher supervision v1.

It documents the current watcher supervision layer only. It does not authorize additional restart behaviors, broader supervision architecture, UI work, or new automation beyond what is already implemented.

## 1. Supervised Component

The supervised component is the local handoff watcher process started by:

- `scripts/watch_handoff_inbox.py`

When deployed under service management, that process is hosted by:

- `mim-handoff-watcher.service`

The supervising component is:

- `scripts/watch_handoff_watcher_supervisor.py`
- `mim-handoff-watcher-supervisor.service`

The supervisor is limited to observing watcher health, deciding whether the current implementation qualifies for restart, attempting that restart through `systemctl`, and writing recovery state artifacts.

## 2. Source Of Truth For Health

The source of truth for watcher health is the watcher heartbeat artifact:

- `handoff/status/HANDOFF_WATCHER.latest.json`

That artifact is written by `scripts/watch_handoff_inbox.py` and evaluated by:

- `scripts/check_handoff_watcher_status.py`
- `scripts/watch_handoff_watcher_supervisor.py`

No other artifact, log file, or UI surface is authoritative for watcher health in v1.

## 3. Exact Stale Criteria

The watcher is considered stale only when all of the following are true:

1. `HANDOFF_WATCHER.latest.json` exists.
2. `lifecycle_state` is not `completed`.
3. `updated_at` is parseable.
4. `stale_after_seconds` is greater than `0`.
5. `age_seconds > stale_after_seconds`, where:

   - `age_seconds = now - updated_at`

When this condition is met, `scripts/check_handoff_watcher_status.py` rewrites the watcher heartbeat artifact with:

- `lifecycle_state = "stale"`
- `stale = true`
- `stale_reason = "heartbeat_expired"`
- `recommended_next_action = "restart_local_handoff_watcher"`

Additional status evaluation rules in v1:

- missing heartbeat artifact -> `status = "missing"`
- heartbeat freshness fields missing -> `status = "blocked"`
- heartbeat fresh -> `status = "ok"`
- watcher completed cleanly -> `status = "ok"`

## 4. Exact Restart Criteria

The supervisor attempts restart only when all of the following are true:

1. `scripts/watch_handoff_watcher_supervisor.py` evaluates watcher health through `evaluate_watcher_status(...)`.
2. The resulting `recommended_next_action` is exactly:

   - `restart_local_handoff_watcher`

3. Restart cooldown is not active.

If those conditions are satisfied, the supervisor issues a service action through `systemctl` against the configured watcher service name.

The exact service action is:

- `restart` if the watcher service is already active
- `start` if the watcher service is not active

No other recovery action is allowed in v1.

## 5. Cooldown Behavior

Cooldown is enforced by the supervisor through:

- `MIM_HANDOFF_RECOVERY_COOLDOWN_SECONDS`

Current default:

- `60`

Cooldown is measured from the most recent recorded recovery attempt timestamp stored in:

- `last_recovery_started_at`

inside:

- `handoff/status/HANDOFF_WATCHER_RECOVERY.latest.json`

If the elapsed time since `last_recovery_started_at` is less than the cooldown value, the supervisor does not restart the watcher. Instead it writes:

- `status = "cooldown_active"`
- `cooldown_remaining_seconds = ...`

Cooldown suppresses repeated restart attempts but does not change the underlying watcher health status artifact.

## 6. Retry And Restart Limits

v1 does not implement a hard maximum restart count.

The only implemented restart limiting mechanism is cooldown-based suppression between attempts.

v1 does record cumulative restart attempts in:

- `restart_attempt_count`

but that field is observational only in this version. It does not enforce a stop condition.

## 7. Recovery State Written And Location

The supervisor writes recovery state to:

- `handoff/status/HANDOFF_WATCHER_RECOVERY.latest.json`

That artifact includes current implementation fields such as:

- `artifact_type`
- `updated_at`
- `handoff_root`
- `watcher_service_name`
- `watcher_service_scope`
- `cooldown_seconds`
- `guard_result`
- `restart_attempt_count`
- `last_recovery_started_at`
- `last_recovery_status`
- `service_action`
- `post_recovery_guard`
- `last_recovery_at`
- `status`
- `reason`
- `recommended_next_action`

The watcher heartbeat artifact remains separate and continues to live at:

- `handoff/status/HANDOFF_WATCHER.latest.json`

## 8. Healthy Vs Degraded Vs Failed

### Healthy

Watcher supervision is healthy when the supervisor writes recovery state with:

- `status = "healthy"`

This corresponds to a watcher guard result of:

- `status = "ok"`

Typical healthy reasons are:

- `watcher_status_fresh`
- `watcher_completed_cleanly`

### Degraded

Watcher supervision is degraded when the watcher or recovery layer is not healthy but automated restart has not ended in verified recovery yet.

In v1, degraded conditions include:

- watcher guard result `missing`
- watcher guard result `blocked`
- watcher guard result `stale`
- recovery artifact `status = "cooldown_active"`
- recovery artifact `status = "blocked"`

### Failed

Watcher supervision is failed when the supervisor attempts recovery and does not verify successful health afterward.

In v1 that is recorded as:

- `status = "recovery_failed"`

This means a restart or start was attempted, but post-recovery validation did not return watcher health `ok`.

## 9. Authoritative Deployment Mode

The authoritative deployment mode for v1 is:

- user-level units

Authoritative units:

- `deploy/systemd-user/mim-handoff-watcher.service`
- `deploy/systemd-user/mim-handoff-watcher-supervisor.service`

Authoritative installer:

- `scripts/install_handoff_watcher_user_units.sh`

System-level units exist only as non-authoritative parity artifacts in v1:

- `deploy/systemd/mim-handoff-watcher.service`
- `deploy/systemd/mim-handoff-watcher-supervisor.service`

They are not the default or authoritative deployment path for this slice.

## 10. What Is Intentionally Not Automated

The following are intentionally out of scope for v1 and are not automated:

- no UI or operator dashboard for watcher supervision
- no generalized supervision framework for unrelated services
- no multi-step recovery workflow beyond `start` or `restart`
- no escalation channel beyond artifact state
- no backoff strategy beyond fixed cooldown
- no hard restart-attempt cap
- no failover to alternate watcher implementations
- no queue draining, replay, or batch reconciliation logic
- no automatic promotion from user-level units to system-level units
- no automatic installation of system-level units
- no health decision source other than `HANDOFF_WATCHER.latest.json`

## Boundary Summary

Handoff watcher supervision v1 is a narrow operational layer with one supervised watcher, one authoritative health artifact, one supported automated recovery action, one cooldown limiter, and one recovery-state artifact. Any expansion beyond that boundary requires a new explicitly reviewed slice.