# Canary harness — May-13 API-tier port

The two scripts in this directory + [`scripts/diagnostics/canary_diff.py`](../../scripts/diagnostics/canary_diff.py) are the merge gate for the May-13 API-tier port. Their behaviour follows §5 of [`ma_poc/docs/MAY13_API_TIER_PORT_PLAN.md`](../../docs/MAY13_API_TIER_PORT_PLAN.md).

## Quickstart

```bash
# 1. Build stratified CSVs from a recent cloud-run report.
#    Pass --report-dir newest first; rows merge keyed by property_id.
python ma_poc/tests/canary/build_canary_csv.py \
    --report-dir ma_poc/data/reports/cloud_run_2026-05-19 \
    --report-dir ma_poc/data/reports/cloud_run_2026-05-18 \
    --strict

# 2. Capture baseline (run against current main *before* any port commit).
python ma_poc/scripts/runners/jugnu.py \
    --csv ma_poc/tests/canary/canary_50.csv \
    --output data/runs/baseline_50/

# 3. Run analyzer to mint baseline summary.json + per-PID CSVs.
python ma_poc/scripts/diagnostics/analyze_cloud_run.py --date <baseline-date>

# 4. Repeat 2–3 on the port branch to get candidate metrics.

# 5. Diff. Exits 0 (gates green) or 1 (one or more gates failed).
python ma_poc/scripts/diagnostics/canary_diff.py \
    --baseline ma_poc/data/reports/cloud_run_<baseline-date> \
    --candidate ma_poc/data/reports/cloud_run_<candidate-date> \
    --out /tmp/canary_diff.json
```

## Stratification rules

`build_canary_csv.py` declares one [`Stratum`](build_canary_csv.py) per row of §5.2's table. Each rule has:
- a target count for `canary_500.csv` and `canary_50.csv`,
- a predicate over `PropertyRow` (drawn from analyzer-output CSVs),
- a free-form note of which commit(s) it validates.

Strata are applied in declared order. The known-SUCCESS regression-watch bucket is first so its 150 rows are reserved before any failure bucket draws from the same pool. Failure buckets follow yield magnitude. Within a bucket, sampling is seeded (`random.Random(seed)`); same seed + same source data ⇒ identical CSV.

When a bucket comes up short of its target, `--strict` exits non-zero. The plan says we'd rather fail loud than ship a degraded mix where a regression in a thin bucket goes undetected.

## What the diff measures

| Gate | Threshold | Source |
|---|---|---|
| Total unit yield | candidate ≥ baseline + 5% | summed `units` from `successes.csv` |
| Tier 1+2 share | candidate ≥ baseline | `summary.json::tier_distribution` |
| SUCCESS→FAILED regressions | ≤ 0.5% of known-SUCCESS bucket | per-PID join across both runs |
| Per-PMS unit yield (≥50 base units) | every PMS ≥ baseline – 5% | grouped from `successes.csv` |
| RealPage OLL label coverage | ≥30 (PASS), ≥20 (WARN), 0 (FAIL when bucket=0) | `summary.json::tier_distribution["TIER_1_API_REALPAGE_OLL"]` |
| New adapter tier labels | ≥1 of the expected new `TIER_1_API_*` set | tier_distribution key diff |
| LLM cost total | ≤ +20% PASS, ≤ +50% WARN, > +50% FAIL | `summary.json::llm_cost_total` — Tier-1 ports should *reduce* spend |
| Hard unit drop on shared SUCCESS | zero PIDs may lose ≥20% silently | per-PID join, baseline-success ∩ candidate-success |

## Determinism

- `build_canary_csv.py --seed 2026` produces identical CSVs given identical source artifacts. The default seed is committed for repeatability.
- `canary_diff.py` does not sample anything; pure aggregation.
- Neither script writes to git-tracked paths (only `out_dir`).

## Failure protocol

When `canary_diff.py` exits 1:
1. Open the `### Regressions` and `### Hard unit-count drops` sections of the markdown table.
2. Map each PID back to the commit-group that ran since the last quick canary.
3. Fix in a new commit on the PR branch (never amend). Re-run the quick canary on the same 50-PID CSV. Iterate until green.

The 500-PID full canary runs only after every commit-group's quick canary is green. If the 500 reveals regressions the 50 missed, same protocol — patch on PR branch, re-run.

## Files

- [`build_canary_csv.py`](build_canary_csv.py) — stratifier
- [`canary_500.csv`](canary_500.csv), [`canary_50.csv`](canary_50.csv) — generated (gitignored)
- [`scripts/diagnostics/canary_diff.py`](../../scripts/diagnostics/canary_diff.py) — merge gate
- [`MAY13_API_TIER_PORT_PLAN.md`](../../docs/MAY13_API_TIER_PORT_PLAN.md) — the plan this implements
