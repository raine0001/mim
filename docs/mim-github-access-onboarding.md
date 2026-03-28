# MIM GitHub Access Onboarding

This runbook defines the minimum information needed to grant MIM access to your repositories safely.

## 1. Required Inputs

- Authentication mode: fine-grained PAT, GitHub App, or SSH.
- Secret storage location: environment variable, password manager, CI secret, or keychain.
- MIM git identity: commit name and email.
- Repository policy: read-only vs read/write per repository.
- Branch safety policy: PR required, protected branches, direct-push rules.
- Local workspace layout: single workspace vs multi-repo clone root.
- Audit policy: whether signed commits are required.

## 2. Repository Scope (Provided)

- raine0001/comm_app
- raine0001/coachMIM
- raine0001/TOD
- raine0001/mim_wall
- raine0001/doach_app
- raine0001/mim_pulz
- raine0001/mim
- raine0001/MIMweb
- raine0001/viasion_app
- raine0001/mim_arm
- raine0001/doach-ai-site
- raine0001/mediapipe
- raine0001/Golf-Swing-Pose-Analysis-Model-Based-on-YOLOv11-Pose
- raine0001/AI-Basketball-Shot-Detection-Tracker
- raine0001/grok-1
- raine0001/OpenExchange

## 3. Security Baseline Recommendation

- Use GitHub App if possible for least-privilege and revocation control.
- If using PAT: fine-grained token scoped only to listed repositories.
- Default permissions:
  - Metadata: Read
  - Contents: Read/Write
  - Pull Requests: Read/Write
  - Issues: Read/Write (optional)
  - Actions: Read
- Enforce PR required on high-impact repos:
  - raine0001/mim
  - raine0001/mim_arm

## 4. Provisioning Steps

1. Fill [config/mim-repo-access.manifest.yaml](config/mim-repo-access.manifest.yaml).
2. Confirm repository visibility and default branch for each repo.
3. Create auth credential (GitHub App installation or fine-grained PAT).
4. Store credential in configured secret source.
5. Configure MIM runtime with secret reference and manifest path.
6. Run access validation:
   - Metadata read for all repos.
   - Clone or ls-remote success for all repos.
   - Write test branch on one non-critical repo.
7. Enable PR workflow and branch protection enforcement.

## 5. Validation Checklist

- Authentication succeeds without interactive prompt.
- All listed repositories can be discovered by API.
- Read operations work on every repo.
- Write operations restricted according to policy.
- PR creation works where write is enabled.
- Protected branches reject direct push when configured.

## 6. Preparation for Next Task (mim_arm Direct Control)

Before enabling direct arm control, define:

- Allowed command envelope (positions, velocities, torque, stop).
- Hard safety limits and geofence/virtual boundaries.
- Human override and emergency-stop mechanism.
- Simulation-first policy and promotion gates to real hardware.
- Required telemetry retention for every control action.

See [docs/mim-arm-control-readiness.md](docs/mim-arm-control-readiness.md) for a preflight checklist.
