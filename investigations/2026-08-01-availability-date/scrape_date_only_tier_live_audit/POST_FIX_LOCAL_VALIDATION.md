# Post-fix local availability-date validation

Capture date: `2026-08-01`.

Scope: local current-live replays only. This is not a GCP canary, production
run, deployment, or paid-browser result. LLM, link-hop, CAPTCHA solving, Web
Unlocker, FlareSolverr, and Hyperbrowser were not used.

## Five-family conclusion

- RealPage OLL and OneSite current APIs had a proven parser defect: the live
  `{response: {units: [...]}}` envelope and `internalAvailableDate` field were
  not consumed. Post-fix replay preserved all 20/20 and 23/23 explicit future
  rows, respectively, in the current future-oracle samples.
- Squarespace had two narrow discovery/formatting gaps: a protocol-relative
  AppFolio tenant URL and yearless visible dates such as `Available 9/1`.
  Both routes preserve their current explicit future rows after the fix;
  visible `Available Now` remains distinct provenance.
- Entrata API and AspenSquare did not show a blanket date-loss defect in the
  live future-oracle samples. Their exact native future dates were already
  preserved, so no scrape-date-driven blanket rewrite was applied.
- OneSite Workflow can legitimately publish native unit IDs without any date
  field. The formatter's capture-date default is not evidence that a future
  date was lost unless another exact property-scoped source publishes one.

## Swifty / 946 MLK production-path validation

The exact Swifty route was generalized only after three independent live
properties exposed the same entry markers and same-origin WordPress AJAX
contract: 946 MLK, BroadVue, and The Kace. The parser accepts both observed
four- and five-column unit-table layouts.

The final 946 MLK replay used the full `scraper.scrape()` detector/dispatcher
path with `page=None`, all LLM budgets set to zero, and link-hop disabled.
Current detector result was `knock`; the empty Knock primary fell through to
the exact-marker generic Swifty recovery and emitted three native priced
units.

| Unit | Raw source token | Final `available_date` | Provenance |
|---|---|---|---|
| 308 | `09-09-2026` | `2026-09-09` | `explicit_future` |
| 213 | `Available Now` | `2026-08-01` | `available_now` |
| 209 | `09-06-2026` | `2026-09-06` | `explicit_future` |

Focused availability/recovery validation after this wiring: `146 passed`;
Ruff checks passed. No commit, push, merge, deployment, or canary has been
performed.
