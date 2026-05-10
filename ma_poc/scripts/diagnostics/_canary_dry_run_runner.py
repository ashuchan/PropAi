"""_canary_dry_run_runner.py — synthetic jugnu runner for canary smoke tests.

Reads a CSV of properties (same format as canary_input.csv) and writes a
synthetic ``events.jsonl`` into ``{data_dir}/runs/{run_date}/events.jsonl``
so the canary's compare + report phases can run end-to-end without launching
a real browser or hitting real URLs.

Every property gets a ``output.property_emitted`` event with:
- ``verdict``  — controlled by ``--verdict`` (default: SUCCESS)
- ``units``    — controlled by ``--units`` (default: 3)
- ``tier_used``  written as an ``extract.tier_won`` event (default: TIER_2_API_NARROW)

Usage (not meant for direct invocation — used by the canary via --dry-run-replay):
  python _canary_dry_run_runner.py \\
      --csv canary_input.csv \\
      --data-dir /tmp/canary_out \\
      --run-date 2026-05-10 \\
      [--verdict FAILED_NO_DATA] \\
      [--units 0] \\
      [--tier TIER_4_LLM_API]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def _emit(kind: str, property_id: str, run_id: str, extra: dict) -> str:
    """Serialise one event line."""
    return json.dumps(
        {
            "kind": kind,
            "property_id": property_id,
            "run_id": run_id,
            "ts": datetime.now(UTC).isoformat(),
            **extra,
        }
    )


def run_dry(
    csv_path: Path,
    data_dir: Path,
    run_date: str,
    verdict: str = "SUCCESS",
    units: int = 3,
    tier: str = "TIER_2_API_NARROW",
) -> int:
    """Write synthetic events for every property in csv_path.

    Args:
        csv_path: Input CSV (must have a ``property_id`` column).
        data_dir: Root data directory; events go to
            ``{data_dir}/runs/{run_date}/events.jsonl``.
        run_date: YYYY-MM-DD string.
        verdict: Verdict string for each ``output.property_emitted`` event.
        units: Unit count for each event.
        tier: Tier string for each ``extract.tier_won`` event.

    Returns:
        0 on success, 1 on error.
    """
    try:
        with csv_path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except OSError as exc:
        print(f"[dry_run] Cannot read {csv_path}: {exc}", file=sys.stderr)
        return 1

    events_dir = data_dir / "runs" / run_date
    events_dir.mkdir(parents=True, exist_ok=True)
    events_path = events_dir / "events.jsonl"

    run_id = f"dry-run-{run_date}"

    with events_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            pid = row.get("property_id", "").strip()
            if not pid:
                continue
            # tier_won event
            fh.write(
                _emit(
                    "extract.tier_won",
                    pid,
                    run_id,
                    {"tier_used": tier},
                )
            )
            fh.write("\n")
            # property_emitted event
            fh.write(
                _emit(
                    "output.property_emitted",
                    pid,
                    run_id,
                    {"verdict": verdict, "units": units},
                )
            )
            fh.write("\n")

    print(
        f"[dry_run] Wrote {len(rows)} synthetic property events → {events_path}",
        file=sys.stderr,
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Synthetic canary runner — writes fake events without real scraping."
    )
    p.add_argument("--csv", required=True, type=Path, metavar="PATH")
    p.add_argument("--data-dir", required=True, type=Path, metavar="PATH")
    p.add_argument("--run-date", required=True, metavar="YYYY-MM-DD")
    p.add_argument("--verdict", default="SUCCESS", metavar="VERDICT")
    p.add_argument("--units", type=int, default=3, metavar="N")
    p.add_argument(
        "--tier",
        default="TIER_2_API_NARROW",
        metavar="TIER",
        help="Tier string written into extract.tier_won events.",
    )
    # Accept (and ignore) flags forwarded by the canary subprocess invocation.
    p.add_argument("--force-scrape", action="store_true", default=False)
    p.add_argument("--limit", type=int, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return run_dry(
        csv_path=args.csv,
        data_dir=args.data_dir,
        run_date=args.run_date,
        verdict=args.verdict,
        units=args.units,
        tier=args.tier,
    )


if __name__ == "__main__":
    sys.exit(main())
