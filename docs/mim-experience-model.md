# MIM Experience Model

This document defines how MIM represents lived operational history as reusable intelligence.

## Purpose

The experience model captures what happened in the environment, what action chain followed, what outcome resulted, and whether future performance improved or degraded.

## Core Concept: ExperienceRecord

An ExperienceRecord aggregates event-to-outcome flow for one operational episode.

### Required fields

- `experience_type`: category (execution, maintenance, planning, interruption, recovery)
- `source_context`: actor/source endpoint and initiating trigger
- `environment_observations`: observed state before/during/after action
- `action_chain`: ordered actions taken, including retries/replans/interruptions
- `decision_links`: related `DecisionRecord` ids
- `outcome`: final outcome summary
- `outcome_quality`: scalar quality estimate in `[0,1]`
- `performance_delta`: improved/neutral/degraded signal with rationale
- `memory_updates`: linked memory entries produced
- `improvement_links`: linked improvement proposals/artifacts
- `safety_events`: warnings/violations encountered
- `metadata_json`: extension payload
- `created_at`: timestamp

## Experience Lifecycle

`observe -> decide -> act -> measure outcome -> update memory -> assess delta -> propose improvement`

## Performance Delta Semantics

- `improved`: reduced friction/risk or increased completion quality
- `neutral`: no material change
- `degraded`: increased retries/failures/risk or lower quality

A degradation signal should automatically become candidate evidence for proposal generation and policy experimentation.

## Experience Sources in Current Build

Experience signals currently come from:

- workspace observations (`WorkspaceObservation`)
- execution actions/results (`Action`, validation paths)
- decision traces (`WorkspaceDecisionRecord`)
- maintenance cycles (`WorkspaceMaintenanceRun`, `WorkspaceMaintenanceAction`)
- improvement workflow (`WorkspaceImprovementProposal`, `WorkspaceImprovementArtifact`)
- memory entries (`MemoryEntry`)

## Relationship to Memory

Memory stores durable facts and summaries. Experience stores causal episodes.

- Memory answers: “what is known?”
- Experience answers: “what happened, why, and with what effect?”

Both are required for adaptive autonomy.

## Minimum Query Surfaces (Target)

- list experiences by type/time range
- fetch full causal chain for one experience
- fetch degraded experiences needing review
- fetch experiences that produced successful improvements

## Governance

Experience model extensions remain bounded by core charter constraints:

- hard safety constraints are immutable
- experiments are sandboxed before production policy changes
- all promotion requires objective-level evidence
