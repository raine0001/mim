# MIM Day 08 Codebase Stewardship Map

This map is a practical edit guide for MIM's current autonomous-execution stack. It identifies the primary ownership seams, the runtime surfaces they control, and the minimum validation expected before trusting a change.

## Top-Level Ownership Map

| area | primary files | purpose | default edit posture | cheapest validation |
| --- | --- | --- | --- | --- |
| conversation intake and routing | `core/routers/gateway.py`, `core/communication_composer.py`, `core/interface_service.py` | accepts user input, selects initiative or conversation route, shapes reply mode | guarded | focused gateway or objective-lifecycle tests |
| initiative planning and continuation | `core/autonomy_driver_service.py`, `core/objective_lifecycle.py` | parses initiative ids, builds task plans, advances local MIM tasks, computes completion | guarded | focused `tests/test_autonomy_driver_service.py` or `tests/test_objective_lifecycle.py` |
| TOD lineage and truth consumption | `scripts/tod_status_signal_lib.py`, `core/primitive_request_recovery_service.py`, `core/execution_truth_service.py`, `core/execution_truth_governance_service.py` | classifies authoritative request and result lineage, prevents stale or wrong-task promotion | high-risk | focused lineage tests plus the coordination harness |
| operator-visible UI truth surfaces | `core/routers/mim_ui.py` | exposes reconciliation snapshots, system activity, runtime health, and recovery state | validation-required | focused mim-ui truth tests |
| execution readiness and recovery policy | `core/execution_readiness_service.py`, `core/execution_recovery_service.py`, `core/execution_policy_gate.py`, `core/policy_conflict_resolution_service.py` | decides whether execution is allowed, degraded, deferred, retried, or operator-gated | high-risk | focused policy or recovery tests |
| runtime and self-healing observation | `core/self_optimizer_service.py`, `core/runtime_recovery_service.py`, `core/stewardship_service.py` | observes stale runtime conditions, bounded recoveries, and stewardship actions | validation-required | focused self-awareness or runtime recovery tests |
| shared-artifact contracts and docs | `contracts/`, `docs/`, `runtime/` schemas and reports | fixes structure and contract drift between MIM, TOD, and UI layers | safe if docs-only, guarded if schema-changing | schema or consumer-specific regression |

## Edit Zones

### Safe Zones

- `docs/` files that explain existing behavior without changing runtime assumptions.
- `contracts/` commentary or examples that do not modify required fields or field meaning.
- reporting or scenario docs such as coordination scenario catalogs and invariant notes.

### Guarded Zones

- `core/routers/gateway.py`: small routing changes can reroute normal chat into initiative execution or stale continuation paths.
- `core/communication_composer.py`: seemingly cosmetic reply changes can suppress necessary uncertainty or reintroduce conversational hedges.
- `core/autonomy_driver_service.py`: edits affect initiative creation, next-task selection, auto-resume, and project progression.
- `core/objective_lifecycle.py`: completion rules must keep planning-only and failed work from appearing complete.

### Validation-Required Zones

- `core/routers/mim_ui.py`: UI snapshots are downstream of multiple truth sources; one field regression can make live state look healthy when it is stale.
- `core/self_optimizer_service.py`: recovery actions can trigger bridge restart, direct fallback, or stale-guard overrides.
- `core/runtime_recovery_service.py`: cooldown and retry evidence must stay bounded and operator-visible.
- `core/routers/public_chat.py` and similar UX surfaces when they mirror gateway response-mode decisions.

### High-Risk Zones

- `scripts/tod_status_signal_lib.py`: this is the core acceptance surface for request/task/result lineage safety.
- `core/primitive_request_recovery_service.py`: authority synthesis here determines whether stale review or fallback artifacts can override the active request.
- `core/execution_truth_governance_service.py` and `core/execution_truth_service.py`: precedence mistakes here can promote non-authoritative execution state.
- `contracts/` schema changes that alter MIM/TOD payload meaning or required lineage fields.

## Runtime Surfaces And Artifacts

### MIM/TOD Shared Artifacts

These files are the main cross-process truth surfaces under the shared runtime root.

- `MIM_TOD_TASK_REQUEST.latest.json`: active bounded request from MIM to TOD.
- `TOD_MIM_TASK_ACK.latest.json`: TOD acknowledgement surface.
- `TOD_MIM_TASK_RESULT.latest.json`: TOD result or failure surface.
- `TOD_MIM_COMMAND_STATUS.latest.json`: command lifecycle and readiness evidence.
- `TOD_EXECUTION_TRUTH.latest.json`: authoritative TOD-side execution truth surface.
- `TOD_MIM_EXECUTION_DECISION.latest.json`: TOD execution decision and routing output.
- `MIM_TASK_STATUS_REVIEW.latest.json`: MIM-side review summary for the current request lane.
- `TOD_INTEGRATION_STATUS.latest.json`: merged coordination and health snapshot.
- `MIM_TOD_FALLBACK_ACTIVATION.latest.json`: fallback activation evidence that must never outrank same-task lineage truth.

### Internal Persistence And Derived State

- `runtime/formal_program_drive_response.json`: strongest current project/program status summary when lighter snapshots disagree.
- `runtime/shared/mim_program_registry.latest.json`: useful but can lag behind formal drive state.
- `runtime/reports/mim_tod_coordination_simulation_report.latest.json`: coordination harness result artifact for lineage-safety regression proof.
- `MIM_CONTEXT_EXPORT.latest.json` and `.yaml`: exported conversational and execution context surfaces that downstream tooling may consume.

## Recommended Validation By Change Type

| change type | minimum validation |
| --- | --- |
| response-policy or confidence wording | focused `tests/test_objective_lifecycle.py` slice |
| initiative parsing, routing, planning-only behavior | focused `tests/test_autonomy_driver_service.py` and `tests/integration/test_gateway_health_governance_integration.py` slice |
| continuation, auto-resume, completion evidence | focused autonomy-driver or objective-lifecycle tests |
| TOD lineage, stale-guard, wrapper-only result handling | focused `tests/integration/test_tod_task_status_review.py` plus the coordination harness |
| UI truth or system-activity state | focused mim-ui truth reconciliation tests and live snapshot inspection if available |
| runtime recovery or self-optimizer logic | focused self-awareness or runtime recovery tests |

## Practical Ownership Notes

- If the bug is about conversational tone, start in `core/communication_composer.py` before changing gateway routing.
- If the bug is about initiatives inheriting the wrong lineage or continuing the wrong work, start in `core/autonomy_driver_service.py` and `core/routers/gateway.py`.
- If the bug is about stale TOD results, wrong-task completions, wrapper-only results, or fallback authority drift, start in `scripts/tod_status_signal_lib.py`.
- If the bug is about what the operator sees in MIM web, start in `core/routers/mim_ui.py`, but expect the true fix to live upstream in truth or recovery services.
- When formal project status and lightweight status snapshots disagree, trust `runtime/formal_program_drive_response.json` first and treat lighter registry files as advisory until refreshed.

## Current Stewardship Rule Of Thumb

Make edits as close as possible to the authority seam that decides state. Avoid fixing UI text, gateway summaries, or downstream reports when the real defect is lineage classification, readiness precedence, or initiative continuation upstream.
