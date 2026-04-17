# MIM Development Autonomy Policy

Date: 2026-03-29
Status: active
Owner: MIM

## Purpose

Prevent execution stalls when the workspace is dirty and MIM must decide how to continue development without repeatedly asking for manual direction.

## Scope

Applies to all feature-development, integration, and test work in this repository when uncommitted changes are present.

## Decision Modes

MIM may choose one of three modes:

1. `MODE_A_TARGETED_CONTINUE`
- Continue work while touching only files required for the active objective.
- Ignore unrelated modified files.

2. `MODE_B_EXPANDED_REVIEW`
- Include review and possible edits to nearby changed test files if they are directly coupled to the active objective.

3. `MODE_C_PAUSE_FOR_SNAPSHOT`
- Stop and require commit/stash only when safety risk exceeds threshold.

## Default Rule

Default to `MODE_A_TARGETED_CONTINUE`.

This is the standard operating mode unless hard-stop conditions are triggered.

## Hard-Stop Conditions (force MODE_C)

MIM must pause for snapshot only if one or more are true:

- Merge conflicts exist in objective-adjacent files.
- The same file needed for objective work has unresolved, unknown edits that change behavioral intent and cannot be safely merged by inspection.
- Repository state indicates destructive ambiguity (for example, rebases in progress with unresolved markers).
- Required validation cannot be trusted due to unstable environment state that cannot be isolated.

## Escalation Rule for MODE_B

Use `MODE_B_EXPANDED_REVIEW` only if all are true:

- Existing changed tests are in the same domain as the active objective.
- Failing or likely-failing tests block confidence in the objective change.
- Reviewing those tests materially reduces regression risk for this objective.

## Operational Algorithm

1. Detect dirty workspace and list changed files.
2. Classify changed files as `objective-coupled` or `unrelated`.
3. Evaluate hard-stop conditions.
4. If hard-stop true, choose `MODE_C_PAUSE_FOR_SNAPSHOT`.
5. Else evaluate expanded-review criteria.
6. If expanded-review true, choose `MODE_B_EXPANDED_REVIEW`.
7. Otherwise choose `MODE_A_TARGETED_CONTINUE`.
8. Log selected mode and rationale in objective documentation.

## File-Touch Guardrails

When in `MODE_A_TARGETED_CONTINUE`:

- Edit only objective-coupled files.
- Do not revert or reformat unrelated files.
- Run targeted tests relevant to touched files.
- Report validation results and residual risk.

## Current Session Decision

Selected mode: `MODE_A_TARGETED_CONTINUE`

Rationale:

- Workspace has many unrelated modifications.
- No evidence of merge conflicts in objective-coupled orchestration integration files.
- The active task can proceed safely via scoped edits and targeted validation.

## Change Control Record Template

Use this block in objective docs when a decision is made:

- policy_mode:
- objective_scope:
- hard_stop_detected:
- expanded_review_needed:
- touched_files:
- validations_run:
- outcome:

## Exit Criteria

Policy is successful when:

- MIM no longer blocks on repetitive mode-selection prompts.
- Objective work continues with scoped edits.
- Regressions from unrelated workspace changes are avoided.
- Decision rationale is consistently documented.
