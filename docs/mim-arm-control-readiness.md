# MIM Arm Control Readiness

Use this checklist before granting direct control from MIM to mim_arm.

## 1. Safety Contract

- Define allowed action types and blocked action types.
- Define max velocity, acceleration, and workspace limits.
- Define collision policy and force thresholds.
- Define emergency stop trigger sources (software + hardware).
- Define automatic fail-safe state (safe idle pose / motor disable).

## 2. Control Authority Plan

- Stage 0: Read-only perception and recommendation.
- Stage 1: Simulated control only.
- Stage 2: Human-confirmed live control.
- Stage 3: Bounded autonomous control in approved zones.
- Stage 4: Full autonomous control only after sustained stability.

## 3. Required Interfaces

- Command API schema (input validation + unit normalization).
- State feedback stream (pose, velocity, current, faults).
- Safety state stream (interlock, estop, watchdog).
- Intent trace linkage from MIM action to arm command.

## 4. Governance and Audit

- Every control decision has reason, confidence, and source policy.
- Every command has trace_id and operator context.
- Every override is logged with actor + timestamp.
- Drift detection alarms on unusual command patterns.

## 5. Promotion Gates

- Simulation pass rate threshold (recommended >= 99%).
- No critical safety violations in soak run.
- Latency budget validated under expected load.
- Recovery behavior validated for communication drop.

## 6. Immediate Next Inputs Needed

- arm hardware model + firmware/API details
- command transport method (ROS2, REST, gRPC, serial)
- kinematic limits and safe zones
- estop wiring and software integration points
- initial task set for Stage 1 simulation
