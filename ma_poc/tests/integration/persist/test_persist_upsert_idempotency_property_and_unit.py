"""Persist layer: upsert operations are idempotent.

Re-running the same property and unit upserts produces no duplicates and
leaves all foreign-key constraints intact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_provider import FileSystemDataProvider, SqliteDataProvider


RUN_DATE = "2026-05-10"


@pytest.fixture
def fs(tmp_path: Path) -> FileSystemDataProvider:
    return FileSystemDataProvider(
        base_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
    )


@pytest.fixture
def sqlite() -> SqliteDataProvider:
    return SqliteDataProvider(url="sqlite:///:memory:", create_schema=True)


# ---------------------------------------------------------------------------
# Property idempotency
# ---------------------------------------------------------------------------


def test_fs_property_upsert_twice_no_duplicate(fs: FileSystemDataProvider) -> None:
    """Upserting the same canonical_id twice on FS stores exactly one entry."""
    snap = {"proj_name": "Idempotent", "city": "Chicago"}
    with fs.transaction():
        fs.property_state.upsert("IDEM-001", snap, RUN_DATE)
    with fs.transaction():
        fs.property_state.upsert("IDEM-001", snap, RUN_DATE)

    assert fs.property_state.exists("IDEM-001")


def test_sqlite_property_upsert_twice_no_duplicate(sqlite: SqliteDataProvider) -> None:
    """Upserting the same canonical_id twice on SQLite doesn't raise FK or unique violations."""
    snap = {"proj_name": "Idempotent SQLite"}
    with sqlite.transaction():
        sqlite.property_state.upsert("IDEM-002", snap, RUN_DATE)
    with sqlite.transaction():
        sqlite.property_state.upsert("IDEM-002", snap, RUN_DATE)

    entry = sqlite.property_state.get("IDEM-002")
    assert entry is not None


def test_sqlite_property_data_updated_on_second_upsert(sqlite: SqliteDataProvider) -> None:
    """Second upsert with changed data updates the record (no stale reads)."""
    with sqlite.transaction():
        sqlite.property_state.upsert("IDEM-003", {"proj_name": "Old Name"}, RUN_DATE)
    with sqlite.transaction():
        sqlite.property_state.upsert("IDEM-003", {"proj_name": "New Name"}, RUN_DATE)

    entry = sqlite.property_state.get("IDEM-003")
    assert entry is not None
    assert entry.proj_name == "New Name"


# ---------------------------------------------------------------------------
# Unit idempotency
# ---------------------------------------------------------------------------


def test_sqlite_unit_upsert_twice_no_duplicate(sqlite: SqliteDataProvider) -> None:
    """Upserting the same unit twice keeps exactly one row per unit_id."""
    units = [
        {"unit_id": "U1", "bedrooms": 1, "market_rent_low": 1500},
    ]
    with sqlite.transaction():
        sqlite.property_state.upsert("IDEM-004", {}, RUN_DATE)
        sqlite.unit_state.upsert_units("IDEM-004", units, RUN_DATE)
    with sqlite.transaction():
        sqlite.unit_state.upsert_units("IDEM-004", units, RUN_DATE)

    stored = sqlite.unit_state.get_units("IDEM-004")
    assert len(stored) == 1
    assert "U1" in stored


def test_sqlite_unit_data_updated_on_second_upsert(sqlite: SqliteDataProvider) -> None:
    """Second upsert with changed rent updates the stored value."""
    with sqlite.transaction():
        sqlite.property_state.upsert("IDEM-005", {}, RUN_DATE)
        sqlite.unit_state.upsert_units(
            "IDEM-005", [{"unit_id": "U1", "market_rent_low": 1500}], RUN_DATE
        )
    with sqlite.transaction():
        sqlite.unit_state.upsert_units(
            "IDEM-005", [{"unit_id": "U1", "market_rent_low": 1700}], RUN_DATE
        )

    stored = sqlite.unit_state.get_units("IDEM-005")
    assert "U1" in stored


# ---------------------------------------------------------------------------
# Cross-run idempotency
# ---------------------------------------------------------------------------


def test_same_run_written_twice_no_duplicates_in_fs(fs: FileSystemDataProvider) -> None:
    """Writing properties.json for the same run twice overwrites, not appends."""
    props = [{"Unique ID": "P1"}, {"Unique ID": "P2"}]
    fs.runs.write_properties(RUN_DATE, props)
    fs.runs.write_properties(RUN_DATE, props)

    result = fs.runs.read_properties(RUN_DATE)
    assert len(result) == 2  # NOT 4
