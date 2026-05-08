"""Export genuine CSV-vs-DB floor-plan disagreements to the review queue.

After Phase 1 + 2 + 3, the matcher pulls the floor of "easy"
disagreements out of the unmatched bucket. What's left is the truly
interesting set: rows where the fuzzy name match is high but bed/bath
disagree (e.g., CSV says "Bennett" is a studio, DB extraction says it's
a 1-bedroom).

This script reads from ``floor_plan_comparison_rows`` for a given
``--run-id`` and writes a row into ``floor_plan_disagreement_review``
for each disagreement matching the heuristics below.

Disagreement criteria (a row qualifies for review when ALL hold):
  - The matcher returned ``name_only`` (DB lost beds/baths) OR
    ``name_semantic`` AND CSV bed/bath disagree with the matched DB
    plan's bed/bath.
  - CSV bed and bath are both populated.
  - Name match score >= ``--min-score`` (default 90; tighter than the
    matcher's 85 to keep the queue actionable).

Idempotency: re-running with the same ``--run-id`` wipes only the
``status='pending'`` rows for that run. Reviewer-marked rows (any
status other than pending) are preserved across re-exports so manual
work isn't lost.

Usage:
    python ma_poc/scripts/floor_plans/export_disagreements.py \\
        --run-id 2026-05-07-phase2

    python ma_poc/scripts/floor_plans/export_disagreements.py \\
        --run-id 2026-05-07-phase2 --min-score 95 --to-csv out.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

_HERE = Path(__file__).resolve()
_MA_POC_ROOT = _HERE.parent.parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_REPO_ROOT, _MA_POC_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

load_dotenv(_MA_POC_ROOT / ".env")

from data_provider.sql.engine import make_engine, make_session_factory  # noqa: E402
from data_provider.sql.models import (  # noqa: E402
    FloorPlanComparisonRowRow,
    FloorPlanDisagreementReviewRow,
)

log = logging.getLogger("export_floor_plan_disagreements")


def _is_disagreement(row: FloorPlanComparisonRowRow, min_score: float) -> bool:
    """Decide whether a comparator row deserves a manual review.

    Implements the criteria in the module docstring; pulled out to keep
    the orchestration loop readable.
    """
    if row.csv_bed is None or row.csv_bath is None:
        return False
    if row.match_score is None or row.match_score < min_score:
        return False

    method = row.match_method
    # Tier (d): DB lost bed/bath, fuzzy-name still hit. Always review-worthy
    # because the bed/bath mismatch is structural — the DB doesn't say.
    if method == "name_only":
        return True

    # Tier (a): clean name match. Only worth reviewing when the
    # *resolved* DB plan disagrees with the CSV — usually the matcher
    # already enforced bed/bath equality (`_bb_equal` gate), but the
    # rescue path in earlier code paths could let a near-match slip
    # through. Belt-and-braces.
    if method == "name_semantic":
        if row.db_beds is None or row.db_baths is None:
            return True
        if int(row.csv_bed) != int(row.db_beds):
            return True
        if abs(float(row.csv_bath) - float(row.db_baths)) > 0.01:
            return True

    return False


def export(
    session: Session, run_id: str, min_score: float
) -> dict[str, int]:
    """Build the review queue for ``run_id`` and write it.

    Only ``status='pending'`` rows are wiped before re-inserting, so
    reviewer-marked entries survive across re-exports.
    """
    counters = {
        "scanned": 0,
        "selected": 0,
        "preserved": 0,
        "wrote": 0,
    }

    # 1. Wipe pending rows for this run (idempotent re-export).
    session.execute(
        delete(FloorPlanDisagreementReviewRow).where(
            and_(
                FloorPlanDisagreementReviewRow.run_id == run_id,
                FloorPlanDisagreementReviewRow.status == "pending",
            )
        )
    )

    # 2. Track reviewer-marked rows so we don't double-insert them.
    preserved_keys: set[tuple[str | None, str | None, int | None]] = set()
    for r in session.scalars(
        select(FloorPlanDisagreementReviewRow).where(
            and_(
                FloorPlanDisagreementReviewRow.run_id == run_id,
                FloorPlanDisagreementReviewRow.status != "pending",
            )
        )
    ):
        counters["preserved"] += 1
        preserved_keys.add(
            (r.canonical_id, r.csv_floorplan_number, r.csv_apartment_id)
        )

    # 3. Stream comparator rows and select disagreements.
    stmt = select(FloorPlanComparisonRowRow).where(
        and_(
            FloorPlanComparisonRowRow.run_id == run_id,
            or_(
                FloorPlanComparisonRowRow.match_method == "name_only",
                FloorPlanComparisonRowRow.match_method == "name_semantic",
            ),
        )
    )
    to_insert: list[FloorPlanDisagreementReviewRow] = []
    for crow in session.scalars(stmt):
        counters["scanned"] += 1
        if not _is_disagreement(crow, min_score):
            continue

        key = (crow.canonical_id, crow.csv_floorplan_number, crow.csv_apartment_id)
        if key in preserved_keys:
            # The reviewer has already touched this one — leave it alone.
            continue

        counters["selected"] += 1
        to_insert.append(
            FloorPlanDisagreementReviewRow(
                run_id=run_id,
                canonical_id=crow.canonical_id,
                csv_apartment_id=crow.csv_apartment_id,
                csv_floorplan_number=crow.csv_floorplan_number,
                csv_description=crow.csv_description,
                csv_bed=crow.csv_bed,
                csv_bath=crow.csv_bath,
                csv_area=crow.csv_area,
                db_floor_plan_name=crow.db_floor_plan_name,
                db_beds=crow.db_beds,
                db_baths=crow.db_baths,
                db_area=crow.db_area,
                name_match_score=crow.match_score,
                status="pending",
            )
        )

    if to_insert:
        session.bulk_save_objects(to_insert)
        counters["wrote"] = len(to_insert)
    session.commit()

    return counters


def dump_csv(session: Session, run_id: str, path: Path) -> int:
    """Write the queue to a flat CSV for offline review."""
    rows = list(
        session.scalars(
            select(FloorPlanDisagreementReviewRow)
            .where(FloorPlanDisagreementReviewRow.run_id == run_id)
            .order_by(
                FloorPlanDisagreementReviewRow.canonical_id,
                FloorPlanDisagreementReviewRow.csv_floorplan_number,
            )
        )
    )
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "run_id",
                "canonical_id",
                "csv_apartment_id",
                "csv_floorplan_number",
                "csv_description",
                "csv_bed",
                "csv_bath",
                "csv_area",
                "db_floor_plan_name",
                "db_beds",
                "db_baths",
                "db_area",
                "name_match_score",
                "status",
                "reviewer_note",
            ]
        )
        for r in rows:
            w.writerow(
                [
                    r.run_id,
                    r.canonical_id,
                    r.csv_apartment_id,
                    r.csv_floorplan_number,
                    r.csv_description,
                    r.csv_bed,
                    r.csv_bath,
                    r.csv_area,
                    r.db_floor_plan_name,
                    r.db_beds,
                    r.db_baths,
                    r.db_area,
                    r.name_match_score,
                    r.status,
                    r.reviewer_note,
                ]
            )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        required=True,
        help="The comparator run_id to export disagreements for.",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=90.0,
        help="Minimum name-match score (0-100) to qualify (default 90).",
    )
    parser.add_argument(
        "--to-csv",
        type=Path,
        default=None,
        help="Optional path to also dump the queue as a CSV.",
    )
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
        "Building review queue for run_id=%s (min_score=%.1f)",
        args.run_id,
        args.min_score,
    )
    with SessionLocal() as session:
        counters = export(session, args.run_id, args.min_score)
        if args.to_csv:
            n = dump_csv(session, args.run_id, args.to_csv)
            log.info("Wrote %d rows to %s", n, args.to_csv)

    print("\n-- floor-plan disagreement review summary --")
    print(f"  run_id:       {args.run_id}")
    print(f"  min_score:    {args.min_score}")
    for k, v in counters.items():
        print(f"  {k:12s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
