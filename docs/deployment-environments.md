# MIM Deployment Environments

This project uses two isolated app environments on the same workstation:

- `mim-prod`: stable runtime (`localhost:8000`)
- `mim-test`: validation playground (`localhost:8001`)

## Isolation model

- Separate compose stacks: `docker/prod/compose.yaml`, `docker/test/compose.yaml`
- Separate env files: `env/.env.prod`, `env/.env.test`
- Separate persistent paths:
  - `runtime/prod/data`, `runtime/prod/logs`, `runtime/prod/reports`, `runtime/prod/backups`
  - `runtime/test/data`, `runtime/test/logs`, `runtime/test/reports`, `runtime/test/backups`
- Separate DBs/users per stack (`mim_prod`, `mim_test`)

### Explicit isolation matrix

| Resource | test | prod |
|---|---|---|
| Database path | `runtime/test/data/postgres` | `runtime/prod/data/postgres` |
| Uploads/assets | `runtime/test/uploads`, `runtime/test/artifacts` | `runtime/prod/uploads`, `runtime/prod/artifacts` |
| Reports | `runtime/test/reports` | `runtime/prod/reports` |
| Logs | `runtime/test/logs` | `runtime/prod/logs` |
| Temp/work | `runtime/test/tmp`, `runtime/test/work` | `runtime/prod/tmp`, `runtime/prod/work` |

Verify at any time with:

```bash
bash ./scripts/verify_isolation.sh
```

## Startup

```bash
docker compose -f docker/prod/compose.yaml --env-file env/.env.prod up -d --build
docker compose -f docker/test/compose.yaml --env-file env/.env.test up -d --build
```

## Smoke tests

```bash
./scripts/smoke_test.sh test
./scripts/smoke_test.sh prod
```

## Backup production

```bash
./scripts/backup_prod.sh
```

Retention is controlled by `BACKUP_RETENTION_DAYS` in `env/.env.prod`.
Backup includes:
- SQL dump (`mim_prod_*.sql`)
- env snapshot (`mim_prod_env_*.env`)
- data archive (`mim_prod_data_*.tgz`) containing `reports`, `uploads`, `artifacts`

### Scheduled automation

- Daily prod backup: `mim-backup-prod.timer` (02:30 local)
- Periodic health check: `mim-healthcheck.timer` (every 5 minutes)

Check timers:

```bash
sudo systemctl list-timers --all | grep -E 'mim-backup-prod|mim-healthcheck'
```

## Promotion flow

1. Deploy and validate in test
2. Run `./scripts/smoke_test.sh test`
3. Run `./scripts/backup_prod.sh`
4. Run `./scripts/promote_test_to_prod.sh`
5. Run `./scripts/smoke_test.sh prod`

## Deployment confidence tools

- System status: `./scripts/mim_status.sh`
- Rollback prod to prior deployment SHA: `./scripts/rollback_prod.sh [git_sha]`
- Run restore test from latest backup artifacts: `./scripts/restore_test_prod_backup.sh`

## Boot-time auto-start with systemd

Install and enable both stacks as system services:

```bash
./scripts/install_systemd_units.sh
```

Start services immediately:

```bash
sudo systemctl start mim-prod
sudo systemctl start mim-test
```

Check status/logs:

```bash
sudo systemctl status mim-prod mim-test
sudo journalctl -u mim-prod -u mim-test -n 100 --no-pager
```

## Safety rules

- Never point test stack at prod DB/paths.
- Never run prod with debug-oriented changes first.
- Rotate `POSTGRES_PASSWORD` values before shared use.
- Keep test access LAN/local only unless explicitly required.

Additional frozen policy is documented in `docs/deployment-policy.md`.
Recovery procedures are documented in `docs/restore-runbook.md`.
