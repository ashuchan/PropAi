# Scrape-date-only tier live availability audit

Capture: `2026-08-01`; local current-live audit, not a paid canary.

A scrape-date-only output is not itself a defect. A property is counted as defective only when its exact native source publishes a future date and the current LLM-off replay fails to preserve it.

| Family | Probed | Future oracle | Defects | Defect rate |
|---|---:|---:|---:|---:|
| AspenSquare | 3 | 2 | 0 | 0.0% |
| Entrata API | 3 | 3 | 0 | 0.0% |
| OneSite / OneSite Workflow | 8 | 3 | 3 | 100.0% |
| RealPage OLL API | 4 | 3 | 3 | 100.0% |
| Squarespace | 3 | 3 | 1 | 33.3% |

Historical-denominator caveat: the July Squarespace unit-block tier contains one property and OneSite Workflow contains two. Supplemental exact configured probes meet the live sample rule but are labeled out-of-denominator.

All final properties passed an explicit property-identity and contamination boundary. No production file, canary, or external solver was changed or invoked by this audit.
