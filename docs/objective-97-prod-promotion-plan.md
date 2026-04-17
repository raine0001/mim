# Objective 97 Production Promotion Plan

Date: 2026-03-28
Objective: 97
Release Tag Target: objective-97
Status: planned_for_promotion_review

## Promotion Intent

Promote the bounded Objective 97 recovery-learning slice after readiness review confirms that scoped escalation, state-bus publication, and operator-visible reasoning remain stable outside the tight 91-97 control-plane lane.

## Promotion Preconditions

- Objective 97 focused lane is green
- adjacent 91-97 execution-control lane is green
- broader adjacent branch-neighborhood lane is green
- backend health on the candidate runtime is healthy before rollout

## Candidate Validation Inputs

- focused Objective 97 lane: `Ran 6 tests ... OK`
- adjacent 91-97 lane: `Ran 16 tests ... OK`
- broader adjacent branch-neighborhood lane: `Ran 37 tests ... OK`

## Production Verification Plan

After promotion, verify the following in order:

1. `GET /health` returns `200 OK`
2. `GET /manifest` reports the expected release tag and git SHA
3. `GET /execution/recovery/learning/profiles` responds and exposes recovery-learning profiles for a seeded scope
4. `GET /mim/ui/state` includes `operator_reasoning.execution_recovery_learning`
5. `GET /state-bus/snapshots` exposes the execution-recovery snapshot with the learning payload for a seeded trace
6. run the normal production smoke flow used for recent objective promotions

## Explicit Behavior Summary For Promotion Review

- repeated failed recovery outcomes escalate the next retry path before another bounded retry is accepted
- repeated successful recovery outcomes remain bounded and inspectable
- mixed histories remain decision-specific inside a scope
- recovery-learning posture is visible through execution APIs, the state bus, and the operator UI

## Known Boundary Conditions

- recovery-learning decay and expiry are not yet implemented
- operator-assisted success contributes positive history but does not yet perform a dedicated profile reset
- environmental change does not yet invalidate recovery-learning directly; the system waits for new execution outcomes and related readiness/stability signals
- this promotion plan assumes the same hardened readiness-fixture semantics used in testing are not required in production because production readiness artifacts are authored by runtime processes rather than reused test fixtures

## Forward Development Control Policy

To avoid repeated workflow stalls when the workspace is dirty, Objective 97 adopts the active policy in [docs/mim-development-autonomy-policy.md](docs/mim-development-autonomy-policy.md).

Policy default:

- `MODE_A_TARGETED_CONTINUE`

Meaning:

- continue objective work without pausing for unrelated modified files
- touch only objective-coupled files
- run targeted validation for touched files
- do not revert unrelated changes

Escalation:

- move to `MODE_B_EXPANDED_REVIEW` only when changed tests are directly coupled and materially affect objective confidence
- move to `MODE_C_PAUSE_FOR_SNAPSHOT` only on hard-stop conditions (merge conflicts, unresolved behavioral ambiguity in required files, or untrustworthy validation state)

## Objective 97 Policy Decision Record

- policy_mode: `MODE_A_TARGETED_CONTINUE`
- objective_scope: Objective 97 recovery-learning promotion and adjacent integration validation
- hard_stop_detected: false
- expanded_review_needed: false
- touched_files: `core/* objective-coupled integration files only`
- validations_run: focused and adjacent objective lanes
- outcome: continue without pause, with scoped edits and targeted tests

## Gateway Governance Hardening

Current status line:

- MIM gateway and orchestration safety were hardened by fixing self-health trend analysis, adding regression coverage for risky user-action inference, blocking dispatch on unresolved user-action safety escalation, and coupling degraded self-health directly into execution governance by converting auto-execution to confirmation-required when system health is not healthy.

Governance precedence:

- explicit operator approval or force override
- hard user-action safety escalation
- degraded or critical self-health confirmation requirement
- benign healthy auto-execution

Operator-visible reasoning requirements:

- degraded or critical self-health must appear in gateway clarification prompts
- combined safety and health signals must be preserved in gateway governance metadata and dispatch refusal context
- safety inquiry payloads must carry current system-health context for operator review

Latest implementation status:

- operator-facing system reasoning now includes gateway governance snapshot data (primary signal, health status, summary)
- physical workspace execution dispatch (`/workspace/action-plans/{plan_id}/execute`) now applies self-health gating and keeps degraded/critical states in confirmation-required posture instead of auto-dispatch
- deterministic integration coverage validates healthy/degraded/critical and combined-signal precedence in-process
- live HTTP governance lane is present and currently skips on runtimes where `/mim/self/health/record-metric` is not mounted; enable self-awareness routes on the target runtime to activate this lane

## Promotion Decision Template

- Promotion: PENDING
- Production Health: PENDING
- Production Smoke: PENDING
- Manifest Verification: PENDING
- Objective 97 Recovery-Learning Probe: PENDING
- State-Bus Recovery Snapshot Probe: PENDING