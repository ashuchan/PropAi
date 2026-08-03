from __future__ import annotations

import pytest

from ma_poc.pms.property_scope import apply_collection_scope


@pytest.mark.parametrize(
    ("property_id", "keep", "drop"),
    [
        ("264077", "Flats - A1", "Rise - A1"),
        ("78783", "Link A2", "Canvas A2"),
        ("98191", "Timber | B1", "Meredith House | B1"),
    ],
    ids=("novi-flats", "link-480", "timber"),
)
def test_verified_collection_keeps_only_configured_subcommunity(
    property_id: str, keep: str, drop: str
) -> None:
    rows, dropped = apply_collection_scope(
        [{"floor_plan_name": keep}, {"floor_plan_name": drop}],
        property_id=property_id,
        tier="TIER_1_API_RENTCAFE_SECURECAFE",
    )
    assert [row["floor_plan_name"] for row in rows] == [keep]
    assert dropped == 1


@pytest.mark.parametrize("property_id", ["52", "1000", "999999"])
def test_unconfigured_collection_is_unchanged(property_id: str) -> None:
    source = [{"floor_plan_name": "North A1"}, {"floor_plan_name": "South B1"}]
    rows, dropped = apply_collection_scope(
        source,
        property_id=property_id,
        tier="TIER_1_API_RENTCAFE_SECURECAFE",
    )
    assert rows == source
    assert dropped == 0

def test_configured_collection_fails_closed_when_no_row_matches() -> None:
    rows, dropped = apply_collection_scope(
        [{"floor_plan_name": "Rise - A1"}],
        property_id="264077",
        tier="TIER_1_API_RENTCAFE_SECURECAFE",
    )
    assert rows == []
    assert dropped == 1


def test_rule_does_not_touch_unrelated_provider() -> None:
    source = [{"floor_plan_name": "Rise - A1"}]
    rows, dropped = apply_collection_scope(
        source,
        property_id="264077",
        tier="TIER_1_API_SIGHTMAP",
    )
    assert rows == source
    assert dropped == 0
