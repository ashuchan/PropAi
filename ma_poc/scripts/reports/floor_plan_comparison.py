"""Generate a markdown report from a `floor_plan_comparison_*` run.

Reads the latest (or `--run-id`) row from `floor_plan_comparison_runs` and
the per-row decisions from `floor_plan_comparison_rows`, recomputes each
property's DB floor plans (same dedup as compare_floor_plans_csv.py) so
we can enumerate the floor plans **present in DB but missing from CSV**,
and writes a markdown report covering:

  - Summary (totals, match rate, DB-only count)
  - Key matches (per-method counts + a handful of examples)
  - Key unmatches (CSV rows with no DB match + property_not_found)
  - Examples (one per match method)
  - DB-only floor plans — properties whose DB has plans the CSV never
    referenced, listed by property with name/beds/baths/area/unit_count

Usage:
    python ma_poc/scripts/report_floor_plan_comparison.py
    python ma_poc/scripts/report_floor_plan_comparison.py --run-id 2026-05-05T16-45-58Z
    python ma_poc/scripts/report_floor_plan_comparison.py --top 50 --out report.md
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import select, text
from sqlalchemy.orm import Session

_HERE = Path(__file__).resolve()
_MA_POC_ROOT = _HERE.parent.parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

load_dotenv(_MA_POC_ROOT / ".env")

# DATABASE_URL in .env uses the asyncpg driver (the FastAPI service is
# async). This script is sync — swap to pg8000 before make_engine reads
# the env var. pg8000 is pure-Python and ships with Cloud SQL connector
# support, so it's already a project dep.
_db_url = os.environ.get("DATABASE_URL", "")
if "+asyncpg" in _db_url:
    os.environ["DATABASE_URL"] = _db_url.replace("+asyncpg", "+pg8000")

from data_provider.sql.engine import make_engine, make_session_factory  # noqa: E402
from data_provider.sql.models import (  # noqa: E402
    FloorPlanComparisonRowRow,
    FloorPlanComparisonRunRow,
    UnitRow,
)

log = logging.getLogger("report_floor_plan_comparison")


# ── Recompute DB floor plans (mirrors compare_floor_plans_csv.py) ──────────


@dataclass
class DbFloorPlan:
    name: str | None
    beds: int | None
    baths: float | None
    area: int | None
    unit_count: int


def _load_db_floor_plans(session: Session, canonical_id: str) -> list[DbFloorPlan]:
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
        key = (
            (r.floor_plan_name or "").strip().lower(),
            int(r.beds) if r.beds is not None else None,
            float(r.baths) if r.baths is not None else None,
            int(r.area) if r.area is not None else None,
        )
        groups[key].append(r)

    plans: list[DbFloorPlan] = []
    for (_, beds, baths, area), bucket in groups.items():
        name = next(
            (b.floor_plan_name for b in bucket if b.floor_plan_name and b.floor_plan_name.strip()),
            None,
        )
        plans.append(DbFloorPlan(name=name, beds=beds, baths=baths, area=area, unit_count=len(bucket)))
    return plans


# ── Report rendering ───────────────────────────────────────────────────────


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _pct(num: int, denom: int) -> str:
    if not denom:
        return "0.00%"
    return f"{(num / denom) * 100:.2f}%"


def _resolve_run_id(session: Session, run_id: str | None) -> str:
    if run_id:
        return run_id
    latest = session.execute(
        text(
            "SELECT run_id FROM floor_plan_comparison_runs "
            "GROUP BY run_id ORDER BY MAX(created_at) DESC LIMIT 1"
        )
    ).scalar()
    if not latest:
        raise SystemExit("No floor_plan_comparison_runs found in DB.")
    return str(latest)


def build_report(
    session: Session,
    run_id: str,
    top_n_properties: int,
    examples_per_method: int,
) -> str:
    # ── Per-property aggregate rows for this run.
    run_rows = session.execute(
        select(FloorPlanComparisonRunRow).where(FloorPlanComparisonRunRow.run_id == run_id)
    ).scalars().all()

    # ── All per-row decisions for this run.
    decision_rows = session.execute(
        select(FloorPlanComparisonRowRow).where(FloorPlanComparisonRowRow.run_id == run_id)
    ).scalars().all()

    if not run_rows and not decision_rows:
        raise SystemExit(f"No rows in either table for run_id={run_id!r}")

    # ── Run-level totals.
    totals = {
        "csv_rows": 0,
        "matched": 0,
        "name": 0,
        "bb_sqft": 0,
        "bb_only": 0,
        "unmatched": 0,
        "property_not_found": 0,
        "sqft_within_buffer": 0,
        "missing_in_csv_count": 0,
        "db_floor_plans_total": 0,
        "properties_compared": len(run_rows),
        "properties_with_any_match": sum(1 for r in run_rows if r.matched_total > 0),
        "properties_with_db_only": sum(1 for r in run_rows if r.missing_in_csv_count > 0),
    }
    for r in run_rows:
        totals["csv_rows"] += r.csv_rows_total
        totals["matched"] += r.matched_total
        totals["name"] += r.name_match_count
        totals["bb_sqft"] += r.bed_bath_sqft_match_count
        totals["bb_only"] += r.bed_bath_only_match_count
        totals["unmatched"] += r.unmatched_count
        totals["sqft_within_buffer"] += r.sqft_within_buffer_count
        totals["missing_in_csv_count"] += r.missing_in_csv_count
        totals["db_floor_plans_total"] += r.db_floor_plans_total
    totals["property_not_found"] = sum(
        1 for d in decision_rows if d.match_method == "property_not_found"
    )

    # ── Group decisions by (canonical_id, match_method).
    by_property: dict[str | None, list[FloorPlanComparisonRowRow]] = defaultdict(list)
    for d in decision_rows:
        by_property[d.canonical_id].append(d)

    # ── Examples: pick a few decisions per method (best-scoring first).
    method_buckets: dict[str, list[FloorPlanComparisonRowRow]] = defaultdict(list)
    for d in decision_rows:
        method_buckets[d.match_method].append(d)

    def _ex(rows: list[FloorPlanComparisonRowRow], k: int) -> list[FloorPlanComparisonRowRow]:
        # name_semantic: highest score first; sqft: tightest |delta| first;
        # else: just take first k.
        if not rows:
            return []
        if rows[0].match_method == "name_semantic":
            return sorted(rows, key=lambda r: -(r.match_score or 0))[:k]
        if rows[0].match_method == "bed_bath_sqft":
            return sorted(rows, key=lambda r: abs(r.sqft_delta) if r.sqft_delta is not None else 9999)[:k]
        return rows[:k]

    # ── DB-only floor plans: for each property, load DB plans, subtract
    # the (name, beds, baths, area) keys we already matched against. Cap
    # at top_n_properties properties to keep the report bounded — but
    # always emit a global count.
    log.info("Resolving DB-only floor plans for %d properties...", len(run_rows))
    sorted_props = sorted(
        run_rows,
        key=lambda r: (-r.missing_in_csv_count, r.canonical_id),
    )
    db_only_per_property: list[tuple[FloorPlanComparisonRunRow, list[DbFloorPlan]]] = []
    for prop in sorted_props:
        if prop.missing_in_csv_count <= 0:
            continue
        if len(db_only_per_property) >= top_n_properties:
            break
        matched_keys: set[tuple[str, int | None, float | None, int | None]] = set()
        for d in by_property.get(prop.canonical_id, []):
            if d.match_method in ("unmatched", "property_not_found"):
                continue
            matched_keys.add(
                (
                    (d.db_floor_plan_name or "").strip().lower(),
                    d.db_beds,
                    d.db_baths,
                    d.db_area,
                )
            )
        db_plans = _load_db_floor_plans(session, prop.canonical_id)
        missing = [
            p for p in db_plans
            if ((p.name or "").strip().lower(), p.beds, p.baths, p.area) not in matched_keys
        ]
        if missing:
            missing.sort(key=lambda p: (-(p.unit_count or 0), p.name or ""))
            db_only_per_property.append((prop, missing))

    # ── Render markdown.
    lines: list[str] = []
    lines.append(f"# Floor Plan Comparison Report — `{run_id}`")
    lines.append("")
    lines.append(f"_Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_")
    lines.append("")

    # ── Summary
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| CSV rows | **{totals['csv_rows']:,}** |")
    lines.append(f"| Matched (any method) | **{totals['matched']:,}** ({_pct(totals['matched'], totals['csv_rows'])}) |")
    lines.append(f"| &nbsp;&nbsp;name_semantic | {totals['name']:,} |")
    lines.append(f"| &nbsp;&nbsp;bed_bath_sqft | {totals['bb_sqft']:,} |")
    lines.append(f"| &nbsp;&nbsp;bed_bath_only | {totals['bb_only']:,} |")
    lines.append(f"| Unmatched | {totals['unmatched']:,} ({_pct(totals['unmatched'], totals['csv_rows'])}) |")
    lines.append(f"| Property not found | {totals['property_not_found']:,} |")
    lines.append(f"| Sqft within buffer | {totals['sqft_within_buffer']:,} |")
    lines.append(f"| Properties compared | {totals['properties_compared']:,} |")
    lines.append(f"| Properties with ≥1 match | {totals['properties_with_any_match']:,} ({_pct(totals['properties_with_any_match'], totals['properties_compared'])}) |")
    lines.append(f"| DB floor plans (total) | {totals['db_floor_plans_total']:,} |")
    lines.append(f"| **DB floor plans missing in CSV** | **{totals['missing_in_csv_count']:,}** ({_pct(totals['missing_in_csv_count'], totals['db_floor_plans_total'])}) |")
    lines.append(f"| Properties with DB-only floor plans | {totals['properties_with_db_only']:,} |")
    lines.append("")

    # ── Key matches
    lines.append("## Key matches")
    lines.append("")
    lines.append(
        "Match cascade: `name_semantic` (rapidfuzz token_set_ratio ≥ threshold, gated on "
        "bed/bath) → `bed_bath_sqft` (bed/bath exact + |Δsqft| ≤ buffer) → `bed_bath_only` "
        "(bed/bath exact, area ignored) → `unmatched`."
    )
    lines.append("")
    lines.append("| Method | Count | Share of CSV rows |")
    lines.append("|---|---:|---:|")
    lines.append(f"| name_semantic | {totals['name']:,} | {_pct(totals['name'], totals['csv_rows'])} |")
    lines.append(f"| bed_bath_sqft | {totals['bb_sqft']:,} | {_pct(totals['bb_sqft'], totals['csv_rows'])} |")
    lines.append(f"| bed_bath_only | {totals['bb_only']:,} | {_pct(totals['bb_only'], totals['csv_rows'])} |")
    lines.append("")

    # ── Key unmatches
    lines.append("## Key unmatches")
    lines.append("")
    lines.append("CSV rows that found no DB plan, and CSV rows where the property itself wasn't in the DB.")
    lines.append("")
    lines.append("| Failure mode | Count | Share of CSV rows |")
    lines.append("|---|---:|---:|")
    lines.append(f"| unmatched (property exists, no plan match) | {totals['unmatched']:,} | {_pct(totals['unmatched'], totals['csv_rows'])} |")
    lines.append(f"| property_not_found | {totals['property_not_found']:,} | {_pct(totals['property_not_found'], totals['csv_rows'])} |")
    lines.append("")

    # Top properties driving unmatched volume.
    unmatched_by_prop = [
        (r, r.unmatched_count) for r in run_rows if r.unmatched_count > 0
    ]
    unmatched_by_prop.sort(key=lambda x: (-x[1], x[0].canonical_id))
    if unmatched_by_prop:
        lines.append("**Top 10 properties by unmatched CSV rows:**")
        lines.append("")
        lines.append("| Property | apartment_id | CSV rows | Matched | Unmatched | DB plans |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for prop, _ in unmatched_by_prop[:10]:
            lines.append(
                f"| {_fmt(prop.proj_name)} | {_fmt(prop.apartment_id)} | "
                f"{prop.csv_rows_total} | {prop.matched_total} | "
                f"{prop.unmatched_count} | {prop.db_floor_plans_total} |"
            )
        lines.append("")

    # ── Examples (one mini-table per method)
    lines.append("## Examples")
    lines.append("")
    for method in ("name_semantic", "bed_bath_sqft", "bed_bath_only", "unmatched", "property_not_found"):
        sample = _ex(method_buckets.get(method, []), examples_per_method)
        if not sample:
            continue
        lines.append(f"### `{method}` — {len(method_buckets[method]):,} rows")
        lines.append("")
        if method == "property_not_found":
            lines.append("| CSV apt_id | floorplan# | description | bed | bath | area |")
            lines.append("|---:|---|---|---:|---:|---:|")
            for d in sample:
                lines.append(
                    f"| {_fmt(d.csv_apartment_id)} | {_fmt(d.csv_floorplan_number)} | "
                    f"{_fmt(d.csv_description)} | {_fmt(d.csv_bed)} | {_fmt(d.csv_bath)} | {_fmt(d.csv_area)} |"
                )
        elif method == "unmatched":
            lines.append("| canonical_id | apt_id | floorplan# | description | bed/bath/area |")
            lines.append("|---|---:|---|---|---|")
            for d in sample:
                bba = f"{_fmt(d.csv_bed)}/{_fmt(d.csv_bath)}/{_fmt(d.csv_area)}"
                lines.append(
                    f"| {_fmt(d.canonical_id)} | {_fmt(d.csv_apartment_id)} | "
                    f"{_fmt(d.csv_floorplan_number)} | {_fmt(d.csv_description)} | {bba} |"
                )
        else:
            lines.append("| CSV (description, b/b/area) | DB (name, b/b/area) | score | Δsqft |")
            lines.append("|---|---|---:|---:|")
            for d in sample:
                csv_side = (
                    f"{_fmt(d.csv_description)} ({_fmt(d.csv_bed)}/{_fmt(d.csv_bath)}/{_fmt(d.csv_area)})"
                )
                db_side = (
                    f"{_fmt(d.db_floor_plan_name)} ({_fmt(d.db_beds)}/{_fmt(d.db_baths)}/{_fmt(d.db_area)})"
                )
                lines.append(
                    f"| {csv_side} | {db_side} | {_fmt(d.match_score)} | {_fmt(d.sqft_delta)} |"
                )
        lines.append("")

    # ── DB-only floor plans (highlighted section)
    lines.append("## ⭐ Floor plans in DB but NOT in CSV")
    lines.append("")
    lines.append(
        f"**{totals['missing_in_csv_count']:,}** distinct DB floor-plan groupings across "
        f"**{totals['properties_with_db_only']:,}** properties were not referenced by any CSV row. "
        f"A grouping is `(floor_plan_name, beds, baths, area)` from the `units` table; "
        f"`unit_count` is the number of underlying unit records in that grouping."
    )
    lines.append("")
    lines.append(
        f"_Showing the top **{len(db_only_per_property)}** properties by DB-only "
        f"floor-plan count (use `--top` to see more)._"
    )
    lines.append("")

    for prop, missing in db_only_per_property:
        lines.append(
            f"### `{prop.canonical_id}` · {_fmt(prop.proj_name)} · apt_id {_fmt(prop.apartment_id)}"
        )
        lines.append("")
        lines.append(
            f"DB has **{prop.db_floor_plans_total}** floor plans, "
            f"CSV had **{prop.csv_rows_total}** rows; "
            f"**{len(missing)}** DB plans never matched a CSV row."
        )
        lines.append("")
        lines.append("| Floor plan name | Beds | Baths | Area (sqft) | Units |")
        lines.append("|---|---:|---:|---:|---:|")
        for p in missing:
            lines.append(
                f"| {_fmt(p.name)} | {_fmt(p.beds)} | {_fmt(p.baths)} | {_fmt(p.area)} | {p.unit_count} |"
            )
        lines.append("")

    return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id",
        default=None,
        help="Comparison run id (default: most recent in DB).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        help="How many properties (most DB-only plans) to enumerate (default: 25).",
    )
    parser.add_argument(
        "--examples-per-method",
        type=int,
        default=5,
        help="Examples to show per match method (default: 5).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output markdown path. Default: ma_poc/data/reports/floor_plan_comparison_{run_id}.md",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-email", action="store_true",
                        help="Skip the email send. The markdown artifact is still written.")
    parser.add_argument("--email-recipients", default=None,
                        help="Comma-separated override of REPORT_RECIPIENTS.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render the email body to stdout but do not actually send.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    engine = make_engine()
    SessionLocal = make_session_factory(engine)

    with SessionLocal() as session:
        run_id = _resolve_run_id(session, args.run_id)
        log.info("Building report for run_id=%s", run_id)
        md = build_report(
            session=session,
            run_id=run_id,
            top_n_properties=args.top,
            examples_per_method=args.examples_per_method,
        )
        # While the session is still open, capture run-level totals so the
        # email summary can quote real numbers without re-opening the DB.
        run_row = session.execute(
            select(FloorPlanComparisonRunRow).where(
                FloorPlanComparisonRunRow.run_id == run_id
            )
        ).scalar_one_or_none()
        summary_stats = _collect_summary_stats(session, run_id) if run_row else {}

    out = args.out or (
        _MA_POC_ROOT
        / "data"
        / "reports"
        / f"floor_plan_comparison_{run_id.replace(':', '-')}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote {out} ({len(md):,} chars)")

    if args.no_email:
        return 0

    from scripts.email.report import email_report

    recipients = (
        [r.strip() for r in args.email_recipients.split(",") if r.strip()]
        if args.email_recipients
        else None
    )
    summary = _build_email_summary(run_id, summary_stats)
    result = email_report(
        subject=f"Floor Plan Comparison — {run_id}",
        summary=summary,
        body_md=md,
        attachments=[out],
        recipients=recipients,
        dry_run=args.dry_run,
    )
    if result.get("isError"):
        print(f"email send failed: {result.get('content')}", file=sys.stderr)
        return 1
    print(f"email sent: {result.get('content') or '(empty)'}")
    return 0


def _collect_summary_stats(session: Session, run_id: str) -> dict[str, int]:
    """Headline numbers for the email TL;DR.

    Re-issues the same ``SELECT FloorPlanComparisonRunRow WHERE run_id=?``
    that ``build_report`` already ran — one extra round-trip on the same
    session. We could thread the rows down from ``build_report`` to skip
    it, but the win is one cached query at run end and the extra arg
    would clutter the rendering pipeline. Keeping the duplicated query
    until either query cost or session-pinning becomes a real concern.
    """
    rows = session.execute(
        select(FloorPlanComparisonRunRow).where(
            FloorPlanComparisonRunRow.run_id == run_id
        )
    ).scalars().all()
    return {
        "csv_rows": sum(r.csv_rows_total for r in rows),
        "matched": sum(r.matched_total for r in rows),
        "unmatched": sum(r.unmatched_count for r in rows),
        "missing_in_csv": sum(r.missing_in_csv_count for r in rows),
        "properties": len(rows),
    }


def _build_email_summary(run_id: str, stats: dict[str, int]) -> str:
    """One-line TL;DR for the floor-plan comparison email."""
    if not stats:
        return f"Floor plan comparison run {run_id} — no rows found."
    csv_rows = stats["csv_rows"]
    matched = stats["matched"]
    rate = (matched / csv_rows * 100) if csv_rows else 0.0
    parts = [
        f"{matched}/{csv_rows} CSV rows matched ({rate:.1f}%)",
        f"{stats['properties']} properties",
    ]
    if stats["unmatched"]:
        parts.append(f"{stats['unmatched']} unmatched")
    if stats["missing_in_csv"]:
        parts.append(f"{stats['missing_in_csv']} DB-only plans")
    return ". ".join(parts) + "."


if __name__ == "__main__":
    sys.exit(main())
