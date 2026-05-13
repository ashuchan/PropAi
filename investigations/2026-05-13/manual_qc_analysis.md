# May 13 manual-QC pattern analysis

Source: `/Users/ankur/Downloads/failed_properties_2026-05-13.xlsx` — 1214 failed
properties (`FAILED_NO_DATA`), of which **400 were manually QC'd** with a `url`
or `comment` column indicating where the data could be found.

## Tagged URL pattern distribution (373 with a manual URL)

| Pattern | Count | % | What it is |
|---|---:|---:|---|
| `/conventional/` (Entrata PMS site) | 108 | 29% | `<vanity>/<region>/<slug>/conventional/` |
| `/floor-plans` | 78 | 21% | Direct sub-path |
| `/floorplans` | 57 | 15% | Direct sub-path |
| `/models` | 16 | 4% | Greystar pattern |
| `/floor-plans-and-pricing` | 13 | 3% | Verbose variant |
| `/floor-plans.aspx` (case-variants) | 14 | 4% | RentCafe legacy ASPX |
| `/floorplans.aspx` | 6 | 2% | RentCafe legacy ASPX |
| `/availability` | 6 | 2% | Generic |
| Cross-domain rebrand | 53 | 14% | Different domain entirely |
| Portal sub-products (securecafe, prospectportal, appfolio) | 16 | 4% | Cross-domain portal |
| Other | 6 | 2% | |

**78% of tagged URLs match one of these URL-path patterns** — but the production
resolver only recognized anchor *text* matching `apply|availab|floor\s*plan|lease|resident.*portal`,
missing all the URL-path-based clues.

## #1 unaddressed pattern: `/conventional/` (Entrata Property Marketing Site)

108 of 373 manual-tagged failures (29%) have unit data at a URL ending with
`/<region>/<slug>/conventional/`. This is the Entrata Property Marketing Site URL
scheme — `conventional` distinguishes it from `student` / `senior` / `affordable`
housing variants on the same CMS.

Examples:
- `hpilegacyatfestival.com/montgomery-montgomery/legacy-at-festival/conventional/`
- `themetropolitanat40park.com/morristown/the-metropolitan-at-40-park/conventional/`
- `93east.prospectportal.com/atlanta/93-east/conventional/`
- `home.securityproperties.com/bellevuecrossing/bend/bellevue-crossing-apartments/conventional/`

**None of the existing adapter fingerprints or resolver path heuristics catch
this**, so all 108 properties currently fail.

## Fix

`ma_poc/pms/resolver.py` extended with:
- `_CTA_PATH_RE` now matches `/conventional`, `/models`, `/vacancies`,
  `/floor-plans-and-pricing`, `/floorplan-availability`, `/check-availability`,
  `/oleapplication`, plus existing patterns.
- `_LEASING_PORTAL_DOMAINS` adds `securecafe.com`, `securecafenet.com`,
  `prospectportal.com`, `appfolio.com`.
- Step 3 splits into two passes: portal-host first (cross-domain OK), then
  same-host path-match (to avoid following random `/listings` on lead-gen sites).
- Candidate cap raised 5 → 8 + dedup by (netloc + path).
- Word-boundary priority matching (no more "uni" in "Communities").

## Predicted URLs for the 814 untagged

See `untagged_predicted_urls.csv` — per-property 5-column predictions (top guess
for each of the dominant patterns). User can click-through each row to confirm
which URL actually has the data, fast-tracking ground-truth tagging.

Top-3 prediction recall on the 373 tagged: 13.1%. Top-anywhere recall: 38.6%.
The region/slug component varies too much for pure construction; the resolver
fix (which scans the rendered DOM at canary time) is the higher-leverage path.
