# Deep-probe grind — "reached but 0 units" + "partial" cohort

Date started: 2026-05-22
Worklist: 458 properties (306 site-opened-no-data + 152 partial-extraction)
Source: the 5K canary run `full-d982dbd` strict-fail cohort (<1 unit with rent+sqft).

## Goal

For each property: reach the **unit-level / availability detail** — not the index —
and record whether real per-unit rent+sqft data exists, where it lives, and what
URL/mechanism serves it. Output feeds adapter fixes (extraction misses) vs
true-negative labelling (sites with genuinely no public data).

## Anti-shallow protocol — MANDATORY per property

Do NOT conclude "no data" from the landing page or the first failing path.
For every property, exhaust this ladder until unit-level data is found OR all
paths are tried:

1. **Landing page** — load `/`, read DOM. Find nav anchors: "Floor Plans",
   "Availability", "Apartments", "Pricing", "Rentals", "Lease", "Find Your Home",
   "Our Apartments", "View Availability", "Models".
2. **Conventional deep URLs** — try ALL variants (hyphenation matters):
   `/floorplans` `/floor-plans` `/floorplans/` `/floor-plans/`
   `/availability` `/availability/` `/view-availability` `/availabilities`
   `/apartments` `/apartments/` `/our-apartments` `/rentals` `/rentals/`
   `/pricing` `/prices` `/lease` `/leasing` `/floor-plans-pricing`
   `/apartments-pricing` `/models` `/units`
3. **Click every drill anchor** — on the floorplans/availability page, click
   EVERY "View Details", "View Availability", "N Available", "Check Availability",
   floorplan card, and bedroom-type tab. Unit data is one level below the plan index.
4. **PMS-specific deep paths** — if a PMS is recognisable:
   - Entrata: `.prospectportal.com/...`, `/Apartments/module/.../conventional/`
   - RentCafe/SecureCafe: `.securecafe.com/onlineleasing/.../availableunits.aspx`
   - SightMap: the `sightmap.com/embed/{id}` iframe → its app
   - RealPage: `onlineleasing.realpage.com`
5. **Network panel** — on the page that should hold units, open the Network tab,
   reload, look for an XHR/fetch returning JSON with unit/floorplan/availability.
   Record the API URL.
6. Only after 1–5 all fail → label `no_public_data`.

## Per-property result record (one JSONL line)

```json
{"property_id":"","url":"","probed_at":"","outcome":"",
 "data_url":"","data_mechanism":"","api_url":"",
 "unit_level_found":false,"plan_level_found":false,
 "rent_present":false,"sqft_present":false,"unit_count":0,
 "pms_guess":"","notes":""}
```

`outcome` ∈ `unit_level_data` | `plan_level_only` | `no_public_data` |
`bot_blocked` | `dead_url` | `needs_login` | `error`.

## Resume

`worklist.json` — each entry has `status` (pending|done|skipped). `results.jsonl`
is append-only, one line per probed property. To resume: skip property_ids
already present in results.jsonl.
