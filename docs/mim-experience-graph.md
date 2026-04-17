# MIM Experience Graph

This document defines the causal linking model that connects perception, decisions, execution, outcomes, memory updates, and improvement proposals.

## Purpose

MIM requires a causal trail, not isolated records. The experience graph links operational entities so the system can explain and improve behavior over time.

## Canonical Chain

`observation -> decision -> action -> outcome -> memory update -> improvement proposal`

## Node Types

- Observation node (`WorkspaceObservation`)
- Decision node (`WorkspaceDecisionRecord`)
- Action node (`Action`, `WorkspaceMaintenanceAction`)
- Outcome node (validation/outcome summary, quality)
- Memory node (`MemoryEntry`)
- Improvement node (`WorkspaceImprovementProposal`, `WorkspaceImprovementArtifact`)

## Edge Types

- `influenced_decision`
- `triggered_action`
- `produced_outcome`
- `recorded_as_memory`
- `generated_improvement`
- `validated_by_experiment` (Objective 51+)

## V1 Storage Strategy

V1 does not require a graph database. Use linked ids and metadata references across existing relational tables.

## V1 Query Goals

- Explain why an action happened
- Trace from observed friction to resulting proposal
- Compare repeated chains and outcome quality
- Identify high-value interventions for policy experiments

## Evolution Path

- V1: documented graph schema + link conventions
- V2: dedicated ExperienceRecord persistence and query endpoints
- V3: multi-session graph analytics and concept abstraction
