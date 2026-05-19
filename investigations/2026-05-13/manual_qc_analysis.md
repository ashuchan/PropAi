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

---

## Round 2 — full failure-mode bucketing (all 400 tagged rows)

After deeper analysis (URL patterns + manual comments) and live URL probes,
the 400 rows fall into 22 buckets:

| Bucket | Count | % | Status after fix |
|---|---:|---:|---|
| U1 `/conventional/` (Entrata PMS) | 105 | 26% | ✅ resolver path-match |
| U3 `/floor-plans`, `/floorplans` | 88 | 22% | ✅ resolver path-match |
| U4 `/floor-plans#/` (SPA fragment) | 28 | 7% | ✅ resolver path-match |
| U16 same-host long-tail paths | 44 | 11% | ✅ extended regex: `/plans.html`, `/plans.asp`, `/units-available`, `/townhome-floorplans`, `/communities/<slug>`, `/property/<slug>`, `/interactive-site-map`, etc. |
| U5 `.aspx` variants | 17 | 4% | ✅ resolver path-match |
| U6 `/models` (Greystar) | 16 | 4% | ✅ resolver path-match |
| U2 `/floor-plans-and-pricing` | 13 | 3% | ✅ resolver path-match |
| F1 multi-click required | 12 | 3% | ⚠ interactive scraping — separate batch |
| U14 cross-domain rebrand | 11 | 3% | ⚠ partial — 301-redirects auto-handled; some need data-ops |
| F4 no public pricing | 11 | 3% | True failure (call for pricing) |
| F2 site moved | 9 | 2% | Most 301-redirect; data-ops for stragglers |
| U8 `/availability` | 6 | 2% | ✅ resolver path-match |
| U11 `*.onlineleasing.realpage.com` | 5 | 1% | ✅ portal allowlist |
| F6 data embedded (iframe/sitemap) | 5 | 1% | ⚠ iframe-content extraction needed |
| U9 `*.securecafe.com` | 4 | 1% | ✅ portal allowlist |
| F5 pricing on homepage | 3 | <1% | Extractor fix needed |
| F3 dead URL | 2 | <1% | Data-ops |
| U7 `/vacancies` | 1 | <1% | ✅ resolver path-match |
| U12 `*.appfolio.com` | 1 | <1% | ✅ portal allowlist |
| F7 price in image (OCR) | 1 | <1% | Vision LLM |
| U10 `*.prospectportal.com` | 1 | <1% | ✅ portal allowlist |
| U15 comment only, no URL | 17 | 4% | Various sub-modes |

## Coverage of the resolver fix on 373 manual URLs

| Source | Count | % |
|---|---:|---:|
| Caught by `_LEASING_PORTAL_DOMAINS` (cross-domain portals) | 30 | 8.0% |
| Caught by `_CTA_PATH_RE` (URL-path match) | 315 | 84.5% |
| **Total caught** | **345** | **92.5%** |
| Not caught | 28 | 7.5% |

The not-caught 28 break down as:
- 6 — homepage `/` (needs extractor work, not resolver)
- ~12 — property-slug at portfolio root (`/the-cove/`, `/616-orange-avenue`) — too generic to match safely
- 4 — Equity Apartments deep paths (`/<city>/<neighborhood>/<property>-apartments`) — could add later
- 1 — `/apply-now` (correctly path-blacklisted to avoid reCAPTCHA)

## Out-of-scope categories (documented for future batches)

### F1: multi-click required (12 properties, 3%)
Data gated behind clicks (click floorplan → click availability → see rent).
Examples: liveatsurf, equityapartments. Needs interactive Playwright scraping.

### U14/F2: cross-domain rebrand (~20-100 properties)
Original domain dead/redirected; data on a different domain. Many redirect via
301 (canary follows); some serve different content directly. Data-ops fix:
update `properties.csv` from observed `final_url`s. Code fix: track final_url
in property profile.

### F4: no public pricing (11 properties)
"Call for pricing." Genuinely no public rent. Treat as `SUCCESS_NO_PRICING`
verdict or accept as failure.

### F6: data embedded in iframe/sitemap (5 properties)
- AppFolio iframe-embedded listings (hayloft, steellakeplaza)
- RentManager interactive sitemap (henryonthepark)

Needs iframe-content extraction.

### F7: price in image (1 property)
cottonwoodcreekapartments — rent rendered as image. Needs Vision LLM tier.
