# MIM Carrier Automation Feature Roadmap

Date: 2026-03-17
Status: proposed
Scope: carrier portal automation, auth/MFA handling, download/reconciliation, and run visibility.

## Priority Order

1. `web_session_control` (critical)
2. `auth_resolution` (critical)
3. `execution_recovery` (critical)
4. `structured_navigation_execution` (very important)
5. `carrier_playbook_manager` (very important)
6. `file_acquisition` (important)
7. `email_reader` (high value)
8. validation/reconciliation extension (important)
9. `execution_status_monitor` (important)

## Technology Baseline

1. Browser engine: Playwright (primary)
2. Runtime mode: headless by default, headed for debug replay
3. Session model: persistent storage state per carrier/account
4. Selector strategy: role/label/text first, CSS/XPath fallback only when needed

## Capability Contracts (Target)

### 1) `web_session_control`

Core operations:

1. open session
2. navigate URL
3. click selector
4. type value
5. wait for condition
6. detect page state
7. snapshot page (html, screenshot)

Suggested endpoints:

1. `POST /automation/web/sessions`
2. `POST /automation/web/sessions/{session_id}/navigate`
3. `POST /automation/web/sessions/{session_id}/actions`
4. `POST /automation/web/sessions/{session_id}/wait`
5. `GET /automation/web/sessions/{session_id}/state`
6. `DELETE /automation/web/sessions/{session_id}`

### 2) `auth_resolution`

Core operations:

1. credential inject
2. MFA challenge detection
3. pause-for-human checkpoint
4. resume-after-auth
5. session persistence (cookies/storage state)

Suggested endpoints:

1. `POST /automation/auth/resolve`
2. `POST /automation/auth/challenges/{challenge_id}/pause`
3. `POST /automation/auth/challenges/{challenge_id}/resume`
4. `GET /automation/auth/challenges/{challenge_id}`

### 3) `structured_navigation_execution`

Input contract:

```json
[
  {"action": "click", "selector": "#login"},
  {"action": "type", "field": "username", "value_ref": "credential.username"},
  {"action": "wait_for", "text": "Dashboard"}
]
```

Execution requirements:

1. step-by-step execution with per-step telemetry
2. stop on failure with exact failed step index
3. allow patch-and-resume from failed step

Suggested endpoint:

1. `POST /automation/navigation/execute`

### 4) `file_acquisition`

Core operations:

1. detect downloadable artifact
2. trigger download
3. confirm file existence/hash/size
4. store metadata (carrier, report type, run id, source url)

Suggested endpoints:

1. `POST /automation/files/detect`
2. `POST /automation/files/download`
3. `GET /automation/files/{file_id}`

### 5) `carrier_playbook_manager`

Playbook schema (minimum):

1. carrier id
2. login strategy
3. navigation steps
4. report discovery logic
5. parser/reconciliation rules
6. known failure signatures and recovery hints

Suggested endpoints:

1. `GET /automation/playbooks`
2. `POST /automation/playbooks`
3. `PUT /automation/playbooks/{carrier_id}`
4. `POST /automation/playbooks/{carrier_id}/refine`

### 6) `execution_recovery`

Failure classes:

1. navigation timeout
2. selector not found
3. stale session
4. MFA expired
5. download missing

Recovery policy:

1. bounded retries with jitter
2. branch-specific fallback actions
3. escalation to human with artifact bundle

Suggested endpoints:

1. `POST /automation/recovery/evaluate`
2. `POST /automation/recovery/retry`

### 7) `email_reader`

Phase 1 scope:

1. mailbox polling for MFA codes
2. extract report links/attachments
3. correlation to run/session ids

Suggested endpoints:

1. `POST /automation/email/poll`
2. `GET /automation/email/messages`
3. `POST /automation/email/extract-mfa`

### 8) Validation/Reconciliation Extension

Extend current validation to include:

1. month-over-month delta thresholds
2. missing carrier detection
3. anomaly flags with confidence
4. reconciliation status per carrier run

### 9) `execution_status_monitor`

Dashboard requirements:

1. status per carrier: pending, running, success, failed, blocked
2. last run timestamp + duration
3. required human actions queue
4. retry/escalation history

Suggested endpoints:

1. `GET /automation/runs`
2. `GET /automation/runs/{run_id}`
3. `GET /automation/runs/{run_id}/carriers`

## Delivery Phases

### Phase 1 (Foundation)

1. `web_session_control`
2. `auth_resolution` (pause/resume included)
3. `structured_navigation_execution` minimal interpreter
4. telemetry and audit trail for every step

Exit criteria:

1. log in and reach dashboard for 3 pilot carriers
2. handle at least one MFA challenge path

### Phase 2 (Operational)

1. `file_acquisition`
2. `carrier_playbook_manager`
3. `execution_recovery` v1

Exit criteria:

1. successful report download on pilot carriers
2. deterministic retry behavior for top failure classes

### Phase 3 (Scale)

1. `email_reader`
2. reconciliation/anomaly extension
3. `execution_status_monitor`

Exit criteria:

1. unattended run across multi-carrier batch with dashboard visibility
2. clear human intervention queue for blocked runs

## Guardrails

1. Do not hardcode per-carrier logic inside router/service code paths.
2. Keep carrier variability in playbooks and recovery rules.
3. Require artifact capture on every failure: screenshot, URL, DOM snippet, step index.
4. Keep secrets out of logs and journal payloads.

## Immediate Next Build Steps

1. Add capability placeholders to planning docs and implementation backlog (not manifest until implemented).
2. Scaffold `core/routers/automation.py` with session/auth/navigation/file endpoints.
3. Add `core/services/web_automation_service.py` backed by Playwright.
4. Define playbook schema and persistence model.
5. Ship pilot run for 1 carrier end-to-end before broad rollout.
