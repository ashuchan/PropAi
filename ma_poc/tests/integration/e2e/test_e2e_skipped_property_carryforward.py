"""E2E: change-detection SKIPPED → carryforward_days increments.

When a scrape run detects no change (SKIPPED verdict), instead of
re-inserting units the runner calls carry_forward_units().  This test
verifies the full seam:

  - Units upserted on run D-1 (baseline)
  - carry_forward_units() called on run D (simulates SKIPPED verdict)
  - carryforward_days increments from 0 to 1
  - A second carry-forward increments to 2
  - run_report reflects the property (carried forward units count as present)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_provider import SqliteDataProvider
from reporting.run_report import build as build_run_report


BASELINE_DATE = "2026-05-09"
RUN_DATE = "2026-05-10"
RUN_DATE_2 = "2026-05-11"
PROP_ID = "E2E-CF-001"
UNIT_ID = "U-CARRY"


@pytest.fixture
def sqlite() -> SqliteDataProvider:
    return SqliteDataProvider(url="sqlite:///:memory:", create_schema=True)


def _make_run_dir(tmp_path: Path, run_date: str) -> Path:
    d = tmp_path / "runs" / run_date
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# carryforward_days mechanics
# ---------------------------------------------------------------------------


def test_carryforward_increments_on_first_skip(sqlite: SqliteDataProvider, tmp_path: Path) -> None:
    """carry_forward_units() increments carryforward_days from 0 to 1."""
    with sqlite.transaction():
        sqlite.property_state.upsert(PROP_ID, {"proj_name": "Carry Test"}, BASELINE_DATE)
        sqlite.unit_state.upsert_units(
            PROP_ID, [{"unit_id": UNIT_ID, "market_rent_low": 1500}], BASELINE_DATE
        )

    carried = sqlite.unit_state.carry_forward_units(PROP_ID, RUN_DATE)

    assert len(carried) == 1
    assert carried[0]["unit_id"] == UNIT_ID
    assert carried[0]["carryforward_days"] == 1


def test_carryforward_increments_again_on_second_skip(sqlite: SqliteDataProvider, tmp_path: Path) -> None:
    """Two carry-forward calls accumulate carryforward_days to 2."""
    with sqlite.transaction():
        sqlite.property_state.upsert(PROP_ID, {}, BASELINE_DATE)
        sqlite.unit_state.upsert_units(
            PROP_ID, [{"unit_id": UNIT_ID, "market_rent_low": 1500}], BASELINE_DATE
        )

    sqlite.unit_state.carry_forward_units(PROP_ID, RUN_DATE)
    carried2 = sqlite.unit_state.carry_forward_units(PROP_ID, RUN_DATE_2)

    assert carried2[0]["carryforward_days"] == 2


def test_fresh_upsert_resets_carryforward(sqlite: SqliteDataProvider, tmp_path: Path) -> None:
    """After a fresh upsert, carryforward_days resets to 0."""
    with sqlite.transaction():
        sqlite.property_state.upsert(PROP_ID, {}, BASELINE_DATE)
        sqlite.unit_state.upsert_units(
            PROP_ID, [{"unit_id": UNIT_ID, "market_rent_low": 1500}], BASELINE_DATE
        )
    # One carry-forward pass
    sqlite.unit_state.carry_forward_units(PROP_ID, RUN_DATE)

    # Fresh upsert on run D+1 resets the counter
    with sqlite.transaction():
        sqlite.unit_state.upsert_units(
            PROP_ID, [{"unit_id": UNIT_ID, "market_rent_low": 1550}], RUN_DATE_2
        )

    stored = sqlite.unit_state.get_units(PROP_ID)
    entry = stored[UNIT_ID]
    # UnitIndexEntry is a Pydantic model — access via attribute, not dict.get()
    assert getattr(entry, "carryforward_days", 0) == 0


def test_carryforward_does_not_duplicate_units(sqlite: SqliteDataProvider, tmp_path: Path) -> None:
    """carry_forward_units must not create duplicate unit rows."""
    with sqlite.transaction():
        sqlite.property_state.upsert(PROP_ID, {}, BASELINE_DATE)
        sqlite.unit_state.upsert_units(
            PROP_ID, [{"unit_id": UNIT_ID, "market_rent_low": 1500}], BASELINE_DATE
        )

    sqlite.unit_state.carry_forward_units(PROP_ID, RUN_DATE)
    sqlite.unit_state.carry_forward_units(PROP_ID, RUN_DATE)  # called twice on same date

    stored = sqlite.unit_state.get_units(PROP_ID)
    assert len(stored) == 1


def test_run_report_reflects_carried_property(sqlite: SqliteDataProvider, tmp_path: Path) -> None:
    """run_report built after carry-forward still counts the property as present."""
    run_dir = _make_run_dir(tmp_path, RUN_DATE)

    with sqlite.transaction():
        sqlite.property_state.upsert(PROP_ID, {}, BASELINE_DATE)
        sqlite.unit_state.upsert_units(
            PROP_ID, [{"unit_id": UNIT_ID, "market_rent_low": 1500}], BASELINE_DATE
        )
    carried = sqlite.unit_state.carry_forward_units(PROP_ID, RUN_DATE)

    # Build a minimal properties list as the runner would produce after carry-forward
    props_for_report = [
        {
            "property_id": PROP_ID,
            "units": carried,
            "_meta": {"verdict": "SKIPPED", "scrape_tier_used": "SKIPPED"},
        }
    ]
    (run_dir / "properties.json").write_text(json.dumps(props_for_report), encoding="utf-8")

    report = build_run_report(props_for_report, run_dir, RUN_DATE)

    assert report["totals"]["properties"] == 1
    assert report["run_date"] == RUN_DATE
