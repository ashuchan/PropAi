#!/usr/bin/env python3
"""Generate comprehensive analysis report for properties and units data.

Produces metrics on:
- 5-day success/failure trends
- Units data quality scores
- Diffs from yesterday
- System performance metrics

CLI flags:
    --no-email           Skip the email send. JSON is still written.
    --email-recipients   Comma-separated override for REPORT_RECIPIENTS.
    --dry-run            Render the email body to stdout, do not send.
"""

import argparse
import contextlib
import io
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
import statistics

_MA_POC_ROOT = Path(__file__).resolve().parent.parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_MA_POC_ROOT, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from data_provider.factory import get_data_provider
from data_provider.sql.models import PropertyRow, UnitRow, ScrapeProfileRow

def get_date_range(days: int = 5) -> list[str]:
    """Get list of dates for the last N days in YYYY-MM-DD format."""
    today = datetime.now().date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days)][::-1]

def analyze_scrape_data(provider, run_date: str) -> dict:
    """Analyze scrape data for a specific run date."""
    profile_rows = provider.get_scrape_profiles(run_date=run_date)

    total = len(profile_rows)
    if total == 0:
        return {
            "date": run_date,
            "total": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "success_rate": 0,
            "failure_rate": 0,
            "skip_rate": 0
        }

    success = sum(1 for p in profile_rows if p.scrape_success)
    failed = sum(1 for p in profile_rows if not p.scrape_success)
    skipped = sum(1 for p in profile_rows if p.skip_reason is not None)

    return {
        "date": run_date,
        "total": total,
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "success_rate": round(100 * success / total, 2) if total > 0 else 0,
        "failure_rate": round(100 * failed / total, 2) if total > 0 else 0,
        "skip_rate": round(100 * skipped / total, 2) if total > 0 else 0
    }

def analyze_units_quality(provider, run_date: str) -> dict:
    """Analyze units data quality for a specific run date."""
    units = provider.get_units(run_date=run_date)

    if not units:
        return {
            "date": run_date,
            "total_units": 0,
            "with_rent": 0,
            "with_availability": 0,
            "with_sqft": 0,
            "avg_rent": 0,
            "median_rent": 0,
            "pct_with_rent": 0,
            "pct_with_availability": 0,
            "pct_with_sqft": 0
        }

    rents = [u.asking_rent for u in units if u.asking_rent is not None]
    with_rent = len(rents)
    with_availability = sum(1 for u in units if u.availability_date is not None)
    with_sqft = sum(1 for u in units if u.sqft is not None)

    avg_rent = statistics.mean(rents) if rents else 0
    median_rent = statistics.median(rents) if rents else 0

    return {
        "date": run_date,
        "total_units": len(units),
        "with_rent": with_rent,
        "with_availability": with_availability,
        "with_sqft": with_sqft,
        "avg_rent": round(avg_rent, 2),
        "median_rent": round(median_rent, 2),
        "pct_with_rent": round(100 * with_rent / len(units), 2) if units else 0,
        "pct_with_availability": round(100 * with_availability / len(units), 2) if units else 0,
        "pct_with_sqft": round(100 * with_sqft / len(units), 2) if units else 0
    }

def analyze_unit_consistency(provider, date_range: list[str]) -> dict:
    """Analyze unit consistency across 3 and 2 day windows."""
    if len(date_range) < 3:
        return {"error": "Insufficient data for consistency analysis"}

    # Get units for all dates
    all_units_by_date = {}
    for date in date_range[-3:]:  # Last 3 days
        units = provider.get_units(run_date=date)
        unit_dict = {f"{u.property_id}_{u.unit_number}": u for u in units}
        all_units_by_date[date] = unit_dict

    if not all_units_by_date:
        return {"error": "No unit data available"}

    last_date = date_range[-1]
    last_2_dates = date_range[-2:]

    # 3-day consistency: units seen in all 3 days
    three_day_units = set(all_units_by_date[date_range[-3]].keys())
    for date in date_range[-2:]:
        three_day_units &= set(all_units_by_date[date].keys())

    # 2-day consistency: units seen in last 2 days
    two_day_units = set(all_units_by_date[last_2_dates[0]].keys())
    two_day_units &= set(all_units_by_date[last_2_dates[1]].keys())

    total_today = len(all_units_by_date.get(last_date, {}))

    return {
        "3_day_consistent": len(three_day_units),
        "2_day_consistent": len(two_day_units),
        "pct_3_day_consistent": round(100 * len(three_day_units) / total_today, 2) if total_today > 0 else 0,
        "pct_2_day_consistent": round(100 * len(two_day_units) / total_today, 2) if total_today > 0 else 0
    }

def analyze_daily_diff(provider, yesterday: str, today: str) -> dict:
    """Analyze differences between yesterday and today."""
    yesterday_props = provider.get_properties(run_date=yesterday)
    today_props = provider.get_properties(run_date=today)

    yesterday_ids = {p.property_id for p in yesterday_props}
    today_ids = {p.property_id for p in today_props}

    new_props = today_ids - yesterday_ids
    missed_props = yesterday_ids - today_ids

    # Rent changes
    yesterday_rents = {p.property_id: p for p in yesterday_props}
    today_rents = {p.property_id: p for p in today_props}

    rent_increases = []
    rent_decreases = []

    for prop_id in today_ids & yesterday_ids:
        yesterday_units = [u for u in provider.get_units(run_date=yesterday)
                          if u.property_id == prop_id and u.asking_rent]
        today_units = [u for u in provider.get_units(run_date=today)
                      if u.property_id == prop_id and u.asking_rent]

        if yesterday_units and today_units:
            yesterday_avg = statistics.mean(u.asking_rent for u in yesterday_units)
            today_avg = statistics.mean(u.asking_rent for u in today_units)

            if today_avg > yesterday_avg:
                rent_increases.append(prop_id)
            elif today_avg < yesterday_avg:
                rent_decreases.append(prop_id)

    return {
        "new_properties": len(new_props),
        "properties_new_ids": list(new_props)[:10],  # First 10
        "missed_properties": len(missed_props),
        "properties_missed_ids": list(missed_props)[:10],  # First 10
        "rent_increases": len(rent_increases),
        "rent_decreases": len(rent_decreases),
        "rent_unchanged": len(today_ids & yesterday_ids) - len(rent_increases) - len(rent_decreases)
    }

def analyze_performance(provider, date_range: list[str]) -> dict:
    """Analyze system performance metrics."""
    perf_data = []

    for date in date_range:
        profiles = provider.get_scrape_profiles(run_date=date)
        if not profiles:
            continue

        runtimes = [p.runtime_seconds for p in profiles if p.runtime_seconds]

        if runtimes:
            perf_data.append({
                "date": date,
                "avg_runtime_per_property": round(statistics.mean(runtimes), 2),
                "median_runtime_per_property": round(statistics.median(runtimes), 2),
                "p95_runtime": round(sorted(runtimes)[int(0.95 * len(runtimes))], 2) if len(runtimes) > 1 else runtimes[0],
                "total_runtime": round(sum(runtimes), 2),
                "properties_count": len(profiles)
            })

    return {
        "by_date": perf_data,
        "note": "Actual shard metrics require infrastructure telemetry"
    }

def _build_email_summary(scrape_analysis: list[dict], diffs: dict) -> str:
    """One-line TL;DR from the latest day's scrape stats + day-over-day diff.

    Used as the email summary callout. Anchors on the most recent
    ``scrape_analysis`` entry so the summary reflects the run that
    triggered this report, not an averaged window.
    """
    if not scrape_analysis:
        return "Analysis report — no scrape data found in the last 5 days."
    today = scrape_analysis[-1]
    parts = [
        f"{today['date']}: {today['success']}/{today['total']} succeeded "
        f"({today['success_rate']:.1f}%)"
    ]
    if today["failed"]:
        parts.append(f"{today['failed']} failed")
    if today["skipped"]:
        parts.append(f"{today['skipped']} skipped")
    if diffs.get("rent_increases") or diffs.get("rent_decreases"):
        parts.append(
            f"{diffs.get('rent_increases', 0)}↑/{diffs.get('rent_decreases', 0)}↓ rents"
        )
    new_props = diffs.get("new_properties", 0)
    missed = diffs.get("missed_properties", 0)
    if new_props or missed:
        parts.append(f"{new_props} new / {missed} missed properties")
    return ". ".join(parts) + "."


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-email", action="store_true",
                        help="Skip the email send. JSON is still written.")
    parser.add_argument("--email-recipients", default=None,
                        help="Comma-separated override of REPORT_RECIPIENTS.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render the email body to stdout but do not send.")
    args = parser.parse_args()

    # Capture every print() so the same text body can be both shown to the
    # operator (Cloud Run logs) and shipped in the email. Tee to real
    # stdout while we're at it so the existing log experience is unchanged.
    captured = io.StringIO()

    class _Tee:
        def __init__(self, *streams: io.TextIOBase) -> None:
            self._streams = streams

        def write(self, s: str) -> int:
            for st in self._streams:
                st.write(s)
            return len(s)

        def flush(self) -> None:
            for st in self._streams:
                st.flush()

    real_stdout = sys.stdout
    tee = _Tee(real_stdout, captured)

    with contextlib.redirect_stdout(tee):
        return _run(args, captured)


def _run(args: argparse.Namespace, captured: io.StringIO) -> int:
    provider = get_data_provider()

    today = datetime.now().date().isoformat()
    yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
    date_range = get_date_range(5)

    print("=" * 80)
    print("DAILY ANALYTICS REPORT")
    print("=" * 80)
    print(f"Generated: {datetime.now().isoformat()}")
    print(f"Analysis period: {date_range[0]} to {date_range[-1]}")
    print()

    # 1. 5-day success/failure trend
    print("1. PROPERTIES SUCCESS/FAILURE - LAST 5 DAYS")
    print("-" * 80)
    scrape_analysis = []
    for date in date_range:
        analysis = analyze_scrape_data(provider, date)
        scrape_analysis.append(analysis)
        print(f"{analysis['date']}: {analysis['success']:4d}/{analysis['total']:4d} success "
              f"({analysis['success_rate']:5.1f}%) | "
              f"{analysis['failed']:4d} failed ({analysis['failure_rate']:5.1f}%) | "
              f"{analysis['skipped']:4d} skipped ({analysis['skip_rate']:5.1f}%)")
    print()

    # 2. Units data quality
    print("2. UNITS DATA QUALITY - LAST 5 DAYS")
    print("-" * 80)
    units_quality = []
    for date in date_range:
        quality = analyze_units_quality(provider, date)
        units_quality.append(quality)
        print(f"{quality['date']}: {quality['total_units']:6d} units | "
              f"Rent: {quality['pct_with_rent']:5.1f}% ({quality['with_rent']:6d}) "
              f"Avg: ${quality['avg_rent']:7.0f} Median: ${quality['median_rent']:7.0f} | "
              f"Avail: {quality['pct_with_availability']:5.1f}% | "
              f"SqFt: {quality['pct_with_sqft']:5.1f}%")
    print()

    # 3. Unit consistency across days
    print("3. UNITS CONSISTENCY & CARRYFORWARD")
    print("-" * 80)
    consistency = analyze_unit_consistency(provider, date_range)
    if "error" not in consistency:
        print(f"Units seen consistently (3 days):     {consistency['3_day_consistent']:6d} "
              f"({consistency['pct_3_day_consistent']:5.1f}%)")
        print(f"Units seen consistently (2 days):     {consistency['2_day_consistent']:6d} "
              f"({consistency['pct_2_day_consistent']:5.1f}%)")
    else:
        print(f"Note: {consistency['error']}")
    print()

    # 4. Diffs from yesterday
    print("4. CHANGES FROM YESTERDAY")
    print("-" * 80)
    diffs = analyze_daily_diff(provider, yesterday, today)
    print(f"New properties today:       {diffs['new_properties']:4d}")
    if diffs['properties_new_ids']:
        print(f"  Sample IDs: {', '.join(diffs['properties_new_ids'][:5])}")
    print(f"Properties missed today:    {diffs['missed_properties']:4d}")
    if diffs['properties_missed_ids']:
        print(f"  Sample IDs: {', '.join(diffs['properties_missed_ids'][:5])}")
    print(f"Rent increases:             {diffs['rent_increases']:4d}")
    print(f"Rent decreases:             {diffs['rent_decreases']:4d}")
    print(f"Rent unchanged:             {diffs['rent_unchanged']:4d}")
    print()

    # 5. Performance
    print("5. SYSTEM PERFORMANCE")
    print("-" * 80)
    perf = analyze_performance(provider, date_range)
    if "by_date" in perf and perf["by_date"]:
        for p in perf["by_date"]:
            print(f"{p['date']}: Avg {p['avg_runtime_per_property']:6.2f}s | "
                  f"Median {p['median_runtime_per_property']:6.2f}s | "
                  f"P95 {p['p95_runtime']:6.2f}s | "
                  f"Total {p['total_runtime']:8.1f}s ({p['properties_count']} props)")

        if date_range[-1] in [p['date'] for p in perf['by_date']]:
            today_perf = next(p for p in perf['by_date'] if p['date'] == date_range[-1])
            print(f"\n  Estimated runtimes with different parallelism:")
            print(f"    With 10 shards:  ~{round(today_perf['total_runtime'] / 10, 1)}s")
            print(f"    With 20 shards:  ~{round(today_perf['total_runtime'] / 20, 1)}s")
    print(f"\n  Note: {perf['note']}")
    print()

    print("=" * 80)
    print("END OF REPORT")
    print("=" * 80)

    # Save JSON output
    report_data = {
        "generated_at": datetime.now().isoformat(),
        "period": {"start": date_range[0], "end": date_range[-1]},
        "scrape_trends": scrape_analysis,
        "units_quality": units_quality,
        "consistency": consistency,
        "daily_diff": diffs,
        "performance": perf
    }

    output_path = _MA_POC_ROOT / "data" / "runs" / today / "analysis_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report_data, f, indent=2)

    print(f"\nJSON report saved to: {output_path}")

    if args.no_email:
        return 0

    from scripts.email.report import email_report

    summary = _build_email_summary(scrape_analysis, diffs)
    recipients = (
        [r.strip() for r in args.email_recipients.split(",") if r.strip()]
        if args.email_recipients
        else None
    )
    result = email_report(
        subject=f"PropAi 5-Day Analytics — {today}",
        summary=summary,
        body_text=captured.getvalue(),
        attachments=[output_path],
        recipients=recipients,
        dry_run=args.dry_run,
    )
    if result.get("isError"):
        print(f"\nemail send failed: {result.get('content')}", file=sys.stderr)
        return 1
    print(f"\nemail sent: {result.get('content') or '(empty)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
