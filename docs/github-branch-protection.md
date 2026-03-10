# GitHub Branch Protection (TOD)

## Target

- Repository: `raine0001/mim`
- Branch: `main`
- Required status check: `TOD Tests`

## Enforced Rules

- Require status checks before merging
- Require branches to be up to date before merging (`strict=true`)
- Require at least 1 pull request review
- Restrict direct pushes to `main` (via PR requirement)
- Require conversation resolution before merge
- Disable force pushes and deletions

## Apply from Development Machine

1. Install and authenticate GitHub CLI:

```bash
gh auth login
```

2. Apply protection:

```bash
bash ./scripts/apply_branch_protection.sh raine0001/mim main
```

3. Verify:

```bash
gh api repos/raine0001/mim/branches/main/protection
```
