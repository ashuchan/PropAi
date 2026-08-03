# Local regression and focused-canary record

Date: 2026-08-02 (America/Chicago)
Branch: `codex/consolidated-canary-2026-08-01`
Branch baseline: `fa1afb7` (contains current `origin/main` `02369d2` plus two
Codex consolidation commits)
Canary status: not launched yet

## Local gates

| Gate | Result | Disposition |
|---|---:|---|
| Yotta + non-registry/date/provenance closure set | 321 passed | Green |
| All adapter tests | 3,944 passed, 4 skipped | Green |
| Shared PMS/core/reporting/runner tests (adapter directory excluded) | 3,075 passed | Green |
| Focused follow-up after the shared gate exposed Apollo outer-card rent loss | 40 passed | Green |
| Changed Python files, Ruff | all checks passed | Green |
| Full `ma_poc/tests` suite | 9,891 passed, 47 skipped, 3 failed | Three failures are pre-existing structure-policy failures, not audit-code failures; see below |

The broad affected regression is therefore 7,019 passing tests with four
skips. The final repository-wide run independently covers those paths and all
remaining repository tests.

## Shared-layer defect caught during closure

The broad shared run found one evidence-backed RS365/Apollo compatibility
regression. A public `div.unit-details` roster card can carry
`data-rent-min`/`data-rent-max` on the outer unit node. The remediated parser
searched only descendant lease-option spans, so it retained native unit `104`,
1 bed, 1 bath, and 465 sqft but dropped the literal $1,450 rent. The parser now
checks the bounded outer unit node and then its descendants. The exact
diagnostic contract and the complete RS365 adapter module pass together.

The concession source-contract failure seen in the same first run was not a
behavioral defect: the required forward sentence-extension loop was present,
but an autoformatter inserted spaces into the slice that a source-text test
pins. The slice was returned to the repository's pinned spelling; behavior is
unchanged.

## Full-suite residuals inherited from `origin/main`

The three full-suite failures are:

1. `test_no_unexpected_files_in_scripts_root`
2. `test_scripts_files_have_main_guard`
3. `test_move_table_covers_scripts_root`

They concern eight already-tracked scripts at `ma_poc/scripts/` root and the
already-tracked `scripts/diagnostics/local_canary_attribution.py` main guard.
`git diff --exit-code origin/main -- <all implicated scripts and the structure
test>` is clean, proving that neither the files nor the policy test changed in
this audit worktree. They are recorded as repository-layout debt and are not
silently counted as an audit regression.

## Fresh live source-to-final checks

### Yotta

Direct property-bound API calls, with no proxy, unlocker, CAPTCHA solver, or
browser evasion, followed by the production formatter returned:

| DBA | Units/native IDs | Provider plan IDs | Floors | `available_now` | `explicit_future` | Response provenance |
|---:|---:|---:|---:|---:|---:|---|
| 200 | 27 / 27 | 7 | 27 | 7 | 20 | 1 MATCH hash / 27 rows |
| 201 | 18 / 18 | 5 | 18 | 9 | 9 | 1 MATCH hash / 18 rows |
| 55 | 13 / 13 | 5 | 13 | 1 | 12 | 1 MATCH hash / 13 rows |

### ShowMojo / Park Northside

Direct configured-page-to-official-manager-to-ShowMojo replay returned 13/13
unique native listings. Twelve literal `Available now` tokens normalized to
the 2026-08-02 capture date with `available_now`; UID `e7c39f1061` preserved
raw `Available September 7th` and emitted `2026-09-07` with
`explicit_future`. Two hashed MATCH roster responses account for all 13
admitted rows.

## Next release gate

Build the audit code without the four user-owned dirty files, run the strict
focused GCP sample defined by the fix plan, reconcile every property against
the corresponding acceptance contract, and append the immutable run URI plus
property-by-property verdicts here. No profile-store or production write is
part of that gate.
