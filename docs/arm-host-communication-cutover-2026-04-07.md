# ARM Host Communication Cutover

Canonical communication authority is `192.168.1.120:/home/testpilot/mim/runtime/shared`.

The ARM host `192.168.1.90` is execution-only. It must not remain a silent fallback communication surface.

## Repo-side patch status

- Communication-specific helpers no longer inherit ARM SSH defaults.
- Bridge audit labeling no longer falls back to `MIM_ARM_SSH_REMOTE_ROOT`.
- Use `scripts/arm_host_communication_cutover.py` for staged disable and cleanup of the legacy ARM-host surface.

## Safe staged workflow

### 1. Check current state

```bash
python scripts/arm_host_communication_cutover.py --mode check
```

Expected result:

- `surface_dir` points at `/home/testpilot/mim/runtime/shared`
- `disabled_exists` is `false` before cutover
- `trap_exists` is `false` before cutover

### 2. Disable instead of deleting

```bash
python scripts/arm_host_communication_cutover.py --mode disable
```

This performs the safe equivalent of:

- rename `/home/testpilot/mim/runtime/shared` to `/home/testpilot/mim/runtime/shared_DISABLED`
- recreate `/home/testpilot/mim/runtime/shared` as a trap directory
- write `ERROR_COMMUNICATION_SURFACE_BLOCKED.txt`
- make the trap directory read-only by default

### 3. Watch for incorrect recreation attempts

After disable, any process still treating `.90` as a communication target will now fail loudly instead of silently repopulating the legacy surface.

Check state again:

```bash
python scripts/arm_host_communication_cutover.py --mode check
```

Expected result after disable:

- `disabled_exists` is `true`
- `trap_exists` is `true`
- `trap_read_only` is `true`

### 4. Purge after validation

Only after validation shows no process still depends on the disabled directory:

```bash
python scripts/arm_host_communication_cutover.py --mode purge
```

This removes `shared_DISABLED` but leaves the trap directory in place.

## Local dry-run and test mode

For rehearsal or test automation:

```bash
python scripts/arm_host_communication_cutover.py \
  --mode disable \
  --local-surface-root /tmp/mim-arm-cutover/shared
```

## Notes

- Do not use the ARM-host shared directory as a communication mirror after cutover.
- Execution-only ARM surfaces remain valid: readiness, dispatch telemetry, host-state sync, camera/runtime evidence.