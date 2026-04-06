# MIM-TOD Dispatch Go/No-Go Checklist

Purpose: provide a strict, reusable gate before MIM treats TOD as dispatch-ready for critical work.

## Scope

Use this checklist for any critical TOD dispatch, including arm-gating and safety-sensitive tasks.

## Gate 1: Listener/Bridge Recovery

Question: Has the TOD listener/bridge process that writes ACK artifacts been recovered and verified healthy?

Pass criteria:
- TOD listener process is running and reachable.
- Shared ACK artifact is writable and timestamp can advance.
- No stale watchdog state indicating frozen listener.

No-go if any fail.

## Gate 2: ACK Freshness Mutation (Hard Precondition)

Question: Does a 2-cycle forced trigger check show ACK timestamp mutation across both cycles?

Pass criteria:
- Cycle 1 produces ACK update.
- Cycle 2 produces a new ACK timestamp different from cycle 1.
- No synthetic single-cycle pass is accepted as dispatch-ready.

Policy:
- This gate is mandatory.
- If this gate fails, MIM must block critical dispatch.

## Gate 3: Critical Task Reissue Integrity

Question: After Gate 2 passes, does the reissued critical task have exact request_id matching in both ACK and RESULT?

Pass criteria:
- Reissued task_id in request artifact equals request_id in ACK artifact.
- Reissued task_id in request artifact equals request_id in RESULT artifact.
- ACK status indicates accepted/acknowledged for the same task.
- RESULT status is terminal and tied to the same task.

No-go if any mismatch occurs.

## Operational Sequence

1. Recover/restart TOD listener bridge owner.
2. Run 2-cycle ACK mutation check.
3. If pass, reissue critical task.
4. Validate request_id equality in request, ACK, and RESULT.
5. Only then proceed to downstream execution phases.

## Required Artifacts

- runtime/shared/MIM_TO_TOD_TRIGGER.latest.json
- runtime/shared/TOD_TO_MIM_TRIGGER_ACK.latest.json
- runtime/shared/MIM_TOD_TASK_REQUEST.latest.json
- runtime/shared/TOD_MIM_TASK_ACK.latest.json
- runtime/shared/TOD_MIM_TASK_RESULT.latest.json

## Authority and Access

Policy: MIM and TOD both have explicit authority to access troubleshooting and resolution artifacts for Objective 97 dispatch governance.

Required authority:
- MIM: read and write access to all required artifacts to evaluate gates, emit triggers, and issue decisions.
- TOD: read and write access to all required artifacts to acknowledge triggers, publish execution status, and provide result evidence.

Operational rule:
- Access denial, stale mount, or write failure on any required artifact is an immediate NO-GO condition until resolved.

Troubleshooting minimum data access:
- trigger packet content and metadata
- trigger ACK packet content and metadata
- task request payload
- task ACK payload
- task result payload
- correlation and request_id linkage fields

## Decision Record Template

- Gate 1: PASS or FAIL
- Gate 2: PASS or FAIL
- Gate 3: PASS or FAIL
- Final: GO or NO-GO
- Block reason code(s):
- Correlation/task id:
- Operator/agent:
- Timestamp (UTC):

## Default Governance Rule

MIM must treat Gate 2 and Gate 3 as blocking controls for critical dispatch.
If either gate is not satisfied, MIM remains in NO-GO state and routes to recovery workflow.
