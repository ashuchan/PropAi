# Refactor Baseline

## Current-pipeline metrics

_Filled by `scripts/refactor_baseline.py` — see appended runs below._

## Known PMS distribution in current property set

_Fill manually from handoff doc + CSV inspection._

## Target metrics after refactor (hypothesis)

- Tier-1 success rate: current X% → target Y%
- LLM calls per property (median): current 0–3 → target 0
- LLM $ per daily run: current $A → target $B
- Redundant-call count: current N → target 0
- `api_provider == null` profiles: current M% → target <10%


## Baseline captured 2026-04-16T20:36:54+00:00

### Run: `2026-04-15`
Properties with reports: **78**

#### Tier distribution
| Extraction Tier | Count | % of total |
| --- | --- | --- |
| FAILED | 32 | 41.0% |
| TIER_5_5_EXPLORATORY | 19 | 24.4% |
| TIER_5_PORTAL | 8 | 10.3% |
| UNKNOWN | 7 | 9.0% |
| TIER_4_LLM | 4 | 5.1% |
| TIER_3_DOM | 3 | 3.8% |
| TIER_1_API | 3 | 3.8% |
| TIER_3_DOM_LLM | 2 | 2.6% |

#### LLM cost
| Metric | Value |
| --- | --- |
| Total LLM cost (USD) | $0.85182 |
| Properties using LLM | 38 |
| Avg cost per LLM-using property | $0.02242 |

#### Failure breakdown
| First error (<=80 chars) | Count |
| --- | --- |
| (no error message) | 28 |
| ElementHandle.inner_text: Error: Node is not an HTMLElement | 4 |
| scrape timeout after 180s | 3 |
| Homepage load error: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ambassador | 1 |
| Homepage load error: Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://bowerypoi | 1 |
| Homepage load error: Page.goto: net::ERR_CONNECTION_TIMED_OUT at https://www.gra | 1 |
| Homepage load error: Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://www.tides | 1 |

#### Profile maturity distribution
| Maturity | Count |
| --- | --- |
| COLD | 51 |
| WARM | 19 |
| HOT | 8 |

#### Profiles with `api_provider == null`
| Metric | Value |
| --- | --- |
| Profiles with unknown api_provider | 70 |
| Total profiles | 78 |
| % unknown | 89.7% |

#### Timing
| Metric | Value |
| --- | --- |
| Avg scrape duration | 18.4s |
| P95 scrape duration | n/a |

#### Redundant LLM calls (UNITS_EMPTY + LLM calls > 0)
| Metric | Value |
| --- | --- |
| Properties wasting LLM spend | 31 |


## Baseline captured 2026-04-16T21:07:06+00:00

### Run: `2026-04-15`
Properties with reports: **78**

#### Tier distribution
| Extraction Tier | Count | % of total |
| --- | --- | --- |
| FAILED | 32 | 41.0% |
| TIER_5_5_EXPLORATORY | 19 | 24.4% |
| TIER_5_PORTAL | 8 | 10.3% |
| UNKNOWN | 7 | 9.0% |
| TIER_4_LLM | 4 | 5.1% |
| TIER_3_DOM | 3 | 3.8% |
| TIER_1_API | 3 | 3.8% |
| TIER_3_DOM_LLM | 2 | 2.6% |

#### LLM cost
| Metric | Value |
| --- | --- |
| Total LLM cost (USD) | $0.85182 |
| Properties using LLM | 38 |
| Avg cost per LLM-using property | $0.02242 |

#### Failure breakdown
| First error (<=80 chars) | Count |
| --- | --- |
| (no error message) | 28 |
| ElementHandle.inner_text: Error: Node is not an HTMLElement | 4 |
| scrape timeout after 180s | 3 |
| Homepage load error: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ambassador | 1 |
| Homepage load error: Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://bowerypoi | 1 |
| Homepage load error: Page.goto: net::ERR_CONNECTION_TIMED_OUT at https://www.gra | 1 |
| Homepage load error: Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://www.tides | 1 |

#### Profile maturity distribution
| Maturity | Count |
| --- | --- |
| COLD | 51 |
| WARM | 19 |
| HOT | 8 |

#### Profiles with `api_provider == null`
| Metric | Value |
| --- | --- |
| Profiles with unknown api_provider | 70 |
| Total profiles | 78 |
| % unknown | 89.7% |

#### Timing
| Metric | Value |
| --- | --- |
| Avg scrape duration | 18.4s |
| P95 scrape duration | n/a |

#### Redundant LLM calls (UNITS_EMPTY + LLM calls > 0)
| Metric | Value |
| --- | --- |
| Properties wasting LLM spend | 31 |


## Baseline captured 2026-04-16T21:10:20+00:00

### Run: `2026-04-15`
Properties with reports: **78**

#### Tier distribution
| Extraction Tier | Count | % of total |
| --- | --- | --- |
| FAILED | 32 | 41.0% |
| TIER_5_5_EXPLORATORY | 19 | 24.4% |
| TIER_5_PORTAL | 8 | 10.3% |
| UNKNOWN | 7 | 9.0% |
| TIER_4_LLM | 4 | 5.1% |
| TIER_3_DOM | 3 | 3.8% |
| TIER_1_API | 3 | 3.8% |
| TIER_3_DOM_LLM | 2 | 2.6% |

#### LLM cost
| Metric | Value |
| --- | --- |
| Total LLM cost (USD) | $0.85182 |
| Properties using LLM | 38 |
| Avg cost per LLM-using property | $0.02242 |

#### Failure breakdown
| First error (<=80 chars) | Count |
| --- | --- |
| (no error message) | 28 |
| ElementHandle.inner_text: Error: Node is not an HTMLElement | 4 |
| scrape timeout after 180s | 3 |
| Homepage load error: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ambassador | 1 |
| Homepage load error: Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://bowerypoi | 1 |
| Homepage load error: Page.goto: net::ERR_CONNECTION_TIMED_OUT at https://www.gra | 1 |
| Homepage load error: Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://www.tides | 1 |

#### Profile maturity distribution
| Maturity | Count |
| --- | --- |
| COLD | 51 |
| WARM | 19 |
| HOT | 8 |

#### Profiles with `api_provider == null`
| Metric | Value |
| --- | --- |
| Profiles with unknown api_provider | 70 |
| Total profiles | 78 |
| % unknown | 89.7% |

#### Timing
| Metric | Value |
| --- | --- |
| Avg scrape duration | 18.4s |
| P95 scrape duration | n/a |

#### Redundant LLM calls (UNITS_EMPTY + LLM calls > 0)
| Metric | Value |
| --- | --- |
| Properties wasting LLM spend | 31 |


## Baseline captured 2026-04-17T03:45:27+00:00

### Run: `2026-04-15`
Properties with reports: **78**

#### Tier distribution
| Extraction Tier | Count | % of total |
| --- | --- | --- |
| FAILED | 32 | 41.0% |
| TIER_5_5_EXPLORATORY | 19 | 24.4% |
| TIER_5_PORTAL | 8 | 10.3% |
| UNKNOWN | 7 | 9.0% |
| TIER_4_LLM | 4 | 5.1% |
| TIER_3_DOM | 3 | 3.8% |
| TIER_1_API | 3 | 3.8% |
| TIER_3_DOM_LLM | 2 | 2.6% |

#### LLM cost
| Metric | Value |
| --- | --- |
| Total LLM cost (USD) | $0.85182 |
| Properties using LLM | 38 |
| Avg cost per LLM-using property | $0.02242 |

#### Failure breakdown
| First error (<=80 chars) | Count |
| --- | --- |
| (no error message) | 28 |
| ElementHandle.inner_text: Error: Node is not an HTMLElement | 4 |
| scrape timeout after 180s | 3 |
| Homepage load error: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ambassador | 1 |
| Homepage load error: Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://bowerypoi | 1 |
| Homepage load error: Page.goto: net::ERR_CONNECTION_TIMED_OUT at https://www.gra | 1 |
| Homepage load error: Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://www.tides | 1 |

#### Profile maturity distribution
| Maturity | Count |
| --- | --- |
| COLD | 51 |
| WARM | 19 |
| HOT | 8 |

#### Profiles with `api_provider == null`
| Metric | Value |
| --- | --- |
| Profiles with unknown api_provider | 70 |
| Total profiles | 78 |
| % unknown | 89.7% |

#### Timing
| Metric | Value |
| --- | --- |
| Avg scrape duration | 18.4s |
| P95 scrape duration | n/a |

#### Redundant LLM calls (UNITS_EMPTY + LLM calls > 0)
| Metric | Value |
| --- | --- |
| Properties wasting LLM spend | 31 |


## Baseline captured 2026-04-17T03:45:56+00:00

### Run: `2026-04-15`
Properties with reports: **78**

#### Tier distribution
| Extraction Tier | Count | % of total |
| --- | --- | --- |
| FAILED | 32 | 41.0% |
| TIER_5_5_EXPLORATORY | 19 | 24.4% |
| TIER_5_PORTAL | 8 | 10.3% |
| UNKNOWN | 7 | 9.0% |
| TIER_4_LLM | 4 | 5.1% |
| TIER_3_DOM | 3 | 3.8% |
| TIER_1_API | 3 | 3.8% |
| TIER_3_DOM_LLM | 2 | 2.6% |

#### LLM cost
| Metric | Value |
| --- | --- |
| Total LLM cost (USD) | $0.85182 |
| Properties using LLM | 38 |
| Avg cost per LLM-using property | $0.02242 |

#### Failure breakdown
| First error (<=80 chars) | Count |
| --- | --- |
| (no error message) | 28 |
| ElementHandle.inner_text: Error: Node is not an HTMLElement | 4 |
| scrape timeout after 180s | 3 |
| Homepage load error: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ambassador | 1 |
| Homepage load error: Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://bowerypoi | 1 |
| Homepage load error: Page.goto: net::ERR_CONNECTION_TIMED_OUT at https://www.gra | 1 |
| Homepage load error: Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://www.tides | 1 |

#### Profile maturity distribution
| Maturity | Count |
| --- | --- |
| COLD | 51 |
| WARM | 19 |
| HOT | 8 |

#### Profiles with `api_provider == null`
| Metric | Value |
| --- | --- |
| Profiles with unknown api_provider | 70 |
| Total profiles | 78 |
| % unknown | 89.7% |

#### Timing
| Metric | Value |
| --- | --- |
| Avg scrape duration | 18.4s |
| P95 scrape duration | n/a |

#### Redundant LLM calls (UNITS_EMPTY + LLM calls > 0)
| Metric | Value |
| --- | --- |
| Properties wasting LLM spend | 31 |


## Baseline captured 2026-04-17T03:47:13+00:00

### Run: `2026-04-15`
Properties with reports: **78**

#### Tier distribution
| Extraction Tier | Count | % of total |
| --- | --- | --- |
| FAILED | 32 | 41.0% |
| TIER_5_5_EXPLORATORY | 19 | 24.4% |
| TIER_5_PORTAL | 8 | 10.3% |
| UNKNOWN | 7 | 9.0% |
| TIER_4_LLM | 4 | 5.1% |
| TIER_3_DOM | 3 | 3.8% |
| TIER_1_API | 3 | 3.8% |
| TIER_3_DOM_LLM | 2 | 2.6% |

#### LLM cost
| Metric | Value |
| --- | --- |
| Total LLM cost (USD) | $0.85182 |
| Properties using LLM | 38 |
| Avg cost per LLM-using property | $0.02242 |

#### Failure breakdown
| First error (<=80 chars) | Count |
| --- | --- |
| (no error message) | 28 |
| ElementHandle.inner_text: Error: Node is not an HTMLElement | 4 |
| scrape timeout after 180s | 3 |
| Homepage load error: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ambassador | 1 |
| Homepage load error: Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://bowerypoi | 1 |
| Homepage load error: Page.goto: net::ERR_CONNECTION_TIMED_OUT at https://www.gra | 1 |
| Homepage load error: Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://www.tides | 1 |

#### Profile maturity distribution
| Maturity | Count |
| --- | --- |
| COLD | 51 |
| WARM | 19 |
| HOT | 8 |

#### Profiles with `api_provider == null`
| Metric | Value |
| --- | --- |
| Profiles with unknown api_provider | 70 |
| Total profiles | 78 |
| % unknown | 89.7% |

#### Timing
| Metric | Value |
| --- | --- |
| Avg scrape duration | 18.4s |
| P95 scrape duration | n/a |

#### Redundant LLM calls (UNITS_EMPTY + LLM calls > 0)
| Metric | Value |
| --- | --- |
| Properties wasting LLM spend | 31 |


## Baseline captured 2026-04-17T03:56:08+00:00

### Run: `2026-04-15`
Properties with reports: **78**

#### Tier distribution
| Extraction Tier | Count | % of total |
| --- | --- | --- |
| FAILED | 32 | 41.0% |
| TIER_5_5_EXPLORATORY | 19 | 24.4% |
| TIER_5_PORTAL | 8 | 10.3% |
| UNKNOWN | 7 | 9.0% |
| TIER_4_LLM | 4 | 5.1% |
| TIER_3_DOM | 3 | 3.8% |
| TIER_1_API | 3 | 3.8% |
| TIER_3_DOM_LLM | 2 | 2.6% |

#### LLM cost
| Metric | Value |
| --- | --- |
| Total LLM cost (USD) | $0.85182 |
| Properties using LLM | 38 |
| Avg cost per LLM-using property | $0.02242 |

#### Failure breakdown
| First error (<=80 chars) | Count |
| --- | --- |
| (no error message) | 28 |
| ElementHandle.inner_text: Error: Node is not an HTMLElement | 4 |
| scrape timeout after 180s | 3 |
| Homepage load error: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ambassador | 1 |
| Homepage load error: Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://bowerypoi | 1 |
| Homepage load error: Page.goto: net::ERR_CONNECTION_TIMED_OUT at https://www.gra | 1 |
| Homepage load error: Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://www.tides | 1 |

#### Profile maturity distribution
| Maturity | Count |
| --- | --- |
| COLD | 51 |
| WARM | 19 |
| HOT | 8 |

#### Profiles with `api_provider == null`
| Metric | Value |
| --- | --- |
| Profiles with unknown api_provider | 70 |
| Total profiles | 78 |
| % unknown | 89.7% |

#### Timing
| Metric | Value |
| --- | --- |
| Avg scrape duration | 18.4s |
| P95 scrape duration | n/a |

#### Redundant LLM calls (UNITS_EMPTY + LLM calls > 0)
| Metric | Value |
| --- | --- |
| Properties wasting LLM spend | 31 |
