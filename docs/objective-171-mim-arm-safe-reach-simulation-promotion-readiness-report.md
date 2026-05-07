# Objective 171 — MIM Arm Safe Reach Simulation: Promotion Readiness Report

**Generated:** 2026-05-06  
**Objective ID:** MIM-ARM-SAFE-REACH-SIMULATION (objective-171)  
**Branch:** feat/objectives-39-40-lifecycle  
**Status:** promoted_verified  

---

## Scope

Implements pre-actuation reachability and collision-risk simulation for the MIM robotic arm.
Before any arm actuation is allowed, the simulation gate must pass — verifying that:

1. A valid safety envelope exists for the target zone.
2. The target zone is not declared unsafe.
3. No known obstacles in the zone push collision risk above the threshold.
4. Stale object detections do not proceed without re-observation.

---

## Endpoint Coverage

| Endpoint | Method | Purpose |
|---|---|---|
| `/workspace/targets/{target_resolution_id}/simulate` | POST | Run safe-reach simulation for a resolved workspace target |

---

## Implementation Components

| File | Role |
|---|---|
| `core/safe_reach_simulation_service.py` | Standalone computation service — `compute_reachability()`, `compute_collision_risk()`, `run_simulation()`, `SimulationResult` |
| `core/models.py` | `WorkspaceReachSimulation` ORM model (line 2304) |
| `core/schemas.py` | `WorkspaceTargetSimulateRequest` schema (line 1550) |
| `core/routers/workspace.py` | Import wiring (line 71, 115), `_autonomy_simulation_safe()` upgrade (line 1100), POST simulate endpoint (line 6697) |
| `tests/integration/test_objective_mim_arm_safe_reach_simulation.py` | 6 integration tests |

---

## Risk Parameters

| Parameter | Value |
|---|---|
| `_RISK_PER_OBSTACLE` | 0.30 |
| `_RISK_UNCERTAIN_IDENTITY` | 0.25 |
| `_RISK_UNSAFE_ZONE` | 0.40 |
| `_RISK_UNKNOWN_ZONE` | 0.50 |
| `SIMULATION_BLOCK_THRESHOLD_DEFAULT` | 0.45 |

---

## Outcome Vocabulary

### Service layer (`simulation_outcome`)

| Value | Meaning |
|---|---|
| `safe` | Reachable, collision risk below threshold |
| `unsafe` | Missing envelope, unsafe zone, unknown zone, or collision risk ≥ threshold |
| `uncertain` | Stale object detections present |

### Plan layer (`simulation_outcome` on linked `WorkspaceActionPlan`)

| Value | Mapped from |
|---|---|
| `plan_safe` | `safe` |
| `plan_blocked` | `unsafe` |
| `plan_requires_adjustment` | `uncertain` |
| `not_run` | (no simulation run yet) |

---

## Validation Results

**Test suite:** `tests/integration/test_objective_mim_arm_safe_reach_simulation.py`  
**Run date:** 2026-05-06  
**Result:** **6 passed** in 1704.64s (0:28:24)

| # | Test | Scenario | Outcome | Gate Passed |
|---|---|---|---|---|
| 1 | `test_missing_safety_envelope_blocks` | No envelope provided | `unsafe` / `confirm` | `False` |
| 2 | `test_nearby_obstruction_raises_collision_risk` | Obstacles present in zone | `unsafe` / `resimulate` | `False` |
| 3 | `test_reachable_clear_target_passes_gate` | Clear zone, valid envelope | `safe` | `True` |
| 4 | `test_simulation_result_persists` | Simulation record persisted | `safe` | `True` |
| 5 | `test_stale_object_requires_reobserve` | Stale detections in zone | `uncertain` / `reobserve` | `False` |
| 6 | `test_target_in_unsafe_zone_blocks` | Target in declared unsafe zone | `unsafe` / `confirm` | `False` |

---

## Acceptance Criteria — All Met

- [x] Missing safety envelope resolves to `unsafe` with `confirm` recovery action
- [x] Target in declared unsafe zone resolves to `unsafe` with `confirm` recovery action
- [x] Unknown zone resolves to `unsafe` with `reobserve` recovery action
- [x] Stale object detections resolve to `uncertain` with `reobserve` recovery action
- [x] Collision risk ≥ threshold resolves to `unsafe` with `resimulate` recovery action
- [x] Clear target in safe zone resolves to `safe` with `simulation_gate_passed = True`

---

## Integration Notes

- No live hardware movement is required or triggered by this objective.
- Simulation is a pre-flight gate only; arm actuation endpoints remain guarded separately.
- `_autonomy_simulation_safe()` in `workspace.py` now also checks `simulation_gate_passed` in the action plan's `trigger_json` before allowing autonomous progression.
- Journal entry written for every simulate call.
- `WorkspaceReachSimulation` record persisted per call, linked to both `WorkspaceTargetResolution` and (if applicable) `WorkspaceActionPlan`.

---

## No Regressions

All new code is additive. No existing endpoints or models were modified except:
- `_autonomy_simulation_safe()` — extended check (backward-compatible: `simulation_gate_passed` defaults absent → no block)
- Import additions to `workspace.py` (line 71, 115)
