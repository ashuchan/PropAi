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
