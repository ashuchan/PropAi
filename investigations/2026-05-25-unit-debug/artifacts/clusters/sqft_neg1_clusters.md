# sqft=-1 cluster report (52 props)

## Top-level breakdown
| Verdict | Count | % |
|---|---:|---:|
| SQFT_TRULY_ABSENT | 42 | 81% |
| SQFT_FOUND_AT_/floorplans | 3 | 6% |
| SQFT_FOUND_AT_/floor-plans | 2 | 4% |
| SQFT_FOUND_AT_landing | 2 | 4% |
| SQFT_FOUND_AT_/floorplans,/floor-plans | 1 | 2% |
| BLOCKED_HTTP_403 | 1 | 2% |
| SQFT_FOUND_AT_landing,/floorplans | 1 | 2% |

## Per-tier extraction-miss rate
| Tier | Total | Sqft Found (adapter miss) | Truly Absent (operator-gap) | Other |
|---|---:|---:|---:|---:|
| TIER_1_DOM_APPFOLIO_VANITY | 8 | 0 (0%) | 8 | 0 |
| TIER_1_DOM_GENERIC_PLAN_TEXT | 8 | 3 (38%) | 5 | 0 |
| TIER_1_API_RENTCAFE_SECURECAFE | 6 | 4 (67%) | 2 | 0 |
| TIER_MERGED_CROSS_PAGE | 5 | 1 (20%) | 4 | 0 |
| TIER_3_DOM | 5 | 1 (20%) | 4 | 0 |
| TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL | 4 | 0 (0%) | 3 | 1 |
| TIER_1_API | 4 | 0 (0%) | 4 | 0 |
| TIER_1_DOM_APPFOLIO_VANITY_PLAN_LEVEL | 3 | 0 (0%) | 3 | 0 |
| TIER_1_API_REPLI360 | 3 | 0 (0%) | 3 | 0 |
| TIER_1_API_REPLI360_PLAN_LEVEL | 2 | 0 (0%) | 2 | 0 |
| TIER_1_API_ONESITE_WORKFLOW | 2 | 0 (0%) | 2 | 0 |
| TIER_1_API_SIGHTMAP_IFRAME | 2 | 0 (0%) | 2 | 0 |

## SQFT_FOUND props (adapter misses — fixable)
- `36268` [Stoney Creek](http://www.stoneycreekapthomes.com/) — tier=TIER_1_DOM_GENERIC_PLAN_TEXT — sqft at: `/floor-plans` — values: [100, 768, 900]
- `32794` [The Majestic](https://majesticvernonhills.com/) — tier=TIER_1_DOM_GENERIC_PLAN_TEXT — sqft at: `landing` — values: [1001, 1045, 1064]
- `37317` [Vestavia Place](https://vestaviaplace.com/) — tier=TIER_1_API_RENTCAFE_SECURECAFE — sqft at: `/floor-plans` — values: [100, 300, 700]
- `278370` [Alder Square](https://www.aldersquare.com/) — tier=TIER_1_DOM_GENERIC_PLAN_TEXT — sqft at: `/floorplans` — values: [1020]
- `33262` [The Mount Royal](https://www.themtroyal.com/) — tier=TIER_1_API_RENTCAFE_SECURECAFE — sqft at: `/floorplans` — values: [100, 550, 850]
- `8376` [Alvista 23](https://www.alvista23.com) — tier=TIER_1_API_RENTCAFE_SECURECAFE — sqft at: `/floorplans` — values: [832, 1050, 1178]
- `245398` [Bellevue Towers](https://www.sspropertiesinvestment.com/vacancies/) — tier=TIER_MERGED_CROSS_PAGE — sqft at: `landing` — values: [230, 540, 625]
- `238181` [Ardence & Bloom](https://www.ardencebloom.com/) — tier=TIER_1_API_RENTCAFE_SECURECAFE — sqft at: `/floorplans, /floor-plans` — values: [761, 1048, 1049]
- `49248` [Sandpiper](https://www.sandpiperapartmentssaltlakecity.com/) — tier=TIER_3_DOM — sqft at: `landing, /floorplans` — values: [458, 930, 1128]