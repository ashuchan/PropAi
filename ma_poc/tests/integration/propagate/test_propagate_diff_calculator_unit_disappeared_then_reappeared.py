"""Propagate layer: diff calculator tracks unit disappearance and reappearance.

A unit that was in run N, missing from run N+1, and back in run N+2 should:
  - Show as disappeared_units in diff(N+1, N)
  - Show as new_available in diff(N+2, N+1) — exactly once, no duplication
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from data_provider.sql.models import Base, PropertySnapshotRow
from services.diff_calculator import compute_daily_diff


@pytest.fixture()
def session() -> Session:
    """In-memory SQLite session with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _snap(session: Session, run_date: str, prop_id: str, units: list[dict[str, Any]], ordinal: int = 1) -> None:
    row = PropertySnapshotRow(
        run_date=run_date,
        canonical_id=prop_id,
        ordinal=ordinal,
        payload={
            "Unique ID": prop_id,
            "Property Name": f"Prop {prop_id}",
            "units": units,
        },
    )
    session.add(row)
    session.commit()


_UNIT = {"unit_id": "U1", "market_rent_low": 1500, "market_rent_high": 1500}


# ---------------------------------------------------------------------------
# Disappearance
# ---------------------------------------------------------------------------


def test_disappeared_unit_counted_in_summary(session: Session) -> None:
    """A unit present in run 1 but absent in run 2 increments disappeared_units."""
    _snap(session, "2026-05-01", "PA", [_UNIT])
    _snap(session, "2026-05-02", "PA", [])  # unit gone

    diff = compute_daily_diff(session, "2026-05-02", "2026-05-01")

    assert diff.summary.disappeared_units == 1


def test_disappeared_unit_not_double_counted(session: Session) -> None:
    """Three distinct units disappear; each counts once."""
    units = [
        {"unit_id": f"U{i}", "market_rent_low": 1500 + i * 100}
        for i in range(3)
    ]
    _snap(session, "2026-05-01", "PB", units)
    _snap(session, "2026-05-02", "PB", [])

    diff = compute_daily_diff(session, "2026-05-02", "2026-05-01")

    assert diff.summary.disappeared_units == 3


# ---------------------------------------------------------------------------
# Reappearance
# ---------------------------------------------------------------------------


def test_reappeared_unit_shows_as_new_available(session: Session) -> None:
    """A unit absent in run 2 but back in run 3 is new_available in diff(3,2)."""
    _snap(session, "2026-05-01", "PC", [_UNIT])
    _snap(session, "2026-05-02", "PC", [])           # gone
    _snap(session, "2026-05-03", "PC", [_UNIT])      # back

    diff_gone = compute_daily_diff(session, "2026-05-02", "2026-05-01")
    diff_back = compute_daily_diff(session, "2026-05-03", "2026-05-02")

    assert diff_gone.summary.disappeared_units == 1
    assert diff_back.summary.new_available == 1


def test_reappeared_unit_counted_exactly_once(session: Session) -> None:
    """The reappeared unit must appear exactly once in new_available — no duplication."""
    _snap(session, "2026-05-01", "PD", [_UNIT])
    _snap(session, "2026-05-02", "PD", [])
    _snap(session, "2026-05-03", "PD", [_UNIT])

    diff = compute_daily_diff(session, "2026-05-03", "2026-05-02")

    assert diff.summary.new_available == 1, (
        f"Expected exactly 1 new_available but got {diff.summary.new_available}"
    )


# ---------------------------------------------------------------------------
# Stable units produce no noise
# ---------------------------------------------------------------------------


def test_stable_unit_produces_no_diff_noise(session: Session) -> None:
    """A unit that stays across three runs produces no disappeared or new entries."""
    _snap(session, "2026-05-01", "PE", [_UNIT])
    _snap(session, "2026-05-02", "PE", [_UNIT])
    _snap(session, "2026-05-03", "PE", [_UNIT])

    diff_ab = compute_daily_diff(session, "2026-05-02", "2026-05-01")
    diff_bc = compute_daily_diff(session, "2026-05-03", "2026-05-02")

    assert diff_ab.summary.disappeared_units == 0
    assert diff_ab.summary.new_available == 0
    assert diff_bc.summary.disappeared_units == 0
    assert diff_bc.summary.new_available == 0
