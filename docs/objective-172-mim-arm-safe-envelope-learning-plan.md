# Objective 172 — MIM Arm Safe Envelope Learning Plan

**Status:** plan  
**Objective ID:** MIM-ARM-SAFE-ENVELOPE-LEARNING-PLAN (objective-172)  
**Depends on:** Objective 171 (MIM-ARM-SAFE-REACH-SIMULATION) — promoted_verified  
**Branch:** feat/objectives-39-40-lifecycle  
**Date:** 2026-05-06  

---

## Purpose

Objective 171 gives MIM the ability to simulate reach and collision risk before actuation.
Objective 172 defines the next learning layer: **safe servo envelope learning** — building
a verified per-servo operational envelope from simulation and supervised physical probing,
so that future simulation and planning use real, confidence-weighted limits rather than
static configured bounds.

**No hardware movement is performed in this planning document.**
Physical probing is defined but gated behind explicit phase transitions under operator approval.

---

## 1. Current Arm Control Surface Inventory

### 1.1 Configured Servo Limits (`core/execution_lane_service.py`)

| Servo ID | Axis / Role | Configured Min | Configured Max | Notes |
|---|---|---|---|---|
| 0 | Base rotation | 0° | 180° | Full sweep |
| 1 | Shoulder | 15° | 165° | Mechanical hard-stop buffer |
| 2 | Elbow | 0° | 180° | Full sweep |
| 3 | Wrist pitch | 0° | 180° | Full sweep |
| 4 | Wrist roll | 0° | 180° | Full sweep |
| 5 | Gripper/Claw | 0° | 180° | Close=50°, Open=125° |

Constants: `MIM_ARM_SERVO_LIMITS`, `MIM_ARM_CLAW_OPEN_ANGLE=125`, `MIM_ARM_CLAW_CLOSE_ANGLE=50`

### 1.2 Known Poses

| Pose | Values (servo 0–5) |
|---|---|
| `safe_home` | [90, 90, 90, 90, 90, 50] |
| `scan_pose` | Routed via `_translate_mim_arm_steps()` |
| Default | [90, 90, 90, 90, 90, 50] |

### 1.3 Bounded Live Actions

`BOUNDED_LIVE_ACTIONS = ("safe_home", "scan_pose", "capture_frame")` — all arm dispatch goes through capability gates.

### 1.4 Motion Gates

| Gate | Field | Source |
|---|---|---|
| Emergency stop OK | `estop_ok` | `mim_arm_status.latest.json` |
| E-stop supported | `estop_supported` | `mim_arm_status.latest.json` |
| Motion allowed | `motion_allowed` | computed from readiness + estop |
| TOD execution allowed | `tod_execution_allowed` | `TOD_MIM_COMMAND_STATUS.latest.json` |
| TOD execution block reason | `tod_execution_block_reason` | router evaluation |
| Motion block reasons | `motion_block_reasons` | list; blocks dispatch if non-empty |

### 1.5 Execution Feedback Fields

| Field | Type | Meaning |
|---|---|---|
| `before_state.current_pose` | list[int, 6] | Actual pose before command |
| `after_state.current_pose` | list[int, 6] | Actual pose after command |
| `step_result.ok` | bool | Step-level success flag |
| `step_result.timed_out` | bool | Step timed out |
| `last_command_status` | str | "ok" / "timed_out" / "error" / "unknown" |
| `servo_states` | dict | Live per-servo state from arm host (currently `{}` in sim mode) |

### 1.6 Safe Reach Simulation (Objective 171)

| Component | Location |
|---|---|
| `run_simulation()` | `core/safe_reach_simulation_service.py` |
| `WorkspaceReachSimulation` | `core/models.py:2304` |
| `POST /workspace/targets/{id}/simulate` | `core/routers/workspace.py:6697` |
| `_autonomy_simulation_safe()` gate | `core/routers/workspace.py:1100` |

---

## 2. Safe Servo Envelope Learning Data Model

### 2.1 `ArmServoEnvelope` (proposed ORM model)

Per-servo learned envelope. One record per servo ID per arm instance.

| Field | Type | Description |
|---|---|---|
| `id` | UUID PK | |
| `arm_id` | str | Arm hardware identity (e.g. host IP or serial) |
| `servo_id` | int | Servo index (0–5) |
| `servo_role` | str | "base", "shoulder", "elbow", "wrist_pitch", "wrist_roll", "gripper" |
| `configured_min` | int | Hard floor from `MIM_ARM_SERVO_LIMITS` |
| `configured_max` | int | Hard ceiling from `MIM_ARM_SERVO_LIMITS` |
| `learned_soft_min` | int \| None | Minimum confirmed safe by probing |
| `learned_soft_max` | int \| None | Maximum confirmed safe by probing |
| `preferred_range_min` | int \| None | Operator/simulation-validated comfortable range min |
| `preferred_range_max` | int \| None | Operator/simulation-validated comfortable range max |
| `unstable_regions` | JSON | List of `{"from": int, "to": int, "reason": str}` — known unstable angle spans |
| `confidence` | float | 0.0–1.0; increases with verified evidence count |
| `evidence_count` | int | Number of successful probe confirmations |
| `last_verified_at` | datetime \| None | Most recent successful physical probe |
| `last_probe_phase` | str | "none", "simulation", "dry_run", "supervised_micro", "autonomous" |
| `stale_after_seconds` | int | Seconds after which envelope must be re-verified (default 86400) |
| `is_stale` | bool (computed) | `now > last_verified_at + stale_after_seconds` |
| `actor` | str | Who set/updated the record |
| `source` | str | "initialization", "simulation", "dry_run", "supervised_probe", "autonomous_probe" |
| `metadata_json` | JSON | Auxiliary fields |
| `created_at` | datetime | |
| `updated_at` | datetime | |

### 2.2 `ArmEnvelopeProbeAttempt` (proposed ORM model)

Log of every probe attempt, regardless of phase.

| Field | Type | Description |
|---|---|---|
| `id` | UUID PK | |
| `envelope_id` | FK → ArmServoEnvelope | Which servo envelope this attempt updates |
| `phase` | str | "simulation", "dry_run", "supervised_micro", "autonomous" |
| `servo_id` | int | Servo index |
| `target_angle` | int | Requested angle |
| `actual_angle` | int \| None | Observed angle post-command (from after_state) |
| `position_error` | int \| None | `abs(target_angle - actual_angle)` |
| `position_mismatch` | bool | `position_error > threshold` |
| `current_spike` | bool | Current draw exceeded safe threshold |
| `motion_timed_out` | bool | Command timed out |
| `unexpected_resistance` | bool | Force/torque feedback indicates resistance |
| `camera_hazard_detected` | bool | Vision system flagged object in path |
| `operator_stopped` | bool | Manual stop received |
| `estop_triggered` | bool | Emergency stop fired |
| `motion_allowed_at_start` | bool | `motion_allowed` value before dispatch |
| `stop_condition_triggered` | str \| None | Which stop condition fired (if any) |
| `outcome` | str | "safe", "stopped", "error", "skipped" |
| `simulation_id` | FK → WorkspaceReachSimulation \| None | Linked simulation result |
| `execution_feedback_json` | JSON | Full `after_state` dump |
| `actor` | str | |
| `created_at` | datetime | |

---

## 3. Safe Probing Phases

All phase transitions require explicit operator authorization or MIM approval under a readiness gate. No phase automatically advances to the next.

### Phase 1 — Simulation Only

**Trigger:** No physical movement. Uses `safe_reach_simulation_service` to evaluate servo sweep paths.  
**Input:** ArmServoEnvelope initialized from `MIM_ARM_SERVO_LIMITS`.  
**Output:** Per-servo simulated reach confidence; simulation records linked to `WorkspaceReachSimulation`.  
**Gate pass:** `simulation_outcome == "safe"` for each sweep path.  
**Envelope update:** `last_probe_phase = "simulation"`, confidence +0.1 per confirmed path.  
**Hardware command issued:** None.

### Phase 2 — Dry-Run Command Generation

**Trigger:** Operator authorization or MIM gate approval after Phase 1 baseline.  
**Action:** Generate the command payloads that *would* be dispatched for a micro-step sweep — but do not dispatch. Write them to `runtime/reports/mim_arm_envelope_dry_run.latest.json`.  
**Output:** Dry-run plan reviewed by operator.  
**Gate pass:** Operator acknowledges dry-run plan.  
**Hardware command issued:** None.

### Phase 3 — Supervised Micro-Step Physical Probing

**Trigger:** Explicit operator authorization per servo, per step.  
**Action:** Dispatch micro-step commands (≤5° increments from safe_home) under full gate checks. Record `ArmEnvelopeProbeAttempt` per step. Stop immediately on any stop condition.  
**Scope:** One servo at a time. Return to safe_home between servos.  
**Gate requirements:**
- `estop_ok = True`
- `motion_allowed = True`
- `tod_execution_allowed = True`
- No camera hazard
- Operator confirmation per probe session

**Envelope update:** `learned_soft_min`, `learned_soft_max` narrowed from observed stop events. Confidence +0.2 per confirmed step.  
**Hardware command issued:** Yes — micro-step only, fully reversible (returns to safe_home after each probe).

### Phase 4 — Confidence-Weighted Autonomous Probing

**Trigger:** Requires Phase 3 completion for all 6 servos, confidence ≥ 0.6, and explicit MIM + operator approval.  
**Action:** MIM autonomously selects next probe angle based on confidence gap in envelope. Probe, record, update envelope.  
**Rate limiting:** Max 3 autonomous probe sessions per day. Each session max 10 steps.  
**Gate requirements:** All Phase 3 gates, plus `envelope.confidence ≥ 0.6` for the target servo.  
**Hardware command issued:** Yes — bounded by learned_soft limits, never exceeds configured limits.

---

## 4. Stop Conditions

All stop conditions are evaluated before dispatch and after each step response. Any triggered stop condition:
1. Halts the current probe session immediately.
2. Commands a return to `safe_home`.
3. Writes the stop event to `ArmEnvelopeProbeAttempt.stop_condition_triggered`.
4. Marks the probe attempt `outcome = "stopped"`.
5. Does **not** update `learned_soft_min`/`learned_soft_max` to a new limit without confirmation.

| Stop Condition | Detection Method | Phase Applicability |
|---|---|---|
| **current_spike** | Current draw from servo feedback exceeds configured threshold | Phase 3, 4 |
| **motion_timeout** | Arm did not reach target angle within `timeout_seconds` | Phase 3, 4 |
| **position_mismatch** | `abs(target - actual) > mismatch_threshold` (default 5°) | Phase 3, 4 |
| **unexpected_resistance** | Force/torque feedback (if available) indicates resistance | Phase 3, 4 |
| **camera_hazard** | Vision system identifies object in arm path | Phase 2, 3, 4 |
| **operator_stop** | Operator sends stop signal via API or UI | Phase 2, 3, 4 |
| **estop_not_ok** | `estop_ok == False` at any check point | Phase 3, 4 |
| **motion_not_allowed** | `motion_allowed == False` before or during dispatch | Phase 3, 4 |
| **simulation_unsafe** | Linked simulation_outcome == "unsafe" | Phase 1, 2, 3, 4 |
| **unstable_region_entry** | Target angle falls within a known `unstable_regions` span | Phase 3, 4 |

---

## 5. Persistence Plan

### 5.1 Tables / Models

| Table | Purpose |
|---|---|
| `arm_servo_envelope` | Per-servo envelope record (one per servo per arm instance) |
| `arm_envelope_probe_attempt` | Audit log of every probe step |

### 5.2 Artifact Files

| File | Purpose |
|---|---|
| `runtime/reports/mim_arm_envelope_summary.latest.json` | Current envelope snapshot for all servos |
| `runtime/reports/mim_arm_envelope_dry_run.latest.json` | Phase 2 dry-run command plan |
| `runtime/reports/mim_arm_safe_envelope_learning_plan.latest.json` | This plan in machine-readable form |

### 5.3 Linkage

- `ArmEnvelopeProbeAttempt.simulation_id` → `WorkspaceReachSimulation.id`
- `ArmEnvelopeProbeAttempt.envelope_id` → `ArmServoEnvelope.id`
- Simulation gate in `_autonomy_simulation_safe()` to be extended: if a servo envelope exists with `confidence < 0.4`, simulation confidence is downgraded to `"uncertain"`.

---

## 6. Integration with Objective 171 Safe Reach Simulation

### 6.1 Envelope-Aware Simulation

Once `ArmServoEnvelope` records exist, `safe_reach_simulation_service.compute_reachability()` must:

1. Load the envelope for the target servo(s).
2. If `is_stale = True`: treat as `stale_envelope` → degrade simulation outcome to `"uncertain"`, recovery_action = `"reverify_envelope"`.
3. If `confidence < 0.4`: treat as `low_confidence_envelope` → degrade simulation outcome to `"uncertain"`, recovery_action = `"probe_envelope"`.
4. If target angle is inside an `unstable_regions` span: treat as `unsafe_zone_equivalent` → outcome = `"unsafe"`, recovery_action = `"reobserve"`.
5. If `learned_soft_min` / `learned_soft_max` tighter than configured: use learned limits for reachability bounds.

### 6.2 Simulation Outcome Propagation

| Envelope State | Simulation Effect |
|---|---|
| `None` (no envelope yet) | No impact; use configured limits |
| Fresh, confidence ≥ 0.7 | Use `learned_soft_min`/`learned_soft_max` as primary bounds |
| Fresh, confidence 0.4–0.7 | Use learned bounds with `uncertain` flag |
| Stale | Degrade to `uncertain`; recovery = `reverify_envelope` |
| confidence < 0.4 | Degrade to `uncertain`; recovery = `probe_envelope` |
| Target in `unstable_regions` | Force `unsafe`; recovery = `reobserve` |

### 6.3 gate_passed Propagation

`simulation_gate_passed` (in `WorkspaceActionPlan.trigger_json`) must also encode envelope state:

```json
{
  "simulation_gate_passed": true,
  "envelope_confidence": 0.82,
  "envelope_state": "verified",
  "envelope_check_at": "2026-05-06T12:00:00Z"
}
```

---

## 7. Implementation Sequence

| Step | Scope | Gate |
|---|---|---|
| 7.1 | Add `ArmServoEnvelope` model and `ArmEnvelopeProbeAttempt` model to `core/models.py` | No hardware |
| 7.2 | Create `core/arm_envelope_service.py` — init from configured limits, query, update | No hardware |
| 7.3 | Create `POST /arm/envelope/initialize` endpoint — seed envelopes from `MIM_ARM_SERVO_LIMITS` | No hardware |
| 7.4 | Integrate envelope lookup into `safe_reach_simulation_service.compute_reachability()` | No hardware |
| 7.5 | Create `POST /arm/envelope/probe/dry_run` — generate Phase 2 plan; write artifact | No hardware |
| 7.6 | Create `POST /arm/envelope/probe/supervised` — Phase 3 micro-step probe with all gates | Operator auth required |
| 7.7 | Create `POST /arm/envelope/probe/autonomous` — Phase 4 autonomous probe session | MIM + operator auth |
| 7.8 | Write integration tests for steps 7.1–7.5 (simulation, initialization, dry run) | No hardware |
| 7.9 | Write supervised/autonomous probe tests against mock arm host | No hardware |

---

## 8. Acceptance Criteria (for objective-172 implementation)

- [ ] `ArmServoEnvelope` model persists configured limits and learned limits separately.
- [ ] Envelope initialization from `MIM_ARM_SERVO_LIMITS` works without hardware.
- [ ] `safe_reach_simulation_service` uses learned envelope when available.
- [ ] Stale envelope degrades simulation to `uncertain`.
- [ ] Low-confidence envelope degrades simulation to `uncertain`.
- [ ] Unstable region entry in target angle forces simulation outcome to `unsafe`.
- [ ] Phase 2 dry-run plan is generated and written as artifact without hardware.
- [ ] All stop conditions are checked before and after each probe step.
- [ ] Phase 3 requires `estop_ok = True`, `motion_allowed = True`, `tod_execution_allowed = True`.
- [ ] No physical probe dispatched without explicit operator gate.
- [ ] `ArmEnvelopeProbeAttempt` is written for every probe, including stopped attempts.
- [ ] Integration tests cover phases 1–2 and envelope-aware simulation; no live hardware path invoked.

---

## 9. Open Questions

| ID | Question | Resolution |
|---|---|---|
| OQ-1 | Does the physical arm report current draw or torque per servo? If not, `current_spike` and `unexpected_resistance` must be simulated or excluded from Phase 3. | To be confirmed from arm host API response at `http://192.168.1.90:5000/arm_state`. |
| OQ-2 | Is micro-step increment of 5° safe for all servos, or should shoulder/elbow use smaller steps? | Default to 3° for servo 1 (shoulder) based on narrow configured range (15°–165°). |
| OQ-3 | Should envelope staleness trigger an operator alert or a silent simulation degradation? | Both: silent degradation in simulation + operator alert surfaced via mim_ui. |
| OQ-4 | Should `arm_id` be derived from arm host IP or a configured identity string? | Use arm host IP from status probe URL as default; allow override via config. |
