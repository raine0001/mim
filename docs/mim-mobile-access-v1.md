# MIM Mobile Access v1

Date: 2026-04-13
Purpose: establish the fastest realistic path to operator phone access for the current MIM interface.
Scope: reuse the existing `/mim` session flow before any dedicated phone-assistant or telecom automation expansion.

## Decision

Use mobile web as the first phone shell for MIM.

Do not make `mim_wall` the first shell for the current `/mim` interface.

## Why

`mim_wall` is an Android app for call screening, SMS handling, notifications, and phone-side action queues. It is a separate client and its current integration boundary is gateway-event ingestion, not embedding or hosting the current `/mim` UI flow.

The current MIM interface already exists as a browser UI at `/mim`, includes mobile viewport support, and is the shortest path to usable phone access this week.

## First Implementation Path

1. Run the current-source MIM server on the workspace runtime.
2. Bind it to a phone-reachable host when needed.
3. Open `/mim` from the operator phone browser.

Local workstation launch:

```bash
/home/testpilot/mim/.venv/bin/python -m uvicorn core.app:app --host 127.0.0.1 --port 18001
```

Phone-reachable LAN launch:

```bash
/home/testpilot/mim/.venv/bin/python -m uvicorn core.app:app --host 0.0.0.0 --port 18001
```

Repo-supported launcher:

```bash
/home/testpilot/mim/scripts/run_mim_mobile_web.sh
```

Simple local start wrapper:

```bash
/home/testpilot/mim/mim_start
```

Persistent startup on the MIM box:

```bash
/home/testpilot/mim/scripts/install_mim_box_user_units.sh
sudo loginctl enable-linger $USER
```

That installs a user `systemd` service for the `:18001` MIM runtime plus a health watcher that restarts it when `/mim/ui/health` stays degraded.

Phone URL:

```text
http://<host-ip>:18001/mim
```

Current workstation LAN URL:

```text
http://192.168.1.120:18001/mim
```

## Validation Contract

The mobile-web path is valid only if one stable conversation session can do all of the following from the same `/mim` browser flow:

1. Open `/mim` on a mobile viewport.
2. Preserve one `conversation_session_id` across turns.
3. Ask `catch me up`.
4. Ask for the current objective.
5. Ask for warnings.
6. Ask one real development or integration question.

The focused integration proof for this contract lives in `tests/integration/test_objective167_mobile_web_remote_continuity.py` and uses a mobile browser user agent plus the existing `/gateway/intake/text` conversation path.

## Explicit Non-Decision

This slice does not add:

- a dedicated mobile app for the `/mim` UI
- call or text automation from the phone
- backend token-proxy infrastructure
- deeper `mim_wall` gateway coupling

## Next Slice If Needed

If mobile web proves insufficient, the next fastest reuse path is a light wrapper around `/mim`, not a full `mim_wall` integration. That wrapper should preserve the existing MIM session flow and avoid duplicating assistant logic.
