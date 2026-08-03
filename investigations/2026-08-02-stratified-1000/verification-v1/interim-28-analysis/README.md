# Interim 28-property verification audit

This directory preserves the audit of the 28 terminal outputs from
`jugnu-verify-7f800ca-thfcv` while PID `52697` (Clearview) is verified in an
isolated one-property retry. The raw run mirror is intentionally git-ignored;
the compact, reproducible audit outputs in this directory are tracked.

## Result

- Output coverage: 28 of 29 expected properties; PID `52697` is the sole
  missing property.
- Verdicts: 22 `SUCCESS`, 5 `SUCCESS_PLAN_LEVEL`, and 1 `FAILED_NO_DATA`.
- Physical output: 1,188 unit rows, including 1,188 retained rent ranges and
  255 rows with a surfaced building ID.
- Data-quality gates: zero synthetic IDs and zero unresolved area rows.
- Diagnostics: 28 of 28 terminal properties have an extraction snapshot.
- Exercised fix clusters passed with no observed defect: natural-ID priority,
  Entrata parallel-roster reconciliation, negative-status date suppression,
  dead-entry salvage, and ManageBuilding response archival.

The report says `HOLD_OUTPUT_DEFECTS` only because the complete verification
manifest contains 29 properties and PID `52697` emitted no terminal output.
That missing-output defect is the subject of the isolated supervisor-timeout
retry; it is not evidence of a defect in any of the 28 completed outputs.

## Files

- `REPORT.md`: human-readable outcome and cluster gates.
- `summary.json`: machine-readable counts, launch lineage, and conclusion.
- `property-observations.csv`: one row per expected property.
- `verification-cases.csv`: all 31 affected/control cases and their runtime
  exercise status.
- `cluster-summary.csv`: cluster-level exercised/pass state.
- `issues.csv`: the sole issue, `EXPECTED_PROPERTY_MISSING` for PID `52697`.

The final 29-property report is generated separately after the isolated retry
and does not overwrite this interim evidence.
