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

After the DB rows are written, the script renders a markdown report from
them (via ``scripts.reports.floor_plan_comparison.build_report``) and
emails it to ``REPORT_RECIPIENTS`` (or ``--email-recipients``). Pass
``--no-email`` to skip the report+email step.

Usage:
    python -m ma_poc.scripts.floor_plans.compare \\
        --csv ma_poc/config/Floorplan-comparisons.csv

    python -m ma_poc.scripts.floor_plans.compare \\
        --csv path/to.csv --run-id 2026-05-02-manual --buffer 50

    # DB-only run, no email:
    python -m ma_poc.scripts.floor_plans.compare \\
        --csv path/to.csv --no-email
"""

from __future__ import annotations

import argparse
import logging
import os
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
_MA_POC_ROOT = _HERE.parent.parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

load_dotenv(_MA_POC_ROOT / ".env")

# DATABASE_URL in .env may carry the asyncpg driver because the FastAPI
# service is async. This script is sync — swap to pg8000 before any
# data_provider import reads the env var.
_db_url = os.environ.get("DATABASE_URL", "")
if "+asyncpg" in _db_url:
    os.environ["DATABASE_URL"] = _db_url.replace("+asyncpg", "+pg8000")

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


def _load_db_floor_plans(
    session: Session,
    canonical_id: str,
    last_seen_on: str | None = None,
) -> list[DbFloorPlan]:
    """Load distinct floor plans for a property from the `units` table.

    De-dupes by (floor_plan_name, beds, baths, area) and counts the units
    in each group. Names are normalised (strip + lower) for the dedup key
    only — the original casing is preserved on the returned record.

    When ``last_seen_on`` is set (``YYYY-MM-DD``), units whose
    ``last_seen_at`` ISO timestamp does not start with that date are
    excluded. ``last_seen_at`` is re-stamped by ``StateStore.upsert_units``
    on every appearance (including carry-forwards), so the filter
    expresses "plans backing units that were seen on this run-date".
    """
    stmt = select(
        UnitRow.floor_plan_name,
        UnitRow.beds,
        UnitRow.baths,
        UnitRow.area,
    ).where(UnitRow.canonical_id == canonical_id)
    if last_seen_on:
        stmt = stmt.where(UnitRow.last_seen_at.like(f"{last_seen_on}%"))
    rows = session.execute(stmt).all()

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
    last_seen_on: str | None = None,
) -> dict[str, Any]:
    """Run the comparison end-to-end. Returns a top-level summary dict.

    When ``last_seen_on`` is set, the DB floor-plan pool is scoped to
    units whose ``last_seen_at`` falls on that date — i.e. plans backing
    units that were upserted or carried forward on that run-date. The
    CSV input is unchanged; only the DB side gets the filter.
    """
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
            db_plans = _load_db_floor_plans(session, canonical_id, last_seen_on)
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
        "last_seen_on": last_seen_on,
        "totals": totals,
    }
    log.info("Comparison complete: %s", summary)
    return summary


# ── Dual-scope orchestration ────────────────────────────────────────────────


TODAY_SUFFIX = "__today"


def _today_run_id(base_run_id: str) -> str:
    return f"{base_run_id}{TODAY_SUFFIX}"


def run_dual_comparison(
    csv_path: Path,
    base_run_id: str,
    name_threshold: float,
    area_buffer: int,
    last_seen_on: str,
    scope: str,
    database_url: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Run the comparison for one or both scopes.

    ``scope`` is ``today``, ``overall``, or ``both``. The "today" pass
    writes to ``<base_run_id>__today`` and filters the DB plan pool to
    ``units.last_seen_at`` starting with ``last_seen_on``. The "overall"
    pass writes to ``<base_run_id>`` and uses every unit on record.

    Returns ``{scope_label: summary_dict}`` so the caller can drive the
    combined report rendering without re-querying the DB.
    """
    if scope not in {"today", "overall", "both"}:
        raise ValueError(f"scope must be today|overall|both, got {scope!r}")

    summaries: dict[str, dict[str, Any]] = {}

    if scope in {"today", "both"}:
        today_run_id = _today_run_id(base_run_id)
        log.info("Running TODAY scope (last_seen_on=%s, run_id=%s)", last_seen_on, today_run_id)
        summaries["today"] = run_comparison(
            csv_path=csv_path,
            run_id=today_run_id,
            name_threshold=name_threshold,
            area_buffer=area_buffer,
            database_url=database_url,
            last_seen_on=last_seen_on,
        )

    if scope in {"overall", "both"}:
        log.info("Running OVERALL scope (run_id=%s)", base_run_id)
        summaries["overall"] = run_comparison(
            csv_path=csv_path,
            run_id=base_run_id,
            name_threshold=name_threshold,
            area_buffer=area_buffer,
            database_url=database_url,
            last_seen_on=None,
        )

    return summaries


# ── CLI ─────────────────────────────────────────────────────────────────────


def _default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _default_last_seen_on() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _print_summary(scope_label: str, summary: dict[str, Any]) -> None:
    """Print a single scope's summary to stdout — visible in Cloud Run logs."""
    t = summary["totals"]
    print(f"\n-- Floor plan comparison summary [{scope_label}] --")
    print(f"  run_id:           {summary['run_id']}")
    print(f"  last_seen_on:     {summary.get('last_seen_on') or '(all units)'}")
    print(f"  name_threshold:   {summary['name_threshold']}")
    print(f"  area_buffer_sqft: ±{summary['area_buffer_sqft']}")
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


def _scope_h1(scope_label: str, summary: dict[str, Any]) -> str:
    """Top-level heading for a scope's section inside the combined report."""
    if scope_label == "today":
        return f"# Today — `last_seen_at = {summary.get('last_seen_on')}`"
    return "# Overall — all units on record"


def _build_combined_markdown(
    summaries: dict[str, dict[str, Any]],
    base_run_id: str,
    top_n_properties: int,
    examples_per_method: int,
    database_url: str | None,
) -> str:
    """Stitch a single markdown document covering every scope that ran.

    Both sections come from the same ``build_report`` helper used by the
    standalone report CLI, so the two paths can never diverge. The shell
    CSS gives every h1 a top border, so the scope sections are visually
    separated in the rendered email.
    """
    # Import lazily — keeps the comparison-only code path (e.g. tests
    # mocking the email layer) free of the report module's heavier deps.
    from scripts.reports.floor_plan_comparison import build_report

    engine = make_engine(database_url)
    SessionLocal = make_session_factory(engine)

    parts: list[str] = []
    parts.append(f"# Floor Plan Comparison — `{base_run_id}`")
    parts.append("")
    parts.append(_combined_tldr_table(summaries))
    parts.append("")

    with SessionLocal() as session:
        for scope_label in ("today", "overall"):
            summary = summaries.get(scope_label)
            if summary is None:
                continue
            section_md = build_report(
                session=session,
                run_id=summary["run_id"],
                top_n_properties=top_n_properties,
                examples_per_method=examples_per_method,
                last_seen_on=summary.get("last_seen_on"),
                title_override=_scope_h1(scope_label, summary),
            )
            parts.append(section_md)
            parts.append("")

    return "\n".join(parts)


def _combined_tldr_table(summaries: dict[str, dict[str, Any]]) -> str:
    """Side-by-side headline table for the combined report."""
    rows: list[str] = []
    rows.append("## Headline")
    rows.append("")
    rows.append("| Metric | Today | Overall |")
    rows.append("|---|---:|---:|")

    def _cell(scope_label: str, key: str) -> str:
        s = summaries.get(scope_label)
        if s is None:
            return "—"
        return f"{s['totals'].get(key, 0):,}"

    def _pct_cell(scope_label: str, num_key: str, denom_key: str) -> str:
        s = summaries.get(scope_label)
        if s is None:
            return "—"
        num = s["totals"].get(num_key, 0)
        denom = s["totals"].get(denom_key, 0)
        if not denom:
            return "0.00%"
        return f"{(num / denom) * 100:.2f}%"

    rows.append(f"| CSV rows | {_cell('today', 'csv_rows')} | {_cell('overall', 'csv_rows')} |")
    rows.append(
        f"| Matched | {_cell('today', 'matched')} ({_pct_cell('today', 'matched', 'csv_rows')}) "
        f"| {_cell('overall', 'matched')} ({_pct_cell('overall', 'matched', 'csv_rows')}) |"
    )
    rows.append(f"| Unmatched | {_cell('today', 'unmatched')} | {_cell('overall', 'unmatched')} |")
    rows.append(f"| db_empty | {_cell('today', 'db_empty')} | {_cell('overall', 'db_empty')} |")
    rows.append(
        f"| Properties compared | {_cell('today', 'properties_compared')} "
        f"| {_cell('overall', 'properties_compared')} |"
    )
    rows.append(
        f"| Properties with any match | {_cell('today', 'properties_with_any_match')} "
        f"| {_cell('overall', 'properties_with_any_match')} |"
    )
    return "\n".join(rows)


def _build_email_summary(summaries: dict[str, dict[str, Any]]) -> str:
    """One-line TL;DR shown in the email summary callout."""
    parts: list[str] = []
    for scope_label in ("today", "overall"):
        s = summaries.get(scope_label)
        if s is None:
            continue
        t = s["totals"]
        csv_rows = t.get("csv_rows", 0)
        matched = t.get("matched", 0)
        rate = (matched / csv_rows * 100) if csv_rows else 0.0
        parts.append(f"{scope_label}: {matched}/{csv_rows} matched ({rate:.1f}%)")
    return "; ".join(parts) + "." if parts else "No comparison ran."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="Path to comparison CSV")
    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "Stable identifier for this comparison (default: UTC timestamp). "
            "For --scope=both, this is the base id; the today-pass writes to "
            "<run-id>__today and the overall-pass writes to <run-id>."
        ),
    )
    parser.add_argument(
        "--scope",
        choices=("today", "overall", "both"),
        default="both",
        help=(
            "Which comparisons to run. 'today' filters the DB plan pool to "
            "units last seen on --last-seen-on; 'overall' uses every unit; "
            "'both' (default) runs both and emails a combined report."
        ),
    )
    parser.add_argument(
        "--last-seen-on",
        default=None,
        help=(
            "YYYY-MM-DD date used by the today-scope filter "
            "(units.last_seen_at LIKE '<date>%%'). Default: today UTC."
        ),
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
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="Properties (most DB-only plans) to enumerate in each scope's section (default: 25).",
    )
    parser.add_argument(
        "--examples-per-method",
        type=int,
        default=5,
        help="Examples to show per match method in each scope's section (default: 5).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Output markdown path for the combined report. "
            "Default: ma_poc/data/reports/floor_plan_comparison_<run-id>.md"
        ),
    )
    parser.add_argument("--no-email", action="store_true",
                        help="Skip the email send. The markdown artifact is still written.")
    parser.add_argument("--email-recipients", default=None,
                        help="Comma-separated override of REPORT_RECIPIENTS.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render the email body to stdout but do not actually send.")
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

    base_run_id = args.run_id or _default_run_id()
    last_seen_on = args.last_seen_on or _default_last_seen_on()

    summaries = run_dual_comparison(
        csv_path=csv_path,
        base_run_id=base_run_id,
        name_threshold=args.threshold,
        area_buffer=args.buffer,
        last_seen_on=last_seen_on,
        scope=args.scope,
        database_url=args.database_url,
    )

    for scope_label in ("today", "overall"):
        if scope_label in summaries:
            _print_summary(scope_label, summaries[scope_label])

    # Build the combined markdown report. Always written to disk so the
    # artifact survives even when --no-email is set or the email transport
    # fails — operators can re-send manually from the saved file.
    combined_md = _build_combined_markdown(
        summaries=summaries,
        base_run_id=base_run_id,
        top_n_properties=args.top,
        examples_per_method=args.examples_per_method,
        database_url=args.database_url,
    )

    out = args.out or (
        _MA_POC_ROOT
        / "data"
        / "reports"
        / f"floor_plan_comparison_{base_run_id.replace(':', '-')}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(combined_md, encoding="utf-8")
    print(f"\nWrote {out} ({len(combined_md):,} chars)")

    if args.no_email:
        return 0

    from scripts.email.report import email_report

    recipients = (
        [r.strip() for r in args.email_recipients.split(",") if r.strip()]
        if args.email_recipients
        else None
    )
    result = email_report(
        subject=f"Floor Plan Comparison — {base_run_id}",
        summary=_build_email_summary(summaries),
        body_md=combined_md,
        attachments=[out],
        recipients=recipients,
        dry_run=args.dry_run,
    )
    if result.get("isError"):
        print(f"email send failed: {result.get('content')}", file=sys.stderr)
        return 1
    print(f"email sent: {result.get('content') or '(empty)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
