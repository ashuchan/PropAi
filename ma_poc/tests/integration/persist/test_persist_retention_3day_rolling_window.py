"""Persist layer: 3-day retention window deletes old run data.

Verifies that `_apply_retention()` removes rows older than 3 days from
`property_snapshots` and leaves recent rows untouched.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from data_provider.sql.models import Base, PropertySnapshotRow
from scripts.sync.run_to_pg import _apply_retention


def _today() -> str:
    return date.today().isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


@pytest.fixture
def engine():
    """In-memory SQLite engine with all tables created."""
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def _insert_snapshot(engine, run_date: str, prop_id: str) -> None:
    with Session(engine) as session:
        row = PropertySnapshotRow(
            run_date=run_date,
            canonical_id=prop_id,
            ordinal=1,
            payload={"Unique ID": prop_id},
        )
        session.add(row)
        session.commit()


def _count_snapshots(engine, run_date: str) -> int:
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT COUNT(*) FROM property_snapshots WHERE run_date = :d"),
            {"d": run_date},
        )
        return result.scalar() or 0


# ---------------------------------------------------------------------------
# Retention enforcement
# ---------------------------------------------------------------------------


def test_old_snapshot_deleted_by_retention(engine) -> None:
    """A property snapshot with run_date older than 3 days is deleted."""
    old_date = _days_ago(5)
    _insert_snapshot(engine, old_date, "P_OLD")

    assert _count_snapshots(engine, old_date) == 1

    _apply_retention(engine)

    assert _count_snapshots(engine, old_date) == 0


def test_recent_snapshot_kept_by_retention(engine) -> None:
    """A property snapshot from today is not deleted by retention."""
    today = _today()
    _insert_snapshot(engine, today, "P_TODAY")

    _apply_retention(engine)

    assert _count_snapshots(engine, today) == 1


def test_boundary_exactly_3_days_ago_is_deleted(engine) -> None:
    """A snapshot exactly 3 days old falls before the cutoff and is deleted."""
    boundary = _days_ago(4)  # strictly older than cutoff (today - 3 days)
    _insert_snapshot(engine, boundary, "P_BOUNDARY")

    _apply_retention(engine)

    assert _count_snapshots(engine, boundary) == 0


def test_retention_returns_deleted_counts(engine) -> None:
    """_apply_retention returns a dict with 'property_snapshots' key."""
    old_date = _days_ago(5)
    _insert_snapshot(engine, old_date, "P_COUNT")

    deleted = _apply_retention(engine)

    assert "property_snapshots" in deleted
    assert deleted["property_snapshots"] >= 1


def test_retention_idempotent_on_second_call(engine) -> None:
    """Calling _apply_retention twice does not raise and deletes nothing the second time."""
    old_date = _days_ago(5)
    _insert_snapshot(engine, old_date, "P_IDEM")

    _apply_retention(engine)
    deleted2 = _apply_retention(engine)

    assert deleted2["property_snapshots"] == 0
