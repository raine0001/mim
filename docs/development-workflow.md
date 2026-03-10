# Development Workflow (Development PC → MIM Server)

## Purpose

This is the operating manual for delivering changes safely from development into production.

## Machine Responsibilities

### Development PC

Use for:
- all coding and refactoring
- branch management and pull requests
- local tests and linting
- preparing release SHA for promotion

Do not use for:
- direct production operations

### MIM Server

Use for:
- running test and prod container stacks
- smoke/health/backup/restore operations
- deployment and rollback execution

Do not use for:
- ad hoc feature coding inside containers

## Repository and Branch Strategy

- `main` = production-ready branch
- `dev` = integration branch deployed to test stack
- `feature/*` = active work branches

Recommended branch flow:
1. create `feature/*` from `dev`
2. implement and validate locally
3. merge `feature/*` into `dev`
4. deploy `dev` SHA to server test stack
5. run smoke + targeted verification
6. promote approved SHA to prod
7. merge/reconcile into `main` when production-ready

## Promotion Path

Development PC → Server test stack → Server prod stack

### Standard promotion sequence

1. On Development PC:
   - ensure clean git state
   - capture release SHA (`git rev-parse HEAD`)
   - push branch updates
2. On MIM Server (test validation):
   - deploy test image from approved SHA
   - run `./scripts/smoke_test.sh test`
   - run targeted checks for changed areas
3. On MIM Server (prod promotion):
   - run `./scripts/promote_test_to_prod.sh <release-tag>`
   - run `./scripts/smoke_test.sh prod`
   - confirm with `./scripts/mim_status.sh`
4. Record deployment in `runtime/prod/deployments.log`

## Deployment Checklist

Before promotion:
- [ ] Feature branch merged into `dev`
- [ ] Test stack updated to candidate SHA
- [ ] Test smoke passed
- [ ] Targeted verification passed
- [ ] Isolation check passed: `bash ./scripts/verify_isolation.sh`
- [ ] Fresh backup generated: `./scripts/backup_prod.sh`

After promotion:
- [ ] Prod smoke passed
- [ ] `/manifest` shows expected SHA/tag
- [ ] `./scripts/mim_status.sh` reviewed
- [ ] Deployment log entry present

## Rollback Trigger Rules

Trigger rollback immediately if any of these occur post-promotion:
- prod smoke fails
- critical endpoint regression (`/objectives`, `/tasks`, `/results`, `/reviews`)
- data integrity issue detected
- severe latency or repeated 5xx spikes

Rollback command:
- `./scripts/rollback_prod.sh` (or `./scripts/rollback_prod.sh <sha>`)

## Operational Guardrails

- Never edit production containers directly.
- Never mount prod data paths in test.
- Never promote unverified SHA.
- Keep test local-only unless explicitly required.
