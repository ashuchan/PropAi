# PID 52697 shard-supervisor verification

Attempt 1 (`ed94525`) proved that the wedged child event loop never reached
its in-process cancellation cleanup. Attempt 2 deploys commit `823ea7c`, which
adds the hard boundary to the existing shard supervisor subprocess.

The child keeps the 120-second property deadline. The supervisor kills it at
150 seconds if it has not returned, materializes a schema-valid
`FAILED_UNREACHABLE` row with both timeout reasons and a content-addressed
zero-row extraction snapshot, then runs the existing sync/upload `finally`.
The Cloud Run task ceiling remains 900 seconds. All profiles and outputs use
new isolated prefixes; the shared profile store is not mutated.

## Result

**PASS.** Execution `jugnu-verify-823ea7c-rf4br` completed 1/1 tasks in
3m32.8s. Clearview reproduced the same second-Hyperbrowser-session wedge. At
the 150-second child-process boundary the supervisor:

- killed the wedged runner at `2026-08-03T01:43:10.929537Z`;
- materialized exactly one terminal record at
  `2026-08-03T01:43:11.066252Z`;
- emitted `FAILED_UNREACHABLE` with
  `per_property_timeout:120s (shard_supervisor_fallback)` and
  `shard_runner_timeout:150s`;
- wrote a content-addressed zero-row extraction snapshot and source manifest;
- uploaded nine run objects; and
- exited the Cloud Run task successfully because the per-property failure was
  now present in the run output rather than silently lost.

The retry prefix was downloaded once into the ignored `canary-output/` mirror.
The combined audit of the original 28 outputs plus this supplement has exact
29/29 property coverage, no duplicates, no critical/high issues, and 29/29
snapshots. See `../../post-run-verification/REPORT.md` and
`execution-evidence.json`.
