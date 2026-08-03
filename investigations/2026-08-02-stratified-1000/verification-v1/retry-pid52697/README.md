# PID 52697 timeout-finalizer verification

The first post-fix verification execution produced 28 of 29 terminal property
artifacts. PID 52697 (Clearview) entered the property-bound Entrata
Hyperbrowser route at `2026-08-03T00:52:08Z`, crossed the 600-second
per-property deadline, and remained live beyond the two bounded local teardown
allowances. The exact execution was cancelled at
`2026-08-03T01:04:17.454521Z` to avoid its four-hour Cloud Run task ceiling;
Cloud Run recorded 28 succeeded tasks and one cancelled task.

Commit `ed94525` tested the first cancellation hypothesis: when the outer
property task is already cancelling, Hyperbrowser cleanup skips local
CDP/Playwright teardown and directly stops the paid remote session. Its local
cancellation-resistant regression test passed, but the isolated GCP retry
**disproved the production fix**. Clearview again stopped emitting logs after
the second Hyperbrowser session opened; the wedged child event loop never
reached the in-process cancellation handler. The retry was cancelled at
`2026-08-03T01:19:59.968748Z` and completed as cancelled at
`2026-08-03T01:20:23.388533Z`, with no terminal property artifact.

This directory preserves that deterministic, disproven one-property retry. It
inherits the
same runtime policy as the 29-property verification, uses a new isolated warm
profile prefix, and did not mutate the shared profile store. The retry used a
120-second property deadline and 900-second task ceiling. Because no terminal
artifact was produced, this attempt is not merged into the final verification
audit. `attempt-2-823ea7c/` contains the process-supervisor retry that enforces
the timeout outside the potentially wedged child event loop.
