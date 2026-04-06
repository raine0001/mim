# Objective 75 TOD Status Publisher Handoff

Date: 2026-03-23
Status: resolved
Scope: external TOD listener and integration-status publisher behavior

## Purpose

This note captured the last external blocker after MIM-side Objective 75 producer and gate hardening.

That blocker is now resolved. TOD canonical publication now surfaces manifest and handshake refresh evidence, and the recoupled bridge path passes the stricter MIM-side gates.

## Topology Clarification

The MIM producer host and the TOD consumer host can be different machines.

Producer host responsibilities:

1. host `/home/testpilot/mim/runtime/shared`
2. run `scripts/export_mim_context.py`
3. generate canonical MIM artifacts
4. publish:
   - `MIM_CONTEXT_EXPORT.latest.json`
   - `MIM_MANIFEST.latest.json`
   - `MIM_TOD_HANDSHAKE_PACKET.latest.json`
   - `MIM_TOD_ALIGNMENT_REQUEST.latest.json`

Consumer host responsibilities:

1. run TOD
2. pull the producer-host artifacts via `Invoke-TODSharedStateSync.ps1`
3. mirror them into the TOD-side staging directory such as `tod/out/context-sync/ssh-shared`
4. compute consumer-side derived state such as `TOD_INTEGRATION_STATUS.latest.json`
5. drive listener, catch-up, and UI status surfaces

Implication:

1. canonical MIM truth is produced on the MIM host, not on the TOD host
2. exporter hardening fixes must exist on the MIM host to change canonical truth
3. the TOD host only ingests, mirrors, evaluates, and displays what the MIM host published

## Correct Recovery Sequence

When canonical MIM truth is stale, the correct fix sequence is:

1. deploy the exporter fix on the MIM producer host
2. rerun `scripts/export_mim_context.py` on the MIM producer host
3. regenerate the canonical MIM artifacts on the producer host
4. run `Invoke-TODSharedStateSync.ps1` on the TOD consumer host
5. let TOD recompute integration status, catch-up, and UI surfaces from the refreshed pull

This means a stale-canonical failure belongs first to MIM producer-side publication, not TOD transport.

## Operator Runbook

Use this sequence when canonical MIM truth is suspected to be stale.

### A. MIM Producer Host

From the MIM checkout on the producer host:

```bash
cd /home/testpilot/mim
source .venv/bin/activate
.venv/bin/python scripts/export_mim_context.py --output-dir runtime/shared --no-root-mirror
```

Validate the regenerated producer artifacts:

```bash
grep -n '"objective_active"\|"current_next_objective"\|"schema_version"\|"release_tag"' runtime/shared/MIM_CONTEXT_EXPORT.latest.json
grep -n '"objective_active"\|"current_next_objective"\|"schema_version"\|"release_tag"' runtime/shared/MIM_TOD_HANDSHAKE_PACKET.latest.json
stat runtime/shared/MIM_CONTEXT_EXPORT.latest.json runtime/shared/MIM_TOD_HANDSHAKE_PACKET.latest.json runtime/shared/MIM_MANIFEST.latest.json
```

Expected outcome:

1. fresh timestamps on the shared artifacts
2. objective, release tag, and schema version match the intended producer-host truth
3. context export, manifest, and handshake packet agree

### B. TOD Consumer Host

From the TOD checkout on the consumer host, run the shared-state pull:

```powershell
Invoke-TODSharedStateSync.ps1
```

Then validate the staged pull and derived TOD status:

```powershell
Get-Content tod/out/context-sync/ssh-shared/MIM_CONTEXT_EXPORT.latest.json
Get-Content tod/out/context-sync/ssh-shared/MIM_TOD_HANDSHAKE_PACKET.latest.json
Get-Content tod/out/context-sync/ssh-shared/MIM_MANIFEST.latest.json
Get-Content tod/out/context-sync/ssh-shared/TOD_INTEGRATION_STATUS.latest.json
```

Expected outcome:

1. the staged `ssh-shared` files match the producer-host exports
2. `TOD_INTEGRATION_STATUS.latest.json` reports populated refresh evidence
3. `objective_alignment` reflects the refreshed canonical MIM truth

### C. Final Cross-Check

Confirm the consumer-host derived status includes all of the following:

1. `mim_refresh.attempted=true`
2. `mim_refresh.copied_json=true`
3. `mim_refresh.copied_yaml=true`
4. `mim_refresh.copied_manifest=true`
5. populated `mim_refresh.source_manifest`
6. populated `mim_refresh.source_handshake_packet`
7. `mim_handshake.available=true`
8. `mim_handshake.objective_active` matches the producer-host export
9. `mim_handshake.schema_version` matches the producer-host manifest truth
10. `objective_alignment.status` is `in_sync` or `aligned`

If the producer-host export is correct but the consumer-host status is stale, the remaining fault is in TOD pull, staging, or derived-status publication.

## Closure Outcome

Observed in the fresh canonical publication and downstream gate artifacts:

1. `mim_handshake.available=true`
2. `mim_handshake.objective_active=75`
3. `mim_handshake.schema_version=2026-03-12-68`
4. `mim_handshake.release_tag=objective-75`
5. `mim_refresh.attempted=true`
6. `mim_refresh.copied_manifest=true`
7. `mim_refresh.source_manifest` is populated
8. `mim_refresh.source_handshake_packet` is populated
9. `objective_alignment.status=in_sync`
10. `TOD_CATCHUP_GATE.latest.json.gate_pass=true`
11. `scripts/check_tod_recoupling_gate.sh` passes with streak `4/3`

This file is retained as a historical record of the publisher-side failure shape and its final resolution.

## What Is Already Proven On The MIM Side

The shared MIM artifacts are aligned to Objective 75 truth:

1. `runtime/shared/MIM_CONTEXT_EXPORT.latest.json`
2. `runtime/shared/MIM_MANIFEST.latest.json`
3. `runtime/shared/MIM_TOD_HANDSHAKE_PACKET.latest.json`
4. `runtime/shared/MIM_TOD_ALIGNMENT_REQUEST.latest.json`

Current intended truth:

1. objective: `75`
2. schema: `2026-03-12-68`
3. release: `objective-75`

MIM-side validation now requires TOD canonical status to publish refresh evidence, not just alignment.

## Historical External Symptom

TOD is still publishing a fresh canonical status file, but the refresh branch is not being surfaced:

Observed in `runtime/shared/TOD_INTEGRATION_STATUS.latest.json`:

1. `generated_at` is fresh
2. `mim_schema` is now `2026-03-12-68`
3. `compatible=true`
4. `objective_alignment.status=in_sync`
5. `objective_alignment.tod_current_objective=75`
6. `objective_alignment.mim_objective_active=75`

But these fields remain empty or false:

1. `mim_handshake.available=false`
2. `mim_handshake.source_path=""`
3. `mim_handshake.schema_version=""`
4. `mim_handshake.release_tag=""`
5. `mim_refresh.attempted=false`
6. `mim_refresh.copied_json=false`
7. `mim_refresh.copied_yaml=false`
8. `mim_refresh.copied_manifest=false`
9. `mim_refresh.source_json=""`
10. `mim_refresh.source_yaml=""`
11. `mim_refresh.source_manifest=""`
12. `mim_refresh.source_handshake_packet=""`
13. `mim_refresh.candidate_paths_tried=[]`
14. `mim_refresh.ssh_attempted=false`

This means TOD is consuming enough state to publish alignment and `mim_schema`, but it is not running or recording the manifest and handshake refresh path.

## Why This Matters

Objective 75 can no longer pass on partial evidence.

The MIM-side gates now fail unless TOD canonical status publishes:

1. `mim_refresh.copied_manifest=true`
2. non-empty `mim_refresh.source_manifest`
3. non-empty `mim_refresh.source_handshake_packet`
4. `mim_handshake.available=true`
5. handshake objective/schema/release matching the shared MIM handshake packet

As a result:

1. `scripts/validate_mim_tod_gate.sh` fails correctly
2. `scripts/check_tod_recoupling_gate.sh` fails correctly
3. `runtime/shared/TOD_CATCHUP_GATE.latest.json` now reports `gate_pass=false`
4. `runtime/logs/tod_catchup_status.latest.json` now reports `refresh.ok=false`

## Concrete Runtime Clues For TOD Owner

The repo contains multiple runtime breadcrumbs that narrow the external writer path.

### 1. Listener Runtime Metadata

Observed in `runtime/shared/TOD_MIM_COMMAND_STATUS.latest.json` and related TOD artifacts:

1. listener mode: `managed_polling_ssh_sync`
2. transport: `ssh_sftp`
3. remote root: `/home/testpilot/mim/runtime/shared`
4. local stage dir: `tod/out/context-sync/listener`
5. single-instance listener mutex: `Global\\TOD-MimPacketListener`

Implication:

TOD likely stages pulled shared files into a local listener directory before publishing integration status.

### 2. Canonical Catchup Writer Metadata

Observed in `runtime/shared/TOD_MIM_CATCHUP_GATE_NOTICE.latest.json`:

1. canonical task: `TOD-CatchupGateWatcher`
2. canonical writer id: `tod-catchup-gate-watcher`
3. mutex: `Global\\TOD-CatchupGateWatcher`
4. rule: only one watcher should write shared catch-up gate artifacts

Implication:

Only one external TOD catchup writer should own the gate files. MIM should not try to replace that writer.

### 3. Historical Upload Receipt

Observed in `runtime/shared/TOD_INTEGRATION_STATUS_UPLOAD_RECEIPT.latest.json`:

1. local path: `E:\\TOD\\shared_state\\integration_status.json`
2. remote path: `/home/testpilot/mim/runtime/shared/TOD_INTEGRATION_STATUS.latest.json`

Implication:

The canonical status file appears to be built on the TOD side and then uploaded into the shared MIM runtime path.

## Most Likely Failure Shape

The external TOD publisher appears to have split behavior:

1. it can parse `MIM_CONTEXT_EXPORT.latest.json`
2. it can extract `objective_active`
3. it can extract enough schema information to set `mim_schema=2026-03-12-68`
4. it can publish alignment status

But it is not entering or recording the refresh branch that should:

1. attempt shared-file pull
2. copy json and yaml artifacts
3. copy manifest
4. locate handshake packet
5. populate `mim_handshake`
6. populate source-path fields
7. record attempted candidate paths and ssh details

The most suspicious field is `mim_refresh.attempted=false` even while `mim_status.source_path` is already populated.

That suggests one of these is true:

1. the refresh code path is bypassed entirely
2. the refresh result object is being reset before publish
3. the status publisher uses a fast path from context export and never joins in the manifest and handshake pull results
4. the listener stage directory contains only context export and not manifest or handshake packet

## External TOD Checklist

The TOD owner should verify these in order.

### A. Verify Listener Stage Contents

Confirm the TOD local stage directory actually contains:

1. `MIM_CONTEXT_EXPORT.latest.json`
2. `MIM_CONTEXT_EXPORT.latest.yaml`
3. `MIM_MANIFEST.latest.json`
4. `MIM_TOD_HANDSHAKE_PACKET.latest.json`

If only the context export is present, the refresh pipeline is incomplete before publish starts.

### B. Verify Refresh Attempt State Is Recorded

Before upload, confirm the local integration-status payload sets:

1. `mim_refresh.attempted=true`
2. `mim_refresh.copied_json=true`
3. `mim_refresh.copied_yaml=true`
4. `mim_refresh.copied_manifest=true`
5. non-empty `mim_refresh.source_json`
6. non-empty `mim_refresh.source_yaml`
7. non-empty `mim_refresh.source_manifest`
8. non-empty `mim_refresh.source_handshake_packet`

### C. Verify Handshake Projection Is Filled

Before upload, confirm the local integration-status payload sets:

1. `mim_handshake.available=true`
2. non-empty `mim_handshake.source_path`
3. `mim_handshake.objective_active=75`
4. `mim_handshake.schema_version=2026-03-12-68`
5. `mim_handshake.release_tag=objective-75`

### D. Verify Canonical Publish Uses The Refresh Object

If the local payload is correct but the shared file is not, inspect the upload/publish step for a stale serialization path using only:

1. context export
2. cached status object
3. old integration-status template

### E. Verify No Single-Writer Conflict

Confirm only the canonical external TOD catchup writer is active:

1. `TOD-CatchupGateWatcher`
2. writer id `tod-catchup-gate-watcher`

If multiple writers exist, the correct status may be getting overwritten.

## Observed Fixed Outcome

The canonical `TOD_INTEGRATION_STATUS.latest.json` now shows all of the following together:

1. `mim_schema=2026-03-12-68`
2. `objective_alignment.status=in_sync` or `aligned`
3. `mim_refresh.attempted=true`
4. `mim_refresh.copied_manifest=true`
5. populated `source_manifest` and `source_handshake_packet`
6. `mim_handshake.available=true`
7. `mim_handshake.objective_active=75`
8. `mim_handshake.schema_version=2026-03-12-68`
9. `mim_handshake.release_tag=objective-75`

With that publication in place, the MIM-side gates now pass without any further MIM code changes.

## MIM-Side Status

No further producer-side contract changes are required from MIM for Objective 75 closure.

The external TOD refresh publication behavior issue is resolved, and Objective 75 is closed as a recoupled interface baseline.
