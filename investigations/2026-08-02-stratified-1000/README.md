# Stratified 1,000-property GCP canary

This directory holds the deterministic release gate requested on 2026-08-02.
The catalog contains address and URL fields but no multifamily asset-type
column, so **property type** is defined as the prior strict output class:
unit-level, plan-level, failed-no-data, unreachable, dead URL, or
no-data-published.

The sample combines:

1. all 386 properties representing the 49 evidence-backed adapter findings;
2. explicit route candidates for the five registered adapters that had no
   attributed unit output in the August 1 benchmark;
3. at least one property from every adapter × property-type cell observed in
   that 4,982-property benchmark;
4. at least three properties per observed adapter where the population allows;
5. every state/territory bucket in the catalog; and
6. a proportional supplement that makes the final property-type counts match
   the 4,982-property fleet by largest-remainder allocation.

`source-benchmark/` and `canary-output/` are intentionally ignored: they are
downloaded immutable GCS evidence, not source.  `manifest-v1/` is committed and
contains the exact launch CSV, selection ledger, coverage tables, summary, and
checksums.

The build script is local-only.  Upload/build/launch details and the downloaded
post-run audit will be appended after the canary finishes.
