# Objective 75 - Active TOD Plan for Long Stalls and Job Feed Pauses

Date: 2026-03-17
Status: active
Scope: MIM <-> TOD shared-feed stall handling (`runtime/shared`)

## Current Situation

- Latest TOD result indicates objective alignment mismatch (`tod_current_objective=4`, `mim_objective_active=75`).
- Repeated stale-feed freeze events are present in `runtime/shared/TOD_LIVENESS_EVENTS.latest.jsonl`.
- Same-task repeat failure pressure has occurred on `objective-75-task-3149`.

## Joint Objective

Restore stable feed cadence and prevent long silent stalls by adding deterministic phases:

1. detect stall quickly
2. ping and verify liveness
3. perform bounded catch-up sync
4. gate retries to avoid same-task loops
5. escalate with structured evidence if unresolved

## Active Plan (MIM + TOD)

1. Phase A - Fast Stall Detect (MIM owner)

- Watch freshness for:
  - `TOD_MIM_TASK_ACK.latest.json`
  - `TOD_MIM_TASK_RESULT.latest.json`
  - `TOD_LOOP_JOURNAL.latest.json`
  - `TOD_INTEGRATION_STATUS.latest.json`
- Mark `freeze_suspected` when oldest age > 45s.
- Open incident only if 2 consecutive freeze samples occur.

1. Phase B - Liveness Ping (MIM owner)

- Emit `MIM_TO_TOD_PING.latest.json` and `MIM_TO_TOD_TRIGGER.latest.json` (`trigger=liveness_ping`).
- Cooldown: one ping every 30s max.
- Expect TOD trigger ACK and fresh artifact movement within 90s.

1. Phase C - Catch-Up Sync (TOD owner)

- Run one explicit shared-folder pull and rebuild `TOD_INTEGRATION_STATUS.latest.json`.
- Validate objective alignment against current MIM truth before accepting next task.
- If alignment mismatches, emit `TOD_CATCHUP_GATE.latest.json` with `promotion_ready=false` and reason.

1. Phase D - Retry Guardrail (joint)

- Cap same-task retries to 3.
- Require objective/task pointer movement after each failed cycle.
- If no pointer movement after 3 fails, stop autonomous loop and emit guardrail alert.

1. Phase E - Escalation (joint)

- Escalate when either condition is true:
  - freeze persists > 10 minutes, or
  - same-task failure cap reached without alignment recovery.
- Required evidence bundle:
  - current request/ack/result IDs
  - alignment status and objective delta
  - regression signature and unchanged cycle count

## Packet-Level Contract for This Plan

- MIM -> TOD:
  - `MIM_TOD_ALIGNMENT_REQUEST.latest.json`
  - `MIM_TO_TOD_PING.latest.json`
  - `MIM_TO_TOD_TRIGGER.latest.json`
- TOD -> MIM:
  - `TOD_INTEGRATION_STATUS.latest.json`
  - `TOD_CATCHUP_GATE.latest.json`
  - `TOD_MIM_TASK_ACK.latest.json`
  - `TOD_MIM_TASK_RESULT.latest.json`
  - `TOD_TO_MIM_TRIGGER_ACK.latest.json`

## Success Criteria

All must be true for closure:

1. `objective_alignment.status` is `aligned` or `in_sync` with matching objective IDs.
2. ACK and RESULT request IDs match current request ID for 3 consecutive cycles.
3. No `freeze_suspected` incidents for 15 minutes.
4. No repeated same-task failure guardrail stop for current objective.

## Immediate Next Actions

1. TOD runs catch-up sync and publishes fresh `TOD_INTEGRATION_STATUS.latest.json`.
2. MIM verifies alignment and emits follow-up go-order only after alignment passes.
3. Joint check after 3 cycles; if unstable, switch to escalation path.

## Two-Track Execution Window (Recommended: 24-48h)

Use this while TOD catches up and MIM continues shipping internal changes.

### Track A - MIM Safe-Lane Backlog

Allowed now:

1. Internal router/service fixes that do not change shared packet shape.
2. Internal lifecycle/state correctness updates inside MIM APIs.
3. Test coverage and reliability hardening for MIM-only paths.
4. UI/voice/workflow improvements that do not alter TOD contract fields.

Do not change during catch-up window:

1. `runtime/shared` packet schemas and required field names.
2. Trigger vocabulary consumed by TOD listeners.
3. Required ACK correlation semantics.

Execution checklist:

1. Before each MIM merge, confirm no shared contract delta in `runtime/shared/*.latest.json` templates/docs.
2. Tag PRs as `safe-lane` if contract-neutral.
3. Defer contract changes into a post-catchup queue.

### Track B - TOD Catch-Up Acceptance Gates

TOD target state:

1. Objective pointer parity with MIM active lane.
2. Stable trigger ACK freshness.
3. Passing review gate for current request id.

Required evidence files:

1. `runtime/shared/TOD_TO_MIM_TRIGGER_ACK.latest.json`
2. `runtime/shared/TOD_INTEGRATION_STATUS.latest.json`
3. `runtime/shared/TOD_MIM_TASK_RESULT.latest.json`
4. `runtime/shared/TOD_CATCHUP_GATE.latest.json`

Gate checks:

1. `TOD_TO_MIM_TRIGGER_ACK.latest.json.generated_at` advances each active cycle.
2. `TOD_INTEGRATION_STATUS.latest.json.objective_alignment.aligned == true`.
3. `TOD_MIM_TASK_RESULT.latest.json.review_gate.passed == true`.
4. `TOD_CATCHUP_GATE.latest.json.gate_pass == true` for 3 consecutive samples.

### Re-Coupling Decision Gate

Resume active MIM<->TOD contract evolution only when all are true:

1. Track B gate checks pass for 3 consecutive cycles.
2. No freeze incidents for 15 minutes.
3. Current request/ack/result IDs remain synchronized across a full cycle.

If any check fails, stay in two-track mode and continue TOD catch-up without introducing new contract changes.

### Baseline Record Command

When TOD confirms gate pass from its side, write a shared baseline record in MIM:

```bash
MODE=external \
EXTERNAL_CAN_RECOUPLE=true \
EXPECTED_OBJECTIVE=74 \
REQUIRED_CONSECUTIVE=3 \
SOURCE='tod-gate-run-YYYY-MM-DD' \
NOTES='4/4 checks passing for 3 consecutive cycles; recoupling approved.' \
scripts/mark_recoupling_baseline.sh
```

Artifact written:

1. `runtime/logs/tod_recoupling_baseline.latest.json`

## Post-Catchup Feature Queue

Once recoupling gate passes, execute the carrier automation roadmap in:

1. `docs/mim-carrier-automation-feature-roadmap.md`

This keeps current Objective 75 stall recovery isolated from larger browser/auth automation changes until shared-feed stability is confirmed.
