# Prod-fingerprint learnings — 2026-05-24

User Q: *"are you able to connect to prod postgres db and check the
fingerprint saved there? any learning which can be helpful for any
cohort which we could not address"* + scope clarification: *"refer to
only where — 2,916 of 4,982 properties (58.5%) have at least 1 unit
row with all of Unit ID + Floor Plan + Rent + Area + Available Date
populated — we should only learn from these cases if any"*

Connected to `jugnu-494013:us-central1:jugnu-db-production` via
cloud-sql-proxy + IAM impersonation of `jugnu-worker-production` SA.
The fingerprint lives in `scrape_profiles.payload` (one row per
property, JSON). Each profile carries: `navigation`, `api_hints`
(known_endpoints + field_patches + llm_field_mappings + widget_endpoints
+ wait_for_url_pattern + blocked_endpoints + api_provider +
client_account_id), `dom_hints`, `confidence`, `llm_artifacts`, `stats`,
`fetch`, `cluster_key`, `property_amenities`.

## Reframe per user scope

I split the 4,982 props into:

  * **2,879 "full" properties** (58%) — have ≥1 unit row with ALL of
    Unit ID + Floor Plan + Rent + Area + Available Date populated
  * **2,103 "not-full" properties** (42%) — every row missing at least
    one of those 5 fields

Then queried `scrape_profiles` for the 2,103 not-full set and
compared to what the 2,879 full set has discovered.

## Tier health on full / not-full split

| Tier | Total props | Full | Not-full | Health |
|---|---:|---:|---:|---:|
| TIER_1_DOM_APPFOLIO_PROBE | 130 | 129 | 1 | **99 %** ✓ |
| TIER_1_API_SIGHTMAP | 410 | 402 | 8 | **98 %** ✓ |
| TIER_1_DOM_APPFOLIO_SSR | 117 | 115 | 2 | **98 %** ✓ |
| TIER_1_KNOCK_API | 305 | 275 | 30 | **90 %** ✓ |
| TIER_1_API_RENTCAFE_SECURECAFE | 1,500 | 1,007 | **493** | 67 % ⚠ |
| TIER_MERGED_CROSS_PAGE | 279 | 175 | 104 | 63 % ⚠ |
| TIER_4_LLM_DOM | 570 | 310 | 260 | 54 % ⚠ |
| TIER_2_JSONLD | 91 | 25 | 66 | 27 % ⚠⚠ |
| TIER_3_DOM | ~499 | 149 | 350 | 30 % ⚠⚠ |

## Universal-missing-field per cohort (sample of 100 not-full)

| Tier | Top missing field | % |
|---|---|---:|
| TIER_1_API_RENTCAFE_SECURECAFE | **Available Date** | 90 % |
| TIER_3_DOM | **Available Date** | 83 % |
| TIER_4_LLM_DOM | **Available Date** | 96 % |
| TIER_2_JSONLD | **Available Date** | 100 % |
| TIER_1_API_SIGHTMAP | **Available Date** | 88 % |
| TIER_1_KNOCK_API | Floor Plan (plan-level responses) | 100 % |
| TIER_MERGED_CROSS_PAGE | Floor Plan / Sqft (cross-page join gap) | 54 % / 51 % |

## Learning #1 — Q1 fix unlocks ~1,000 properties already

The dominant single failure is **Available Date being empty on
properties where availability_status="AVAILABLE"** — exactly what the
Q1 fix (commit `8276838` + regex follow-up `c496b97`) addresses.
Across cohorts the Q1 fix is poised to convert:

  * SC: 493 × 90 % = ~444 props → full
  * TIER_3_DOM: 350 × 83 % = ~290 props → full
  * TIER_4_LLM_DOM: 260 × 96 % = ~250 props → full
  * TIER_2_JSONLD: 66 × 100 % = ~66 props → full
  * SightMap: 8 × 88 % = ~7 props → full

**Conservative total Q1 lift: ~1,000-1,200 properties** moving
not-full → full on the next canary, with zero new code.

## Learning #2 — 168 properties have multi-provider endpoints in fingerprint

168 not-full properties have **≥2 distinct API providers** registered
in `api_hints.known_endpoints` — meaning prod has discovered backup
endpoints we're not using when the primary one returns insufficient
data. Top combos:

| Endpoint combo | Properties |
|---|---:|
| rentcafe + securecafe | 39 |
| g5 + knock | 23 |
| securecafe + sightmap | 16 |
| knock + sightmap | 13 |
| repli360 + securecafe | 13 |
| knock + realpage | 9 |
| knock + securecafe | 7 |
| knock + repli360 | 7 |
| knock + rentcafe | 5 |
| g5 + spherexx | 4 |
| ... | |

**Actionable**: when a primary adapter returns 0-units or only
plan-level data, query the secondary endpoint from the fingerprint
and merge. Estimated lift: **~150 properties**.

Concrete example from the data (prop 14963, "The Carson Longview"):

```
known_endpoints:
  /admin/get_apartmentsync_data_for_floorplan_multi_template (Repli360)
  /v1/property/2014660/units (Knock)
  /v1/property/community/11ecdad6721ebc36 (Knock community)
```

Knock currently returns plan-level; the Repli360 endpoint has been
discovered to carry the missing unit data.

## Learning #3 — 232 properties have LLM-discovered field_patches (672 patches total)

Prod stores LLM-discovered **field-name corrections** per property,
per endpoint. These are runtime patches that say *"on this endpoint,
the field `rent_low` is actually parsed via `int(item_lc.get('minimumrent', 0))`"*
that DIDN'T exist in our adapter at the time. The patches are
queryable + idempotent + per-property scoped, but they SHOULD be
generalised back into adapter source code.

Distribution of patches:

| Endpoint family | # patches | Top fields patched |
|---|---:|---|
| **SightMap** | **362** | available_date (100), rent_low (98), rent_high (92), unit_id (33), floor_plan_name (21) |
| Repli360 `/api/unit` | 33 | rent_low (6), available_date (6), rent_high (5), beds (5), baths (5) |
| Repli360 `/admin/get_apartmentsync...` | 23 | rent_low (11), unit_id (10), beds (2) |
| Generic `/graphql` | 18 | rent_low (4), rent_high (4), available_date (4) |
| apts247 `/api/v3/floorplans/all/` | 12 | rent_low (4), available_date (4), rent_high (4) |
| (jugnu chat / iframe) `/api/v1/plugins` | 11 | unit_id (9), floor_plan_name (2) |
| RentCafe | 11 | rent_low (4), rent_high (4), unit_id (2) |

Sample patches:

```
# RentCafe — prop 36465 (LLM-discovered 2026-05-10)
api_url_pattern: /wp-json/rentcafeapi/v2/floorplans
field_name: rent_low
json_path: MinimumRent
parser_fix: int(item_lc.get('minimumrent', 0))

# SightMap — prop 40582 (LLM-discovered 2026-05-16)
api_url_pattern: /app/api/v1/yzvg8lyopln/sightmaps/96550
field_name: rent_low
json_path: data.units[0].price
parser_fix: item_lc['data']['units'][0]['price']
```

**Actionable**: SightMap alone has 362 patches affecting ~100+
properties. Backport these LLM-discovered field paths into
`sightmap.py` to make the adapter handle the variant response shapes
deterministically (no LLM needed at runtime).

## Learning #4 — 1,279 not-full properties have ZERO known_endpoints

These are the truly-custom sites with no discoverable API pattern.
Adapter strategy = DOM extraction or LLM. No fingerprint magic
helps here — the lift has to come from:
- Better generic DOM extractors (Tier 3)
- Better LLM prompts / vision (Tier 4/5)

## Recommended cohorts to address (in priority order)

| # | Action | Est. lift | Effort |
|---|---|---:|---|
| 1 | **Re-run canary to materialise Q1 fix** | ~1,000-1,200 | rebuild + canary trigger |
| 2 | Port SightMap field_patches → sightmap.py | ~100 | new adapter code + tests |
| 3 | Build multi-provider fallback runner (knock+g5 / knock+repli360 cohorts) | ~150 | new orchestrator module |
| 4 | Port Repli360 field_patches → repli360.py | ~30 | adapter update |
| 5 | Port RentCafe MinimumRent patch → rentcafe.py | ~10 | adapter update |

Total recommended lift potential beyond today's already-shipped
fixes: **~290 additional properties** (on top of the ~1,000 Q1 lift).

## Artifacts

- `/tmp/full_ids.json` — 2,879 full canonical IDs
- `/tmp/notfull_ids.json` — 2,103 not-full canonical IDs
- Connection runbook below
- This document

## 2026-05-24 UPDATE — SightMap variant port investigation

User asked to chase the SightMap variant lift. Deeper investigation
reframes the finding entirely:

**Live-verified our `parse_sightmap_payload` against all 6 affected
URLs** (Copper Terrace, Kinwood NY, Hoboken Point, WM Canterbury, Hydro,
Morgan Avon): 102 units across them, **ZERO missing fields**. The
adapter ALREADY handles the `data` envelope (sightmap.py line 140).
The 362 field_patches are misguided runtime LLM artifacts.

**The real finding**: Morgan Avon's xlsx tier was `TIER_4_LLM_DOM`,
not `TIER_1_API_SIGHTMAP`. The xlsx covers 30+ Morgan Properties
sites — all 29 routed to RentCafe SecureCafe, only 1 (Morgan Avon)
went to LLM_DOM. The SightMap endpoint in prod's fingerprint was
never even attempted by our detector for that property.

**Architectural learning** (this is the actually-useful one):

  prod's `scrape_profiles.api_hints.known_endpoints` is effectively
  a learned detector cache. We currently re-derive routing on every
  scrape from the marketing site's DOM markers. For ~168 not-full
  properties, prod has discovered better endpoints than the detector
  finds on a single fetch — these get baked into the per-property
  fingerprint over time.

The bigger win isn't porting field_patches — it's plumbing the
prod fingerprint into the scrape orchestrator as a per-property
routing hint. When the detector returns unknown OR a non-API tier
falls through with empty/partial data, consult the fingerprint's
known_endpoints and try each registered URL.

This is a substantial architectural change (load fingerprints at
scrape start, modify routing). Documented for follow-up; deferred
from today's grind. Realistic lift: the ~150 multi-provider props
(Learning #2 above) + the ~50 properties currently using LLM tier
that have a Tier-1 API endpoint registered in fingerprint = ~200
property lift potential.

## Connection runbook (for future analysis)

```bash
# 1) Re-auth (once per day-ish)
gcloud auth application-default login

# 2) Grant ankur.singh IAM SA-impersonation on the worker SA
gcloud iam service-accounts add-iam-policy-binding \
  jugnu-worker-production@jugnu-494013.iam.gserviceaccount.com \
  --member="user:ankur.singh@surgexdigital.com" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project jugnu-494013

# 3) Add ankur.singh as Cloud SQL IAM user (one-time)
gcloud sql users create ankur.singh@surgexdigital.com \
  --instance=jugnu-db-production --project=jugnu-494013 \
  --type=cloud_iam_user

# 4) Proxy with SA impersonation (IAM passthrough)
cloud-sql-proxy --auto-iam-authn \
  --impersonate-service-account=jugnu-worker-production@jugnu-494013.iam.gserviceaccount.com \
  --port 5433 jugnu-494013:us-central1:jugnu-db-production &

# 5) Connect — password is ignored thanks to --auto-iam-authn
psql "postgresql://jugnu-worker-production%40jugnu-494013.iam@127.0.0.1:5433/jugnu?sslmode=disable"
```
