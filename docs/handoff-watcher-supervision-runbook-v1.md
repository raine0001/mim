# Handoff Watcher Supervision Runbook v1

Date: 2026-04-13
Status: active runbook
Scope: operator inspection and manual recovery for handoff watcher supervision v1.

This runbook is bounded by [docs/handoff-watcher-supervision-contract-v1.md](docs/handoff-watcher-supervision-contract-v1.md).

## Purpose

Use this runbook to:

1. Inspect the two authoritative supervision artifacts.
2. Interpret watcher and recovery state as healthy, degraded, or failed.
3. Verify the authoritative user-level services are running.
4. Apply the current manual recovery steps after `recovery_failed`.

This runbook does not add or authorize new automation.

## Authoritative Artifacts

Watcher heartbeat:

- `handoff/status/HANDOFF_WATCHER.latest.json`

Recovery state:

- `handoff/status/HANDOFF_WATCHER_RECOVERY.latest.json`

These are the only authoritative artifacts for supervision v1.

## Authoritative Deployment Mode

The authoritative deployment mode in v1 is user-level systemd.

Authoritative units:

- `mim-handoff-watcher.service`
- `mim-handoff-watcher-supervisor.service`

Authoritative installer:

- `scripts/install_handoff_watcher_user_units.sh`

## Quick Inspection Commands

Inspect current watcher heartbeat:

```bash
cat /home/testpilot/mim/handoff/status/HANDOFF_WATCHER.latest.json
```

Inspect current recovery state:

```bash
cat /home/testpilot/mim/handoff/status/HANDOFF_WATCHER_RECOVERY.latest.json
```

Check authoritative user-level service state:

```bash
systemctl --user --no-pager --full status mim-handoff-watcher.service mim-handoff-watcher-supervisor.service
```

Check whether both authoritative units are active:

```bash
systemctl --user is-active mim-handoff-watcher.service mim-handoff-watcher-supervisor.service
```

## How To Read Healthy State

Healthy supervision usually looks like this:

Watcher heartbeat:

- `lifecycle_state = "polling"` or `"completed"`
- `stale = false`
- `updated_at` is recent relative to `stale_after_seconds`

Recovery state:

- `status = "healthy"`
- `guard_result.status = "ok"`
- `recommended_next_action = "none"`

Service state:

- both user units report `active (running)`

## How To Read Degraded State

Degraded supervision includes any of the following:

Watcher heartbeat:

- missing `HANDOFF_WATCHER.latest.json`
- `lifecycle_state = "stale"`
- `stale = true`
- freshness fields missing or invalid

Recovery state:

- `status = "cooldown_active"`
- `status = "blocked"`
- `guard_result.status = "missing"`
- `guard_result.status = "blocked"`
- `guard_result.status = "stale"`

Service state:

- one or both authoritative user units are inactive

In degraded state, the supervisor may still be actively trying to recover or waiting for cooldown to expire.

## How To Read Failed State

Failed supervision is recorded as:

- `HANDOFF_WATCHER_RECOVERY.latest.json`
  - `status = "recovery_failed"`

This means the supervisor attempted `start` or `restart`, but post-recovery validation did not return watcher health `ok`.

Typical indicators to inspect in the recovery artifact:

- `service_action.action`
- `service_action.returncode`
- `service_action.stdout`
- `service_action.stderr`
- `post_recovery_guard.status`
- `post_recovery_guard.reason`
- `restart_attempt_count`

## Graceful-Failure Helper Outputs

The plain-text helper:

- `scripts/print_handoff_watcher_supervision_summary.py`

remains advisory only. It does not restart anything, change artifacts, or alter supervision behavior.

When authoritative artifacts are missing or malformed, the helper must still print a concise bounded summary and mark manual action as needed.

### Missing Watcher Artifact

If `HANDOFF_WATCHER.latest.json` is missing, the helper prints:

```text
Watcher state: missing
Recovery state: healthy
Manual action needed: yes
```

### Missing Recovery Artifact

If `HANDOFF_WATCHER_RECOVERY.latest.json` is missing, the helper prints:

```text
Watcher state: polling
Recovery state: missing
Manual action needed: yes
```

### Malformed Watcher Artifact

If `HANDOFF_WATCHER.latest.json` exists but is malformed, the helper prints:

```text
Watcher state: malformed
Recovery state: healthy
Manual action needed: yes
```

### Malformed Recovery Artifact

If `HANDOFF_WATCHER_RECOVERY.latest.json` exists but is malformed, the helper prints:

```text
Watcher state: polling
Recovery state: malformed
Manual action needed: yes
```

In all four cases the helper output is intentionally bounded to local operator guidance only. It does not escalate, retry, or repair anything.

## Minimal Operator Verification Sequence

Use this sequence before taking manual action:

1. Inspect `HANDOFF_WATCHER.latest.json`.
2. Inspect `HANDOFF_WATCHER_RECOVERY.latest.json`.
3. Check `systemctl --user` status for both authoritative units.
4. Confirm whether the watcher heartbeat `updated_at` is still moving.
5. Confirm whether recovery is currently suppressed only by cooldown.

If the heartbeat is moving and recovery state is `healthy`, no manual action is required.

## Manual Steps After `recovery_failed`

Apply only these bounded manual steps in v1:

1. Inspect the recovery artifact for the last attempted systemctl action and stderr/stdout.
2. Check unit state directly:

```bash
systemctl --user --no-pager --full status mim-handoff-watcher.service
systemctl --user --no-pager --full status mim-handoff-watcher-supervisor.service
```

3. If the watcher unit is inactive or failed, restart the authoritative units manually:

```bash
systemctl --user restart mim-handoff-watcher.service mim-handoff-watcher-supervisor.service
```

4. Re-check the two authoritative artifacts:

```bash
cat /home/testpilot/mim/handoff/status/HANDOFF_WATCHER.latest.json
cat /home/testpilot/mim/handoff/status/HANDOFF_WATCHER_RECOVERY.latest.json
```

5. Confirm that:

- watcher `updated_at` is recent
- watcher `stale = false`
- recovery `status = "healthy"`

## When Manual Recovery Is Considered Successful

Manual recovery is successful only when all of the following are true:

1. `mim-handoff-watcher.service` is active.
2. `mim-handoff-watcher-supervisor.service` is active.
3. `HANDOFF_WATCHER.latest.json` exists and shows a fresh `updated_at`.
4. `HANDOFF_WATCHER.latest.json` has `stale = false`.
5. `HANDOFF_WATCHER_RECOVERY.latest.json` reports:
   - `status = "healthy"`
   - `guard_result.status = "ok"`

## What Not To Do

Do not treat these as part of the v1 runbook:

- do not switch to system-level units as an ad hoc recovery step
- do not add new supervisor environment variables during recovery unless separately reviewed
- do not add a second watcher process manually
- do not broaden recovery into general host supervision
- do not invent new recovery actions beyond the current user-level service restart path

## Escalation Boundary

If manual restart of the authoritative user-level units does not return the artifacts to healthy state, treat the issue as outside the scope of automated watcher supervision v1. At that point the next action is code or deployment debugging, not repeated unbounded restart attempts.