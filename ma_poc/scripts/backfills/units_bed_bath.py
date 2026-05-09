"""Backfill ``units.beds`` / ``units.baths`` from ``floor_plan_name``.

Many production rows landed in DB with NULL beds/baths because the
adapter / API didn't volunteer those fields, even when the plan name
unambiguously encodes them ("1BD/1BR-1", "2x2", "Studio"). The v2
transform now infers them at write time (Phase 2), but historical rows
need a one-shot backfill so the floor-plan comparator and downstream
reports can match them.

The script ONLY fills NULL values — it never overwrites an existing
beds/baths. Re-running is idempotent: a second run touches no rows.

Usage:
    python ma_poc/scripts/backfills/units_bed_bath.py --dry-run
    python ma_poc/scripts/backfills/units_bed_bath.py
    python ma_poc/scripts/backfills/units_bed_bath.py --batch-size 500
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

# Ensure both ma_poc/ and its parent are on sys.path so the script
# can be invoked directly: ma_poc/ unlocks `from data_provider.*` and
# the parent unlocks `from ma_poc.pms.*` (the convention the rest of
# the repo uses for the adapter package).
_HERE = Path(__file__).resolve()
_MA_POC_ROOT = _HERE.parent.parent.parent  # ma_poc/
_REPO_ROOT = _MA_POC_ROOT.parent  # PropAi/
for _p in (_REPO_ROOT, _MA_POC_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

load_dotenv(_MA_POC_ROOT / ".env")

from data_provider.sql.engine import make_engine, make_session_factory  # noqa: E402
from data_provider.sql.models import UnitRow  # noqa: E402
from ma_poc.pms.adapters._parsing import infer_bed_bath_from_name  # noqa: E402

log = logging.getLogger("backfill_units_bed_bath")


def backfill(
    session: Session, batch_size: int, dry_run: bool
) -> dict[str, int]:
    """Walk units with NULL beds or baths and fill from floor_plan_name.

    Updates are issued one row at a time inside an explicit transaction
    so a partial run still leaves the DB consistent. ``batch_size``
    controls the commit cadence — every N updates the transaction is
    committed and a fresh one opened.
    """
    counters = {
        "scanned": 0,
        "no_name": 0,
        "no_inference": 0,
        "beds_filled": 0,
        "baths_filled": 0,
        "rows_updated": 0,
    }

    # Stream candidates: anything with at least one NULL bed/bath. We
    # fetch the full row so SQLAlchemy can issue a row-targeted UPDATE
    # against the composite primary key (canonical_id, unit_id).
    stmt = select(UnitRow).where(
        or_(UnitRow.beds.is_(None), UnitRow.baths.is_(None))
    )
    pending = 0
    for unit in session.scalars(stmt).yield_per(1000):
        counters["scanned"] += 1
        name = unit.floor_plan_name
        if not name:
            counters["no_name"] += 1
            continue

        inf_beds, inf_baths = infer_bed_bath_from_name(name)
        if inf_beds is None and inf_baths is None:
            counters["no_inference"] += 1
            continue

        new_beds = unit.beds
        new_baths = unit.baths
        if unit.beds is None and inf_beds is not None:
            new_beds = inf_beds
            counters["beds_filled"] += 1
        if unit.baths is None and inf_baths is not None:
            new_baths = inf_baths
            counters["baths_filled"] += 1

        if (new_beds is unit.beds) and (new_baths is unit.baths):
            # Inference returned only the field that was already set —
            # no actual gap to fill.
            continue

        counters["rows_updated"] += 1
        if dry_run:
            continue

        session.execute(
            update(UnitRow)
            .where(
                UnitRow.canonical_id == unit.canonical_id,
                UnitRow.unit_id == unit.unit_id,
            )
            .values(beds=new_beds, baths=new_baths)
        )
        pending += 1
        if pending >= batch_size:
            session.commit()
            pending = 0

    if not dry_run and pending:
        session.commit()

    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Commit cadence (default 500 updates per transaction).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk and count without issuing UPDATEs.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    engine = make_engine(args.database_url)
    SessionLocal = make_session_factory(engine)

    log.info(
        "Starting backfill (dry_run=%s, batch_size=%d)",
        args.dry_run,
        args.batch_size,
    )
    with SessionLocal() as session:
        counters = backfill(session, args.batch_size, args.dry_run)

    print("\n-- units bed/bath backfill summary --")
    for k, v in counters.items():
        print(f"  {k:18s} {v}")
    print(f"  dry_run            {args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
