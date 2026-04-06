# TOD-MIM Communication Acceptance Checklist (2026-04-01)

This checklist gates the synthetic conversation harness in this repository.

## Global Rules

- run only against synthetic roots
- do not read from or write to live `runtime/shared`
- treat `MIM_TOD_DIALOG.sessions.latest.json` as the actionable inbox
- answer the active request on the same session log
- do not reopen a new session when a valid reply already exists on the current session

## Diagnostic Roundtrip

- diagnostic request is written to a synthetic session log
- MIM appends exactly one `handoff_response`
- `reply_to_turn` targets the request turn
- TOD closes with `resolution_notice`

## Next-Step Consensus Roundtrip

- the session index is consulted before generic dialog history
- timed-out actionable sessions remain processable when `open_reply` still targets MIM
- Windows-style mirrored `session_path` values are resolved to the local synthetic dialog root
- MIM response includes `summary`
- MIM response includes `finding_positions`
- each finding position includes `finding_id`, `decision`, `reason`, `confidence`, and `local_blockers`

## Supersede And Reissue

- the superseded request and the reissued request stay on the same session id
- only the latest actionable request receives a MIM response
- MIM does not answer the superseded turn after the reissue exists
- TOD can close the reissued request with a same-session `resolution_notice`

## Gate Outcome

The harness passes only when every scenario passes and no scenario touches live shared artifacts.

## Standing Gate

- fast synthetic contract gate entrypoint: `scripts/run_tod_mim_contract_gate.sh`
- communication scenarios remain the first required half of that gate