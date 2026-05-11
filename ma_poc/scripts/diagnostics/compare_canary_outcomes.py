"""Compare canary outcomes against cloud-run baseline + expected verdicts.

Reads the canary's properties.json + events.jsonl, joins against the
canary_input.csv (cloud baseline + basket label), and emits a per-property
verdict comparison. Use this when ``local_canary.py``'s built-in compare
phase mis-locates the events file (e.g. schema-version prefix).
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def main(canary_dir: Path) -> int:
    input_csv = canary_dir / "canary_input.csv"
    events_path = canary_dir / "v2" / "runs" / "2026-05-11" / "events.jsonl"
    props_path = canary_dir / "v2" / "runs" / "2026-05-11" / "properties.json"

    if not input_csv.exists():
        print(f"missing: {input_csv}")
        return 2
    if not events_path.exists():
        print(f"missing: {events_path}")
        return 2

    cloud_rows: dict[str, dict] = {}
    with input_csv.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cloud_rows[row["property_id"]] = row

    # Verdict per property from events
    verdict_by_pid: dict[str, str] = {}
    units_by_pid: dict[str, int] = {}
    tier_by_pid: dict[str, str] = {}
    with events_path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = str(evt.get("property_id", "") or "")
            kind = evt.get("kind", "")
            if kind == "output.property_emitted":
                verdict_by_pid[pid] = evt.get("verdict", "?")
                units_by_pid[pid] = int(evt.get("units", 0))
            elif kind == "extract.tier_won":
                tier = evt.get("tier_used") or ""
                if tier:
                    tier_by_pid[pid] = tier

    # Plan-level counts from properties.json
    plan_count_by_pid: dict[str, int] = {}
    if props_path.exists():
        try:
            props_data = json.loads(props_path.read_text(encoding="utf-8"))
            for p in props_data if isinstance(props_data, list) else []:
                pid = str(p.get("apartment_id", "") or "")
                plan_summaries = p.get("plan_summaries") or []
                plan_count_by_pid[pid] = len(plan_summaries) if isinstance(plan_summaries, list) else 0
        except (json.JSONDecodeError, OSError):
            pass

    # Render comparison
    print(f"# Local Canary — live-fetch results ({canary_dir.name})")
    print()
    print(f"**Run dir:** `{canary_dir}`")
    print(f"**Properties tested:** {len(cloud_rows)}")
    print()
    print("## Per-property delta")
    print()
    print("| property_id | name (domain) | basket | cloud (cloud_units) → canary | canary tier | canary units / plans | status |")
    print("|---|---|---|---|---|---|---|")

    n_improved = 0
    n_regressed = 0
    n_demoted_as_expected = 0
    n_unchanged_ok = 0
    n_unchanged_fail = 0
    n_timeout = 0

    SUCCESS_VERDICTS = {"SUCCESS", "SUCCESS_PLAN_LEVEL", "CARRY_FORWARD"}

    for pid, row in cloud_rows.items():
        cloud_verdict = row.get("verdict", "?")
        cloud_units = row.get("cloud_units") or row.get("units") or "0"
        basket = row.get("basket", "FAILURE")
        domain = row.get("domain") or ""
        canary_verdict = verdict_by_pid.get(pid, "TIMEOUT_IN_CANARY")
        canary_units = units_by_pid.get(pid, 0)
        canary_plans = plan_count_by_pid.get(pid, 0)
        canary_tier = tier_by_pid.get(pid, "")

        cloud_ok = cloud_verdict in SUCCESS_VERDICTS
        canary_ok = canary_verdict in SUCCESS_VERDICTS

        if canary_verdict == "TIMEOUT_IN_CANARY":
            status = "TIMEOUT"
            n_timeout += 1
        elif basket == "EXPECTED_DEMOTION":
            # Cloud said SUCCESS with junk units; canary should DEMOTE
            if not canary_ok or canary_units < int(cloud_units):
                status = "DEMOTED_AS_EXPECTED"
                n_demoted_as_expected += 1
            else:
                status = "REGRESSED_FAKE_PRESERVED"
                n_regressed += 1
        elif basket == "DEAD_URL_CANDIDATE":
            if canary_verdict == "DEAD_URL":
                status = "IMPROVED"
                n_improved += 1
            elif not canary_ok:
                status = "UNCHANGED_FAIL"
                n_unchanged_fail += 1
            else:
                status = "REGRESSED"
                n_regressed += 1
        elif basket == "REAL_FIX":
            if canary_ok and canary_units > 0:
                status = "IMPROVED"
                n_improved += 1
            elif cloud_ok and not canary_ok:
                status = "REGRESSED"
                n_regressed += 1
            else:
                status = "UNCHANGED"
                n_unchanged_fail += 1
        else:
            # FAILURE basket: was FAILED; expect IMPROVED or UNCHANGED_FAIL
            if canary_ok:
                status = "IMPROVED"
                n_improved += 1
            else:
                status = "UNCHANGED_FAIL"
                n_unchanged_fail += 1

        print(
            f"| {pid} | {domain} | {basket} | "
            f"`{cloud_verdict}` ({cloud_units}u) → `{canary_verdict}` | "
            f"`{canary_tier}` | {canary_units}u / {canary_plans}p | **{status}** |"
        )

    print()
    print("## Summary")
    print()
    print("```")
    print(f"  IMPROVED:                 {n_improved:4d}")
    print(f"  DEMOTED_AS_EXPECTED:      {n_demoted_as_expected:4d}")
    print(f"  UNCHANGED_OK:             {n_unchanged_ok:4d}")
    print(f"  UNCHANGED_FAIL:           {n_unchanged_fail:4d}")
    print(f"  REGRESSED:                {n_regressed:4d}")
    print(f"  TIMEOUT:                  {n_timeout:4d}")
    print("```")
    print()
    gate = "PASS" if n_regressed == 0 else "FAIL"
    print(f"**Gate (REGRESSED == 0):** **{gate}**")
    return 0 if n_regressed == 0 else 1


if __name__ == "__main__":
    canary_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "data/canary/local_runs/2026-05-12_real_canary")
    sys.exit(main(canary_dir))
