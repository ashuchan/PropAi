# Post-ledger plan→unit state

The archived authoritative TSV has 330 unique properties and SHA-256
`b428382a1ff33375cca65f7088eff566b2a1be02792a8e5cbb33e50eff1afb6f`.
It was last written on 2026-07-31 and is internally reproducible.

The stopped worker's final verified counter was 365/549 after later recovery
tranches and the final Hurston Spherexx admission. Those 35 post-ledger
admissions were implemented/tested in the worker source but were not emitted as
one reconciled tabular ledger before interruption. Therefore:

- 330 is the last fully materialized local ledger.
- 365 is the frozen worker discovery counter.
- 303 is the prior strict GCP canary result.
- None of these is labeled as the forthcoming consolidated full-canary result.

The consolidated full canary will rebuild the authoritative per-property
outcome set from the integrated source and make the 35-row reconstruction
unnecessary for decision-making. The final canary audit should still retain a
delta table against both the 303 GCP baseline and the 365 local discovery set.
