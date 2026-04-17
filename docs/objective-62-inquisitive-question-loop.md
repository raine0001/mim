# Objective 62: Inquisitive Question Loop

Objective 62 adds an uncertainty-triggered inquiry loop so MIM can pause at consequential ambiguity, ask structured explainable questions, and apply answer-driven downstream effects.

## Scope Implemented

- Added persisted inquiry question model and lifecycle in `workspace_inquiry_questions`.
- Added inquiry generation service with explicit trigger types:
  - `target_confidence_too_low`
  - `conflicting_domain_evidence`
  - `strategy_blocked_by_missing_information`
  - `repeated_soft_constraint_friction`
  - `low_confidence_perception_blocking_strategic_goal`
  - `ambiguous_next_action_under_multiple_valid_paths`
- Added inquiry API endpoints:
  - `POST /inquiry/questions/generate`
  - `GET /inquiry/questions`
  - `GET /inquiry/questions/{question_id}`
  - `POST /inquiry/questions/{question_id}/answer`
- Added explainability fields on each question:
  - triggering uncertainty and evidence
  - waiting decision context
  - safe no-answer behavior
- Implemented answer-effect coupling into downstream systems:
  - unblock horizon plans
  - shift strategy influence/ranking
  - create reobserve/rescan workspace proposals
  - adjust autonomy boundary profile
  - create bounded improvement proposals

## Validation Intent

Objective 62 verifies that the system asks only when uncertainty is materially relevant, avoids noisy-question spam, and safely defaults when unanswered while still allowing operator answers to change subsequent planning behavior.
