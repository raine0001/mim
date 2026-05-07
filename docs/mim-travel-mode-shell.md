# MIM Travel Mode Shell

This runbook deploys a phone-safe remote shell for MIM while the authoritative runtime stays on the local desk box.

## Scope

- Local runtime remains the source of truth.
- Remote access is browser-based over HTTPS.
- Travel mode blocks destructive changes, large refactors, and hardware actions by default.
- Allowed remote work stays bounded: training, UI fixes, parsing fixes, state validation, and small runtime patches.

## Runtime Surface

- `GET /shell`: mobile-first remote shell UI.
- `GET /shell/state`: compact travel-safe state payload.
- `POST /shell/chat`: remote chat bridge with travel-mode guardrails.
- `GET /shell/health`: shell health snapshot.
- `GET /shell/reports/daily`: current daily summary.
- `GET /shell/reports/blockers`: blocker report.

## Phase 1: Local Runtime Prep

1. Keep the local MIM runtime under the existing user service.
1. Verify the health and shell endpoints:

```bash
curl -fsS http://127.0.0.1:18001/health
curl -fsS http://127.0.0.1:18001/shell/health
curl -fsS http://127.0.0.1:18001/shell/state | python -m json.tool
```

1. Verify remote-shell chat locally:

```bash
curl -fsS -X POST http://127.0.0.1:18001/shell/chat \
  -H 'content-type: application/json' \
  -d '{"message":"Show current blockers and validate shell state."}' | python -m json.tool
```

1. Verify destructive requests are blocked:

```bash
curl -fsS -X POST http://127.0.0.1:18001/shell/chat \
  -H 'content-type: application/json' \
  -d '{"message":"Delete the repo logs and wipe runtime state."}' | python -m json.tool
```

## Phase 2: Cloudflare Tunnel

1. Install `cloudflared` on the local MIM box.

The current workstation install is user-space:

```bash
/home/testpilot/.local/bin/cloudflared --version
```

If you want the bare command available in future shells:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

1. Provision or recover the named tunnel identity:

```bash
/home/testpilot/mim/scripts/provision_mim_cloudflare_tunnel.sh
```

The provisioner will:

- create or reuse the named tunnel
- write `~/.cloudflared/mim-travel-shell.json`
- write `deploy/cloudflare/mim-shell-tunnel.yml` once `MIM_REMOTE_SHELL_HOSTNAME` is set
- create the DNS CNAME automatically if both `MIM_REMOTE_SHELL_ZONE` and a zone-scoped API token are available
- infer `MIM_REMOTE_SHELL_ZONE` automatically when the hostname suffix matches one visible zone

Credential note:

- the provisioner reads `CLOUDFLARE_API_TOKEN`
- if `CLOUDFLARE_API_TOKEN` is unset, it will fall back to `CLOUDFLARE_CUSTOM_KEY`
- for DNS-zone operations, set `CLOUDFLARE_ZONE_API_TOKEN` when the tunnel token cannot see the zone; otherwise the provisioner falls back to `CLOUDFLARE_GLOBAL_API_TOKEN`, then `CLOUDFLARE_API_TOKEN`
- a valid tunnel-scoped token is not enough for hostname cutover; the token must also see the target DNS zone

1. If you need to prepare the config manually, create a real config from the example and set the hostname you will publish:

```bash
cp deploy/cloudflare/mim-shell-tunnel.example.yml deploy/cloudflare/mim-shell-tunnel.yml
```

The copied config should point at the named tunnel UUID and the user-space credential file under `~/.cloudflared`.

1. Route your hostname:

```bash
/home/testpilot/mim/scripts/provision_mim_cloudflare_tunnel.sh
```

If the Cloudflare account has no DNS zone yet, the provisioner will stop after creating the named tunnel and credential file and will print `visible_zone_count=0`. That is the only remaining blocker to a stable public tunnel hostname.

For the stable long-term path, set these env vars before rerunning the provisioner:

```bash
MIM_REMOTE_SHELL_HOSTNAME=mim.yourdomain.com
MIM_REMOTE_SHELL_ZONE=yourdomain.com
```

If `MIM_REMOTE_SHELL_ZONE` is omitted and the token can already see the zone, the provisioner will infer it from `MIM_REMOTE_SHELL_HOSTNAME`.

1. Run the tunnel locally:

```bash
/home/testpilot/mim/scripts/run_cloudflared_tunnel.sh
```

1. Or install the user service once the real config exists:

```bash
/home/testpilot/mim/scripts/install_mim_travel_shell_user_units.sh
```

1. Protect the hostname with Cloudflare Access at minimum.

## Phase 3: Remote Shell UI

- Default remote UI: `https://mim.yourdomain.com/shell`
- Compact state: `https://mim.yourdomain.com/shell/state`
- Reports: `/shell/reports/daily` and `/shell/reports/blockers`

While the tunnel hostname is still pending, set `MIM_REMOTE_SHELL_DOMAIN` to the stable Worker URL so `/shell/state` advertises the current public entrypoint.

The shell is optimized for:

- one conversation thread
- compact objective and task visibility
- health visibility
- blocker visibility
- phone-safe interaction

## Phase 4: Optional Render Frontend

If you later want a separate public frontend, keep it thin.

- Render should call the desk-box origin through Cloudflare.
- Do not move the MIM runtime authority to Render.
- Keep auth and summary rendering in the cloud layer only.

### Optional Cloudflare Worker Frontend

This repo now includes a minimal Worker deploy target so `npx wrangler deploy` has an explicit entrypoint instead of trying to guess a static site.

1. Deploy the Worker from the repo root:

```bash
npx wrangler deploy
```

1. Set the Worker variable `MIM_REMOTE_SHELL_ORIGIN` to the public shell origin you want the Worker to proxy, for example:

```text
https://mim.yourdomain.com
```

1. Optional: set `MIM_REMOTE_SHELL_PATH_PREFIX` if the shell lives behind a non-root path on the upstream origin.

Do not check a tunnel origin into `wrangler.toml`. Pass the current origin at deploy time or set it in Cloudflare so a dead tunnel URL cannot be silently promoted by a future deploy.

1. Verify the Worker wiring:

```bash
curl -fsS https://<your-worker-domain>/healthz
```

Without `MIM_REMOTE_SHELL_ORIGIN`, the Worker still deploys successfully and returns a setup page instead of failing the build.

## Phase 5: Restart And Recovery

1. Rehydrate the named tunnel credentials and config:

```bash
/home/testpilot/mim/scripts/provision_mim_cloudflare_tunnel.sh
```

1. Restart the local runtime and the tunnel user service:

```bash
systemctl --user restart mim-mobile-web.service
systemctl --user restart mim-cloudflared-tunnel.service
```

1. Verify local shell health and the named tunnel process:

```bash
curl -fsS http://127.0.0.1:18001/shell/health | python -m json.tool
systemctl --user --no-pager --full status mim-cloudflared-tunnel.service | sed -n '1,120p'
```

1. Redeploy the Worker with the current upstream origin if the origin changed:

```bash
./scripts/deploy_mim_cloudflare_worker.sh
```

That helper reads `MIM_REMOTE_SHELL_HOSTNAME` first and automatically falls back to a Node 20 Wrangler runtime on hosts that still have Node 18.

1. Verify the public entrypoint from an external network:

```bash
curl -fsS https://mim.yourdomain.com/healthz | python -m json.tool
curl -fsS https://mim.yourdomain.com/shell/state | python -m json.tool
```

## Phase 6: Travel Mode Safeguards

Environment flags:

- `MIM_TRAVEL_MODE_ENABLED=true`
- `MIM_TRAVEL_MODE_ALLOW_DESTRUCTIVE=false`
- `MIM_TRAVEL_MODE_ALLOW_LARGE_REFACTORS=false`
- `MIM_TRAVEL_MODE_ALLOW_HARDWARE_ACTIONS=false`

The remote shell blocks:

- destructive repo or runtime changes
- broad rewrite / large refactor requests
- host-control and hardware actions

## Acceptance Checklist

- Local runtime survives service restarts and still serves `/shell/state`.
- `/shell/chat` sends and receives bounded conversation traffic.
- Shell thread survives page reload because messages are persisted server-side.
- Travel mode rejects blocked categories with explicit operator feedback.
- Cloudflare tunnel exposes the local runtime without opening inbound ports.
- The tunnel service starts from the user account without requiring sudo or a root-owned `cloudflared` install.
