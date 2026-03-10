# MIM Deployment Policy (Frozen Rules)

These rules are mandatory for this host.

## Promotion rules

1. All code changes deploy to `test` first.
2. `test` smoke must pass before any prod change.
3. Production updates must come from a known git revision (recorded SHA).
4. Production containers must not receive ad hoc in-container edits.
5. Prod runtime paths or data must never be mounted into `test`.

## Isolation rules

- `prod` uses only `runtime/prod/*` paths.
- `test` uses only `runtime/test/*` paths.
- `test` API exposure is localhost-only by default.
- Secrets are sourced from `env/.env.prod` and `env/.env.test`; never commit real credentials.

## Security hardening baseline

- Keep `test` bound to localhost unless explicitly required.
- Expose only required ports (`8000` for prod app, DB internal to compose).
- Keep Docker socket access restricted (limit membership in `docker` group).
- Keep firewall rules explicit and minimal for required ingress.
- Do not store real secrets in git-tracked files.

## Host scope (allowed use)

- yes: MIM app/runtime host
- yes: test validation host
- maybe: lightweight DB host
- maybe later: model-serving host
- no for now: general experiment host for unrelated workloads

## Operational checks (required)

- Run `bash ./scripts/verify_isolation.sh` before promotion.
- Run `./scripts/smoke_test.sh test` before promotion.
- Run `./scripts/smoke_test.sh prod` after promotion.
- Record deployment metadata (git SHA, release tag, timestamp) via `./scripts/promote_test_to_prod.sh`.
