# Objective 97 Observation Runbook

Date: 2026-03-28
Objective: 97
Purpose: Stand up and run a repeatable local observation workflow for recovery-learning and escalation behavior.

## Scope

This runbook validates Objective 97 behavior in a local observation environment by:

1. Seeding repeated failed recovery outcomes in a scoped lane.
2. Running a fresh recovery evaluation probe for the same scope.
3. Verifying learning and escalation surfaces across:
   - `POST /execution/recovery/evaluate`
   - `GET /execution/recovery/learning/profiles`
   - `GET /execution/recovery/learning/telemetry`
   - `GET /execution/recovery/{trace_id}`
   - `GET /mim/ui/state`
   - `GET /state-bus/snapshots/{execution-recovery:<scope>:<trace>}`

## Prerequisites

1. Backend runtime is healthy on the target base URL.
2. Python/venv dependencies are installed.
3. State-bus and execution-control surfaces are enabled in the running build.

## Start Local Runtime (if needed)

```bash
/home/testpilot/mim/.venv/bin/python -m uvicorn core.app:app --host 127.0.0.1 --port 18001
```

## Run Observation Workflow

Default local run (`:18001`):

```bash
cd /home/testpilot/mim
MIM_TEST_BASE_URL=http://127.0.0.1:18001 ./scripts/run_objective97_observation.sh
```

Run with an explicit scope and output directory:

```bash
cd /home/testpilot/mim
MIM_TEST_BASE_URL=http://127.0.0.1:18001 \
  ./scripts/run_objective97_observation.sh objective97-observe-manual runtime/reports/objective97_observation
```

## Output Artifacts

The script writes one JSON artifact per scope:

- `runtime/reports/objective97_observation/<scope>.json`

Each artifact contains:

1. Run metadata (`generated_at`, `base_url`, `scope`, `trace_id`, `execution_id`).
2. Pass/fail checks for each required observation surface.
3. Raw response snapshots from recovery/profile/telemetry/UI/state-bus endpoints.

## Pass Criteria

Observation run is considered healthy when all checks pass:

1. Recovery payload includes `recovery_learning`.
2. Escalation decision after seeded failures is `require_operator_takeover`.
3. Learning profile endpoint returns `profiles` and `latest_profile` contract.
4. Telemetry endpoint returns `window`, `metrics`, and `alerts`.
5. UI includes `operator_reasoning.execution_recovery_learning`.
6. State-bus snapshot includes `state_payload_json.recovery_learning`.

## Troubleshooting

1. If `/health` fails, start or restart local runtime and retry.
2. If state-bus snapshot lookup fails, ensure state-bus writer is active in the runtime.
3. If escalation check fails, inspect `checks[]` and `recovery_evaluate` in the artifact for the exact learning state and decision values.
