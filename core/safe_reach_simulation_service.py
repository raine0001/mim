"""
Safe reach simulation computation layer.

Provides reachability estimation and collision-risk scoring before any arm
actuation is dispatched.  All functions are pure / stateless; callers own
persistence and endpoint wiring.

Outcome vocabulary
------------------
simulation_outcome  : "safe" | "unsafe" | "uncertain"
  Maps to WorkspaceActionPlan.simulation_outcome as:
    "safe"      → "plan_safe"
    "unsafe"    → "plan_blocked"
    "uncertain" → "plan_requires_adjustment"

recovery_action
  "reobserve"   – re-scan the workspace to refresh object / zone state
  "confirm"     – require explicit operator confirmation before proceeding
  "resimulate"  – clear obstacle/zone state then re-run simulation
  ""            – not blocked; no recovery needed
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STALE_OBJECT_STATUSES: frozenset[str] = frozenset({"uncertain", "stale", "missing"})

# Collision-risk contribution weights
_RISK_PER_OBSTACLE = 0.30
_RISK_UNCERTAIN_IDENTITY = 0.25
_RISK_UNSAFE_ZONE = 0.40
_RISK_UNKNOWN_ZONE = 0.50

# Outcome thresholds for gate logic
_GATE_OUTCOME_SAFE = "safe"
_GATE_OUTCOME_UNSAFE = "unsafe"
_GATE_OUTCOME_UNCERTAIN = "uncertain"

# Mapping to WorkspaceActionPlan.simulation_outcome values
OUTCOME_TO_PLAN_STATUS: dict[str, str] = {
    _GATE_OUTCOME_SAFE: "plan_safe",
    _GATE_OUTCOME_UNSAFE: "plan_blocked",
    _GATE_OUTCOME_UNCERTAIN: "plan_requires_adjustment",
}


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReachabilityResult:
    """Outcome of arm reachability analysis for a target zone / object."""

    reachable: bool
    confidence: float
    reason: str  # "clear" | "unknown_zone" | "unsafe_zone" | "no_safety_envelope"


@dataclass(frozen=True)
class CollisionRiskResult:
    """Collision-risk score and contributing factors."""

    risk_score: float          # 0.0–1.0
    obstacle_count: int
    has_stale_object: bool
    has_unsafe_zone: bool
    has_unknown_zone: bool
    obstacle_names: list[str]  # canonical_name of each obstacle
    blocked_vectors: list[str] # e.g. ["direct", "side_approach"]
    warnings: list[str]        # human-readable warning tokens


@dataclass
class SimulationResult:
    """Combined reachability + collision result, ready for persistence."""

    # Core outcome
    simulation_outcome: str      # "safe" | "unsafe" | "uncertain"
    simulation_status: str       # always "completed" when produced by run_simulation
    simulation_gate_passed: bool

    # Human-readable block context
    blocked_reason: str    # empty when gate passes
    recovery_action: str   # "reobserve" | "confirm" | "resimulate" | ""

    # Sub-results
    reachability: ReachabilityResult
    collision_risk: CollisionRiskResult

    # Full JSON payload for persistence in simulation_json column
    simulation_json: dict = field(default_factory=dict)

    @property
    def plan_outcome(self) -> str:
        """Return the WorkspaceActionPlan-compatible outcome string."""
        return OUTCOME_TO_PLAN_STATUS.get(self.simulation_outcome, "plan_blocked")


# ---------------------------------------------------------------------------
# Reachability computation
# ---------------------------------------------------------------------------


def compute_reachability(
    *,
    target_zone: str,
    zone_hazard_level: int | None,
    safety_envelope: dict,
) -> ReachabilityResult:
    """
    Estimate whether the arm can safely reach the target zone.

    Parameters
    ----------
    target_zone:
        The normalised zone name for the target (e.g. "front-center").
        Empty string → unknown zone.
    zone_hazard_level:
        Hazard level of the target zone from WorkspaceZone.hazard_level.
        None indicates the zone is not found in the DB (unknown).
    safety_envelope:
        Dict from the request or schema default.  Must be non-empty for full
        confidence; missing/empty → degraded confidence and uncertain result.
    """
    unknown_zone = not target_zone or zone_hazard_level is None
    unsafe_zone = not unknown_zone and zone_hazard_level > 0
    missing_envelope = not safety_envelope

    if missing_envelope:
        return ReachabilityResult(
            reachable=False,
            confidence=0.0,
            reason="no_safety_envelope",
        )

    if unknown_zone:
        return ReachabilityResult(
            reachable=False,
            confidence=0.0,
            reason="unknown_zone",
        )

    if unsafe_zone:
        return ReachabilityResult(
            reachable=False,
            confidence=0.0,
            reason="unsafe_zone",
        )

    # Zone is known and safe: base confidence from envelope presence
    reach_confidence = float(safety_envelope.get("reach_confidence", 0.90))
    reach_confidence = max(0.0, min(1.0, reach_confidence))
    return ReachabilityResult(
        reachable=True,
        confidence=reach_confidence,
        reason="clear",
    )


# ---------------------------------------------------------------------------
# Collision-risk computation
# ---------------------------------------------------------------------------


def compute_collision_risk(
    *,
    target_zone: str,
    zone_hazard_level: int | None,
    nearby_objects: list[dict],
    target_object_status: str,
    safety_envelope: dict,
) -> CollisionRiskResult:
    """
    Estimate collision risk for a planned reach motion.

    Parameters
    ----------
    target_zone:
        Normalised zone name for the target.
    zone_hazard_level:
        Hazard level of the target zone.  None → unknown zone.
    nearby_objects:
        List of obstacle dicts: {id, canonical_name, zone, status, confidence}.
        Should exclude the target object itself.
    target_object_status:
        Status of the target WorkspaceObjectMemory ("active", "stale", etc.).
    safety_envelope:
        Safety-envelope payload.  Empty → adds full unknown-zone penalty.
    """
    unknown_zone = not target_zone or zone_hazard_level is None
    unsafe_zone = not unknown_zone and zone_hazard_level > 0
    has_stale_object = target_object_status in STALE_OBJECT_STATUSES

    risk = 0.0
    obstacle_count = len(nearby_objects)

    risk += min(0.6, obstacle_count * _RISK_PER_OBSTACLE)
    if has_stale_object:
        risk += _RISK_UNCERTAIN_IDENTITY
    if unsafe_zone:
        risk += _RISK_UNSAFE_ZONE
    if unknown_zone:
        risk += _RISK_UNKNOWN_ZONE

    risk = min(1.0, risk)

    obstacle_names = [str(o.get("canonical_name", "")) for o in nearby_objects]

    blocked_vectors: list[str] = []
    if obstacle_count >= 1:
        blocked_vectors.append("direct")
    if obstacle_count >= 2:
        blocked_vectors.append("side_approach")

    warnings: list[str] = []
    if unknown_zone:
        warnings.append("unknown_zone")
    if unsafe_zone:
        warnings.append("unsafe_zone")
    if has_stale_object:
        warnings.append("uncertain_object_identity")
    for name in obstacle_names:
        warnings.append(f"obstacle:{name}")

    return CollisionRiskResult(
        risk_score=round(risk, 4),
        obstacle_count=obstacle_count,
        has_stale_object=has_stale_object,
        has_unsafe_zone=unsafe_zone,
        has_unknown_zone=unknown_zone,
        obstacle_names=obstacle_names,
        blocked_vectors=blocked_vectors,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Combined simulation runner
# ---------------------------------------------------------------------------


def run_simulation(
    *,
    target_zone: str,
    target_object_status: str,
    target_confidence: float,
    zone_hazard_level: int | None,
    safety_envelope: dict,
    nearby_objects: list[dict],
    collision_risk_threshold: float = 0.45,
    approach_direction: str = "unknown",
    clearance_m: float = 0.35,
    path_length: float | None = None,
) -> SimulationResult:
    """
    Run complete safe-reach simulation: reachability + collision risk → outcome.

    Returns a :class:`SimulationResult` with all fields populated.  Does not
    touch the database; the caller is responsible for persisting the result.
    """
    reach = compute_reachability(
        target_zone=target_zone,
        zone_hazard_level=zone_hazard_level,
        safety_envelope=safety_envelope,
    )

    collision = compute_collision_risk(
        target_zone=target_zone,
        zone_hazard_level=zone_hazard_level,
        nearby_objects=nearby_objects,
        target_object_status=target_object_status,
        safety_envelope=safety_envelope,
    )

    # ----------------------------------------------------------------
    # Determine simulation_outcome
    # ----------------------------------------------------------------
    if not safety_envelope:
        outcome = _GATE_OUTCOME_UNSAFE
        blocked_reason = "Missing safety envelope — operator must provide arm reach bounds before dispatch."
        recovery_action = "confirm"

    elif collision.has_unknown_zone or collision.has_unsafe_zone:
        outcome = _GATE_OUTCOME_UNSAFE
        if collision.has_unsafe_zone:
            blocked_reason = f"Target zone '{target_zone}' has hazard level > 0 and cannot be reached safely."
            recovery_action = "confirm"
        else:
            blocked_reason = f"Target zone '{target_zone}' is not registered in the workspace map."
            recovery_action = "reobserve"

    elif collision.has_stale_object:
        outcome = _GATE_OUTCOME_UNCERTAIN
        blocked_reason = "Target object is stale or identity uncertain — reobserve before dispatching."
        recovery_action = "reobserve"

    elif collision.risk_score >= collision_risk_threshold or collision.obstacle_count > 0:
        outcome = _GATE_OUTCOME_UNSAFE
        blocked_reason = (
            f"Collision risk {collision.risk_score:.2f} exceeds threshold {collision_risk_threshold:.2f} "
            f"with {collision.obstacle_count} obstacle(s) in path."
        )
        recovery_action = "resimulate"

    else:
        outcome = _GATE_OUTCOME_SAFE
        blocked_reason = ""
        recovery_action = ""

    gate_passed = outcome == _GATE_OUTCOME_SAFE

    # ----------------------------------------------------------------
    # Estimated path length if not provided
    # ----------------------------------------------------------------
    computed_path = path_length if path_length is not None else round(
        0.8 + (0.35 * collision.obstacle_count) + (0.25 if collision.has_stale_object else 0.0), 3
    )

    # ----------------------------------------------------------------
    # simulation_json — full reasoning record
    # ----------------------------------------------------------------
    effective_confidence = round(
        max(0.0, min(1.0, target_confidence * (1.0 - collision.risk_score))), 3
    )

    sim_json: dict = {
        "reachable": reach.reachable,
        "reachability_reason": reach.reason,
        "reachability_confidence": round(reach.confidence, 4),
        "collision_risk": collision.risk_score,
        "obstacle_count": collision.obstacle_count,
        "collision_candidates": [
            {
                "object_memory_id": o.get("id"),
                "canonical_name": o.get("canonical_name"),
                "zone": o.get("zone"),
                "status": o.get("status"),
                "confidence": o.get("confidence"),
            }
            for o in nearby_objects
        ],
        "blocked_approach_vectors": collision.blocked_vectors,
        "obstacle_warnings": collision.warnings,
        "path_length": computed_path,
        "approach_direction": approach_direction,
        "clearance": clearance_m,
        "confidence": effective_confidence,
        "outcome": outcome,
        "plan_outcome": OUTCOME_TO_PLAN_STATUS.get(outcome, "plan_blocked"),
        "target_zone": target_zone,
        "gate": {
            "collision_risk_threshold": collision_risk_threshold,
            "blocked": not gate_passed,
        },
        "blocked_reason": blocked_reason,
        "recovery_action": recovery_action,
    }

    return SimulationResult(
        simulation_outcome=outcome,
        simulation_status="completed",
        simulation_gate_passed=gate_passed,
        blocked_reason=blocked_reason,
        recovery_action=recovery_action,
        reachability=reach,
        collision_risk=collision,
        simulation_json=sim_json,
    )
