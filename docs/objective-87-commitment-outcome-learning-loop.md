# Objective 87: Commitment Outcome and Learning Loop

Objective 87 extends the operator commitment lifecycle past active enforcement. Once a commitment has played out, MIM now records whether it was satisfied, abandoned, ineffective, harmful, or superseded, and converts that result into reusable learning for later strategy, improvement, autonomy, UI, and inquiry decisions.

## Scope

- Add durable commitment outcome profiles tied to the existing operator commitment row.
- Evaluate post-commitment evidence across monitoring, stewardship, maintenance, inquiry, and execution-truth signals.
- Resolve commitments into terminal outcomes without introducing a parallel lifecycle store.
- Generate reusable learning signals for strategy scoring, backlog prioritization, autonomy boundaries, and future inquiry bias.
- Surface commitment outcome state in operator APIs and the MIM reasoning UI.

## Data Model

- `WorkspaceOperatorResolutionCommitment` remains the lifecycle anchor.
- `WorkspaceOperatorResolutionCommitmentOutcomeProfile` stores:
  - `outcome_status`
  - `outcome_reason`
  - evidence counts and retry pressure
  - learning signals
  - recent pattern summary
  - recommended follow-up actions

## API Surface

- `POST /operator/resolution-commitments/{commitment_id}/outcomes/evaluate`
  - Evaluates recent evidence and persists an outcome profile.
  - Applies a derived terminal status to the commitment when appropriate.
- `GET /operator/resolution-commitments/{commitment_id}/outcomes`
  - Lists recent outcome profiles for a commitment.
- `GET /operator/resolution-commitments/{commitment_id}/outcomes/{outcome_id}`
  - Returns a specific outcome profile.
- `POST /operator/resolution-commitments/{commitment_id}/resolve`
  - Allows an operator to mark a commitment as a terminal outcome directly.

## Learning Behavior

- Strategy scoring now considers the latest scoped commitment outcome.
- Improvement backlog refresh now uses the latest scoped commitment outcome as a priority influence.
- Adaptive autonomy boundaries now apply conservative caps when recent commitment outcomes were ineffective, abandoned, or harmful.
- Governed inquiry can now open a follow-up learning review when a recent commitment ended poorly.
- Inquiry answers can record a future commitment bias such as avoiding similar commitments.

## Operator Visibility

- `/mim/ui/state` now includes `operator_reasoning.commitment_outcome`.
- Current recommendation selection can prefer a problematic commitment outcome when it is the most important active operator-facing signal.

## Validation

Focused validation added in `tests/integration/test_objective87_commitment_outcome_learning_loop.py`:

- commitment outcome evaluation updates the commitment and downstream reasoning
- commitment learning inquiry records an avoid-similar bias
- manual terminal resolution to `abandoned`

Adjacent regression lane validated together:

- Objective 83
- Objective 84
- Objective 85
- Objective 86
- Objective 87
