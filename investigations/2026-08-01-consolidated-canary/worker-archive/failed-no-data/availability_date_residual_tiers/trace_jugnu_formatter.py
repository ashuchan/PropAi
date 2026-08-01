#!/usr/bin/env python3
"""Replay audited adapter rows through the production Jugnu unit formatter."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


REPO = Path("/Users/ankur/PropAi-codex-availability-date")
INPUT = Path(
    "/private/tmp/propai-fnd-vBkmT9/availability_date_residual_tiers/"
    "with_hyperbrowser/current_live_unit_evidence.csv"
)
OUTPUT = INPUT.parent / "jugnu_formatter_trace.csv"
SUMMARY = INPUT.parent / "jugnu_formatter_trace_summary.json"

sys.path.insert(0, str(REPO))

from ma_poc.scripts.runners.jugnu import _format_v2_unit  # noqa: E402


def as_bool(value: bool) -> str:
    return "true" if value else "false"


def main() -> None:
    with INPUT.open(encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))

    out: list[dict[str, str]] = []
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    for row in source_rows:
        adapter_row = json.loads(row.get("adapter_row_json") or "{}")
        formatter: dict = {}
        error = ""
        if adapter_row:
            try:
                captured = datetime.fromisoformat(
                    row["capture_timestamp_utc"].replace("Z", "+00:00")
                )
                has_anchor = bool(
                    str(
                        adapter_row.get("unit_id")
                        or adapter_row.get("unit_number")
                        or ""
                    ).strip()
                )
                formatter = _format_v2_unit(
                    dict(adapter_row),
                    captured,
                    property_id=row["property_id"],
                    property_plan_level=not has_anchor,
                )
            except Exception as exc:  # evidence must retain trace errors
                error = f"{type(exc).__name__}: {str(exc)[:240]}"

        normalized = row.get("normalized_availability_date") or ""
        jugnu_date = str(formatter.get("available_date") or "")
        is_future = row.get("availability_semantic") == "explicit_future"
        preserved = is_future and jugnu_date == normalized
        category = row["category"]
        if is_future:
            by_category[category]["future_rows"] += 1
            if preserved:
                by_category[category]["preserved_rows"] += 1
            else:
                by_category[category]["missed_rows"] += 1
        if error:
            by_category[category]["trace_errors"] += 1

        out.append(
            {
                "category": category,
                "property_id": row["property_id"],
                "source_row_id": row["source_row_id"],
                "source_semantic": row["availability_semantic"],
                "source_raw_availability": row["raw_availability_value"],
                "source_normalized_availability_date": normalized,
                "adapter_row_present": as_bool(bool(adapter_row)),
                "adapter_availability_key": row["adapter_availability_key"],
                "adapter_availability_value": row["adapter_availability_value"],
                "jugnu_available_date": jugnu_date,
                "jugnu_availability_date_provenance": str(
                    formatter.get("availability_date_provenance") or ""
                ),
                "explicit_future_preserved_by_jugnu": as_bool(preserved),
                "jugnu_trace_error": error,
                "source_evidence_url": row["evidence_url"],
            }
        )

    with OUTPUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(out[0]))
        writer.writeheader()
        writer.writerows(out)

    summary = {
        "result_type": "current_local_production_jugnu_formatter_replay_not_canary",
        "input": str(INPUT),
        "source_rows": len(source_rows),
        "adapter_rows_replayed": sum(
            row["adapter_row_present"] == "true" for row in out
        ),
        "explicit_future_rows": sum(
            row["source_semantic"] == "explicit_future" for row in out
        ),
        "explicit_future_preserved_by_jugnu_rows": sum(
            row["explicit_future_preserved_by_jugnu"] == "true" for row in out
        ),
        "trace_errors": sum(bool(row["jugnu_trace_error"]) for row in out),
        "by_category": {
            category: dict(counter)
            for category, counter in sorted(by_category.items())
        },
        "guardrails": {
            "repository_edits": False,
            "llm_enabled": False,
            "paid_canary": False,
            "captcha_solving": False,
        },
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
