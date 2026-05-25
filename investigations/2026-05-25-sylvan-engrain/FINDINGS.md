# Sylvan Tributary / Engrain SightMap rent extraction

User-flagged residue: 2026-05-25
URL: <https://www.sylvantributary.com/floor-plans>
Operator note: "rent data in engrain sitemap, not captured"

## TL;DR — no bug; ship is regression coverage

The existing pipeline already extracts all 125 priced units from this
site end-to-end. The "not captured" report was a forward-looking flag,
not an observed production failure. The ship adds 8 regression tests
and the live HTML / embed-page / API JSON fixtures so any future change
that breaks this path fails loudly.

## Probe (deep-probe, 2026-05-25)

### Site shape

- Squarespace-hosted marketing page, **not** in `properties.csv`.
- HTML size: 534 KB. Curl_cffi chrome120 returns 200 OK.
- Page contains TWO embeds, both as `<iframe src=>`:

  | Iframe | Offset in HTML | Data |
  |---|---|---|
  | `sightmap.com/embed/40vl503rwle/` | ~349 KB | 125 priced units (Engrain) |
  | `www.embed.fortresstech.io/unit-availability/.../` | ~465 KB | FortressTech leasing portal |

  The user's note confirms SightMap is where the rent lives. FortressTech
  appears to be an auxiliary embed.

### SightMap API behind the iframe

- Embed code `40vl503rwle` → embed page → `window.__APP_CONFIG__.sightmaps[].href`
  → `https://sightmap.com/app/api/v1/60p7y1x3p7n/sightmaps/121550`
- API response: 219 KB JSON, `data.units` has 125 entries joined to
  7 floor plans.
- 125 / 125 units have a positive numeric `price`, a `display_price`
  string, an `area`, and an `available_on` date. No rent gap.

## Diagnosis — existing pipeline handles this site

### Detector (offline, HTML-only)

`_iter_html_markers` yields, in priority order:

```
sightmap       0.93  STRONG: sightmap.com/embed/ in HTML
fortresstech   0.90  STRONG: embed.fortresstech.io/unit-availability/
squarespace_nopms 0.85 MEDIUM: squarespace.com script in HTML
sightmap       0.80  WEAK:   bare sightmap.com substring
```

`detect_pms` picks `sightmap` (0.93) — correct.

### Adapter

`SightMapAdapter.extract` with the captured XHR present →
`parse_sightmap_payload` → 125 units, tier `TIER_1_API_SIGHTMAP`,
confidence > 0.7.

With `api_responses=[]` (simulating a canary capture window that misses
the iframe XHR) → `_try_sightmap_iframe_fallback` →
`find_sightmap_embed_codes` returns `["40vl503rwle"]` →
`extract_sightmap_api_url` returns the canonical API URL → httpx GET
→ same 125 units, tier `TIER_1_API_SIGHTMAP_IFRAME`.

### Verified locally with `scrape()` orchestrator

```
extraction_tier_used: TIER_1_API_SIGHTMAP_IFRAME
_adapter_used:        sightmap
unit count:           125
sample (A-101):       $1,774 / 856 sqft / 2 Bedroom / AVAILABLE 2026-05-26
errors:               []
```

## Why this ship still matters

The detector margin between `sightmap` 0.93 and `fortresstech` 0.90 is
**only 0.03**. Any future change that:

- lowers the SightMap STRONG signal (e.g. retitling the marker to make
  it less specific), OR
- raises the FortressTech STRONG signal,

would silently misroute Sylvan-class sites (Squarespace + dual-iframe)
to FortressTech and lose all 125 priced units. The regression test
`test_sightmap_signal_margin_over_fortresstech_recorded` makes this
failure mode loud.

The iframe-fallback test (`test_adapter_iframe_fallback_when_xhr_
uncaptured`) is also new coverage — the existing test suite had no
hermetic verification that the embed-page → API URL chain produces
units end-to-end. It now does.

## Ship

| Artifact | Purpose |
|---|---|
| `ma_poc/tests/pms/adapters/test_sylvan_tributary_sightmap.py` | 8 new tests |
| `ma_poc/tests/pms/adapters/fixtures/sightmap/sylvan_tributary/floor_plans.html` | Live 2026-05-25 page |
| `ma_poc/tests/pms/adapters/fixtures/sightmap/sylvan_tributary/embed_40vl503rwle.html` | Live SightMap embed page |
| `ma_poc/tests/pms/adapters/fixtures/sightmap/sylvan_tributary/api_response.json` | Live SightMap API payload |

Test results: 8/8 PASS. Full sightmap suite: 103/103 PASS (95 prior + 8 new).

## Out-of-scope follow-ups (not in this commit)

- `properties.csv` does not contain `sylvantributary.com`. If the user
  wants the property under production scrape coverage, it needs adding.
- The FortressTech iframe on the same page is an interesting data
  cross-check — could compare unit counts to SightMap to detect partial-
  publish drift. Worth flagging if FortressTech adapter coverage grows.
