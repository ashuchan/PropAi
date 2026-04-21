# Branch Protection Rules

These rules are applied in the GitHub UI at **Settings → Branches → Branch protection rules** and cannot be stored as code.

## Rule: `main`

| Setting | Value |
|---------|-------|
| Require a pull request before merging | ✅ Enabled |
| Require approvals | 1 (increase to 2 when team grows beyond 3) |
| Dismiss stale PR approvals when new commits are pushed | ✅ Enabled |
| Require review from Code Owners | ✅ Enabled (after CODEOWNERS is in place) |
| Require status checks to pass before merging | ✅ Enabled |
| **Required check** | `gate-summary` (single aggregate check — see ci.yml §4) |
| Require branches to be up to date before merging | ✅ Enabled |
| Require conversation resolution before merging | ✅ Enabled |
| Do not allow bypassing the above settings | ✅ Enabled |
| Allow force pushes | ❌ Disabled |
| Allow deletions | ❌ Disabled |

## Applying these rules (step-by-step)

1. Go to your repo → **Settings** → **Branches**
2. Click **Add rule**
3. Branch name pattern: `main`
4. Check "Require a pull request before merging" → set approvals to 1
5. Check "Require status checks to pass" → search for `gate-summary`
6. Check "Require branches to be up to date before merging"
7. Check "Do not allow bypassing the above settings"
8. Click **Create**

## Verifying branch protection is enforced

Create a PR with a failing lint check. The "Merge" button must be disabled. This is the most important verification — branch protection configured but not tested is no protection at all.

## CODEOWNERS

`.github/CODEOWNERS` is already in the repo. To activate code owner reviews:
1. Ensure "Require review from Code Owners" is checked in branch protection
2. Update handles in `.github/CODEOWNERS` to real GitHub usernames
