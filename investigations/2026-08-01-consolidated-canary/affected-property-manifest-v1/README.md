# Deterministic affected-property manifest

This is a local, zero-cost launch input generated from the 2026-08-02 audit.
It contains **386 unique properties** and **392 property/finding rows**.
All 49 findings are represented, including cleared findings through explicit
regression controls.

- `launch_properties.csv` is the exact seven-column future job input.
- `launch_index.csv` explains each launch property's linked findings and roles.
- `affected_properties.jsonl` and `.csv` are the traceable property/finding ledger.
- `finding_coverage.json` proves findings 1-49 are represented and records the
  acceptance contract, evidence line, and local test selectors.
- `future_launch_contract.json` freezes the no-launch state, compliance flags,
  three-call Hyperbrowser ceiling, and one reserved exact-route slot.
- `manifest_summary.json` pins all source hashes and confirms no build, deploy,
  upload, or job launch occurred.
- `SHA256SUMS.json` pins every generated artifact except itself.

Finding 32 deliberately uses the deterministic July On-Site success superset
plus the two named current no-link controls. The live audit measured a moving
49-property attribution set but did not save that exact scan ledger; using the
superset avoids silently dropping a previously affected property.

Rebuild locally:

```bash
python investigations/2026-08-01-consolidated-canary/build_affected_property_manifest.py
```

Verify byte-for-byte determinism:

```bash
python investigations/2026-08-01-consolidated-canary/build_affected_property_manifest.py --check
```

Neither command contacts GCP or any property website.
