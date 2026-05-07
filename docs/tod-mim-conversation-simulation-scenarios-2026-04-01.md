# TOD-MIM Conversation Simulation Scenarios (2026-04-01)

This repository carries a MIM-side mirror of the April 1 synthetic interoperability catalog so communication regressions can be exercised without touching live shared state.

## Scope

The harness must run only against synthetic roots.

Each scenario creates its own synthetic `shared` and `shared/dialog` tree, writes the same session-index and session-log artifacts used in live coordination, and validates that MIM responds on the same session.

## Scenario Catalog

### 1. Diagnostic Roundtrip

- Purpose: prove that TOD can open a synthetic diagnostic session, MIM can answer it, and TOD can close the same session.
- Synthetic sequence:
  - TOD appends a `handoff_request` with `intent=diagnostic_roundtrip`.
  - MIM appends a `handoff_response` with a bounded diagnostic summary.
  - TOD appends a `resolution_notice` on the same session.
- Pass conditions:
  - exactly one MIM response is appended
  - `reply_to_turn` points to the diagnostic request turn
  - TOD closes the same session with `resolution_notice`

### 2. Next-Step Consensus Roundtrip

- Purpose: prove that MIM consumes the actionable inbox first, resolves the session path, and emits a valid `handoff_response` for next-step consensus.
- Synthetic sequence:
  - TOD writes `MIM_TOD_DIALOG.sessions.latest.json` with an actionable session entry.
  - The session entry uses the live edge shape that previously caused misses:
    - `status=timed_out`
    - `open_reply.to=MIM`
    - `open_reply.message_type=handoff_request`
    - `last_message.message_type=status_request`
    - Windows-style `session_path`
  - TOD appends a `handoff_request` with `response_contract.required_fields = [summary, finding_positions]`.
  - MIM appends a `handoff_response` on the same session.
  - TOD appends a `resolution_notice` after consuming the response.
- Pass conditions:
  - MIM processes the timed-out indexed session
  - response includes `summary`
  - response includes `finding_positions[]`
  - each `finding_positions[]` entry contains:
    - `finding_id`
    - `decision`
    - `reason`
    - `confidence`
    - `local_blockers`

### 3. Supersede And Reissue On The Same Session

- Purpose: prove that a superseded request can be reissued on the same session without reopening a new dialog stream and that MIM answers only the latest actionable request.
- Synthetic sequence:
  - TOD appends an initial `handoff_request`.
  - TOD appends a `resolution_notice` marking the earlier request superseded.
  - TOD appends a replacement `handoff_request` on the same session.
  - MIM appends one `handoff_response` replying to the replacement request turn.
  - TOD appends a final `resolution_notice` after consuming the replacement response.
- Pass conditions:
  - only one MIM response is emitted
  - that response points to the reissued turn, not the superseded turn
  - the session id stays constant across the reissue sequence

## Harness Entry Points

- PowerShell entrypoint: `tod/Invoke-TODMimConversationSimulation.ps1`
- Python implementation: `scripts/run_tod_mim_conversation_simulation.py`
- Regular regression gate: `tests/tod/test_tod_mim_conversation_simulation.py`