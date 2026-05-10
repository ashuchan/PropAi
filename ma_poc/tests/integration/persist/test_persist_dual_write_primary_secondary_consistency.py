"""Persist layer: DualWriteDataProvider writes reach both primary and secondary.

After a write through DualWriteDataProvider, both the FS primary and the
SQLite secondary should contain the same data. Reads must come from the primary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_provider import DualWriteDataProvider, FileSystemDataProvider, SqliteDataProvider


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


@pytest.fixture
def dual(fs: FileSystemDataProvider, sqlite: SqliteDataProvider) -> DualWriteDataProvider:
    return DualWriteDataProvider(primary=fs, secondary=sqlite)


# ---------------------------------------------------------------------------
# property_state consistency
# ---------------------------------------------------------------------------


def test_property_written_to_both_providers(
    dual: DualWriteDataProvider,
    fs: FileSystemDataProvider,
    sqlite: SqliteDataProvider,
) -> None:
    """A property upserted via dual_provider lands in both FS and SQLite."""
    snap = {"proj_name": "Twin Oaks", "city": "Dallas"}
    with dual.transaction():
        dual.property_state.upsert("DUAL-001", snap, RUN_DATE)

    fs_entry = fs.property_state.get("DUAL-001")
    sqlite_entry = sqlite.property_state.get("DUAL-001")

    assert fs_entry is not None, "FS primary missing after dual write"
    assert sqlite_entry is not None, "SQLite secondary missing after dual write"
    assert fs_entry.proj_name == "Twin Oaks"
    assert sqlite_entry.proj_name == "Twin Oaks"


def test_read_comes_from_primary(
    dual: DualWriteDataProvider,
    fs: FileSystemDataProvider,
    sqlite: SqliteDataProvider,
) -> None:
    """dual.property_state.get() reads from the FS primary, not SQLite."""
    snap = {"proj_name": "Primary Source"}
    with dual.transaction():
        dual.property_state.upsert("DUAL-002", snap, RUN_DATE)

    # Read via dual interface — must match FS (the primary)
    got = dual.property_state.get("DUAL-002")
    fs_got = fs.property_state.get("DUAL-002")

    assert got is not None
    assert got.canonical_id == fs_got.canonical_id


# ---------------------------------------------------------------------------
# unit_state consistency
# ---------------------------------------------------------------------------


def test_units_written_to_both_providers(
    dual: DualWriteDataProvider,
    fs: FileSystemDataProvider,
    sqlite: SqliteDataProvider,
) -> None:
    """Units upserted via dual_provider appear in both providers."""
    snap = {"proj_name": "Lakeview"}
    units = [
        {"unit_id": "U1", "bedrooms": 1, "market_rent_low": 1500},
        {"unit_id": "U2", "bedrooms": 2, "market_rent_low": 1900},
    ]
    with dual.transaction():
        dual.property_state.upsert("DUAL-003", snap, RUN_DATE)
        dual.unit_state.upsert_units("DUAL-003", units, RUN_DATE)

    fs_units = fs.unit_state.get_units("DUAL-003")
    sqlite_units = sqlite.unit_state.get_units("DUAL-003")

    assert len(fs_units) == 2
    assert len(sqlite_units) == 2
    assert set(fs_units.keys()) == set(sqlite_units.keys())
