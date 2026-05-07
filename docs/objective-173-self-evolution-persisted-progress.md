# Objective 173 - Self-Evolution Persisted Progress And Promotion

## Goal

Turn the six-hour self-evolution plan from Objective 172 into real persisted runtime state so MIM can:

- remember the active slice across requests
- evaluate pass or fail against the current slice gate
- auto-promote to the next slice on pass
- hold the current slice on fail or block
- expose actual progress through briefing, UI state, and gateway replies

## Implementation

- Reused the existing workspace state bus snapshot store as the persistence layer for natural-language self-evolution progress.
- Added persisted progress state keyed by actor and source so tests and live runs do not share the same slice cursor.
- Added evaluation logic for the active slice using the slice pass metrics and forbidden failure tags.
- Added three improvement routes:
  - `GET /improvement/self-evolution/natural-language/progress`
  - `POST /improvement/self-evolution/natural-language/reset`
  - `POST /improvement/self-evolution/natural-language/evaluate`
- Hydrated the self-evolution briefing from persisted progress so:
  - `active_slice`
  - `selected_skill`
  - `next_step_summary`
  - `progress_summary`
  all reflect the current runtime cursor instead of always pointing at slice 1.
- Mirrored the progress summary into operator-visible state and gateway next-work responses.
- Added integration coverage for reset, pass promotion, fail hold, and blocked hold behavior.

## Result

MIM now exposes a real six-hour slice cursor rather than only a descriptive plan. The self-evolution loop can persist and report:

- which slice is active now
- how many slices have completed in the current cycle
- whether the current slice is running, repairing, blocked, or stopped
- what proof was recorded on the last evaluation
- when the next slice promotion happened automatically