# Stratified 1,000-property canary audit

Capture date: `2026-08-02`
Offline run mirror: `/Users/ankur/PropAi-codex-recovery-replay/investigations/2026-08-02-stratified-1000/canary-output`

## Release conclusion

**HOLD: 275 critical/high output-contract issue(s) require review.**

The conclusion is based on the deterministic output manifest, local immutable source archives, and the finding-mapped regression suite. A live route that did not win in this run is explicitly reported as not runtime-exercised; fixture proof is not mislabeled as live proof.

## Run outcome

| Measure | Result |
| --- | --- |
| Expected / output properties | 1000 / 1000 |
| Unit-level success | 868 / 1000 (86.80%) |
| Plan-level success | 53 |
| Failed no data | 43 |
| Unit rows | 21468 |
| Real / synthetic identities | 21379 / 89 |
| Avoidable synthetic identities | 89 |
| Unresolved area rows | 184 |
| Retained area / rent ranges | 12 / 4386 |
| Same-raw-source prior future dates preserved | 3193 / 3193 |
| Properties with extraction snapshots | 993 / 1000 |
| Archived source responses | 1374 |

## Quality issues

| Severity | Count |
| --- | --- |
| critical | 140 |
| high | 135 |
| medium | 0 |
| low | 0 |
| info | 0 |

| Issue code | Count |
| --- | --- |
| ENTRATA_PARALLEL_ROSTER_DUPLICATE | 103 |
| SYNTHETIC_OUTPUT_WITH_PREFORMAT_NATURAL_ID | 89 |
| SOURCE_HASH_NOT_ARCHIVED | 17 |
| CANONICAL_UNIQUENESS_GATE_FAILED | 15 |
| AVAILABLE_NOW_PROVENANCE_LOST | 8 |
| DUPLICATE_CANONICAL_UNIT_ID | 8 |
| NEGATIVE_STATUS_CAPTURE_DATE | 8 |
| FINAL_COUNT_MISMATCH | 7 |
| OFFLINE_ARCHIVE_INVALID | 7 |
| PROVENANCE_FIELDS_MISSING | 7 |
| FAILURE_VERDICT_WITH_UNITS | 6 |

## Adapter-fix validation

Finding-mapped regression suite: **984 passed, 2 skipped across 50 finding-mapped modules**.

Declared skips: test_g5.py has two explicitly skipped Apollo-cache fallback tests; the merged G5 adapter exits on NO_URN before that fallback.

| Runtime status | Findings |
| --- | --- |
| FAIL_OUTPUT_CONTRACT | 1 |
| NOT_TARGET_ROUTE_EXERCISED | 9 |
| PASS_RUNTIME_EXERCISED | 39 |

See `finding-validation.csv` for every finding's acceptance contract, fixture selectors, sampled properties, observed winners, and runtime status. See `adapter-result-matrix.csv` for every prior adapter stratum, `adapter-route-coverage-matrix.csv` for every explicit registered/finding route (including prior N0 adapters), and `property-ledger.csv` for every property.

## Reproduction

```bash
python investigations/2026-08-02-stratified-1000/audit_stratified_canary.py
```

The audit performs no network calls and reads only the one-time local mirror.
