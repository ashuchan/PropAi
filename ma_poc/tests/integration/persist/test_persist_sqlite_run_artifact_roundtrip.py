"""Persist layer: SqliteDataProvider run artifact roundtrip.

Verifies that property state and unit state written through SqliteDataProvider
are re-read correctly; row counts match expected values.
"""

from __future__ import annotations

from typing import Any

import pytest

from data_provider import SqliteDataProvider


RUN_DATE = "2026-05-10"


@pytest.fixture
def sqlite() -> SqliteDataProvider:
    return SqliteDataProvider(url="sqlite:///:memory:", create_schema=True)


# ---------------------------------------------------------------------------
# property_state roundtrip
# ---------------------------------------------------------------------------


def test_property_state_upsert_and_get(sqlite: SqliteDataProvider) -> None:
    """Property state written via upsert is retrieved via get()."""
    snap = {"proj_name": "River Walk", "website": "https://example.com", "city": "Austin"}
    with sqlite.transaction():
        sqlite.property_state.upsert("SQLITE-001", snap, RUN_DATE)

    entry = sqlite.property_state.get("SQLITE-001")

    assert entry is not None
    assert entry.canonical_id == "SQLITE-001"
    assert entry.proj_name == "River Walk"


def test_property_state_new_flag_on_first_upsert(sqlite: SqliteDataProvider) -> None:
    """First upsert returns True (new), second returns False (update)."""
    snap = {"proj_name": "Test Property"}
    with sqlite.transaction():
        is_new = sqlite.property_state.upsert("SQLITE-002", snap, RUN_DATE)
    assert is_new is True

    with sqlite.transaction():
        is_new2 = sqlite.property_state.upsert("SQLITE-002", snap, RUN_DATE)
    assert is_new2 is False


def test_property_state_exists(sqlite: SqliteDataProvider) -> None:
    """exists() returns False before upsert, True after."""
    assert sqlite.property_state.exists("SQLITE-003") is False

    with sqlite.transaction():
        sqlite.property_state.upsert("SQLITE-003", {}, RUN_DATE)

    assert sqlite.property_state.exists("SQLITE-003") is True


# ---------------------------------------------------------------------------
# unit_state roundtrip
# ---------------------------------------------------------------------------


def test_unit_state_upsert_and_get_units(sqlite: SqliteDataProvider) -> None:
    """Units written via upsert_units are returned by get_units()."""
    units = [
        {"unit_id": "U1", "bedrooms": 1, "market_rent_low": 1500},
        {"unit_id": "U2", "bedrooms": 2, "market_rent_low": 1800},
    ]
    with sqlite.transaction():
        sqlite.property_state.upsert("SQLITE-004", {}, RUN_DATE)
        sqlite.unit_state.upsert_units("SQLITE-004", units, RUN_DATE)

    stored = sqlite.unit_state.get_units("SQLITE-004")

    assert len(stored) == 2
    assert "U1" in stored
    assert "U2" in stored


def test_unit_state_row_count_matches_input(sqlite: SqliteDataProvider) -> None:
    """Row count in unit_state matches the number of units upserted."""
    units = [{"unit_id": f"U{i}", "bedrooms": 1} for i in range(5)]
    with sqlite.transaction():
        sqlite.property_state.upsert("SQLITE-005", {}, RUN_DATE)
        diff = sqlite.unit_state.upsert_units("SQLITE-005", units, RUN_DATE)

    stored = sqlite.unit_state.get_units("SQLITE-005")
    assert len(stored) == 5


# ---------------------------------------------------------------------------
# profile roundtrip
# ---------------------------------------------------------------------------


def test_profile_put_and_get(sqlite: SqliteDataProvider) -> None:
    """ScrapeProfile saved via profiles.put() is re-read by profiles.get()."""
    from models.scrape_profile import ScrapeProfile

    profile = ScrapeProfile(canonical_id="SQLITE-006")
    profile.navigation.winning_page_url = "https://example.com/floor-plans"

    sqlite.profiles.put(profile)
    loaded = sqlite.profiles.get("SQLITE-006")

    assert loaded is not None
    assert loaded.canonical_id == "SQLITE-006"
    assert loaded.navigation.winning_page_url == "https://example.com/floor-plans"
