# T4_code_merge_cross_page — 45-HAR bucket summary

Date: 2026-05-21
Input: 45 HARs from properties where production adapter ran but the cross-page
merge step failed (units got dropped between pages).

## Headline

This bucket is heterogeneous — it's not one failure pattern, it's "adapter
detection found a multi-page site but the merge logic dropped units."
Verdict distribution:

| Verdict | n | % |
|---|---:|---:|
| jsonld_only | 8 | 17.8% |
| tier1_api_exists | 5 | 11.1% |
| html_only_dom | 1 | 2.2% |
| weak_signal | 16 | 35.6% |
| no_unit_signal | 15 | 33.3% |

Of the 14 strong-signal HARs, the data IS in the capture — the production
issue is on the merge axis, not the extraction axis.

## Discoveries — new portal patterns observed

### AMLI uses Next.js per-city SSR JSON

`amlisouthshore` (HAR file is misnamed — actually `www.amli.com` content):
  - **Top URL:** `www.amli.com/_next/data/{hash}/en/apartments/austin/downtown-austin-apartments.json?region=austin&subregion=downtown-austin`
  - **Shape:** 319 KB JSON, 229 unit-keys per city-subregion file
  - **Pattern:** one JSON file per `{city}/{subregion}-apartments.json` route

`www.amli.com` (different HAR — Dallas region):
  - JSON-LD heavy (30 nodes on a 410 KB HTML)
  - Pattern: Next.js HTML pre-rendered with JSON-LD `Apartment` entries

The merge issue for AMLI is likely: each `/apartments/{city}/{property}` URL
produces its own JSON-LD set; the adapter visits ≥2 properties but the merge
step's de-dup doesn't recognize them as distinct properties.

### Adobe Experience Manager (`/content/.../jcr:content/...`)

`www.laurelcrossingapthomes.com` ships unit data via:
  - `www.laurelcrossingapthomes.com/content/air-properties/laurel-crossing/us/en/residences/jcr:content/root/container/container/floorplans.json`

This is **Adobe Experience Manager** — JSON files served at `jcr:content` paths.
Not in current PMS markers. The "air-properties" segment is the Air
Communities brand (formerly AIMCO) — a large portfolio. Worth adding as
a Tier 1 adapter pattern.

### `/wp-content/uploads/Custom_JSON_Files/schema.json`

`www.liveatthemirage.com` hosts a static `Custom_JSON_Files/schema.json`
file with 14 JSON-LD nodes inline. Several other properties in the bucket
follow this pattern — WordPress sites that bake their floor-plan schema
into a static `.json` file. This file is `application/json` MIME and 7.8 KB.

**Phase 6.5 (MIME relaxation) does NOT catch this** — Strategy A expects the
file to be a `<script>` block in HTML, not a standalone file. Worth a small
extension: when the adapter discovers a JSON-LD-shaped static `.json` file
referenced from a `<link rel="prefetch">` or imported by a known WordPress
theme, fetch + run JSON-LD extractor on it.

### Knock adapter is the right route for some — verify it actually fires

5 properties in this bucket emit `doorway-api.knockrentals.com/v1/property/community/{id}`
on the page load:
  - `www.lochravenapts.com`, `www.manchesterlake.com`, others

The Knock adapter already exists ([_knock_units.py]?). These properties
ended up in merge_cross_page rather than as Knock-routed because something
in the routing made them not match Knock. **Investigate:** are they detected
as something else first, then fall through to a multi-PMS merge that drops
the Knock response?

## The "weak_signal" subset (16 properties) — the floor-plan page wasn't captured

These have score 2 (just 2 sqft tokens, no rent + bed co-occurrence) and
mostly land on a landing page or about page. Many of them share a markers
cluster `entrata+knock+realpage_oll+wordpress` — Harbor Group portfolio
characteristic. From the [project_grind600 memory](../../../../../../.claude/projects/-Users-ankur-PropAi-main/memory/project_grind600_findings_2026-05-21.md):
"Harbor Group portfolio is plan-only dead-end."

These 16 are not the merge bug — they're the manual capture not getting
deep enough.

`chandlersbay`, `hampdenheights`, `signalpointe`, `www.harborgroupmanagement.com`
all score 88 — these have meaningful content but the probe rates them
weak because the rent+bed+sqft tokens don't co-occur cleanly in the same
DOM subtree. May actually be extractable by Phase 6.6 (container-discovery).

## The 15 no_unit_signal subset

These have markers but the HAR captured no unit data — operator landed on a
floorplan link → got redirected/blocked → captured nothing useful.
6 of them have ≤4 responses (thin captures); the other 9 had normal session
volume but the unit-data response was never made.

Worth re-probing these via Chrome MCP or fresh HAR capture on the actual
`/floorplans` URL.

## Recommendations

1. **Add Adobe Experience Manager adapter** — `/content/{brand}/.../jcr:content/.../floorplans.json` pattern, Air Communities + likely other large portfolios.
2. **Add Next.js per-city merge logic** for AMLI-shape portfolios — visit `_next/data/{hash}/.../apartments/{city}/{subregion}-apartments.json` for each region.
3. **Investigate Knock routing** for the 5 lochraven/manchesterlake cluster — Knock adapter exists but the cross-PMS merge is dropping the response.
4. **Extend Phase 6.5** — fetch standalone `*.json` files referenced from `<link rel="prefetch">` or imported by WordPress themes (e.g. `/wp-content/uploads/Custom_JSON_Files/schema.json`).
5. **Phase 6.6 (container-discovery)** would catch `www.edgewood-cedarrapids.com`-shaped sites where rent+bed+sqft+sqft all co-occur on the homepage without any selector-list match.

## Per-property quick-reference

| Property | Verdict | Top URL signature |
|---|---|---|
| amlisouthshore | tier1_api | `amli.com/_next/data/.../apartments/{city}/{subregion}-apartments.json` |
| www.amli.com | jsonld_only | `amli.com/apartments/{city}/{subregion}/{property}` html with JSON-LD |
| livebh.com | jsonld_only | `livebh.com/apartments/{property}` JSON-LD |
| www.liveatthemirage.com | jsonld_only | `/wp-content/uploads/Custom_JSON_Files/schema.json` static |
| livemadisonwakefield.com | jsonld_only | homepage JSON-LD |
| www.willowtrailjax.com | jsonld_only | misrouted to `/contact-us/` page |
| www.thelandingsatnorthingle.com | jsonld_only | webflow + `apartmentschema-1.0.0.js` |
| poudretrailsapartments.com | jsonld_only | `/floor-plans/` html with JSON-LD |
| www.eastperimeterpointe.com | jsonld_only | property-page html with JSON-LD |
| www.laurelcrossingapthomes.com | tier1_api | Adobe Experience Manager `jcr:content` |
| 980central.com | tier1_api | `omappapi.com/v2/embed/...` (OptinMonster — probably noise) |
| www.lochravenapts.com | tier1_api | `doorway-api.knockrentals.com` |
| www.manchesterlake.com | tier1_api | `doorway-api.knockrentals.com` |
| www.edgewood-cedarrapids.com | html_only_dom | homepage with rent+bed+sqft co-occurring |
| (16 weak_signal) | weak | Harbor Group + similar cluster |
| (15 no_unit_signal) | empty | thin captures or wrong URL captured |
