#!/usr/bin/env python3
"""Project the July 31 cohort impact of the availability alias fix.

This is deliberately labelled a projection, not a post-fix canary result. The
July 31 formatter discarded ``availability_date`` before writing raw companions,
so the archived properties cannot be losslessly replayed. The strict projection
uses only the Knock/G5 signatures confirmed by source code plus six live probes;
the high-confidence projection adds the generic cluster validated on three live
sites. Both retain the audit's exact property/native-unit matching rules.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd


GOOD_COMPARISONS = {"exact", "sx_plus_one_day", "sx_minus_one_day"}


def _truthy(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().isin({"true", "1", "yes"})


def _integer(value: Any) -> int:
    return int(value) if pd.notna(value) else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matched", type=Path, required=True)
    parser.add_argument("--baseline-summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    matched = pd.read_csv(args.matched, dtype=str, keep_default_na=False)
    baseline = json.loads(args.baseline_summary.read_text(encoding="utf-8"))

    future = _truthy(matched["rp_future"])
    good = future & matched["comparison"].isin(GOOD_COMPARISONS)
    confirmed = future & _truthy(matched["confirmed_alias_default_signature"])
    generic = (
        future
        & _truthy(matched["probable_missing_key_or_default"])
        & matched["adapter_family"].eq("GENERIC_DOM")
        & matched["comparison"].eq("rp_future_to_capture_date")
    )

    strict_preserved = good | confirmed
    high_confidence_preserved = strict_preserved | generic
    future_rows = matched.loc[future].copy()
    future_rows["baseline_within_one_day"] = good.loc[future].to_numpy()
    future_rows["strict_formatter_alias_recovery"] = confirmed.loc[future].to_numpy()
    future_rows["high_confidence_generic_recovery"] = generic.loc[future].to_numpy()
    future_rows["strict_projected_preserved"] = strict_preserved.loc[future].to_numpy()
    future_rows["high_confidence_projected_preserved"] = (
        high_confidence_preserved.loc[future].to_numpy()
    )
    future_rows["strict_remaining"] = ~future_rows["strict_projected_preserved"]
    future_rows["high_confidence_remaining"] = ~future_rows[
        "high_confidence_projected_preserved"
    ]

    grouped = (
        future_rows.groupby("adapter_family", dropna=False)
        .agg(
            rp_future_units=("property_id", "size"),
            properties=("property_id", "nunique"),
            baseline_within_one_day=("baseline_within_one_day", "sum"),
            future_to_capture=(
                "comparison",
                lambda values: int((values == "rp_future_to_capture_date").sum()),
            ),
            strict_alias_recovery=("strict_formatter_alias_recovery", "sum"),
            generic_recovery=("high_confidence_generic_recovery", "sum"),
            strict_remaining=("strict_remaining", "sum"),
            high_confidence_remaining=("high_confidence_remaining", "sum"),
        )
        .reset_index()
        .sort_values(
            ["high_confidence_remaining", "rp_future_units"],
            ascending=[False, False],
        )
    )
    for column in grouped.columns:
        if column != "adapter_family":
            grouped[column] = grouped[column].map(_integer)

    denominator = int(future.sum())
    baseline_preserved = int(good.sum())
    strict_recovered = int(confirmed.sum())
    generic_recovered = int(generic.sum())
    strict_total = int(strict_preserved.sum())
    high_total = int(high_confidence_preserved.sum())

    summary = {
        "result_type": "local_projection_not_canary",
        "source_run": baseline["provenance"]["gcp_run"],
        "cohort_properties": baseline["scope"]["cohort_properties"],
        "exact_common_native_unit_keys": baseline["scope"][
            "exact_common_native_unit_keys"
        ],
        "rp_future_matched_units": denominator,
        "baseline": {
            "future_within_one_day_units": baseline_preserved,
            "future_preservation_rate": baseline_preserved / denominator,
            "future_to_capture_gap_units": int(
                baseline["availability"]["matched_rp_future_comparison_counts"][
                    "rp_future_to_capture_date"
                ]
            ),
        },
        "strict_confirmed_formatter_fix": {
            "recovered_units": strict_recovered,
            "recovered_properties": int(matched.loc[confirmed, "property_id"].nunique()),
            "projected_preserved_units": strict_total,
            "projected_future_preservation_rate": strict_total / denominator,
            "remaining_units": denominator - strict_total,
            "basis": "Knock + G5 code-path proof and 3 live probes per adapter",
        },
        "high_confidence_with_generic": {
            "additional_generic_units": generic_recovered,
            "additional_generic_properties": int(
                matched.loc[generic, "property_id"].nunique()
            ),
            "projected_preserved_units": high_total,
            "projected_future_preservation_rate": high_total / denominator,
            "remaining_units": denominator - high_total,
            "basis": "strict confirmed set plus generic code path and 3 live probes",
        },
        "limitations": [
            "Projection uses the exact July 31 matched native-unit base but is not a paid post-fix canary.",
            "Archived output cannot replay discarded adapter aliases because the old formatter also blanked raw provenance.",
            "Fresh source dates may differ from RP by one day; the audit treats exact and plus/minus one day as preserved.",
            "Properties without an RP-future oracle or exact common native unit remain outside this unit-level rate.",
        ],
    }

    (args.output_dir / "projection_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )
    grouped.to_csv(args.output_dir / "projection_by_adapter.csv", index=False)
    future_rows.to_csv(args.output_dir / "projection_unit_ledger.csv", index=False)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
