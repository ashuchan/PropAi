# 2026-04-20 Adapter-Fix Canary

Verification scaffolding for the six changes in
`ma_poc/claude_adapter_fixes.md`. Use this checklist to hand-verify the
next daily run (2026-04-21) before declaring the fixes green.

---

## Canary table — one property per failure class

| # | Class                                     | Property                              | PropID | Pre-fix tier_used               | Expected post-fix tier_used                             |
|---|-------------------------------------------|---------------------------------------|--------|---------------------------------|---------------------------------------------------------|
| 1 | RentCafe misroute → Funnel                | Olympic by Windsor (Los Angeles)      | 65069  | `TIER_1_API_RENTCAFE`           | `TIER_1_API_FUNNEL` with ≥ 1 unit                       |
| 2 | RentCafe misroute → Funnel                | Windsor Sugarloaf (Suwanee)           | 77589  | `TIER_1_API_RENTCAFE`           | `TIER_1_API_FUNNEL` with ≥ 1 unit                       |
| 3 | RentCafe misroute → Funnel                | Windsor Westminster                   | 5715   | `TIER_1_API_RENTCAFE`           | `TIER_1_API_FUNNEL` with ≥ 1 unit                       |
| 4 | SightMap misroute → TouchTour             | Summer Winds (Las Vegas)              | 24928  | `TIER_1_API_SIGHTMAP`           | `TIER_1_API_TOUCHTOUR_RESEARCH_BLOCKED` (research-blocked) |
| 5 | SightMap misroute → TouchTour             | Madera (Las Vegas)                    | 26151  | `TIER_1_API_SIGHTMAP`           | `TIER_1_API_TOUCHTOUR_RESEARCH_BLOCKED` (research-blocked) |
| 6 | SightMap misroute → TouchTour             | Positano (Las Vegas)                  | 27595  | `TIER_1_API_SIGHTMAP`           | `TIER_1_API_TOUCHTOUR_RESEARCH_BLOCKED` (research-blocked) |
| 7 | Genuine RentCafe (control)                | The Continental (Dallas)              | 35593  | `TIER_1_API_RENTCAFE` (success) | `TIER_1_API_RENTCAFE` (unchanged, ≥ 1 unit)             |
| 8 | Genuine SightMap (control)                | Hawthorne at Traditions               | 268836 | `TIER_1_API_SIGHTMAP` (success) | `TIER_1_API_SIGHTMAP` (unchanged, ≥ 1 unit)             |
| 9 | TIER_1_API LLM waste                      | Harbour Pointe                        | 6477   | `TIER_4_LLM_DOM`, llm_cost > 0  | `LLM_GATE_NO_BODY` (or similar), `llm_cost_usd == 0`    |
|10 | Genuine fallback (control)                | (any TIER_4_LLM_DOM success from 04-20) | —    | `TIER_4_LLM_DOM` (success)      | `TIER_4_LLM_DOM` (unchanged, ≥ 1 unit)                  |

---

## Change 1 replay assertions

- RentCafe failures must now split across ≥ 2 sub-tier codes — any one of
  `TIER_1_API_RENTCAFE_NO_RESPONSE`, `_SHAPE_REJECTED`, `_LIST_EMPTY`,
  `_PARSE_ZERO`. A bare `TIER_1_API_RENTCAFE` on a failed property is a
  regression.
- SightMap failures likewise split across `TIER_1_API_SIGHTMAP_NO_RESPONSE`
  / `_SHAPE_REJECTED` / `_AMENITIES_ONLY` / `_PARSE_FAILED`.
- Successful SightMap scrapes with > 20% join loss carry a
  `SIGHTMAP_PARTIAL_JOIN` entry in `errors`.

```bash
grep -c "TIER_1_API_RENTCAFE_" data/runs/2026-04-21/report.json  # ≥ 2
grep -c "TIER_1_API_SIGHTMAP_" data/runs/2026-04-21/report.json  # ≥ 2
```

## Change 2 replay assertions

- Every property in `_detected_pms.pms ∈ {rentcafe, sightmap, funnel,
  entrata, appfolio, onesite, avalonbay}` must carry
  `_detection_confirmed.confirmed == True` when units > 0.
- The 12 Windsor properties (mgmt = "Windsor Communities"): demoted or
  re-routed — `_detection_confirmed.initial_pms != _detection_confirmed.final_pms`
  OR `final_pms == "funnel"`.
- The 3 Vegas properties: `final_pms == "touchtour"`.

## Change 5 replay assertions

Target list (from claude_adapter_fixes.md §Change 5 gate) — these 13
properties wasted $0.94 on LLM calls on 04-20 and should see `llm_cost_usd == 0`:

```
45791, 77558, 6477, 58699, 21589, 4953, 242945, 71773, 1783,
61377, 78179, 5859, 221319
```

Each should carry a `tier_used` that starts with `LLM_GATE_` (one of
`LLM_GATE_NO_BODY`, `LLM_GATE_DETERMINISTIC_TIERS_SUFFICED`) or otherwise
show `_extract_result.llm_cost_usd == 0`.

---

## Research-blocked fixtures

The following real-property captures are still missing and block the
corresponding happy-path tests:

| Adapter    | Needed canonical_ids            | Notes                                                                     |
|------------|---------------------------------|---------------------------------------------------------------------------|
| funnel     | 65069 / 77589 / 5715            | Playwright capture of `nestiolistings.com/api/v2/listings/residential/rentals/?key=<public>` |
| touchtour  | 24928 / 26151 / 27595           | TouchTour appears DOM-only; capture the SSR floorplans page HTML + any XHR |

Until ≥ 2 captures per adapter exist under
`ma_poc/tests/pms/adapters/fixtures/<adapter>/`, the real-capture test
in each adapter's test file stays `pytest.mark.skip(reason="research-blocked: ...")`.

---

## How to re-run the canary locally

```bash
# 1. Run the full adapter + service test suite — no new failures beyond the
#    pre-existing raw_api fixture gaps should appear.
python -m pytest ma_poc/tests/pms/ ma_poc/tests/services/ --tb=short

# 2. Lint everything touched by Changes 1–6.
ruff check ma_poc/pms/ ma_poc/services/llm_gate.py

# 3. Replay the 04-20 dataset (once scripts/replay_run.py lands) and diff
#    tier_used across the three canary tiers above.
```

---

*Authored alongside Change 6 of `ma_poc/claude_adapter_fixes.md`. Keep
this file in sync with the canary list when new failure classes emerge.*
