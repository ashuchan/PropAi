# Jugnu Baseline — captured 2026-04-18T03:51:50Z

Source run: `data\runs\2026-04-17`

## 1. Totals

| Metric | Value | Notes |
|---|---|---|
| CSV rows | 78 | |
| Properties OK | 41 | |
| Properties failed | 36 | |
| Failure rate | 46.15% | |
| Carry-forward | 1 | |
| DLQ eligible | 36 | |

## 2. Tier distribution

| Tier | Count | % |
|---|---|---|
| FAILED | 25 | 32.1% |
| TIER_5_5_EXPLORATORY | 23 | 29.5% |
| UNKNOWN | 7 | 9.0% |
| TIER_4_LLM | 6 | 7.7% |
| FAILED_UNREACHABLE | 5 | 6.4% |
| TIER_1_API | 3 | 3.8% |
| TIER_5_PORTAL | 3 | 3.8% |
| TIER_3_DOM | 3 | 3.8% |
| TIER_3_DOM_LLM | 2 | 2.6% |
| TIER_5_VISION | 1 | 1.3% |

## 3. LLM cost

| Metric | Value |
|---|---|
| Total cost | $0.8487 |
| Properties with LLM calls | 0 |
| Properties with Vision calls | 0 |
| Avg cost per LLM property | $0.8487 |

### Wasted LLM calls

Count: 0


## 4. Failure signatures

| Signature | Count | Sample CIDs |
|---|---|---|
| NO_ERROR_MESSAGE | 25 | `252511`, `272886`, `265766`, `291336`, `17976` |
| ElementHandle.inner_text: Error: Node is not an HTMLElement
Call log:
  - waitin | 4 | `11797`, `284187`, `293707`, `265817` |
| scrape timeout after 180s | 3 | `46954`, `60167`, `3218` |
| Homepage load error: Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://www.tides | 1 | `5317` |
| Homepage load error: Page.goto: net::ERR_CONNECTION_TIMED_OUT at https://www.gra | 1 | `26217` |
| Homepage load error: Page.goto: net::ERR_TOO_MANY_REDIRECTS at https://bowerypoi | 1 | `237747` |
| Homepage load error: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://www.83frei | 1 | `240551` |
| Homepage load error: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://ambassador | 1 | `15808` |

## 5. Profile maturity

| Maturity | Count | % |
|---|---|---|
| COLD | 441 | 76.4% |
| WARM | 126 | 21.8% |
| HOT | 10 | 1.7% |

Properties with `api_provider == null`: 70 (12.1%).

## 6. Timing

No per-property timing data available.

## 7. Change detection

Current skip rate: 0% (not implemented).

## 8. Targets for Jugnu (to be filled by the human)

| Metric | Current (J0) | Target (post-J9) |
|---|---|---|
| Success rate | 53.85% | >= 95% |
| LLM cost / run | $0.8487 | <= $0.0849 |
| Wasted LLM calls | 0 | 0 |
| api_provider == null | 12.1% | < 10% |
| Change-detection skip | 0% | >= 30% |
| Failure rate | 46.15% | <= 5% |
