# MIM Expert Communication Implementation Plan

## Objective

Upgrade MIM from deterministic, robotic conversation replies to expert-level communication while preserving the current safety, approval, and execution-control architecture.

## Target Outcome

MIM should:

- understand loose or multi-part user intent reliably
- answer directly before falling back to process language
- maintain natural, concise, context-aware replies
- ask at most one clarifying question unless safety requires more
- avoid repetitive canned phrasing
- preserve session continuity and execution boundaries
- use bounded Codex/OpenAI assistance for communication planning and drafting where appropriate

## Current Constraint

The main conversation path is still dominated by deterministic reply logic in gateway conversation handlers. That makes safety behavior stable, but it limits reply quality, flexibility, and naturalness.

## Fastest Credible Architecture

Use a hybrid model:

1. Keep deterministic routing for:
   - approvals
   - goal creation confirmation
   - hard safety boundaries
   - identity capture
   - execution dispatch separation
2. Add a model-backed communication composer for normal conversation and conversational follow-ups.
3. Feed the composer compact session context, current objective context, and explicit output rules.
4. Keep deterministic fallback if the model path fails validation or is unavailable.

## Implementation Phases

### Phase 1: Communication Contract

Define a structured communication contract for expert replies.

Required reply qualities:

- direct answer first
- short acknowledgement of user intent
- supporting context only when useful
- one next best step when action is helpful
- one clarifier maximum unless blocked by safety
- explicit uncertainty when knowledge is incomplete
- no repeated boilerplate unless required by policy

Required structured output fields:

- intent_understanding
- direct_answer
- supporting_context
- next_best_step
- optional_single_clarifier
- tone_profile
- confidence
- safety_boundary

### Phase 2: Communication Composer Service

Create a dedicated communication service that accepts:

- normalized user input
- recent session context
- last topic and last answer
- active objective summary
- current execution/safety posture
- communication style rules

The service should produce structured conversational output and plain-text reply text suitable for MIM UI rendering.

### Phase 3: Gateway Integration

Integrate the new composer into the current store-only conversation path.

Do not replace deterministic handling for:

- action approvals
- pending-action pause/resume/cancel
- direct safety refusals
- identity capture
- execution triggering

### Phase 4: Context Compression

Create a compact conversation briefing object that emphasizes:

- current user goal
- open threads
- prior answer summary
- session identity
- current objective
- current blockers
- recent result or failure context

This context should be concise enough for repeated model invocation without flooding the prompt.

### Phase 5: Evaluation Upgrade

Expand conversation evaluation so it measures:

- directness
- naturalness
- contextual coherence
- non-repetition
- initiative quality
- clarifier discipline
- uncertainty honesty
- usefulness of next step
- preservation of execution boundaries

Add scenario packs for:

- casual conversation
- ambiguous requests
- frustrated users
- planning requests
- corrective follow-ups
- return briefings
- multi-intent turns

### Phase 6: Shadow Rollout

Run deterministic and model-backed conversation in parallel for selected scenarios.

Promote model-backed replies only when:

- structured output validates
- safety posture is unchanged or better
- reply quality beats deterministic baseline
- no execution-boundary regression is introduced

## Execution Boundaries

The communication upgrade must not:

- create direct execution side effects from model output
- bypass operator approval rules
- bypass TOD/MIM contract boundaries
- introduce a second unmanaged execution authority
- mix freeform model output directly into dispatch instructions

## Resource Utilization

Use the existing local broker and handoff pipeline to support bounded communication-improvement work.

Allowed uses:

- Codex-assisted implementation planning
- OpenAI-assisted bounded response-composer drafting
- structured comparison of baseline vs upgraded replies
- bounded help generating communication rubrics and test packs

Not allowed:

- unconstrained tool-calling loops
- freeform execution delegation without bounded artifact validation

## First Bounded Build Slice

1. Add a communication rubric module.
2. Add a communication composer service with structured outputs.
3. Wire it into the normal conversation path only.
4. Preserve deterministic fallback.
5. Add focused tests for expert communication quality.
6. Expand the evaluation harness and run comparison reports.

## Success Criteria

- MIM answers normal questions with materially more natural replies.
- Repetition drops sharply.
- Clarification spam approaches zero.
- Current safety/approval tests stay green.
- Session continuity remains intact.
- The evaluation harness shows durable quality improvement.

## Immediate Deliverables

- communication rubric and response contract
- communication composer service skeleton
- gateway integration seam behind a feature flag
- upgraded evaluation metrics
- focused regression tests for communication quality
- shadow-mode comparison report