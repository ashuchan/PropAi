"""Propagate layer: diff calculator detects rent changes between runs.

Verifies that `compute_daily_diff()` correctly identifies rent increases and
decreases when the same unit appears in two consecutive run snapshots with
different rent values.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from data_provider.sql.models import Base, PropertySnapshotRow
from services.diff_calculator import DailyDiff, compute_daily_diff


@pytest.fixture()
def session() -> Session:
    """In-memory SQLite session with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


_ordinal_counter: dict[str, int] = {}


def _insert_snapshot(session: Session, run_date: str, prop_id: str, units: list[dict[str, Any]]) -> None:
    """Insert a PropertySnapshotRow with the given run_date and units."""
    key = run_date
    _ordinal_counter[key] = _ordinal_counter.get(key, 0) + 1
    row = PropertySnapshotRow(
        run_date=run_date,
        canonical_id=prop_id,
        ordinal=_ordinal_counter[key],
        payload={
            "Unique ID": prop_id,
            "Property Name": f"Test Property {prop_id}",
            "units": units,
        },
    )
    session.add(row)
    session.commit()


# ---------------------------------------------------------------------------
# Rent increase
# ---------------------------------------------------------------------------


def test_rent_increase_detected(session: Session) -> None:
    """A unit whose rent rises from $1500 to $1700 appears in rent_changes with direction='up'."""
    _insert_snapshot(session, "2026-05-01", "P1", [
        {"unit_id": "U1", "market_rent_low": 1500, "market_rent_high": 1500},
    ])
    _insert_snapshot(session, "2026-05-02", "P1", [
        {"unit_id": "U1", "market_rent_low": 1700, "market_rent_high": 1700},
    ])

    diff = compute_daily_diff(session, "2026-05-02", "2026-05-01")

    assert diff.summary.rents_increased == 1
    assert diff.summary.rents_decreased == 0
    assert len(diff.rent_changes) == 1
    rc = diff.rent_changes[0]
    assert rc.direction == "up"
    assert rc.unit_id == "U1"
    assert rc.current_rent > rc.previous_rent


def test_rent_decrease_detected(session: Session) -> None:
    """A unit whose rent drops from $2000 to $1700 appears in rent_changes with direction='down'."""
    _insert_snapshot(session, "2026-05-01", "P2", [
        {"unit_id": "U2", "market_rent_low": 2000, "market_rent_high": 2000},
    ])
    _insert_snapshot(session, "2026-05-02", "P2", [
        {"unit_id": "U2", "market_rent_low": 1700, "market_rent_high": 1700},
    ])

    diff = compute_daily_diff(session, "2026-05-02", "2026-05-01")

    assert diff.summary.rents_decreased == 1
    rc = diff.rent_changes[0]
    assert rc.direction == "down"


def test_unchanged_rent_not_flagged(session: Session) -> None:
    """A unit with identical rent on both days produces no rent_changes entry."""
    _insert_snapshot(session, "2026-05-01", "P3", [
        {"unit_id": "U3", "market_rent_low": 1800, "market_rent_high": 1800},
    ])
    _insert_snapshot(session, "2026-05-02", "P3", [
        {"unit_id": "U3", "market_rent_low": 1800, "market_rent_high": 1800},
    ])

    diff = compute_daily_diff(session, "2026-05-02", "2026-05-01")

    assert diff.summary.rents_increased == 0
    assert diff.summary.rents_decreased == 0
    assert len(diff.rent_changes) == 0


def test_new_unit_counted_as_new_available(session: Session) -> None:
    """A unit that appears in the current run but not the prior run is new_available."""
    _insert_snapshot(session, "2026-05-01", "P4", [
        {"unit_id": "U4a", "market_rent_low": 1500, "market_rent_high": 1500},
    ])
    _insert_snapshot(session, "2026-05-02", "P4", [
        {"unit_id": "U4a", "market_rent_low": 1500, "market_rent_high": 1500},
        {"unit_id": "U4b", "market_rent_low": 1600, "market_rent_high": 1600},
    ])

    diff = compute_daily_diff(session, "2026-05-02", "2026-05-01")

    assert diff.summary.new_available == 1


def test_missing_previous_date_returns_empty_diff(session: Session) -> None:
    """With no previous_date, an empty DailyDiff is returned without error."""
    _insert_snapshot(session, "2026-05-01", "P5", [
        {"unit_id": "U5", "market_rent_low": 1500, "market_rent_high": 1500},
    ])

    diff = compute_daily_diff(session, "2026-05-01", None)

    assert isinstance(diff, DailyDiff)
    assert diff.summary.rents_increased == 0
    assert len(diff.rent_changes) == 0
