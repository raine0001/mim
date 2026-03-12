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

## Future Objectives

- Objective 55+ — Cross-domain integration and cognition bridge
- Objective 56+ — Experience graph operationalization at scale

## Layered Objective Grouping (Canonical Sequence)

- 21–23: Input, gateway, operator control
- 24–27: Memory, identity, spatial reasoning
- 28–31: Proposals, targeting, planning, simulation
- 32–38: Execution, monitoring, interruption, replanning
- 39–44: Priority, preferences, constraint reasoning
- 45–49: Constraint learning, horizon planning, strategy, self-improvement
- 50+: Environment maintenance, policy experimentation, concept formation, developmental memory, self-guided improvement

## Governance Notes

- Every objective follows the lifecycle: `implement -> focused gate -> full regression gate -> promote -> production verification -> report`.
- Objective ordering is cumulative; later objectives should preserve prior objective contracts.
- Promotion evidence belongs in objective-specific readiness and production reports, indexed in `docs/objective-index.md`.
