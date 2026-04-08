# Objectives 111-115 Production Promotion Report

Date: 2026-04-07
Objectives: 111-115
Title: Bounded Composed ARM Task Production Promotion
Release Tag: objective-115
Recorded Git SHA: df4981105d9d19ef47ea025a1f638e25b3c282c5
Recorded Build Timestamp: 2026-04-07T04:08:49Z
Manifest Schema Version: 2026-03-24-70

## Promotion Outcome

- Promotion: EXECUTED
- Production Health: PASSED
- Production Smoke: PASSED
- Manifest Verification: PASSED
- Isolation Verification: PASSED
- Governance Caveat: dirty working tree deployed

Production deployment was executed in this session.

## Completed Host Flow

Per `docs/deployment-policy.md`, the required flow is:

- `bash ./scripts/verify_isolation.sh`
- `./scripts/smoke_test.sh test`
- `./scripts/promote_test_to_prod.sh objective-115`
- `./scripts/smoke_test.sh prod`

Observed results in this session:

- `verify_isolation.sh`: PASS (`compose definitions keep prod/test runtime paths isolated`)
- `smoke_test.sh test`: PASS
- `promote_test_to_prod.sh objective-115`: PASS
- `smoke_test.sh prod`: PASS

## Deployment Evidence

Stamped production metadata:

- `RELEASE_TAG=objective-115`
- `BUILD_GIT_SHA=df4981105d9d19ef47ea025a1f638e25b3c282c5`
- `BUILD_TIMESTAMP=2026-04-07T04:08:49Z`

Deployment log:

- `2026-04-07T04:08:49Z release=objective-115 git_sha=df4981105d9d19ef47ea025a1f638e25b3c282c5`

Post-promotion production manifest surface confirmed on `http://127.0.0.1:8000/manifest`:

- `environment = prod`
- `release_tag = objective-115`
- `git_sha = df4981105d9d19ef47ea025a1f638e25b3c282c5`
- `build_timestamp = 2026-04-07T04:08:49Z`
- `schema_version = 2026-03-24-70`

## Provenance Caveat

The production image was built from the current workspace while the repository had uncommitted changes.

That means the recorded `BUILD_GIT_SHA` points to the checked-out HEAD commit, but it is not a cryptographically exact identifier for the full image contents that were deployed in this session.

Operationally, the promotion succeeded and the live prod surface is healthy. Governance-wise, this deployment should be treated as production-promoted with a provenance caveat until the same content is captured in a clean commit and, if required, re-promoted from that clean revision.

## Readiness Traceability

Promotion readiness evidence remains recorded in `docs/objective-111-115-promotion-readiness-report.md`.

That readiness record includes:

- focused regression coverage (`36/36` pass)
- review-loop regression coverage (`29/29` pass)
- live composed proof trace `trace-de8e318b97644fe4851a782d99bfef1e`
- final composed-task state `status = completed`