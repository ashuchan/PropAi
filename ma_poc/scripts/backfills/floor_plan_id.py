"""Backfill ``units.floor_plan_id`` from existing (canonical_id, name, beds, baths).

Phase 3 stamps ``floor_plan_id`` at write time, but historical units
predating the change have NULL. This script computes the id with the
same helper the writer uses (``compute_floor_plan_id``) so future
updates to the algorithm only need to be applied in one place.

The script is idempotent: only rows where ``floor_plan_id`` is NULL
are touched. Re-runs are no-ops.

Usage:
    python ma_poc/scripts/backfill_floor_plan_id.py --dry-run
    python ma_poc/scripts/backfill_floor_plan_id.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import select, update
from sqlalchemy.orm import Session

_HERE = Path(__file__).resolve()
_MA_POC_ROOT = _HERE.parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_REPO_ROOT, _MA_POC_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

load_dotenv(_MA_POC_ROOT / ".env")

from data_provider.sql.engine import make_engine, make_session_factory  # noqa: E402
from data_provider.sql.models import UnitRow  # noqa: E402
from ma_poc.pms.adapters._parsing import compute_floor_plan_id  # noqa: E402

log = logging.getLogger("backfill_floor_plan_id")


def backfill(
    session: Session, batch_size: int, dry_run: bool
) -> dict[str, int]:
    """Walk units with NULL floor_plan_id and populate them.

    The helper returns None when a row is plan-anchorless (no name and
    no beds/baths). Those rows are counted but not updated, since they
    can't be reliably grouped.
    """
    counters = {
        "scanned": 0,
        "no_anchor": 0,
        "rows_updated": 0,
    }

    stmt = select(UnitRow).where(UnitRow.floor_plan_id.is_(None))
    pending = 0
    for unit in session.scalars(stmt).yield_per(1000):
        counters["scanned"] += 1
        fpid = compute_floor_plan_id(
            unit.canonical_id, unit.floor_plan_name, unit.beds, unit.baths
        )
        if fpid is None:
            counters["no_anchor"] += 1
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
            .values(floor_plan_id=fpid)
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
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    engine = make_engine(args.database_url)
    SessionLocal = make_session_factory(engine)

    log.info(
        "Starting floor_plan_id backfill (dry_run=%s, batch_size=%d)",
        args.dry_run,
        args.batch_size,
    )
    with SessionLocal() as session:
        counters = backfill(session, args.batch_size, args.dry_run)

    print("\n-- floor_plan_id backfill summary --")
    for k, v in counters.items():
        print(f"  {k:18s} {v}")
    print(f"  dry_run            {args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
