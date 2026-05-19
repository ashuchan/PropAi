# Canary Iterate — 2026-05-17 — Artifact Index

Durable record of the canary unit-level-extraction investigation. **Nothing
lives in `/tmp` anymore** — every working document is copied here and
git-committed. Narrative + decisions are in `FINDINGS.md`; this README is the
file-by-file index.

## Start here
| File | What it is |
|---|---|
| `FINDINGS.md` | **Primary narrative** — every iteration (iter-16..19), the systemic root cause, all user-validated eyeball verdicts, sizing, decisions. Read this first. |
| `EXECUTIVE_SUMMARY.md` / `FINAL_5K_SUMMARY.md` | Earlier high-level summaries (pre-iter-16). |
| `accumulate_best.py` | Ledger accumulator (unit-level + tier-class + goal_met logic). |

## Headline numbers (as of this snapshot)
- Prod LIVE Tier-1 unit-level: **~30%** (1,511/4,982). Canary-stack achievable (measured): **~60%** (~2,982/4,982), securecafe-dominated.
- iter-16 apts247, iter-17 spherexx-ZRS, iter-18 entrata-WP, iter-19 sightmap-Step7b — all committed, additive, completeness-verified (apts247 8/9, securecafe 12/12 — not truncating).
- Systemic root cause: ONE gap — pipeline doesn't crawl+render per-floorplan **detail** pages (units live one level deeper across every platform).
- "~456 genuine-custom" true split: ~206 vendor-template + ~39 misfp-known + 209 static-unclassified → static auto-check confirmed only 69 HAS_UNITS (proves JS-render → needs the #1 render-crawl). Eyeball batch 3 (50 sites) outstanding to finalize the rate.

## artifacts/eyeball/ — human validation (the protocol record)
| File | Detail |
|---|---|
| `eyeball_batch3.txt` / `.json` | **OUTSTANDING** — 50-site sample of the 385 JS-rendered "no static signal" set, awaiting user U/F/D verdicts. Statistically defensible (13% of 385). |
| `needs_eyeball.txt` | Eyeball ledger — batches 1 & 2 with the prompts shown to the user. |
| `gc_triage.csv` | Per-site triage: url,plat,class,evidence. Includes user-confirmed verdicts (jaxon=HAS_UNITS, princeton=floorplan-only, on-site=RealPage, sussexmanor=AppFolio, etc.). |

## artifacts/analysis/ — investigation data
| File | Detail |
|---|---|
| `probe456_out.txt` | All-456 genuine-custom automated unit check summary: 69 HAS_UNITS / 385 NEEDS_EYEBALL / 2 DEAD (static-only — JS-rendered invisible). |
| `probe456_res.json` | Per-site result for all 456 (url, verdict, signal). |
| `gc_vendor_sized.txt` | Vendor-template cluster sizing of the 456: 206 template / 39 misfp-PMS / 209 unclassified. |
| `gc_vendor_res.json` | Per-site vendor signature for all 456. |
| `gc_sample.json` | The 24-site Chrome-MCP triage sample. |
| `domllm_ledger.json` | The 3,456 residual (NOT-genuine-Tier-1) fingerprint ledger — resumable; platform per site. |
| `tier34_pop.json` / `tier34_out.txt` | Full TIER-3/4 LLM+DOM "next focus" population (2,650) + cluster map. |
| `cohort_results.jsonl` | Per-cluster Tier-1 conversion measurements (resman/spherexx/realpage/entrata/funnel/…). |
| `completeness_out.txt` | apts247 per-property completeness vs API truth (8/9 complete). |
| `sc_verify2_out.txt` | securecafe completeness vs live availableunits.aspx (12/12, run==fresh re-parse — not truncating). |
| `partial_bucket.json` | The 149 "data-reached-but-floorplan/no-rent" partial sites by platform. |
| `none_diag.json` | NONE-bucket failure-cause diagnosis sample. |
| `sightmap_misroute.json` | The 249 sightmap→Entrata misrouted sites (iter-19 fix target). |
| `q1257.json` | 1,257 never-fed prod-SUCCESS quality audit rows (tier, units, real-unit-rent). |
| `never_fed_ids.txt` / `target1257.txt` | Coverage audit: ids never fed to any canary run / the 1,257 prod-SUCCESS subset. |
| `bespoke_targets.json` | Genuine-bespoke Chrome-target carve. |
| `probe842_results.json` / `probe604_results.json` / `probe295_results.json` | Earlier deep-probe pools (842 "other", 604 unit-via-LLM/DOM, 295 deficient prod-SUCCESS). |
| `prod0517_paths.txt` | GCS paths of the prod 2026-05-17 run shards (baseline source). |
| `verify_samples.json` | Per-cluster no-unit verification samples. |

## artifacts/cohorts/ — canary input cohorts
`cohort63.csv` (untested+broken), `cohort_apts247.csv` (223), `cohort_qualgap.csv`
(295 deficient prod-SUCCESS), `cohort_securecafe.csv` (1,400), `cohort_p2.csv`
(#2 CF-managed-challenge), `bigpool.csv` (1,384), `cohort_plan.json` (sequential
plan), and `cluster/` (per-platform cluster cohort CSVs: resman/spherexx/realpage/
entrata/funnel/appfolio/g5/onesite/rentcafe/sightmap/securecafe).

## artifacts/scripts/ — reproducibility
`probe456.py` (all-456 unit check), `probe295.py`, `tier34_enum.py`,
`gc_vendor_fp.py` (vendor sizing), `measure_cohort.py` (per-cohort Tier-1),
`sc_verify2.py`/`sc_complete.py` (securecafe completeness),
`completeness_check.py` (apts247 completeness). Re-run with
`PYTHONPATH=<repo-root> python3 <script>`.

## Open / gated
- **#2 re-baseline** running on iter-19 (securecafe/sightmap/apts247 + g5/appfolio regression guards) — locks the regression baseline.
- **#1** (generalizable per-floorplan-detail crawl+render+parse) — GATED on a clean #2 baseline + explicit user OK. It is also the only true all-456 per-site check (static proven insufficient: 69/456).
- **Provenance gap**: securecafe runs store no source URL — completeness only auditable via live re-derive (fix pending).
