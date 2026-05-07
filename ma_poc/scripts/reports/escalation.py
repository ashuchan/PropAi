"""escalation_report.py — summarise fetch-tier escalation activity.

Reads the event ledger (data/runs/<latest>/events.jsonl) and the profile
store to produce a per-tier summary: escalation counts, promotion/demotion
counts, DLQ_PARK counts, and an estimated cost breakdown.

Usage:
    python -m ma_poc.scripts.escalation_report [--run-dir PATH]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path


def _latest_run_dir(data_dir: Path) -> Path | None:
    runs = sorted((data_dir / "runs").glob("*"), reverse=True)
    return runs[0] if runs else None


def _load_events(events_path: Path) -> list[dict]:
    events = []
    if not events_path.exists():
        return events
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def _summarise(events: list[dict]) -> dict:
    counts: dict[str, int] = defaultdict(int)
    escalations_per_tier: dict[str, int] = defaultdict(int)
    promotions: dict[str, int] = defaultdict(int)
    demotions: dict[str, int] = defaultdict(int)
    dlq_parks = 0
    probe_successes = 0
    probe_failures = 0

    for ev in events:
        kind = ev.get("kind", "")
        counts[kind] += 1

        if kind == "fetch.tier_escalated":
            tier = ev.get("tier", "UNKNOWN")
            escalations_per_tier[tier] += 1

        elif kind == "fetch.tier_persisted":
            tier = ev.get("new_floor", "UNKNOWN")
            promotions[tier] += 1

        elif kind == "fetch.tier_demoted":
            tier = ev.get("new_floor", "UNKNOWN")
            demotions[tier] += 1

        elif kind == "fetch.ladder_exhausted":
            dlq_parks += 1

        elif kind == "fetch.tier_probe_success":
            probe_successes += 1

        elif kind == "fetch.tier_probe_failed":
            probe_failures += 1

    return {
        "total_events": len(events),
        "escalations_per_tier": dict(escalations_per_tier),
        "promotions_to_tier": dict(promotions),
        "demotions_to_tier": dict(demotions),
        "dlq_parks": dlq_parks,
        "probe_successes": probe_successes,
        "probe_failures": probe_failures,
        "event_counts": dict(counts),
    }


def run(run_dir: Path | None = None) -> dict:
    data_dir = Path(__file__).resolve().parent.parent / "data"

    if run_dir is None:
        run_dir = _latest_run_dir(data_dir)

    if run_dir is None:
        print("No run directory found. Run the scraper first.", file=sys.stderr)
        return {}

    events_path = run_dir / "events.jsonl"
    events = _load_events(events_path)
    summary = _summarise(events)

    _print_report(summary, run_dir)
    return summary


def _print_report(summary: dict, run_dir: Path) -> None:
    print(f"\n{'='*60}")
    print(f"FETCH-TIER ESCALATION REPORT")
    print(f"Run dir: {run_dir}")
    print(f"{'='*60}")
    print(f"Total events processed : {summary['total_events']}")
    print(f"DLQ parks (exhausted)  : {summary['dlq_parks']}")
    print(f"Probe successes        : {summary['probe_successes']}")
    print(f"Probe failures         : {summary['probe_failures']}")

    print("\nEscalations per tier:")
    for tier, count in sorted(summary["escalations_per_tier"].items()):
        print(f"  {tier:<20} {count:>6}")

    print("\nPromotions to tier (floor raised):")
    for tier, count in sorted(summary["promotions_to_tier"].items()):
        print(f"  {tier:<20} {count:>6}")

    print("\nDemotions to tier (floor lowered):")
    for tier, count in sorted(summary["demotions_to_tier"].items()):
        print(f"  {tier:<20} {count:>6}")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch-tier escalation report")
    parser.add_argument("--run-dir", type=Path, default=None)
    args = parser.parse_args()
    run(run_dir=args.run_dir)
