# Stratified 1,000-property GCP canary

For the complete cross-stream implementation and evidence handoff—including
the 49 adapter findings, recovery logic, availability dates, new unit columns,
daily-history key, raw-source capture, warm-profile stores, and post-fix
verification—read [`CONSOLIDATED_RELEASE_README.md`](CONSOLIDATED_RELEASE_README.md).

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

The exact build, image digest, input generation, isolated warm-profile seed,
Cloud Run job, and execution are recorded in `launch-manifest.json`. The job
uses 100 shards at parallelism 50, Hyperbrowser with proxy enabled, and no LLM,
Web Unlocker, or FlareSolverr tier.

After completion the GCS run prefix is mirrored **once** into ignored
`canary-output/`. `audit_stratified_canary.py` then operates entirely offline
and writes its committed evidence to `post-run-audit/`:

- a 1,000-row property ledger;
- adapter/result and property-type transition matrices;
- a severity-ranked issue ledger;
- a finding-by-finding validation matrix for all 49 fixes; and
- a concise Markdown conclusion plus machine-readable JSON summary.

The 50 finding-mapped regression modules are also run as a separate gate. This
keeps fixture-backed semantic proof distinct from a route actually exercised
by the live sample.

## Final post-fix verification

The post-canary output fixes were checked with a deterministic 29-property
affected cohort. The first execution returned 28 properties; the sole missing
property reproduced an event-loop wedge. A process-supervisor fallback was
then live-verified on that property in an isolated retry. The combined audit
has exact 29/29 coverage, 29/29 extraction snapshots, zero critical/high
issues, zero avoidable synthetic IDs, and zero unresolved-area rows.

See `verification-v1/post-run-verification/REPORT.md`. Its `PARTIAL` conclusion
means some originally affected routes did not fail again and therefore were
not live-exercised; it does not mean a detected output defect remains.
