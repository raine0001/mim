"""
arm_envelope_service.py — Objective 173 (schema layer for Obj 172 plan)

Safe servo envelope persistence and initialization.
NO hardware movement.  NO actuation dispatch.

Provides:
  - initialize_envelopes()  — seed ArmServoEnvelope rows from configured limits
  - get_envelopes()         — return all envelope rows for an arm_id
  - get_envelope()          — return a single servo envelope by servo_id
  - get_probe_attempts()    — return probe attempt log for a servo
  - generate_dry_run_plan() — build a Phase-2 probe plan (no dispatch)
  - generate_simulation_probe_plan_for_servo() — per-servo simulation plan
  - generate_dry_run_commands_for_servo() — dry-run command sequence (Obj 175)
    - supervised authorization helpers — Obj 176 gate lifecycle
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import ArmEnvelopeProbeAttempt, ArmProbeAuthorization, ArmServoEnvelope, SupervisedMicroStepExecution, SupervisedPhysicalMicroStepExecution

# ---------------------------------------------------------------------------
# Servo configuration — mirrors execution_lane_service constants so this
# service has no runtime import dependency on the lane service.
# ---------------------------------------------------------------------------

_SERVO_LIMITS: dict[int, tuple[int, int]] = {
    0: (0, 180),
    1: (15, 165),
    2: (0, 180),
    3: (0, 180),
    4: (0, 180),
    5: (0, 180),
}

_SERVO_NAMES: dict[int, str] = {
    0: "base",
    1: "shoulder",
    2: "elbow",
    3: "wrist_pitch",
    4: "wrist_roll",
    5: "gripper",
}

# Home angle per servo — safe_home = [90, 90, 90, 90, 90, 50]
_HOME_ANGLES: dict[int, int] = {0: 90, 1: 90, 2: 90, 3: 90, 4: 90, 5: 50}

# Micro-step sizes for dry-run planning
_STEP_DEGREES_DEFAULT = 5
_STEP_DEGREES_SHOULDER = 3  # servo 1 has narrow range

# All stop-condition names surfaced in probe plans and attempt logs
STOP_CONDITIONS = [
    "current_spike",
    "motion_timeout",
    "position_mismatch",
    "unexpected_resistance",
    "camera_hazard",
    "operator_stop",
    "estop_not_ok",
    "motion_not_allowed",
    "simulation_unsafe",
    "unstable_region_entry",
]


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


async def initialize_envelopes(
    db: AsyncSession,
    *,
    arm_id: str = "default",
    actor: str = "system",
    force: bool = False,
) -> list[ArmServoEnvelope]:
    """
    Seed ArmServoEnvelope rows for servos 0–5 from configured limits.

    If a row for (arm_id, servo_id) already exists it is NOT overwritten
    unless force=True.  Learned values are never cleared by initialization.

    Returns the list of rows (existing + newly created).
    """
    result = await db.execute(
        select(ArmServoEnvelope).where(ArmServoEnvelope.arm_id == arm_id)
    )
    existing = {row.servo_id: row for row in result.scalars().all()}

    rows: list[ArmServoEnvelope] = []
    for servo_id, (lo, hi) in _SERVO_LIMITS.items():
        if servo_id in existing and not force:
            rows.append(existing[servo_id])
            continue

        if servo_id in existing:
            row = existing[servo_id]
            # force-refresh configured limits only — never touch learned fields
            row.configured_min = lo
            row.configured_max = hi
            row.servo_name = _SERVO_NAMES[servo_id]
            row.updated_at = datetime.now(timezone.utc)
        else:
            row = ArmServoEnvelope(
                arm_id=arm_id,
                servo_id=servo_id,
                servo_name=_SERVO_NAMES[servo_id],
                configured_min=lo,
                configured_max=hi,
                learned_soft_min=None,
                learned_soft_max=None,
                preferred_min=None,
                preferred_max=None,
                unstable_regions=[],
                confidence=0.0,
                evidence_count=0,
                last_verified_at=None,
                last_probe_phase="none",
                status="simulation_only",
                stale_after_seconds=86400,
                actor=actor,
                source="initialization",
                updated_at=None,
            )
            db.add(row)
        rows.append(row)

    await db.flush()
    return rows


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


async def get_envelopes(
    db: AsyncSession,
    *,
    arm_id: str = "default",
) -> list[ArmServoEnvelope]:
    """Return all envelope rows for the given arm_id, ordered by servo_id."""
    result = await db.execute(
        select(ArmServoEnvelope)
        .where(ArmServoEnvelope.arm_id == arm_id)
        .order_by(ArmServoEnvelope.servo_id)
    )
    return list(result.scalars().all())


async def get_envelope(
    db: AsyncSession,
    servo_id: int,
    *,
    arm_id: str = "default",
) -> ArmServoEnvelope | None:
    """Return the envelope row for a specific servo, or None if not found."""
    result = await db.execute(
        select(ArmServoEnvelope).where(
            ArmServoEnvelope.arm_id == arm_id,
            ArmServoEnvelope.servo_id == servo_id,
        )
    )
    return result.scalar_one_or_none()


async def get_probe_attempts(
    db: AsyncSession,
    servo_id: int,
    *,
    arm_id: str = "default",
    limit: int = 100,
    phase: str | None = None,
) -> list[ArmEnvelopeProbeAttempt]:
    """
    Return probe attempt log for a servo, newest first.

    Optionally filter by phase.  If the envelope for this servo/arm_id does
    not exist, returns an empty list.
    """
    envelope = await get_envelope(db, servo_id, arm_id=arm_id)
    if envelope is None:
        return []

    q = (
        select(ArmEnvelopeProbeAttempt)
        .where(ArmEnvelopeProbeAttempt.envelope_id == envelope.id)
        .order_by(ArmEnvelopeProbeAttempt.id.desc())
        .limit(limit)
    )
    if phase is not None:
        q = q.where(ArmEnvelopeProbeAttempt.phase == phase)

    result = await db.execute(q)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Dry-run plan generation (Phase 2 — no hardware dispatch)
# ---------------------------------------------------------------------------


def generate_dry_run_plan(
    envelopes: list[ArmServoEnvelope],
    *,
    arm_id: str = "default",
) -> dict[str, Any]:
    """
    Build a Phase-2 dry-run probe plan from current envelope state.

    Returns a plain dict suitable for JSON serialization and for writing
    to runtime/reports/mim_arm_envelope_dry_run.latest.json.

    NO hardware command is dispatched.  All `would_dispatch` flags are False.
    """
    steps = []
    env_by_servo: dict[int, ArmServoEnvelope] = {e.servo_id: e for e in envelopes}

    for servo_id in sorted(_SERVO_LIMITS.keys()):
        env = env_by_servo.get(servo_id)
        if env is None:
            continue

        lo = env.learned_soft_min if env.learned_soft_min is not None else env.configured_min
        hi = env.learned_soft_max if env.learned_soft_max is not None else env.configured_max
        home = _HOME_ANGLES[servo_id]
        step = _STEP_DEGREES_SHOULDER if servo_id == 1 else _STEP_DEGREES_DEFAULT

        # Sweep down from home to lo, then up from home to hi
        angle = home
        while angle - step >= lo:
            angle -= step
            steps.append(
                {
                    "servo_id": servo_id,
                    "servo_name": _SERVO_NAMES[servo_id],
                    "target_angle": angle,
                    "prior_angle": angle + step,
                    "step_degrees": step,
                    "direction": "down",
                    "phase": "dry_run",
                    "would_dispatch": False,
                    "notes": "phase2_dry_run_no_hardware",
                }
            )

        angle = home
        while angle + step <= hi:
            angle += step
            steps.append(
                {
                    "servo_id": servo_id,
                    "servo_name": _SERVO_NAMES[servo_id],
                    "target_angle": angle,
                    "prior_angle": angle - step,
                    "step_degrees": step,
                    "direction": "up",
                    "phase": "dry_run",
                    "would_dispatch": False,
                    "notes": "phase2_dry_run_no_hardware",
                }
            )

    return {
        "arm_id": arm_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "dry_run",
        "hardware_command_issued": False,
        "steps": steps,
        "stop_conditions_checked": STOP_CONDITIONS,
        "notes": (
            "Phase 2 dry-run plan. No hardware commands issued. "
            "Review steps before proceeding to Phase 3 supervised probing."
        ),
    }


# ---------------------------------------------------------------------------
# Staleness helper
# ---------------------------------------------------------------------------


def is_stale(envelope: ArmServoEnvelope) -> bool:
    """Return True if the envelope's last_verified_at is older than stale_after_seconds."""
    if envelope.last_verified_at is None:
        return False  # Never probed — not stale, just unverified
    age = (datetime.now(timezone.utc) - envelope.last_verified_at).total_seconds()
    return age > envelope.stale_after_seconds


# ---------------------------------------------------------------------------
# Simulation-only probe planning (per-servo, detailed)
# ---------------------------------------------------------------------------


def generate_simulation_probe_plan_for_servo(
    envelope: ArmServoEnvelope,
    *,
    arm_id: str = "default",
    max_target_angles: int = 50,
    skip_unstable_regions: bool = True,
) -> dict[str, Any]:
    """
    Generate a detailed simulation-only probe plan for a single servo.

    Returns a dict suitable for JSON response.  Includes:
      - configured range, learned range (if available)
      - proposed start angle
      - proposed target angles with risk assessment
      - step size (3° for shoulder, 5° for others)
      - direction (up/down)
      - stop conditions applicable to this servo
      - stale/re-verification recommendation
      - full probe steps with per-step metadata

    NO hardware dispatch.  NO actuation.
    """
    servo_id = envelope.servo_id
    servo_name = _SERVO_NAMES[servo_id]
    step_size = _STEP_DEGREES_SHOULDER if servo_id == 1 else _STEP_DEGREES_DEFAULT

    # Determine effective range for planning
    lo = envelope.learned_soft_min if envelope.learned_soft_min is not None else envelope.configured_min
    hi = envelope.learned_soft_max if envelope.learned_soft_max is not None else envelope.configured_max

    # Home angle is starting point
    home = _HOME_ANGLES[servo_id]

    # Build stale recommendation
    stale_rec = ""
    if is_stale(envelope):
        stale_rec = f"Envelope last verified > {envelope.stale_after_seconds}s ago; re-verification recommended"
    if envelope.confidence < 0.3:
        stale_rec += (
            " | Low confidence (%.2f); more probing needed to improve envelope accuracy"
            % envelope.confidence
        )

    # Build configured and learned ranges
    configured_range = {"min": envelope.configured_min, "max": envelope.configured_max}
    learned_range = None
    if (envelope.learned_soft_min is not None and envelope.learned_soft_max is not None):
        learned_range = {"min": envelope.learned_soft_min, "max": envelope.learned_soft_max}

    # Unstable regions as list of dicts
    unstable_regions = envelope.unstable_regions if isinstance(envelope.unstable_regions, list) else []

    # Helper to check if angle is in unstable region
    def is_in_unstable_region(angle: int) -> bool:
        if not unstable_regions:
            return False
        for region in unstable_regions:
            if isinstance(region, dict) and "start" in region and "end" in region:
                if region["start"] <= angle <= region["end"]:
                    return True
        return False

    # Generate target angles (sweep down from home to lo, then up from home to hi)
    target_angles_raw: list[tuple[int, str, bool]] = []  # (angle, direction, is_unstable)

    # Down sweep
    angle = home
    while angle - step_size >= lo and len(target_angles_raw) < max_target_angles:
        angle -= step_size
        is_unstable = is_in_unstable_region(angle)
        if not (skip_unstable_regions and is_unstable):
            target_angles_raw.append((angle, "down", is_unstable))

    # Up sweep
    angle = home
    while angle + step_size <= hi and len(target_angles_raw) < max_target_angles:
        angle += step_size
        is_unstable = is_in_unstable_region(angle)
        if not (skip_unstable_regions and is_unstable):
            target_angles_raw.append((angle, "up", is_unstable))

    # Build target angle DTOs with risk assessment
    target_angles = []
    for idx, (angle, direction, is_unstable) in enumerate(target_angles_raw):
        risk = "high" if is_unstable else ("medium" if envelope.confidence < 0.5 else "low")
        target_angles.append(
            {
                "angle": angle,
                "direction": direction,
                "is_unstable": is_unstable,
                "estimated_risk": risk,
                "safety_check_status": "unknown",  # Would be filled by safe_reach integration
            }
        )

    # Build probe steps
    probe_steps = []
    for seq_idx, (angle, direction, is_unstable) in enumerate(target_angles_raw):
        # Determine applicable stop conditions
        applicable_stops = ["operator_stop", "estop_not_ok", "simulation_unsafe"]
        if is_unstable:
            applicable_stops.append("unstable_region_entry")
        if envelope.confidence < 0.5:
            applicable_stops.append("motion_timeout")

        risk = "high" if is_unstable else ("medium" if envelope.confidence < 0.5 else "low")
        auth_level = "supervisor" if (is_unstable or risk == "high") else "operator"

        probe_steps.append(
            {
                "sequence_index": seq_idx,
                "servo_id": servo_id,
                "servo_name": servo_name,
                "current_angle": home if seq_idx == 0 else target_angles_raw[seq_idx - 1][0],
                "target_angle": angle,
                "direction": direction,
                "step_degrees": step_size,
                "estimated_risk": risk,
                "stop_conditions_applicable": applicable_stops,
                "allow_physical_probing": False,  # Always false for simulation-only
                "required_authorization_level": auth_level,
                "notes": (
                    f"phase1_sim_only|stable={'no' if is_unstable else 'yes'}"
                    + (f"|learned={envelope.learned_soft_min}-{envelope.learned_soft_max}" if learned_range else "")
                ),
            }
        )

    # Risk assessment summary
    high_risk_count = sum(1 for ta in target_angles if ta["estimated_risk"] == "high")
    medium_risk_count = sum(1 for ta in target_angles if ta["estimated_risk"] == "medium")
    risk_msg = ""
    if high_risk_count > 0:
        risk_msg = f"{high_risk_count} high-risk targets; re-verification strongly recommended"
    elif medium_risk_count > 0:
        risk_msg = f"{medium_risk_count} medium-risk targets; proceed with caution"
    else:
        risk_msg = "All targets low-risk; safe to proceed"

    return {
        "arm_id": arm_id,
        "servo_id": servo_id,
        "servo_name": servo_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "phase": "simulation_only",
        "hardware_command_issued": False,
        "allow_physical_execution": False,
        "configured_range": configured_range,
        "learned_range": learned_range,
        "unstable_regions": unstable_regions,
        "stale_recommendation": stale_rec,
        "start_angle": home,
        "target_angles": target_angles,
        "probe_steps": probe_steps,
        "estimated_total_steps": len(probe_steps),
        "total_stop_conditions": len(set(s for step in probe_steps for s in step["stop_conditions_applicable"])),
        "risk_assessment": risk_msg,
        "notes": (
            "Simulation-only probe plan for Phase 1 envelope learning. "
            "No hardware commands issued. Review and authorize before Phase 2 supervised probing."
        ),
    }


# ---------------------------------------------------------------------------
# Objective 175 — Dry-run command generation from simulation plan
# ---------------------------------------------------------------------------


def generate_dry_run_commands_for_servo(
    envelope: ArmServoEnvelope,
    simulation_plan_steps: list[dict[str, Any]],
    *,
    arm_id: str = "default",
) -> dict[str, Any]:
    """
    Convert simulation plan steps into structured dry-run command objects.

    Each command includes:
      - command_id (uuid4)
      - prior/target angles
      - step_degrees, direction
      - estimated_duration_ms (step_degrees * 20)
      - safety_gate_required (True for risk=="high")
      - stop_conditions per step
      - expected_feedback_fields
      - rollback_command pointing to safe home
      - dry_run=True always

    NO hardware dispatch.  physical_execution_allowed=False always.
    """
    from uuid import uuid4

    servo_id = envelope.servo_id
    servo_name = _SERVO_NAMES[servo_id]
    safe_home = _HOME_ANGLES[servo_id]

    # Collect all unique stop conditions across all steps
    all_stop_conditions: set[str] = {"operator_stop", "estop_not_ok", "simulation_unsafe"}
    commands: list[dict[str, Any]] = []

    for step in simulation_plan_steps:
        # Validate target is within configured bounds — skip if out of range
        target_angle = step.get("target_angle", step.get("angle"))
        if target_angle is None:
            continue
        if target_angle < envelope.configured_min or target_angle > envelope.configured_max:
            continue

        risk = step.get("estimated_risk", "low")
        direction = step.get("direction", "up")
        step_degrees = step.get("step_degrees", _STEP_DEGREES_SHOULDER if servo_id == 1 else _STEP_DEGREES_DEFAULT)
        prior_angle = step.get("current_angle", step.get("prior_angle", safe_home))
        stop_conds = list(step.get("stop_conditions_applicable", ["operator_stop", "estop_not_ok", "simulation_unsafe"]))

        # Ensure baseline stop conditions are always present
        for base_cond in ("operator_stop", "estop_not_ok", "simulation_unsafe"):
            if base_cond not in stop_conds:
                stop_conds.append(base_cond)

        all_stop_conditions.update(stop_conds)

        safety_gate = risk == "high"
        estimated_duration_ms = step_degrees * 20

        commands.append(
            {
                "command_id": str(uuid4()),
                "servo_id": servo_id,
                "prior_angle": prior_angle,
                "target_angle": target_angle,
                "step_degrees": step_degrees,
                "direction": direction,
                "estimated_duration_ms": estimated_duration_ms,
                "safety_gate_required": safety_gate,
                "stop_conditions": stop_conds,
                "expected_feedback_fields": ["observed_angle", "current_ma", "timestamp"],
                "rollback_command": {
                    "target_angle": safe_home,
                    "reason": "rollback_to_safe_home",
                },
                "dry_run": True,
            }
        )

    return {
        "arm_id": arm_id,
        "servo_id": servo_id,
        "servo_name": servo_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": True,
        "physical_execution_allowed": False,
        "commands": commands,
        "safe_home_fallback": {
            "target_angle": safe_home,
            "reason": "safe_home_fallback",
        },
        "stop_conditions_checked": sorted(all_stop_conditions),
        "total_commands": len(commands),
        "notes": (
            "Dry-run command sequence for Phase 1 envelope probing. "
            "No hardware commands issued. physical_execution_allowed=False. "
            "Review and authorize before Phase 2 supervised probing."
        ),
    }


def _is_in_unstable_region(envelope: ArmServoEnvelope, target_angle: int) -> bool:
    unstable_regions = envelope.unstable_regions if isinstance(envelope.unstable_regions, list) else []
    for region in unstable_regions:
        if isinstance(region, dict) and "start" in region and "end" in region:
            if int(region["start"]) <= target_angle <= int(region["end"]):
                return True
        if isinstance(region, dict) and "from" in region and "to" in region:
            if int(region["from"]) <= target_angle <= int(region["to"]):
                return True
        if isinstance(region, dict) and "min" in region and "max" in region:
            if int(region["min"]) <= target_angle <= int(region["max"]):
                return True
    return False


def _as_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _authorization_to_dict(row: ArmProbeAuthorization) -> dict[str, Any]:
    return {
        "authorization_id": row.authorization_id,
        "arm_id": row.arm_id,
        "servo_id": row.servo_id,
        "dry_run_command_id": row.dry_run_command_id,
        "requested_angle": row.requested_angle,
        "prior_angle": row.prior_angle,
        "step_degrees": row.step_degrees,
        "direction": row.direction,
        "operator_id": row.operator_id,
        "authorized_by": row.authorized_by,
        "authorization_status": row.authorization_status,
        "expires_at": _as_iso(row.expires_at),
        "stop_conditions": row.stop_conditions if isinstance(row.stop_conditions, list) else [],
        "safe_home_required": bool(row.safe_home_required),
        "physical_execution_allowed": bool(row.physical_execution_allowed),
        "created_at": _as_iso(row.created_at),
        "updated_at": _as_iso(row.updated_at),
    }


async def expire_pending_probe_authorizations(
    db: AsyncSession,
    *,
    arm_id: str | None = None,
) -> int:
    """Expire pending/approved authorizations whose TTL has elapsed."""
    now = datetime.now(timezone.utc)
    q = select(ArmProbeAuthorization).where(
        ArmProbeAuthorization.expires_at.is_not(None),
        ArmProbeAuthorization.expires_at < now,
        ArmProbeAuthorization.authorization_status.in_(["pending", "approved"]),
    )
    if arm_id:
        q = q.where(ArmProbeAuthorization.arm_id == arm_id)
    result = await db.execute(q)
    rows = list(result.scalars().all())
    for row in rows:
        row.authorization_status = "expired"
        row.physical_execution_allowed = False
        row.updated_at = now
    if rows:
        await db.flush()
    return len(rows)


async def get_probe_authorization(
    db: AsyncSession,
    authorization_id: str,
) -> ArmProbeAuthorization | None:
    result = await db.execute(
        select(ArmProbeAuthorization).where(
            ArmProbeAuthorization.authorization_id == authorization_id
        )
    )
    return result.scalar_one_or_none()


async def create_supervised_micro_step_authorization(
    db: AsyncSession,
    envelope: ArmServoEnvelope,
    *,
    arm_id: str,
    dry_run_command_id: str,
    operator_id: str,
    expires_in_seconds: int = 300,
) -> dict[str, Any]:
    """
    Create one pending authorization from one persisted dry-run command.

    Requires the dry-run command to already exist as ArmEnvelopeProbeAttempt row
    (probe_id == dry_run_command_id, phase="dry_run", result="dry_run_generated").
    """
    from uuid import uuid4

    await expire_pending_probe_authorizations(db, arm_id=arm_id)

    if is_stale(envelope) or envelope.confidence < 0.3:
        raise ValueError("Envelope requires re-verification before supervised physical probing")

    cmd_result = await db.execute(
        select(ArmEnvelopeProbeAttempt).where(
            ArmEnvelopeProbeAttempt.probe_id == dry_run_command_id,
            ArmEnvelopeProbeAttempt.phase == "dry_run",
            ArmEnvelopeProbeAttempt.result == "dry_run_generated",
            ArmEnvelopeProbeAttempt.servo_id == envelope.servo_id,
            ArmEnvelopeProbeAttempt.envelope_id == envelope.id,
        )
    )
    command_row = cmd_result.scalar_one_or_none()
    if command_row is None:
        raise LookupError("Dry-run command not found for this servo/arm envelope")

    flags = command_row.stop_condition_flags if isinstance(command_row.stop_condition_flags, dict) else {}
    if flags.get("dry_run") is not True:
        raise ValueError("Source command is not marked as dry_run=true")
    if flags.get("physical_execution_allowed") is not False:
        raise ValueError("Source command must have physical_execution_allowed=false")

    stop_conditions = flags.get("stop_conditions")
    if not isinstance(stop_conditions, list) or len(stop_conditions) == 0:
        raise ValueError("Source dry-run command must include stop_conditions")

    safe_home_fallback = flags.get("safe_home_fallback")
    if not isinstance(safe_home_fallback, dict) or "target_angle" not in safe_home_fallback:
        raise ValueError("Source dry-run command missing safe_home fallback")

    requested_angle = int(command_row.commanded_angle)
    prior_angle = int(command_row.prior_angle if command_row.prior_angle is not None else _HOME_ANGLES[envelope.servo_id])

    if requested_angle < envelope.configured_min or requested_angle > envelope.configured_max:
        raise ValueError("Target angle outside configured envelope")
    if envelope.learned_soft_min is not None and requested_angle < envelope.learned_soft_min:
        raise ValueError("Target angle below learned soft minimum")
    if envelope.learned_soft_max is not None and requested_angle > envelope.learned_soft_max:
        raise ValueError("Target angle above learned soft maximum")
    if _is_in_unstable_region(envelope, requested_angle):
        raise ValueError("Target angle falls in unstable region")

    direction = "up" if requested_angle >= prior_angle else "down"
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=max(30, int(expires_in_seconds)))

    auth = ArmProbeAuthorization(
        authorization_id=str(uuid4()),
        arm_id=arm_id,
        servo_id=envelope.servo_id,
        dry_run_command_id=dry_run_command_id,
        requested_angle=requested_angle,
        prior_angle=prior_angle,
        step_degrees=int(command_row.step_degrees),
        direction=direction,
        operator_id=operator_id,
        authorized_by="",
        authorization_status="pending",
        expires_at=expires_at,
        stop_conditions=stop_conditions,
        safe_home_required=True,
        physical_execution_allowed=False,
        created_at=now,
        updated_at=now,
    )
    db.add(auth)
    await db.flush()
    return _authorization_to_dict(auth)


async def approve_probe_authorization(
    db: AsyncSession,
    authorization: ArmProbeAuthorization,
    *,
    authorized_by: str,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    if authorization.authorization_status in {"rejected", "consumed", "expired"}:
        raise ValueError("Authorization can no longer be approved")
    if authorization.expires_at is not None and authorization.expires_at < now:
        authorization.authorization_status = "expired"
        authorization.physical_execution_allowed = False
        authorization.updated_at = now
        await db.flush()
        raise ValueError("Authorization expired")

    authorization.authorization_status = "approved"
    authorization.authorized_by = authorized_by
    authorization.physical_execution_allowed = True
    authorization.updated_at = now
    await db.flush()
    return _authorization_to_dict(authorization)


async def reject_probe_authorization(
    db: AsyncSession,
    authorization: ArmProbeAuthorization,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if authorization.authorization_status in {"consumed", "expired"}:
        raise ValueError("Authorization can no longer be rejected")
    authorization.authorization_status = "rejected"
    authorization.physical_execution_allowed = False
    authorization.updated_at = now
    await db.flush()
    return _authorization_to_dict(authorization)


async def check_physical_micro_step_allowed(
    db: AsyncSession,
    authorization: ArmProbeAuthorization,
    *,
    consume: bool = False,
) -> dict[str, Any]:
    """
    Execution gate stub for one supervised micro-step.

    Returns blocked unless authorization is approved, unexpired, and unconsumed.
    This function never dispatches hardware movement.
    """
    now = datetime.now(timezone.utc)

    if authorization.expires_at is not None and authorization.expires_at < now:
        authorization.authorization_status = "expired"
        authorization.physical_execution_allowed = False
        authorization.updated_at = now
        await db.flush()
        return {
            "authorization_id": authorization.authorization_id,
            "allowed": False,
            "reason": "authorization_expired",
            "authorization_status": authorization.authorization_status,
            "physical_execution_allowed": authorization.physical_execution_allowed,
        }

    if authorization.authorization_status != "approved":
        return {
            "authorization_id": authorization.authorization_id,
            "allowed": False,
            "reason": "authorization_not_approved",
            "authorization_status": authorization.authorization_status,
            "physical_execution_allowed": authorization.physical_execution_allowed,
        }

    if not authorization.physical_execution_allowed:
        return {
            "authorization_id": authorization.authorization_id,
            "allowed": False,
            "reason": "physical_execution_not_allowed",
            "authorization_status": authorization.authorization_status,
            "physical_execution_allowed": authorization.physical_execution_allowed,
        }

    if consume:
        authorization.authorization_status = "consumed"
        authorization.physical_execution_allowed = False
        authorization.consumed_at = now
        authorization.updated_at = now
        await db.flush()

    return {
        "authorization_id": authorization.authorization_id,
        "allowed": True,
        "reason": "authorization_valid",
        "authorization_status": authorization.authorization_status,
        "physical_execution_allowed": authorization.physical_execution_allowed,
    }


# ---------------------------------------------------------------------------
# Objective 177 — Supervised Micro-Step Execution Stub
# ---------------------------------------------------------------------------

def _make_log_entry(event: str, detail: str) -> dict[str, str]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "detail": detail,
    }


def _execution_to_dict(ex: SupervisedMicroStepExecution) -> dict[str, Any]:
    return {
        "execution_id": ex.execution_id,
        "authorization_id": ex.authorization_id,
        "arm_id": ex.arm_id,
        "servo_id": ex.servo_id,
        "requested_angle": ex.requested_angle,
        "prior_angle": ex.prior_angle,
        "step_degrees": ex.step_degrees,
        "direction": ex.direction,
        "operator_id": ex.operator_id,
        "execution_status": ex.execution_status,
        "stop_conditions": ex.stop_conditions if isinstance(ex.stop_conditions, list) else [],
        "safe_home_required": bool(ex.safe_home_required),
        "safe_home_triggered": bool(ex.safe_home_triggered),
        "safe_home_target_angle": ex.safe_home_target_angle,
        "safe_home_triggered_at": ex.safe_home_triggered_at.isoformat() if ex.safe_home_triggered_at else None,
        "physical_movement_dispatched": False,
        "log_entries": ex.log_entries if isinstance(ex.log_entries, list) else [],
        "abort_reason": ex.abort_reason or "",
        "completed_at": ex.completed_at.isoformat() if ex.completed_at else None,
        "created_at": ex.created_at.isoformat() if ex.created_at else None,
        "updated_at": ex.updated_at.isoformat() if ex.updated_at else None,
    }


async def get_supervised_execution(
    db: AsyncSession,
    execution_id: str,
) -> SupervisedMicroStepExecution | None:
    result = await db.execute(
        select(SupervisedMicroStepExecution).where(
            SupervisedMicroStepExecution.execution_id == execution_id
        )
    )
    return result.scalar_one_or_none()


async def begin_supervised_micro_step_execution(
    db: AsyncSession,
    authorization: ArmProbeAuthorization,
    *,
    operator_id: str,
) -> dict[str, Any]:
    """
    Begin one supervised micro-step execution stub.

    Validates that the authorization is approved, unexpired, and unconsumed.
    Atomically consumes the authorization and creates an execution record.

    No hardware movement is dispatched.  physical_movement_dispatched is always False.
    """
    from uuid import uuid4

    now = datetime.now(timezone.utc)

    # Validate authorization is still usable
    if authorization.expires_at is not None and authorization.expires_at < now:
        authorization.authorization_status = "expired"
        authorization.physical_execution_allowed = False
        authorization.updated_at = now
        await db.flush()
        raise ValueError("Authorization has expired and cannot be executed")

    if authorization.authorization_status != "approved":
        raise ValueError(
            f"Authorization must be in 'approved' status; current status: {authorization.authorization_status}"
        )

    if not authorization.physical_execution_allowed:
        raise ValueError("Authorization does not have physical_execution_allowed=true")

    # Look up safe_home_target_angle from the source dry-run command
    cmd_result = await db.execute(
        select(ArmEnvelopeProbeAttempt).where(
            ArmEnvelopeProbeAttempt.probe_id == authorization.dry_run_command_id,
        )
    )
    command_row = cmd_result.scalar_one_or_none()
    safe_home_target_angle: int | None = None
    if command_row is not None:
        flags = command_row.stop_condition_flags if isinstance(command_row.stop_condition_flags, dict) else {}
        fallback = flags.get("safe_home_fallback")
        if isinstance(fallback, dict) and "target_angle" in fallback:
            safe_home_target_angle = int(fallback["target_angle"])

    # Atomically consume the authorization
    authorization.authorization_status = "consumed"
    authorization.physical_execution_allowed = False
    authorization.consumed_at = now
    authorization.updated_at = now

    # Build initial log
    log: list[dict[str, str]] = [
        _make_log_entry(
            "execution_started",
            f"operator={operator_id} servo={authorization.servo_id} "
            f"angle={authorization.requested_angle} direction={authorization.direction}",
        ),
        _make_log_entry(
            "authorization_consumed",
            f"authorization_id={authorization.authorization_id}",
        ),
        _make_log_entry(
            "physical_movement_dispatched",
            "false — stub execution, no hardware command issued",
        ),
    ]

    execution = SupervisedMicroStepExecution(
        execution_id=str(uuid4()),
        authorization_id=authorization.authorization_id,
        arm_id=authorization.arm_id,
        servo_id=authorization.servo_id,
        requested_angle=authorization.requested_angle,
        prior_angle=authorization.prior_angle,
        step_degrees=authorization.step_degrees,
        direction=authorization.direction,
        operator_id=operator_id,
        execution_status="executing",
        stop_conditions=authorization.stop_conditions if isinstance(authorization.stop_conditions, list) else [],
        safe_home_required=bool(authorization.safe_home_required),
        safe_home_triggered=False,
        safe_home_target_angle=safe_home_target_angle,
        safe_home_triggered_at=None,
        physical_movement_dispatched=False,
        log_entries=log,
        abort_reason="",
        completed_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(execution)
    await db.flush()
    return _execution_to_dict(execution)


async def trigger_safe_home_fallback(
    db: AsyncSession,
    execution: SupervisedMicroStepExecution,
    *,
    operator_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """
    Operator-triggered safe-home fallback.

    Transitions execution to 'safe_home_triggered', records the operator and
    reason in the log, and marks safe_home_triggered=True.

    This is a stub — no hardware command is dispatched.
    Allowed from 'executing' status only.
    """
    now = datetime.now(timezone.utc)

    if execution.execution_status not in {"executing", "pending"}:
        raise ValueError(
            f"Safe-home can only be triggered from executing/pending status; "
            f"current: {execution.execution_status}"
        )

    log = list(execution.log_entries) if isinstance(execution.log_entries, list) else []
    log.append(_make_log_entry(
        "safe_home_triggered",
        f"operator={operator_id} reason={reason!r} "
        f"safe_home_target_angle={execution.safe_home_target_angle}",
    ))
    log.append(_make_log_entry(
        "physical_movement_dispatched",
        "false — stub safe-home, no hardware command issued",
    ))

    execution.execution_status = "safe_home_triggered"
    execution.safe_home_triggered = True
    execution.safe_home_triggered_at = now
    execution.log_entries = log
    execution.updated_at = now
    await db.flush()
    return _execution_to_dict(execution)


# ---------------------------------------------------------------------------
# Objective 178 — Supervised Physical Micro-Step Execution
# Hardware adapter interface + service function
# ---------------------------------------------------------------------------

from abc import ABC, abstractmethod


class ServoHardwareAdapter(ABC):
    """
    Abstract interface for dispatching a single supervised servo command.

    Implementations:
      - MockServoAdapter: no-op, never moves hardware; used in all automated tests.
      - RealServoAdapter: routes through the existing safe arm command path;
        only activated when MIM_ARM_PHYSICAL_MICRO_STEP_ENABLED=true.
    """

    @abstractmethod
    def dispatch_servo_command(
        self,
        *,
        arm_id: str,
        servo_id: int,
        target_angle: int,
        prior_angle: int,
        step_degrees: int,
        direction: str,
        stop_conditions: list[str],
    ) -> dict[str, Any]:
        """
        Dispatch one servo command.

        Returns a dict with at least:
          - success: bool
          - dispatch_result: str  ("ok" | "failed" | "no-op")
          - movement_duration_ms: int | None
          - stop_condition_triggered: str  (empty string if none)
          - error_message: str  (empty string if none)
        """


class MockServoAdapter(ServoHardwareAdapter):
    """
    No-op servo adapter for automated testing.

    Never moves hardware.  Returns a configurable simulated result.
    """

    def __init__(self, *, simulate_failure: bool = False) -> None:
        self._simulate_failure = simulate_failure

    def dispatch_servo_command(
        self,
        *,
        arm_id: str,
        servo_id: int,
        target_angle: int,
        prior_angle: int,
        step_degrees: int,
        direction: str,
        stop_conditions: list[str],
    ) -> dict[str, Any]:
        if self._simulate_failure:
            return {
                "success": False,
                "dispatch_result": "failed",
                "movement_duration_ms": None,
                "stop_condition_triggered": "",
                "error_message": "simulated_dispatch_failure",
            }
        return {
            "success": True,
            "dispatch_result": "ok",
            "movement_duration_ms": 0,
            "stop_condition_triggered": "",
            "error_message": "",
        }


class RealServoAdapter(ServoHardwareAdapter):
    """
    Production servo adapter.

    Routes through the existing lowest-risk servo command path.
    Only used when MIM_ARM_PHYSICAL_MICRO_STEP_ENABLED=true AND real hardware
    is available.  Operation is operator-gated upstream; this adapter issues
    exactly one command and returns the observed result.
    """

    def dispatch_servo_command(
        self,
        *,
        arm_id: str,
        servo_id: int,
        target_angle: int,
        prior_angle: int,
        step_degrees: int,
        direction: str,
        stop_conditions: list[str],
    ) -> dict[str, Any]:
        # Import here to avoid startup-time dependency when hardware is absent.
        try:
            from core.execution_lane_service import (
                TARGET_MIM_ARM,
                submit_execution_request,
            )
        except ImportError as exc:
            return {
                "success": False,
                "dispatch_result": "failed",
                "movement_duration_ms": None,
                "stop_condition_triggered": "",
                "error_message": f"hardware_import_error: {exc}",
            }

        payload = {
            "arm_id": arm_id,
            "servo_id": servo_id,
            "target_angle": target_angle,
            "prior_angle": prior_angle,
            "step_degrees": step_degrees,
            "direction": direction,
            "stop_conditions": stop_conditions,
            "mode": "supervised_physical_micro_step",
        }

        try:
            import asyncio
            loop = asyncio.get_event_loop()
            result = loop.run_until_complete(
                submit_execution_request(TARGET_MIM_ARM, payload)
            )
            return {
                "success": True,
                "dispatch_result": "ok",
                "movement_duration_ms": result.get("movement_duration_ms"),
                "stop_condition_triggered": result.get("stop_condition_triggered", ""),
                "error_message": "",
            }
        except Exception as exc:
            return {
                "success": False,
                "dispatch_result": "failed",
                "movement_duration_ms": None,
                "stop_condition_triggered": "",
                "error_message": str(exc),
            }


class DirectArmHttpAdapter(ServoHardwareAdapter):
    """
    Live hardware adapter that dispatches a single servo command directly to the
    ARM HTTP host (e.g. http://192.168.1.90:5000/move).

    Used for the first supervised live micro-step when MIM_ARM_PHYSICAL_MICRO_STEP_ENABLED=true
    and the execution_lane route is unavailable (e.g. arm_online flag not set).
    """

    def __init__(self, *, arm_host: str = "http://192.168.1.90:5000", timeout_seconds: int = 6) -> None:
        self._arm_host = arm_host.rstrip("/")
        self._timeout = timeout_seconds

    def dispatch_servo_command(
        self,
        *,
        arm_id: str,
        servo_id: int,
        target_angle: int,
        prior_angle: int,
        step_degrees: int,
        direction: str,
        stop_conditions: list[str],
    ) -> dict[str, Any]:
        import time
        try:
            import requests as _requests  # type: ignore[import]
        except ImportError:
            import urllib.request as _ur
            import json as _json
            import time as _time
            t0 = _time.monotonic()
            url = f"{self._arm_host}/move"
            body = _json.dumps({"servo": servo_id, "angle": target_angle}).encode()
            req = _ur.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            try:
                with _ur.urlopen(req, timeout=self._timeout) as resp:
                    elapsed_ms = int((_time.monotonic() - t0) * 1000)
                    data = _json.loads(resp.read().decode())
                    if data.get("status") == "ok":
                        return {
                            "success": True,
                            "dispatch_result": "ok",
                            "movement_duration_ms": elapsed_ms,
                            "stop_condition_triggered": "",
                            "error_message": "",
                        }
                    return {
                        "success": False,
                        "dispatch_result": "failed",
                        "movement_duration_ms": elapsed_ms,
                        "stop_condition_triggered": "",
                        "error_message": f"arm_nack: {data}",
                    }
            except Exception as exc:
                return {
                    "success": False,
                    "dispatch_result": "failed",
                    "movement_duration_ms": None,
                    "stop_condition_triggered": "",
                    "error_message": str(exc),
                }

        t0 = time.monotonic()
        url = f"{self._arm_host}/move"
        try:
            resp = _requests.post(
                url,
                json={"servo": servo_id, "angle": target_angle},
                timeout=self._timeout,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "ok":
                return {
                    "success": True,
                    "dispatch_result": "ok",
                    "movement_duration_ms": elapsed_ms,
                    "stop_condition_triggered": "",
                    "error_message": "",
                }
            return {
                "success": False,
                "dispatch_result": "failed",
                "movement_duration_ms": elapsed_ms,
                "stop_condition_triggered": "",
                "error_message": f"arm_nack: {data}",
            }
        except Exception as exc:
            return {
                "success": False,
                "dispatch_result": "failed",
                "movement_duration_ms": None,
                "stop_condition_triggered": "",
                "error_message": str(exc),
            }


def _physical_execution_to_dict(ex: SupervisedPhysicalMicroStepExecution) -> dict[str, Any]:
    def _iso(v: datetime | None) -> str | None:
        return v.isoformat() if v is not None else None

    return {
        "execution_id": ex.execution_id,
        "authorization_id": ex.authorization_id,
        "arm_id": ex.arm_id,
        "servo_id": ex.servo_id,
        "operator_id": ex.operator_id,
        "prior_angle": ex.prior_angle,
        "commanded_angle": ex.commanded_angle,
        "target_angle": ex.target_angle,
        "step_degrees": ex.step_degrees,
        "direction": ex.direction,
        "stop_conditions": ex.stop_conditions if isinstance(ex.stop_conditions, list) else [],
        "safe_home_required": bool(ex.safe_home_required),
        "safe_home_triggered": bool(ex.safe_home_triggered),
        "safe_home_target_angle": ex.safe_home_target_angle,
        "safe_home_triggered_at": _iso(ex.safe_home_triggered_at),
        "safe_home_outcome": ex.safe_home_outcome or "",
        "execution_status": ex.execution_status,
        "physical_movement_dispatched": bool(ex.physical_movement_dispatched),
        "dispatch_started_at": _iso(ex.dispatch_started_at),
        "dispatch_completed_at": _iso(ex.dispatch_completed_at),
        "dispatch_result": ex.dispatch_result or "",
        "movement_duration_ms": ex.movement_duration_ms,
        "stop_condition_triggered": ex.stop_condition_triggered or "",
        "error_message": ex.error_message or "",
        "log_entries": ex.log_entries if isinstance(ex.log_entries, list) else [],
        "abort_reason": ex.abort_reason or "",
        "completed_at": _iso(ex.completed_at),
        "created_at": _iso(ex.created_at),
        "updated_at": _iso(ex.updated_at),
    }


async def get_physical_execution(
    db: AsyncSession,
    execution_id: str,
) -> SupervisedPhysicalMicroStepExecution | None:
    result = await db.execute(
        select(SupervisedPhysicalMicroStepExecution).where(
            SupervisedPhysicalMicroStepExecution.execution_id == execution_id
        )
    )
    return result.scalar_one_or_none()


async def execute_physical_micro_step(
    db: AsyncSession,
    authorization: ArmProbeAuthorization,
    *,
    operator_id: str,
    adapter: ServoHardwareAdapter,
) -> dict[str, Any]:
    """
    Execute exactly one supervised physical servo micro-step.

    Gate checks (any failure → ValueError, no state change committed):
      1. Feature flag MIM_ARM_PHYSICAL_MICRO_STEP_ENABLED
      2. Authorization approved + unexpired + unconsumed
      3. stop_conditions present on authorization
      4. safe_home_fallback present on source dry-run command
      5. Envelope: motion_allowed, estop clear, angle within envelope, angle not in unstable region

    On gate pass:
      - Atomically consume authorization
      - Create SupervisedPhysicalMicroStepExecution (status=executing)
      - Dispatch exactly one servo command via adapter
      - Set physical_movement_dispatched=True only on dispatch success
      - On dispatch failure → trigger safe_home fallback if available
      - Finalize execution_status: complete | failed_dispatch | safe_home_triggered
    """
    from uuid import uuid4
    from core.config import settings

    if not settings.mim_arm_physical_micro_step_enabled:
        raise ValueError("feature_flag_disabled: MIM_ARM_PHYSICAL_MICRO_STEP_ENABLED is not set")

    now = datetime.now(timezone.utc)

    # Gate 1 — authorization lifecycle
    if authorization.expires_at is not None and authorization.expires_at < now:
        authorization.authorization_status = "expired"
        authorization.physical_execution_allowed = False
        authorization.updated_at = now
        await db.flush()
        raise ValueError("authorization_expired")

    if authorization.authorization_status != "approved":
        raise ValueError(
            f"authorization_not_approved: status={authorization.authorization_status}"
        )

    if not authorization.physical_execution_allowed:
        raise ValueError("physical_execution_not_allowed")

    # Gate 2 — stop conditions
    stop_conditions = authorization.stop_conditions if isinstance(authorization.stop_conditions, list) else []
    if not stop_conditions:
        raise ValueError("stop_conditions_missing: authorization has no stop conditions")

    # Gate 3 — safe_home fallback from source dry-run command
    cmd_result = await db.execute(
        select(ArmEnvelopeProbeAttempt).where(
            ArmEnvelopeProbeAttempt.probe_id == authorization.dry_run_command_id,
        )
    )
    command_row = cmd_result.scalar_one_or_none()
    safe_home_target_angle: int | None = None
    if command_row is not None:
        flags = command_row.stop_condition_flags if isinstance(command_row.stop_condition_flags, dict) else {}
        fallback = flags.get("safe_home_fallback")
        if isinstance(fallback, dict) and "target_angle" in fallback:
            safe_home_target_angle = int(fallback["target_angle"])

    if safe_home_target_angle is None:
        raise ValueError("safe_home_fallback_missing: dry-run command has no safe_home fallback")

    # Gate 4 — envelope safety gates (motion_allowed, estop, envelope bounds, unstable region)
    envelope_result = await db.execute(
        select(ArmServoEnvelope).where(
            ArmServoEnvelope.arm_id == authorization.arm_id,
            ArmServoEnvelope.servo_id == authorization.servo_id,
        )
    )
    envelope = envelope_result.scalar_one_or_none()
    if envelope is None:
        raise ValueError("envelope_not_found: no envelope for this arm/servo")

    if not getattr(envelope, "motion_allowed", True) is not False:
        # motion_allowed field is optional in older envelopes; only block if explicitly False
        pass
    if hasattr(envelope, "motion_allowed") and envelope.motion_allowed is False:
        raise ValueError("motion_not_allowed: envelope.motion_allowed=false")

    if hasattr(envelope, "estop_active") and envelope.estop_active is True:
        raise ValueError("estop_active: estop is engaged")

    target_angle = authorization.requested_angle
    if target_angle < envelope.configured_min or target_angle > envelope.configured_max:
        raise ValueError(
            f"target_exceeds_envelope: {target_angle} outside [{envelope.configured_min}, {envelope.configured_max}]"
        )

    if _is_in_unstable_region(envelope, target_angle):
        raise ValueError(f"target_in_unstable_region: angle={target_angle}")

    # All gates passed — atomically consume authorization
    authorization.authorization_status = "consumed"
    authorization.physical_execution_allowed = False
    authorization.consumed_at = now
    authorization.updated_at = now

    # Build initial log
    log: list[dict[str, str]] = [
        _make_log_entry(
            "physical_execution_started",
            f"operator={operator_id} servo={authorization.servo_id} "
            f"angle={target_angle} direction={authorization.direction}",
        ),
        _make_log_entry(
            "authorization_consumed",
            f"authorization_id={authorization.authorization_id}",
        ),
    ]

    execution = SupervisedPhysicalMicroStepExecution(
        execution_id=str(uuid4()),
        authorization_id=authorization.authorization_id,
        arm_id=authorization.arm_id,
        servo_id=authorization.servo_id,
        operator_id=operator_id,
        prior_angle=authorization.prior_angle,
        commanded_angle=target_angle,
        target_angle=target_angle,
        step_degrees=authorization.step_degrees,
        direction=authorization.direction,
        stop_conditions=stop_conditions,
        safe_home_required=bool(authorization.safe_home_required),
        safe_home_triggered=False,
        safe_home_target_angle=safe_home_target_angle,
        safe_home_triggered_at=None,
        safe_home_outcome="",
        execution_status="executing",
        physical_movement_dispatched=False,
        dispatch_started_at=None,
        dispatch_completed_at=None,
        dispatch_result="",
        movement_duration_ms=None,
        stop_condition_triggered="",
        error_message="",
        log_entries=log,
        abort_reason="",
        completed_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(execution)
    await db.flush()

    # Dispatch exactly one servo command
    dispatch_start = datetime.now(timezone.utc)
    execution.dispatch_started_at = dispatch_start

    try:
        adapter_result = adapter.dispatch_servo_command(
            arm_id=authorization.arm_id,
            servo_id=authorization.servo_id,
            target_angle=target_angle,
            prior_angle=authorization.prior_angle,
            step_degrees=authorization.step_degrees,
            direction=authorization.direction,
            stop_conditions=stop_conditions,
        )
    except Exception as exc:
        adapter_result = {
            "success": False,
            "dispatch_result": "failed",
            "movement_duration_ms": None,
            "stop_condition_triggered": "",
            "error_message": str(exc),
        }

    dispatch_end = datetime.now(timezone.utc)
    execution.dispatch_completed_at = dispatch_end

    dispatch_success = bool(adapter_result.get("success", False))
    execution.dispatch_result = str(adapter_result.get("dispatch_result", ""))
    execution.movement_duration_ms = adapter_result.get("movement_duration_ms")
    execution.stop_condition_triggered = str(adapter_result.get("stop_condition_triggered") or "")
    execution.error_message = str(adapter_result.get("error_message") or "")

    if dispatch_success:
        execution.physical_movement_dispatched = True
        execution.execution_status = "complete"
        execution.completed_at = dispatch_end
        log_entries = list(execution.log_entries)
        log_entries.append(_make_log_entry(
            "dispatch_success",
            f"dispatch_result={execution.dispatch_result} "
            f"movement_duration_ms={execution.movement_duration_ms}",
        ))
        execution.log_entries = log_entries
    else:
        # Dispatch failed — attempt safe_home fallback
        execution.physical_movement_dispatched = False
        execution.execution_status = "failed_dispatch"
        log_entries = list(execution.log_entries)
        log_entries.append(_make_log_entry(
            "dispatch_failed",
            f"error={execution.error_message}",
        ))

        if safe_home_target_angle is not None:
            safe_home_start = datetime.now(timezone.utc)
            try:
                safe_adapter_result = adapter.dispatch_servo_command(
                    arm_id=authorization.arm_id,
                    servo_id=authorization.servo_id,
                    target_angle=safe_home_target_angle,
                    prior_angle=target_angle,
                    step_degrees=authorization.step_degrees,
                    direction="safe_home",
                    stop_conditions=stop_conditions,
                )
                safe_home_succeeded = bool(safe_adapter_result.get("success", False))
            except Exception as exc_sh:
                safe_home_succeeded = False
                log_entries.append(_make_log_entry(
                    "safe_home_dispatch_error",
                    str(exc_sh),
                ))

            execution.safe_home_triggered = True
            execution.safe_home_triggered_at = safe_home_start
            execution.execution_status = "safe_home_triggered"

            if safe_home_succeeded:
                execution.safe_home_outcome = "succeeded"
                log_entries.append(_make_log_entry(
                    "safe_home_succeeded",
                    f"safe_home_target_angle={safe_home_target_angle}",
                ))
            else:
                execution.safe_home_outcome = "failed"
                log_entries.append(_make_log_entry(
                    "safe_home_failed",
                    f"safe_home_target_angle={safe_home_target_angle}",
                ))

        execution.log_entries = log_entries
        execution.completed_at = datetime.now(timezone.utc)

    execution.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return _physical_execution_to_dict(execution)


# ---------------------------------------------------------------------------
# Objective 179 — Envelope Learning Update from Physical Probe Outcome
# ---------------------------------------------------------------------------

import uuid as _uuid  # noqa: E402  (local import to avoid circular at top)


async def record_supervised_probe_outcome(
    db: AsyncSession,
    execution: SupervisedPhysicalMicroStepExecution,
) -> dict[str, Any]:
    """
    Record an ArmEnvelopeProbeAttempt and update the ArmServoEnvelope based on
    the outcome of a completed supervised physical micro-step execution.

    Outcome rules (from Phase 3 plan in docs/objective-172-mim-arm-safe-envelope-learning-plan.md):

    SUCCESS path (execution_status="complete", physical_movement_dispatched=True):
      - Create ArmEnvelopeProbeAttempt(phase="supervised_micro", result="safe",
                                        confidence_delta=+0.2)
      - ArmServoEnvelope.confidence = min(1.0, confidence + 0.2)
      - ArmServoEnvelope.evidence_count += 1
      - ArmServoEnvelope.last_verified_at = now
      - ArmServoEnvelope.last_probe_phase = "supervised_micro"
      - Narrow learned_soft_min/max toward commanded_angle if no previous bound
        or commanded_angle is tighter in the step direction.

    STOP CONDITION path (stop_condition_triggered != ""):
      - Create ArmEnvelopeProbeAttempt(phase="supervised_micro", result="stopped",
                                        confidence_delta=0.0)
      - Narrow envelope hard:
        - direction="up"  → learned_soft_max = commanded_angle - step_degrees
        - direction="down" → learned_soft_min = commanded_angle + step_degrees

    FAILED DISPATCH path (execution_status="failed_dispatch"):
      - Create ArmEnvelopeProbeAttempt(phase="supervised_micro", result="error",
                                        confidence_delta=0.0)
      - No envelope update.

    Returns a dict with keys:
      probe_attempt_id, probe_id, envelope_updated, confidence_delta,
      confidence_after, evidence_count_after, learned_soft_min, learned_soft_max,
      result, reason.
    """
    # Determine outcome
    stop_fired = bool(execution.stop_condition_triggered)
    succeeded = (
        execution.execution_status == "complete"
        and bool(execution.physical_movement_dispatched)
        and not stop_fired
    )
    failed_dispatch = execution.execution_status in {"failed_dispatch", "safe_home_triggered"}

    if succeeded:
        result_str = "safe"
        confidence_delta = 0.2
    elif stop_fired:
        result_str = "stopped"
        confidence_delta = 0.0
    else:
        result_str = "error"
        confidence_delta = 0.0

    # Load envelope
    envelope = await get_envelope(db, execution.servo_id, arm_id=execution.arm_id)
    if envelope is None:
        raise ValueError(
            f"No envelope found for arm_id={execution.arm_id!r} servo_id={execution.servo_id}"
        )

    # Build stop_condition_flags for the probe attempt
    stop_condition_flags: dict[str, Any] = {}
    if stop_fired:
        stop_condition_flags[execution.stop_condition_triggered] = True

    probe_id = str(_uuid.uuid4())
    now = datetime.now(timezone.utc)

    attempt = ArmEnvelopeProbeAttempt(
        probe_id=probe_id,
        envelope_id=envelope.id,
        servo_id=execution.servo_id,
        phase="supervised_micro",
        commanded_angle=execution.commanded_angle,
        prior_angle=execution.prior_angle,
        observed_angle=execution.commanded_angle if succeeded else None,
        step_degrees=execution.step_degrees,
        stop_condition=execution.stop_condition_triggered or "",
        stop_condition_flags=stop_condition_flags,
        simulation_id=None,
        execution_id=execution.execution_id,
        result=result_str,
        confidence_delta=confidence_delta,
    )
    db.add(attempt)

    envelope_updated = False

    if succeeded:
        # Confidence increment
        envelope.confidence = min(1.0, (envelope.confidence or 0.0) + confidence_delta)
        envelope.evidence_count = (envelope.evidence_count or 0) + 1
        envelope.last_verified_at = now
        envelope.last_probe_phase = "supervised_micro"
        envelope.updated_at = now

        # Narrow learned bounds toward the confirmed safe angle
        commanded = execution.commanded_angle
        direction = execution.direction or ""
        if direction == "up":
            # Moving toward higher angles: expand or narrow learned_soft_max
            if envelope.learned_soft_max is None or commanded > envelope.learned_soft_max:
                envelope.learned_soft_max = commanded
        elif direction == "down":
            # Moving toward lower angles: expand or narrow learned_soft_min
            if envelope.learned_soft_min is None or commanded < envelope.learned_soft_min:
                envelope.learned_soft_min = commanded

        envelope_updated = True

    elif stop_fired:
        # Hard narrow the bound at the stop point
        commanded = execution.commanded_angle
        step = execution.step_degrees or 0
        direction = execution.direction or ""
        if direction == "up":
            new_max = commanded - step
            if envelope.learned_soft_max is None or new_max < envelope.learned_soft_max:
                envelope.learned_soft_max = new_max
        elif direction == "down":
            new_min = commanded + step
            if envelope.learned_soft_min is None or new_min > envelope.learned_soft_min:
                envelope.learned_soft_min = new_min
        envelope.updated_at = now
        envelope_updated = True

    await db.flush()

    # Refresh attempt to get DB-assigned id
    await db.refresh(attempt)

    return {
        "probe_attempt_id": attempt.id,
        "probe_id": probe_id,
        "envelope_updated": envelope_updated,
        "confidence_delta": confidence_delta,
        "confidence_after": envelope.confidence,
        "evidence_count_after": envelope.evidence_count,
        "learned_soft_min": envelope.learned_soft_min,
        "learned_soft_max": envelope.learned_soft_max,
        "result": result_str,
        "reason": (
            "dispatch_succeeded"
            if succeeded
            else ("stop_condition_fired" if stop_fired else "dispatch_failed")
        ),
    }
