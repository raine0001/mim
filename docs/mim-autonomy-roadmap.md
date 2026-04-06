# MIM Autonomy Roadmap

This roadmap is the master sequencing document for MIM objective development. It groups objectives by capability layer so planning, implementation, and verification remain aligned with the autonomy architecture.

## Foundation Objectives

- Objective 21 — Unified input and perception gateway
- Objective 22 — MIM↔TOD execution feedback integration
- Objective 23 — Operator control and exception handling
- Objective 23B — Safe capability expansion

## Perception and Input Objectives

- Objective 21 — Unified input and perception gateway
- Objective 24 — Workspace observation memory
- Objective 27 — Workspace map and relational context

## Memory and Identity Objectives

- Objective 24 — Workspace observation memory
- Objective 25 — Memory-informed routing
- Objective 26 — Object identity persistence
- Objective 27 — Workspace map and relational context
- Objective 40 — Human preference and routine memory
- Objective 52 — Concept and pattern memory
- Objective 53 — Multi-session developmental memory

## Planning and Execution Objectives

- Objective 28 — Autonomous task proposals
- Objective 29 — Directed targeting
- Objective 30 — Safe directed action planning
- Objective 31 — Safe reach/approach simulation
- Objective 32 — Safe reach execution
- Objective 33 — Autonomous execution proposals
- Objective 34 — Continuous workspace monitoring loop
- Objective 35 — Autonomous task execution policies
- Objective 36 — Multi-step autonomous task chaining
- Objective 37 — Human-aware interruption and safe pause handling
- Objective 38 — Predictive workspace change and replanning
- Objective 41 — Closed-loop autonomous task execution
- Objective 42 — Multi-capability coordination
- Objective 43 — Human-aware workspace behavior

## Autonomy and Strategy Objectives

- Objective 39 — Policy-based autonomous priority selection
- Objective 44 — Constraint evaluation engine
- Objective 45 — Constraint weight learning
- Objective 46 — Long-horizon planning
- Objective 47 — Environment strategy formation
- Objective 48 — Human preference strategy integration

## Self-Improvement Objectives

- Objective 49 — Self-improvement proposal engine
- Objective 50 — Environment maintenance autonomy
- Objective 51 — Policy experiment sandbox
- Objective 52 — Concept and pattern memory
- Objective 53 — Multi-session developmental memory
- Objective 54 — Self-guided improvement loop
- Objective 55 — Improvement prioritization and governance

## Runtime Governance and Inspectability Objectives

- Objective 57 — Goal strategy engine
- Objective 58 — Adaptive autonomy boundaries
- Objective 60 — Environment stewardship loop
- Objective 80 — Execution truth convergence
- Objective 81 — Execution truth governance loop
- Objective 82 — Live perception governance grounding
- Objective 83 — Governed inquiry resolution loop
- Objective 84 — Operator-visible system reasoning
- Objective 85 — Operator-governed resolution commitments
- Objective 86 — Commitment enforcement and drift monitoring loop
- Objective 87 — Commitment outcome and learning loop
- Objective 88 — Operator preference and policy convergence
- Objective 89 — Proposal policy convergence
- Objective 90 — Cross-policy conflict resolution

Current checkpoint before Objective 91:

- Objective 90 is broadened and green in the current bounded slice, not complete.
- The stable validated checkpoint covers proposal shaping, stewardship, autonomy, governed inquiry answer-path arbitration, bounded governed inquiry decision-state suppression/cooldown hold behavior, pre-execution policy gating across the live execution creation paths, and mandatory TOD execution-readiness enforcement across execution, trace, proposal, conflict, and state-bus surfaces.
- The remaining Objective 90 gap is the wider contradictory-reopen inquiry matrix, which is explicitly deferred rather than silently treated as done.

Objectives 91 through 95 now add the control and stability plane on top of that checkpoint:

- Objective 91 adds durable execution traces and causality events.
- Objective 92 persists intent lineage independently from transient dispatch.
- Objective 93 stores orchestration checkpoints for governed executions.
- Objective 94 adds scope-aware operator hard-stop, pause, and redirect overrides.
- Objective 95 adds execution stability scoring and mitigation reporting.

Next planned slice after the green 95 checkpoint:

- Objective 96 extends the control plane into recovery and safe resume so failed, blocked, paused, and degraded executions can be evaluated and retried through one bounded recovery contract instead of ad hoc follow-up.

Current bounded MIM ARM execution trust checkpoint:

- Objective 107 closed the bounded remote attribution gap with an explicit readiness-surface boundary.
- Objective 108 adds a true dispatch-authoritative telemetry lane for bounded `safe_home`, with per-dispatch records, a latest pointer, dedicated API endpoints, proof-script integration, and operator-facing UI exposure.
- Objective 109 extends that lane to a second bounded live action, `scan_pose`, and tightens dispatch telemetry so executor-originated host timestamps outrank flatter fallback fields.
- Objective 109 is closed on the current-source runtime: bounded `scan_pose` now produces fresh dispatch telemetry, aligned TOD ACK and RESULT evidence, explicit host attribution, and `proof_chain_complete = true`.
- Objective 110 normalizes bounded multi-action execution so `safe_home`, `scan_pose`, and `capture_frame` share one attribution contract with no hidden action fallback defaults in the execution path, and one proof checklist now governs every bounded-action live proof.

## Future Objectives

- Objective 58 — Adaptive autonomy boundaries (experience-conditioned autonomy limits)

## Layered Objective Grouping (Canonical Sequence)

- 21–23: Input, gateway, operator control
- 24–27: Memory, identity, spatial reasoning
- 28–31: Proposals, targeting, planning, simulation
- 32–38: Execution, monitoring, interruption, replanning
- 39–44: Priority, preferences, constraint reasoning
- 45–49: Constraint learning, horizon planning, strategy, self-improvement
- 50+: Environment maintenance, policy experimentation, concept formation, developmental memory, self-guided improvement
- 80–84: Runtime governance, inquiry control, and operator-visible reasoning
- 85–87: Durable operator resolution commitments, enforcement, and outcome learning
- 88–90: Preference convergence, proposal arbitration propagation, proposal policy stabilization, and cross-policy conflict resolution across stacked governance surfaces

## Governance Notes

- Every objective follows the lifecycle: `implement -> focused gate -> full regression gate -> promote -> production verification -> report`.
- Objective ordering is cumulative; later objectives should preserve prior objective contracts.
- Promotion evidence belongs in objective-specific readiness and production reports, indexed in `docs/objective-index.md`.
