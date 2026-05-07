# Bounded Privileged Actions

This repository now includes a narrow privileged-action path for host recovery work that cannot be completed from the unprivileged MIM process.

## Scope

The current bounded action set is intentionally small:

- `disable-system-tod-liveness-watcher`
- `enable-system-tod-liveness-watcher`
- `status-system-tod-liveness-watcher`

These actions target only the overlapping system unit `mim-watch-tod-liveness.service`.

## Repository Artifacts

- Runner script: `scripts/mim_privileged_action.py`
- Example sudoers allowlist: `deploy/sudoers/mim-privileged-actions.example`
- MIM runtime bridge: `core/privileged_actions.py`

## Recommended Installation

1. Install the runner to a root-owned path:

```bash
sudo install -o root -g root -m 0755 scripts/mim_privileged_action.py /usr/local/bin/mim-privileged-action
```

2. Install the sudoers allowlist:

```bash
sudo install -o root -g root -m 0440 deploy/sudoers/mim-privileged-actions.example /etc/sudoers.d/mim-privileged-actions
sudo visudo -cf /etc/sudoers.d/mim-privileged-actions
```

3. Enable the bridge in MIM's environment:

```bash
MIM_PRIVILEGED_ACTIONS_ENABLED=true
MIM_PRIVILEGED_ACTION_COMMAND=sudo /usr/local/bin/mim-privileged-action
```

## Runtime Behavior

When `deduplicate_bridge_watchers` sees duplicate `watch_tod_liveness.sh` processes, it first asks the bounded privileged runner to disable the overlapping system unit. After that, it terminates extra watcher PIDs and keeps one watcher alive.

If the bridge is not enabled, MIM keeps the previous unprivileged behavior and only removes duplicate processes.

## Verification

Use the runner directly to confirm the allowlist works:

```bash
sudo /usr/local/bin/mim-privileged-action status-system-tod-liveness-watcher
```

Then verify from MIM:

1. `/mim/self/recommendations` no longer reports `opt-deduplicate-bridge-watchers` after the health window refresh.
2. `pgrep -af watch_tod_liveness.sh` returns a single watcher.

## Extension Rule

Do not add arbitrary command execution to this path. Each new privileged action should be:

- named explicitly
- hard-mapped to one exact system command sequence
- covered by a focused test
- added to the sudoers allowlist individually