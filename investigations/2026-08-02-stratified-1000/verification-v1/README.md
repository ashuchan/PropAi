# Post-fix affected-property verification canary

This is a deterministic, cost-bounded follow-up to the completed stratified
1,000-property run. It does **not** replace or re-label that run. Its only job
is to exercise the exact runtime defect clusters discovered by the offline
1,000-property audit after their code fixes.

`properties.csv` contains every affected property plus controls needed for the
single-property clusters. `verification-ledger.csv` records the observed
baseline evidence and the post-fix acceptance contract for each case. Two
ManageBuilding controls were outside the original 1,000 sample and are included
from the canonical property catalog because they prove property binding.

The canary must use an isolated warm-profile prefix and must not write to the
shared profile store. Hyperbrowser may be used; LLM, CAPTCHA solver, Web
Unlocker, FlareSolverr, and fingerprint rotation remain disabled for this gate.

Generate the manifest reproducibly with:

```bash
python investigations/2026-08-02-stratified-1000/build_verification_manifest.py
```

Before using it on the post-fix run, `audit_verification_canary.py` was replayed
against the immutable pre-fix 1,000-property mirror. It correctly returned
`HOLD_OUTPUT_DEFECTS` and independently detected defects in all six clusters:
avoidable synthetic IDs, Entrata parallel rosters/lineage, negative-status
capture dates, dead-entry failure verdicts with recovered units, missing timeout
snapshots, and the unarchived ManageBuilding response hash. This establishes
that a later pass is not caused by an audit that simply cannot see the original
defects.
