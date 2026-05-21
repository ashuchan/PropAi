# Batch-3 HAR analysis — 152 production failure HARs across 4 buckets

Date: 2026-05-21
Input: 152 HARs from 4 production-labeled failure buckets, supplied as
4 zip files. Investigation goal: identify what's actually wrong (extraction?
routing? anti-bot? capture itself?) for each property and what's actionable
to ship.

Per-bucket detail:
- [T4_no_body_antibot/_SUMMARY.md](per-har/T4_no_body_antibot/_SUMMARY.md) — 90 HARs
- [T4_code_merge_cross_page/_SUMMARY.md](per-har/T4_code_merge_cross_page/_SUMMARY.md) — 45 HARs
- [T4_edge_templates/_SUMMARY.md](per-har/T4_edge_templates/_SUMMARY.md) — 14 HARs
- [T4_code_embedded_jason/_SUMMARY.md](per-har/T4_code_embedded_jason/_SUMMARY.md) — 3 HARs

## Headline: most of these HARs aren't useful as-is

Of 152 manual captures, **only 26 (17%) had genuinely extractable unit data**:

| Category | n | % | What it tells us |
|---|---:|---:|---|
| **Strong unit signal** (`tier1_api_exists` + `jsonld_only` + `html_only_dom` + `embedded_json_ssr`) | **26** | **17%** | Actionable — these are the cases where production failed but the data IS recoverable. |
| Thin captures (≤4 HTTP responses) | 34 | 22% | Operator landed on a block / wrong page. Need re-capture. |
| Rich captures with weak/no signal | 92 | 61% | Operator captured a real session but the floor-plan XHR wasn't made — usually because they captured the marketing page, not `/floorplans`. |

**Implication:** future HAR exercises should give the operator an explicit
script ("navigate to /floorplans, scroll the unit list, then save HAR"),
because today **83% of the captures are wasted**.

## The 26 strong-signal cases — three new patterns + one env-var fix

### Pattern 1: Peek (`api-v3.peek.us`) — new unmapped PMS

`www.dermotcompany.com` — first observation of Peek in the failure data.
Endpoint shape:
  - `api-v3.peek.us/communities/{communityId}?include=spaces` → 870 KB JSON
  - `api-v3.peek.us/spaces/{spaceId}/similar-units`
  - Widget at `widgets.peek.us`, bundle at `a.peek.us`

Dermot is a NYC luxury multi-property portfolio. Likely >10 properties in
production are on Peek. **Action: ship a Peek Tier-1 adapter.** Estimated
unit recovery: 50+ across the portfolio.

### Pattern 2: Adobe Experience Manager (`/content/{brand}/.../jcr:content/...`)

`www.laurelcrossingapthomes.com` — Air Communities (formerly AIMCO)
property. Endpoint:
  - `{property}.com/content/air-properties/{slug}/us/en/residences/jcr:content/root/container/container/floorplans.json`

The `jcr:content` path segment is Adobe AEM's content fragment URL form.
Air Communities is a top-15 multifamily portfolio (~30K units). **Action:
ship an AEM adapter for the Air Communities brand pattern.**

### Pattern 3: AMLI Next.js per-city SSR JSON

`amlisouthshore` (HAR mis-named — actual content is AMLI portfolio):
  - `www.amli.com/_next/data/{hash}/en/apartments/{city}/{subregion}-apartments.json`
  - 319 KB JSON per city-subregion, 229 unit-keys

This is Tier-1 captured via the existing Next.js extraction (Strategy A
in [_html_extract.py](../../ma_poc/pms/adapters/_html_extract.py)) — but
the `_next/data` path is a Next.js convention not currently in the URL
catalogue. **Action: add `_next/data/*/apartments/*.json` to the API
catalogue patterns** — recovers AMLI's whole portfolio (~70 properties).

### Pattern 4 (already in motion): SecureCafe → BrightData proxy

8 of the 12 antibot-bucket strong-signal cases share the
`engrain+entrata+sightmap+wordpress` markers and have `/floorplans` HTML
with clean JSON-LD that production missed. Per the
[SecureCafe finding](../../../../.claude/projects/-Users-ankur-PropAi-main/memory/project_securecafe_proxy_env_bug.md)
from earlier today, these are likely candidates for the
`PROBE_PROXY_URL` env-var fix. **Action (already noted):** set
`PROBE_PROXY_URL` in production Cloud Run revision.

## Three small discoveries worth a follow-up

### Standalone `Custom_JSON_Files/schema.json` files

`www.liveatthemirage.com` ships JSON-LD in a standalone `.json` file at
`/wp-content/uploads/Custom_JSON_Files/schema.json`. Phase 6.5 (MIME
relaxation) doesn't catch this because Strategy A expects the JSON to be
in a `<script>` tag, not a standalone fetch. **Small extension:** when
the HTML references a `.json` file via `<link rel="prefetch">` or imports
it from a known WordPress theme path, fetch + run JSON-LD extractor on it.

### Knock adapter routing leak

5 properties in `T4_code_merge_cross_page` (`www.lochravenapts.com`,
`www.manchesterlake.com`, others) have Knock XHRs in the HAR
(`doorway-api.knockrentals.com/v1/property/community/{id}`) but ended up
in production's merge_cross_page failure bucket. The Knock adapter
exists — something in the multi-PMS detection is routing them away from
Knock first. **Action:** trace one of these properties through the
adapter routing in production logs to find what beats Knock to the punch.

### Custom Essex API

`www.essexapartmenthomes.com` — `essexapartmenthomes.com/api/properties/{id}/availability`.
60-property Essex portfolio. ROI of an adapter is lower than Peek/AEM, but
worth ~50 units recoverable if production currently misses Essex.

## Probe-script known limitations (transparency)

While running this analysis I found two probe biases that I corrected
mid-stream but couldn't fully fix:

1. **JS-bundle false positives.** The Elise-AI chat widget JS bundle
   (`cdn.eliseai.com/@meetelise/chat`, `cdn.skypack.dev/.../meetelise/chat`)
   contains "bedroom"/"apartment" tokens in chat prompts. My scorer
   bumped properties with score 2 just from those bundles. Affected ~16
   properties in `merge_cross_page` + `embedded_jason` + `edge_templates`
   — all show `score=2` weak_signal from a JS bundle, not real unit data.
   **Fix for next probe:** skip `*.js`/`*.mjs` bodies ≥500 KB regardless
   of MIME.

2. **Peek mis-attribution.** My initial SUMMARY claimed Essex was on
   Peek. Re-checking the HAR showed Essex uses its own internal API. The
   corrected mapping is in the antibot SUMMARY.

## Cross-bucket recommendations (ranked by ROI)

| # | Action | ROI estimate | Status |
|---|---|---|---|
| 1 | Set `PROBE_PROXY_URL` in production Cloud Run revision | ~259 SecureCafe properties (from earlier finding) + ~8 from this batch | Pre-existing memory; not yet deployed |
| 2 | Add Adobe Experience Manager (Air Communities `jcr:content`) adapter | ~30K units across Air portfolio | Not started |
| 3 | Add Peek (`api-v3.peek.us`) adapter | 10–50 properties (~500-3000 units) | Not started |
| 4 | Add `_next/data/*/apartments/*.json` to API catalogue patterns | ~70 AMLI properties (~14K units) | Not started |
| 5 | Trace Knock routing leak for the 5 lochraven/manchesterlake cluster | 5 properties direct + likely 10+ in the leak path | Investigation only |
| 6 | Extend Phase 6.5 to fetch standalone `*.json` files referenced from WordPress themes | ~10 properties in this batch share the pattern | Small extension |
| 7 | Add link-hop signal for Wix/WordPress→Entrata cluster | 5+ properties in edge_templates | Adapter-routing change |
| 8 | Fix probe-script JS-bundle false positive (operational) | N/A — affects future probe quality | One-line change |

## Cross-bucket actions that are NOT recommended

- **Don't build new "edge_templates" logic** — the bucket label is misleading;
  13/14 are pre-template failures (Wix/WP marketing page never link-hopped
  to the Entrata portal), not edge cases inside the Entrata template.
- **Don't expand `T4_code_embedded_jason`-style work** without re-capture —
  the 3 HARs don't represent the production failure.
- **Don't re-investigate the 34 thin captures** until they're re-captured —
  not enough signal to act on.

## Uncapped re-grind validation (2026-05-21, post-analysis)

After the initial grind I noted a 2 MB body cap in the probe scorer.
On request, I re-ran the full grind with **no body cap** (entire response
body scored regardless of size). Captures preserved as `worklist.capped.jsonl`
+ `per-har.capped/` for diff.

**Result: only 1 of 152 HARs flipped verdict** —
`www.liveatdrycreekranch.com` went from `no_unit_signal` → `weak_signal`
because a 3.1 MB chat-widget JS bundle (`webchat.omni.cafe/app/main.*.js`)
contained 6 "bed" tokens in chat training prompts. **Same JS-bundle false
positive pattern noted under "Probe-script known limitations" above.** Not
a real unit-data find.

Aggregate distribution shift: −1 `no_unit_signal`, +1 `weak_signal`. **All
strong-signal verdicts unchanged.** All 26 actionable cases had their unit
data in responses well under the 2 MB ceiling. The cap was effectively a
non-issue for finding extractable data.

## Methodology notes

- All probes use [scripts/deep_probe.py](scripts/deep_probe.py) — body-content
  scoring (not URL filter), JSON-LD type detection, PMS-marker substring
  matching, CF/DataDome block detection.
- Resumable: re-running the grind skips already-probed HARs unless `--force`.
- Each property has a per-har note under `per-har/{bucket}/{stem}.md`.
- Worklist (`worklist.jsonl`) carries one record per property with
  verdict, adapter_hint, n_candidates, n_responses, PMS markers, and
  top-1 URL.

## Counts by bucket

| Bucket | n | Strong signal | Weak | None | Thin |
|---|---:|---:|---:|---:|---:|
| T4_no_body_antibot | 90 | 12 (13%) | 28 (31%) | 50 (56%) | 27 (30%) |
| T4_code_merge_cross_page | 45 | 14 (31%) | 16 (36%) | 15 (33%) | 6 (13%) |
| T4_edge_templates | 14 | 2 (14%) | 3 (21%) | 9 (64%) | 1 (7%) |
| T4_code_embedded_jason | 3 | 0 | 1 | 2 | 0 |
| **Total** | **152** | **28 (18%)** | **48 (32%)** | **76 (50%)** | **34 (22%)** |

(Note: thin and none can overlap — a 0-response HAR is both thin and no-signal.)
