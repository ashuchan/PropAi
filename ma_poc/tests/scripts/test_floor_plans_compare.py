"""Tests for `scripts/floor_plans/compare.py`.

Covers the two pieces of the dual-scope refactor that are easy to get wrong:

  1. `_load_db_floor_plans(..., last_seen_on=...)` must filter the units
     pool by the ISO date prefix on `last_seen_at`. Units last seen on a
     different day are excluded; units with NULL `last_seen_at` are
     excluded; an unfiltered call (`last_seen_on=None`) returns the
     full set.

  2. `run_dual_comparison(scope="both")` must write distinct run-ids to
     `floor_plan_comparison_runs` — the today-scope under
     `<base>__today`, the overall-scope under `<base>` — and the two
     summaries must reflect the scope filter (today bucketing properties
     as `db_empty` when nothing was upserted today, overall bucketing
     them as matched).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from data_provider.sql.engine import make_engine, make_session_factory
from data_provider.sql.models import (
    Base,
    FloorPlanComparisonRunRow,
    PropertyRow,
    UnitRow,
)
from scripts.floor_plans.compare import (
    TODAY_SUFFIX,
    _load_db_floor_plans,
    run_dual_comparison,
)

TODAY = "2026-05-11"
YESTERDAY = "2026-05-10"

CSV_CONTENT = (
    "apartmentid,floorplannumber,description,bed,bath,area\n"
    # P-100 has one plan today + one plan only seen yesterday.
    "100,A1,Alpha One,1,1.0,720\n"
    "100,B2,Beta Two,2,2.0,1100\n"
    # P-200 has nothing seen today (carry-forward gap). Overall it has
    # one matching plan.
    "200,C3,Cedar Three,1,1.0,650\n"
)


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'compare_test.db'}"


@pytest.fixture
def seeded_session(sqlite_url: str) -> Session:
    """Empty SQLite DB with the compare-relevant tables and seed rows."""
    engine = make_engine(sqlite_url)
    Base.metadata.create_all(engine)
    SessionLocal = make_session_factory(engine)
    with SessionLocal() as session:
        session.add(
            PropertyRow(
                canonical_id="P-100",
                apartment_id=100,
                proj_name="Property Hundred",
            )
        )
        session.add(
            PropertyRow(
                canonical_id="P-200",
                apartment_id=200,
                proj_name="Property Two-Hundred",
            )
        )
        # P-100: plan A1 seen TODAY, plan B2 seen only YESTERDAY.
        session.add(
            UnitRow(
                canonical_id="P-100",
                unit_id="u-1",
                floor_plan_name="Alpha One",
                beds=1,
                baths=1.0,
                area=720,
                last_seen_at=f"{TODAY}T03:14:00Z",
            )
        )
        session.add(
            UnitRow(
                canonical_id="P-100",
                unit_id="u-2",
                floor_plan_name="Beta Two",
                beds=2,
                baths=2.0,
                area=1100,
                last_seen_at=f"{YESTERDAY}T03:14:00Z",
            )
        )
        # P-200: only seen YESTERDAY — today-scope must treat as db_empty.
        session.add(
            UnitRow(
                canonical_id="P-200",
                unit_id="u-3",
                floor_plan_name="Cedar Three",
                beds=1,
                baths=1.0,
                area=650,
                last_seen_at=f"{YESTERDAY}T08:00:00Z",
            )
        )
        # Decoy: NULL last_seen_at must never match a today filter.
        session.add(
            UnitRow(
                canonical_id="P-100",
                unit_id="u-4",
                floor_plan_name="Ghost Plan",
                beds=3,
                baths=2.5,
                area=1400,
                last_seen_at=None,
            )
        )
        session.commit()
        yield session


@pytest.fixture
def csv_path(tmp_path: Path) -> Path:
    p = tmp_path / "floorplans.csv"
    p.write_text(CSV_CONTENT, encoding="utf-8")
    return p


# ── _load_db_floor_plans filter ────────────────────────────────────────────


class TestLoadDbFloorPlans:
    def test_unfiltered_returns_all_groups_including_null_last_seen(
        self, seeded_session: Session
    ) -> None:
        plans = _load_db_floor_plans(seeded_session, "P-100", last_seen_on=None)
        names = {p.name for p in plans}
        # All three plans for P-100 are present: Alpha One, Beta Two, Ghost Plan.
        assert names == {"Alpha One", "Beta Two", "Ghost Plan"}

    def test_today_filter_excludes_yesterday_and_null(
        self, seeded_session: Session
    ) -> None:
        plans = _load_db_floor_plans(seeded_session, "P-100", last_seen_on=TODAY)
        names = {p.name for p in plans}
        # Only Alpha One was last_seen on TODAY.
        assert names == {"Alpha One"}

    def test_today_filter_on_property_with_no_today_units_returns_empty(
        self, seeded_session: Session
    ) -> None:
        plans = _load_db_floor_plans(seeded_session, "P-200", last_seen_on=TODAY)
        assert plans == []

    def test_yesterday_filter_picks_up_yesterday_only(
        self, seeded_session: Session
    ) -> None:
        plans = _load_db_floor_plans(seeded_session, "P-100", last_seen_on=YESTERDAY)
        names = {p.name for p in plans}
        assert names == {"Beta Two"}


# ── run_dual_comparison orchestration ──────────────────────────────────────


class TestRunDualComparison:
    def test_both_writes_two_run_ids_with_distinct_summaries(
        self, sqlite_url: str, seeded_session: Session, csv_path: Path
    ) -> None:
        # The fixture has already created the schema and seeded rows on
        # the same SQLite file. Close the fixture's session so the
        # orchestrator's own session can take over the file lock cleanly.
        seeded_session.close()

        base_run_id = f"unit-test-{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}"
        summaries = run_dual_comparison(
            csv_path=csv_path,
            base_run_id=base_run_id,
            name_threshold=85.0,
            area_buffer=35,
            last_seen_on=TODAY,
            scope="both",
            database_url=sqlite_url,
        )

        assert set(summaries.keys()) == {"today", "overall"}
        assert summaries["today"]["run_id"] == f"{base_run_id}{TODAY_SUFFIX}"
        assert summaries["overall"]["run_id"] == base_run_id
        assert summaries["today"]["last_seen_on"] == TODAY
        assert summaries["overall"]["last_seen_on"] is None

        # Overall matches more rows than today (today excludes plans only
        # seen yesterday and the P-200 property entirely as db_empty).
        assert (
            summaries["overall"]["totals"]["matched"]
            > summaries["today"]["totals"]["matched"]
        )
        # P-200 contributes to db_empty in the today scope (had no units
        # today) but not in the overall scope (one unit on record).
        assert summaries["today"]["totals"]["db_empty"] >= 1
        assert summaries["overall"]["totals"]["db_empty"] == 0

        # Persistence check: both run_ids landed in the comparison-runs table.
        engine = make_engine(sqlite_url)
        SessionLocal = make_session_factory(engine)
        with SessionLocal() as session:
            persisted = {
                r.run_id
                for r in session.execute(
                    select(FloorPlanComparisonRunRow)
                ).scalars().all()
            }
        assert base_run_id in persisted
        assert f"{base_run_id}{TODAY_SUFFIX}" in persisted

    def test_scope_overall_writes_only_base_run_id(
        self, sqlite_url: str, seeded_session: Session, csv_path: Path
    ) -> None:
        seeded_session.close()
        base_run_id = "single-scope-overall"
        summaries = run_dual_comparison(
            csv_path=csv_path,
            base_run_id=base_run_id,
            name_threshold=85.0,
            area_buffer=35,
            last_seen_on=TODAY,
            scope="overall",
            database_url=sqlite_url,
        )
        assert set(summaries.keys()) == {"overall"}

        engine = make_engine(sqlite_url)
        SessionLocal = make_session_factory(engine)
        with SessionLocal() as session:
            persisted = {
                r.run_id
                for r in session.execute(
                    select(FloorPlanComparisonRunRow)
                ).scalars().all()
            }
        assert persisted == {base_run_id}

    def test_scope_today_writes_only_today_suffix(
        self, sqlite_url: str, seeded_session: Session, csv_path: Path
    ) -> None:
        seeded_session.close()
        base_run_id = "single-scope-today"
        summaries = run_dual_comparison(
            csv_path=csv_path,
            base_run_id=base_run_id,
            name_threshold=85.0,
            area_buffer=35,
            last_seen_on=TODAY,
            scope="today",
            database_url=sqlite_url,
        )
        assert set(summaries.keys()) == {"today"}

        engine = make_engine(sqlite_url)
        SessionLocal = make_session_factory(engine)
        with SessionLocal() as session:
            persisted = {
                r.run_id
                for r in session.execute(
                    select(FloorPlanComparisonRunRow)
                ).scalars().all()
            }
        assert persisted == {f"{base_run_id}{TODAY_SUFFIX}"}

    def test_invalid_scope_raises(
        self, sqlite_url: str, csv_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="scope must be"):
            run_dual_comparison(
                csv_path=csv_path,
                base_run_id="x",
                name_threshold=85.0,
                area_buffer=35,
                last_seen_on=TODAY,
                scope="invalid-scope",
                database_url=sqlite_url,
            )
