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

Current recovery-governance checkpoint after Objective 130:

- Objective 116 propagates one autonomy-boundary envelope through planning, execution, recovery, journal, and UI surfaces.
- Objectives 117 and 118 extend that envelope into task chains and capability chains.
- Objective 119 adds stable recovery taxonomy.
- Objective 120 adds inspectable recovery-policy tuning.
- Objective 121 bridges that tuning into a durable operator-governed commitment.
- Objective 122 evaluates that recovery-derived commitment against future recovery evidence.
- Objective 123 adds explicit expiry pressure and expiry-ready signaling for recovery-derived commitments.
- Objective 124 adds bounded reapplication of expired recovery-derived commitments with lineage back to the prior commitment.
- Objective 125 adds explicit manual reset semantics distinct from passive expiry or generic revocation.
- Objective 126 makes recovery-derived commitments a first-class conflict source inside existing governance arbitration.
- Objective 127 defines deterministic inherited scope propagation across parent, child, chain, and execution scopes.
- Objective 128 adds rollout preview before recovery-governance transitions are applied.
- Objective 129 applies recovery-derived posture to live admission-control decisions.
- Objective 130 consolidates the full recovery-governance story into one operator-facing rollup.

Current bounded MIM ARM execution trust checkpoint:

- Objective 107 closed the bounded remote attribution gap with an explicit readiness-surface boundary.
- Objective 108 adds a true dispatch-authoritative telemetry lane for bounded `safe_home`, with per-dispatch records, a latest pointer, dedicated API endpoints, proof-script integration, and operator-facing UI exposure.
- Objective 109 extends that lane to a second bounded live action, `scan_pose`, and tightens dispatch telemetry so executor-originated host timestamps outrank flatter fallback fields.
- Objective 109 is closed on the current-source runtime: bounded `scan_pose` now produces fresh dispatch telemetry, aligned TOD ACK and RESULT evidence, explicit host attribution, and `proof_chain_complete = true`.
- Objective 110 normalizes bounded multi-action execution so `safe_home`, `scan_pose`, and `capture_frame` share one attribution contract with no hidden action fallback defaults in the execution path, and one proof checklist now governs every bounded-action live proof.

Current conversation and dialog checkpoint after Objectives 142 and 143:

- Objective 142 hardens the deterministic conversation layer so interruptions, direct corrections, short-response preferences, and bounded follow-up formatting requests stay on-topic instead of falling through to generic acknowledgements.
- Objective 142 also strengthens the structured conversation regression scorer so interruption, correction, concise-response, mode-shift, and remembered-preference failures are explicitly detectable.
- Objective 143 keeps TOD dialog mirrors converged by updating each per-session `.latest.json` snapshot when the aggregate dialog index is marked `replied`.

Current action-control checkpoint after Objectives 144 and 145:

- Objective 144 adds a bounded action confirmation layer so imperative conversation turns now require explicit operator confirmation before they are treated as approved action requests.
- Objective 144 also tightens regression scoring so `ask_confirmation_before_action` only passes when a real confirmation prompt is present.
- Objective 145 makes pause, resume, cancel, and stop control turns deterministic in the conversation lane, including while an action-confirmation thread is pending.
- Objective 145 fixes control precedence so pause/resume/cancel paths are not swallowed by the older generic interruption branch.

Current error-clarity and operator-trust checkpoint after Objectives 146 through 149:

- Objective 146 adds explicit error-clarity replies for unsafe requests, private-runtime disclosure requests, ambiguous external-action requests, and unsupported claims that external work already happened.
- Objective 146 also extends regression scoring so safe refusals now fail if they do not offer a constructive alternative.
- Objective 147 exposes current recommendation summaries more directly in the operator-visible UI state and system reasoning panel.
- Objective 148 centralizes conversation boundary handling so safety and limitation replies execute before generic control-path fallbacks.
- Objective 149 exposes strategy-derived trust signals in the operator-visible reasoning surface, including what MIM did, what it will do next, and why the current confidence posture is justified.

Current autonomy-feedback-stability checkpoint after Objectives 150 through 152:

- Objective 150 exposes a bounded autonomy posture that tells the operator whether automatic continuation is currently allowed for low-risk steps or held behind safeguards.
- Objective 151 exposes a compact human-feedback loop summary so the latest execution feedback posture is visible without reading raw execution payloads.
- Objective 152 exposes a stability-guard summary that rolls runtime health, recovery posture, governance signals, and TOD escalation blockers into one operator-visible guard surface.
- This closes the bounded 142-152 conversation and operator-awareness tranche in the current repo state.

Current conversation continuity checkpoint after Objective 153:

- Objective 153 persists gateway text turns into workspace interface sessions and message history so the conversation lane can recover continuity from stored session context.
- Pending action requests now survive confirm, revise, cancel, pause, and resume follow-ups through the interface-session bridge instead of depending on one transient clarification branch.
- Precision-prompt follow-ups now preserve prior topic continuity for terse `status`, `after`, and `recap` replies.
- The authoritative focused lane passed on the fresh current-source runtime on `:18001` across Objectives 79, 153, and 74.

Current self-evolution checkpoint after Objective 164:

- Objective 164 adds `/improvement/self-evolution` as a bounded aggregate over proposals, recommendations, and ranked backlog state.
- Refresh mode reuses the existing governed Objective 55 backlog refresh path, so the new surface extends the current improvement loop instead of duplicating it.
- The snapshot exposes loop status, summary text, status counts, risk/governance counts, and top ranked items so self-evolution state is inspectable from one endpoint.
- The authoritative focused lane passed on the fresh current-source runtime on `:18001` across Objectives 54, 55, and 164.

Current self-evolution guidance checkpoint after Objective 165:

- Objective 165 adds `/improvement/self-evolution/next-action` as a bounded decision surface over the Objective 164 snapshot.
- The next-action contract recommends one inspectable follow-up step at a time, including review of operator-gated recommendations, ranked backlog inspection, recommendation generation, or bounded refresh.
- This slice stays non-destructive: it recommends existing improvement routes rather than mutating state or creating a parallel approval engine.
- The authoritative focused lane passed on the fresh current-source runtime on `:18001` across Objectives 54, 55, 164, and 165.

Current self-evolution briefing checkpoint after Objective 166:

- Objective 166 adds `/improvement/self-evolution/briefing` as a resolved operator packet over the Objective 165 next-action contract.
- The briefing returns the current snapshot, the recommended next action, and the concrete target proposal, recommendation, or backlog detail needed to inspect that action.
- This slice stays bounded and non-destructive: it resolves existing state into one operator-facing packet without introducing a new mutation path.
- The authoritative focused lane passed on the fresh current-source runtime on `:18001` across Objectives 54, 55, 164, 165, and 166.

Current self-evolution operator-visibility checkpoint after Objective 167:

- Objective 167 threads the Objective 166 briefing packet into `/mim/ui/state` so the operator_reasoning surface and system reasoning panel expose the current self-evolution snapshot, recommended next action, and resolved target summary in one place.
- This slice stays non-destructive: the MIM UI reads the existing self-evolution briefing contract with `refresh=false` and does not create a parallel recommendation path.
- The authoritative focused lane passed on the fresh current-source runtime on `:18001` across Objectives 54, 55, 164, 165, 166, and 167.

Current self-evolution operator-actionability checkpoint after Objective 168:

- Objective 168 normalizes the bounded self-evolution next action into an operator-ready UI contract so `/mim/ui/state` exposes the exact method, path, and concise action summary for the recommended follow-up call.
- The action contract is mirrored into `conversation_context` for downstream conversational surfaces and rendered in the system reasoning panel as the explicit next step.
- This slice stays non-destructive: it reuses the existing Objective 166 action packet and does not introduce a new mutation or approval path.
- The authoritative focused lane passed on the fresh current-source runtime on `:18001` across Objectives 54, 55, 164, 165, 166, 167, and 168.

Current self-evolution operator-command checkpoint after Objective 169:

- Objective 169 packages the bounded self-evolution follow-up route into a first-class `operator_commands` list so operator surfaces can present the next self-evolution call in the same command shape used by other control surfaces.
- The primary command is mirrored into `conversation_context` and rendered in the system reasoning panel as an explicit command summary.
- This slice stays non-destructive: it derives command packaging from the existing Objective 166 action packet and does not add a new execution path.
- The authoritative focused lane passed on the fresh current-source runtime on `:18001` across Objectives 54, 55, 164, 165, 166, 167, 168, and 169.

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
- 116–122: Boundary-envelope propagation plus recovery taxonomy, tuning, recovery-derived commitments, and recovery-aware commitment evaluation
- 123–130: Recovery-commitment lifecycle, conflict handling, propagation, rollout preview, admission control, and operator rollup
- 131–141: Strategy planning, intent understanding, explainability, confidence, refinement, environment awareness, context persistence, coordination, and safety envelope
- 142–153: Conversation reliability, TOD dialog convergence, action confirmation, interrupt/control continuity, error clarity, system awareness, consistency enforcement, trust signals, lightweight autonomy, human feedback capture, stability guards, and session continuity bridging
- 164–169: Self-evolution core snapshot, bounded next-action guidance, resolved briefing packets, operator-visible UI integration, operator-ready action contracts, and operator-command packaging over proposals, recommendations, and backlog governance

## Governance Notes

- Every objective follows the lifecycle: `implement -> focused gate -> full regression gate -> promote -> production verification -> report`.
- Objective ordering is cumulative; later objectives should preserve prior objective contracts.
- Promotion evidence belongs in objective-specific readiness and production reports, indexed in `docs/objective-index.md`.
