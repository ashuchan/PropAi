# Post-fix affected-property canary

**HOLD: critical/high verification defects were detected.**

This is the 29-property follow-up gate for defects discovered by the completed stratified 1,000-property run; it is not a replacement fleet benchmark.

## Outcome

| Measure | Result |
| --- | --- |
| Expected / output | 29 / 28 |
| Verdicts | {"FAILED_NO_DATA": 1, "SUCCESS": 22, "SUCCESS_PLAN_LEVEL": 5} |
| Unit rows | 1188 |
| Synthetic IDs | 0 |
| Unresolved area rows | 0 |
| Snapshots | 28 / 28 |
| Critical / high issues | 1 / 0 |

## Fix-cluster gates

| Cluster | Affected exercised | All cases exercised | Issues | Status |
| --- | --- | --- | --- | --- |
| dead_entry_salvage | 5 / 6 | 5 / 6 | 0 | PARTIAL_RUNTIME_EXERCISED |
| entrata_parallel_roster | 8 / 8 | 8 / 8 | 0 | PASS_RUNTIME_EXERCISED |
| identity_natural_id | 2 / 2 | 4 / 4 | 0 | PASS_RUNTIME_EXERCISED |
| managebuilding_archive | 1 / 1 | 1 / 3 | 0 | PASS_RUNTIME_EXERCISED |
| negative_status_date | 1 / 1 | 3 / 3 | 0 | PASS_RUNTIME_EXERCISED |
| timeout_diagnostics | 0 / 7 | 0 / 7 | 1 | FAIL_OUTPUT_CONTRACT |

`verification-cases.csv` preserves pass/fail/not-exercised status per property. `issues.csv` contains only observed output evidence; fixture proof is not labeled as live exercise.
