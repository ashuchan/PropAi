# Strict warm-profile candidate v2 — 2026-08-01

## Result

The July GCP archive and current public source responses support a local,
sanitized candidate containing **2,566 of 3,449 profiles (74.40%)**.

| Verdict | Profiles | Meaning |
|---|---:|---|
| `ADMIT` | **2,566** | At least one retained replay route independently identifies the configured property |
| `QUARANTINE` | **51** | No positive route remains and at least one route identifies a different property |
| `REVIEW` | **832** | No mismatch was proved, but no route supplied enough identity to admit safely |

This is a local candidate only. It was not uploaded to the shared profile store,
did not switch a production pointer, and did not launch a canary.

## RP and unit data are not the oracle

Neither RealPage/RP comparison data nor unit-row agreement is admission
evidence. RP may be stale, duplicated, or mapped to the wrong property, and a
clean-looking unit roster can itself belong to a sibling community. Unit IDs,
rents, counts, floor-plan names, and RP overlap are therefore ignored by the
admission algorithm.

A replay route is positive only when its own vendor response or listing scope
matches the configured property through one of these independent signals:

- an exact or distinctive property name;
- a strong street-address match;
- a vendor property/asset identifier whose metadata resolves to that property;
- an exact AppFolio listing-address scope for the configured community.

Name/address conflicts, explicit phase conflicts, and vendor metadata resolving
to another property are mismatches. Missing or ambiguous metadata is
`UNKNOWN`, never an inferred match. References to `realpage` in the ledgers mean
the source vendor route, not an RP export used as ground truth.

## Evidence used

The audit joined the frozen 3,449 candidate profiles to the read-only July GCP
run at `gs://jugnu-canary/runs/2026-07-31-fetchfix-5k/` and to current direct
public responses.

| Evidence pass | Scope | Result |
|---|---:|---|
| GCP property reports | 3,449 | Every candidate has a historical report |
| GCP raw HTML inventory | 2,995 | Presence and hashes inventoried; not blindly treated as route identity |
| GCP API samples | 1,091 | Exact captured route bodies parsed where available |
| Historical winner present | 2,916 | Exact winning route reconciled to current profile routes |
| Self-describing live vendor routes | 1,345 | 1,277 match, 63 mismatch, 5 fetch-failed |
| Unresolved winner direct probe | 2,529 | 1,624 match, 10 mismatch, 533 unknown, 362 fetch-failed |
| AppFolio exact-address recheck | 205 | 125 match, 80 unknown |
| Generic-title false-positive control | 9 | 9 changed from apparent match to unknown |

The AppFolio pass overrides the generic result only when the current portfolio
listing contains the configured address. The production adapter's looser
nearby-building tolerance is intentionally not enough for strict admission.

Raw marketing HTML is also not sufficient to certify an unrelated embedded
API. A correct property page can carry a stale sibling-community widget. The
route that will actually be replayed must itself be bound to the configured
property.

## Route sanitization

The 3,449 profiles contained 7,158 reusable route candidates:

| Route verdict | Routes |
|---|---:|
| `MATCH` | **3,025** |
| `MISMATCH` | **73** |
| `UNRESOLVED` | **4,060** |

The materializer keeps only the 3,025 positive routes. It removes all
mismatched and unresolved winning URLs, availability links, widget endpoints,
known endpoints, field mappings, and patches. It also clears unbound navigation
history, explored links, blocked endpoints, source observations, and wait
patterns.

- **14** admitted profiles were salvaged by removing one or more mismatched
  alternates while retaining a separate positive route.
- **1,988** admitted profiles had unresolved alternates removed.
- Every one of the 2,566 output profiles validates against `ScrapeProfile`.
- Recomputed retained route hashes equal the 3,025 admitted route hashes; no
  non-positive replay route survives.

Raw sanitized profile JSON remains git-ignored because public widget URLs can
contain query credentials. The committed ledger records the source hash,
sanitized hash, admitted route hashes, and every removed route hash.

## Review tail

The largest `REVIEW` groups are RealPage CWS unit routes (116), Entrata
ProspectPortal unit pages (110), obsolete/currently inaccessible G5 routes
(99), SecureCafe routes (93), AppFolio vanity routes (75), ResMan (39), and
OnSite (39). These are not declared bad. They are withheld because their
current unit-producing response does not independently prove property identity.

Browser access may recover fetch-blocked pages, but access alone does not make
their units correct. A browser result can move a profile to `ADMIT` only if the
returned source also supplies an independent identity match. CAPTCHA solving,
Web Unlocker, FlareSolverr, and fingerprint rotation are not part of this audit.

## Artifacts and reproduction

Durable, URL-redacted evidence:

- `gcp-evidence-audit-v1/archive-evidence-ledger.jsonl`
- `live-winner-audit-v1/live-winner-ledger.jsonl`
- `live-winner-audit-v2-appfolio/live-winner-ledger.jsonl`
- `strict-warm-profile-candidate-v2/strict-profile-ledger.jsonl`
- matching `summary.json` files in each directory

Materialize the local candidate after regenerating the evidence ledgers:

```bash
python -m ma_poc.scripts.diagnostics.materialize_strict_warm_profiles \
  --profiles-dir investigations/2026-08-01-consolidated-canary/july-vetted-profile-snapshot-v1/profiles \
  --archive-ledger investigations/2026-08-01-consolidated-canary/gcp-evidence-audit-v1/archive-evidence-ledger.jsonl \
  --live-winner-ledger \
    investigations/2026-08-01-consolidated-canary/live-winner-audit-v1/live-winner-ledger.jsonl \
    investigations/2026-08-01-consolidated-canary/live-winner-audit-v2-appfolio/live-winner-ledger.jsonl \
    investigations/2026-08-01-consolidated-canary/live-winner-audit-v2-krc/live-winner-ledger.jsonl \
  --output-dir investigations/2026-08-01-consolidated-canary/strict-warm-profile-candidate-v2
```

The evidence ledgers retain route/response SHA-256 hashes, safe vendor
locators, status, and extracted identity. They do not retain endpoint URLs,
response bodies, API keys, authorization headers, or RP unit data.
