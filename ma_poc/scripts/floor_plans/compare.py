"""Compare a floor-plan CSV against the floor plans recorded in the DB.

Input CSV columns (header row required):
    apartmentid,floorplannumber,description,bed,bath,area

For every CSV row:
  1. Resolve `apartmentid` → `canonical_id` via `properties.apartment_id`.
  2. Pull the property's distinct floor plans from `units` (de-duped by
     name + bed/bath/area, with a unit-count per group). Treat
     ``area <= 0`` (the v2 ``-1`` sqft sentinel and any zero/negative
     stragglers) as ``None`` so the matcher doesn't compute spurious
     deltas like ``csv_area - (-1)``.
  3. Try to match the CSV row to one DB floor plan, in this order:

       a. name_semantic   — rapidfuzz token_set_ratio(description, db_name)
                            ≥ NAME_THRESHOLD (default 85), filtered to
                            DB plans whose beds/baths match.
       b. bed_bath_sqft   — beds + baths exact AND |csv_area - db_area|
                            ≤ AREA_BUFFER_SQFT (default 35).
       c. bed_bath_only   — beds + baths exact, area ignored.
       d. name_only       — rapidfuzz token_set_ratio ≥ NAME_THRESHOLD
                            against a DB plan whose beds OR baths is
                            NULL (DB lost bed/bath during extraction).
                            Lower-confidence rescue tier — the score is
                            still reported so callers can filter.
       e. unmatched       — none of the above.

Beyond the four match tiers, two additional outcomes are surfaced so the
"unmatched" headline reflects only genuine match failures:

  - ``property_not_found`` — apartmentid not present in ``properties``.
  - ``db_empty``           — apartmentid resolved to a canonical_id, but
                             the ``units`` table has zero rows for that
                             property (typically a scrape failure).

Results land in two Postgres tables (alembic 0008):
  - `floor_plan_comparison_runs`  — per-property aggregate scores
  - `floor_plan_comparison_rows`  — per-CSV-row match decision

Re-running with the same `--run-id` replaces results for that run.

Usage:
    python ma_poc/scripts/compare_floor_plans_csv.py \\
        --csv ma_poc/config/Floorplan-\\ comparisons.csv

    python ma_poc/scripts/compare_floor_plans_csv.py \\
        --csv path/to.csv --run-id 2026-05-02-manual --buffer 50
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from dotenv import load_dotenv
from rapidfuzz import fuzz
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

# Add ma_poc to sys.path so this script can be run directly.
_HERE = Path(__file__).resolve()
_MA_POC_ROOT = _HERE.parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

load_dotenv(_MA_POC_ROOT / ".env")

from data_provider.sql.engine import make_engine, make_session_factory  # noqa: E402
from data_provider.sql.models import (  # noqa: E402
    FloorPlanComparisonRowRow,
    FloorPlanComparisonRunRow,
    PropertyRow,
    UnitRow,
)

log = logging.getLogger("compare_floor_plans_csv")

DEFAULT_NAME_THRESHOLD = 85.0
DEFAULT_AREA_BUFFER_SQFT = 35


# ── Match logic ─────────────────────────────────────────────────────────────


@dataclass
class DbFloorPlan:
    """A distinct floor-plan grouping derived from `units`."""

    name: str | None
    beds: int | None
    baths: float | None
    area: int | None
    unit_count: int

    @property
    def has_name(self) -> bool:
        return bool(self.name and self.name.strip())


@dataclass
class CsvFloorPlan:
    apartment_id: int | None
    floorplan_number: str | None
    description: str | None
    bed: int | None
    bath: float | None
    area: int | None


@dataclass
class MatchResult:
    # name_semantic | bed_bath_sqft | bed_bath_only | name_only |
    # unmatched | property_not_found | db_empty
    method: str
    score: float | None
    matched: DbFloorPlan | None
    sqft_within_buffer: bool | None
    sqft_delta: int | None


def _bb_equal(a: CsvFloorPlan, b: DbFloorPlan) -> bool:
    if a.bed is None or b.beds is None:
        return False
    if a.bath is None or b.baths is None:
        return False
    return int(a.bed) == int(b.beds) and float(a.bath) == float(b.baths)


def _sqft_delta(csv: CsvFloorPlan, db: DbFloorPlan) -> int | None:
    # ``DbFloorPlan.area`` is already coerced to None for the -1 sentinel /
    # zero / negative values at load time, so this just needs to handle the
    # symmetric None-on-either-side case.
    if csv.area is None or db.area is None:
        return None
    return int(csv.area) - int(db.area)


def _bb_partial(a: CsvFloorPlan, b: DbFloorPlan) -> bool:
    """True when CSV has both beds+baths but DB lost at least one of them.

    Used by the (d) ``name_only`` rescue tier so plans whose extraction
    dropped bed/bath but kept the marketing name are still reachable.
    """
    if a.bed is None or a.bath is None:
        return False
    return b.beds is None or b.baths is None


def match_floor_plan(
    csv: CsvFloorPlan,
    db_plans: list[DbFloorPlan],
    name_threshold: float,
    area_buffer: int,
) -> MatchResult:
    """Run the five-level cascade for one CSV row against this property's plans.

    Order: name_semantic → bed_bath_sqft → bed_bath_only → name_only →
    unmatched. Callers handle the ``property_not_found`` / ``db_empty``
    outcomes outside this function.
    """
    if not db_plans:
        return MatchResult("unmatched", None, None, None, None)

    # ── (a) name_semantic — rapidfuzz token_set_ratio, gated on bed/bath ─
    if csv.description and csv.description.strip():
        best: tuple[float, DbFloorPlan] | None = None
        for plan in db_plans:
            if not plan.has_name:
                continue
            if not _bb_equal(csv, plan):
                continue
            score = float(fuzz.token_set_ratio(csv.description, plan.name))
            if score >= name_threshold and (best is None or score > best[0]):
                best = (score, plan)
        if best is not None:
            score, plan = best
            delta = _sqft_delta(csv, plan)
            within = (
                None
                if delta is None
                else abs(delta) <= area_buffer
            )
            return MatchResult("name_semantic", score, plan, within, delta)

    # ── (b) bed_bath_sqft ───────────────────────────────────────────────
    if csv.area is not None:
        best_delta: tuple[int, DbFloorPlan] | None = None
        for plan in db_plans:
            if not _bb_equal(csv, plan):
                continue
            if plan.area is None:
                continue
            delta = abs(int(csv.area) - int(plan.area))
            if delta <= area_buffer and (best_delta is None or delta < best_delta[0]):
                best_delta = (delta, plan)
        if best_delta is not None:
            delta, plan = best_delta
            signed = _sqft_delta(csv, plan)
            return MatchResult("bed_bath_sqft", None, plan, True, signed)

    # ── (c) bed_bath_only ───────────────────────────────────────────────
    bb_only_candidates = [p for p in db_plans if _bb_equal(csv, p)]
    if bb_only_candidates:
        # Prefer the one whose area is closest if both have area; else the first.
        chosen = bb_only_candidates[0]
        if csv.area is not None:
            with_area = [p for p in bb_only_candidates if p.area is not None]
            if with_area:
                chosen = min(with_area, key=lambda p: abs(int(csv.area) - int(p.area)))  # type: ignore[arg-type]
        delta = _sqft_delta(csv, chosen)
        within = (
            None
            if delta is None
            else abs(delta) <= area_buffer
        )
        return MatchResult("bed_bath_only", None, chosen, within, delta)

    # ── (d) name_only — rescue tier: DB lost bed/bath, name still scores
    # high. Limited to plans where DB has the name AND beds-or-baths are
    # NULL, so it never overrides a clean (a–c) match. Score is preserved
    # so analysts can filter or downgrade as needed.
    if csv.description and csv.description.strip():
        best_no: tuple[float, DbFloorPlan] | None = None
        for plan in db_plans:
            if not plan.has_name:
                continue
            if not _bb_partial(csv, plan):
                continue
            score = float(fuzz.token_set_ratio(csv.description, plan.name))
            if score >= name_threshold and (best_no is None or score > best_no[0]):
                best_no = (score, plan)
        if best_no is not None:
            score, plan = best_no
            delta = _sqft_delta(csv, plan)
            within = (
                None
                if delta is None
                else abs(delta) <= area_buffer
            )
            return MatchResult("name_only", score, plan, within, delta)

    return MatchResult("unmatched", None, None, None, None)


# ── DB access ───────────────────────────────────────────────────────────────


def _resolve_apartment_id_to_canonical(
    session: Session, apartment_ids: Iterable[int]
) -> dict[int, tuple[str, str | None]]:
    """Return `{apartment_id: (canonical_id, proj_name)}` for all known IDs."""
    ids = sorted({int(a) for a in apartment_ids})
    if not ids:
        return {}
    rows = session.execute(
        select(PropertyRow.apartment_id, PropertyRow.canonical_id, PropertyRow.proj_name)
        .where(PropertyRow.apartment_id.in_(ids))
    ).all()
    return {int(r.apartment_id): (r.canonical_id, r.proj_name) for r in rows if r.apartment_id is not None}


def _load_db_floor_plans(session: Session, canonical_id: str) -> list[DbFloorPlan]:
    """Load distinct floor plans for a property from the `units` table.

    De-dupes by (floor_plan_name, beds, baths, area) and counts the units
    in each group. Names are normalised (strip + lower) for the dedup key
    only — the original casing is preserved on the returned record.
    """
    rows = session.execute(
        select(
            UnitRow.floor_plan_name,
            UnitRow.beds,
            UnitRow.baths,
            UnitRow.area,
        ).where(UnitRow.canonical_id == canonical_id)
    ).all()

    groups: dict[tuple[str, int | None, float | None, int | None], list[Any]] = defaultdict(list)
    for r in rows:
        # The v2 schema transform writes ``-1`` for "sqft absent" (see
        # ``ma_poc/scripts/schema_v2.py::_format_area``), and a small number
        # of legacy rows carry literal 0. Normalise both to None so they
        # don't anchor the dedup key or feed _sqft_delta.
        area_val: int | None = None
        if r.area is not None:
            try:
                area_int = int(r.area)
                if area_int > 0:
                    area_val = area_int
            except (TypeError, ValueError):
                area_val = None

        key = (
            (r.floor_plan_name or "").strip().lower(),
            int(r.beds) if r.beds is not None else None,
            float(r.baths) if r.baths is not None else None,
            area_val,
        )
        groups[key].append(r)

    plans: list[DbFloorPlan] = []
    for (_, beds, baths, area), bucket in groups.items():
        # Use the first non-empty original name in the bucket.
        name = next(
            (b.floor_plan_name for b in bucket if b.floor_plan_name and b.floor_plan_name.strip()),
            None,
        )
        plans.append(
            DbFloorPlan(
                name=name,
                beds=beds,
                baths=baths,
                area=area,
                unit_count=len(bucket),
            )
        )
    return plans


# ── CSV loading ─────────────────────────────────────────────────────────────


def _coerce_int(v: Any) -> int | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _coerce_float(v: Any) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_str(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def load_csv(csv_path: Path) -> list[CsvFloorPlan]:
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    rows: list[CsvFloorPlan] = []
    for _, r in df.iterrows():
        rows.append(
            CsvFloorPlan(
                apartment_id=_coerce_int(r.get("apartmentid")),
                floorplan_number=_coerce_str(r.get("floorplannumber")),
                description=_coerce_str(r.get("description")),
                bed=_coerce_int(r.get("bed")),
                bath=_coerce_float(r.get("bath")),
                area=_coerce_int(r.get("area")),
            )
        )
    return rows


# ── Orchestration ───────────────────────────────────────────────────────────


def run_comparison(
    csv_path: Path,
    run_id: str,
    name_threshold: float,
    area_buffer: int,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Run the comparison end-to-end. Returns a top-level summary dict."""
    log.info("Reading CSV: %s", csv_path)
    csv_rows = load_csv(csv_path)
    log.info("Loaded %d CSV rows", len(csv_rows))

    engine = make_engine(database_url)
    SessionLocal = make_session_factory(engine)

    # Group CSV rows by apartment_id so we load each property's floor plans once.
    by_aid: dict[int | None, list[CsvFloorPlan]] = defaultdict(list)
    for r in csv_rows:
        by_aid[r.apartment_id].append(r)

    with SessionLocal() as session:
        # 1. Wipe any prior rows for this run_id (idempotent re-runs).
        session.execute(
            delete(FloorPlanComparisonRowRow).where(FloorPlanComparisonRowRow.run_id == run_id)
        )
        session.execute(
            delete(FloorPlanComparisonRunRow).where(FloorPlanComparisonRunRow.run_id == run_id)
        )

        # 2. Resolve apartment_id → canonical_id in one query.
        aid_to_cid = _resolve_apartment_id_to_canonical(
            session, [a for a in by_aid.keys() if a is not None]
        )
        log.info(
            "Resolved %d/%d apartment_ids to canonical_ids",
            len(aid_to_cid),
            sum(1 for a in by_aid if a is not None),
        )

        run_rows: list[FloorPlanComparisonRunRow] = []
        per_row_records: list[FloorPlanComparisonRowRow] = []

        # Bulk counters across the whole run.
        totals = {
            "csv_rows": 0,
            "matched": 0,
            "name": 0,
            "bb_sqft": 0,
            "bb_only": 0,
            "name_only": 0,
            "unmatched": 0,
            "property_not_found": 0,
            "db_empty": 0,
            "sqft_within_buffer": 0,
            "properties_compared": 0,
            "properties_with_any_match": 0,
        }

        for apartment_id, rows in by_aid.items():
            if apartment_id is None or apartment_id not in aid_to_cid:
                # No DB property for this apartment_id — record each row as
                # property_not_found and skip the per-property summary.
                for csv_row in rows:
                    per_row_records.append(
                        FloorPlanComparisonRowRow(
                            run_id=run_id,
                            canonical_id=None,
                            csv_apartment_id=csv_row.apartment_id,
                            csv_floorplan_number=csv_row.floorplan_number,
                            csv_description=csv_row.description,
                            csv_bed=csv_row.bed,
                            csv_bath=csv_row.bath,
                            csv_area=csv_row.area,
                            match_method="property_not_found",
                            match_score=None,
                            sqft_within_buffer=None,
                            sqft_delta=None,
                            db_floor_plan_name=None,
                            db_beds=None,
                            db_baths=None,
                            db_area=None,
                            db_unit_count=None,
                        )
                    )
                    totals["csv_rows"] += 1
                    totals["property_not_found"] += 1
                continue

            canonical_id, proj_name = aid_to_cid[apartment_id]
            db_plans = _load_db_floor_plans(session, canonical_id)
            matched_db_keys: set[tuple[Any, ...]] = set()

            per_property = {
                "csv_rows": 0,
                "matched": 0,
                "name": 0,
                "bb_sqft": 0,
                "bb_only": 0,
                "name_only": 0,
                "unmatched": 0,
                "db_empty": 0,
                "sqft_within_buffer": 0,
            }

            # Properties whose canonical_id resolved but ``units`` is empty
            # are scrape failures, not matching defects. Bucket their rows
            # as ``db_empty`` so the unmatched headline reflects only real
            # match failures.
            db_is_empty = len(db_plans) == 0

            for csv_row in rows:
                if db_is_empty:
                    result = MatchResult("db_empty", None, None, None, None)
                else:
                    result = match_floor_plan(csv_row, db_plans, name_threshold, area_buffer)
                per_property["csv_rows"] += 1
                totals["csv_rows"] += 1

                if result.method == "name_semantic":
                    per_property["name"] += 1
                    per_property["matched"] += 1
                    totals["name"] += 1
                    totals["matched"] += 1
                elif result.method == "bed_bath_sqft":
                    per_property["bb_sqft"] += 1
                    per_property["matched"] += 1
                    totals["bb_sqft"] += 1
                    totals["matched"] += 1
                elif result.method == "bed_bath_only":
                    per_property["bb_only"] += 1
                    per_property["matched"] += 1
                    totals["bb_only"] += 1
                    totals["matched"] += 1
                elif result.method == "name_only":
                    per_property["name_only"] += 1
                    per_property["matched"] += 1
                    totals["name_only"] += 1
                    totals["matched"] += 1
                elif result.method == "db_empty":
                    per_property["db_empty"] += 1
                    totals["db_empty"] += 1
                else:
                    per_property["unmatched"] += 1
                    totals["unmatched"] += 1

                if result.sqft_within_buffer:
                    per_property["sqft_within_buffer"] += 1
                    totals["sqft_within_buffer"] += 1

                if result.matched is not None:
                    matched_db_keys.add(
                        (
                            (result.matched.name or "").strip().lower(),
                            result.matched.beds,
                            result.matched.baths,
                            result.matched.area,
                        )
                    )

                per_row_records.append(
                    FloorPlanComparisonRowRow(
                        run_id=run_id,
                        canonical_id=canonical_id,
                        csv_apartment_id=csv_row.apartment_id,
                        csv_floorplan_number=csv_row.floorplan_number,
                        csv_description=csv_row.description,
                        csv_bed=csv_row.bed,
                        csv_bath=csv_row.bath,
                        csv_area=csv_row.area,
                        match_method=result.method,
                        match_score=result.score,
                        sqft_within_buffer=result.sqft_within_buffer,
                        sqft_delta=result.sqft_delta,
                        db_floor_plan_name=result.matched.name if result.matched else None,
                        db_beds=result.matched.beds if result.matched else None,
                        db_baths=result.matched.baths if result.matched else None,
                        db_area=result.matched.area if result.matched else None,
                        db_unit_count=result.matched.unit_count if result.matched else None,
                    )
                )

            totals["properties_compared"] += 1
            if per_property["matched"] > 0:
                totals["properties_with_any_match"] += 1

            # Count DB plans that no CSV row matched to.
            db_keys_all = {
                ((p.name or "").strip().lower(), p.beds, p.baths, p.area)
                for p in db_plans
            }
            missing_in_csv = len(db_keys_all - matched_db_keys)

            run_rows.append(
                FloorPlanComparisonRunRow(
                    run_id=run_id,
                    canonical_id=canonical_id,
                    apartment_id=apartment_id,
                    proj_name=proj_name,
                    csv_rows_total=per_property["csv_rows"],
                    db_floor_plans_total=len(db_plans),
                    matched_total=per_property["matched"],
                    name_match_count=per_property["name"],
                    bed_bath_sqft_match_count=per_property["bb_sqft"],
                    bed_bath_only_match_count=per_property["bb_only"],
                    name_only_match_count=per_property["name_only"],
                    unmatched_count=per_property["unmatched"],
                    db_empty_count=per_property["db_empty"],
                    sqft_within_buffer_count=per_property["sqft_within_buffer"],
                    missing_in_csv_count=missing_in_csv,
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )

        # 3. Bulk insert.
        if run_rows:
            session.bulk_save_objects(run_rows)
        if per_row_records:
            session.bulk_save_objects(per_row_records)
        session.commit()

    summary = {
        "run_id": run_id,
        "csv_path": str(csv_path),
        "name_threshold": name_threshold,
        "area_buffer_sqft": area_buffer,
        "totals": totals,
    }
    log.info("Comparison complete: %s", summary)
    return summary


# ── CLI ─────────────────────────────────────────────────────────────────────


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="Path to comparison CSV")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Stable identifier for this comparison (default: UTC timestamp).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_NAME_THRESHOLD,
        help=f"rapidfuzz token_set_ratio threshold (default: {DEFAULT_NAME_THRESHOLD})",
    )
    parser.add_argument(
        "--buffer",
        type=int,
        default=DEFAULT_AREA_BUFFER_SQFT,
        help=f"Sqft buffer for bed/bath/sqft matching (default: ±{DEFAULT_AREA_BUFFER_SQFT})",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL (otherwise read from env via make_engine)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    csv_path = args.csv if args.csv.is_absolute() else (Path.cwd() / args.csv).resolve()
    if not csv_path.exists():
        log.error("CSV not found: %s", csv_path)
        return 2

    run_id = args.run_id or _default_run_id()
    summary = run_comparison(
        csv_path=csv_path,
        run_id=run_id,
        name_threshold=args.threshold,
        area_buffer=args.buffer,
        database_url=args.database_url,
    )

    print("\n-- Floor plan comparison summary --")
    print(f"  run_id:           {summary['run_id']}")
    print(f"  csv:              {summary['csv_path']}")
    print(f"  name_threshold:   {summary['name_threshold']}")
    print(f"  area_buffer_sqft: ±{summary['area_buffer_sqft']}")
    t = summary["totals"]
    print(f"  csv rows:                   {t['csv_rows']}")
    print(f"  matched (any method):       {t['matched']}")
    print(f"    name_semantic:            {t['name']}")
    print(f"    bed_bath_sqft:            {t['bb_sqft']}")
    print(f"    bed_bath_only:            {t['bb_only']}")
    print(f"    name_only (rescue):       {t['name_only']}")
    print(f"  unmatched:                  {t['unmatched']}")
    print(f"  db_empty (scrape failed):   {t['db_empty']}")
    print(f"  property_not_found:         {t['property_not_found']}")
    print(f"  sqft within +/-{summary['area_buffer_sqft']}:        {t['sqft_within_buffer']}")
    print(f"  properties compared:        {t['properties_compared']}")
    print(f"  properties with any match:  {t['properties_with_any_match']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
