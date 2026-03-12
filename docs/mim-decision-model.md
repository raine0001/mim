# MIM Decision Model

This document defines the Decision and Improvement concepts that let MIM reason about why actions were chosen and how policy should evolve.

## Purpose

The decision model provides an auditable structure for:

- what was decided
- why it was decided
- what constraints/preferences/strategies influenced it
- what result occurred
- whether future behavior should be revised

## Core Concepts

## DecisionRecord

Decision records are the canonical trace of choice.

### Required fields

- `decision_type`: category of decision (for example: `strategy_selection`, `constraint_evaluation`, `maintenance_action`)
- `source_context`: endpoint/source/actor context
- `relevant_state`: state snapshot used for the decision
- `preferences_applied`: preference signals considered
- `constraints_applied`: hard/soft constraints applied
- `strategies_applied`: strategy context applied
- `options_considered`: alternatives explicitly evaluated
- `selected_option`: selected path
- `decision_reason`: human-readable reason
- `confidence`: decision confidence
- `result_quality`: post-outcome quality estimate
- `resulting_goal_or_plan_id`: downstream linkage id
- `metadata_json`: extension metadata
- `created_at`: timestamp

### Interpretation

A DecisionRecord is not merely logging. It is a structured explanation object that can be reused for:

- explainability endpoints
- policy debugging
- proposal generation
- post-incident analysis

## ImprovementProposal

Improvement proposals are review-gated candidate changes generated from recurring friction patterns.

### Required fields

- `proposal_type`
- `trigger_pattern`
- `evidence_summary`
- `evidence_json`
- `affected_component`
- `suggested_change`
- `confidence`
- `safety_class`
- `risk_summary`
- `test_recommendation`
- `status`
- `review_reason`
- `metadata_json`
- `created_at`

### Lifecycle

`proposed -> accepted|rejected -> artifact pending_review -> gated implementation`

### Safety boundary

ImprovementProposal acceptance does not mutate runtime policy directly. It produces bounded artifacts for test/review workflows.

## Decision Quality and Learning Signals

Decision quality must be estimated after outcomes are observed.

### Quality sources

- validation success/failure
- retry/replan pressure
- operator overrides
- safety warnings triggered
- stability of resulting state

### Usage

Quality signals feed:

- improvement proposal generation
- strategy refinement
- constraint weight learning
- policy experiment candidate selection

## Decision Model Contracts in Code

Primary implementation surfaces:

- `core/models.py` (`WorkspaceDecisionRecord`, `WorkspaceImprovementProposal`, `WorkspaceImprovementArtifact`)
- `core/decision_record_service.py`
- `core/improvement_service.py`
- `core/routers/decision_records.py`
- `core/routers/improvement.py`

## Governance

All model changes follow:

`implement -> focused gate -> full regression gate -> promote -> production verification -> report`

This ensures decision intelligence evolves without bypassing safety and auditability.
