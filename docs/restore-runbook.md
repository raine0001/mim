# MIM Restore Runbook

## A) Host reboot recovery

1. Ensure Docker is up: `sudo systemctl status docker`.
2. Start stacks: `sudo systemctl start mim-prod mim-test`.
3. Verify: `./scripts/smoke_test.sh prod && ./scripts/smoke_test.sh test`.

## B) Container rebuild recovery

1. Rebuild prod stack: `sudo docker compose -f docker/prod/compose.yaml --env-file env/.env.prod up -d --build`.
2. Rebuild test stack: `sudo docker compose -f docker/test/compose.yaml --env-file env/.env.test up -d --build`.
3. Verify with smoke tests.

## C) Prod database restore

1. Select backup file in `runtime/prod/backups/mim_prod_*.sql`.
2. Ensure prod DB container is running.
3. Restore:
   `cat <backup.sql> | sudo docker compose -f docker/prod/compose.yaml --env-file env/.env.prod exec -T mim_db_prod psql -U mim_prod -d mim_prod`
4. Run `./scripts/smoke_test.sh prod`.

## D) Full prod rehydrate

1. Pull repo at known good revision.
2. Restore `env/.env.prod` (or secure secret source).
3. Restore prod DB from backup.
4. Restore prod runtime data dirs as needed (`runtime/prod/reports`, `runtime/prod/uploads`, `runtime/prod/artifacts`).
5. Start stack: `sudo systemctl start mim-prod`.
6. Validate: `./scripts/smoke_test.sh prod` and `curl -sS http://127.0.0.1:8000/manifest`.

## E) Backup restore test procedure (monthly)

1. Pick latest prod backup and env snapshot.
2. Apply into test DB (`mim_db_test`) using the same restore pattern.
3. Run `./scripts/smoke_test.sh test`.
4. Record result in operations notes.
