# MIM Carrier Automation Capabilities

Date: 2026-03-17
Scope: Automation APIs under `/automation/*`, environment configuration, and live-browser prerequisites.

## Environment Source

Runtime now loads both:

1. `/home/testpilot/mim/.env`
2. `/home/testpilot/mim/env/.env`

If the same key exists in both files, `env/.env` takes precedence.

## Required Variables

Automation controls:

1. `AUTOMATION_ENABLED=true`
2. `AUTOMATION_DEFAULT_SIMULATION=true|false`
3. `AUTOMATION_ALLOW_LIVE_BROWSER=true|false`
4. `AUTOMATION_BROWSER_HEADLESS=true|false`
5. `AUTOMATION_STORAGE_DIR=/home/testpilot/mim/runtime/automation`
6. `AUTOMATION_DEFAULT_TIMEOUT_SECONDS=20`

Email ingestion (IMAP) for MFA/report extraction:

1. `IMAP_HOST=...`
2. `IMAP_PORT=993`
3. `IMAP_USERNAME=...`
4. `IMAP_PASSWORD=...`
5. `IMAP_USE_SSL=true`
6. `IMAP_INBOX=INBOX`

SMTP placeholders (future outbound workflows):

1. `SMTP_HOST=...`
2. `SMTP_PORT=587`
3. `SMTP_USERNAME=...`
4. `SMTP_PASSWORD=...`
5. `SMTP_USE_TLS=true`
6. `SMTP_FROM_ADDRESS=...`

## Live Browser Prerequisites

For non-simulation browser sessions (`simulation_mode=false`), both are required:

1. `AUTOMATION_ALLOW_LIVE_BROWSER=true`
2. Playwright installed in the runtime environment

Install commands:

```bash
/home/testpilot/mim/.venv/bin/pip install playwright
/home/testpilot/mim/.venv/bin/playwright install chromium
```

If prerequisites are missing, `POST /automation/web/sessions` returns:

1. `422 {"detail":"playwright_not_installed"}`
2. `422 {"detail":"live_browser_disabled"}`

## Endpoint Groups

1. Web sessions: `/automation/web/sessions*`
2. Auth + MFA pause/resume: `/automation/auth/*`
3. Navigation executor: `/automation/navigation/execute`
4. Files detect/download: `/automation/files/*`
5. Playbooks: `/automation/playbooks*`
6. Recovery: `/automation/recovery/*`
7. Email poll/extract: `/automation/email/*`
8. Reconciliation: `/automation/reconciliation/evaluate`
9. Runs + status dashboard: `/automation/runs*`, `/automation/status/monitor`

## Validation Commands

Objective 75 integration subset:

```bash
MIM_TEST_BASE_URL=http://127.0.0.1:18001 \
/home/testpilot/mim/.venv/bin/python -m unittest -v \
  tests/integration/test_objective75_web_summary_gateway.py \
  tests/integration/test_objective75_carrier_automation_api.py
```
