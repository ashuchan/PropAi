# T4_no_body_antibot — 90-HAR bucket summary

Date: 2026-05-21
Input: 90 HARs labeled "no body returned, anti-bot blocked" in production.
Method: body-content scoring (not URL filter); each HAR's largest responses
scanned for unit-data signals + anti-bot block markers.

## Headline

The manual capture itself is the failure mode for **30% of this bucket**.
Of the 90 HARs:

| Capture quality | n | What it means |
|---|---:|---|
| **Thin (≤4 HTTP responses)** | **27** | Operator landed on a blocked page; HAR has nothing to learn from. |
| Rich (≥5 responses) with no signal | 23 | Operator session captured fine, but their browser also saw no unit data. |
| Rich + weak signal (score 2) | 28 | Page content scored low — usually the operator landed on a marketing/landing page, not the floor-plans URL. |
| Rich + extractable | **12** | The manual capture DID get past the production block. These are the high-value cases. |

**Actionable subset:** 12 of 90 (13%). The other 78 either need a re-capture of the actual `/floorplans` page, or the production block isn't the only thing wrong — the marketing page genuinely doesn't carry unit data either.

## The 12 extractable cases — what bypass worked?

Sorted by signal strength:

| Property | Verdict | Top URL | Adapter hint |
|---|---|---|---|
| `www.dermotcompany.com` | tier1_api_exists | `api-v3.peek.us/communities/.../spaces` | **NEW: Peek PMS unmapped** |
| `www.essexapartmenthomes.com` | tier1_api_exists | `essexapartmenthomes.com/api/properties/{id}/availability` | **Custom Essex API** (not Peek — corrected) |
| `www.moderamosaic.com` | jsonld_only | `/floorplans` HTML | jsonld_extractor |
| `www.800sixth.com` | jsonld_only | `/floorplans` HTML | jsonld_extractor |
| `www.centennialplaceapts.com` | jsonld_only | `/floorplans` HTML | jsonld_extractor |
| `www.heatherridgeapts.net` | jsonld_only | `/floorplans` HTML | jsonld_extractor |
| `www.liveamalie.com` | jsonld_only | `/floorplans` HTML | jsonld_extractor |
| `www.renewonridgewood.com` | jsonld_only | `/floorplans` HTML | jsonld_extractor |
| `www.thepointatmonroeplace.com` | jsonld_only | `/floorplans` HTML | jsonld_extractor |
| `www.thepointatwestchester.com` | jsonld_only | `/floorplans` HTML | jsonld_extractor |
| `www.highlandatspringhill.com` | tier1_api_exists | (small score) | generic_api |
| `www.veloontheboulevard.com` | embedded_json_ssr | `app.repli360.com/admin/get_apartmentsync_data_for_floorplan_multi_template` | extract_embedded_blobs (or repli360 adapter) |

## Three discoveries worth shipping

### 1. Peek (`api-v3.peek.us`) is a new unmapped PMS — confirmed 1 property

One confirmed property uses the Peek portal:
  - `www.dermotcompany.com` → `api-v3.peek.us/communities/5e78ba4f.../spaces`

Body: ~870 KB JSON per community, 125+ unit-keys, `spaces` array carries the
unit-level records. Not in the current PMS marker set. Endpoint shapes
observed in the HAR:
  - `api-v3.peek.us/communities/{communityId}` — community + spaces include
  - `api-v3.peek.us/spaces/{spaceId}/similar-units`
  - `listings-api.peek.us/rest/v2/amenities` + `users/{userId}`
  - Widget host: `widgets.peek.us` + JS bundle host `a.peek.us`

Dermot is a multi-property portfolio (NYC luxury); likely 10+ properties
in production are on Peek. Worth adding a Tier 1 adapter.

### 1b. Essex has its own custom API — separate finding

`www.essexapartmenthomes.com` uses `/api/properties/{id}/availability` —
a custom Essex-internal API, NOT Peek. Different pattern entirely. Essex
is a single ~60-property portfolio — adapter ROI lower, but still a clean
deterministic path if production should support it.

### 2. The 8 jsonld_only properties show what was lost to the block

These have clean Schema.org JSON-LD on `/floorplans` but production saw the
anti-bot block instead. They cluster heavily — all 8 share `engrain+entrata+sightmap+wordpress`
markers, suggesting a single WordPress-on-CF-Entrata cluster where:
  - The page itself is CF-protected.
  - Once past the block, the JSON-LD is right there.

These are the **`PROBE_PROXY_URL`-fix beneficiaries** — same root cause as the
[SecureCafe finding](../../../../../../.claude/projects/-Users-ankur-PropAi-main/memory/project_securecafe_proxy_env_bug.md).
If prod ran with the BrightData proxy, these 8 likely flip to SUCCESS.

### 3. Repli360 (`app.repli360.com/admin/get_apartmentsync_data_for_floorplan_multi_template`)

`www.veloontheboulevard.com` ships unit data via Repli360 — another portal worth
checking against the existing adapter set. The PMS marker labeled it `rentcafe`
which is incorrect; Repli360 is a distinct platform.

## Bucket-wide PMS marker distribution (informational)

Heavy on Entrata (56%), WordPress (60%), Sightmap (29%), RealPage-OLL (28%) —
shows this bucket is mostly multi-PMS marketing pages where the floor-plan
section sits behind anti-bot.

## Recommendations

1. **Add Peek (`api-v3.peek.us`) adapter** — 2 confirmed properties, likely 10×
   that in full production.
2. **Set `PROBE_PROXY_URL` in production** (already noted in cross-bucket
   summary + memory).
3. **Re-capture the 27 thin HARs** — they're not actionable as-is. Need the
   operator to actually navigate to `/floorplans` on the property page.
4. **Check Repli360 routing** — `app.repli360.com/admin/get_apartmentsync_data_for_floorplan_multi_template`
   has a discoverable endpoint shape worth instrumenting.

## Thin-capture list (27 — re-capture needed)

`brookmeade-apartments.rentcafewebsite.com`, `citizenanza`, `cortlanddriveside`,
`deerwood`, `fairwayliving`, `fisherbuildings`, `hillcollectiveseattle`,
`laspalomaslasvegas`, `majesticvernonhills.com` (0 responses),
`noorwoodcourtapartment`, `summerhillterraceaptli`, `summerwindaptsfl.com` (0),
`trailsatdominion`, `www.atriaatcrabtreevalley.com`,
`www.centrepointegreensliving.com`, `www.hubrealty.com`,
`www.joplinatcrestview.com`, `www.livegreentreeapts.com`,
`www.solameerslc.com` (0), `www.talltimberapts.com`,
`www.wilmingtonpointe.com` (0), `auradelreybeach`, `liveatthearia`,
`risejulington`, `stationnorthapts`, `villasatgv`, `yardleydechman`.
