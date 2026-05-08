"""Sync `property_snapshots` rows into the current-state `properties` table.

Motivation:
  - The Jugnu pipeline writes per-run payloads to `data/v2/runs/{date}/properties.json`
    → `property_snapshots` table.
  - It does NOT update `data/state/property_index.json`, so `backfill_pg.py --only state`
    only captures properties seen by the legacy `daily_runner.py`.
  - This script bridges the gap: reads `property_snapshots` for a given run_date
    and upserts every row into `properties` (current-state) via the standard
    SqlPropertyStateStore upsert path.

Idempotent — re-running replaces each row in place.

Usage:
    python scripts/sync/properties_from_snapshots.py --run-date 2026-04-21
    python scripts/sync/properties_from_snapshots.py --run-date 2026-04-21 --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_MA_POC_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402

from data_provider import PostgresDataProvider  # noqa: E402
from data_provider.sql.engine import resolve_database_url  # noqa: E402

log = logging.getLogger("sync_properties_from_snapshots")


def _verdict_to_status(verdict: str | None) -> str | None:
    if not verdict:
        return None
    if verdict == "SUCCESS":
        return "SUCCESS"
    if verdict.startswith("FAILED"):
        return "FAILED"
    if verdict == "CARRY_FORWARD":
        return "CARRIED_FORWARD"
    return verdict


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-date", required=True, help="e.g. 2026-04-21")
    ap.add_argument("--url", default=None, help="DATABASE_URL override")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_url = args.url or resolve_database_url()
    engine = create_engine(db_url)

    with engine.connect() as c:
        rows = c.execute(
            text("select canonical_id, payload from property_snapshots where run_date = :d order by ordinal"),
            {"d": args.run_date},
        ).all()

    log.info("snapshots for %s: %d", args.run_date, len(rows))
    if args.dry_run:
        return 0

    dst = PostgresDataProvider(url=db_url)
    upserted = new_count = existing = skipped = 0
    with dst.transaction():
        for cid, payload in rows:
            if not cid:
                skipped += 1
                continue
            # Snapshot-level fields only — drop `units` and any `_*` private keys.
            snap = {k: v for k, v in payload.items() if not k.startswith("_") and k != "units"}
            meta = payload.get("_meta") or {}
            status = _verdict_to_status(meta.get("verdict"))
            if status:
                snap["last_scrape_status"] = status
            units = payload.get("units") or []
            snap["last_units_count"] = len(units)

            is_new = dst.property_state.upsert(cid, snap, args.run_date)
            upserted += 1
            if is_new:
                new_count += 1
            else:
                existing += 1
            if upserted % 200 == 0:
                log.info("  %d/%d", upserted, len(rows))
    dst.close()
    log.info(
        "done. upserted=%d new=%d existed=%d skipped_no_cid=%d",
        upserted,
        new_count,
        existing,
        skipped,
    )

    # Summary query for operator visibility
    with engine.connect() as c:
        total = c.execute(text("select count(*) from properties")).scalar()
        seen = c.execute(
            # last_seen_at is a full ISO timestamp; compare the YYYY-MM-DD prefix.
            text("select count(*) from properties where LEFT(last_seen_at, 10) = :d"),
            {"d": args.run_date},
        ).scalar()
        log.info("properties total=%d; seen on %s: %d", total, args.run_date, seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
