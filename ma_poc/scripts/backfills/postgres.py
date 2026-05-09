"""Backfill the Postgres (or any SQL) provider from existing FS data.

Reads everything under the current DATA_DIR / CONFIG_DIR via
`FileSystemDataProvider` and writes it through a target SQL provider
(`postgres` by default — override with --target sqlite|postgres).

Idempotent: re-running replaces per-run artifacts in place, upserts on all
identity keys, and skips duplicates where the SQL schema enforces PK
uniqueness (e.g. scrape_events by event_id).

Usage:
    python scripts/backfills/postgres.py                             # defaults
    python scripts/backfills/postgres.py --target sqlite
    python scripts/backfills/postgres.py --only runs,state
    python scripts/backfills/postgres.py --run-dates 2026-04-14,2026-04-19
    python scripts/backfills/postgres.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_MA_POC_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

from data_provider import (  # noqa: E402
    DataProvider,
    FileSystemDataProvider,
    PostgresDataProvider,
    SqliteDataProvider,
)

log = logging.getLogger("backfill_pg")


SECTIONS = ("state", "profiles", "events", "runs", "extractions")


def _build_source() -> FileSystemDataProvider:
    import os

    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    config_dir = Path(os.getenv("CONFIG_DIR", "./config"))
    if not data_dir.is_absolute():
        data_dir = (_MA_POC_ROOT / data_dir).resolve()
    if not config_dir.is_absolute():
        config_dir = (_MA_POC_ROOT / config_dir).resolve()
    return FileSystemDataProvider(base_dir=data_dir, config_dir=config_dir)


def _build_target(kind: str, url: str | None) -> DataProvider:
    if kind == "postgres":
        return PostgresDataProvider(url=url)
    if kind == "sqlite":
        return SqliteDataProvider(url=url)
    raise SystemExit(f"--target must be postgres|sqlite, got {kind!r}")


# ── Section copiers ──────────────────────────────────────────────────────────


def copy_state(src: DataProvider, dst: DataProvider, dry_run: bool) -> None:
    ids = sorted(src.property_state.all_canonical_ids())
    log.info("state: %d canonical_ids to copy", len(ids))
    if dry_run:
        return
    for i, cid in enumerate(ids, 1):
        entry = src.property_state.get(cid)
        if entry is None:
            continue
        snap = entry.model_dump(exclude={"canonical_id"})
        # Use the entry's own last_seen_at (date portion) as the run_date so
        # the SQL row carries the same provenance as the FS row.
        seen_at_date = (entry.last_seen_at or "")[:10]
        run_date = seen_at_date or entry.first_seen_date or "backfill"
        dst.property_state.upsert(cid, snap, run_date)

        units = src.unit_state.get_units(cid)
        if units:
            # upsert_units expects today_units dicts with unit_id keys —
            # emulate by feeding every unit as "today" at the last_seen_date.
            units_list = [u.model_dump() for u in units.values()]
            dst.unit_state.upsert_units(cid, units_list, run_date)

        if i % 50 == 0:
            log.info("state: %d/%d", i, len(ids))


def copy_profiles(src: DataProvider, dst: DataProvider, dry_run: bool) -> None:
    ids = src.profiles.list_ids()
    log.info("profiles: %d profiles to copy", len(ids))
    if dry_run:
        return
    for i, cid in enumerate(ids, 1):
        prof = src.profiles.get(cid)
        if prof is None:
            continue
        dst.profiles.put(prof)
        if i % 100 == 0:
            log.info("profiles: %d/%d", i, len(ids))


def copy_events(src: DataProvider, dst: DataProvider, dry_run: bool) -> None:
    events = list(src.scrape_events.read_all())
    log.info("events: %d scrape_events to copy", len(events))
    if dry_run:
        return
    for i, ev in enumerate(events, 1):
        dst.scrape_events.append(ev)
        if i % 500 == 0:
            log.info("events: %d/%d", i, len(events))


def copy_runs(
    src: DataProvider,
    dst: DataProvider,
    dry_run: bool,
    only_dates: set[str] | None,
) -> None:
    all_dates = src.runs.list_runs()
    dates = [d for d in all_dates if not only_dates or d in only_dates]
    log.info("runs: %d run_dates to copy (of %d available)", len(dates), len(all_dates))
    if dry_run:
        return
    for rd in dates:
        t0 = time.time()
        props = src.runs.read_properties(rd)
        if props:
            dst.runs.write_properties(rd, props)
        report = src.runs.read_report(rd)
        if report is not None:
            dst.runs.write_report(rd, report)
        issues = list(src.runs.read_issues(rd))
        for issue in issues:
            dst.runs.append_issue(rd, issue)
        ledger = list(src.runs.read_ledger(rd))
        for entry in ledger:
            dst.runs.append_ledger_entry(rd, entry)
        log.info(
            "runs: %s — %d properties, %d issues, %d ledger rows (%.1fs)",
            rd,
            len(props),
            len(issues),
            len(ledger),
            time.time() - t0,
        )


def copy_extractions(
    src: DataProvider,
    dst: DataProvider,
    dry_run: bool,
    only_dates: set[str] | None,
) -> None:
    """FS layout stores extraction results under extraction_output/{pid}/{date}.json.

    Since there's no per-store `list_all()`, we iterate runs × canonical_ids.
    This is a best-effort copy; missing files are skipped silently.
    """
    dates = [d for d in src.runs.list_runs() if not only_dates or d in only_dates]
    ids = src.property_state.all_canonical_ids()
    log.info("extractions: scanning %d runs × %d properties", len(dates), len(ids))
    if dry_run:
        return
    copied = skipped = 0
    for rd in dates:
        for cid in ids:
            res = src.extraction_results.read(rd, cid)
            if res is None:
                skipped += 1
                continue
            dst.extraction_results.write(rd, res)
            copied += 1
    log.info("extractions: %d copied, %d missing", copied, skipped)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Backfill SQL provider from FS data.")
    ap.add_argument("--target", choices=("postgres", "sqlite"), default="postgres")
    ap.add_argument("--url", default=None, help="DATABASE_URL override")
    ap.add_argument(
        "--only",
        default=",".join(SECTIONS),
        help=f"Comma-separated section list (default: all). Options: {SECTIONS}",
    )
    ap.add_argument(
        "--run-dates",
        default=None,
        help="Comma-separated run_dates; restricts runs+extractions sections",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sections = {s.strip() for s in args.only.split(",") if s.strip()}
    unknown = sections - set(SECTIONS)
    if unknown:
        log.error("Unknown sections: %s (valid: %s)", unknown, SECTIONS)
        return 2

    only_dates: set[str] | None = None
    if args.run_dates:
        only_dates = {d.strip() for d in args.run_dates.split(",") if d.strip()}

    log.info(
        "Backfill %s→%s  sections=%s  dates=%s  dry_run=%s",
        "filesystem",
        args.target,
        sorted(sections),
        sorted(only_dates) if only_dates else "all",
        args.dry_run,
    )

    src = _build_source()
    dst = _build_target(args.target, args.url)

    try:
        with dst.transaction():
            if "state" in sections:
                copy_state(src, dst, args.dry_run)
            if "profiles" in sections:
                copy_profiles(src, dst, args.dry_run)
            if "events" in sections:
                copy_events(src, dst, args.dry_run)
            if "runs" in sections:
                copy_runs(src, dst, args.dry_run, only_dates)
            if "extractions" in sections:
                copy_extractions(src, dst, args.dry_run, only_dates)
    finally:
        src.close()
        dst.close()

    log.info("Backfill complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
